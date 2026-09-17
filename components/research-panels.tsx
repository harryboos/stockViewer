'use client';

import { useState } from 'react';
import { useResearch } from '@/lib/use-research';
import type { ConceptComparison, Operations, Rotation, SelectionHistory, DailyDigestData } from '@/lib/research-types';
import { percent, timestamp, tone, amount } from './concept-recommendations';
import { JobButton, ResearchError } from './research-common';
import { StockLink } from './stock-detail';
import { jsonFetch, errorMessage } from '@/lib/client-api';

const diff = (value: number | null | undefined) => value == null ? '—' : `${value > 0 ? '+' : ''}${value.toFixed(2)} 个百分点`;
const statusText: Record<string, string> = { idle: '未开始', pending: '待生成', running: '运行中', succeeded: '已完成', failed: '失败', not_configured: '未配置' };
const operationsPolling = { active: 5000, idle: 30000, isActive: (data: Operations) => [...data.jobs, ...data.aiRuns].some(job => job.status === 'running') };

export function RotationPanel() {
  const { data, error, refresh } = useResearch<Rotation>('/api/research/rotation', 12_000);
  const [days, setDays] = useState('5');
  const [query, setQuery] = useState('');
  const sorted = [...(data?.items || [])].filter(row => `${row.name}${row.code}`.includes(query)).sort((a, b) => a.ranks[days] - b.ranks[days]);
  return <section className="research-panel"><header className="research-heading"><div><p className="eyebrow">从单日热点到持续趋势</p><h2>板块轮动</h2><p>比较完整交易日涨幅，排名变化相对 5 个交易日前，采用同一已覆盖概念范围。</p></div><JobButton jobKey="rotation" label="更新轮动数据" /></header>
    <ResearchError error={error} retry={refresh} /><div className="research-filters"><div className="feedback-periods">{['5', '10', '20'].map(value => <button key={value} aria-pressed={days === value} onClick={() => setDays(value)}>{value} 个交易日</button>)}</div><input placeholder="搜索概念名称" aria-label="搜索轮动概念" value={query} onChange={event => setQuery(event.target.value)} /></div>
    <p className="data-note">截至 {data?.asOf || '等待数据'} · 已核对 {data?.coveredCount ?? 0}/{data?.totalCount ?? 0} 个概念 · 部分覆盖时排名仅代表已核对范围，缺失值不按 0 计算。</p>
    {sorted.length ? <div className="research-table-wrap"><table className="research-table"><thead><tr><th>概念 / 排名</th><th>{days} 日涨幅</th><th>排名升降</th><th>近 26 日走势</th><th>最新上涨广度</th></tr></thead><tbody>{sorted.map(row => {
      const min = Math.min(...row.path.map(point => point.close)); const max = Math.max(...row.path.map(point => point.close));
      const points = row.path.map((point, index) => `${index / Math.max(1, row.path.length - 1) * 140},${35 - (point.close - min) / (max - min || 1) * 30}`).join(' ');
      return <tr key={row.code}><th>{row.ranks[days]} · {row.name}<small>{row.code}</small></th><td className={tone(row.returns[days])}>{percent(row.returns[days])}</td><td>{row.rankChanges[days] > 0 ? '↑' : row.rankChanges[days] < 0 ? '↓' : '—'} {Math.abs(row.rankChanges[days]) || ''}</td><td><svg viewBox="0 0 145 40" className="rotation-sparkline" role="img" aria-label={`${row.name} 近26日收盘走势`}><polyline points={points} fill="none" stroke="currentColor" strokeWidth="2" /></svg></td><td>{row.snapshot ? `${row.snapshot.breadth.toFixed(1)}%` : '—'}</td></tr>;
    })}</tbody></table></div> : <p className="concept-ai-state">尚无匹配的完整日线。可更新数据，后台会每批核对最多 40 个概念。</p>}
    {data?.snapshotAsOf && <p className="data-note">广度快照：{timestamp(data.snapshotAsOf)}；与左侧历史窗口分别标注。</p>}
  </section>;
}

