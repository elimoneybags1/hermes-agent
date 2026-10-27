//! `gateway-rs proxy`: the Team side of the Portkey connection, in front of an unpatched Portkey.
//!
//! Listens on the catalog's gateway host:port (what LiteLLM talks to), resolves
//! `x-portkey-config: <name>` to the one-line JSON of `$PORTKEY_CONFIG_DIR/<name>.json`
//! (cached by mtime; unknown name → 400 before anything is forwarded), forwards the
//! request to Portkey on 127.0.0.1:<portkey_port>, and appends one JSON line per `/v1`
//! request to `$PORTKEY_REQUEST_LOG`: time, config, trace id, metadata, status, latency,
//! the provider and model that answered, tokens, and the attempts. Never prompt or
//! answer text. Streaming responses pass through chunk by chunk and are logged when
//! their headers arrive (tokens unknown).

use crate::util::{is_config_name, iso_now};
use bytes::Bytes;
use http_body_util::{BodyExt, Full, combinators::BoxBody};
use hyper::body::Incoming;
use hyper::header::{HeaderName, HeaderValue};
use hyper::server::conn::http1;
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::client::legacy::{Client, connect::HttpConnector};
use hyper_util::rt::{TokioExecutor, TokioIo};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Instant, SystemTime};
use tokio::net::TcpListener;

const CONFIG: &str = "x-portkey-config";
const TRACE_ID: &str = "x-portkey-trace-id";
const METADATA: &str = "x-portkey-metadata";
const PROVIDER: &str = "x-portkey-provider";
const LAST_USED: &str = "x-portkey-last-used-option-index";
const MAX_ERROR: usize = 300;

pub struct Settings {
    pub listen_host: String,
    pub listen_port: u16,
    pub upstream_port: u16,
    pub config_dir: Option<PathBuf>,
    pub log_path: Option<PathBuf>,
}

#[derive(Clone)]
struct CachedConfig {
    mtime: SystemTime,
    text: String,
    value: Value,
}

struct State {
    settings: Settings,
    client: Client<HttpConnector, Full<Bytes>>,
    cache: Mutex<HashMap<PathBuf, CachedConfig>>,
}

type Body = BoxBody<Bytes, hyper::Error>;

fn full(b: impl Into<Bytes>) -> Body {
    Full::new(b.into()).map_err(|never| match never {}).boxed()
}

fn failure(status: StatusCode, message: String) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .body(full(json!({"status": "failure", "message": message}).to_string()))
        .unwrap()
}

/// The config as one-line JSON plus its parsed value; None when there is no such file. Err on bad JSON.
fn load_named(state: &State, name: &str) -> Result<Option<CachedConfig>, String> {
    let Some(dir) = &state.settings.config_dir else { return Ok(None) };
    if !is_config_name(name) {
        return Ok(None);
    }
    let file = dir.join(format!("{name}.json"));
    let mtime = match std::fs::metadata(&file).and_then(|m| m.modified()) {
        Ok(t) => t,
        Err(_) => return Ok(None),
    };
    if let Some(hit) = state.cache.lock().unwrap().get(&file) {
        if hit.mtime == mtime {
            return Ok(Some(hit.clone()));
        }
    }
    let raw = std::fs::read_to_string(&file).map_err(|e| e.to_string())?;
    let value: Value = serde_json::from_str(&raw).map_err(|e| e.to_string())?;
    let entry = CachedConfig { mtime, text: value.to_string(), value };
    state.cache.lock().unwrap().insert(file, entry.clone());
    Ok(Some(entry))
}

/// `config.targets[0].targets[1]` → [0, 1]
fn parse_index_path(s: &str) -> Vec<usize> {
    s.split("targets[").skip(1).filter_map(|part| part.split(']').next()?.parse().ok()).collect()
}

fn leaves(v: &Value, out: &mut Vec<Value>) {
    match v.get("targets").and_then(Value::as_array) {
        Some(ts) => ts.iter().for_each(|t| leaves(t, out)),
        None => out.push(v.clone()),
    }
}

