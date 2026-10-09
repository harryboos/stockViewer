import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

let server;
let components;
before(async () => {
  server = await createServer({
    configFile: false, server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    resolve: { alias: { '@': resolve(import.meta.dirname, '..') } },
    appType: 'custom', logLevel: 'error', oxc: { jsx: { runtime: 'automatic' } },
  });
  components = await server.ssrLoadModule('/components/forecast-risk.tsx');
});
after(async () => { await server?.close(); });
const render = (name, props) => renderToStaticMarkup(createElement(components[name], props));
const concepts = [{ code: 'BK1001', name: '测试方向甲' }, { code: 'BK1002', name: '测试方向乙' }];

function relativeFixture() {
  return { historyAsOf: '2026-09-18', benchmarks: [
    { code: 'sh000001', name: '上证指数', periods: {
      '5d': { status: 'available', startDate: '2026-09-11', endDate: '2026-09-18', excessPctPoints: 3.14, reason: null },
      '10d': { status: 'unavailable', startDate: null, endDate: null, excessPctPoints: null, reason: '不足同日期价格观测' },
    } },
    { code: 'sh000688', name: '科创50', periods: {
      '5d': { status: 'available', startDate: '2026-09-11', endDate: '2026-09-18', excessPctPoints: -2.35, reason: null },
      '10d': { status: 'available', startDate: '2026-09-04', endDate: '2026-09-18', excessPctPoints: 0, reason: null },
    } },
  ] };
}

function riskFixture() {
  const pair = { leftCode: 'BK1001', rightCode: 'BK1002',
    correlation20d: { status: 'available', value: .987, startDate: '2026-09-04', endDate: '2026-09-18',
      returnCount: 10, requestedReturnCount: 20, smallSample: true, reason: '配对不足20个，仅供观察' },
    strongStockSampleOverlap: { leftSampleSize: 2, rightSampleSize: 3, sharedCount: 1, sharedCodes: ['600001'],
      overlapOfSmallerSamplePct: 50, jaccardPct: 25 },
    sharedNewsSources: { count: 1, leftSourceCount: 2, rightSourceCount: 1, urls: ['https://example.com/news'] } };
  return { asOf: '2026-09-18', pairs: [pair], limits: ['双方同时缺失的交易日仍可能无法识别。'] };
}

test('discovery coverage distinguishes snapshot, available histories and final research count', () => {
  const html = render('ForecastDiscoveryCoverage', { selection: {
    snapshotCount: 456, availableCount: 450, historyCheckedCount: 48, historyAlignedCount: 44, selectedCount: 12,
    note: '有限范围，历史未读取不代表没有机会。',
  } });
  for (const text of ['456 个快照', '48 个已取得历史', '12 个进入研究', '48 / 44 个', '有限范围']) {
    assert.ok(html.includes(text), text);
  }
  const legacy = render('ForecastDiscoveryCoverage', { selection: { availableCount: 23, selectedCount: 12 } });
  assert.ok(legacy.includes('未知 个已取得历史'));
  assert.ok(!legacy.includes('0 个已取得历史'));
});

test('relative returns show percentage points and the actual historical window', () => {
  const html = render('ForecastRelativeStrength', { data: relativeFixture(), name: '测试方向甲' });
  for (const text of ['相对上证指数', '相对科创50', '+3.14 个百分点', '-2.35 个百分点', '0.00 个百分点',
    '2026-09-11 → 2026-09-18', '2026-09-04 → 2026-09-18', '未知', '不足同日期价格观测', '不保证末日已收盘']) {
    assert.ok(html.includes(text), text);
  }
});

test('unavailable or nonfinite relative returns never become zero or stale displayed values', () => {
  const data = relativeFixture();
  for (const benchmark of data.benchmarks) {
    benchmark.periods['5d'] = { status: 'unavailable', excessPctPoints: 12.34, startDate: null, endDate: null };
    benchmark.periods['10d'] = { status: 'available', excessPctPoints: NaN, startDate: null, endDate: null };
  }
  const html = render('ForecastRelativeStrength', { data, name: '测试' });
  assert.ok(html.includes('未知'));
  for (const text of ['12.34', 'NaN', '0.00 个百分点']) assert.ok(!html.includes(text), text);
});

test('risk disclosure limits comparison to selected concepts and marks sample limits', () => {
  const context = riskFixture();
  context.pairs.push({ ...context.pairs[0], rightCode: 'BK9999', correlation20d: { ...context.pairs[0].correlation20d, value: -.76 } });
  const html = render('ForecastCommonRisk', { context, concepts });
  for (const text of ['测试方向甲 / 测试方向乙', '相关性 0.99', '10 个日收益配对', '短样本', '1 只',
    '50.0%', '并非全体成份股重合', '1 个页面', '不等于同一事件', 'https://example.com/news']) {
    assert.ok(html.includes(text), text);
  }
  assert.ok(!html.includes('-0.76'));
  assert.ok(!html.includes('BK9999'));
});

test('missing stocks, news or correlation are unknown and unsafe links are not rendered', () => {
  const context = riskFixture();
  const pair = context.pairs[0];
  pair.correlation20d.status = 'unavailable';
  pair.correlation20d.value = NaN;
  pair.strongStockSampleOverlap = { leftSampleSize: 0, rightSampleSize: 3, sharedCount: null,
    sharedCodes: [], jaccardPct: null, overlapOfSmallerSamplePct: null };
  pair.sharedNewsSources = { count: 0, leftSourceCount: 0, rightSourceCount: 2,
    urls: ['javascript:alert(1)', 'https://user:password@example.com/private'] };
  const html = render('ForecastCommonRisk', { context, concepts });
  assert.ok(html.includes('相关性 未知'));
  assert.ok(html.includes('占较小样本 未知'));
  for (const text of ['NaN', '0.0%', '0 个页面', 'href="javascript', 'user:password']) assert.ok(!html.includes(text), text);
});

test('a checked zero source overlap is distinct from no news samples', () => {
  const context = riskFixture();
  context.pairs[0].sharedNewsSources = { count: 0, leftSourceCount: 1, rightSourceCount: 2, urls: [] };
  const html = render('ForecastCommonRisk', { context, concepts });
  assert.ok(html.includes('0 个页面'));
  assert.ok(html.includes('不代表催化独立'));
});

test('legacy forecasts and one-direction results do not require new optional risk data', () => {
  assert.equal(render('ForecastDiscoveryCoverage', {}), '');
  assert.equal(render('ForecastRelativeStrength', { name: '历史概念' }), '');
  assert.equal(render('ForecastCommonRisk', { concepts }), '');
  assert.equal(render('ForecastCommonRisk', { context: riskFixture(), concepts: concepts.slice(0, 1) }), '');
  const html = render('ForecastCommonRisk', { context: { ...riskFixture(), pairs: [] }, concepts });
  assert.ok(html.includes('共同风险未知'));
});
