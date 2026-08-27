import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createPortalServer } from './server.mjs';

const settings = {
  PORTAL_DOMAIN: 'home.test',
  STOCK_DOMAIN: 'stocks.test',
  FOOTBALL_DOMAIN: 'football.test',
  COOKIE_DOMAIN: 'test',
  COOKIE_SECURE: 'false',
  SITE_ACCESS_KEY: 'correct-horse-battery-staple',
  PORTAL_SESSION_SECRET: '0123456789abcdef0123456789abcdef'
};

let server;
let origin;

before(async () => {
  server = createPortalServer(settings);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  server.closeAllConnections();
  await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
});

async function unlock(accessKey, returnTo = '') {
  return fetch(`${origin}/api/auth/unlock`, {
    method: 'POST',
    headers: {'content-type': 'application/json', origin: 'http://home.test'},
    body: JSON.stringify({accessKey, returnTo})
  });
}

test('错误密钥不会创建访问会话', async () => {
  const response = await unlock('incorrect-key');
  assert.equal(response.status, 401);
  assert.equal(response.headers.get('set-cookie'), null);
});

test('正确密钥创建跨子域会话并兼容足球应用', async () => {
  const response = await unlock(settings.SITE_ACCESS_KEY, 'http://stocks.test/strategies');
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.returnTo, 'http://stocks.test/strategies');
  const cookies = response.headers.getSetCookie();
  assert.equal(cookies.length, 2);
  assert.ok(cookies.some(cookie => cookie.startsWith('private_portal_access=')));
  assert.ok(cookies.some(cookie => cookie.startsWith('lfm_site_access=')));
  assert.ok(cookies.every(cookie => cookie.includes('Domain=test')));
  assert.ok(cookies.every(cookie => cookie.includes('HttpOnly')));
  assert.ok(cookies.every(cookie => cookie.includes('SameSite=Lax')));
});

test('有效入口会话通过反向代理前置校验', async () => {
  const unlockResponse = await unlock(settings.SITE_ACCESS_KEY);
  const portalCookie = unlockResponse.headers.getSetCookie()
    .find(cookie => cookie.startsWith('private_portal_access='))
    .split(';', 1)[0];
  const response = await fetch(`${origin}/api/auth/check`, {headers: {cookie: portalCookie}});
  assert.equal(response.status, 204);
});

test('未登录访问应用会安全跳回入口', async () => {
  const response = await fetch(`${origin}/api/auth/check`, {
    redirect: 'manual',
    headers: {
      'x-forwarded-host': 'football.test',
      'x-forwarded-proto': 'http',
      'x-forwarded-uri': '/?game=ABC123'
    }
  });
  assert.equal(response.status, 302);
  const location = new URL(response.headers.get('location'));
  assert.equal(location.origin, 'http://home.test');
  assert.equal(location.searchParams.get('returnTo'), 'http://football.test/?game=ABC123');
});

test('拒绝把登录成功后的跳转地址指向站外', async () => {
  const response = await unlock(settings.SITE_ACCESS_KEY, 'https://attacker.example/steal');
  assert.equal(response.status, 200);
  assert.equal((await response.json()).returnTo, 'http://home.test');
});

test('子域名不属于 Cookie 根域名时入口保持关闭', async () => {
  const temporaryServer = createPortalServer({...settings, COOKIE_DOMAIN: 'another.test', PORT: 0});
  await new Promise(resolve => temporaryServer.listen(0, '127.0.0.1', resolve));
  const temporaryOrigin = `http://127.0.0.1:${temporaryServer.address().port}`;
  const response = await fetch(`${temporaryOrigin}/api/auth/status`);
  assert.deepEqual(await response.json(), {configured: false, unlocked: false});
  temporaryServer.closeAllConnections();
  await new Promise(resolve => temporaryServer.close(resolve));
});
