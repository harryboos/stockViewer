import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const currentDirectory = path.dirname(fileURLToPath(import.meta.url));
const publicDirectory = path.join(currentDirectory, 'public');
const SESSION_COOKIE = 'private_portal_access';
const FOOTBALL_GATE_COOKIE = 'lfm_site_access';
const SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60;
const FAILURE_WINDOW_MS = 10 * 60 * 1000;
const MAX_FAILURES = 5;
const MAX_BODY_BYTES = 4_096;

const CONTENT_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.ico': 'image/x-icon',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml'
};

function cleanDomain(value) {
  return String(value || '').trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/$/, '');
}

function cleanCookieDomain(value) {
  return cleanDomain(value).replace(/^\./, '');
}

function hostnameOf(value) {
  try {
    return new URL(`http://${value}`).hostname.toLowerCase();
  } catch {
    return '';
  }
}

function validDomainValue(value) {
  return Boolean(value) && !/[\/?#@\s]/.test(value) && Boolean(hostnameOf(value));
}

function booleanValue(value, fallback) {
  if (value === undefined || value === null || value === '') return fallback;
  return !['0', 'false', 'no', 'off'].includes(String(value).trim().toLowerCase());
}

function digest(value) {
  return crypto.createHash('sha256').update(String(value || '')).digest();
}

function safeEqual(left, right) {
  const a = Buffer.isBuffer(left) ? left : Buffer.from(String(left || ''));
  const b = Buffer.isBuffer(right) ? right : Buffer.from(String(right || ''));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function hmac(secret, message) {
  return crypto.createHmac('sha256', secret).update(message).digest('base64url');
}

export function createConfig(overrides = {}) {
  const input = {...process.env, ...overrides};
  const portalDomain = cleanDomain(input.PORTAL_DOMAIN || 'home.example.com');
  const stockDomain = cleanDomain(input.STOCK_DOMAIN || 'stocks.example.com');
  const footballDomain = cleanDomain(input.FOOTBALL_DOMAIN || 'football.example.com');
  const cookieDomain = cleanCookieDomain(input.COOKIE_DOMAIN || 'example.com');
  const secureCookies = booleanValue(input.COOKIE_SECURE, true);
  const protocol = secureCookies ? 'https' : 'http';

  return {
    port: Number(input.PORT || 3000),
    accessKey: String(input.SITE_ACCESS_KEY || ''),
    sessionSecret: String(input.PORTAL_SESSION_SECRET || ''),
    portalDomain,
    stockDomain,
    footballDomain,
    cookieDomain,
    secureCookies,
    portalUrl: `${protocol}://${portalDomain}`,
    stockUrl: `${protocol}://${stockDomain}`,
    footballUrl: `${protocol}://${footballDomain}`
  };
}

function configured(config) {
  const cookieHost = hostnameOf(config.cookieDomain);
  const appDomains = [config.portalDomain, config.stockDomain, config.footballDomain];
  const domainsShareCookieScope = cookieHost && appDomains.every(domain => {
    const host = hostnameOf(domain);
    return host === cookieHost || host.endsWith(`.${cookieHost}`);
  });
  return config.accessKey.length >= 12
    && config.sessionSecret.length >= 32
    && [...appDomains, config.cookieDomain].every(validDomainValue)
    && Boolean(domainsShareCookieScope);
}

function cookieValue(request, name) {
  const source = String(request.headers.cookie || '');
  const item = source.split(';').map(part => part.trim()).find(part => part.startsWith(`${name}=`));
  if (!item) return '';
  try {
    return decodeURIComponent(item.slice(name.length + 1));
  } catch {
    return '';
  }
}

function sessionToken(config) {
  return configured(config)
    ? hmac(config.sessionSecret, `private-portal-v1:${digest(config.accessKey).toString('hex')}`)
    : '';
}

function footballGateToken(config) {
  return configured(config)
    ? hmac(config.accessKey, 'legend-football-manager-site-gate-v1')
    : '';
}

function hasValidSession(request, config) {
  return configured(config) && safeEqual(cookieValue(request, SESSION_COOKIE), sessionToken(config));
}

function sessionCookie(config, name, value, maximumAgeSeconds) {
  const parts = [
    `${name}=${encodeURIComponent(value)}`,
    `Max-Age=${maximumAgeSeconds}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Lax'
  ];
  if (config.cookieDomain) parts.push(`Domain=${config.cookieDomain}`);
  if (config.secureCookies) parts.push('Secure');
  return parts.join('; ');
}

function accessCookies(config) {
  return [
    sessionCookie(config, SESSION_COOKIE, sessionToken(config), SESSION_MAX_AGE_SECONDS),
    sessionCookie(config, FOOTBALL_GATE_COOKIE, footballGateToken(config), SESSION_MAX_AGE_SECONDS)
  ];
}

function clearedCookies(config) {
  return [
    sessionCookie(config, SESSION_COOKIE, '', 0),
    sessionCookie(config, FOOTBALL_GATE_COOKIE, '', 0)
  ];
}

function responseHeaders(extra = {}) {
  return {
    'cache-control': 'no-store',
    'content-security-policy': "default-src 'self'; base-uri 'none'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'",
    'cross-origin-opener-policy': 'same-origin',
    'referrer-policy': 'no-referrer',
    'x-content-type-options': 'nosniff',
    'x-frame-options': 'DENY',
    ...extra
  };
}

function sendJson(response, status, payload, headers = {}) {
  response.writeHead(status, responseHeaders({'content-type': CONTENT_TYPES['.json'], ...headers}));
  response.end(JSON.stringify(payload));
}

function sendRedirect(response, location) {
  response.writeHead(302, responseHeaders({location}));
  response.end();
}

async function readJson(request) {
  let source = '';
  for await (const chunk of request) {
    source += chunk;
    if (Buffer.byteLength(source) > MAX_BODY_BYTES) throw Object.assign(new Error('请求内容过大'), {status: 413});
  }
  if (!source) return {};
  try {
    return JSON.parse(source);
  } catch {
    throw Object.assign(new Error('请求格式无效'), {status: 400});
  }
}

function clientIdentifier(request) {
  const forwarded = String(request.headers['x-forwarded-for'] || '').split(',').map(value => value.trim()).filter(Boolean);
  return forwarded.at(-1) || request.socket.remoteAddress || 'unknown';
}

function requestOriginAllowed(request, config) {
  const origin = String(request.headers.origin || '');
  return !origin || origin === config.portalUrl;
}

function allowedReturnTo(value, config) {
  if (!value) return '';
  try {
    const candidate = new URL(String(value));
    const allowedOrigins = new Set([config.portalUrl, config.stockUrl, config.footballUrl]);
    return allowedOrigins.has(candidate.origin) && !candidate.username && !candidate.password ? candidate.toString() : '';
  } catch {
    return '';
  }
}

function forwardedReturnTo(request, config) {
  const host = cleanDomain(request.headers['x-forwarded-host']);
  const forwardedUri = String(request.headers['x-forwarded-uri'] || '/');
  const protocol = String(request.headers['x-forwarded-proto'] || (config.secureCookies ? 'https' : 'http'));
  if (!host || !forwardedUri.startsWith('/') || !['http', 'https'].includes(protocol)) return '';
  return allowedReturnTo(`${protocol}://${host}${forwardedUri}`, config);
}

function loginLocation(returnTo, config) {
  const target = new URL('/', config.portalUrl);
  if (returnTo) target.searchParams.set('returnTo', returnTo);
  return target.toString();
}

function serveStatic(request, response, pathname) {
  if (!['GET', 'HEAD'].includes(request.method)) {
    sendJson(response, 405, {error: '此接口不支持当前请求方式'});
    return;
  }
  const requested = pathname === '/' ? 'index.html' : pathname.slice(1);
  let decoded;
  try {
    decoded = decodeURIComponent(requested);
  } catch {
    sendJson(response, 400, {error: '路径格式无效'});
    return;
  }
  const file = path.resolve(publicDirectory, decoded);
  if (!file.startsWith(`${path.resolve(publicDirectory)}${path.sep}`) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    sendJson(response, 404, {error: '页面不存在'});
    return;
  }
  const extension = path.extname(file);
  const cacheControl = extension === '.html' ? 'no-store' : 'public, max-age=3600';
  response.writeHead(200, responseHeaders({
    'cache-control': cacheControl,
    'content-type': CONTENT_TYPES[extension] || 'application/octet-stream'
  }));
  if (request.method === 'HEAD') return response.end();
  fs.createReadStream(file).pipe(response);
}

export function createPortalServer(overrides = {}) {
  const config = createConfig(overrides);
  const expectedKeyHash = configured(config) ? digest(config.accessKey) : null;
  const failures = new Map();

  function retryAfterSeconds(identifier) {
    const record = failures.get(identifier);
    if (!record) return 0;
    if (record.blockedUntil > Date.now()) return Math.ceil((record.blockedUntil - Date.now()) / 1_000);
    if (Date.now() - record.windowStartedAt >= FAILURE_WINDOW_MS) failures.delete(identifier);
    return 0;
  }

  function recordFailure(identifier) {
    const now = Date.now();
    const previous = failures.get(identifier);
    const record = previous && now - previous.windowStartedAt < FAILURE_WINDOW_MS
      ? previous
      : {count: 0, windowStartedAt: now, blockedUntil: 0};
    record.count += 1;
    if (record.count >= MAX_FAILURES) record.blockedUntil = now + FAILURE_WINDOW_MS;
    failures.set(identifier, record);
  }

  return http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url || '/', 'http://localhost');

      if (url.pathname === '/health') {
        sendJson(response, 200, {status: 'ok', configured: configured(config)});
        return;
      }

      if (url.pathname === '/api/config') {
        if (request.method !== 'GET') throw Object.assign(new Error('此接口仅支持 GET'), {status: 405});
        sendJson(response, 200, {
          portalUrl: config.portalUrl,
          stockUrl: config.stockUrl,
          footballUrl: config.footballUrl
        });
        return;
      }

      if (url.pathname === '/api/auth/status') {
        if (request.method !== 'GET') throw Object.assign(new Error('此接口仅支持 GET'), {status: 405});
        sendJson(response, 200, {configured: configured(config), unlocked: hasValidSession(request, config)});
        return;
      }

      if (url.pathname === '/api/auth/check') {
        if (request.method !== 'GET') throw Object.assign(new Error('此接口仅支持 GET'), {status: 405});
        if (hasValidSession(request, config)) {
          response.writeHead(204, responseHeaders());
          response.end();
          return;
        }
        sendRedirect(response, loginLocation(forwardedReturnTo(request, config), config));
        return;
      }

      if (url.pathname === '/api/auth/unlock') {
        if (request.method !== 'POST') throw Object.assign(new Error('此接口仅支持 POST'), {status: 405});
        if (!requestOriginAllowed(request, config)) throw Object.assign(new Error('请求来源无效'), {status: 403});
        if (!configured(config)) throw Object.assign(new Error('服务器尚未完成访问密钥配置'), {status: 503});
        const identifier = clientIdentifier(request);
        const retryBeforeAttempt = retryAfterSeconds(identifier);
        if (retryBeforeAttempt) {
          sendJson(response, 429, {error: `尝试次数过多，请在 ${retryBeforeAttempt} 秒后重试`}, {'retry-after': String(retryBeforeAttempt)});
          return;
        }
        const body = await readJson(request);
        if (!safeEqual(expectedKeyHash, digest(body.accessKey))) {
          recordFailure(identifier);
          const retryAfterFailure = retryAfterSeconds(identifier);
          if (retryAfterFailure) {
            sendJson(response, 429, {error: `尝试次数过多，请在 ${retryAfterFailure} 秒后重试`}, {'retry-after': String(retryAfterFailure)});
          } else {
            sendJson(response, 401, {error: '访问密钥不正确'});
          }
          return;
        }
        failures.delete(identifier);
        sendJson(response, 200, {
          unlocked: true,
          returnTo: allowedReturnTo(body.returnTo, config) || config.portalUrl
        }, {'set-cookie': accessCookies(config)});
        return;
      }

      if (url.pathname === '/api/auth/logout') {
        if (request.method !== 'POST') throw Object.assign(new Error('此接口仅支持 POST'), {status: 405});
        if (!requestOriginAllowed(request, config)) throw Object.assign(new Error('请求来源无效'), {status: 403});
        sendJson(response, 200, {loggedOut: true}, {'set-cookie': clearedCookies(config)});
        return;
      }

      serveStatic(request, response, url.pathname);
    } catch (error) {
      sendJson(response, Number(error.status) || 500, {error: error.message || '服务暂时不可用'});
    }
  });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const config = createConfig();
  const server = createPortalServer();
  server.listen(config.port, '0.0.0.0', () => {
    process.stdout.write(`私人应用入口已启动：http://0.0.0.0:${config.port}\n`);
    if (!configured(config)) process.stderr.write('入口尚未配置：SITE_ACCESS_KEY 至少 12 位，PORTAL_SESSION_SECRET 至少 32 位。\n');
  });
}
