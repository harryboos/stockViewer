type Visibility = Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;

type PollingOptions<T> = {
  read: (signal: AbortSignal) => Promise<T>;
  onValue: (value: T) => void;
  onError: (error: unknown) => void;
  interval: number | ((latest: T | null) => number);
  retryInterval?: number;
  refreshOnVisible?: boolean;
  visibility?: Visibility;
};

// Poll only while visible, with at most one read in flight. Callers own mutations;
// resuming a tab only re-reads status and never starts another background task.
export function startPolling<T>({ read, onValue, onError, interval, retryInterval,
  refreshOnVisible = true, visibility = document }: PollingOptions<T>) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let latest: T | null = null;
  let reading = false;
  let started = false;

  function schedule(delay: number) {
    clearTimeout(timer);
    if (!controller.signal.aborted && !visibility.hidden && Number.isFinite(delay) && delay > 0) {
      timer = setTimeout(poll, delay);
    }
  }

  async function poll() {
    if (reading || controller.signal.aborted || visibility.hidden) return;
    clearTimeout(timer);
    reading = true;
    started = true;
    let failed = false;
    try {
      const value = await read(controller.signal);
      if (!controller.signal.aborted) { latest = value; onValue(value); }
    } catch (error) {
      failed = true;
      if (!controller.signal.aborted) onError(error);
    } finally {
      reading = false;
      schedule(failed && retryInterval !== undefined ? retryInterval
        : typeof interval === 'number' ? interval : interval(latest));
    }
  }

  const visible = () => {
    if (visibility.hidden) clearTimeout(timer);
    else if (!started || refreshOnVisible) void poll();
  };
  visibility.addEventListener('visibilitychange', visible);
  void poll();
  return () => {
    controller.abort();
    clearTimeout(timer);
    visibility.removeEventListener('visibilitychange', visible);
  };
}
