'use client';

import { useState } from 'react';
import { errorMessage, jsonFetch } from '@/lib/client-api';
import { useResearch } from '@/lib/use-research';
import type { ResearchJob } from '@/lib/research-types';
import { submissionMessage, type SubmissionFailure } from '@/lib/task-submission';

const jobPolling = { active: 4000, idle: 30000, isActive: (job: ResearchJob) => job.status === 'running' };

export function ResearchError({ error, retry }: { error: string | null; retry: () => void }) {
  return error ? <p className="concept-ai-error" role="alert">{error} <button onClick={retry}>重试读取</button></p> : null;
}

export function JobButton({ jobKey, label }: { jobKey: string; label: string }) {
  const { data: job, refresh, error: readError } = useResearch<ResearchJob>(`/api/research/jobs?key=${jobKey}`, jobPolling);
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<SubmissionFailure | null>(null);
  const error = submissionMessage(failure, job?.startedAt);
  const busy = submitting || job?.status === 'running';
  async function start() {
    if (busy) return;
    setSubmitting(true); setFailure(null);
    try { await jsonFetch(`/api/research/jobs?key=${jobKey}`, { method: 'POST' }); }
    catch (cause) { setFailure({ message: errorMessage(cause, '任务启动失败'), previousStartedAt: job ? job.startedAt ?? null : undefined }); }
    finally { setSubmitting(false); refresh(); }
  }
  return <div className="research-job-control"><button className="refresh-button" disabled={busy} onClick={start}>{busy ? '后台更新中…' : label}</button>
    {busy && <small role="status">{job?.progress.stage || '正在准备'}{job?.progress.total ? ` · ${job.progress.done ?? 0}/${job.progress.total}` : ''}</small>}
    {(error || readError || job?.error) && <small className="research-error">{error || readError || job?.error}</small>}
  </div>;
}
