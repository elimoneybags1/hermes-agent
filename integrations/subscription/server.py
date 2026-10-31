"""OpenAI HTTP boundary for the pinned Claude subscription transport.

Hermes -> LiteLLM -> Portkey -> this process -> official Claude CLI.
The transport supplies the caller's tools as inert schemas; Hermes executes them.
"""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import json
from pathlib import Path
import sys
import types

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def load_client(directory: Path):
    """Load the transport without registering a direct Hermes provider."""
    name = "team_subscription_transport"
    package = types.ModuleType(name)
    package.__path__ = [str(directory.resolve())]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(
        f"{name}.directsdk", directory / "directsdk.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.Client


def error(message: str, status: int):
    return JSONResponse({"error": {"message": message, "type": "subscription_error"}}, status)


def create_app(client_factory, token: str, models: list[str]) -> FastAPI:
    if not token:
        raise ValueError("A local subscription bridge token is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            return error("Invalid local bridge token", 401)
        return await call_next(request)

    @app.get("/v1/models")
    async def inventory():
        return {"object": "list", "data": [
            {"id": model, "object": "model", "owned_by": "claude-subscription"}
            for model in models
        ]}

    @app.post("/v1/chat/completions")
    async def completion(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return error("Invalid JSON", 400)
        if not isinstance(body, dict) or body.get("model") not in models:
            return error("Unknown subscription model", 400)
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            return error("messages must be a nonempty list", 400)
        for message in body["messages"]:
            if not isinstance(message, dict):
                return error("Each message must be an object", 400)
            details = (message.get("provider_specific_fields") or {}).get("reasoning_details")
            if details and "reasoning_details" not in message:
                message["reasoning_details"] = details
        # OpenAI clients flatten extra_body onto the HTTP request.
        if "reasoning_effort" in body:
            body["extra_body"] = {**body.get("extra_body", {}),
                                  "reasoning": {"effort": body.pop("reasoning_effort")}}
        client = client_factory()
        try:
            result = await client.create(**body)
        except (ValueError, TypeError, RuntimeError, OSError) as exc:
            client.close()
            status = 400 if isinstance(exc, ValueError) else getattr(exc, "status_code", None) or 502
            return error(str(exc), status)
        except BaseException:
            client.close()
            raise
        if not body.get("stream"):
            client.close()
            return JSONResponse(result.model_dump())

        async def events():
            try:
                async for chunk in result:
                    payload = chunk.model_dump()
                    payload.pop("_response", None)
                    yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                yield f'data: {json.dumps({"error": {"message": str(exc)}})}\n\n'
            finally:
                await result.aclose()
                client.close()

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-dir", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    catalog = json.loads((Path(__file__).parents[1] / "portkey/catalog.json").read_text())
    models = [s["provider_model_id"] for m in catalog["models"] for s in m["served_by"]
              if s["provider"] == "claude-subscription"]
    # Keep the plugin's inherited-environment auth guard: never pass Client(env=...).
    factory = load_client(args.plugin_dir)
    uvicorn.run(create_app(factory, token, models), host="127.0.0.1", port=args.port,
                access_log=False)


if __name__ == "__main__":
    main()
