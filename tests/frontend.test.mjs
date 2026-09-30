import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { NextRequest } from 'next/server.js';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

let server;
let summary;
let watchlist;
let client;
let backend;
let concepts;

before(async () => {
  server = await createServer({
    configFile: false,
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    resolve: { alias: { '@': resolve(import.meta.dirname, '..') } },
    appType: 'custom',
    logLevel: 'error',
    oxc: { jsx: { runtime: 'automatic' } },
  });
  [summary, watchlist, client, backend, concepts] = await Promise.all([
    server.ssrLoadModule('/lib/strategy-summary.ts'),
    server.ssrLoadModule('/lib/watchlist-summary.ts'),
    server.ssrLoadModule('/lib/client-api.ts'),
    server.ssrLoadModule('/lib/backend.ts'),
    server.ssrLoadModule('/components/concept-recommendations.tsx'),
  ]);
});

after(async () => { await server?.close(); });

test('superseded reads cannot overwrite newer watchlist mutations even if cancellation arrives late', async () => {
  const { createRequestGate } = await server.ssrLoadModule('/lib/request-gate.ts');
  const gate = createRequestGate();
  const read = gate.begin();
  const mutation = gate.begin();
  let stocks = ['new stock'];
  await Promise.resolve().then(() => { if (read.isCurrent()) stocks = ['stale stock']; });
  assert.deepEqual(stocks, ['new stock']);
  assert.equal(read.signal.aborted, true);
  assert.equal(mutation.isCurrent(), true);
  gate.cancel();
  assert.equal(mutation.isCurrent(), false);
});

test('JSON request deadline includes a stalled response body and preserves explicit cancellation', async (t) => {
  t.mock.method(globalThis, 'fetch', async (_url, { signal }) => ({
    ok: true, status: 200,
    text: () => new Promise((_resolve, reject) => {
      if (signal.aborted) reject(signal.reason);
      else signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    }),
  }));
  await assert.rejects(client.jsonFetch('/slow-body', { timeoutMs: 20 }), /请求等待超时/);
  const controller = new AbortController();
  const request = client.jsonFetch('/cancel', { signal: controller.signal });
  controller.abort();
  await assert.rejects(request, cause => client.isAbortError(cause));
});

test('shared numeric formatters never show missing or nonfinite data as zero', async () => {
  const format = await server.ssrLoadModule('/lib/format.ts');
  for (const value of [null, undefined, NaN, Infinity]) {
    assert.equal(format.percent(value), '—');
    assert.equal(format.amount(value), '—');
    assert.equal(format.tone(value), '');
  }
  assert.equal(format.percent(0), '+0.00%');
  assert.equal(format.percent(-3.5), '-3.50%');
  assert.equal(format.timestamp('invalid'), '时间暂缺');
});

test('daily digest formats market units and dates and distinguishes completed and tracking feedback', async () => {
  const { DailyDigestContent } = await server.ssrLoadModule('/components/daily-digest.tsx');
  const html = renderToStaticMarkup(createElement(DailyDigestContent, {
    refresh() {}, navigate() {}, data: {
      asOf: '2026-09-20T12:00:00+08:00', quoteDate: '20260918',
      market: { tradeDate: '20260918', updatedAt: '2026-09-20T10:00:00+08:00', snapshot: { breadth: 78.6, turnover: 2092915000000 }, warnings: ['测试行情缺口'] },
      watchMovers: [{ tsCode: '688981.SH', symbol: '688981', name: '中芯国际', quote: { pctChg: -2.85 } }],
      strongBoards: [{ code: 'BK1001', name: '测试概念', pctChg: 4.96, breadth: 85.2 }], sectorAsOf: null, sectorTradeDate: '20260917',
      recentSelections: [{ id: 1, name: '空名单策略', runDate: '2026-09-20', picks: [] },
        { id: 2, name: '测试策略', runDate: '2026-09-19', picks: [{ code: '688981', name: '中芯国际' }] }],
      forecastSummaries: { '15': { sampleCount: 2, averageReturnPct: 9, trackingCount: 3, missingCount: 1 },
        '30': { sampleCount: 0, averageReturnPct: null, trackingCount: 5, missingCount: 0 } },
      note: '不额外调用 AI',
    },
  }));
  for (const text of ['2.09 万亿', '78.6%', '2026-09-18', '2026-09-17', '-2.85%', '+9.00%', '本次未选出股票',
    '等待样本到期', '2 个到期样本', '延伸观察', '测试行情缺口', '查看 688981 个股详情']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('20260918'));
});

test('daily digest missing data never becomes zero returns or zero market breadth', async () => {
  const { DailyDigestContent } = await server.ssrLoadModule('/components/daily-digest.tsx');
  const html = renderToStaticMarkup(createElement(DailyDigestContent, {
    data: null, error: '摘要读取失败', refresh() {}, navigate() {},
  }));
  for (const text of ['摘要读取失败', '尚未取得大盘快照', '暂无可比较行情', '暂无策略名单']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('0.00%'));
  assert.ok(!html.includes('>0.0%'));
  assert.ok(!html.includes('正在读取摘要'));
});

