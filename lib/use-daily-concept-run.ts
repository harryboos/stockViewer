'use client';

import { useEffect, useRef, useState } from 'react';
import { errorMessage, jsonFetch } from '@/lib/client-api';
import type { ConceptRun } from '@/lib/concept-types';
import { startPolling } from './polling';
import { submissionMessage, type SubmissionFailure } from './task-submission';
import { chinaDay } from './format';

type DailyConceptRun<Result> = Omit<ConceptRun, 'result' | 'previousResult'> & { result: Result | null; previousResult?: Result | null };

export function dailyConceptRunState<Result>(run: DailyConceptRun<Result> | null, today: string) {
  // A paid task can cross midnight or survive a provider migration. Keep it
  // visible and locked, while yesterday's results remain in their own archive.
  const current = run?.runDate === today || run?.status === 'running' ? run : null;
  const result = current?.runDate === today
    ? current.status === 'succeeded' ? current.result : current.previousResult ?? null
    : null;
  return { current, result, running: current?.status === 'running' };
}

// Reading a tab never starts paid work. Both modules keep their own daily server cache.
export function useDailyConceptRun<Result>(endpoint: string, label: string) {
  type Run = DailyConceptRun<Result>;
  const [run, setRun] = useState<Run | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [submissionFailure, setSubmissionFailure] = useState<(SubmissionFailure & { runDate: string }) | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const [today, setToday] = useState(chinaDay);
  const submittingRef = useRef(false);
  const requestRef = useRef<AbortController | null>(null);
  const stopPollingRef = useRef<(() => void) | null>(null);
  const refresh = () => setRevision((value) => value + 1);

  useEffect(() => {
    const timer = window.setInterval(() => setToday(chinaDay()), 60_000);
    return () => { window.clearInterval(timer); requestRef.current?.abort(); };
  }, []);

  useEffect(() => {
    if (submittingRef.current) return;
    const stop = startPolling<Run>({
      read: signal => jsonFetch<Run>(endpoint, { signal, cache: 'no-store' }),
      onValue: next => { setRun(next); setReadError(null); },
      onError: error => setReadError(errorMessage(error, `${label}读取失败`)),
      interval: next => next?.status === 'running' ? 3000 : 0,
      retryInterval: 10_000,
    });
    stopPollingRef.current = stop;
    return () => { stop(); if (stopPollingRef.current === stop) stopPollingRef.current = null; };
  }, [endpoint, label, revision, today]);

  const { current, result, running } = dailyConceptRunState(run, today);
  const requestError = submissionMessage(submissionFailure?.runDate === today ? submissionFailure : null, current?.startedAt) || readError;
  const busy = submitting || running;

  async function generate() {
    if (submittingRef.current || busy || !current || current.status === 'not_configured') return;
    const force = Boolean(result);
    if (force && !window.confirm(`重新分析会更新今天的${label}，并再次调用 AI。继续吗？`)) return;
    submittingRef.current = true;
    // An earlier status response must not overwrite the submitted task or its error.
    stopPollingRef.current?.();
    setSubmitting(true);
    setReadError(null);
    setSubmissionFailure(null);
    const controller = new AbortController();
    requestRef.current = controller;
    try {
      const next = await jsonFetch<Run>(`${endpoint}${force ? '?force=true' : ''}`, { method: 'POST', signal: controller.signal });
      if (!controller.signal.aborted) setRun(next);
    } catch (error) {
      if (!controller.signal.aborted) setSubmissionFailure({ message: errorMessage(error, `${label}生成失败`), previousStartedAt: current.startedAt ?? null, runDate: today });
    } finally {
      submittingRef.current = false;
      if (!controller.signal.aborted) { setSubmitting(false); refresh(); }
    }
  }

  return { current, result, busy, submitting, requestError, today, generate, refresh };
}
