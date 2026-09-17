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

test('forecast cards show a dated horizon, three evidence views, stocks and invalidation', async () => {
  const { ForecastResearchCards } = await server.ssrLoadModule('/components/concept-forecast.tsx');
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

test('forecast navigation contains prediction and historical feedback modules', async () => {
  const { AppHeader } = await server.ssrLoadModule('/components/app-header.tsx');
  const { ConceptForecast } = await server.ssrLoadModule('/components/concept-forecast.tsx');
  const header = renderToStaticMarkup(createElement(AppHeader, { activeTab: 'forecast', status: null, tradeDate: null }));
  assert.match(header, /class="nav-item active" aria-current="page">预测/);
  const html = renderToStaticMarkup(createElement(ConceptForecast));
  assert.equal((html.match(/<h2 /g) || []).length, 2);
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