/// In a nested fallback config, every leaf that was tried before and including the one at `path`.
fn tried(config: &Value, path: &[usize]) -> Vec<Value> {
    let mut out = Vec::new();
    let mut node = config;
    for &i in path {
        let Some(ts) = node.get("targets").and_then(Value::as_array) else { break };
        for t in ts.iter().take(i) {
            leaves(t, &mut out);
        }
        match ts.get(i) {
            Some(t) => node = t,
            None => return out,
        }
    }
    if node.get("targets").is_none() {
        out.push(node.clone());
    }
    out
}

fn attempt(leaf: &Value, status: Option<u16>) -> Value {
    json!({
        "provider": leaf.get("provider").cloned().unwrap_or(Value::Null),
        "model": leaf.pointer("/override_params/model").cloned().unwrap_or(Value::Null),
        "status": status,
        "latency_ms": Value::Null,
    })
}

fn header_str<'a>(req_headers: &'a hyper::HeaderMap, name: &str) -> Option<&'a str> {
    req_headers.get(name).and_then(|v| v.to_str().ok())
}

struct LogContext {
    time: String,
    started: Instant,
    config_name: Option<String>,
    config: Option<Value>,
    trace_id: Option<String>,
    metadata: Value,
    method: String,
    path: String,
    model_requested: Value,
    stream: bool,
}

fn build_line(ctx: &LogContext, status: u16, resp_headers: &hyper::HeaderMap, body: Option<&Value>) -> Value {
    let last = header_str(resp_headers, LAST_USED).map(parse_index_path);
    let attempts: Vec<Value> = match (&ctx.config, &last) {
        (Some(cfg), Some(path)) => {
            let t = tried(cfg, path);
            let n = t.len();
            t.iter().enumerate().map(|(i, leaf)| attempt(leaf, (i + 1 == n).then_some(status))).collect()
        }
        _ => Vec::new(),
    };
    let last_leaf = attempts.last().cloned().unwrap_or(Value::Null);
    let mut line = json!({
        "time": ctx.time,
        "config": ctx.config_name,
        "trace_id": ctx.trace_id,
        "metadata": ctx.metadata,
        "method": ctx.method,
        "path": ctx.path,
        "status": status,
        "latency_ms": ctx.started.elapsed().as_millis() as u64,
        "provider": header_str(resp_headers, PROVIDER).map(Value::from).unwrap_or(Value::Null),
        "model_requested": ctx.model_requested,
        "provider_model": last_leaf.get("model").cloned().unwrap_or(Value::Null),
        "model_answered": body.and_then(|b| b.get("model")).cloned().unwrap_or(Value::Null),
        "prompt_tokens": body.and_then(|b| b.pointer("/usage/prompt_tokens")).cloned().unwrap_or(Value::Null),
        "completion_tokens": body.and_then(|b| b.pointer("/usage/completion_tokens")).cloned().unwrap_or(Value::Null),
        "stream": ctx.stream,
        "attempts": attempts,
    });
    if status >= 400 {
        let message = body.and_then(|b| {
            b.get("message").or_else(|| b.pointer("/error/message")).or_else(|| b.get("error")).cloned()
        });
        line["error"] = match message {
            Some(Value::String(s)) => Value::from(s.chars().take(MAX_ERROR).collect::<String>()),
            Some(other) => Value::from(other.to_string().chars().take(MAX_ERROR).collect::<String>()),
            None => Value::Null,
        };
    }
    line
}

async fn append_line(path: &Path, line: &Value) {
    use std::os::unix::fs::OpenOptionsExt;
    let mut opts = std::fs::OpenOptions::new();
    opts.append(true).create(true).mode(0o600);
    let text = format!("{line}\n");
    match tokio::task::spawn_blocking({
        let path = path.to_path_buf();
        move || -> std::io::Result<()> {
            use std::io::Write;
            opts.open(path)?.write_all(text.as_bytes())
        }
    })
    .await
    {
        Ok(Ok(())) => {}
        Ok(Err(e)) => eprintln!("gateway-rs proxy: request log: {e}"),
        Err(e) => eprintln!("gateway-rs proxy: request log: {e}"),
    }
}

