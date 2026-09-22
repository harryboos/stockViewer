import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

const scriptUrl = new URL('../scripts/run-daily.mjs', import.meta.url).href;

function runDaily(runs) {
  // Exercise the command's actual exit status without a server or paid API call.
  const result = { public: { strategies: [{ id: 'momentum' }] }, ai: { runs } };
  const child = spawnSync(process.execPath, ['--input-type=module', '--eval', `
    let calls = 0;
    globalThis.fetch = async (url, options) => {
      calls += 1;
      if (calls === 1 && new URL(url).pathname === '/api/system') return new Response('{"ok":true}');
      if (calls === 2 && new URL(url).pathname === '/api/daily' && options.method === 'POST') {
        return new Response(JSON.stringify(${JSON.stringify(result)}));
      }
      throw new Error('Unexpected additional request');
    };
    await import(${JSON.stringify(scriptUrl)});
    process.stdout.write('request-count=' + calls + '\\n');
  `], { encoding: 'utf8', timeout: 10_000 });
  assert.equal(child.error, undefined);
  assert.equal(child.signal, null);
  assert.match(child.stdout, /request-count=2/);
  return child;
}

test('daily command returns failure when HTTP success contains a failed AI provider', () => {
  const child = runDaily([
    { provider: 'deepseek', status: 'succeeded' },
    { provider: 'glm', status: 'failed' },
    { provider: 'qwen', status: 'succeeded' },
  ]);
  assert.equal(child.status, 1);
  assert.match(child.stdout, /公开策略 1 组；AI deepseek:succeeded, glm:failed, qwen:succeeded/);
  assert.match(child.stderr, /AI 任务失败：glm/);
  assert.match(child.stderr, /已保留成功结果/);
});

test('daily command succeeds when configured providers succeed and others are unconfigured', () => {
  for (const runs of [
    [{ provider: 'glm', status: 'succeeded' }, { provider: 'qwen', status: 'not_configured' }],
    [{ provider: 'glm', status: 'not_configured' }],
    [{ provider: 'glm', status: 'succeeded' }, { provider: 'qwen', status: 'succeeded' }],
  ]) {
    const child = runDaily(runs);
    assert.equal(child.status, 0);
    assert.equal(child.stderr, '');
  }
});