export function SelectionHistoryPanel() {
  const [sessions, setSessions] = useState(10); const [strategy, setStrategy] = useState(''); const [page, setPage] = useState(1);
  const { data, error, refresh } = useResearch<SelectionHistory>(`/api/research/selections?sessions=${sessions}&page=${page}&strategy=${encodeURIComponent(strategy)}`, 12_000);
  return <section className="research-panel"><header className="research-heading"><div><p className="eyebrow">规则策略与 AI 的共同成绩单</p><h2>历史选股表现</h2><p>升级后固定每天首次成功选股，从实际发布后首个交易日开盘开始观察。</p></div><JobButton jobKey="selections" label="更新历史成绩" /></header>
    <ResearchError error={error} retry={refresh} /><div className="research-filters"><div className="feedback-periods">{[5, 10, 20].map(value => <button key={value} aria-pressed={sessions === value} onClick={() => { setSessions(value); setPage(1); }}>{value} 个交易日</button>)}</div><select aria-label="筛选历史策略" value={strategy} onChange={event => { setStrategy(event.target.value); setPage(1); }}><option value="">全部策略</option>{data?.summaries.map(row => <option key={row.key} value={row.key}>{row.name}</option>)}</select></div>
    {!!data?.summaries.length && <div className="research-table-wrap"><table className="research-table"><thead><tr><th>策略</th><th>到期样本</th><th>平均涨幅</th><th>上涨比例</th><th>平均最大回撤</th><th>超额：上证 / 科创50</th></tr></thead><tbody>{data.summaries.filter(row => !strategy || row.key === strategy).map(row => <tr key={row.key}><th>{row.name}<small>{row.days} 个选股日 · {row.trackingCount} 个跟踪中</small></th><td>{row.sampleCount}</td><td className={tone(row.averageReturnPct)}>{percent(row.averageReturnPct)}</td><td>{percent(row.positiveRate)}</td><td>{percent(row.averageDrawdownPct)}</td><td>{diff(row.benchmarks.sh000001.averageExcessPct)} / {diff(row.benchmarks.sh000688.averageExcessPct)}<small>配对样本 {row.benchmarks.sh000001.sampleCount} / {row.benchmarks.sh000688.sampleCount}</small></td></tr>)}</tbody></table></div>}
    {!data?.total && <p className="concept-ai-state">暂无已保存的选股档案；完成规则策略或 AI 选股后自动留档，不补造历史名单。</p>}
    <div className="research-records">{data?.entries.map(entry => <details key={entry.id}><summary>{entry.runDate} · {entry.name} · {entry.picks.length} 只</summary><small>真实发布于 {timestamp(entry.publishedAt)} · {entry.origin === 'retained_cache' ? '升级前留存版本，无法确认是否当日首版' : '当日首次成功名单'}</small>{entry.picks.map(pick => <div className="selection-pick" key={pick.code}><StockLink code={pick.code}>{pick.name} {pick.code}</StockLink><strong className={tone(pick.outcome.returnPct ?? null)}>{percent(pick.outcome.returnPct ?? null)}</strong><span>{pick.outcome.status === 'completed' ? '已到期' : '跟踪 / 等待数据'} · {pick.outcome.entryDate || '—'} → {pick.outcome.exitDate || '—'}</span><p>{pick.outcome.note}</p><small>{pick.reason}</small></div>)}</details>)}</div>
    {!!data && data.total > 10 && <div className="feedback-pagination"><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>{page} / {Math.ceil(data.total / 10)}</span><button disabled={page * 10 >= data.total} onClick={() => setPage(value => value + 1)}>下一页</button></div>}
    <p className="data-note">升级前缓存按其实际保存版本与发布时间评估，不能恢复已被覆盖的首版。仅到期且数据完整的个股样本计入均值，等权统计；重叠样本不独立。前复权价格表现未计手续费、滑点、涨跌停成交限制，不是实际组合收益。缺失或停牌期间不拼接成完整窗口。</p>
  </section>;
}

export function ComparisonPanel({ reportId, days }: { reportId: number; days: '15' | '30' }) {
  const { data, error, refresh } = useResearch<ConceptComparison>(`/api/research/comparison?report_id=${reportId}&days=${days}`, 15_000);
  return <ComparisonResult data={data} error={error} refresh={refresh} />;
}

