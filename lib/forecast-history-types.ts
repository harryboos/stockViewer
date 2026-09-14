import type { ForecastResearch } from '@/lib/forecast-types';

export type Horizon = '15' | '30';
export type BenchmarkCode = 'sh000001' | 'sh000688';
export type BenchmarkOutcome = {
  name: string; returnPct: number | null; excessPct: number | null;
  entryPrice?: number | null; exitPrice?: number | null; source?: string | null; url?: string | null;
};
export type ForecastOutcome = {
  status: 'pending' | 'tracking' | 'completed' | 'missing_data'; targetDate: string; note: string;
  returnPct: number | null; entryDate: string | null; exitDate: string | null;
  entryPrice: number | null; exitPrice: number | null; maxDrawdownPct: number | null;
  maxRisePct: number | null; maxFallPct: number | null; checkedAt?: string; source?: string | null; url?: string | null;
  benchmarks: Partial<Record<BenchmarkCode, BenchmarkOutcome>>;
};
export type FeedbackSummary = {
  sampleCount: number; totalCount: number; trackingCount: number; missingCount: number;
  averageReturnPct: number | null; positiveRate: number | null; averageDrawdownPct: number | null;
  benchmarks: Record<BenchmarkCode, { name: string; sampleCount: number; averageReturnPct: number | null;
    averageExcessPct: number | null; outperformRate: number | null }>;
};
export type HistoryEntry = {
  id: number; runDate: string; publishedAt: string; model: string; promptVersion: string | null;
  includedInStats: boolean; checkedAt: string | null; summary: string; window: ForecastResearch['window'];
  concepts: { code: string; name: string; outcomes: Record<Horizon, ForecastOutcome> }[];
};
export type FeedbackRefresh = { status: 'idle' | 'running' | 'succeeded' | 'failed'; finishedAt: string | null; error: string | null };
export type ForecastHistoryData = {
  reports: HistoryEntry[]; page: number; pageSize: number; totalReports: number; forecastDays: number; abstentionDays: number;
  summaries: Record<Horizon, FeedbackSummary>; refresh: FeedbackRefresh; asOf: string;
};
