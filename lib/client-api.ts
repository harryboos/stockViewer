export async function jsonFetch<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
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
  return body;
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
