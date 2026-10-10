import { percent, tone, rate, percentagePoints as gap } from '@/lib/format';
import type { ForecastEvaluationData, ForecastEvaluationGroup, Horizon } from '@/lib/forecast-history-types';
import styles from './forecast-evaluation.module.css';

const availability = { in_pool: '已入池但未选', outside_pool: '未进入候选池', unknown: '原始候选记录不足' };

function VersionEvaluation({ group }: { group: ForecastEvaluationGroup }) {
  return <article className={styles.card}>
    <header className={styles.header}>
      <div><h4>{group.promptVersion}</h4><p>{group.conceptProvider === 'ths' ? '同花顺概念' : '东方财富概念'} · {group.reportCount} 份每日首次预测</p></div>
      <span className={styles.tag}>{group.matureReportCount} 份已到期</span>
    </header>
    <dl className={styles.stats}>
      <div><dt>完整核对报告平均涨幅</dt><dd className={tone(group.averageReturnPct)}>{percent(group.averageReturnPct)}</dd><small>{group.verifiedReportCount} 份报告 · 每份报告内概念等权</small></div>
      <div><dt>概念最大回撤均值</dt><dd>{percent(group.averageSelectedMaxDrawdownPct)}</dd><small>{group.drawdownReportCount} 份报告 · 并非组合回撤</small></div>
      <div><dt>已到期报告观望率</dt><dd>{rate(group.abstentionRate)}</dd><small>{group.abstentionCount} / {group.matureReportCount} 份 · 观望不记零收益</small></div>
    </dl>
    <p className={styles.note}>完整核对中可取 {group.nonOverlappingReportCount} 份互不重叠报告；{group.unverifiedReportCount} 份到期报告行情待补齐。重叠窗口不能当作独立试验。</p>
    {group.calendarWindowMissingCount > 0 && <p className={styles.warning}>{group.calendarWindowMissingCount} 份报告缺少覆盖观察期的交易日历，暂不计算收益。</p>}
    <div className={styles.scroll} role="region" aria-label={`${group.promptVersion} 规则基线对比，可横向滚动`} tabIndex={0}>
      <table className={styles.table}>
        <caption>与预测时冻结的规则基线比较</caption>
        <thead><tr><th scope="col">规则基线</th><th scope="col">配对报告</th><th scope="col">同批 AI 涨幅</th><th scope="col">基线涨幅</th><th scope="col">AI 相对基线</th></tr></thead>
        <tbody>{group.baselines.map(item => <tr key={item.id}><th scope="row">{item.name}</th><td>{item.pairedReportCount} 份</td><td>{percent(item.averageAiReturnPct)}</td><td>{percent(item.averageBaselineReturnPct)}</td><td className={tone(item.averageExcessPct)}>{gap(item.averageExcessPct)}</td></tr>)}</tbody>
      </table>
    </div>
    <p className={styles.note}>当日涨幅前三以当时保存的行情快照为范围，不代表全市场。仅在同一报告的全部预测方向与该基线行情完整时配对；不同基线的配对报告可能不同。{group.baselineMissingPlanCount > 0 && <> {group.baselineMissingPlanCount} 份到期记录没有可用的冻结基线，保留原预测，不事后补造。</>}</p>
    <details className={styles.details}>
      <summary>查看覆盖范围、排名与漏选归因</summary>
      <dl className={styles.stats}>
        <div><dt>已覆盖范围平均排名百分位</dt><dd>{rate(group.ranking.averagePercentile)}</dd><small>数值越高，排名越靠前</small></div>
        <div><dt>进入已覆盖范围前 20%</dt><dd>{rate(group.ranking.top20Rate)}</dd><small>{group.ranking.conceptCount} 个选中概念</small></div>
        <div><dt>冻结比较范围平均覆盖率</dt><dd>{rate(group.ranking.averageCoveragePct)}</dd><small>{group.ranking.reportCount} 份可排名报告</small></div>
      </dl>
      <p className={styles.note}>{group.rankingMissingUniverseCount > 0 && <>{group.rankingMissingUniverseCount} 份已核对报告未保存有效的事前比较范围，无法排名。 </>}排名仅针对已核对的同源、同区间范围，不能当作全市场排名。</p>
      <p className={styles.note}>漏选归因：已入池但未选 {group.missedLeader.inPoolCount} 次 · 未进入候选池 {group.missedLeader.outsidePoolCount} 次 · 原始记录不足 {group.missedLeader.unknownCount} 次。重叠期间可能重复出现同一赢家。</p>
      {group.missedLeader.cases.length > 0 && <ul className={styles.cases}>{group.missedLeader.cases.map(item => <li key={item.reportId}>
        <div><strong>{item.predictedOn} · {item.name}</strong><span className={tone(item.returnPct)}>{percent(item.returnPct)}</span></div>
        <small>{availability[item.candidateAvailability]} · {item.entryDate} 至 {item.exitDate} · 核对 {item.coveredCount}/{item.totalCount} 个概念</small>
      </li>)}</ul>}
      <p className={styles.note}>收益差距只能说明结果，不能据此认定催化失效、入选过晚或当时必然可预测。</p>
    </details>
  </article>;
}

export function ForecastEvaluation({ data, days }: { data?: ForecastEvaluationData; days: Horizon }) {
  if (!data || !data.readReportCount) return null;
  const groups = data.groups.filter(group => String(group.horizonDays) === days);
  return <details className={styles.panel}>
    <summary><span>版本与规则基线评估</span><small>{days === '15' ? '半个月' : '一个月延伸'} · {groups.length} 个版本与数据源分组</small></summary>
    <p className={styles.note}>读取最近 {data.readReportCount} 份每日首次预测，最多 {data.reportLimit} 份。不同版本对应不同日期和行情，分组差异不能证明版本改动带来了提升。</p>
    {!data.calendarAvailable && <p className={styles.warning}>交易日历暂不可用，收益与基线核对等待补齐，不使用工作日猜测交易日。</p>}
    {data.skippedReportCount > 0 && <p className={styles.note}>{data.skippedReportCount} 份记录缺少可审计信息或超出评估时点，未参与分组。</p>}
    {groups.length === 0 ? <p className={styles.note}>尚无可按版本与数据源核对的报告。</p> : groups.map(group => <VersionEvaluation key={`${group.promptVersion}:${group.conceptProvider}`} group={group} />)}
    <p className={styles.note}>{data.sampleUnit}。{data.limits}</p>
  </details>;
}
