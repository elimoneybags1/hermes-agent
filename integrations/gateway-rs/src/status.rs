//! `gateway-rs status`: what is actually running, from launchd, the sockets, the
//! generated files and the logs. Exit 1 when anything is off.

use crate::build;
use crate::util::Ports;
use serde_json::Value;
use std::path::Path;
use std::process::Command;

fn sh(cmd: &str, args: &[&str]) -> String {
    Command::new(cmd)
        .args(args)
        .output()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .unwrap_or_default()
}

fn launchd(label: &str) -> Option<(String, String)> {
    let uid = sh("id", &["-u"]).trim().to_string();
    let out = sh("launchctl", &["print", &format!("gui/{uid}/{label}")]);
    if out.is_empty() {
        return None;
    }
    let field = |k: &str| {
        out.lines()
            .find_map(|l| l.trim().strip_prefix(&format!("{k} = ")).map(str::to_string))
            .unwrap_or_else(|| "?".into())
    };
    Some((field("state"), field("pid")))
}

fn listener(port: u16) -> Option<String> {
    let out = sh("lsof", &["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN"]);
    out.lines().nth(1).and_then(|l| l.split_whitespace().nth(8).map(str::to_string))
}

fn http_ok(url: &str) -> bool {
    Command::new("curl")
        .args(["-sf", "-o", "/dev/null", "--max-time", "3", url])
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn last_request(log: &Path) -> String {
    let Ok(text) = std::fs::read_to_string(log) else { return "none logged yet".into() };
    let n = text.lines().count();
    match text.lines().last().and_then(|l| serde_json::from_str::<Value>(l).ok()) {
        Some(v) => format!(
            "{n} lines; last: {} {} {} {} {} trace {}",
            v["time"].as_str().unwrap_or("?"),
            v["status"],
            v["config"].as_str().unwrap_or("null"),
            v["provider"].as_str().unwrap_or("null"),
            v["model_answered"].as_str().unwrap_or("null"),
            v["trace_id"].as_str().unwrap_or("null")
        ),
        None => format!("{n} lines"),
    }
}

fn last_call(db: &Path) -> String {
    if !db.exists() {
        return "none yet".into();
    }
    let Ok(conn) = rusqlite::Connection::open_with_flags(db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY) else {
        return "unreadable".into();
    };
    let n: i64 = conn.query_row("select count(*) from calls", [], |r| r.get(0)).unwrap_or(0);
    let last = conn
        .query_row(
            "select ts, ok, \"group\", deployment, session, latency_ms from calls order by id desc limit 1",
            [],
            |r| {
                Ok(format!(
                    "{} ok={} {} {} session {} {} ms",
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    r.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                    r.get::<_, Option<i64>>(5)?.unwrap_or(0)
                ))
            },
        )
        .unwrap_or_default();
    format!("{n} calls; last: {last}")
}

pub fn run(catalog_path: &Path, ports: &Ports) -> Result<(), String> {
    let state = build::state_dir();
    let mut ok = true;
    let mut line = |label: &str, text: String, good: bool| {
        println!("{label:<9}{text}");
        if !good {
            ok = false;
        }
    };

    for (label, port, url) in [
        ("com.fullmoney.team-portkey", ports.portkey_port, format!("http://127.0.0.1:{}/", ports.portkey_port)),
        ("com.fullmoney.team-gateway", ports.gateway_port, format!("http://127.0.0.1:{}/", ports.gateway_port)),
        ("com.fullmoney.team-litellm", ports.litellm_port, format!("http://127.0.0.1:{}/health/liveliness", ports.litellm_port)),
    ] {
        match launchd(label) {
            Some((state, pid)) => line("launchd", format!("{label}: {state} pid {pid}"), state == "running"),
            None => line("launchd", format!("{label}: not loaded"), false),
        }
        match listener(port) {
            Some(sock) => line("listen", sock.clone(), sock.starts_with("127.0.0.1:")),
            None => line("listen", format!("nothing on :{port}"), false),
        }
        let up = http_ok(&url);
        line("http", format!("GET {url} {}", if up { "ok" } else { "failed" }), up);
    }
    match build::run(catalog_path, true) {
        Ok(msg) => line("configs", msg, true),
        Err(msg) => line("configs", msg, false),
    }
    line("requests", last_request(&state.join("portkey").join("requests.jsonl")), true);
    line("usage.db", last_call(&state.join("litellm").join("usage.db")), true);
    if ok { Ok(()) } else { Err("status: something is off".into()) }
}
