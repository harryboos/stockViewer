'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { AddStockModal } from '@/components/add-stock-modal';
import { AppHeader } from '@/components/app-header';
import { MarketOverviewView } from '@/components/market-overview-view';
import { SectorConceptView } from '@/components/sector-concept-view';
import { StrategiesView } from '@/components/strategies-view';
import { WatchlistView } from '@/components/watchlist-view';
import { errorMessage, isAbortError, jsonFetch } from '@/lib/client-api';
import { formatChinaDate } from '@/lib/format';
import { buildStrategySummary } from '@/lib/strategy-summary';
import {
  AI_PROVIDERS,
  type AiRunView,
  type MarketOverview,
  type PublicStrategyResult,
  type SectorOverview,
  type StockBasic,
  type SystemStatus,
  type WatchlistResponse,
  type WatchlistStock,
} from '@/lib/types';


type ActiveTab = 'watchlist' | 'strategies' | 'market' | 'sectors';

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
  const [error, setError] = useState('');
  const today = useMemo(() => formatChinaDate(), []);

  const applyWatchlist = useCallback((data: WatchlistResponse) => {
    setStocks(data.stocks);
    setStatus((current) => current ? { ...current, dataSource: data.dataSource } : current);
  }, []);

  useEffect(() => {
    Promise.all([
      jsonFetch<SystemStatus>('/api/system'),
      jsonFetch<WatchlistResponse>('/api/watchlist'),
    ]).then(([system, watchlist]) => {
      setStatus({ ...system, dataSource: watchlist.dataSource ?? system.dataSource });
      setStocks(watchlist.stocks);
    }).catch((reason) => setError(errorMessage(reason, '应用初始化失败')))
      .finally(() => setLoading(false));
  }, []);

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
        setSearchResults(data.stocks.filter(
          (item) => !stocks.some((stock) => stock.tsCode === item.tsCode),
        ));
      }).catch((reason) => {
        if (!isAbortError(reason)) setToast(errorMessage(reason, '搜索失败'));
      }).finally(() => setSearching(false));
    }, 320);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [addQuery, modalOpen, stocks]);

  const loadStrategies = useCallback(async (force = false) => {
    setStrategyLoading(true);
    setError('');
    try {
      if (force) {
        setAiRuns((runs) => runs.map((run) => (
          run.status === 'not_configured'
            ? run
            : { ...run, status: 'running', error: null }
        )));
        const [publicData, aiData] = await Promise.all([
          jsonFetch<{ status: string; strategies: PublicStrategyResult[] }>('/api/strategies/public?force=true'),
          jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai?force=true', { method: 'POST' }),
        ]);
        setPublicStrategies(publicData.strategies);
        setAiRuns(aiData.runs);
        const succeeded = aiData.runs.filter((run) => run.status === 'succeeded').length;
        setToast(`手动重跑完成，AI ${succeeded}/3 已完成`);
        return;
      }

      const [publicData, aiData] = await Promise.all([
        jsonFetch<{ status: string; strategies: PublicStrategyResult[] }>('/api/strategies/public'),
        jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai'),
      ]);
      setPublicStrategies(publicData.strategies);
      setAiRuns(aiData.runs);
      if (aiData.runs.some((run) => run.status === 'pending' || run.status === 'failed')) {
        setAiRuns((runs) => runs.map((run) => (
          run.status === 'pending' || run.status === 'failed'
            ? { ...run, status: 'running', error: null }
            : run
        )));
        const completed = await jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai', { method: 'POST' });
        setAiRuns(completed.runs);
      }
    } catch (reason) {
      setError(errorMessage(reason, '策略加载失败'));
    } finally {
      setStrategyLoading(false);
    }
  }, []);

  const rerunStrategies = useCallback(() => {
    const confirmed = window.confirm(
      '手动重跑会重新获取行情、重新计算全部规则策略，并再次调用所有已配置的 AI，可能产生接口费用。确定继续吗？',
    );
    if (confirmed) void loadStrategies(true);
  }, [loadStrategies]);

  const openStrategies = useCallback(() => {
    setActiveTab('strategies');
    if (!strategyLoading && !publicStrategies.length && !aiRuns.length) void loadStrategies();
  }, [aiRuns.length, loadStrategies, publicStrategies.length, strategyLoading]);

  const loadMarketOverview = useCallback(async (force = false) => {
    setMarketLoading(true);
    setError('');
    try {
      const queryString = force ? '?force=true' : '';
      setMarketOverview(await jsonFetch<MarketOverview>(`/api/market/overview${queryString}`));
    } catch (reason) {
      setError(errorMessage(reason, '大盘数据加载失败'));
    } finally {
      setMarketLoading(false);
    }
  }, []);

  const openMarket = useCallback(() => {
    setActiveTab('market');
    if (!marketLoading && !marketOverview) void loadMarketOverview();
  }, [loadMarketOverview, marketLoading, marketOverview]);

  const loadSectorOverview = useCallback(async (force = false) => {
    setSectorLoading(true);
    setError('');
    try {
      const queryString = force ? '?force=true' : '';
      setSectorOverview(await jsonFetch<SectorOverview>(`/api/market/sectors${queryString}`));
    } catch (reason) {
      setError(errorMessage(reason, '板块概念数据加载失败'));
    } finally {
      setSectorLoading(false);
    }
  }, []);

  const openSectors = useCallback(() => {
    setActiveTab('sectors');
    if (!sectorLoading && !sectorOverview) void loadSectorOverview();
  }, [loadSectorOverview, sectorLoading, sectorOverview]);

  async function addStock(stock: StockBasic) {
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
    }
  }

  async function removeStock(stock: WatchlistStock) {
    try {
      const data = await jsonFetch<WatchlistResponse>(
        `/api/watchlist?tsCode=${encodeURIComponent(stock.tsCode)}`,
        { method: 'DELETE' },
      );
      applyWatchlist(data);
      setToast(`已将 ${stock.name} 移出自选`);
    } catch (reason) {
      setToast(errorMessage(reason, '移除失败'));
    }
  }

  async function refreshData() {
    setLoading(true);
    setError('');
    try {
      applyWatchlist(await jsonFetch<WatchlistResponse>('/api/watchlist?refresh=true'));
      setToast('免费行情已检查更新');
    } catch (reason) {
      setError(errorMessage(reason, '刷新失败'));
    } finally {
      setLoading(false);
    }
  }

  function updateAddQuery(value: string) {
    setAddQuery(value);
    if (!value.trim()) setSearchResults([]);
  }

  const tradeDate = stocks.find((stock) => stock.quote)?.quote?.tradeDate ?? marketOverview?.tradeDate ?? sectorOverview?.tradeDate ?? null;
  const completedAiCount = aiRuns.filter((run) => run.status === 'succeeded').length;
  const missingAiCount = status
    ? AI_PROVIDERS.filter((provider) => !status.providers[provider]).length
    : 0;
  const strategySummary = useMemo(
    () => buildStrategySummary(publicStrategies, aiRuns, status?.dataSource.source ?? ''),
    [aiRuns, publicStrategies, status?.dataSource.source],
  );

  return (
    <main className="app-shell">
      <AppHeader
        activeTab={activeTab}
        status={status}
        tradeDate={tradeDate}
        onOpenWatchlist={() => setActiveTab('watchlist')}
        onOpenStrategies={openStrategies}
        onOpenMarket={openMarket}
        onOpenSectors={openSectors}
      />

      {error && (
        <div className="global-error" role="alert"><span>!</span><strong>{error}</strong><button onClick={() => setError('')}>关闭</button></div>
      )}

      {activeTab === 'watchlist' ? (
        <WatchlistView
          today={today}
          stocks={stocks}
          query={query}
          loading={loading}
          missingAiCount={missingAiCount}
          completedAiCount={completedAiCount}
          onQueryChange={setQuery}
          onRefresh={() => void refreshData()}
          onOpenAdd={() => setModalOpen(true)}
          onOpenStrategies={openStrategies}
          onRemove={(stock) => void removeStock(stock)}
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
        />
      ) : activeTab === 'market' ? (
        <MarketOverviewView
          today={today}
          data={marketOverview}
          loading={marketLoading}
          onRefresh={() => void loadMarketOverview(true)}
        />
      ) : (
        <SectorConceptView
          today={today}
          data={sectorOverview}
          loading={sectorLoading}
          onRefresh={() => void loadSectorOverview(true)}
        />
      )}

      {modalOpen && (
        <AddStockModal
          query={addQuery}
          searching={searching}
          results={searchResults}
          onQueryChange={updateAddQuery}
          onAdd={(stock) => void addStock(stock)}
          onClose={() => setModalOpen(false)}
        />
      )}

      {toast && <div className="toast" role="status"><span>✓</span>{toast}</div>}
    </main>
  );
}