test('market fallback prominently labels old data and preserves the actual quote date', async () => {
  const { MarketOverviewView } = await server.ssrLoadModule('/components/market-overview-view.tsx');
  const data = { tradeDate: '20260921', updatedAt: '2026-09-21T15:10:00+08:00', source: '测试源', stale: true,
    snapshot: { turnover: 100, breadth: 50, advancers: 1, decliners: 1, flat: 0 },
    turnoverHistory: [], fundFlowHistory: [], latestFlow: null, topTurnover: [], warnings: [] };
  const render = () => renderToStaticMarkup(createElement(MarketOverviewView, {
    today: '2026年9月22日', data, loading: false, onRefresh() {},
  }));
  const cached = render();
  assert.ok(cached.includes('2026-09-21'));
  assert.ok(cached.includes('并非最新行情'));
  assert.ok(cached.includes('最近成功快照汇总'));
  assert.ok(!cached.includes('全市场最新快照汇总'));
  data.stale = false;
  data.dataStatus = { quotes: { state: 'fallback' }, turnoverComparison: {}, fundFlow: {} };
  const fallback = render();
  assert.ok(fallback.includes('已自动切换腾讯证券行情'));
  assert.ok(!fallback.includes('并非最新行情'));
});

test('comparison distinguishes attempted source failure from a never-started refresh', async () => {
  const { ComparisonResult } = await server.ssrLoadModule('/components/research-panels.tsx');
  const html = renderToStaticMarkup(createElement(ComparisonResult, { data: {
    reportId: 9, days: 15, status: 'tracking', strongest: null, leaders: [], selected: [],
    coveredCount: 0, totalCount: 456, missingCount: 3, staleCount: 0,
    entryDate: '2026-09-18', exitDate: '2026-09-21', averageSelectedReturn: null,
    scope: '原概念范围', universeAsOf: null, lastCheckedAt: '2026-09-22T08:00:00+08:00',
    message: '概念日线连接中断；缺少 2026-09-18、2026-09-21',
  } }));
  for (const text of ['概念日线连接中断', '最近尝试核对', '缺少日线 3 个概念', '缺失值不会记作 0%']) {
    assert.ok(html.includes(text), text);
  }
  assert.ok(!html.includes('尚无同区间的完整对照行情'));
});

test('THS comparison states its sample scope even when all frozen candidates have data', async () => {
  const { ComparisonResult } = await server.ssrLoadModule('/components/research-panels.tsx');
  const winner = { code: 'THS:886042', name: '同花顺概念', returnPct: 8, entryPrice: 100, exitPrice: 108, url: 'https://q.10jqka.com.cn/gn/' };
  const html = renderToStaticMarkup(createElement(ComparisonResult, { data: {
    reportId: 12, days: 15, status: 'completed', conceptProvider: 'ths', strongest: winner, leaders: [winner], selected: [],
    coveredCount: 24, totalCount: 24, fullCoverage: true, selectedCount: 0,
    entryDate: '2026-09-18', exitDate: '2026-09-30', averageSelectedReturn: null,
    scope: '同花顺公开榜单活跃概念样本（非全市场）', universeAsOf: null,
  } }));
  for (const text of ['同期样本最强概念对照', '非全市场', '比较范围内最强', '+8.00%']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('全市场最强'));
});

test('THS board overview labels sample counts, source and missing fund data', async () => {
  const { SectorConceptView } = await server.ssrLoadModule('/components/sector-concept-view.tsx');
  const board = { kind: 'concept', code: 'THS:886042', name: '同花顺概念', pctChg: 2, amount: 100_000_000,
    breadth: 80, upCount: 8, downCount: 2, mainNetInflow: null, previousAmount: null, amountDelta: null, leaders: [] };
  const html = renderToStaticMarkup(createElement(SectorConceptView, {
    today: '2026-10-01', loading: false, onRefresh() {}, data: {
      tradeDate: '20260930', updatedAt: '2026-10-01T10:00:00+08:00', source: '同花顺公开板块行情（活跃样本）',
      sourceProvider: 'ths', scope: '同花顺活跃概念样本（非全市场）',
      summary: { industryCount: 0, conceptCount: 1, risingIndustryCount: 0, risingConceptCount: 1, topBoard: board, topFundBoard: null },
      industryBoards: [], conceptBoards: [board], turnoverBoards: [board], warnings: [],
    },
  }));
  for (const text of ['样本上涨概念', '样本强度第一', '同花顺活跃概念样本（非全市场）', '2026-09-30']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('东方财富行业板块口径'));
});

