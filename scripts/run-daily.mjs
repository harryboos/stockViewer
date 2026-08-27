const backendUrl = process.env.STOCK_BACKEND_URL || 'http://127.0.0.1:8000';
const runSecret = process.env.DAILY_RUN_SECRET || '';

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
  const system = await readJson(await fetch(`${backendUrl}/api/system`));
  if (!system.providers.marketData) throw new Error('免费行情服务未就绪，请先运行 npm run setup');

  const headers = runSecret ? { 'x-daily-run-secret': runSecret } : {};
  const result = await readJson(await fetch(`${backendUrl}/api/daily`, { method: 'POST', headers }));
  const aiSummary = result.ai.runs.map((run) => `${run.provider}:${run.status}`).join(', ');
  process.stdout.write(`公开策略 ${result.public.strategies.length} 组；AI ${aiSummary}\n`);
} catch (error) {
  process.stderr.write(`每日策略运行失败：${error instanceof Error ? error.message : String(error)}\n`);
  process.exitCode = 1;
}
