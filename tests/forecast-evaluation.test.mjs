import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { resolve } from 'node:path';
import { createServer } from 'vite';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

let server;
let ForecastEvaluation;

before(async () => {
  server = await createServer({
    configFile: false, server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    resolve: { alias: { '@': resolve(import.meta.dirname, '..') } },
    appType: 'custom', logLevel: 'error', oxc: { jsx: { runtime: 'automatic' } },
  });
  ({ ForecastEvaluation } = await server.ssrLoadModule('/components/forecast-evaluation.tsx'));
});
after(async () => { await server?.close(); });

function group() {
  return {
    promptVersion: 'forecast-v7-test', conceptProvider: 'ths', horizonDays: 15,
    reportCount: 4, matureReportCount: 3, abstentionCount: 1, abstentionRate: 33.333,
    verifiedReportCount: 1, unverifiedReportCount: 1, nonOverlappingReportCount: 1,
    averageReturnPct: -3, averageSelectedMaxDrawdownPct: -5, drawdownReportCount: 1,
    baselineMissingPlanCount: 2, rankingMissingUniverseCount: 0, calendarWindowMissingCount: 0,
    latestPublishedAt: '2026-10-10T12:00:00+08:00',
    baselines: [{ id: 'daily_momentum', name: '当日涨幅前三', pairedReportCount: 1,
      averageAiReturnPct: -3, averageBaselineReturnPct: 2, averageExcessPct: -5 },
    { id: 'candidate_equal_weight', name: '模型候选等权', pairedReportCount: 0,
      averageAiReturnPct: null, averageBaselineReturnPct: null, averageExcessPct: null }],
    ranking: { reportCount: 1, conceptCount: 3, averagePercentile: 40, top20Rate: 0, averageCoveragePct: 50 },
    missedLeader: { inPoolCount: 1, outsidePoolCount: 0, unknownCount: 0, cases: [
      { reportId: 1, predictedOn: '2026-09-12', entryDate: '2026-09-14', exitDate: '2026-09-25',
        code: 'THS:885955', name: '示例概念', returnPct: 15, candidateAvailability: 'in_pool',
        coveredCount: 50, totalCount: 100, independentSample: false }] },
  };
}

function payload(groups = [group()]) {
  return { asOf: '2026-10-10T12:00:00+08:00', reportLimit: 120, readReportCount: 4,
    skippedReportCount: 0, calendarAvailable: true, sampleUnit: '每日首次成功预测',
    limits: '缺失不按零收益；30日是延伸观察。', groups };
}

test('evaluation shows only selected horizon, strict paired returns, coverage and noncausal attribution', () => {
  const data = payload([group(), { ...group(), horizonDays: 30, promptVersion: 'extended-only-version' }]);
  const html = renderToStaticMarkup(createElement(ForecastEvaluation, { data, days: '15' }));
  for (const text of ['同花顺概念', 'forecast-v7-test', '-3.00%', '-5.00 个百分点', '33.3%', '50.0%',
    '已入池但未选', '核对 50/100 个概念', '不能证明版本改动带来了提升', '不事后补造',
    '并非组合回撤', '不能据此认定催化失效', '互不重叠报告']) assert.ok(html.includes(text), text);
  assert.ok(!html.includes('extended-only-version'));
  assert.ok(!html.includes('+0.00%'));
});

test('missing calendar and old baseline records remain explicit unavailable values', () => {
  const empty = group();
  Object.assign(empty, { verifiedReportCount: 0, averageReturnPct: null, averageSelectedMaxDrawdownPct: null,
    abstentionRate: null, calendarWindowMissingCount: 3 });
  empty.baselines = empty.baselines.map(row => ({ ...row, pairedReportCount: 0,
    averageAiReturnPct: null, averageBaselineReturnPct: null, averageExcessPct: null }));
  const html = renderToStaticMarkup(createElement(ForecastEvaluation, {
    data: { ...payload([empty]), calendarAvailable: false }, days: '15',
  }));
  assert.ok(html.includes('交易日历暂不可用'));
  assert.ok(html.includes('3 份报告缺少覆盖观察期的交易日历'));
  assert.ok(html.includes('2 份到期记录没有可用的冻结基线'));
  assert.ok(html.includes('—'));
  assert.ok(!html.includes('+0.00%'));
});

test('legacy endpoints without evaluation and empty archives do not create a misleading panel', () => {
  assert.equal(renderToStaticMarkup(createElement(ForecastEvaluation, { days: '15' })), '');
  assert.equal(renderToStaticMarkup(createElement(ForecastEvaluation, {
    data: { ...payload([]), readReportCount: 0 }, days: '15',
  })), '');
});
