import { shortTradeDate } from '@/lib/format';
import { AI_META, PUBLIC_STRATEGY_META, type StrategyMeta } from '@/lib/strategy-meta';
import {
  AI_PROVIDERS,
  type AiRunView,
  type PublicStrategyResult,
  type StrategyId,
  type StrategySummary,
  type SystemStatus,
} from '@/lib/types';


type StrategiesViewProps = {
  today: string;
  status: SystemStatus | null;
  strategies: PublicStrategyResult[];
  aiRuns: AiRunView[];
  summary: StrategySummary;
  loading: boolean;
  onLoad: () => void;
  onRerun: () => void;
};

type ExpandableCopyProps = {
  text: string;
  previewLength: number;
  className: string;
  label?: string;
};

function ExpandableCopy({ text, previewLength, className, label }: ExpandableCopyProps) {
  const characters = Array.from(text);
  const labelNode = label ? <span className="copy-label">{label}</span> : null;
  if (characters.length <= previewLength) {
    return <p className={className}>{labelNode}{text}</p>;
  }

  return (
    <details className={`ai-expandable ${className}`}>
      <summary>
        <span className="ai-expandable-preview">{labelNode}{characters.slice(0, previewLength).join('')}…</span>
        <span className="ai-expandable-action ai-expandable-open">展开全文</span>
        <span className="ai-expandable-action ai-expandable-close">收起</span>
      </summary>
      <p>{labelNode}{text}</p>
    </details>
  );
}

function displayedStrategies(strategies: PublicStrategyResult[]): PublicStrategyResult[] {
  if (strategies.length) return strategies;
  return (Object.entries(PUBLIC_STRATEGY_META) as Array<[StrategyId, StrategyMeta]>).map(([id, meta]) => ({
    id,
    name: meta.fallbackName,
    description: '首次打开时会从免费数据源计算今日结果。',
    runDate: '',
    tradeDate: '',
    picks: [],
  }));
}

function displayedAiRuns(status: SystemStatus | null, runs: AiRunView[]): AiRunView[] {
  if (runs.length) return runs;
  return AI_PROVIDERS.map((provider) => ({
    provider,
    model: AI_META[provider].model,
    status: status?.providers[provider] ? 'pending' : 'not_configured',
    result: null,
    error: null,
    finishedAt: null,
  }));
}

function runStatusLabel(run: AiRunView): string {
  if (run.status === 'succeeded') return '已完成';
  if (run.status === 'running') return '运行中';
  if (run.status === 'failed') return '失败';
  if (run.status === 'not_configured') return '待配置';
  return '待运行';
}

