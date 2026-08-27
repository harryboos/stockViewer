const byId = id => document.getElementById(id);
const loadingView = byId('loadingView');
const lockedView = byId('lockedView');
const dashboardView = byId('dashboardView');
const unlockForm = byId('unlockForm');
const accessKeyInput = byId('accessKey');
const unlockButton = byId('unlockButton');
const formMessage = byId('formMessage');
const togglePassword = byId('togglePassword');
const logoutButton = byId('logoutButton');
const returnTo = new URLSearchParams(window.location.search).get('returnTo') || '';

function show(view) {
  [loadingView, lockedView, dashboardView].forEach(item => item.classList.toggle('hidden', item !== view));
}

async function api(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: {'content-type': 'application/json', ...(options?.headers || {})}
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || '请求失败，请稍后再试');
  return body;
}

async function load() {
  try {
    const [config, status] = await Promise.all([api('/api/config'), api('/api/auth/status')]);
    byId('stockLink').href = config.stockUrl;
    byId('footballLink').href = config.footballUrl;
    if (!status.configured) {
      show(lockedView);
      formMessage.textContent = '服务器尚未完成密钥配置，请联系管理员。';
      accessKeyInput.disabled = true;
      unlockButton.disabled = true;
      return;
    }
    if (status.unlocked && returnTo) {
      window.location.replace(returnTo);
      return;
    }
    show(status.unlocked ? dashboardView : lockedView);
    if (!status.unlocked) accessKeyInput.focus();
  } catch (error) {
    show(lockedView);
    formMessage.textContent = error.message;
  }
}

unlockForm.addEventListener('submit', async event => {
  event.preventDefault();
  unlockButton.disabled = true;
  formMessage.textContent = '';
  try {
    const result = await api('/api/auth/unlock', {
      method: 'POST',
      body: JSON.stringify({accessKey: accessKeyInput.value, returnTo})
    });
    accessKeyInput.value = '';
    if (returnTo && result.returnTo) {
      window.location.replace(result.returnTo);
      return;
    }
    show(dashboardView);
  } catch (error) {
    formMessage.textContent = error.message;
    accessKeyInput.focus();
  } finally {
    unlockButton.disabled = false;
  }
});

togglePassword.addEventListener('click', () => {
  const visible = accessKeyInput.type === 'text';
  accessKeyInput.type = visible ? 'password' : 'text';
  togglePassword.textContent = visible ? '显示' : '隐藏';
  togglePassword.setAttribute('aria-label', visible ? '显示访问密钥' : '隐藏访问密钥');
  accessKeyInput.focus();
});

logoutButton.addEventListener('click', async () => {
  logoutButton.disabled = true;
  try {
    await api('/api/auth/logout', {method: 'POST', body: '{}'});
    window.history.replaceState({}, '', '/');
    show(lockedView);
    accessKeyInput.focus();
  } catch (error) {
    window.alert(error.message);
  } finally {
    logoutButton.disabled = false;
  }
});

load();
