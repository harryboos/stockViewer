export type ConceptSource = {
  id: string;
  kind: 'data' | 'news';
  title: string;
  excerpt: string;
  url: string;
  source: string;
  publishedAt: string;
};

export type RecommendedConcept = {
  code: string;
  name: string;
  tradeDate: string;
  pctChg: number;
  amount: number | null;
  amountDelta: number | null;
  breadth: number;
  upCount: number;
  downCount: number;
  mainNetInflow: number | null;
  change5d: number;
  change10d: number | null;
  reason: string;
  risk: string;
  warnings: string[];
  stocks: {
    code: string; name: string; price: number; pctChg: number; amount: number;
    turnoverRate: number | null; tradeDate: string; reason: string;
  }[];
  drivers: {
    kind: 'data' | 'news' | 'hypothesis'; title: string; explanation: string; sources: ConceptSource[];
  }[];
};

export type ConceptResearch = {
  summary: string;
  tradeDate: string;
  dataAsOf: string;
  scope: string;
  warnings: string[];
  concepts: RecommendedConcept[];
};

export type ConceptRun = {
  provider: 'glm';
  model: string | null;
  runDate: string;
  status: 'idle' | 'running' | 'succeeded' | 'failed' | 'not_configured';
  result: ConceptResearch | null;
  error: string | null;
  finishedAt: string | null;
};
