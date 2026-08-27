import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = resolve(import.meta.dirname, '..');
const venvPython = join(root, '.venv', 'bin', 'python');

function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: 'inherit' });
  if (result.status !== 0) process.exit(result.status ?? 1);
}

function findPython() {
  const candidates = [
    process.env.STOCK_PYTHON,
    join(homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3'),
    'python3.13',
    'python3.12',
    'python3.11',
    'python3',
  ].filter(Boolean);
  for (const candidate of candidates) {
    const result = spawnSync(candidate, ['--version'], { encoding: 'utf8' });
    if (result.status === 0) return candidate;
  }
  throw new Error('没有找到 Python 3.11 或更高版本');
}

if (!existsSync(venvPython)) {
  const python = findPython();
  process.stdout.write('正在创建本地 Python 环境…\n');
  run(python, ['-m', 'venv', '.venv']);
}

process.stdout.write('正在安装免费行情与本地服务依赖…\n');
run(venvPython, ['-m', 'pip', 'install', '--upgrade', 'pip']);
run(venvPython, ['-m', 'pip', 'install', '-r', 'backend/requirements.txt']);
process.stdout.write('本地数据服务已准备好。\n');
