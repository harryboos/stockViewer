import { shortTradeDate, tone, isFiniteNumber as finite, percentagePoints } from '@/lib/format';
import type { ForecastCandidateSelection, ForecastConcept, ForecastRelativeStrength, ForecastRiskContext, ForecastRiskPair } from '@/lib/forecast-types';

const count = (value: number | null | undefined) => finite(value) && value >= 0 ? String(value) : '未知';
const period = (start?: string | null, end?: string | null) => start && end ? `${shortTradeDate(start)} → ${shortTradeDate(end)}` : '观察区间未知';

export function ForecastDiscoveryCoverage({ selection }: { selection?: ForecastCandidateSelection }) {
  if (!selection) return null;
  return <details className="forecast-method-note" aria-label="本次候选发现覆盖">
    <summary>候选发现 · {count(selection.snapshotCount ?? selection.availableCount)} 个快照 · {count(selection.historyCheckedCount)} 个已取得历史 · {count(selection.selectedCount)} 个进入研究</summary>
    <div className="forecast-evidence-body"><dl className="forecast-technical">
      <div><dt>候选快照</dt><dd>{count(selection.snapshotCount ?? selection.availableCount)} 个</dd></div>
      <div><dt>已取得 / 同日对齐历史</dt><dd>{count(selection.historyCheckedCount)} / {count(selection.historyAlignedCount)} 个</dd></div>
      <div><dt>进入 AI 比较</dt><dd>{count(selection.selectedCount)} 个</dd></div>
    </dl></div>
    <p>{selection.note || '仅反映本次已取得的候选资料，未读取的概念不代表没有机会。旧报告可能未记录历史覆盖。'}</p>
  </details>;
}

export function ForecastRelativeStrength({ data, name }: { data?: ForecastRelativeStrength | null; name: string }) {
  if (!data) return null;
  return <details className="forecast-source-disclosure" open aria-label={`${name}相对基准表现`}>
    <summary>相对基准表现 · 同区间收益差</summary>
    <dl className="forecast-technical" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
      {data.benchmarks.flatMap(benchmark => (['5d', '10d'] as const).map(key => {
        const comparison = benchmark.periods[key];
        const value = comparison.status === 'available' ? comparison.excessPctPoints : null;
        return <div key={`${benchmark.code}-${key}`}>
          <dt>近 {key === '5d' ? 5 : 10} 日 · 相对{benchmark.name}</dt>
          <dd className={tone(value)}>{percentagePoints(value, '未知')}</dd>
          <p className="forecast-small-note">{period(comparison.startDate, comparison.endDate)}{comparison.reason && <><br />{comparison.reason}</>}</p>
        </div>;
      }))}
    </dl>
    <p className="forecast-small-note">收益差以百分点表示；只比较相同日期的价格，缺失不计为 0。价格截至 {data.historyAsOf ? shortTradeDate(data.historyAsOf) : '未知'}，不保证末日已收盘。</p>
  </details>;
}

function safeNewsUrl(url: string) {
  try {
    const parsed = new URL(url);
    return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed : null;
  } catch { return null; }
}

function RiskPair({ pair, names }: { pair: ForecastRiskPair; names: Map<string, string> }) {
  const correlation = pair.correlation20d;
  const overlap = pair.strongStockSampleOverlap;
  const news = pair.sharedNewsSources;
  const value = correlation.status === 'available' && finite(correlation.value) ? correlation.value.toFixed(2) : '未知';
  const hasStockSamples = overlap.leftSampleSize > 0 && overlap.rightSampleSize > 0;
  const hasNewsSamples = news.leftSourceCount > 0 && news.rightSourceCount > 0;
  const links = news.urls.flatMap(url => { const parsed = safeNewsUrl(url); return parsed ? [{ url, host: parsed.hostname }] : []; });
  return <details className="forecast-catalyst">
    <summary><strong>{names.get(pair.leftCode)} / {names.get(pair.rightCode)}</strong><span className="concept-ai-evidence data">相关性 {value}</span><span className="forecast-disclosure-mark" aria-hidden="true">+</span></summary>
    <div className="forecast-evidence-body"><dl className="forecast-technical">
      <div><dt>日收益相关性</dt><dd>{value}</dd><p className="forecast-small-note">{period(correlation.startDate, correlation.endDate)}<br />{count(correlation.returnCount)} 个日收益配对{correlation.smallSample && ' · 短样本'}{correlation.reason && <><br />{correlation.reason}</>}</p></div>
      <div><dt>强势股样本重合</dt><dd>{hasStockSamples && finite(overlap.sharedCount) ? `${overlap.sharedCount} 只` : '未知'}</dd><p className="forecast-small-note">两侧样本 {count(overlap.leftSampleSize)} / {count(overlap.rightSampleSize)} 只<br />占较小样本 {hasStockSamples && finite(overlap.overlapOfSmallerSamplePct) ? `${overlap.overlapOfSmallerSamplePct.toFixed(1)}%` : '未知'}；并非全体成份股重合。</p></div>
      <div><dt>共用新闻来源</dt><dd>{hasNewsSamples && finite(news.count) ? `${news.count} 个页面` : '未知'}</dd><p className="forecast-small-note">两侧来源 {count(news.leftSourceCount)} / {count(news.rightSourceCount)} 个<br />相同页面不等于同一事件，没有共用页面也不代表催化独立。</p></div>
    </dl>
      {!!links.length && <div className="concept-ai-source">{links.map((link, index) => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">共用来源 {index + 1} · {link.host} ↗</a>)}</div>}
    </div>
  </details>;
}

export function ForecastCommonRisk({ context, concepts }: { context?: ForecastRiskContext; concepts: Pick<ForecastConcept, 'code' | 'name'>[] }) {
  if (!context || concepts.length < 2) return null;
  const names = new Map(concepts.map(concept => [concept.code, concept.name]));
  const pairs = context.pairs.filter(pair => names.has(pair.leftCode) && names.has(pair.rightCode));
  return <section className="forecast-evidence-panel mt-5" aria-label="入选概念共同风险">
    <div className="forecast-subheading"><div><small>检查是否依赖相同驱动</small><h4>入选概念的共同风险</h4></div><span>截至 {shortTradeDate(context.asOf)}</span></div>
    {pairs.map(pair => <RiskPair key={`${pair.leftCode}-${pair.rightCode}`} pair={pair} names={names} />)}
    {!pairs.length && <p className="forecast-small-note">尚无可核对的概念配对数据，共同风险未知。</p>}
    <p className="forecast-small-note">相关性越接近 1，已观察区间的同涨同跌越明显；不代表未来必然同步。强势股仅为当前取得的样本，以上信息不等于真实持仓风险。</p>
    <details className="forecast-method-note"><summary>覆盖与计算限制</summary>{context.limits.map((limit, index) => <p key={index}>{limit}</p>)}</details>
  </section>;
}
