import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'vite';

let server;
let startPolling;
before(async () => {
  server = await createServer({ configFile: false, appType: 'custom', logLevel: 'error',
    server: { middlewareMode: true, watch: null, hmr: false, ws: false } });
  ({ startPolling } = await server.ssrLoadModule('/lib/polling.ts'));
});
after(async () => { await server?.close(); });

class Visibility extends EventTarget {
  hidden = false;
  change(hidden) { this.hidden = hidden; this.dispatchEvent(new Event('visibilitychange')); }
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

test('polling pauses while hidden and rereads finished task status on return', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  let reads = 0;
  const values = [];
  const stop = startPolling({ visibility,
    read: async () => ({ status: ++reads === 1 ? 'running' : 'succeeded' }),
    onValue: value => values.push(value.status), onError: assert.fail,
    interval: value => value?.status === 'running' ? 3000 : 0,
  });
  t.after(stop);
  await flush();
  assert.equal(reads, 1);
  visibility.change(true);
  t.mock.timers.tick(60_000);
  await flush();
  assert.equal(reads, 1, 'hidden pages do not send recurring status reads');
  visibility.change(false);
  await flush();
  assert.equal(reads, 2);
  assert.deepEqual(values, ['running', 'succeeded']);
  t.mock.timers.tick(60_000);
  await flush();
  assert.equal(reads, 2, 'completed tasks stop their periodic reads');
  visibility.change(true);
  visibility.change(false);
  await flush();
  assert.equal(reads, 3, 'returning to the page discovers work initiated elsewhere');
});

test('visibility events cannot overlap pending reads or revive disposed requests', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  const values = [];
  let resolveRead;
  let signal;
  let reads = 0;
  const stop = startPolling({ visibility,
    read: received => { signal = received; reads += 1; return new Promise(resolve => { resolveRead = resolve; }); },
    onValue: value => values.push(value), onError: assert.fail, interval: 3000,
  });
  t.after(stop);
  visibility.change(true);
  visibility.change(false);
  t.mock.timers.tick(30_000);
  assert.equal(reads, 1);
  stop();
  assert.equal(signal.aborted, true);
  resolveRead('late response');
  await flush();
  visibility.change(true);
  visibility.change(false);
  t.mock.timers.tick(30_000);
  await flush();
  assert.equal(reads, 1);
  assert.deepEqual(values, []);
});

test('a one-shot read mounted in the background starts once when first visible', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  visibility.hidden = true;
  let reads = 0;
  const stop = startPolling({ visibility, read: async () => ++reads,
    onValue() {}, onError: assert.fail, interval: 0, refreshOnVisible: false });
  t.after(stop);
  await flush();
  assert.equal(reads, 0);
  visibility.change(false);
  await flush();
  assert.equal(reads, 1);
  visibility.change(true);
  visibility.change(false);
  await flush();
  assert.equal(reads, 1);
});

test('failed status reads use the retry delay and recover without starting a task', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const visibility = new Visibility();
  let reads = 0;
  const values = [];
  const errors = [];
  const stop = startPolling({ visibility,
    read: async () => { if (++reads === 1) throw new Error('temporary connection failure'); return 'complete'; },
    onValue: value => values.push(value), onError: error => errors.push(error.message),
    interval: 0, retryInterval: 10_000,
  });
  t.after(stop);
  await flush();
  assert.deepEqual(errors, ['temporary connection failure']);
  t.mock.timers.tick(9999);
  await flush();
  assert.equal(reads, 1);
  t.mock.timers.tick(1);
  await flush();
  assert.equal(reads, 2);
  assert.deepEqual(values, ['complete']);
});
