'use client';

import { useRef, useState, type ReactNode } from 'react';
import { errorMessage, jsonFetch } from '@/lib/client-api';
import { useResearch } from '@/lib/use-research';
import type { ResearchJob } from '@/lib/research-types';
import { submissionMessage, type SubmissionFailure } from '@/lib/task-submission';

const jobPolling = { active: 4000, idle: 30000, isActive: (job: ResearchJob) => job.status === 'running' };

type JobActionProps = {
  jobKey: string;
  label: string;
  children?: ReactNode;
  compactError?: string;
};

export function ResearchError({ error, retry }: { error: string | null; retry: () => void }) {
  return error ? <p className="concept-ai-error" role="alert">{error} <button onClick={retry}>重试读取</button></p> : null;
}

export function JobButton({ jobKey, ...props }: JobActionProps) {
  const { data: job, refresh, error: readError } = useResearch<ResearchJob>(`/api/research/jobs?key=${jobKey}`, jobPolling);
  return <JobControl {...props} jobKey={jobKey} job={job} refresh={refresh} readError={readError} />;
}

// The task center already polls all jobs; reuse that snapshot instead of adding
// one independent status request for every button on the page.
export function JobControl({ jobKey, label, job, refresh, readError = null, children, compactError }: JobActionProps & {
  job: ResearchJob | null;
  refresh: () => void;
  readError?: string | null;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [failure, setFailure] = useState<SubmissionFailure | null>(null);
  const submittingRef = useRef(false);
  const error = submissionMessage(failure, job?.startedAt);
  const busy = submitting || job?.status === 'running';
  const statusError = error || readError || job?.error;
  const errorTitle = error ? '任务启动未确认' : readError ? '任务状态暂不可用' : busy ? '上次任务未完成' : compactError;
  async function start() {
    if (busy || submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true); setFailure(null);
    try { await jsonFetch(`/api/research/jobs?key=${encodeURIComponent(jobKey)}`, { method: 'POST' }); }
    catch (cause) { setFailure({ message: errorMessage(cause, '任务启动失败'), previousStartedAt: job ? job.startedAt ?? null : undefined }); }
    finally { submittingRef.current = false; setSubmitting(false); refresh(); }
  }
  return <div className="research-job-control">
    <div className="research-job-actions">{children}<button className="refresh-button" disabled={busy} onClick={start}>{busy ? '后台更新中…' : label}</button></div>
    {busy && <small role="status">{job?.progress.stage || '正在准备'}{job?.progress.total ? ` · ${job.progress.done ?? 0}/${job.progress.total}` : ''}</small>}
    {statusError && (compactError ? <details className="research-job-notice">
      <summary>
        <svg className="research-job-notice-icon" aria-hidden="true" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="10" cy="10" r="7.25" /><path d="M10 5.8v4.7M10 13v1.2" /></svg>
        <span role="status">{errorTitle}</span>
        <span className="research-job-notice-toggle">详情<svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="m4 6 4 4 4-4" /></svg></span>
      </summary>
      <p>{statusError}</p>
    </details> : <small className="research-error">{statusError}</small>)}
  </div>;
}
