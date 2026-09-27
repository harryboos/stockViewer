'use client';

import { useCallback, useEffect, useState } from 'react';
import { errorMessage, jsonFetch } from './client-api';
import { startPolling } from './polling';

type Polling<T> = number | { active: number; idle: number; isActive: (value: T) => boolean };

// Each module owns its errors and retains its last successful response while refreshing.
export function useResearch<T>(url: string, interval: Polling<T> = 0) {
  const [record, setRecord] = useState<{ url: string; value: T } | null>(null);
  const [failure, setFailure] = useState<{ url: string; message: string } | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => startPolling<T>({
    read: signal => jsonFetch<T>(url, { signal, cache: 'no-store' }),
    onValue: value => { setRecord({ url, value }); setFailure(null); },
    onError: cause => setFailure({ url, message: errorMessage(cause, '读取失败，请重试') }),
    interval: typeof interval === 'number' ? interval
      : latest => latest && interval.isActive(latest) ? interval.active : interval.idle,
    refreshOnVisible: Boolean(interval),
  }), [url, interval, revision]);
  return { data: record?.url === url ? record.value : null, error: failure?.url === url ? failure.message : null, refresh };
}