test('comparison distinguishes waiting requests from confirmed missing daily bars', async () => {
  const { ComparisonResult } = await server.ssrLoadModule('/components/research-panels.tsx');
  const data = {
    reportId: 9, days: 15, status: 'tracking', strongest: null, leaders: [], selected: [],
    coveredCount: 0, totalCount: 456, waitingCount: 8, missingCount: 0, staleCount: 0,
    entryDate: '2026-09-18', exitDate: '2026-09-29', averageSelectedReturn: null,
    scope: '原概念范围', universeAsOf: null, lastCheckedAt: null,
    message: '请求仍在排队，来源暂不可用',
  };
  const waiting = renderToStaticMarkup(createElement(ComparisonResult, { data }));
  for (const text of ['待取得行情 8 个概念', '请求仍在排队，来源暂不可用', '“核对同期最强”继续读取', '不会额外调用 AI']) assert.ok(waiting.includes(text), text);
  assert.ok(!waiting.includes('缺少日线'));
  assert.ok(!waiting.includes('+0.00%'));
  const mixed = renderToStaticMarkup(createElement(ComparisonResult, { data: { ...data, missingCount: 2 } }));
  assert.ok(mixed.includes('待取得行情 8 个概念'));
  assert.ok(mixed.includes('缺少日线 2 个概念'));
  assert.ok(!mixed.includes('缺少日线 10 个概念'));
});

test('a lost job submission response yields to confirmed new task status, not an old success', async () => {
  const { submissionMessage } = await server.ssrLoadModule('/lib/task-submission.ts');
  const job = { key: 'comparison', name: '同期最强', status: 'succeeded', progress: {},
    startedAt: '2026-10-01T09:00:00+08:00', finishedAt: '2026-10-01T09:02:00+08:00' };
  const failure = { message: '请求等待超时', previousStartedAt: job.startedAt };
  assert.equal(submissionMessage(failure, job.startedAt), failure.message, 'the preceding successful run does not confirm this submission');
  assert.equal(submissionMessage(failure, '2026-10-01T08:00:00+08:00'), failure.message, 'an older late response does not confirm a newer task');
  assert.equal(submissionMessage(failure, '2026-10-01T01:00:00Z'), failure.message, 'the same instant with another timezone is still the preceding run');
  assert.equal(submissionMessage(failure, 'invalid'), failure.message);
  assert.equal(submissionMessage(failure, null), failure.message);
  const accepted = { ...job, status: 'running', startedAt: '2026-10-01T09:10:00+08:00', finishedAt: null };
  assert.equal(submissionMessage(failure, accepted.startedAt), null, 'polling confirms the server accepted the disconnected request');
  assert.equal(submissionMessage(failure, accepted.startedAt), null);
  const failed = { ...accepted, status: 'failed', error: '概念行情来源暂不可用' };
  assert.equal(submissionMessage(failure, failed.startedAt) || failed.error, failed.error, 'the actual task failure replaces the transport error');
  assert.equal(submissionMessage({ message: failure.message, previousStartedAt: null }, accepted.startedAt), null, 'the first job can also be confirmed');
  assert.equal(submissionMessage({ message: failure.message }, job.startedAt), failure.message, 'an unknown initial status is not evidence that an existing task is new');
});

test('fresh successful GETs of a prior task preserve submission failures and the saved forecast', async (t) => {
  const { submissionMessage } = await server.ssrLoadModule('/lib/task-submission.ts');
  const { startPolling } = await server.ssrLoadModule('/lib/polling.ts');
  const before = { status: 'succeeded', startedAt: '2026-10-01T09:00:00+08:00', result: 'saved forecast' };
  const methods = [];
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    methods.push(options.method || 'GET');
    return options.method === 'POST'
      ? new Response('{"error":"服务拒绝启动任务"}', { status: 401 })
      : new Response(JSON.stringify(before));
  });
  let failure;
  try { await client.jsonFetch('/test-forecast', { method: 'POST' }); }
  catch (error) { failure = { message: error.message, previousStartedAt: before.startedAt }; }
  let after;
  let readError = '此前读取失败';
  let stop;
  t.after(() => stop?.());
  await new Promise((resolve, reject) => {
    const visibility = new EventTarget(); visibility.hidden = false;
    stop = startPolling({ visibility, interval: 0,
      read: signal => client.jsonFetch('/test-forecast', { signal }),
      onValue: value => { after = value; readError = null; resolve(); }, onError: reject,
    });
  });
  assert.equal(submissionMessage(failure, after.startedAt), '服务拒绝启动任务');
  assert.equal(after.result, 'saved forecast');
  assert.equal(readError, null, 'only the read error is repaired by reading the preceding run');
  assert.deepEqual(methods, ['POST', 'GET'], 'recovery must not automatically submit paid work again');
});

