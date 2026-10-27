//! `gateway-rs trace <id>`: one story for one id, joined across LiteLLM's usage.db
//! (`session`) and Portkey's requests.jsonl (`trace_id`), in time order.

use crate::util::home;
use serde_json::Value;
use std::path::Path;

struct Event {
    time: String,
    source: &'static str,
    text: String,
}

fn usage_rows(db: &Path, id: &str) -> Result<Vec<Event>, String> {
    if !db.exists() {
        return Ok(Vec::new());
    }
    let conn = rusqlite::Connection::open_with_flags(db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| format!("{}: {e}", db.display()))?;
    let mut stmt = conn
        .prepare(
            "select ts, \"group\", deployment, model, tokens_in, tokens_out, latency_ms, ok, rate_limited, error \
             from calls where session = ?1 order by id",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([id], |r| {
            let ts: String = r.get(0)?;
            let group: Option<String> = r.get(1)?;
            let dep: Option<String> = r.get(2)?;
            let model: Option<String> = r.get(3)?;
            let tin: Option<i64> = r.get(4)?;
            let tout: Option<i64> = r.get(5)?;
            let lat: Option<i64> = r.get(6)?;
            let ok: i64 = r.get(7)?;
            let rl: i64 = r.get(8)?;
            let err: Option<String> = r.get(9)?;
            let text = if ok == 1 {
                format!(
                    "ok   group {} deployment {} tokens {}/{} {} ms",
                    group.unwrap_or_default(),
                    dep.unwrap_or_default(),
                    tin.unwrap_or(0),
                    tout.unwrap_or(0),
                    lat.unwrap_or(0)
                )
            } else {
                format!(
                    "FAIL group {} deployment {}{} {} ms: {}",
                    group.unwrap_or_default(),
                    dep.unwrap_or_default(),
                    if rl == 1 { " (rate limited)" } else { "" },
                    lat.unwrap_or(0),
                    err.unwrap_or_default().chars().take(120).collect::<String>()
                )
            };
            let _ = model;
            Ok(Event { time: ts.replace("+00:00", "Z"), source: "litellm ", text })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<Vec<_>, _>>().map_err(|e| e.to_string())
}

fn log_lines(log: &Path, id: &str) -> Result<Vec<Event>, String> {
    let Ok(text) = std::fs::read_to_string(log) else { return Ok(Vec::new()) };
    let mut out = Vec::new();
    for line in text.lines() {
        let Ok(v) = serde_json::from_str::<Value>(line) else { continue };
        if v.get("trace_id").and_then(Value::as_str) != Some(id) {
            continue;
        }
        let s = |k: &str| v.get(k).map(|x| match x { Value::String(s) => s.clone(), other => other.to_string() }).unwrap_or_default();
        let attempts: Vec<String> = v
            .get("attempts")
            .and_then(Value::as_array)
            .map(|a| a.iter().map(|x| format!("{} {}", x["provider"].as_str().unwrap_or("?"), x["status"])).collect())
            .unwrap_or_default();
        let mut text = format!(
            "{} {} config {} provider {} model {} tokens {}/{} {} ms attempts [{}]",
            s("status"),
            s("path"),
            s("config"),
            s("provider"),
            s("model_answered"),
            s("prompt_tokens"),
            s("completion_tokens"),
            s("latency_ms"),
            attempts.join(", ")
        );
        if let Some(e) = v.get("error").and_then(Value::as_str) {
            text.push_str(&format!(": {e}"));
        }
        out.push(Event { time: s("time"), source: "portkey ", text });
    }
    Ok(out)
}

pub fn run(id: &str) -> Result<(), String> {
    let state = crate::build::state_dir();
    let mut events = usage_rows(&state.join("litellm").join("usage.db"), id)?;
    events.extend(log_lines(&state.join("portkey").join("requests.jsonl"), id)?);
    if events.is_empty() {
        println!("gateway-rs trace {id}: nothing in usage.db or requests.jsonl under {}", state.display());
        return Ok(());
    }
    events.sort_by(|a, b| a.time.cmp(&b.time));
    for e in &events {
        println!("{} {} {}", e.time, e.source, e.text);
    }
    let _ = home();
    Ok(())
}
