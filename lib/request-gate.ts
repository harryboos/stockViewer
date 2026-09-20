// Cancellation alone is insufficient: an already-resolved response can still arrive late.
export function createRequestGate() {
  let current: AbortController | null = null;
  return {
    begin() {
      current?.abort();
      const controller = new AbortController();
      current = controller;
      return { signal: controller.signal, isCurrent: () => current === controller && !controller.signal.aborted };
    },
    cancel() { current?.abort(); current = null; },
  };
}