test('suspending status reads for submission rejects late old results and resumes read-only recovery', async () => {
  const { startPolling } = await server.ssrLoadModule('/lib/polling.ts');
  const visibility = new EventTarget();
  visibility.hidden = false;
  const state = { status: 'succeeded', result: 'saved forecast' };
  let resolveOld;
  let oldSignal;
  const stop = startPolling({ visibility, interval: 0,
    read: signal => { oldSignal = signal; return new Promise(resolve => { resolveOld = resolve; }); },
    onValue: value => Object.assign(state, value), onError: assert.fail,
  });
  stop();
  Object.assign(state, { status: 'running', previousResult: state.result });
  resolveOld({ status: 'succeeded', result: 'saved forecast' });
  await Promise.resolve(); await Promise.resolve();
  assert.equal(oldSignal.aborted, true);
  assert.equal(state.status, 'running', 'an old status read must not re-enable generation after the new task starts');
  assert.equal(state.previousResult, 'saved forecast');
  let resumedReads = 0;
  const stopRecovery = startPolling({ visibility, interval: 0,
    read: async () => { resumedReads += 1; return { status: 'failed', error: '行情暂不可用', previousResult: 'saved forecast' }; },
    onValue: value => Object.assign(state, value), onError: assert.fail,
  });
  await Promise.resolve(); await Promise.resolve();
  stopRecovery();
  assert.equal(resumedReads, 1);
  assert.equal(state.status, 'failed');
  assert.equal(state.previousResult, 'saved forecast', 'recovery preserves the last successful forecast');
  assert.equal(state.error, '行情暂不可用');
});

test('research job requests preserve the job key and block cross-site execution', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(new URL(url).searchParams.get('key'), 'comparison');
    assert.equal(options.method, 'POST');
    return new Response('{"status":"running"}');
  });
  const route = await server.ssrLoadModule('/app/api/research/jobs/route.ts');
  const allowed = await route.POST(new NextRequest('http://localhost:3000/api/research/jobs?key=comparison', { method: 'POST' }));
  assert.equal(allowed.status, 200);
  const blocked = await route.POST(new NextRequest('http://localhost:3000/api/research/jobs?key=comparison', {
    method: 'POST', headers: { origin: 'https://untrusted.test', 'sec-fetch-site': 'cross-site' },
  }));
  assert.equal(blocked.status, 403);
  assert.equal(fetch.mock.callCount(), 1);
});

test('watch notes forward PATCH body and reject cross-site edits', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (_url, options) => {
    assert.equal(options.method, 'PATCH');
    assert.equal(JSON.parse(options.body).note, '关注订单');
    return new Response('{"stocks":[]}');
  });
  const route = await server.ssrLoadModule('/app/api/watchlist/route.ts');
  const result = await route.PATCH(new NextRequest('http://localhost:3000/api/watchlist', {
    method: 'PATCH', body: JSON.stringify({ tsCode: '600519.SH', note: '关注订单' }),
    headers: { 'content-type': 'application/json' },
  }));
  assert.equal(result.status, 200);
  const blocked = await route.PATCH(new NextRequest('http://localhost:3000/api/watchlist', {
    method: 'PATCH', headers: { origin: 'https://untrusted.test' },
  }));
  assert.equal(blocked.status, 403);
  assert.equal(fetch.mock.callCount(), 1);
});

test('hindsight comparison labels partial coverage and exact dates, never calls it prediction accuracy', async () => {
  const { ComparisonResult } = await server.ssrLoadModule('/components/research-panels.tsx');
  const leader = { code: 'BK1002', name: '测试赢家', returnPct: 12, entryPrice: 100, exitPrice: 112, url: 'https://example.org', checkedAt: '2026-09-28T18:00:00+08:00' };
  const data = { reportId: 1, days: 15, strongest: leader, leaders: [leader], status: 'completed',
    coveredCount: 2, totalCount: 3, fullCoverage: false, scope: '预测时冻结的范围', universeAsOf: '2026-09-12T18:00:00+08:00',
    entryDate: '2026-09-14', exitDate: '2026-09-24', averageSelectedReturn: 8, selectedCount: 2,
    selected: [{ code: 'BK1001', name: '测试预测', rank: 2, returnPct: 8, gapPct: -4 }] };
  const html = renderToStaticMarkup(createElement(ComparisonResult, { data }));
  for (const text of ['事后比较', '已覆盖范围最强', '2026-09-14', '2026-09-24', '2/3', '+12.00%', '+8.00%', '-4.00 个百分点', '1/2', '不计入预测命中率']) {
    assert.ok(html.includes(text), text);
  }
  const empty = renderToStaticMarkup(createElement(ComparisonResult, { data: null }));
  assert.ok(empty.includes('尚无同区间的完整对照行情'));
  assert.ok(!empty.includes('0.00%'));
});

test('stock candle chart renders verified OHLC and no fake bars for missing values', async () => {
  const { CandleChart } = await server.ssrLoadModule('/components/stock-detail.tsx');
  const rows = [{ date: '20260917', open: 100, high: 112, low: 99, close: 110, vol: 2500 }];
  const html = renderToStaticMarkup(createElement(CandleChart, { rows }));
  assert.ok(html.includes('20260917 开 100 高 112 低 99 收 110 成交量 2500 股'));
  assert.ok(html.includes('前复权'));
  const missing = renderToStaticMarkup(createElement(CandleChart, { rows: [{ ...rows[0], open: null }] }));
  assert.ok(missing.includes('尚无可展示的完整日线'));
  assert.ok(!missing.includes('<svg'));
});

