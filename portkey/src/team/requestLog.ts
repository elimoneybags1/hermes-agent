// [team] Durable request log. Part of the Team fork, see PROVENANCE.md at the repo root.
//
// The open-source gateway only streams logs to the console over SSE. When
// $PORTKEY_REQUEST_LOG is set, one JSON line per /v1 request is appended there:
// time, config name, trace id, metadata, status, latency, the provider and
// model that answered, token counts, and one entry per attempt. Never the
// prompt or the answer text.
import { appendFile } from 'fs/promises';
import { Context } from 'hono';
import { HEADER_KEYS } from '../globals';
import { CONFIG_NAME, CONFIG_NAME_KEY } from './configByName';

const MAX_ERROR = 300;

function parseMetadata(raw: string | undefined) {
  if (!raw) return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return raw.slice(0, MAX_ERROR);
  }
}

async function finalBody(c: Context, attempts: any[]): Promise<any> {
  if (attempts[0]?.requestParams?.stream) return undefined;
  try {
    return await c.res.clone().json();
  } catch {
    return undefined;
  }
}

/** The line for this request, from the context the gateway already keeps. */
export async function requestLogLine(c: Context, startedAt: number) {
  const attempts: any[] = c.get('requestOptions') ?? [];
  const last = attempts[attempts.length - 1];
  // Taken before the first await: the upstream console log replaces each
  // attempt's `response` with the parsed body once it runs.
  const perAttempt = attempts.map((a) => ({
    provider: a?.providerOptions?.provider ?? null,
    model: a?.providerOptions?.overrideParams?.model ?? null,
    status: a?.response?.status ?? null,
    latency_ms: a?.executionTime ?? null,
  }));
  const header = c.req.header(HEADER_KEYS.CONFIG);
  const body = await finalBody(c, attempts);
  const line: Record<string, any> = {
    time: new Date(startedAt).toISOString(),
    config:
      c.get(CONFIG_NAME_KEY as any) ??
      (header && CONFIG_NAME.test(header) ? header : null),
    trace_id: c.req.header(HEADER_KEYS.TRACE_ID) ?? null,
    metadata: parseMetadata(c.req.header(HEADER_KEYS.METADATA)) ?? null,
    method: c.req.method,
    path: c.req.path,
    status: c.res.status,
    latency_ms: Date.now() - startedAt,
    provider: last?.providerOptions?.provider ?? null,
    model_requested: last?.finalUntransformedRequest?.body?.model ?? null,
    provider_model: last?.providerOptions?.overrideParams?.model ?? null,
    model_answered: body?.model ?? null,
    prompt_tokens: body?.usage?.prompt_tokens ?? null,
    completion_tokens: body?.usage?.completion_tokens ?? null,
    stream: Boolean(attempts[0]?.requestParams?.stream),
    attempts: perAttempt,
  };
  if (c.res.status >= 400) {
    const message = body?.message ?? body?.error?.message ?? body?.error;
    line.error =
      typeof message === 'string'
        ? message.slice(0, MAX_ERROR)
        : message
          ? JSON.stringify(message).slice(0, MAX_ERROR)
          : null;
  }
  return line;
}

/** Append the line for this request to $PORTKEY_REQUEST_LOG; a no-op without it. */
export async function teamRequestLog(c: Context, startedAt: number) {
  const file = process.env.PORTKEY_REQUEST_LOG;
  if (!file || !c.req.path.startsWith('/v1/')) return;
  try {
    const line = await requestLogLine(c, startedAt);
    await appendFile(file, JSON.stringify(line) + '\n', { mode: 0o600 });
  } catch (e: any) {
    console.error('[team] request log:', e?.message ?? e);
  }
}
