'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';

import { AddStockModal } from '@/components/add-stock-modal';
import { AppHeader } from '@/components/app-header';
import { StrategiesView } from '@/components/strategies-view';
import { WatchlistView } from '@/components/watchlist-view';
import { errorMessage, isAbortError, jsonFetch } from '@/lib/client-api';
import { formatChinaDate } from '@/lib/format';
import { buildStrategySummary } from '@/lib/strategy-summary';
import {
  AI_PROVIDERS,
  type AiRunView,
  type PublicStrategyResult,
  type StockBasic,
  type SystemStatus,
  type WatchlistResponse,
  type WatchlistStock,
} from '@/lib/types';


type ActiveTab = 'watchlist' | 'strategies';

export default function Home() {
  const [activeTab, setActiveTab] = useState<ActiveTab>('watchlist');
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [stocks, setStocks] = useState<WatchlistStock[]>([]);
  const [publicStrategies, setPublicStrategies] = useState<PublicStrategyResult[]>([]);
  const [aiRuns, setAiRuns] = useState<AiRunView[]>([]);
  const [query, setQuery] = useState('');
  const [addQuery, setAddQuery] = useState('');
  const [searchResults, setSearchResults] = useState<StockBasic[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [strategyLoading, setStrategyLoading] = useState(false);
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

  const loadStrategies = useCallback(async () => {
    setStrategyLoading(true);
    setError('');
    try {
      const [publicData, aiData] = await Promise.all([
        jsonFetch<{ status: string; strategies: PublicStrategyResult[] }>('/api/strategies/public'),
        jsonFetch<{ runs: AiRunView[] }>('/api/strategies/ai'),
      ]);
      setPublicStrategies(publicData.strategies);
      setAiRuns(aiData.runs);
      if (aiData.runs.some((run) => run.status === 'pending')) {
        setAiRuns((runs) => runs.map((run) => (
          run.status === 'pending' ? { ...run, status: 'running' } : run
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

  const openStrategies = useCallback(() => {
    setActiveTab('strategies');
    if (!strategyLoading && !publicStrategies.length && !aiRuns.length) void loadStrategies();
  }, [aiRuns.length, loadStrategies, publicStrategies.length, strategyLoading]);

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

  const tradeDate = stocks.find((stock) => stock.quote)?.quote?.tradeDate ?? null;
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
      ) : (
        <StrategiesView
          today={today}
          status={status}
          strategies={publicStrategies}
          aiRuns={aiRuns}
          summary={strategySummary}
          loading={strategyLoading}
          onLoad={() => void loadStrategies()}
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