test('stock charts omit invalid OHLC and never turn invalid volume into a chart coordinate', async () => {
  const { CandleChart } = await server.ssrLoadModule('/components/stock-detail.tsx');
  const bar = { date: '20260917', open: 100, high: 112, low: 99, close: 110, vol: 2500 };
  for (const invalid of [
    { close: null }, { close: NaN }, { close: Infinity }, { low: 101 },
    { high: 105 }, { open: -10 }, { high: 0 },
  ]) {
    const html = renderToStaticMarkup(createElement(CandleChart, { rows: [{ ...bar, ...invalid }] }));
    assert.ok(html.includes('尚无可展示的完整日线'), JSON.stringify(invalid));
    assert.ok(!html.includes('<svg'));
  }
  for (const vol of [NaN, Infinity, -100]) {
    const html = renderToStaticMarkup(createElement(CandleChart, { rows: [{ ...bar, vol }] }));
    assert.ok(html.includes('成交量 缺失 股'));
    assert.ok(!html.includes('NaN') && !html.includes('Infinity'));
    assert.ok(html.includes('<svg'), 'valid prices remain visible when only volume is missing');
  }
});

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

test('proxy cancels an upstream response body when the client disconnects', async (t) => {
  const controller = new AbortController();
  let upstreamSignal;
  let bodyStarted;
  const started = new Promise(resolve => { bodyStarted = resolve; });
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    upstreamSignal = options.signal;
    return { ok: true, status: 200, text: () => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
      bodyStarted();
    }) };
  });
  const pending = backend.forwardToBackend(new NextRequest('http://localhost:3000/api/system', {
    signal: controller.signal,
  }), '/api/system');
  await started;
  controller.abort();
  const response = await pending;
  assert.equal(upstreamSignal.aborted, true);
  assert.equal(response.status, 499);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.match((await response.json()).error, /已取消/);
});

test('proxy never contacts the backend for an already cancelled request', async (t) => {
  const controller = new AbortController();
  controller.abort();
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new Error('must not fetch'); });
  const response = await backend.forwardToBackend(new NextRequest('http://localhost:3000/api/system', {
    signal: controller.signal,
  }), '/api/system');
  assert.equal(response.status, 499);
  assert.equal(fetch.mock.callCount(), 0);
});

test('JSON API boundaries reject empty and primitive successful payloads before rendering', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(''));
  for (const value of ['', 'null', 'true', '1', '"temporarily unavailable"']) {
    fetch.mock.mockImplementation(async () => new Response(value));
    const response = await backend.forwardToBackend(new NextRequest('http://localhost:3000/api/system'), '/api/system');
    assert.equal(response.status, 502, value);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    await assert.rejects(client.jsonFetch('/api/system'), /没有返回数据|无效的数据格式/, value);
  }
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

const conceptResearch = {
  summary: '测试概念近期走势保持相对强势。', tradeDate: '20260911', dataAsOf: '2026-09-11T14:00:00+08:00',
  scope: '最多8个活跃概念候选', warnings: [], concepts: [{
    code: 'BK1001', name: '测试概念', pctChg: 2.5, change5d: 6.2, change10d: null, amount: 120000000,
    mainNetInflow: null, breadth: 75, upCount: 3, downCount: 1, reason: '广度与动量共同走强。', risk: '持续性待观察。',
    strengthStatus: 'recent_strength', historyAsOf: '20260911',
    catalysts: [{ title: '产区天气变化', event: '气象报道出现产区天气风险。', transmission: '天气风险 → 产量预期 → 糖价 → 企业盈利',
      impact: '需要跟踪真实减产幅度。', status: 'reported', sources: [{ id: 'weather:1', kind: 'news', title: '产区天气报道',
        excerpt: '供核对的天气报道摘要', url: 'https://example.org/weather', source: '气象来源', publishedAt: '2026-09-10T10:00:00+08:00' }] }],
    warnings: ['近10个交易日历史不足'], stocks: [{
      code: '600001', name: '测试强势股', price: 12.5, pctChg: 5, amount: 100000000, turnoverRate: null, reason: '成交活跃。',
    }], drivers: [{ kind: 'news', title: '产业资讯', explanation: '仅作资讯线索。', sources: [{
      id: 'news:1', title: '产业新进展', url: 'https://finance.eastmoney.com/a/123.html', source: '东方财富',
      publishedAt: '2026-09-11T10:00:00+08:00', excerpt: '资讯摘要',
    }] }, { kind: 'hypothesis', title: '待验证因素', explanation: '缺少明确证据。', sources: [] }],
  }],
};