export function ComparisonResult({ data, error = null, refresh = () => {} }: { data: ConceptComparison | null; error?: string | null; refresh?: () => void }) {
  return <section className="comparison-panel"><h4>同期最强概念对照 <span>事后比较</span></h4><ResearchError error={error} retry={refresh} />
    <p>{data?.entryDate || '等待观察区间'} → {data?.exitDate || '—'} · 已核对 {data?.coveredCount ?? 0}/{data?.totalCount ?? 0} 个概念{data?.status === 'tracking' ? ' · 期间尚未结束' : ''}</p>
    {data?.strongest ? <><div className="comparison-lead"><div><small>{data.fullCoverage ? '比较范围内最强' : '已覆盖范围最强'}</small><strong>{data.strongest.name}</strong><b className={tone(data.strongest.returnPct)}>{percent(data.strongest.returnPct)}</b></div><div><small>已核对预测概念均值</small><strong className={tone(data.averageSelectedReturn)}>{percent(data.averageSelectedReturn)}</strong><span>{data.selected.length}/{data.selectedCount} 个预测概念</span></div><div><small>与同期最强的差距</small><strong>{diff(data.averageSelectedReturn === null ? null : data.averageSelectedReturn - data.strongest.returnPct)}</strong></div></div>
      <div className="research-chips">{data.selected.map(row => <span key={row.code}>{row.name} · 第 {row.rank} 名<small>涨幅 {percent(row.returnPct)} · 差距 {diff(row.gapPct)}</small></span>)}</div>
      <details><summary>查看同期前五名及核对依据</summary>{data.leaders.map((row, index) => <p key={row.code}>{index + 1}. {row.name} {percent(row.returnPct)} · 首日开盘 {row.entryPrice.toFixed(2)} → 末日收盘 {row.exitPrice.toFixed(2)} <a href={row.url} target="_blank" rel="noreferrer">行情来源</a></p>)}</details>
    </> : <p className="concept-ai-state">尚无同区间的完整对照行情。点击上方“核对同期最强”后分批更新。</p>}
    <p className="data-note">{data?.scope || '等待读取比较范围'}{data?.universeAsOf ? ` · 范围记录于 ${timestamp(data.universeAsOf)}` : ''}。统一首日开盘至末日收盘口径，缺失行情不计排名；事后最强不是当时可知的推荐，不计入预测命中率。</p>
  </section>;
}

export function OperationsView() {
  const { data, error, refresh } = useResearch<Operations>('/api/research/operations', operationsPolling);
  const [busy, setBusy] = useState<string | null>(null); const [actionError, setActionError] = useState<string | null>(null);
  async function runAi(provider: string, succeeded: boolean) {
    if (busy || !window.confirm('这会调用 AI 分析接口，可能产生费用。继续吗？')) return;
    const endpoint = provider === 'forecast:glm' ? '/api/forecast/concepts' : provider === 'concept:glm' ? '/api/market/concepts/ai' : `/api/strategies/ai?provider=${provider}`;
    setBusy(provider); setActionError(null);
    try { await jsonFetch(`${endpoint}${succeeded ? `${endpoint.includes('?') ? '&' : '?'}force=true` : ''}`, { method: 'POST' }); refresh(); }
    catch (cause) { setActionError(errorMessage(cause, 'AI 任务启动失败')); }
    finally { setBusy(null); }
  }
  return <div className="content"><header className="research-heading"><div><p className="eyebrow">数据与任务中心</p><h1>知道数据来自何时，任务进行到哪里</h1><p>页面只读取状态；付费分析由你明确发起。</p></div><button className="refresh-button" onClick={refresh}>刷新状态</button></header><ResearchError error={error || actionError} retry={refresh} />
    <div className="research-card-grid">{data?.sources.map(source => <article className="research-card" key={source.key}><h3>{source.name}</h3><strong>{({ fresh: '最近取得数据', cached: '已有缓存', degraded: '部分来源异常', unavailable: '尚无成功数据' })[source.state]}</strong><p>行情日期 {source.tradeDate || '—'}<br />最近成功 {source.updatedAt ? timestamp(source.updatedAt) : '—'}</p>{source.error && <p className="research-error">{source.error}</p>}</article>)}</div>
    <section className="research-panel"><h2>后台数据任务</h2><div className="research-card-grid">{data?.jobs.map(job => <article className="research-card" key={job.key}><h3>{job.name}</h3><p>{statusText[job.status]} · {job.finishedAt ? timestamp(job.finishedAt) : '尚未完成'}</p><JobButton jobKey={job.key} label={job.status === 'failed' ? '重试此任务' : '更新此模块'} /></article>)}</div><p className="data-note">下一次规则策略：{data?.nextRulesAt ? timestamp(data.nextRulesAt) : '未启用定时规则策略'} · {data?.researchSchedule}</p></section>
    <section className="research-panel"><h2>AI 分析任务</h2><p>今日生成任务 {data?.aiUsage.runsToday ?? 0} 次 · 分析请求 {data?.aiUsage.requestsToday ?? 0} 次 · 失败任务 {data?.aiUsage.failedToday ?? 0} 次</p><p className="data-note">{data?.aiUsage.note}</p><div className="research-card-grid">{data?.aiRuns.map(run => <article className="research-card" key={run.provider}><h3>{run.provider === 'forecast:glm' ? '半个月预测' : run.provider === 'concept:glm' ? '近期概念推荐' : `${run.provider} 选股`}</h3><small>{run.provider.endsWith(':glm') ? 'GLM 5.3 MAX' : run.model}</small><p>{statusText[run.status]}{run.status === 'running' && run.stage ? ` · ${run.stage}` : ''}</p>{run.error && <p className="research-error">{run.error}</p>}<button className="refresh-button" disabled={!!busy || run.status === 'running' || run.status === 'not_configured'} onClick={() => runAi(run.provider, run.status === 'succeeded')}>{busy === run.provider ? '提交中…' : run.status === 'failed' ? '仅重试此 AI 任务' : run.status === 'succeeded' ? '重新生成' : '生成'}</button></article>)}</div></section>
  </div>;
}

