//! Small shared helpers: ISO timestamps, the catalog's ports, config-name rules.

use serde_json::{Map, Value};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// `2026-10-27T16:24:57.081Z` from the system clock, without a date crate.
pub fn iso_now() -> String {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    iso_from_millis(d.as_millis() as i64)
}

pub fn iso_from_millis(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let millis = ms.rem_euclid(1000);
    let days = secs.div_euclid(86400);
    let sod = secs.rem_euclid(86400);
    // civil_from_days (Howard Hinnant)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        y,
        m,
        d,
        sod / 3600,
        (sod % 3600) / 60,
        sod % 60,
        millis
    )
}

pub fn is_config_name(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
}

pub struct Ports {
    pub gateway_host: String,
    pub gateway_port: u16,
    pub portkey_port: u16,
    pub litellm_host: String,
    pub litellm_port: u16,
}

pub fn load_catalog(path: &Path) -> Result<Map<String, Value>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let v: Value = serde_json::from_str(&text).map_err(|e| format!("catalog is not JSON: {e}"))?;
    v.as_object().cloned().ok_or_else(|| "catalog is not an object".to_string())
}

pub fn ports(catalog: &Map<String, Value>) -> Result<Ports, String> {
    let g = catalog.get("gateway").and_then(Value::as_object).ok_or("catalog has no gateway")?;
    let l = catalog.get("litellm").and_then(Value::as_object).ok_or("catalog has no litellm")?;
    let port = |m: &Map<String, Value>, k: &str| -> Result<u16, String> {
        m.get(k).and_then(Value::as_u64).and_then(|n| u16::try_from(n).ok()).ok_or(format!("catalog: {k} missing"))
    };
    Ok(Ports {
        gateway_host: g.get("host").and_then(Value::as_str).unwrap_or("127.0.0.1").to_string(),
        gateway_port: port(g, "port")?,
        portkey_port: port(g, "portkey_port")?,
        litellm_host: l.get("host").and_then(Value::as_str).unwrap_or("127.0.0.1").to_string(),
        litellm_port: port(l, "port")?,
    })
}

pub fn home() -> PathBuf {
    PathBuf::from(std::env::var("HOME").expect("HOME"))
}