async fn handle(state: Arc<State>, req: Request<Incoming>) -> Result<Response<Body>, hyper::Error> {
    let started = Instant::now();
    let time = iso_now();
    let (mut parts, body) = req.into_parts();
    let path = parts.uri.path().to_string();
    let is_v1 = path.starts_with("/v1/");

    // Config by name, before anything is forwarded.
    let mut config_name = None;
    let mut config = None;
    if is_v1 {
        if let Some(value) = header_str(&parts.headers, CONFIG).map(str::to_string) {
            if is_config_name(&value) {
                if state.settings.config_dir.is_none() {
                    return Ok(failure(
                        StatusCode::BAD_REQUEST,
                        "Config names need PORTKEY_CONFIG_DIR to be set on the gateway".into(),
                    ));
                }
                match load_named(&state, &value) {
                    Err(e) => {
                        return Ok(failure(StatusCode::BAD_REQUEST, format!("Config '{value}' is not valid JSON: {e}")));
                    }
                    Ok(None) => {
                        let line = json!({
                            "time": time, "config": value, "trace_id": header_str(&parts.headers, TRACE_ID),
                            "metadata": parse_metadata(header_str(&parts.headers, METADATA)),
                            "method": parts.method.as_str(), "path": path, "status": 400,
                            "latency_ms": started.elapsed().as_millis() as u64,
                            "provider": null, "model_requested": null, "provider_model": null, "model_answered": null,
                            "prompt_tokens": null, "completion_tokens": null, "stream": false, "attempts": [],
                            "error": format!("Unknown config name: {value}"),
                        });
                        if let Some(log) = &state.settings.log_path {
                            append_line(log, &line).await;
                        }
                        return Ok(failure(StatusCode::BAD_REQUEST, format!("Unknown config name: {value}")));
                    }
                    Ok(Some(cfg)) => {
                        parts.headers.insert(HeaderName::from_static(CONFIG), HeaderValue::from_str(&cfg.text).unwrap());
                        config_name = Some(value);
                        config = Some(cfg.value);
                    }
                }
            } else if let Ok(v) = serde_json::from_str::<Value>(&value) {
                config = Some(v); // inline JSON: still useful for naming attempts
            }
        }
    }

    let body_bytes = body.collect().await?.to_bytes();
    let (model_requested, stream) = if is_v1 {
        match serde_json::from_slice::<Value>(&body_bytes) {
            Ok(v) => (
                v.get("model").cloned().unwrap_or(Value::Null),
                v.get("stream").and_then(Value::as_bool).unwrap_or(false),
            ),
            Err(_) => (Value::Null, false),
        }
    } else {
        (Value::Null, false)
    };

    let ctx = LogContext {
        time,
        started,
        config_name,
        config,
        trace_id: header_str(&parts.headers, TRACE_ID).map(str::to_string),
        metadata: parse_metadata(header_str(&parts.headers, METADATA)),
        method: parts.method.to_string(),
        path: path.clone(),
        model_requested,
        stream,
    };

    // Forward to Portkey.
    let pq = parts.uri.path_and_query().map(|p| p.as_str().to_string()).unwrap_or(path);
    let uri: hyper::Uri = format!("http://127.0.0.1:{}{}", state.settings.upstream_port, pq).parse().unwrap();
    let mut upstream = Request::builder().method(parts.method.clone()).uri(uri);
    for (k, v) in parts.headers.iter() {
        if k != "host" && k != "content-length" {
            upstream = upstream.header(k, v);
        }
    }
    let upstream = upstream.body(Full::new(body_bytes)).unwrap();
    let resp = match state.client.request(upstream).await {
        Ok(r) => r,
        Err(e) => {
            let msg = format!("Portkey on 127.0.0.1:{} is not answering: {e}", state.settings.upstream_port);
            if is_v1 {
                if let Some(log) = &state.settings.log_path {
                    let mut line = build_line(&ctx, 502, &hyper::HeaderMap::new(), None);
                    line["error"] = Value::from(msg.clone());
                    append_line(log, &line).await;
                }
            }
            return Ok(failure(StatusCode::BAD_GATEWAY, msg));
        }
    };
    let (rparts, rbody) = resp.into_parts();
    let status = rparts.status.as_u16();
    let streaming = stream
        || header_str(&rparts.headers, "content-type").map(|c| c.starts_with("text/event-stream")).unwrap_or(false);

    if streaming {
        if is_v1 {
            if let Some(log) = &state.settings.log_path {
                let line = build_line(&ctx, status, &rparts.headers, None);
                append_line(log, &line).await;
            }
        }
        return Ok(Response::from_parts(rparts, rbody.boxed()));
    }

    let bytes = rbody.collect().await?.to_bytes();
    if is_v1 {
        if let Some(log) = &state.settings.log_path {
            let parsed = serde_json::from_slice::<Value>(&bytes).ok();
            let line = build_line(&ctx, status, &rparts.headers, parsed.as_ref());
            append_line(log, &line).await;
        }
    }
    let mut out = Response::from_parts(rparts, full(bytes));
    out.headers_mut().remove("transfer-encoding");
    Ok(out)
}

