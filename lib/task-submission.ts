export type SubmissionFailure = { message: string; previousStartedAt?: string | null };

// Reading an old successful task does not confirm a lost submission response.
// Undefined means the prior task was unknown; null means no task had started.
export function submissionMessage(failure: SubmissionFailure | null, startedAt?: string | null) {
  if (!failure) return null;
  const current = Date.parse(startedAt ?? '');
  const previous = Date.parse(failure.previousStartedAt ?? '');
  const confirmed = Number.isFinite(current) && (failure.previousStartedAt === null
    || (Number.isFinite(previous) && current > previous));
  return confirmed ? null : failure.message;
}