export function DailyDigest({ navigate }: { navigate: (tab: 'watchlist' | 'market' | 'sectors' | 'strategies' | 'forecast') => void }) {
  const { data, error, refresh } = useResearch<DailyDigestData>('/api/research/digest', 15000);
  return <div className="content"><header className="research-heading"><div><p className="eyebrow">每日观察摘要</p><h1>今天值得回看的变化</h1><p>把自选、大盘、板块与研究结果放在一起。</p></div><button className="refresh-button" onClick={refresh}>刷新摘要</button></header><ResearchError error={error} retry={refresh} />
    <div className="research-card-grid"><section className="research-panel"><h2>自选股波动</h2><small>行情日期 {data?.quoteDate || '—'}</small>{data?.watchMovers.map(stock => <p className="digest-line" key={stock.symbol}><StockLink code={stock.symbol}>{stock.name}</StockLink><strong className={tone(stock.quote?.pctChg ?? null)}>{percent(stock.quote?.pctChg ?? null)}</strong></p>)}{data && !data.watchMovers.length && <p>暂无可比较行情。</p>}<button className="text-button" onClick={() => navigate('watchlist')}>查看全部自选 →</button></section>
    <section className="research-panel"><h2>大盘与热点</h2>{data?.market ? <><p>上涨广度 {data.market.snapshot.breadth.toFixed(1)}% · 成交额 {amount(data.market.snapshot.turnover)}</p><small>大盘更新于 {timestamp(data.market.updatedAt)}</small></> : <p>尚未取得大盘快照。</p>}{data?.strongBoards.map(board => <p key={board.code}>{board.name} <b className={tone(board.pctChg)}>{percent(board.pctChg)}</b></p>)}<small>板块更新于 {data?.sectorAsOf ? timestamp(data.sectorAsOf) : '—'}</small><p><button className="text-button" onClick={() => navigate('market')}>大盘观察 →</button> <button className="text-button" onClick={() => navigate('sectors')}>板块与轮动 →</button></p></section>
    <section className="research-panel"><h2>近期策略名单</h2>{data?.recentSelections.map(entry => <article className="digest-selection" key={entry.id}><strong>{entry.runDate} · {entry.name}</strong><p>{entry.picks.map(pick => <StockLink key={pick.code} code={pick.code}>{pick.name}　</StockLink>)}</p></article>)}<button className="text-button" onClick={() => navigate('strategies')}>查看策略和历史成绩 →</button></section>
    <section className="research-panel"><h2>预测复盘进展</h2>{(['15', '30'] as const).map(days => { const summary = data?.forecastSummaries[days]; return <p key={days}>{days === '15' ? '半个月' : '一个月'}：{summary?.sampleCount ?? 0} 个已核对样本 · 平均涨幅 {percent(summary?.averageReturnPct ?? null)}<small>{summary?.trackingCount ?? 0} 个跟踪中 · {summary?.missingCount ?? 0} 个到期待补数据</small></p>; })}<button className="text-button" onClick={() => navigate('forecast')}>查看预测与同期最强对照 →</button></section></div><p className="data-note">{data?.note}</p>
  </div>;
}
