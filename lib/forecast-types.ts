import type { ConceptSource, ConceptRun, RecommendedConcept } from '@/lib/concept-types';

type Assessment = { summary: string; status: 'supported' | 'hypothesis'; sources: ConceptSource[] };

export type ForecastConcept = Omit<RecommendedConcept, 'reason' | 'risk' | 'drivers'> & {
  thesis: string;
  conviction: 'medium' | 'low';
  confirmation: string;
  invalidation: string;
  technical: Assessment;
  fundamental: Assessment;
  news: Assessment;
  technicalData: {
    lastClose: number | null; ma5: number | null; ma10: number | null; ma20: number | null;
    rsi14: number | null; distanceTo20dHigh: number | null; amountRatio5d: number | null;
    historyAsOf: string | null;
  };
  fundamentalData: {
    coverage: 'valuation_only' | 'missing'; sampleSize: number; note: string;
    valuations: { code: string; name: string; peDynamic: number | null; pb: number | null; marketCap: number | null }[];
  };
};

export type ForecastResearch = {
  summary: string; tradeDate: string; dataAsOf: string; scope: string; warnings: string[];
  window: { generatedOn: string; startDate: string; endDate: string; calendarDays: number };
  concepts: ForecastConcept[];
};

export type ForecastRun = Omit<ConceptRun, 'result'> & { result: ForecastResearch | null };
