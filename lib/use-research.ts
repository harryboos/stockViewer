'use client';

import { useCallback, useEffect, useState } from 'react';
import { errorMessage, jsonFetch } from './client-api';

type Polling<T> = number | { active: number; idle: number; isActive: (value: T) => boolean };

// Each module owns its errors and retains its last successful response while refreshing.
export function useResearch<T>(url: string, interval: Polling<T> = 0) {
  const [record, setRecord] = useState<{ url: string; value: T } | null>(null);
  const [failure, setFailure] = useState<{ url: string; message: string } | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let latest: T | null = null;
    let reading = false;
    const schedule = () => {
      if (controller.signal.aborted || !interval) return;
      const delay = typeof interval === 'number' ? interval : latest && interval.isActive(latest) ? interval.active : interval.idle;
      timer = setTimeout(() => { if (document.hidden) schedule(); else void read(); }, delay);
    };
    const read = async () => {
      if (reading || controller.signal.aborted) return;
      reading = true;
      clearTimeout(timer);
      try {
        const value = await jsonFetch<T>(url, { signal: controller.signal, cache: 'no-store' });
        latest = value;
        if (!controller.signal.aborted) { setRecord({ url, value }); setFailure(null); }
      } catch (cause) {
        if (!controller.signal.aborted) setFailure({ url, message: errorMessage(cause, '读取失败，请重试') });
      } finally {
        reading = false;
        schedule();
      }
    };
    const visible = () => { if (!document.hidden && interval) void read(); };
    document.addEventListener('visibilitychange', visible);
    void read();
    return () => { controller.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, [url, interval, revision]);
  return { data: record?.url === url ? record.value : null, error: failure?.url === url ? failure.message : null, refresh };
}
