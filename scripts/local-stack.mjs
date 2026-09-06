import { existsSync } from 'node:fs';
import { resolve, join } from 'node:path';
import { spawn } from 'node:child_process';

const root = resolve(import.meta.dirname, '..');
const mode = process.argv[2] === 'start' ? 'start' : 'dev';
const python = join(root, '.venv', 'bin', 'python');
const vinext = join(root, 'node_modules', '.bin', 'vinext');

if (!existsSync(python)) {
  process.stderr.write('尚未安装本地数据服务，请先运行 npm run setup\n');
  process.exit(1);
}

const backendPort = process.env.STOCK_BACKEND_PORT || '8000';
const stackEnv = { ...process.env, STOCK_BACKEND_URL: process.env.STOCK_BACKEND_URL?.trim() || `http://127.0.0.1:${backendPort}` };
const backend = spawn(
  python,
  ['-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', backendPort],
  { cwd: root, stdio: 'inherit', env: { ...process.env, PYTHONUNBUFFERED: '1' } },
);
const frontend = spawn(vinext, [mode], { cwd: root, stdio: 'inherit', env: stackEnv });
const children = [backend, frontend];
let stopping = false;

function stop(signal = 'SIGTERM') {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (!child.killed) child.kill(signal);
  }
}

for (const child of children) {
  child.on('error', (error) => {
    process.stderr.write(`启动失败：${error.message}\n`);
    process.exitCode = 1;
    stop();
  });
  child.on('exit', (code, signal) => {
    if (!stopping) {
      stop();
      process.exitCode = code ?? (signal ? 1 : 0);
    }
  });
}
process.on('SIGINT', () => stop('SIGTERM'));
process.on('SIGTERM', () => stop('SIGTERM'));
