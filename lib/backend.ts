import { NextRequest, NextResponse } from 'next/server';

const backendBaseUrl = process.env.STOCK_BACKEND_URL?.trim() || 'http://127.0.0.1:8000';

export async function forwardToBackend(
  request: NextRequest,
  path: string,
  options: { useServerDailySecret?: boolean } = {},
) {
  const target = new URL(path, backendBaseUrl);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.append(key, value));
  const headers = new Headers();
  const contentType = request.headers.get('content-type');
  if (contentType) headers.set('content-type', contentType);
  const suppliedSecret = request.headers.get('x-daily-run-secret');
  const serverSecret = options.useServerDailySecret ? process.env.DAILY_RUN_SECRET?.trim() : null;
  if (serverSecret || suppliedSecret) headers.set('x-daily-run-secret', serverSecret || suppliedSecret || '');

  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : await request.text(),
      cache: 'no-store',
      signal: AbortSignal.timeout(120_000),
    });
    const raw = await response.text();
    let payload: unknown;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch {
      payload = { error: raw || '本地数据服务返回了无法解析的内容' };
    }
    if (payload && typeof payload === 'object' && 'detail' in payload && !('error' in payload)) {
      payload = { ...payload, error: String((payload as { detail: unknown }).detail) };
    }
    return NextResponse.json(payload, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: `本地数据服务未启动：${error instanceof Error ? error.message : '连接失败'}` },
      { status: 503 },
    );
  }
}

export function createBackendHandler(
  path: string,
  options: { useServerDailySecret?: boolean } = {},
) {
  return (request: NextRequest) => forwardToBackend(request, path, options);
}
