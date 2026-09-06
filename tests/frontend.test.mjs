import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { NextRequest } from 'next/server.js';

let server;
let summary;
let watchlist;
let client;
let backend;

before(async () => {
  server = await createServer({
    configFile: false,
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    resolve: { alias: { '@': resolve(import.meta.dirname, '..') } },
    appType: 'custom',
    logLevel: 'error',
  });
  [summary, watchlist, client, backend] = await Promise.all([
    server.ssrLoadModule('/lib/strategy-summary.ts'),
    server.ssrLoadModule('/lib/watchlist-summary.ts'),
    server.ssrLoadModule('/lib/client-api.ts'),
    server.ssrLoadModule('/lib/backend.ts'),
  ]);
});

after(async () => { await server?.close(); });

test('watchlist excludes missing changes, flat stocks and stale trading dates from gains', () => {
  const values = [
    { tradeDate: '20260907', pctChg: 2 }, { tradeDate: '20260907', pctChg: -1 },
    { tradeDate: '20260907', pctChg: 0 }, { tradeDate: '20260907', pctChg: null },
    { tradeDate: '20260904', pctChg: 10 }, { tradeDate: '20260907', pctChg: NaN }, null,
  ];
  assert.deepEqual(watchlist.summarizeWatchlist(values.map((quote) => ({ quote }))), {
    tradeDate: '20260907', averageChange: 1 / 3, upCount: 1, downCount: 1, flatCount: 1,
  });
});

test('empty watchlist has no fabricated average', () => {
  assert.equal(watchlist.summarizeWatchlist([]).averageChange, null);
});

const pick = { code: '600519', name: '测试', score: 80 };
const rule = { id: 'momentum', name: '动量', runDate: '2026-09-07', picks: [pick] };

test('failed and running AI results never contribute to consensus', () => {
  for (const status of ['failed', 'running', 'pending', 'not_configured']) {
    const result = summary.buildStrategySummary([rule], [
      { provider: 'deepseek', status, result: { picks: [pick] } },
    ], 'AKShare');
    assert.equal(result.consensus.length, 0);
    assert.equal(result.aiCount, 0);
  }
});

test('a successful independent AI result contributes once to consensus', () => {
  const result = summary.buildStrategySummary([rule], [
    { provider: 'deepseek', status: 'succeeded', result: { picks: [pick, pick] } },
  ], 'AKShare');
  assert.equal(result.consensus[0].count, 2);
  assert.equal(result.consensus[0].scoreCount, 2);
});

test('JSON client reports malformed successful responses and backend errors', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>bad gateway</html>', { status: 200 }));
  await assert.rejects(client.jsonFetch('/api/system'), /无法解析/);
  globalThis.fetch = async () => new Response(JSON.stringify({ error: '服务暂不可用' }), { status: 503 });
  await assert.rejects(client.jsonFetch('/api/system'), /服务暂不可用/);
});

test('proxy blocks cross-site paid runs before contacting the backend', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new Error('must not run'); });
  const request = new NextRequest('http://localhost:3000/api/strategies/ai', {
    method: 'POST', headers: { origin: 'https://untrusted.test' },
  });
  const response = await backend.forwardToBackend(request, '/api/strategies/ai', { useServerDailySecret: true });
  assert.equal(response.status, 403);
  assert.equal(fetch.mock.callCount(), 0);
});

test('proxy preserves query strings, method and backend validation errors', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(new URL(url).searchParams.get('q'), '中文');
    assert.equal(options.method, 'GET');
    return new Response(JSON.stringify({ detail: 'invalid' }), { status: 422 });
  });
  const response = await backend.forwardToBackend(
    new NextRequest('http://localhost:3000/api/stocks/search?q=中文'), '/api/stocks/search',
  );
  assert.equal(response.status, 422);
  assert.equal((await response.json()).error, 'invalid');
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(fetch.mock.callCount(), 1);
});

test('proxy distinguishes timeouts from connection failures', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => { throw new DOMException('deadline', 'TimeoutError'); });
  const response = await backend.forwardToBackend(new NextRequest('http://localhost:3000/api/system'), '/api/system');
  assert.equal(response.status, 504);
  assert.match((await response.json()).error, /响应超时/);
});

test('proxy accepts browser same-origin requests behind HTTPS termination', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('{"ok":true}'));
  const response = await backend.forwardToBackend(new NextRequest('http://stock.example/api/watchlist', {
    method: 'POST', headers: { origin: 'https://stock.example', 'sec-fetch-site': 'same-origin' },
  }), '/api/watchlist');
  assert.equal(response.status, 200);
});

test('proxy treats a malformed successful upstream response as an error', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>failure</html>'));
  const response = await backend.forwardToBackend(new NextRequest('http://localhost:3000/api/system'), '/api/system');
  assert.equal(response.status, 502);
});
