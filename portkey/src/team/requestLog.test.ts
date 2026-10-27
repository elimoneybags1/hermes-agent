import { Hono } from 'hono';
import { mkdtempSync, readFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { teamRequestLog } from './requestLog';

const dir = mkdtempSync(join(tmpdir(), 'team-log-'));
const file = join(dir, 'requests.jsonl');

function app(attempts: any[], status = 200, body: any = {}) {
  const a = new Hono();
  a.use('*', async (c, next) => {
    const start = Date.now();
    await next();
    await teamRequestLog(c, start);
  });
  a.post('/v1/chat/completions', (c) => {
    if (status === 200) (c as any).set('teamConfigName', 'team-model-x');
    (c as any).set('requestOptions', attempts);
    return c.json(body, status as any);
  });
  a.get('/', (c) => c.text('hey'));
  return a;
}

const attempt = (provider: string, model: string, status: number) => ({
  providerOptions: { provider, overrideParams: { model } },
  finalUntransformedRequest: {
    body: {
      model: 'x',
      messages: [{ role: 'user', content: 'secret prompt' }],
    },
  },
  requestParams: { model, stream: false },
  response: { status },
  executionTime: 12,
});

const lines = () =>
  readFileSync(file, 'utf-8')
    .trim()
    .split('\n')
    .map((l) => JSON.parse(l));

describe('[team] durable request log', () => {
  beforeEach(() => {
    process.env.PORTKEY_REQUEST_LOG = file;
  });

  test('one line per /v1 request with provider, models, tokens, trace id and attempts; no prompt text', async () => {
    const answer = {
      model: 'gemma-4-31b-it',
      usage: { prompt_tokens: 8, completion_tokens: 1 },
      choices: [{ message: { role: 'assistant', content: 'pong' } }],
    };
    const res = await app(
      [
        attempt('openai', 'google/gemma-4-31b-it', 429),
        attempt('openrouter', 'google/gemma-4-31b-it:free', 200),
      ],
      200,
      answer
    ).request('http://localhost/v1/chat/completions', {
      method: 'POST',
      headers: {
        'x-portkey-config': 'team-model-x',
        'x-portkey-trace-id': 'smoke-1',
        'x-portkey-metadata': '{"session":"smoke-1","caller":"litellm"}',
      },
      body: '{}',
    });
    expect(res.status).toBe(200);
    const [line] = lines().slice(-1);
    expect(line).toMatchObject({
      config: 'team-model-x',
      trace_id: 'smoke-1',
      metadata: { session: 'smoke-1', caller: 'litellm' },
      status: 200,
      provider: 'openrouter',
      model_requested: 'x',
      provider_model: 'google/gemma-4-31b-it:free',
      model_answered: 'gemma-4-31b-it',
      prompt_tokens: 8,
      completion_tokens: 1,
      stream: false,
    });
    expect(line.attempts).toEqual([
      {
        provider: 'openai',
        model: 'google/gemma-4-31b-it',
        status: 429,
        latency_ms: 12,
      },
      {
        provider: 'openrouter',
        model: 'google/gemma-4-31b-it:free',
        status: 200,
        latency_ms: 12,
      },
    ]);
    expect(typeof line.latency_ms).toBe('number');
    expect(JSON.stringify(line)).not.toContain('secret prompt');
    expect(JSON.stringify(line)).not.toContain('pong');
  });

  test('a failed request keeps the error message, truncated', async () => {
    await app([], 400, {
      status: 'failure',
      message: 'Unknown config name: x'.padEnd(500, '!'),
    }).request('http://localhost/v1/chat/completions', {
      method: 'POST',
      headers: { 'x-portkey-config': 'x' },
      body: '{}',
    });
    const [line] = lines().slice(-1);
    expect(line.status).toBe(400);
    expect(line.config).toBe('x');
    expect(line.attempts).toEqual([]);
    expect(line.error.length).toBe(300);
  });

  test('requests outside /v1 are not logged, and no file means no-op', async () => {
    const before = lines().length;
    await app([]).request('http://localhost/');
    expect(lines().length).toBe(before);
    delete process.env.PORTKEY_REQUEST_LOG;
    await app([]).request('http://localhost/v1/chat/completions', {
      method: 'POST',
      body: '{}',
    });
    expect(lines().length).toBe(before);
  });
});
