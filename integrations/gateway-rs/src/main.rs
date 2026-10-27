//! gateway-rs: Team's connection layer between LiteLLM and Portkey, as one binary.
//!
//!   gateway-rs build [--check]     the catalog generator (twin of integrations/portkey/build.py)
//!   gateway-rs proxy               config-by-name resolver + durable request log, in front of Portkey
//!   gateway-rs trace <id>          one id's story across usage.db and requests.jsonl
//!   gateway-rs status              launchd, sockets, drift, last call, for all three processes
//!   gateway-rs ports               shell-evalable host/port lines from the catalog
//!
//! `--catalog PATH` is accepted by every subcommand; the default is found by walking up
//! from the binary to <repo>/integrations/portkey/catalog.json. State is $TEAM_STATE or
//! $HOME/state/team.

mod build;
mod proxy;
mod status;
mod trace;
mod util;

use std::path::PathBuf;

fn default_catalog() -> PathBuf {
    let exe = std::env::current_exe().expect("current_exe");
    let mut dir = exe.parent().map(PathBuf::from).unwrap_or_default();
    for _ in 0..6 {
        let candidate = dir.join("integrations").join("portkey").join("catalog.json");
        if candidate.exists() {
            return candidate;
        }
        match dir.parent() {
            Some(p) => dir = p.to_path_buf(),
            None => break,
        }
    }
    PathBuf::from("integrations/portkey/catalog.json")
}

fn usage() -> ! {
    eprintln!("usage: gateway-rs (build [--check] | proxy | trace <id> | status | ports) [--catalog PATH]");
    std::process::exit(2)
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut catalog = default_catalog();
    if let Some(i) = args.iter().position(|a| a == "--catalog") {
        if i + 1 >= args.len() {
            usage();
        }
        catalog = PathBuf::from(&args[i + 1]);
        args.drain(i..=i + 1);
    }
    let result: Result<(), String> = match args.first().map(String::as_str) {
        Some("build") => {
            let check = match args.get(1).map(String::as_str) {
                None => false,
                Some("--check") => true,
                _ => usage(),
            };
            build::run(&catalog, check).map(|msg| println!("gateway-rs {msg}"))
        }
        Some("proxy") => util::load_catalog(&catalog).and_then(|c| util::ports(&c)).and_then(|p| {
            let state = build::state_dir();
            proxy::run(proxy::Settings {
                listen_host: p.gateway_host,
                listen_port: p.gateway_port,
                upstream_port: p.portkey_port,
                config_dir: Some(
                    std::env::var("PORTKEY_CONFIG_DIR").map(PathBuf::from).unwrap_or(state.join("portkey").join("configs")),
                ),
                log_path: Some(
                    std::env::var("PORTKEY_REQUEST_LOG")
                        .map(PathBuf::from)
                        .unwrap_or(state.join("portkey").join("requests.jsonl")),
                ),
            })
        }),
        Some("trace") => match args.get(1) {
            Some(id) => trace::run(id),
            None => usage(),
        },
        Some("status") => util::load_catalog(&catalog).and_then(|c| util::ports(&c)).and_then(|p| status::run(&catalog, &p)),
        Some("ports") => util::load_catalog(&catalog).and_then(|c| util::ports(&c)).map(|p| {
            println!("GATEWAY_HOST={}", p.gateway_host);
            println!("GATEWAY_PORT={}", p.gateway_port);
            println!("PORTKEY_PORT={}", p.portkey_port);
            println!("LITELLM_HOST={}", p.litellm_host);
            println!("LITELLM_PORT={}", p.litellm_port);
        }),
        _ => usage(),
    };
    if let Err(msg) = result {
        eprintln!("gateway-rs {msg}");
        std::process::exit(1);
    }
}
