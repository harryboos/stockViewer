'use client';

import { useId, useState, type KeyboardEvent } from 'react';
import { amount, percent, timestamp, tone, shortTradeDate } from '@/lib/format';
import { SourceLinks } from '@/components/concept-recommendations';
import type { ForecastConcept, ForecastResearch } from '@/lib/forecast-types';
import type { ConceptRun, ConceptSource } from '@/lib/concept-types';
import { useDailyConceptRun } from '@/lib/use-daily-concept-run';
import { ForecastHistory } from '@/components/forecast-history';
import { JobButton } from './research-common';
import { StockLink } from './stock-detail';

const number = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? '—' : value.toFixed(2);
const perspectives = [['technical', '技术面'], ['fundamental', '基本面'], ['news', '时事新闻']] as const;
type Perspective = typeof perspectives[number][0];

function EvidenceSources({ sources }: { sources: ConceptSource[] }) {
  if (!sources.length) return <p className="forecast-source-empty">暂无可核对的直接来源，需继续验证。</p>;
  return <details className="forecast-source-disclosure"><summary>查看 {sources.length} 项依据与来源 <span aria-hidden="true">↗</span></summary><SourceLinks sources={sources} /></details>;
}

function ForecastEvidence({ concept }: { concept: ForecastConcept }) {
  const [active, setActive] = useState<Perspective>('technical');
  const id = useId();
  function switchTab(event: KeyboardEvent<HTMLButtonElement>, position: number) {
    const next = event.key === 'ArrowRight' ? (position + 1) % perspectives.length
      : event.key === 'ArrowLeft' ? (position + perspectives.length - 1) % perspectives.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? perspectives.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    setActive(perspectives[next][0]);
    document.getElementById(`${id}-tab-${perspectives[next][0]}`)?.focus();
  }
  return <section className="forecast-evidence-panel" aria-label={`${concept.name}三方面预测依据`}>
    <div className="forecast-subheading"><div><small>交叉核对</small><h4>三个视角，看判断依据</h4></div><div className="forecast-evidence-tabs" role="tablist" aria-label={`${concept.name}证据视角`}>
      {perspectives.map(([key, label], index) => <button type="button" role="tab" key={key} id={`${id}-tab-${key}`} aria-controls={`${id}-panel-${key}`} aria-selected={active === key} tabIndex={active === key ? 0 : -1} onKeyDown={event => switchTab(event, index)} onClick={() => setActive(key)}>{label}</button>)}
    </div></div>
    {perspectives.map(([key, label]) => <div key={key} role="tabpanel" id={`${id}-panel-${key}`} aria-labelledby={`${id}-tab-${key}`} tabIndex={0} hidden={active !== key} className="forecast-evidence-body">
      <div className="forecast-evidence-intro"><span className={`concept-ai-evidence ${concept[key].status === 'supported' ? 'data' : 'hypothesis'}`}>{concept[key].status === 'supported' ? '有材料支持' : '待验证'}</span><p>{concept[key].summary}</p></div>
      {key === 'technical' && <><dl className="forecast-technical">
        <div><dt>指数最新值</dt><dd>{number(concept.technicalData.lastClose)}</dd></div>
        <div><dt>5 / 10 / 20 日均线</dt><dd>{number(concept.technicalData.ma5)} / {number(concept.technicalData.ma10)} / {number(concept.technicalData.ma20)}</dd></div>
        <div><dt>RSI（14 日）</dt><dd>{number(concept.technicalData.rsi14)}</dd></div>
        <div><dt>距 20 日最高收盘价</dt><dd>{percent(concept.technicalData.distanceTo20dHigh)}</dd></div>
        <div><dt>完整 5 日成交额比</dt><dd>{number(concept.technicalData.amountRatio5d)}{concept.technicalData.amountRatio5d == null ? '' : ' 倍'}</dd></div>
      </dl><p className="forecast-small-note">成交额比剔除行情当日，比较最近 5 个完整交易日与此前 5 日。历史截至 {concept.technicalData.historyAsOf ? shortTradeDate(concept.technicalData.historyAsOf) : '暂缺'}。</p></>}
      {key === 'fundamental' && <p className="forecast-small-note">{concept.fundamentalData.note}</p>}
      <EvidenceSources sources={concept[key].sources} />
      <span className="sr-only">{label}预测依据</span>
    </div>)}
  </section>;
}