export function StrategiesView({
  today,
  status,
  strategies,
  aiRuns,
  summary,
  loading,
  onLoad,
  onRerun,
}: StrategiesViewProps) {
  const publicItems = displayedStrategies(strategies);
  const aiItems = displayedAiRuns(status, aiRuns);
  const completedAiCount = aiRuns.filter((run) => run.status === 'succeeded').length;
  const aiRunning = aiRuns.some((run) => run.status === 'running');

  return (
    <section className="content strategies-page page-enter">
      <div className="strategy-hero">
        <div>
          <p className="eyebrow">多视角选股实验室</p>
          <h1>把真实数据与不同方法，放在同一张桌上</h1>
          <p className="subtitle">规则策略每日计算；三家 AI 使用同一提示语、同一候选池和同一输出约束。结果写入本地 SQLite，避免重复调用。</p>
        </div>
        <div className="strategy-actions">
          <button type="button" className="daily-status daily-button" onClick={onLoad} disabled={loading}>
            <span className="pulse-dot" />
            <div><strong>{loading ? '策略运行中…' : '今日策略状态'}</strong><small>{today} · 检查缺失或失败结果</small></div>
          </button>
          <button type="button" className="daily-status daily-button manual-rerun" onClick={onRerun} disabled={loading}>
            <span className="rerun-mark" aria-hidden="true">↻</span>
            <div><strong>{loading ? '请等待当前任务' : '手动重跑全部'}</strong><small>重新取数 · 会再次调用 AI</small></div>
          </button>
        </div>
      </div>

      <section className="strategy-section">
        <div className="section-heading">
          <div><span className="section-index">01</span><div><h2>规则策略</h2><p>公开指数、GitHub 开源方法与自定义条件，使用免费行情和复权历史每日计算</p></div></div>
          <span className="source-count">{strategies.length ? `${strategies.length} 种已计算` : status?.providers.marketData ? '等待计算' : '数据服务未就绪'}</span>
        </div>
        <div className="public-grid">
          {publicItems.map((strategy) => {
            const meta = PUBLIC_STRATEGY_META[strategy.id];
            const strictNoFill = strategy.id === 'volume_breakout';
            const hotConcept = strategy.id === 'hot_concept';
            const hasRun = Boolean(strategy.runDate);
            return (
              <article className={`public-card ${meta.color}`} key={strategy.id}>
                <div className="strategy-card-top"><span>{meta.index}</span><span className="method-mark">◌</span></div>
                <h3>{strategy.name}</h3><p>{strategy.description}</p>
                <div className="tag-row">{meta.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
                <div className="mini-picks">
                  {strategy.picks.length ? strategy.picks.map((pick, index) => (
                    <div key={pick.code}>
                      <span className="pick-rank">{String(index + 1).padStart(2, '0')}</span>
                      <strong>{pick.name}</strong>
                      <small>{pick.code} · {strictNoFill ? '命中全部条件' : `${pick.score}分`}</small>
                      <p>{pick.reason}</p>
                    </div>
                  )) : (
                    <div className="strategy-empty">
                      <strong>{loading
                        ? '正在计算真实因子…'
                        : strictNoFill
                          ? '今日没有股票同时满足全部条件'
                          : hotConcept
                            ? '今日热点源暂不可用或没有符合项'
                            : hasRun ? '今日没有股票满足前置条件' : '暂无今日结果'}</strong>
                      <small>{strictNoFill
                        ? '严格筛选不会用接近条件的股票补位'
                        : hotConcept
                          ? '不使用旧日期概念或模拟数据补位'
                          : hasRun ? '不会用接近条件的股票强行补位' : '点击上方“今日策略状态”开始计算'}</small>
                    </div>
                  )}
                </div>
                {meta.url ? (
                  <a href={meta.url} target="_blank" rel="noreferrer" className="source-link">查看方法来源 <span>↗</span><small>{meta.source}</small></a>
                ) : (
                  <div className="source-link source-static"><span>严格按条件执行</span><small>{meta.source}</small></div>
                )}
              </article>
            );
          })}
        </div>
      </section>

      <section className="strategy-section ai-section">
        <div className="section-heading">
          <div><span className="section-index">02</span><div><h2>AI 每日选股</h2><p>三家官方 API 使用完全相同的提示语与输入；失败不会用演示结果冒充</p></div></div>
          <span className={`source-count ${aiRunning ? 'ai-running' : 'ai-ready'}`}><i /> {completedAiCount}/3 已完成</span>
        </div>
        <div className="ai-grid">
          {aiItems.map((run) => {
            const meta = AI_META[run.provider];
            const apiKeyName = {
              deepseek: 'DEEPSEEK_API_KEY',
              glm: 'GLM_API_KEY',
              qwen: 'QWEN_API_KEY',
            }[run.provider];
            return (
              <article className={`ai-card ${meta.theme}`} key={run.provider}>
                <div className="ai-card-heading">
                  <span className="ai-monogram">{meta.monogram}</span>
                  <div><small>{meta.model} · {run.model}</small><h3>{run.result?.title ?? meta.fallbackTitle}</h3></div>
                  <span className={`run-badge status-${run.status}`}>{runStatusLabel(run)}</span>
                </div>
                <ExpandableCopy className="ai-desc" previewLength={180} text={run.result?.summary ?? (
                  run.status === 'not_configured'
                    ? `请在 .env.local 配置 ${apiKeyName}`
                    : run.status === 'failed' ? run.error : '今日首次进入策略页时自动运行。'
                ) ?? '模型运行失败，请点击重试'} />
                <div className="logic-line"><span>选股逻辑</span><ExpandableCopy className="logic-copy" previewLength={120} text={run.result?.logic ?? meta.logic} /></div>
                <div className="ai-picks">
                  {run.result?.picks.map((pick, index) => (
                    <div className="ai-pick-row" key={pick.code}>
                      <span className="rank-circle">{index + 1}</span>
                      <div className="ai-pick-copy">
                        <div className="ai-pick-name"><strong>{pick.name}</strong><small>{pick.code}</small></div>
                        <ExpandableCopy className="ai-pick-reason" previewLength={110} text={pick.reason} />
                        <ExpandableCopy className="pick-risk" previewLength={80} text={pick.risk} label="风险" />
                      </div>
                      <div className="score"><i style={{ width: `${Math.max(pick.score - 25, 10)}%` }} /><span>{pick.score}</span></div>
                    </div>
                  )) ?? (
                    <div className="ai-empty"><span className={run.status === 'running' ? 'loading-ring' : ''} /><strong>{run.status === 'running' ? '模型正在分析真实行情' : '暂无真实结果'}</strong></div>
                  )}
                </div>
                <div className="ai-footer">
                  <span>真实 API 结果</span>
                  <span>{run.finishedAt ? new Date(run.finishedAt).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '每日一次'}</span>
                </div>
              </article>
            );
          })}
        </div>
      </section>

      <aside className="risk-banner"><span>研</span><div><strong>模型排序不等于投资建议</strong><p>所有策略仅在有限候选池和已取得的数据上排序；请自行核对公告、停牌、复权、财务报告期和交易风险。</p></div></aside>

      <section className="strategy-summary" aria-labelledby="daily-summary-title">
        <div className="summary-heading"><div><span className="section-index">03</span><div><p className="eyebrow">每日收束</p><h2 id="daily-summary-title">今日策略总结</h2></div></div><span>{shortTradeDate(strategies[0]?.tradeDate)}</span></div>
        <div className="summary-overview">
          <div className="summary-conclusion"><small>今日结论</small><h3>{summary.headline}</h3><p>{summary.ruleCount} 种规则策略已完成，其中 {summary.rulesWithPicks} 种给出候选；{summary.aiCount} 家 AI 已完成，共覆盖 {summary.uniquePicks} 只不重复股票。</p></div>
          <div className="summary-metrics">
            <div><strong>{summary.ruleCount}<small>/10</small></strong><span>规则策略</span></div>
            <div><strong>{summary.aiCount}<small>/3</small></strong><span>AI 完成</span></div>
            <div><strong>{summary.consensus.length}</strong><span>共识股票</span></div>
          </div>
        </div>
        <div className="summary-body">
          <div className="consensus-panel">
            <div className="summary-subhead"><strong>多策略共识</strong><span>至少被两种独立方法选中</span></div>
            {summary.consensus.length ? (
              <div className="consensus-list">{summary.consensus.map((item, index) => (
                <div key={item.code}><span className="consensus-rank">{index + 1}</span><div><strong>{item.name}</strong><small>{item.code} · {item.sources.join(' / ')}</small></div><span className="consensus-count"><b>{item.count}</b> 种</span></div>
              ))}</div>
            ) : <div className="summary-empty">暂无多策略共识，单一策略信号不作为总结结论。</div>}
          </div>
          <div className="summary-notes">
            <div className="summary-subhead"><strong>口径与提醒</strong><span>自动根据今日运行状态生成</span></div>
            <ul>{summary.notes.map((note) => <li key={note}>{note}</li>)}</ul>
            <div className="coverage-row"><span>覆盖方法</span><p>价值 · 动量 · 低波 · 多因子 · 均线 · RSI · 热门概念 · AI</p></div>
          </div>
        </div>
        <p className="summary-footnote">总结只统计页面中已成功取得的真实结果；“多策略共识”表示方法重合，不代表未来上涨概率或买入建议。</p>
      </section>
    </section>
  );
}
