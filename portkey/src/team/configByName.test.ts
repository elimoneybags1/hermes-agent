import { Hono } from 'hono';
import { mkdtempSync, writeFileSync, utimesSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  clearConfigCache,
  configByName,
  loadNamedConfig,
} from './configByName';

const dir = mkdtempSync(join(tmpdir(), 'team-configs-'));
const config = {
  strategy: { mode: 'fallback', on_status_codes: [429] },
  targets: [{ provider: 'openai', api_key: 'k1' }],
};
writeFileSync(join(dir, 'team-model-x.json'), JSON.stringify(config, null, 2));
writeFileSync(join(dir, 'broken.json'), '{not json');

function app() {
  const a = new Hono();
  a.use('/v1/*', configByName);
  a.post('/v1/chat/completions', (c) =>
    c.json({
      config: c.req.header('x-portkey-config'),
      name: (c as any).get('teamConfigName'),
    })
  );
  return a;
}

const post = (headers: Record<string, string>) =>
  app().request('http://localhost/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: '{}',
  });

describe('[team] config by name', () => {
  beforeEach(() => {
    process.env.PORTKEY_CONFIG_DIR = dir;
    clearConfigCache();
  });

  test('a name becomes the file contents on one line, and the name is kept on the context', async () => {
    const res = await post({ 'x-portkey-config': 'team-model-x' });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.config).toBe(JSON.stringify(config));
    expect(body.name).toBe('team-model-x');
  });

  test('inline JSON passes through untouched', async () => {
    const inline = JSON.stringify(config);
    const res = await post({ 'x-portkey-config': inline });
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.config).toBe(inline);
    expect(body.name).toBeUndefined();
  });

  test('an unknown name is a 400', async () => {
    const res = await post({ 'x-portkey-config': 'team-model-missing' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as any).message).toMatch(/Unknown config name/);
  });

  test('a file that is not JSON is a 400, not a crash', async () => {
    const res = await post({ 'x-portkey-config': 'broken' });
    expect(res.status).toBe(400);
    expect(((await res.json()) as any).message).toMatch(/not valid JSON/);
  });

  test('no PORTKEY_CONFIG_DIR means every name is a 400', async () => {
    delete process.env.PORTKEY_CONFIG_DIR;
    const res = await post({ 'x-portkey-config': 'team-model-x' });
    expect(res.status).toBe(400);
  });

  test('names outside ^[a-z0-9-]+$ are never looked up', () => {
    expect(loadNamedConfig('../etc/passwd', dir)).toBeUndefined();
    expect(loadNamedConfig('Team-Model-X', dir)).toBeUndefined();
  });

  test('the cache is refreshed when the file mtime changes', () => {
    const file = join(dir, 'team-model-y.json');
    writeFileSync(file, JSON.stringify({ provider: 'openai', api_key: 'a' }));
    utimesSync(file, 1000, 1000);
    expect(loadNamedConfig('team-model-y', dir)).toContain('"a"');
    writeFileSync(file, JSON.stringify({ provider: 'openai', api_key: 'b' }));
    utimesSync(file, 2000, 2000);
    expect(loadNamedConfig('team-model-y', dir)).toContain('"b"');
  });
});