test('concept cards render sourced data, missing values and labelled hypotheses', () => {
  const html = renderToStaticMarkup(createElement(concepts.ConceptResearchCards, { research: conceptResearch, today: '2026-09-11' }));
  for (const value of ['今日行情', '+6.20%', '测试强势股', '¥12.50', '1.20 亿', '资讯线索', '待验证推测', '产业新进展']) {
    assert.ok(html.includes(value), value);
  }
  assert.match(html, /近 10 日<\/dt><dd class="">—/);
  assert.match(html, /主力净流入<\/dt><dd class="">—/);
  assert.match(html, /href="https:\/\/finance.eastmoney.com\/a\/123.html"/);
  for (const value of ['现实催化', '影响传导', '产区天气报道', '对该概念的意义', '供核对的天气报道摘要']) assert.ok(html.includes(value));
});

test('history outages render active observation with missing returns and an empty-stock explanation', () => {
  const research = structuredClone(conceptResearch);
  Object.assign(research.concepts[0], { strengthStatus: 'today_active', change5d: null, change10d: null, stocks: [] });
  const html = renderToStaticMarkup(createElement(concepts.ConceptResearchCards, { research, today: '2026-09-11' }));
  assert.ok(html.includes('当日活跃观察'));
  assert.ok(html.includes('近期趋势待确认'));
  assert.ok(html.includes('暂无可核对的同一交易日强势股行情'));
  assert.match(html, /近 5 日<\/dt><dd class="">—/);
});

test('concept cards label an earlier session explicitly instead of calling it today', () => {
  const html = renderToStaticMarkup(createElement(concepts.ConceptResearchCards, { research: conceptResearch, today: '2026-09-12' }));
  assert.ok(html.includes('最近交易日行情'));
  assert.ok(html.includes('2026-09-11'));
  assert.ok(!html.includes('今日行情'));
  assert.ok(!html.includes('今日成交额'));
});

test('concept route blocks cross-site generation before contacting the backend', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => { throw new Error('must not run'); });
  const route = await server.ssrLoadModule('/app/api/market/concepts/ai/route.ts');
  const response = await route.POST(new NextRequest('http://localhost:3000/api/market/concepts/ai?force=true', {
    method: 'POST', headers: { origin: 'https://untrusted.test', 'sec-fetch-site': 'cross-site' },
  }));
  assert.equal(response.status, 403);
  assert.equal(fetch.mock.callCount(), 0);
});

function forecastFixture() {
  const research = structuredClone(conceptResearch);
  research.window = { startDate: '2026-09-13', endDate: '2026-09-27', calendarDays: 15 };
  Object.assign(research.concepts[0], {
    thesis: '结合订单和技术走势观察半个月内的相对强势机会。', conviction: 'low',
    confirmation: '订单确认且上涨广度扩大。', invalidation: '订单取消或价格趋势转弱。',
    technical: { status: 'supported', summary: '技术趋势有待确认。', sources: [] },
    fundamental: { status: 'hypothesis', summary: '盈利转化仍需验证。', sources: [] },
    news: { status: 'supported', summary: '新闻提供产业线索。', sources: research.concepts[0].catalysts[0].sources },
    technicalData: { lastClose: 105, ma5: null, ma10: null, ma20: null, rsi14: null, distanceTo20dHigh: null, amountRatio5d: null, historyAsOf: '20260911' },
    fundamentalData: { coverage: 'valuation_only', sampleSize: 1, note: '未覆盖完整财报。', valuations: [
      { code: '600001', name: '测试强势股', peDynamic: 23.5, pb: null, marketCap: 1000000000 },
    ] },
  });
  return research;
}

test('forecast cards show a dated horizon, three evidence views, stocks and invalidation', async () => {
  const { ForecastResearchCards } = await server.ssrLoadModule('/components/concept-forecast.tsx');
  const research = forecastFixture();
  const html = renderToStaticMarkup(createElement(ForecastResearchCards, { research, today: '2026-09-12' }));
  for (const value of ['2026-09-13', '2026-09-27', '15 个自然日', '最近交易日行情', '技术面', '基本面', '时事新闻',
    '未来半个月的影响', '订单取消', '研究把握度：较低', '测试强势股', '动态 PE 23.50', 'PB —', '产区天气报道']) {
    assert.ok(html.includes(value), value);
  }
  assert.ok(!html.includes('今日行情'));
  assert.match(html, /RSI（14 日）<\/dt><dd>—/);
  research.concepts[0].stocks = [];
  assert.ok(renderToStaticMarkup(createElement(ForecastResearchCards, { research, today: '2026-09-12' })).includes('暂无可核对的同一交易日强势股行情'));
  research.concepts = [];
  assert.ok(renderToStaticMarkup(createElement(ForecastResearchCards, { research, today: '2026-09-12' })).includes('暂不强行给出名单'));
});