function ForecastCatalysts({ concept }: { concept: ForecastConcept }) {
  return <section className="forecast-catalysts" aria-label={`${concept.name}现实影响因素`}>
    <div className="forecast-subheading"><div><small>从事件到产业</small><h4>现实影响因素</h4></div><span>为什么可能走强</span></div>
    {!concept.catalysts.length && <p className="forecast-small-note">暂未找到可核对的现实催化，不能仅凭价格走势解释原因。</p>}
    {concept.catalysts.map((catalyst, index) => <details className="forecast-catalyst" key={index} open={index === 0}>
      <summary><span className="forecast-catalyst-index">{String(index + 1).padStart(2, '0')}</span><strong>{catalyst.title}</strong><span className={`concept-ai-evidence ${catalyst.status === 'reported' ? 'news' : 'hypothesis'}`}>{catalyst.status === 'reported' ? '有报道支持' : '待验证机制'}</span><span className="forecast-disclosure-mark" aria-hidden="true">+</span></summary>
      <div className="forecast-catalyst-body"><p>{catalyst.event}</p><div className="forecast-transmission"><span>影响传导</span><p>{catalyst.transmission}</p></div><p><b>未来半个月的影响：</b>{catalyst.impact}</p><EvidenceSources sources={catalyst.sources} /></div>
    </details>)}
  </section>;
}

function ForecastStocks({ concept, sessionLabel }: { concept: ForecastConcept; sessionLabel: string }) {
  return <section className="forecast-stocks" aria-label={`${concept.name}强势股票`}>
    <div className="forecast-subheading"><div><small>观察代表公司</small><h4>强势股票</h4></div><span>{sessionLabel}表现</span></div>
    {!concept.stocks.length && <p className="forecast-stock-empty">暂无可核对的同一交易日强势股行情。</p>}
    {concept.stocks.map(stock => {
      const valuation = concept.fundamentalData.valuations.find(row => row.code === stock.code);
      return <article className="forecast-stock" key={stock.code}><div className="forecast-stock-quote"><div><StockLink code={stock.code}><strong>{stock.name}</strong></StockLink><small>{stock.code} · ¥{number(stock.price)}</small></div><b className={tone(stock.pctChg)}>{percent(stock.pctChg)}</b></div>
        <p className="forecast-stock-turnover">成交 {amount(stock.amount)} · 换手 {number(stock.turnoverRate)}{stock.turnoverRate == null ? '' : '%'}</p><p className="forecast-stock-reason">{stock.reason}</p>
        <details className="forecast-stock-valuation"><summary>估值快照</summary><p>动态 PE {number(valuation?.peDynamic)} · PB {number(valuation?.pb)} · 市值 {amount(valuation?.marketCap)}</p></details>
      </article>;
    })}
    <p className="forecast-small-note">当前强势股不代表未来必然领涨；估值为已取得成份股的快照。</p>
  </section>;
}

