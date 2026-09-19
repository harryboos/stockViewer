'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { percent, timestamp, tone } from '@/components/concept-recommendations';
import { errorMessage, jsonFetch } from '@/lib/client-api';
import type { ForecastResearch } from '@/lib/forecast-types';
import type { BenchmarkCode, FeedbackRefresh, FeedbackSummary, ForecastHistoryData, ForecastOutcome, HistoryEntry, Horizon } from '@/lib/forecast-history-types';
import { ComparisonPanel } from './research-panels';
import { JobButton } from './research-common';

const benchmarks = [['sh000001', '上证指数'], ['sh000688', '科创50']] as const;
const rate = (value: number | null) => value === null ? '—' : `${value.toFixed(1)}%`;
const price = (value: number | null | undefined) => value == null ? '—' : value.toFixed(2);
const excess = (value: number | null | undefined) => value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)} 个百分点`;

export function FeedbackSummaryCard({ days, summary }: { days: Horizon; summary: FeedbackSummary }) {
  return <article className="feedback-summary-card">
    <header><h3>{days === '15' ? '半个月反馈' : '一个月延伸反馈'}</h3><span>{days} 个自然日</span></header>
    <div className="feedback-summary-lead"><strong className={tone(summary.averageReturnPct)}>{percent(summary.averageReturnPct)}</strong><div>已到期平均涨幅<small>{summary.sampleCount} 个有效概念样本</small></div></div>
    <dl className="feedback-stat-grid">
      <div><dt>上涨命中率</dt><dd>{rate(summary.positiveRate)}</dd></div>
      <div><dt>平均收盘最大回撤</dt><dd>{percent(summary.averageDrawdownPct)}</dd></div>
      {benchmarks.map(([code, label]) => <div key={code}><dt>相对{label}平均超额</dt><dd className={tone(summary.benchmarks[code].averageExcessPct)}>{excess(summary.benchmarks[code].averageExcessPct)}</dd><small>跑赢率 {rate(summary.benchmarks[code].outperformRate)} · {summary.benchmarks[code].sampleCount} 个配对样本</small></div>)}
    </dl>
    <p className="concept-ai-data-note">{summary.trackingCount} 个跟踪中 · {summary.missingCount} 个到期待补数据{summary.sampleCount < 20 ? ' · 样本积累中，暂不据此判断稳定性' : ''}</p>
  </article>;
}

function RelativeReturn({ outcome, code }: { outcome: ForecastOutcome; code: BenchmarkCode }) {
  const item = outcome.benchmarks[code];
  return <><strong className={tone(item?.excessPct ?? null)}>{excess(item?.excessPct)}</strong><small>{item?.excessPct == null ? '等待同区间行情' : item.excessPct > 0 ? '跑赢' : item.excessPct < 0 ? '跑输' : '持平'} · 基准 {percent(item?.returnPct ?? null)}</small></>;
}

export function HistoryReportTable({ entry, days }: { entry: HistoryEntry; days: Horizon }) {
  if (entry.concepts.length === 0) return <p className="concept-ai-state">当时未给出预测方向，作为观望记录保留，不产生收益样本。</p>;
  return <div className="feedback-table-scroll" role="region" aria-label={`${entry.runDate} 预测表现，可横向滚动`} tabIndex={0}><table className="feedback-table">
    <caption className="sr-only">{entry.runDate} 预测的 {days} 天实际表现</caption>
    <thead><tr><th scope="col">概念 / 核对状态</th><th scope="col">实际观察区间</th><th scope="col">概念涨幅</th><th scope="col">相对上证指数</th><th scope="col">相对科创50</th></tr></thead>
    <tbody>{entry.concepts.map(concept => {
      const item = concept.outcomes[days];
      return <tr key={concept.code}>
        <th scope="row"><strong>{concept.name}</strong><small>{concept.code}</small><span className={`feedback-status ${item.status}`}>{item.dataStatus === 'unavailable' && item.status === 'tracking' ? '跟踪中 · 数据待补齐' : ({ completed: '已到期 · 已核对', missing_data: '已到期 · 待补数据', tracking: '跟踪中', pending: '尚未开始' })[item.status]}</span></th>
        <td><span>{item.entryDate || (item.dataStatus === 'waiting_for_close' ? '等待首个交易日收盘' : item.dataStatus === 'unavailable' ? '行情核对未完成' : '等待核对收盘行情')}<br />— {item.exitDate || '—'}</span><small>计划到期 {item.targetDate}</small>{(item.returnPct === null || item.dataStatus === 'unavailable') && <small>{item.note}</small>}</td>
        <td><strong className={tone(item.returnPct)}>{percent(item.returnPct)}</strong><small>{item.status === 'completed' ? '到期收益' : '阶段表现 · 不计统计'}</small>
          <details className="feedback-price-detail"><summary>波动与价格依据</summary><p>首日开盘 {price(item.entryPrice)} → 末日收盘 {price(item.exitPrice)}</p><p>期间最高涨幅 {percent(item.maxRisePct)}<br />期间最低涨幅 {percent(item.maxFallPct)}<br />收盘最大回撤 {percent(item.maxDrawdownPct)}</p><p>{item.note}</p>
            {item.url && <a href={item.url} target="_blank" rel="noreferrer">{item.source || '概念行情来源'}</a>}
            {benchmarks.map(([code, label]) => { const source = item.benchmarks[code]; return <p key={code}>{label}：{price(source?.entryPrice)} → {price(source?.exitPrice)}{source?.url && <> · <a href={source.url} target="_blank" rel="noreferrer">{source.source || '行情来源'}</a></>}</p>; })}
            {item.checkedAt && <small>核对于 {timestamp(item.checkedAt)}（北京时间）</small>}
          </details>
        </td>
        <td><RelativeReturn outcome={item} code="sh000001" /></td><td><RelativeReturn outcome={item} code="sh000688" /></td>
      </tr>;
    })}</tbody>
  </table></div>;
}

export function ForecastHistory({ finishedAt, renderResearch }: { finishedAt?: string | null; renderResearch: (research: ForecastResearch) => ReactNode }) {
  const [data, setData] = useState<ForecastHistoryData | null>(null);
  const [days, setDays] = useState<Horizon>('15');
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [report, setReport] = useState<{ id: number; result: ForecastResearch } | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const mutation = useRef<AbortController | null>(null);

  useEffect(() => () => mutation.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        const next = await jsonFetch<ForecastHistoryData>(`/api/forecast/history?page=${page}`, { signal: controller.signal, cache: 'no-store' });
        if (controller.signal.aborted) return;
        setData(next); setError(null);
        timer = setTimeout(read, next.refresh.status === 'running' ? 3000 : 30_000);
      } catch (cause) {
        if (!controller.signal.aborted) { setError(errorMessage(cause, '历史预测读取失败')); timer = setTimeout(read, 10_000); }
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [page, revision, finishedAt]);

  useEffect(() => {
    if (selected === null) return;
    const controller = new AbortController();
    void jsonFetch<{ id: number; result: ForecastResearch }>(`/api/forecast/history/report?id=${selected}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setReport(value); })
      .catch(cause => { if (!controller.signal.aborted) setDetailError(errorMessage(cause, '原始预测读取失败')); });
    return () => controller.abort();
  }, [selected]);

  async function update() {
    if (mutation.current || data?.refresh.status === 'running') return;
    const controller = new AbortController(); mutation.current = controller;
    setSubmitting(true); setError(null);
    try {
      const refresh = await jsonFetch<FeedbackRefresh>('/api/forecast/history', { method: 'POST', signal: controller.signal });
      if (!controller.signal.aborted) { setData(value => value ? { ...value, refresh } : value); setRevision(value => value + 1); }
    } catch (cause) { if (!controller.signal.aborted) setError(errorMessage(cause, '更新实际表现失败')); }
    finally { mutation.current = null; if (!controller.signal.aborted) setSubmitting(false); }
  }

  const busy = submitting || data?.refresh.status === 'running';
  return <section className="concept-ai-section forecast-history-section" aria-labelledby="forecast-history-title" aria-busy={busy}>
    <div className="concept-ai-heading"><div><p className="eyebrow">预测 → 实际表现 → 反馈</p><h2 id="forecast-history-title">历史预测与准确率</h2><p>保留当时的判断，用之后的行情检验，让下一次预测有据可循。</p></div><div className="research-filters"><button className="refresh-button" disabled={busy || !data?.totalReports} onClick={update}>{busy ? '正在核对行情…' : '更新实际表现'}</button><JobButton jobKey="comparison" label="核对同期最强" /></div></div>
    {error && <p className="concept-ai-error" role="alert">{error} <button onClick={() => setRevision(value => value + 1)}>重试</button></p>}
    {!data && !error && <p className="concept-ai-state">正在读取历史预测…</p>}
    {data && <>
      <div className="feedback-summaries">{(['15', '30'] as const).map(horizon => <FeedbackSummaryCard key={horizon} days={horizon} summary={data.summaries[horizon]} />)}</div>
      <details className="feedback-method"><summary>如何计算准确率、收益和反馈？</summary>
        <p>从预测窗口内、发布之后首个交易日的开盘价起算，到窗口末日或此前最近交易日的收盘价结束。半个月为 15 个自然日，一个月为 30 个自然日，均含休市日；30 天用于观察原半个月预测的持续性。盘中行情不作为最终结果。</p>
        <p>每个概念等权计算算术平均，例如 +8% 与 +10% 的平均涨幅为 +9%。上涨命中率是已到期有效样本中收益大于 0 的比例；跑赢率是超额收益大于 0 的比例。超额收益 = 概念涨幅 − 同一实际区间基准涨幅，单位为百分点。</p>
        <p>每个生成日只采用首次成功预测，重生成版本保留但不计入汇总；未选方向记为观望。缺行情不按 0 处理，基准缺失时只排除该项配对统计。重叠区间和重复概念不是独立样本，这些数字也不是资金组合收益。</p>
        <p>期间最高／最低涨幅相对首日开盘计算；最大回撤按每日收盘价和起始开盘价计算。上证指数代表大盘对照，科创50作为科创板对照。历史收益不代表真实交易收益，未计成本。</p>
        <p>已到期的统计与近期案例会提供给下一次 GLM 5.3 MAX，帮助审视追涨、证据不足和催化失效风险；价格结果本身不能证明新闻因果或某项失效条件已经发生。更新反馈只读取行情，不额外调用 AI。</p>
      </details>
      <div className="feedback-list-heading"><div><h3>预测档案</h3><p>{data.forecastDays} 个预测日 · {data.totalReports} 个版本 · {data.abstentionDays} 天观望</p></div><div className="feedback-periods" role="group" aria-label="历史表现观察周期">{(['15', '30'] as const).map(horizon => <button key={horizon} aria-pressed={days === horizon} onClick={() => setDays(horizon)}>{horizon === '15' ? '半个月' : '一个月'}</button>)}</div></div>
      {data.totalReports === 0 && <div className="concept-ai-state"><strong>还没有历史预测</strong><p>首次生成预测后会自动留档，随后跟踪实际行情。满 15 天、30 天且行情完整时，分别计入反馈统计；不会补造过去的预测。</p></div>}
      {data.page === page && data.reports.map(entry => <article className="feedback-report" key={entry.id}>
        <header><div><h4>{entry.runDate} 预测</h4><small>发布于 {timestamp(entry.publishedAt)} · 北京时间 · 版本 #{entry.id}</small></div><span className={`feedback-cohort ${entry.includedInStats ? 'primary' : ''}`}>{entry.includedInStats ? '当日首次 · 计入统计' : '重生成版本 · 不计统计'}</span></header>
        <HistoryReportTable entry={entry} days={days} />
        <ComparisonPanel reportId={entry.id} days={days} />
        <button className="feedback-original-button" aria-expanded={selected === entry.id} onClick={() => { setDetailError(null); setReport(null); setSelected(value => value === entry.id ? null : entry.id); }}>{selected === entry.id ? '收起当时预测' : '查看当时概念数据、强势股与预测依据'}</button>
        {selected === entry.id && <div className="feedback-original"><p className="concept-ai-data-note">以下保留发布时的行情、判断和来源，用于对照当时的预测依据。</p>{detailError ? <p className="concept-ai-error" role="alert">{detailError}</p> : report?.id === entry.id ? renderResearch(report.result) : <p className="concept-ai-state">正在读取当时的原始预测…</p>}</div>}
      </article>)}
      {data.page !== page && <p className="concept-ai-state">正在读取这一页…</p>}
      {data.totalReports > data.pageSize && <nav className="feedback-pagination" aria-label="历史预测分页"><button disabled={page <= 1} onClick={() => { setSelected(null); setPage(value => value - 1); }}>上一页</button><span>第 {page} / {Math.ceil(data.totalReports / data.pageSize)} 页</span><button disabled={page >= Math.ceil(data.totalReports / data.pageSize)} onClick={() => { setSelected(null); setPage(value => value + 1); }}>下一页</button></nav>}
      {data.refresh.error && <p className="concept-ai-error" role="alert">{data.refresh.error}</p>}
      <p className="concept-ai-provider">{data.refresh.finishedAt ? `最近核对 ${timestamp(data.refresh.finishedAt)} · ` : ''}服务运行期间每日 16:20—22:20 按小时核对，启动时补查；较早未完成的记录会分批补查。预测档案长期保留。</p>
    </>}
  </section>;
}
