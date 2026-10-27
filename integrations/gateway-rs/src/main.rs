//! gateway-rs: Team's connection layer between LiteLLM and Portkey, as one binary.
//!
//!   gateway-rs build [--check] [--catalog PATH]   the catalog generator (twin of integrations/portkey/build.py)
//!
//! Paths derive from $HOME (or $TEAM_STATE for the state dir) and from this binary's
//! location inside the repo; nothing is hardcoded.

mod build;

use std::path::PathBuf;

fn default_catalog() -> PathBuf {
    // <repo>/integrations/gateway-rs/target/<profile>/gateway-rs -> <repo>/integrations/portkey/catalog.json
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
    eprintln!("usage: gateway-rs build [--check] [--catalog PATH]");
    std::process::exit(2)
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("build") => {
            let mut check = false;
            let mut catalog = default_catalog();
            let mut it = args[1..].iter();
            while let Some(a) = it.next() {
                match a.as_str() {
                    "--check" => check = true,
                    "--catalog" => catalog = PathBuf::from(it.next().unwrap_or_else(|| usage())),
                    _ => usage(),
                }
            }
            match build::run(&catalog, check) {
                Ok(msg) => println!("gateway-rs {msg}"),
                Err(msg) => {
                    eprintln!("gateway-rs {msg}");
                    std::process::exit(1)
                }
            }
        }
        _ => usage(),
    }
}
