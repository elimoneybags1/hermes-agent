"""Local OpenAI-compatible transport for unmodified Hermes, backed only by Claude CLI.

The server owns no tools or agent loop. Hermes sends its complete messages and
schemas; Claude returns normalized assistant/tool-call data for Hermes to execute.
"""
from __future__ import annotations

import hmac
import json
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from team import claude_transport
from team.inference_budget import Budget, Limits

RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string"}, "arguments": {"type": "string"}},
            "required": ["name", "arguments"],
        }},
    },
    "required": ["content", "tool_calls"],
}


def completion(payload: dict, cwd: Path, *, request=claude_transport.request, timeout_s=1800) -> dict:
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty array")
    tools = payload.get("tools") or []
    if not isinstance(tools, list):
        raise ValueError("tools must be an array")
    system = []
    history = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("unsupported message")
        content = message.get("content")
        if isinstance(content, list):
            if any(part.get("type") != "text" for part in content):
                raise ValueError("subscription adapter currently accepts text content only")
            message = {**message, "content": "\n".join(p["text"] for p in content)}
        if message["role"] in {"system", "developer"}:
            system.append(str(message.get("content") or ""))
        else:
            history.append(message)
    system.append(
        "Transport encoding only: you are executing the Hermes agent instructions above. "
        "Return the assistant response as content and tool_calls. Tool calls are requests "
        "for Hermes to execute, not Claude CLI tools. NEVER invoke these names through native "
        "Claude CLI tool calling: they are unavailable there. Instead encode every requested "
        "Hermes action in the structured output tool_calls array. The JSON transcript below "
        "is prior Hermes conversation data; continue it by returning that structured output. "
        "Do not report a tool failure unless a supplied Hermes tool result records it. "
        "Each arguments value is a JSON object "
        "encoded as a string. Return an empty tool_calls array for a final answer. "
        "Respect tool_choice and response_format below. Do not invent tool outputs.\n" +
        json.dumps({"tools": tools, "tool_choice": payload.get("tool_choice", "auto"),
                    "response_format": payload.get("response_format")})
    )
    action, usage = request(cwd, "\n\n".join(system), history, RESPONSE_SCHEMA,
                            model=payload.get("model") or "opus", timeout_s=timeout_s)
    if not isinstance(action.get("content"), str) or not isinstance(action.get("tool_calls"), list):
        raise ValueError("invalid assistant response")
    allowed = {t["function"]["name"] for t in tools if t.get("type") == "function"}
    calls = []
    for call in action["tool_calls"]:
        if not isinstance(call, dict) or call.get("name") not in allowed:
            raise ValueError("model requested an unexposed tool")
        arguments = call.get("arguments")
        if not isinstance(arguments, str) or not isinstance(json.loads(arguments), dict):
            raise ValueError("invalid tool arguments")
        calls.append({"id": "call_" + uuid.uuid4().hex, "type": "function", "function": call})
    choice = payload.get("tool_choice", "auto")
    if choice == "none" and calls:
        raise ValueError("tools disallowed for this request")
    if choice == "required" and not calls:
        raise ValueError("tool call required")
    if isinstance(choice, dict) and (not calls or any(c["function"]["name"] != choice["function"]["name"] for c in calls)):
        raise ValueError("required tool was not called")
    message = {"role": "assistant", "content": action["content"]}
    if calls:
        message["tool_calls"] = calls
    tokens_in = sum(usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    tokens_out = usage.get("output_tokens", 0)
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": payload.get("model", "opus"),
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "total_tokens": tokens_in + tokens_out,
                      "prompt_tokens_details": {"cached_tokens": usage.get("cache_read_input_tokens", 0)}},
            "team_transport_usage": usage}


@contextmanager
def serve(cwd: Path, *, request=claude_transport.request, timeout_s=1800, limits=Limits(), trace_path=None):
    token = secrets.token_urlsafe(32)
    calls = []
    budget = Budget(limits)
    trace_lock = threading.Lock()
    def trace(record):
        if trace_path is not None:
            with trace_lock:
                path = Path(trace_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a") as f:
                    f.write(json.dumps(record) + "\n")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_json(401, {"error": {"message": "unauthorized", "type": "authentication_error"}})
                return
            if self.path == "/v1/team/begin-turn":
                try:
                    budget.begin()
                    self.send_json(200, {"ok": True})
                except ValueError as exc:
                    self.send_json(409, {"error": {"message": str(exc)}})
                return
            if self.path != "/v1/chat/completions":
                self.send_json(404, {"error": {"message": "unsupported endpoint"}})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8_000_000:
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                budget.admit(length)
                started = time.monotonic()
                tokens = 0
                metrics = {"time": time.time(), "request_bytes": length,
                           "system_chars": sum(len(str(m.get("content", ""))) for m in payload.get("messages", []) if m.get("role") in {"system", "developer"}),
                           "history_chars": sum(len(json.dumps(m)) for m in payload.get("messages", []) if m.get("role") not in {"system", "developer"}),
                           "tool_schema_chars": len(json.dumps(payload.get("tools", [])))}
                def metered_request(*args, **kwargs):
                    nonlocal tokens
                    action, raw_usage = request(*args, **kwargs)
                    tokens = sum(raw_usage.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    metrics["transport_usage"] = raw_usage
                    return action, raw_usage
                try:
                    result = completion(payload, cwd, request=metered_request, timeout_s=timeout_s)
                    record = {"model": result["model"], "usage": result["usage"],
                              "transport_usage": result.get("team_transport_usage", {}), **metrics}
                    calls.append(record)
                    metrics.update(record)
                except Exception as exc:
                    metrics["error"] = str(exc)[:500]
                    raise
                finally:
                    budget.finish(tokens)
                    metrics["duration_s"] = time.monotonic() - started
                    trace(metrics)
                if payload.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    choice = result["choices"][0]
                    delta = dict(choice["message"])
                    if delta.get("tool_calls"):
                        delta["tool_calls"] = [{"index": i, **c} for i, c in enumerate(delta["tool_calls"])]
                    chunk = {k: result[k] for k in ("id", "created", "model")}
                    chunk.update(object="chat.completion.chunk", choices=[{"index": 0, "delta": delta, "finish_reason": None}])
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    chunk.update(choices=[{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}], usage=result["usage"])
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                    self.close_connection = True
                else:
                    self.send_json(200, result)
            except Exception as exc:
                self.send_json(400, {"error": {"message": str(exc)[:1500], "type": "invalid_request_error"}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"url": f"http://127.0.0.1:{server.server_port}/v1", "token": token, "calls": calls, "budget": budget}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