export function ForecastResearchCards({ research, today }: { research: ForecastResearch; today: string }) {
  const [selected, setSelected] = useState(research.concepts[0]?.code);
  const concept = research.concepts.find(item => item.code === selected) || research.concepts[0];
  const sessionLabel = shortTradeDate(research.tradeDate) === today ? '今日' : '最近交易日';
  const id = useId();
  return <div className="forecast-research">
    <div className="forecast-window"><div><span>本次预测区间</span><strong>{research.window.startDate}<i aria-hidden="true">→</i>{research.window.endDate}</strong></div><span>{research.window.calendarDays} 个自然日 · 含休市日</span></div>
    <section className="forecast-overview"><div className="forecast-overview-label"><span>本次判断</span><small>{research.concepts.length ? `${research.concepts.length} 个观察方向` : '保持观望'}</small></div><div><p>{research.summary}</p><span className="forecast-asof">{sessionLabel}行情 · {shortTradeDate(research.tradeDate)} · 快照更新 {timestamp(research.dataAsOf)}（北京时间）{research.conceptProvider && ` · ${research.conceptProvider === 'ths' ? '同花顺样本' : '东方财富概念'}`}</span></div></section>
    {research.feedbackUsed?.asOf && <p className="forecast-feedback-note"><span aria-hidden="true">↻</span> 本次已参考到期反馈：半个月 {research.feedbackUsed.sampleCounts['15'] ?? 0} 个样本 · 一个月 {research.feedbackUsed.sampleCounts['30'] ?? 0} 个样本。仅使用生成前已核对的结果。</p>}
    {!research.concepts.length && <div className="forecast-empty-result"><span aria-hidden="true">—</span><div><strong>等待更充分的证据</strong><p>当前证据不足以选出预测方向，暂不强行给出名单。可在行情或新闻更新后重新分析。</p></div></div>}
    {research.concepts.length > 1 && <nav className="forecast-direction-list" aria-label="切换预测概念">{research.concepts.map((item, index) => <button key={item.code} type="button" aria-pressed={item.code === concept?.code} aria-controls={`${id}-concept`} onClick={() => setSelected(item.code)}><span className="forecast-direction-top"><small>方向 {String(index + 1).padStart(2, '0')}</small><span aria-hidden="true">↗</span></span><strong>{item.name}</strong><span className="forecast-direction-values"><span>{sessionLabel} <b className={tone(item.pctChg)}>{percent(item.pctChg)}</b></span><span>近 5 日 <b className={tone(item.change5d)}>{percent(item.change5d)}</b></span></span></button>)}</nav>}
    {concept && <article className="forecast-focus-card" id={`${id}-concept`} aria-label={`${concept.name}预测详情`} key={concept.code}>
      <header className="forecast-focus-heading"><div><span className="forecast-code">潜在强势方向 / {concept.code}</span><h3>{concept.name}<span className={`forecast-conviction ${concept.conviction}`}>研究把握度：{concept.conviction === 'medium' ? '中等' : '较低'}</span></h3></div><div className="forecast-session-move"><small>{sessionLabel}涨幅</small><strong className={tone(concept.pctChg)}>{percent(concept.pctChg)}</strong></div></header>
      <div className="forecast-main-thesis"><span>核心判断</span><p>{concept.thesis}</p></div>
      <dl className="forecast-key-metrics"><div><dt>近 5 日涨幅</dt><dd className={tone(concept.change5d)}>{percent(concept.change5d)}</dd></div><div><dt>近 10 日涨幅</dt><dd className={tone(concept.change10d)}>{percent(concept.change10d)}</dd></div><div><dt>{sessionLabel}成交额</dt><dd>{amount(concept.amount)}</dd></div><div><dt>主力净流入</dt><dd className={tone(concept.mainNetInflow)}>{amount(concept.mainNetInflow, true)}</dd></div><div><dt>上涨广度</dt><dd>{Number.isFinite(concept.breadth) ? `${concept.breadth.toFixed(1)}%` : '—'}</dd></div><div><dt>上涨 / 下跌家数</dt><dd>{concept.upCount} / {concept.downCount}</dd></div></dl>
      <div className="forecast-research-columns"><ForecastCatalysts concept={concept} /><ForecastStocks concept={concept} sessionLabel={sessionLabel} /></div>
      <ForecastEvidence concept={concept} />
      <div className="forecast-conditions"><section><span className="forecast-condition-icon" aria-hidden="true">↗</span><div><h4>什么出现，判断更成立</h4><p>{concept.confirmation}</p></div></section><section><span className="forecast-condition-icon" aria-hidden="true">!</span><div><h4>什么出现，预测应失效</h4><p>{concept.invalidation}</p></div></section></div>
      {!!concept.warnings.length && <p className="forecast-data-warning"><strong>数据提示</strong>{concept.warnings.join('；')}</p>}
    </article>}
    <details className="forecast-method-note"><summary>研究口径与数据说明</summary><p>{research.scope} 研究把握度不是统计胜率；以上为基于当前资料的条件推演，不保证收益。新闻提供事实线索，未来影响仍需验证，缺失数据以“—”展示。</p>{!!research.warnings.length && <p>行情提示：{research.warnings.join('；')}</p>}</details>
  </div>;
}

type ForecastTaskStateProps = {
  current: Omit<ConceptRun, 'result' | 'previousResult'> | null;
  busy: boolean; submitting: boolean; requestError: string | null; refresh: () => void;
};

