import { NextRequest, NextResponse } from 'next/server';

const backendBaseUrl = process.env.STOCK_BACKEND_URL?.trim() || 'http://127.0.0.1:8000';
const responseHeaders = { 'Cache-Control': 'no-store' };

export async function forwardToBackend(
  request: NextRequest,
  path: string,
  options: { useServerDailySecret?: boolean } = {},
) {
  if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method)) {
    const origin = request.headers.get('origin');
    const fetchSite = request.headers.get('sec-fetch-site');
    // Browsers set this forbidden header from the public origin, including
    // deployments where a trusted gateway terminates HTTPS before Vinext.
    if (fetchSite === 'cross-site'
      || (origin && origin !== request.nextUrl.origin && fetchSite !== 'same-origin')) {
      return NextResponse.json({ error: '不允许跨站修改数据或运行策略' }, { status: 403, headers: responseHeaders });
    }
  }
  const target = new URL(path, backendBaseUrl);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));
  const headers = new Headers();
  const contentType = request.headers.get('content-type');
  if (contentType) headers.set('content-type', contentType);
  const suppliedSecret = request.headers.get('x-daily-run-secret');
  const serverSecret = options.useServerDailySecret ? process.env.DAILY_RUN_SECRET?.trim() : null;
  if (serverSecret || suppliedSecret) headers.set('x-daily-run-secret', serverSecret || suppliedSecret || '');

  // Disconnects must also cancel the upstream connection and response body,
  // otherwise closing a tab leaves expensive reads alive until the deadline.
  const signal = AbortSignal.any([request.signal, AbortSignal.timeout(180_000)]);
  try {
    signal.throwIfAborted();
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : await request.text(),
      cache: 'no-store',
      signal,
    });
    const raw = await response.text();
    let payload: unknown;
    try {
      payload = raw ? JSON.parse(raw) : null;
    } catch {
      return NextResponse.json(
        { error: '本地数据服务返回了无法解析的内容' },
        { status: response.ok ? 502 : response.status, headers: responseHeaders },
      );
    }
    if (response.ok && (!payload || typeof payload !== 'object')) {
      return NextResponse.json({ error: '本地数据服务返回了无效的数据格式' }, { status: 502, headers: responseHeaders });
    }
    if (payload && typeof payload === 'object' && 'detail' in payload && !('error' in payload)) {
      payload = { ...payload, error: String((payload as { detail: unknown }).detail) };
    }
    return NextResponse.json(payload, { status: response.status, headers: responseHeaders });
  } catch (error) {
    if (request.signal.aborted) {
      return NextResponse.json({ error: '请求已取消' }, { status: 499, headers: responseHeaders });
    }
    const timedOut = signal.reason?.name === 'TimeoutError' || error instanceof Error && error.name === 'TimeoutError';
    return NextResponse.json(
      { error: timedOut ? '数据服务响应超时，请稍后检查结果' : '无法连接本地数据服务，请检查服务是否启动' },
      { status: timedOut ? 504 : 503, headers: responseHeaders },
    );
  }
}

export function createBackendHandler(
  path: string,
  options: { useServerDailySecret?: boolean } = {},
) {
  return (request: NextRequest) => forwardToBackend(request, path, options);
}