fn parse_metadata(raw: Option<&str>) -> Value {
    match raw {
        None => Value::Null,
        Some(s) => serde_json::from_str(s).unwrap_or_else(|_| Value::from(s.chars().take(MAX_ERROR).collect::<String>())),
    }
}

pub fn run(settings: Settings) -> Result<(), String> {
    let rt = tokio::runtime::Runtime::new().map_err(|e| e.to_string())?;
    rt.block_on(async move {
        let addr = format!("{}:{}", settings.listen_host, settings.listen_port);
        let listener = TcpListener::bind(&addr).await.map_err(|e| format!("cannot bind {addr}: {e}"))?;
        let client: Client<HttpConnector, Full<Bytes>> = Client::builder(TokioExecutor::new()).build_http();
        eprintln!(
            "gateway-rs proxy: listening on {addr}, forwarding to 127.0.0.1:{}, configs {}, log {}",
            settings.upstream_port,
            settings.config_dir.as_ref().map(|p| p.display().to_string()).unwrap_or("(unset)".into()),
            settings.log_path.as_ref().map(|p| p.display().to_string()).unwrap_or("(unset)".into()),
        );
        let state = Arc::new(State { settings, client, cache: Mutex::new(HashMap::new()) });
        loop {
            let (stream, _) = listener.accept().await.map_err(|e| e.to_string())?;
            let st = state.clone();
            tokio::spawn(async move {
                let io = TokioIo::new(stream);
                let svc = service_fn(move |req| handle(st.clone(), req));
                if let Err(e) = http1::Builder::new().serve_connection(io, svc).await {
                    let s = e.to_string();
                    if !s.contains("connection closed") && !s.contains("IncompleteMessage") {
                        eprintln!("gateway-rs proxy: {s}");
                    }
                }
            });
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn index_path_and_tried_leaves() {
        assert_eq!(parse_index_path("config.targets[0].targets[1]"), vec![0, 1]);
        assert_eq!(parse_index_path("config"), Vec::<usize>::new());
        let cfg = json!({"strategy": {"mode": "fallback"}, "targets": [
            {"strategy": {"mode": "fallback"}, "targets": [
                {"provider": "openai", "override_params": {"model": "a1"}},
                {"provider": "groq", "override_params": {"model": "a2"}}]},
            {"strategy": {"mode": "fallback"}, "targets": [
                {"provider": "google", "override_params": {"model": "b1"}}]}]});
        let t = tried(&cfg, &[1, 0]);
        let models: Vec<&str> = t.iter().map(|l| l.pointer("/override_params/model").unwrap().as_str().unwrap()).collect();
        assert_eq!(models, vec!["a1", "a2", "b1"]);
        let t = tried(&cfg, &[0, 1]);
        let models: Vec<&str> = t.iter().map(|l| l.pointer("/override_params/model").unwrap().as_str().unwrap()).collect();
        assert_eq!(models, vec!["a1", "a2"]);
    }
}
