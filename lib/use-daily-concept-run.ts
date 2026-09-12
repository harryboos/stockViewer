'use client';

import { useEffect, useRef, useState } from 'react';
import { errorMessage, jsonFetch } from '@/lib/client-api';
import type { ConceptRun } from '@/lib/concept-types';

export function chinaDay() {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
}

// Reading a tab never starts paid work. Both modules keep their own daily server cache.
export function useDailyConceptRun<Result>(endpoint: string, label: string) {
  type Run = Omit<ConceptRun, 'result'> & { result: Result | null };
  const [run, setRun] = useState<Run | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const [today, setToday] = useState(chinaDay);
  const submittingRef = useRef(false);
  const requestRef = useRef<AbortController | null>(null);
  const refresh = () => setRevision((value) => value + 1);

  useEffect(() => {
    const timer = window.setInterval(() => setToday(chinaDay()), 60_000);
    return () => { window.clearInterval(timer); requestRef.current?.abort(); };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function read() {
      try {
        const next = await jsonFetch<Run>(endpoint, { signal: controller.signal, cache: 'no-store' });
        if (controller.signal.aborted) return;
        setRun(next);
        setRequestError(null);
        if (next.status === 'running') timer = setTimeout(read, 3000);
      } catch (error) {
        if (controller.signal.aborted) return;
        setRequestError(errorMessage(error, `${label}读取失败`));
        timer = setTimeout(read, 10_000);
      }
    }
    void read();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [endpoint, label, revision, today]);

  const current = run?.runDate === today ? run : null;
  const result = current?.status === 'succeeded' ? current.result : null;
  const busy = submitting || current?.status === 'running';

  async function generate() {
    if (submittingRef.current || busy || !current || current.status === 'not_configured') return;
    const force = Boolean(result);
    if (force && !window.confirm(`重新分析会更新今天的${label}，并再次调用 AI。继续吗？`)) return;
    submittingRef.current = true;
    setSubmitting(true);
    setRequestError(null);
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const next = await jsonFetch<Run>(`${endpoint}${force ? '?force=true' : ''}`, { method: 'POST', signal: controller.signal });
      if (!controller.signal.aborted) setRun(next);
    } catch (error) {
      if (!controller.signal.aborted) setRequestError(errorMessage(error, `${label}生成失败`));
    } finally {
      submittingRef.current = false;
      if (!controller.signal.aborted) { setSubmitting(false); refresh(); }
    }
  }

  return { current, result, busy, submitting, requestError, today, generate, refresh };
}