export function ForecastTaskState({ current, busy, submitting, requestError, refresh }: ForecastTaskStateProps) {
  return <div className="forecast-task-states" aria-live="polite">
    {!current && !requestError && <div className="forecast-status-card"><span className="loading-ring" /><div><strong>正在读取当天预测</strong><p>先查看已保存的分析与任务状态。</p></div></div>}
    {current?.status === 'idle' && !submitting && <div className="forecast-intro"><div><span className="forecast-intro-mark" aria-hidden="true">↗</span><h3>下一段行情，从哪些方向开始？</h3><p>比较近期趋势，核对产业与新闻催化，寻找未来半个月值得持续观察的概念。</p></div><ol><li><span>01</span><div><strong>看趋势</strong><p>近期强度、成交与技术结构</p></div></li><li><span>02</span><div><strong>找原因</strong><p>产业、政策与供需变化</p></div></li><li><span>03</span><div><strong>留验证条件</strong><p>最多 3 个方向，持续跟踪与复盘</p></div></li></ol><p className="forecast-small-note">预测从生成日次日起算，当天结果会保存。点击生成后才调用 GLM 5.3 MAX 与联网检索。</p></div>}
    {current?.status === 'not_configured' && <div className="forecast-status-card"><span className="forecast-status-symbol" aria-hidden="true">◇</span><div><strong>尚未配置 GLM 5.3 MAX</strong><p>请在服务配置中填写 GLM 的密钥，重启服务后即可生成预测。</p><button className="refresh-button" onClick={refresh}>重新检查</button></div></div>}
    {busy && <div className="forecast-status-card forecast-running"><span className="loading-ring" /><div><span className="forecast-status-eyebrow">预测任务进行中</span><strong>{current?.status === 'running' && current.stage ? current.stage : '正在准备 MAX 深度预测'}</strong><p>高峰时自动排队重试，接入后保留完整的深度分析时间。{current?.maxRunSeconds ? `整个任务最长约 ${Math.ceil(current.maxRunSeconds / 60)} 分钟。` : ''}可以切换页面，返回后继续查看进度和结果。</p>{current?.status === 'running' && current.startedAt && <small>开始于 {timestamp(current.startedAt)} · 仅展示完整核对后的预测</small>}</div></div>}
    {current?.status === 'failed' && !submitting && <div className="forecast-failure" role="alert"><span className="forecast-status-symbol" aria-hidden="true">!</span><div><strong>本次预测尚未完成</strong><p>{current.error || '预测未完成，请稍后重新生成。'}</p><div className="forecast-recovery-actions"><JobButton jobKey="sectors" label="更新板块行情" /><button className="text-button" onClick={refresh}>重新读取状态</button></div><small>可先更新行情，或稍后重新生成。已保存的预测仍可查看。</small></div></div>}
    {requestError && <div className="forecast-failure" role="alert"><span className="forecast-status-symbol" aria-hidden="true">!</span><div><strong>暂时未能取得最新状态</strong><p>{requestError}</p><button className="text-button" onClick={refresh}>重新读取</button><small>重新读取仅检查已有任务，不会再次提交预测。</small></div></div>}
  </div>;
}

export function ConceptForecast() {
  const { current, result, busy, submitting, requestError, today, generate, refresh } = useDailyConceptRun<ForecastResearch>('/api/forecast/concepts', '预测');
  return <div className="content forecast-content forecast-workspace">
    <section className="forecast-section" aria-labelledby="concept-forecast-title" aria-busy={busy}>
      <header className="forecast-hero"><div><div className="forecast-hero-kicker"><span className="eyebrow">AI 概念预测</span><span className="forecast-model-badge"><i aria-hidden="true" />GLM 5.3 MAX</span></div><h1 id="concept-forecast-title">未来半个月强势概念预测</h1><p>把技术面、基本面与时事新闻放在一起，寻找值得跟踪的方向。</p></div><div className="forecast-hero-actions"><button className="concept-ai-generate" onClick={generate} disabled={busy || !current || current.status === 'not_configured'}><span aria-hidden="true">✦</span> {busy ? '正在预测…' : result ? '重新生成预测' : '生成半个月预测'}</button><a href="#forecast-history-title">查看历史复盘 <span aria-hidden="true">↘</span></a></div></header>
      <div className="forecast-hero-meta"><span>技术面 × 基本面 × 时事新闻</span><span>未来 15 个自然日</span><span>{current?.status === 'succeeded' && current.finishedAt ? `分析完成 ${timestamp(current.finishedAt)}（北京时间）` : `研究日期 ${today}`}</span></div>
      <ForecastTaskState current={current} busy={busy} submitting={submitting} requestError={requestError} refresh={refresh} />
      {current?.status !== 'succeeded' && result && <p className="forecast-retained-result"><span aria-hidden="true">↶</span> 以下保留上次成功结果 · {current?.previousFinishedAt ? timestamp(current.previousFinishedAt) : '完成时间待补'}，新结果生成前仍可查看。</p>}
      {result && <ForecastResearchCards research={result} today={today} />}
      <p className="forecast-provider-note">GLM 5.3 MAX 深度分析 · 点击生成才会调用 AI 与联网检索 · 条件推演不代表收益承诺</p>
    </section>
    <ForecastHistory finishedAt={current?.finishedAt} renderResearch={research => <ForecastResearchCards research={research} today={today} />} />
  </div>;
}
