import { spawn } from 'node:child_process';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const child = spawn(resolve(root, '.venv/bin/python'), ['-m', 'backend.probe_sources', ...process.argv.slice(2)], {
  cwd: root, stdio: 'inherit', env: process.env,
});
child.on('error', () => { process.stderr.write('无法启动行情检查，请先运行 npm run setup\n'); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
