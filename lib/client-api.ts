export async function jsonFetch<T>(url: string, options: RequestInit & { timeoutMs?: number } = {}): Promise<T> {
  const { timeoutMs = 190_000, signal, ...request } = options;
  const controller = new AbortController();
  const abort = () => controller.abort(signal?.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener('abort', abort, { once: true });
  const timer = setTimeout(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs);
  try {
    // The deadline covers the response body as well as the initial connection.
    const response = await fetch(url, { ...request, signal: controller.signal });
    const raw = await response.text();
    let body: (T & { error?: string }) | null = null;
    try {
      body = raw ? JSON.parse(raw) as T & { error?: string } : null;
    } catch {
      if (!response.ok) throw new Error(`请求失败（${response.status}）`);
      throw new Error('服务返回了无法解析的数据');
    }
    if (!response.ok) throw new Error(body?.error || `请求失败（${response.status}）`);
    if (body === null) throw new Error('服务没有返回数据');
    if (typeof body !== 'object') throw new Error('服务返回了无效的数据格式');
    return body;
  } catch (cause) {
    if (controller.signal.aborted && controller.signal.reason?.name === 'TimeoutError') {
      throw new Error('请求等待超时，请稍后检查结果');
    }
    throw cause;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', abort);
  }
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}
