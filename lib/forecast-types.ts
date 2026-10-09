import type { ConceptSource, ConceptRun, RecommendedConcept } from '@/lib/concept-types';

type Assessment = { summary: string; status: 'supported' | 'hypothesis'; sources: ConceptSource[] };

export type ForecastCandidateSelection = {
  method?: string; snapshotCount?: number; availableCount?: number; selectedCount?: number; limit?: number;
  historyCheckedCount?: number; historyAlignedCount?: number; historyFeatureReadyCount?: number;
  historyUnseenCount?: number; historyMissingOrStaleCount?: number; historyInsufficientCount?: number;
  channels?: Record<string, { availableCount: number; selectedCount: number }>;
  note?: string;
};

type HistorySource = { names: string[]; urls: string[]; note: string };
type HistoryCoverage = { observedDateCount: number; validCloseCount: number; invalidPriceDates: string[]; excludedRowCount: number };

export type ForecastRelativePeriod = {
  status: 'available' | 'unavailable'; startDate: string | null; endDate: string | null;
  sessionCount: number; priceCount: number; conceptReturnPct: number | null;
  benchmarkReturnPct: number | null; excessPctPoints: number | null; reason: string | null;
};

export type ForecastRelativeStrength = {
  historyAsOf: string | null; source: HistorySource; coverage: HistoryCoverage; limits: string[];
  benchmarks: { code: string; name: string; historyAsOf: string | null; source: HistorySource;
    coverage: HistoryCoverage; periods: Record<'5d' | '10d', ForecastRelativePeriod> }[];
};

export type ForecastRiskPair = {
  leftCode: string; rightCode: string;
  correlation20d: { status: 'available' | 'unavailable'; value: number | null;
    startDate: string | null; endDate: string | null; returnCount: number; requestedReturnCount: number;
    smallSample: boolean; source: string; reason: string | null };
  strongStockSampleOverlap: { leftSampleSize: number; rightSampleSize: number; sharedCodes: string[];
    sharedCount: number | null; jaccardPct: number | null; overlapOfSmallerSamplePct: number | null;
    source: string; note: string };
  sharedNewsSources: { urls: string[]; count: number; leftSourceCount: number; rightSourceCount: number;
    source: string; note: string };
};

export type ForecastRiskContext = {
  version: string; asOf: string; source: string; limits: string[]; pairs: ForecastRiskPair[];
  coverage: { candidateCount: number; pairCount: number; availableCorrelationPairCount: number;
    benchmarkObservedDateCount: number; pairAnalysisIncluded?: boolean };
};

export type ForecastConcept = Omit<RecommendedConcept, 'reason' | 'risk' | 'drivers'> & {
  thesis: string;
  conviction: 'medium' | 'low';
  confirmation: string;
  invalidation: string;
  technical: Assessment;
  fundamental: Assessment;
  news: Assessment;
  relativeStrength?: ForecastRelativeStrength | null;
  technicalData: {
    lastClose: number | null; ma5: number | null; ma10: number | null; ma20: number | null;
    rsi14: number | null; distanceTo20dHigh: number | null; amountRatio5d: number | null;
    historyAsOf: string | null;
    change3d?: number | null; change20d?: number | null; previousChange5d?: number | null;
    momentumChange5d?: number | null; distanceToMa20?: number | null; ma5Slope5d?: number | null;
    upDays5d?: number | null; drawdown5d?: number | null;
  };
  fundamentalData: {
    coverage: 'valuation_only' | 'missing'; sampleSize: number; note: string;
    valuations: { code: string; name: string; peDynamic: number | null; pb: number | null; marketCap: number | null }[];
  };
};

export type ForecastResearch = {
  conceptProvider?: 'ths' | 'eastmoney' | null;
  summary: string; tradeDate: string; dataAsOf: string; scope: string; warnings: string[];
  window: { generatedOn: string; startDate: string; endDate: string; calendarDays: number };
  concepts: ForecastConcept[];
  candidateSelection?: ForecastCandidateSelection;
  riskContext?: ForecastRiskContext;
  feedbackUsed?: { asOf: string | null; sampleCounts: Partial<Record<'15' | '30', number>>; missedLeaderCaseCount?: number };
};

export type ForecastRun = Omit<ConceptRun, 'result'> & { result: ForecastResearch | null };
