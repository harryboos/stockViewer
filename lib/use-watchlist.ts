'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { jsonFetch, errorMessage } from './client-api';
import { createRequestGate } from './request-gate';
import type { DataSourceStatus, StockBasic, WatchlistResponse, WatchlistStock } from './types';

export type WatchNote = { tsCode: string; groupName: string; reason: string; note: string };

export function useWatchlist(onSource: (source: DataSourceStatus) => void, onError: (message: string) => void, notify: (message: string) => void) {
  const [stocks, setStocks] = useState<WatchlistStock[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const gate = useRef(createRequestGate());
  const mutation = useRef(false);
  const apply = useCallback((data: WatchlistResponse) => {
    setStocks(data.stocks);
    onSource(data.dataSource);
  }, [onSource]);

  useEffect(() => {
    const requests = gate.current;
    const request = requests.begin();
    void (async () => {
      try {
        const cached = await jsonFetch<WatchlistResponse>('/api/watchlist?cached_only=true', { signal: request.signal });
        if (!request.isCurrent()) return;
        apply(cached);
        setLoading(false);
        const latest = await jsonFetch<WatchlistResponse>('/api/watchlist', { signal: request.signal });
        if (request.isCurrent()) apply(latest);
      } catch (cause) {
        if (request.isCurrent()) onError(errorMessage(cause, '自选行情读取失败'));
      } finally {
        if (request.isCurrent()) setLoading(false);
      }
    })();
    return () => requests.cancel();
  }, [apply, onError]);

  async function update(url: string, options?: RequestInit) {
    if (mutation.current) throw new Error('正在处理自选更新，请稍后再试');
    mutation.current = true;
    const request = gate.current.begin();
    setBusy(true);
    onError('');
    try {
      const data = await jsonFetch<WatchlistResponse>(url, { ...options, signal: request.signal });
      if (request.isCurrent()) apply(data);
    } finally {
      mutation.current = false;
      if (request.isCurrent()) { setBusy(false); setLoading(false); }
    }
  }

  async function add(stock: StockBasic) {
    await update('/api/watchlist', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tsCode: stock.tsCode }) });
    notify(`已将 ${stock.name} 加入自选`);
  }

  async function remove(stock: WatchlistStock) {
    try {
      await update(`/api/watchlist?tsCode=${encodeURIComponent(stock.tsCode)}`, { method: 'DELETE' });
      notify(`已将 ${stock.name} 移出自选`);
    } catch (cause) { onError(errorMessage(cause, '移除失败')); }
  }

  async function refresh() {
    try {
      await update('/api/watchlist?refresh=true');
      notify('免费行情已检查更新');
    } catch (cause) { onError(errorMessage(cause, '刷新失败')); }
  }

  async function saveNote(note: WatchNote) {
    await update('/api/watchlist', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(note) });
  }

  return { stocks, loading, busy, add, remove, refresh, saveNote };
}
