'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AddStockModal } from '@/components/add-stock-modal';
import { AppHeader } from '@/components/app-header';
import { ConceptForecast } from '@/components/concept-forecast';
import { MarketOverviewView } from '@/components/market-overview-view';
import { SectorConceptView } from '@/components/sector-concept-view';
import { StrategiesView } from '@/components/strategies-view';
import { WatchlistView } from '@/components/watchlist-view';
import { StockDetailProvider } from '@/components/stock-detail';
import { DailyDigest, OperationsView } from '@/components/research-panels';
import { errorMessage, isAbortError, jsonFetch } from '@/lib/client-api';
import { formatChinaDate } from '@/lib/format';
import { buildStrategySummary } from '@/lib/strategy-summary';
import {
  AI_PROVIDERS,
  type AiRunView,
  type AiProvider,
  type MarketOverview,
  type PublicStrategyResult,
  type SectorOverview,
  type StockBasic,
  type SystemStatus,
  type WatchlistResponse,
  type WatchlistStock,
} from '@/lib/types';


type ActiveTab = 'watchlist' | 'strategies' | 'market' | 'sectors' | 'forecast' | 'digest' | 'operations';

export default function Home() {
  const [activeTab, setActiveTab] = useState<ActiveTab>('watchlist');
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [stocks, setStocks] = useState<WatchlistStock[]>([]);
  const [publicStrategies, setPublicStrategies] = useState<PublicStrategyResult[]>([]);
  const [aiRuns, setAiRuns] = useState<AiRunView[]>([]);
  const [marketOverview, setMarketOverview] = useState<MarketOverview | null>(null);
  const [sectorOverview, setSectorOverview] = useState<SectorOverview | null>(null);
  const [query, setQuery] = useState('');
  const [addQuery, setAddQuery] = useState('');
  const [searchResults, setSearchResults] = useState<StockBasic[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [strategyLoading, setStrategyLoading] = useState(false);
  const [marketLoading, setMarketLoading] = useState(false);
  const [sectorLoading, setSectorLoading] = useState(false);
  const [searching, setSearching] = useState(false);
  const [toast, setToast] = useState('');
  const [errors, setErrors] = useState<Partial<Record<ActiveTab, string>>>({});
  const setError = useCallback((value: string, tab: ActiveTab = 'watchlist') => setErrors(current => ({ ...current, [tab]: value })), []);
  const [aiSubmitting, setAiSubmitting] = useState(false);
  const aiSubmitLock = useRef(false);
  const [watchlistBusy, setWatchlistBusy] = useState(false);
  const watchlistLock = useRef(false);
  const strategyLock = useRef(false);
  const today = formatChinaDate();

  useEffect(() => { window.scrollTo({ top: 0, behavior: 'instant' }); }, [activeTab]);

  const applyWatchlist = useCallback((data: WatchlistResponse) => {
    setStocks(data.stocks);
    setStatus((current) => current ? { ...current, dataSource: data.dataSource } : current);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    void jsonFetch<SystemStatus>('/api/system', options).then(value => { if (!controller.signal.aborted) setStatus(value); }).catch(cause => { if (!controller.signal.aborted) setError(errorMessage(cause, '系统状态读取失败'), 'operations'); });
    const readWatchlist = async () => {
      try {
        const cached = await jsonFetch<WatchlistResponse>('/api/watchlist?cached_only=true', options);
        if (controller.signal.aborted) return;
        applyWatchlist(cached); setLoading(false);
        const latest = await jsonFetch<WatchlistResponse>('/api/watchlist', options);
        if (!controller.signal.aborted) applyWatchlist(latest);
      } catch (cause) {
        if (!controller.signal.aborted) setError(errorMessage(cause, '自选行情读取失败'));
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void readWatchlist();
    void jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai', options).then(value => { if (!controller.signal.aborted) setAiRuns(value.runs); }).catch(cause => { if (!controller.signal.aborted) setError(errorMessage(cause, 'AI 状态读取失败'), 'strategies'); });
    return () => controller.abort();
  }, [applyWatchlist, setError]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 2300);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!modalOpen || !addQuery.trim()) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSearching(true);
      jsonFetch<{ stocks: StockBasic[] }>(
        `/api/stocks/search?q=${encodeURIComponent(addQuery)}`,
        { signal: controller.signal },
      ).then((data) => {
        if (controller.signal.aborted) return;
        setSearchResults(data.stocks.filter(
          (item) => !stocks.some((stock) => stock.tsCode === item.tsCode),
        ));
      }).catch((reason) => {
        if (!controller.signal.aborted && !isAbortError(reason)) setToast(errorMessage(reason, '搜索失败'));
      }).finally(() => {
        if (!controller.signal.aborted) setSearching(false);
      });
    }, 320);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [addQuery, modalOpen, stocks]);

  const loadStrategies = useCallback(async (force = false) => {
    if (strategyLock.current) return;
    strategyLock.current = true;
    setStrategyLoading(true);
    setError('', 'strategies');
    const loadRules = async () => {
      const data = await jsonFetch<{ strategies: PublicStrategyResult[] }>(`/api/strategies/public${force ? '?force=true' : ''}`);
      setPublicStrategies(data.strategies);
    };
    const loadAi = async () => {
      const aiData = await jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai');
      setAiRuns(aiData.runs);
    };
    try {
      const results = await Promise.allSettled([loadRules(), loadAi()]);
      const failures = results.filter((result) => result.status === 'rejected');
      if (failures.length) setError(failures.map((result) => errorMessage(result.reason, '策略加载失败')).join('；'), 'strategies');
      else if (force) setToast('规则策略已更新，AI 结果保持原样');
    } finally {
      strategyLock.current = false;
      setStrategyLoading(false);
    }
  }, [setError]);

  async function generateAi(provider?: AiProvider, failedOnly = false) {
    if (aiSubmitLock.current) return;
    aiSubmitLock.current = true; setAiSubmitting(true); setError('', 'strategies');
    const params = new URLSearchParams();
    if (provider) params.set('provider', provider);
    if (failedOnly) params.set('failed_only', 'true');
    try {
      const result = await jsonFetch<{ runs: AiRunView[] }>(`/api/strategies/ai?${params}`, { method: 'POST' });
      setAiRuns(result.runs);
    } catch (cause) { setError(errorMessage(cause, 'AI 生成失败，稍后检查运行状态'), 'strategies'); }
    finally { aiSubmitLock.current = false; setAiSubmitting(false); }
  }

  const hasRunningAi = aiRuns.some((run) => run.status === 'running');
  useEffect(() => {
    if (!hasRunningAi && !aiSubmitting) return;
    const controller = new AbortController();
    let timer: number;
    const poll = async () => {
      try {
        const data = await jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai', { signal: controller.signal });
        if (!controller.signal.aborted) setAiRuns(data.runs);
      } catch {
        // A temporary connection failure should not trigger another paid run.
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 5000);
      }
    };
    timer = window.setTimeout(poll, 3000);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [hasRunningAi, aiSubmitting]);

  const rerunStrategies = useCallback(() => {
    void loadStrategies(true);
  }, [loadStrategies]);

  const openStrategies = useCallback(() => {
    setActiveTab('strategies');
    if (!strategyLoading && !publicStrategies.length) void loadStrategies();
  }, [loadStrategies, publicStrategies.length, strategyLoading]);

  const loadMarketOverview = useCallback(async (force = false) => {
    setMarketLoading(true);
    setError('', 'market');
    try {
      const queryString = force ? '?force=true' : '';
      setMarketOverview(await jsonFetch<MarketOverview>(`/api/market/overview${queryString}`));
    } catch (reason) {
      setError(errorMessage(reason, '大盘数据加载失败'), 'market');
    } finally {
      setMarketLoading(false);
    }
  }, [setError]);

  const openMarket = useCallback(() => {
    setActiveTab('market');
    if (!marketLoading && !marketOverview) void loadMarketOverview();
  }, [loadMarketOverview, marketLoading, marketOverview]);

  const loadSectorOverview = useCallback(async (force = false) => {
    setSectorLoading(true);
    setError('', 'sectors');
    try {
      const queryString = force ? '?force=true' : '';
      setSectorOverview(await jsonFetch<SectorOverview>(`/api/market/sectors${queryString}`));
    } catch (reason) {
      setError(errorMessage(reason, '板块概念数据加载失败'), 'sectors');
    } finally {
      setSectorLoading(false);
    }
  }, [setError]);

  const openSectors = useCallback(() => {
    setActiveTab('sectors');
    if (!sectorLoading && !sectorOverview) void loadSectorOverview();
  }, [loadSectorOverview, sectorLoading, sectorOverview]);

  async function addStock(stock: StockBasic) {
    if (watchlistLock.current || loading) return;
    watchlistLock.current = true;
    setWatchlistBusy(true);
    try {
      const data = await jsonFetch<WatchlistResponse>('/api/watchlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tsCode: stock.tsCode }),
      });
      applyWatchlist(data);
      setToast(`已将 ${stock.name} 加入自选`);
      setAddQuery('');
      setSearchResults([]);
    } catch (reason) {
      setToast(errorMessage(reason, '添加失败'));
    } finally {
      watchlistLock.current = false;
      setWatchlistBusy(false);
    }
  }

  async function removeStock(stock: WatchlistStock) {
    if (watchlistLock.current || loading) return;
    watchlistLock.current = true;
    setWatchlistBusy(true);
    try {
      const data = await jsonFetch<WatchlistResponse>(
        `/api/watchlist?tsCode=${encodeURIComponent(stock.tsCode)}`,
        { method: 'DELETE' },
      );
      applyWatchlist(data);
      setToast(`已将 ${stock.name} 移出自选`);
    } catch (reason) {
      setToast(errorMessage(reason, '移除失败'));
    } finally {
      watchlistLock.current = false;
      setWatchlistBusy(false);
    }
  }

  async function refreshData() {
    if (watchlistLock.current || loading) return;
    watchlistLock.current = true;
    setLoading(true);
    setError('');
    try {
      applyWatchlist(await jsonFetch<WatchlistResponse>('/api/watchlist?refresh=true'));
      setToast('免费行情已检查更新');
    } catch (reason) {
      setError(errorMessage(reason, '刷新失败'));
    } finally {
      watchlistLock.current = false;
      setLoading(false);
    }
  }

  function updateAddQuery(value: string) {
    setAddQuery(value);
    setSearchResults([]);
    setSearching(Boolean(value.trim()));
  }

  const tradeDate = activeTab === 'market' ? marketOverview?.tradeDate
    : activeTab === 'sectors' ? sectorOverview?.tradeDate
      : activeTab === 'strategies' ? publicStrategies[0]?.tradeDate
        : stocks.reduce<string | null>((latest, stock) => {
          const date = stock.quote?.tradeDate;
          return date && (!latest || date > latest) ? date : latest;
        }, null);
  const completedAiCount = aiRuns.filter((run) => run.status === 'succeeded').length;
  const missingAiCount = status
    ? AI_PROVIDERS.filter((provider) => !status.providers[provider]).length
    : 0;
  const strategySummary = useMemo(
    () => buildStrategySummary(publicStrategies, aiRuns, status?.dataSource.source ?? ''),
    [aiRuns, publicStrategies, status?.dataSource.source],
  );

  return (
    <StockDetailProvider><div className="app-shell">
      <a className="skip-link" href="#main-content">跳至页面内容</a>
      <AppHeader
        activeTab={activeTab}
        status={status}
        tradeDate={tradeDate ?? null}
        onOpenWatchlist={() => setActiveTab('watchlist')}
        onOpenStrategies={openStrategies}
        onOpenMarket={openMarket}
        onOpenSectors={openSectors}
        onOpenForecast={() => setActiveTab('forecast')}
        onOpenDigest={() => setActiveTab('digest')}
        onOpenOperations={() => setActiveTab('operations')}
      />

      <main id="main-content" className="app-main" tabIndex={-1}>
      {errors[activeTab] && (
        <div className="global-error" role="alert"><span>!</span><strong>{errors[activeTab]}</strong><button onClick={() => setError('', activeTab)}>关闭</button></div>
      )}

      {activeTab === 'watchlist' ? (
        <WatchlistView
          today={today}
          stocks={stocks}
          query={query}
          loading={loading || watchlistBusy}
          missingAiCount={missingAiCount}
          completedAiCount={completedAiCount}
          onQueryChange={setQuery}
          onRefresh={() => void refreshData()}
          onOpenAdd={() => setModalOpen(true)}
          onOpenStrategies={openStrategies}
          onRemove={(stock) => void removeStock(stock)}
          onUpdated={applyWatchlist}
        />
      ) : activeTab === 'strategies' ? (
        <StrategiesView
          today={today}
          status={status}
          strategies={publicStrategies}
          aiRuns={aiRuns}
          summary={strategySummary}
          loading={strategyLoading}
          onLoad={() => void loadStrategies()}
          onRerun={rerunStrategies}
          aiLoading={aiSubmitting}
          onGenerateAi={(provider, failedOnly) => void generateAi(provider, failedOnly)}
        />
      ) : activeTab === 'market' ? (
        <MarketOverviewView
          today={today}
          data={marketOverview}
          loading={marketLoading}
          onRefresh={() => void loadMarketOverview(true)}
        />
      ) : activeTab === 'digest' ? (
        <DailyDigest navigate={tab => { if (tab === 'market') openMarket(); else if (tab === 'sectors') openSectors(); else if (tab === 'strategies') openStrategies(); else setActiveTab(tab); }} />
      ) : activeTab === 'operations' ? (
        <OperationsView />
      ) : activeTab === 'forecast' ? (
        <ConceptForecast />
      ) : (
        <SectorConceptView
          today={today}
          data={sectorOverview}
          loading={sectorLoading}
          onRefresh={() => void loadSectorOverview(true)}
        />
      )}

      </main>
      {modalOpen && (
        <AddStockModal
          query={addQuery}
          searching={searching}
          adding={watchlistBusy}
          results={searchResults}
          onQueryChange={updateAddQuery}
          onAdd={(stock) => void addStock(stock)}
          onClose={() => { setModalOpen(false); updateAddQuery(''); }}
        />
      )}

      {toast && <div className="toast" role="status"><span>✓</span>{toast}</div>}
    </div></StockDetailProvider>
  );
}
