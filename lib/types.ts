export const AI_PROVIDERS = ['deepseek', 'gemini', 'openai'] as const;
export type AiProvider = (typeof AI_PROVIDERS)[number];

export const STRATEGY_IDS = [
  'dividend',
  'momentum',
  'lowvol',
  'qlib_alpha158',
  'sma_cross',
  'rsi_rebound',
  'trend_confirmation',
  'value_momentum',
  'volume_breakout',
  'hot_concept',
] as const;
export type StrategyId = (typeof STRATEGY_IDS)[number];

export type StockBasic = {
  tsCode: string;
  symbol: string;
  name: string;
  area: string | null;
  industry: string | null;
  market: string | null;
  exchange: string;
  listDate: string | null;
};

export type Quote = {
  tsCode?: string;
  tradeDate: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number;
  preClose: number | null;
  change: number | null;
  pctChg: number | null;
  vol: number | null;
  amount: number | null;
  turnoverRate?: number | null;
  volumeRatio?: number | null;
  amplitude?: number | null;
  peTtm?: number | null;
  pb?: number | null;
  totalMv?: number | null;
  floatMv?: number | null;
  source: string;
  fetchedAt: string;
};

export type AiPick = {
  code: string;
  name: string;
  score: number;
  reason: string;
  risk: string;
};

export type AiResult = {
  title: string;
  summary: string;
  logic: string;
  picks: [AiPick, AiPick, AiPick];
};

export type WatchlistStock = StockBasic & {
  positionWeight: number;
  quote: Quote | null;
};

export type DataSourceStatus = {
  primary: string;
  fallback: string;
  source: string;
  transport: string;
  updatedAt: string | null;
  error: string | null;
  usingFallback: boolean;
  health: 'healthy' | 'degraded' | 'waiting';
  retryAt: string | null;
};

export type SystemStatus = {
  ok: boolean;
  database: boolean;
  providers: { marketData: boolean } & Record<AiProvider, boolean>;
  dataSource: DataSourceStatus;
  scheduler: { enabled: boolean; time: string; timezone: string };
};

export type AiRunView = {
  provider: AiProvider;
  model: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'not_configured';
  result: AiResult | null;
  error: string | null;
  finishedAt: string | null;
};

export type StrategyPick = {
  code: string;
  name: string;
  industry: string;
  score: number;
  reason: string;
};

export type PublicStrategyResult = {
  id: StrategyId;
  name: string;
  description: string;
  runDate: string;
  tradeDate: string;
  picks: StrategyPick[];
};

export type WatchlistResponse = {
  stocks: WatchlistStock[];
  dataSource: DataSourceStatus;
  updatedAt?: string;
};

export type StrategySummary = {
  consensus: Array<{
    code: string;
    name: string;
    sources: string[];
    count: number;
    averageScore: number;
  }>;
  ruleCount: number;
  rulesWithPicks: number;
  aiCount: number;
  uniquePicks: number;
  notes: string[];
  headline: string;
};