test('forecast overview keeps three directions discoverable while focusing one detailed concept', async () => {
  const { ForecastResearchCards } = await server.ssrLoadModule('/components/concept-forecast.tsx');
  const research = forecastFixture();
  research.concepts.push(...[2, 3].map(index => ({ ...structuredClone(research.concepts[0]),
    code: `BK100${index}`, name: `备选概念${index}`, thesis: `仅在选择概念${index}后展示的详细判断`,
  })));
  const html = renderToStaticMarkup(createElement(ForecastResearchCards, { research, today: '2026-09-12' }));
  assert.ok(html.includes('aria-label="切换预测概念"'));
  assert.equal((html.match(/aria-pressed="true"/g) || []).length, 1);
  assert.equal((html.match(/aria-pressed="false"/g) || []).length, 2);
  assert.equal((html.match(/class="forecast-focus-card"/g) || []).length, 1);
  assert.ok(html.includes('备选概念2') && html.includes('备选概念3'));
  assert.ok(!html.includes('仅在选择概念2后展示的详细判断'));
  assert.ok(html.indexOf('核心判断') < html.indexOf('现实影响因素'));
  assert.ok(html.includes('role="tablist"'));
  assert.equal((html.match(/role="tab"/g) || []).length, 3);
  assert.equal((html.match(/role="tabpanel"/g) || []).length, 3);
  assert.equal((html.match(/aria-selected="true"/g) || []).length, 1);
  assert.equal((html.match(/hidden="" class="forecast-evidence-body"/g) || []).length, 2);
  for (const tab of html.matchAll(/role="tab"[^>]*id="([^"]+)" aria-controls="([^"]+)"/g)) {
    assert.ok(html.includes(`id="${tab[2]}" aria-labelledby="${tab[1]}"`));
  }
  assert.ok(html.includes('class="forecast-catalyst" open=""'));
  assert.ok(html.includes('产区天气报道'));
  assert.ok(html.includes('估值快照'));
});

test('forecast task states show actionable recovery and real progress without claiming a result', async () => {
  const { ForecastTaskState } = await server.ssrLoadModule('/components/concept-forecast.tsx');
  const base = { provider: 'glm', model: 'GLM 5.3 MAX', runDate: '2026-09-30', error: null, finishedAt: null };
  const render = (props) => renderToStaticMarkup(createElement(ForecastTaskState, {
    busy: false, submitting: false, requestError: null, refresh() {}, ...props,
  }));
  const failed = render({ current: { ...base, status: 'failed', error: '概念快照日期过旧：2026-09-25' } });
  for (const text of ['本次预测尚未完成', '2026-09-25', '更新板块行情', '重新读取状态', '已保存的预测仍可查看']) assert.ok(failed.includes(text), text);
  const running = render({ busy: true, current: { ...base, status: 'running', stage: '正在核对概念日线', maxRunSeconds: 900, startedAt: '2026-09-30T09:00:00+08:00' } });
  for (const text of ['正在核对概念日线', '15 分钟', '可以切换页面']) assert.ok(running.includes(text), text);
  assert.ok(!running.includes('已完成'));
  assert.ok(!running.includes('更新板块行情'));
  const disconnected = render({ current: null, requestError: '暂时无法连接服务' });
  assert.ok(disconnected.includes('不会再次提交预测'));
  assert.ok(!disconnected.includes('正在读取当天预测'));
});

test('forecast navigation contains prediction and historical feedback modules', async () => {
  const { AppHeader } = await server.ssrLoadModule('/components/app-header.tsx');
  const { ConceptForecast } = await server.ssrLoadModule('/components/concept-forecast.tsx');
  const header = renderToStaticMarkup(createElement(AppHeader, { activeTab: 'forecast', status: null, tradeDate: null }));
  const activeNavigation = header.match(/<button[^>]*aria-current="page"[^>]*>[\s\S]*?<\/button>/g) || [];
  assert.equal(activeNavigation.length, 1);
  assert.match(activeNavigation[0], /<span>概念预测<\/span>/);
  const html = renderToStaticMarkup(createElement(ConceptForecast));
  assert.equal((html.match(/<h1 /g) || []).length, 1);
  assert.equal((html.match(/<h2 /g) || []).length, 1);
  assert.ok(html.includes('历史预测与准确率'));
  assert.ok(html.includes('未来半个月强势概念预测'));
  assert.ok(html.includes('GLM 5.3 MAX'));
  assert.ok(!html.includes('近期强势概念推荐'));
});

test('forecast proxy blocks cross-site paid calls and forwards its own route', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(new URL(url).pathname, '/api/forecast/concepts');
    assert.equal(options.method, 'GET');
    return new Response(JSON.stringify({ status: 'idle' }));
  });
  const route = await server.ssrLoadModule('/app/api/forecast/concepts/route.ts');
  const response = await route.POST(new NextRequest('http://localhost:3000/api/forecast/concepts?force=true', {
    method: 'POST', headers: { origin: 'https://untrusted.test', 'sec-fetch-site': 'cross-site' },
  }));
  assert.equal(response.status, 403);
  assert.equal(fetch.mock.callCount(), 0);
  const read = await route.GET(new NextRequest('http://localhost:3000/api/forecast/concepts'));
  assert.equal(read.status, 200);
  assert.equal(fetch.mock.callCount(), 1);
});

