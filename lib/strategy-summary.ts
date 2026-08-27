import { AI_META } from '@/lib/strategy-meta';
import type { AiRunView, PublicStrategyResult, StrategySummary } from '@/lib/types';


type Signal = {
  code: string;
  name: string;
  sources: string[];
  scoreTotal: number;
  scoreCount: number;
};

export function buildStrategySummary(
  publicStrategies: PublicStrategyResult[],
  aiRuns: AiRunView[],
  dataSourceName: string,
): StrategySummary {
  const signals = new Map<string, Signal>();
  const addSignal = (code: string, name: string, source: string, score: number) => {
    const current = signals.get(code) ?? { code, name, sources: [], scoreTotal: 0, scoreCount: 0 };
    if (!current.sources.includes(source)) current.sources.push(source);
    current.scoreTotal += score;
    current.scoreCount += 1;
    signals.set(code, current);
  };

  publicStrategies.forEach((strategy) => {
    strategy.picks.forEach((pick) => addSignal(pick.code, pick.name, strategy.name, pick.score));
  });
  aiRuns.forEach((run) => {
    run.result?.picks.forEach((pick) => addSignal(pick.code, pick.name, AI_META[run.provider].model, pick.score));
  });

  const ranked = [...signals.values()]
    .map((item) => ({
      ...item,
      count: item.sources.length,
      averageScore: Math.round(item.scoreTotal / Math.max(item.scoreCount, 1)),
    }))
    .sort((left, right) => right.count - left.count || right.averageScore - left.averageScore);
  const consensus = ranked.filter((item) => item.count >= 2).slice(0, 5);
  const ruleCount = publicStrategies.filter((strategy) => strategy.runDate).length;
  const rulesWithPicks = publicStrategies.filter((strategy) => strategy.picks.length > 0).length;
  const aiCount = aiRuns.filter((run) => run.status === 'succeeded').length;
  const notes: string[] = [];
  const hotConcept = publicStrategies.find((strategy) => strategy.id === 'hot_concept');
  const strictScreen = publicStrategies.find((strategy) => strategy.id === 'volume_breakout');
  const rsiScreen = publicStrategies.find((strategy) => strategy.id === 'rsi_rebound');

  if (dataSourceName.includes('BaoStock')) notes.push('当前行情已降级到 BaoStock 日线，盘中量比与热门概念可能缺失。');
  if (hotConcept && !hotConcept.picks.length) notes.push('热门概念今日无可用结果，不使用旧日期热点补位。');
  if (strictScreen && !strictScreen.picks.length) notes.push('强势缩量筛选今日没有股票命中全部条件。');
  if (rsiScreen && !rsiScreen.picks.length) notes.push('RSI 超跌回升是严格触发策略，今日没有信号属于正常情况。');
  if (aiRuns.length && aiCount < 3) notes.push(`AI 当前完成 ${aiCount}/3；未配置或失败的模型不参与共识统计。`);
  if (publicStrategies.length && !consensus.length) notes.push('各方法选股分歧较大，暂未形成至少两种策略的共同关注。');
  if (!publicStrategies.length) notes.push('点击“今日策略状态”后，将根据真实规则与 AI 结果生成总结。');
  if (!notes.length) notes.push('今日策略与数据源运行完整，暂未检测到需要额外提示的降级状态。');

  return {
    consensus,
    ruleCount,
    rulesWithPicks,
    aiCount,
    uniquePicks: ranked.length,
    notes,
    headline: !publicStrategies.length
      ? '运行今日策略后，这里会自动形成总结'
      : consensus[0]
        ? `${consensus[0].name} 获得 ${consensus[0].count} 种方法共同关注`
        : '今日信号分散，暂未形成多策略共识',
  };
}
