// [team] Config by name. Part of the Team fork, see PROVENANCE.md at the repo root.
//
// `x-portkey-config: <name>` (not JSON, matching ^[a-z0-9-]+$) is resolved to
// the contents of $PORTKEY_CONFIG_DIR/<name>.json before the request validator
// and the handlers see it, so callers never carry provider keys. Files are
// cached and re-read when their mtime changes. An unknown name is a 400.
//
// Fail closed: every /v1 request must name a config. A missing header (the
// caller's own x-portkey-provider and key) or an inline JSON config is a 400,
// so the gateway only ever calls what build.py generated from the catalog.
import { readFileSync, statSync } from 'fs';
import { join } from 'path';
import { Context, Next } from 'hono';
import { HEADER_KEYS } from '../globals';

export const CONFIG_NAME = /^[a-z0-9-]+$/;
export const CONFIG_NAME_KEY = 'teamConfigName';

const cache = new Map<string, { mtimeMs: number; text: string }>();

/** The config as a single-line JSON string, or undefined when there is no such file. Throws on invalid JSON. */
export function loadNamedConfig(
  name: string,
  dir: string | undefined = process.env.PORTKEY_CONFIG_DIR
): string | undefined {
  if (!dir || !CONFIG_NAME.test(name)) return undefined;
  const file = join(dir, `${name}.json`);
  let mtimeMs: number;
  try {
    mtimeMs = statSync(file).mtimeMs;
  } catch {
    return undefined;
  }
  const hit = cache.get(file);
  if (hit && hit.mtimeMs === mtimeMs) return hit.text;
  // Header values cannot hold newlines, so the file is re-serialised on one line.
  const text = JSON.stringify(JSON.parse(readFileSync(file, 'utf-8')));
  cache.set(file, { mtimeMs, text });
  return text;
}

export function clearConfigCache() {
  cache.clear();
}

function failure(c: Context, message: string) {
  return c.json({ status: 'failure', message }, 400);
}

export const configByName = async (c: Context, next: Next) => {
  const value = c.req.header(HEADER_KEYS.CONFIG);
  if (!value) {
    return failure(
      c,
      `${HEADER_KEYS.CONFIG} must name a config; requests without one are refused`
    );
  }
  if (!CONFIG_NAME.test(value)) {
    return failure(
      c,
      `Inline configs are refused; ${HEADER_KEYS.CONFIG} must be a config name matching ${CONFIG_NAME}`
    );
  }
  if (!process.env.PORTKEY_CONFIG_DIR) {
    return failure(
      c,
      `Config names need PORTKEY_CONFIG_DIR to be set on the gateway`
    );
  }
  let text: string | undefined;
  try {
    text = loadNamedConfig(value);
  } catch (e: any) {
    return failure(c, `Config '${value}' is not valid JSON: ${e.message}`);
  }
  if (text === undefined) {
    return failure(c, `Unknown config name: ${value}`);
  }
  c.req.raw.headers.set(HEADER_KEYS.CONFIG, text);
  c.set(CONFIG_NAME_KEY, value);
  await next();
};
