const backendUrl = (process.env.STOCK_BACKEND_URL?.trim() || `http://127.0.0.1:${process.env.STOCK_BACKEND_PORT || '8000'}`).replace(/\/$/, '');
const runSecret = process.env.DAILY_RUN_SECRET?.trim() || '';

async function readJson(response) {
  const raw = await response.text();
  let body;
  try {
    body = raw ? JSON.parse(raw) : {};
  } catch {
    throw new Error(`数据服务返回了无法解析的内容（HTTP ${response.status}）`);
  }
  if (!response.ok) throw new Error(body.error || body.detail || `HTTP ${response.status}`);
  return body;
}

try {
  // Reachable service can obtain its first quotes during calculation; no cached quote is required.
  await readJson(await fetch(`${backendUrl}/api/system`, { signal: AbortSignal.timeout(10_000) }));

  const headers = runSecret ? { 'x-daily-run-secret': runSecret } : {};
  const result = await readJson(await fetch(`${backendUrl}/api/daily`, { method: 'POST', headers, signal: AbortSignal.timeout(600_000) }));
  const aiSummary = result.ai.runs.map((run) => `${run.provider}:${run.status}`).join(', ');
  process.stdout.write(`公开策略 ${result.public.strategies.length} 组；AI ${aiSummary}\n`);
  const failed = result.ai.runs.filter((run) => run.status === 'failed');
  if (failed.length) {
    throw new Error(`AI 任务失败：${failed.map((run) => run.provider).join('、')}。已保留成功结果，可在页面重试失败任务`);
  }
} catch (error) {
  process.stderr.write(`每日策略运行失败：${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