test('feedback cards distinguish averages, hit rates, paired samples and missing values', async () => {
  const { FeedbackSummaryCard } = await server.ssrLoadModule('/components/forecast-history.tsx');
  const summary = { sampleCount: 2, totalCount: 4, trackingCount: 1, missingCount: 1,
    averageReturnPct: 9, positiveRate: 100, averageDrawdownPct: -3,
    benchmarks: { sh000001: { averageExcessPct: 6, sampleCount: 2, outperformRate: 100 },
      sh000688: { averageExcessPct: null, sampleCount: 0, outperformRate: null } } };
  const html = renderToStaticMarkup(createElement(FeedbackSummaryCard, { days: '15', summary }));
  for (const value of ['半个月反馈', '+9.00%', '100.0%', '+6.00 个百分点', '2 个有效概念样本', '0 个配对样本', '1 个到期待补数据']) assert.ok(html.includes(value), value);
  const extended = renderToStaticMarkup(createElement(FeedbackSummaryCard, { days: '30', summary }));
  assert.ok(extended.includes('一个月延伸反馈'));
  assert.ok(!html.includes('NaN'));
});

test('historical concept table shows actual windows and never labels incomplete returns as final', async () => {
  const { HistoryReportTable } = await server.ssrLoadModule('/components/forecast-history.tsx');
  const outcome = { status: 'missing_data', targetDate: '2026-09-27', note: '等待完整行情', returnPct: null,
    entryDate: null, exitDate: null, entryPrice: null, exitPrice: null, maxDrawdownPct: null, maxRisePct: null, maxFallPct: null, benchmarks: {} };
  const entry = { runDate: '2026-09-12', concepts: [{ code: 'BK1001', name: 'MLCC', outcomes: { '15': outcome } }] };
  const html = renderToStaticMarkup(createElement(HistoryReportTable, { entry, days: '15' }));
  for (const value of ['MLCC', '已到期 · 待补数据', '2026-09-27', '阶段表现 · 不计统计', '收盘最大回撤', '等待同区间行情']) assert.ok(html.includes(value), value);
  assert.ok(!html.includes('NaN'));
  assert.ok(!html.includes('+0.00%'));
  entry.concepts = [];
  assert.ok(renderToStaticMarkup(createElement(HistoryReportTable, { entry, days: '15' })).includes('作为观望记录保留'));
});

test('history route is read-only on GET and protects feedback refresh from cross-site writes', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(new URL(url).pathname, '/api/forecast/history');
    assert.equal(new URL(url).searchParams.get('page'), '2');
    assert.equal(options.method, 'GET');
    return new Response(JSON.stringify({ reports: [] }));
  });
  const route = await server.ssrLoadModule('/app/api/forecast/history/route.ts');
  const rejected = await route.POST(new NextRequest('http://localhost:3000/api/forecast/history', {
    method: 'POST', headers: { origin: 'https://untrusted.test', 'sec-fetch-site': 'cross-site' },
  }));
  assert.equal(rejected.status, 403);
  assert.equal(fetch.mock.callCount(), 0);
  assert.equal((await route.GET(new NextRequest('http://localhost:3000/api/forecast/history?page=2'))).status, 200);
  assert.equal(fetch.mock.callCount(), 1);
});

test('history distinguishes missing data from waiting for the first close', async () => {
  const { HistoryReportTable } = await server.ssrLoadModule('/components/forecast-history.tsx');
  const outcome = { status: 'tracking', dataStatus: 'unavailable', targetDate: '2026-09-27',
    note: '交易日历解析失败，请更新服务后重试', returnPct: null, entryDate: null, exitDate: null,
    entryPrice: null, exitPrice: null, maxDrawdownPct: null, maxRisePct: null, maxFallPct: null, benchmarks: {} };
  const entry = { runDate: '2026-09-12', concepts: [{ code: 'BK0976', name: '被动元件概念', outcomes: { '15': outcome } }] };
  const broken = renderToStaticMarkup(createElement(HistoryReportTable, { entry, days: '15' }));
  assert.ok(broken.includes('跟踪中 · 数据待补齐'));
  assert.ok(broken.includes('行情核对未完成'));
  assert.ok(!broken.includes('等待首个交易日'));
  assert.ok(broken.indexOf('交易日历解析失败') < broken.indexOf('<details'), 'reason is visible without expanding details');
  outcome.dataStatus = 'waiting_for_close';
  outcome.note = '观察期从 2026-09-15 开始，等待首个交易日的完整收盘行情';
  const waiting = renderToStaticMarkup(createElement(HistoryReportTable, { entry, days: '15' }));
  assert.ok(waiting.includes('等待首个交易日收盘'));
  assert.ok(waiting.includes('观察期从 2026-09-15 开始'));
  assert.ok(!waiting.includes('行情核对未完成'));
});
