'use strict';
// ─── State ──────────────────────────────────────────────────────────────────
const API_BASE = 'http://127.0.0.1:8765';
let logFilter = 'all';
let allLogs   = [];
let ws        = null;
let _wsReconnectTimer = null;
let _statusInterval  = null;
let _triggers = [];

// FIX: таймер до следующего поднятия
let _nextRaiseAt = null;
let _raiseCountdownTimer = null;

// ─── Navigation ─────────────────────────────────────────────────────────────
function go(page) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  document.getElementById('nav-' + page)?.classList.add('active');
  if (page === 'settings') loadSettings();
  if (page === 'ar')       loadAR();
  if (page === 'raise')    loadRaise();
}

// ─── Toast ───────────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'show ' + (type === 'ok' ? 'ok' : type === 'err' ? 'err' : type === 'warn' ? 'warn' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.className = '', 3500);
}

// ─── Desktop notification ────────────────────────────────────────────────────
function requestNotifPermission() {
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function showDesktopNotif(title, body) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  try {
    new Notification(title, { body, silent: false });
  } catch (_) {}
}

// ─── API helpers ─────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  try {
    const r = await fetch(API_BASE + path, opts);
    return await r.json();
  } catch (e) {
    return { ok: false, message: 'Нет соединения с бэкендом' };
  }
}

async function apiPost(path, data = {}) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
}

// ─── Bot control ─────────────────────────────────────────────────────────────
async function startBot() {
  const d = await api('/api/start', { method: 'POST' });
  toast(d.message, d.ok ? 'ok' : 'err');
  if (d.ok) updateStatus();
}

async function stopBot() {
  const d = await api('/api/stop', { method: 'POST' });
  toast(d.message, d.ok ? '' : 'err');
  stopRaiseCountdown();
  updateStatus();
}

async function doRefresh() {
  const d = await api('/api/refresh', { method: 'POST' });
  toast(d.message, d.ok ? 'ok' : 'err');
  if (d.ok) updateStatus();
}

async function doConnect() {
  const btn = document.getElementById('btn-connect');
  if (btn) { btn.disabled = true; btn.textContent = 'Подключение...'; }
  const d = await api('/api/connect', { method: 'POST' });
  if (btn) { btn.disabled = false; btn.textContent = '⚡ Подключить'; }
  toast(d.message, d.ok ? 'ok' : 'err');
  if (d.ok) {
    // Небольшая задержка чтобы бэкенд успел обновить account.get()
    setTimeout(updateStatus, 300);
    setTimeout(updateStatus, 1500);
  }
}

// ─── Status polling ──────────────────────────────────────────────────────────
async function updateStatus() {
  const d = await api('/api/status');
  if (!d || !d.stats) return;

  // Status dot
  const dot = document.getElementById('status-dot');
  dot.className = 'status-dot ' + d.status;
  const lbl = { running: 'Работает', connecting: 'Подключение...', stopped: 'Остановлен', error: 'Ошибка' };
  document.getElementById('status-text').textContent = lbl[d.status] || d.status;

  // Bot session stats
  const s = d.stats || {};
  document.getElementById('s-rcv').textContent = s.messages_received ?? 0;
  document.getElementById('s-snt').textContent = s.messages_sent ?? 0;
  document.getElementById('s-ord').textContent = s.orders_processed ?? 0;
  document.getElementById('s-rsd').textContent = s.lots_raised ?? 0;
  document.getElementById('s-rev').textContent = s.reviews_sent ?? 0;
  document.getElementById('s-upt').textContent = s.uptime || '—';

  // Account block
  const ab = document.getElementById('acc-block');
  const btn = document.getElementById('btn-connect');

  if (d.account && d.account.username) {
    const a = d.account;
    // FIX: правильные скобки вокруг тернарного оператора
    const statusBadge = (d.status === 'running') ? 'Бот активен' : 'Аккаунт подключён';
    const badgeClass = (d.status === 'running') ? 'badge-green' : 'badge-blue';
    ab.innerHTML =
      '<div class="acc-row">' +
        '<div class="acc-avatar">&#128100;</div>' +
        '<div>' +
          '<div class="acc-name">' + esc(a.username) + '</div>' +
          '<div class="acc-meta">ID: ' + esc(String(a.id)) + '</div>' +
        '</div>' +
        '<div class="acc-badges">' +
          '<span class="badge ' + badgeClass + '" style="font-size:12px;padding:4px 12px">' +
            esc(statusBadge) +
          '</span>' +
        '</div>' +
      '</div>';

    // Защита от null/undefined — показываем 0 если нет данных
    const bal = (a.balance !== null && a.balance !== undefined) ? a.balance : '—';
    const cur = a.currency || '';
    const sales = (a.active_sales !== null && a.active_sales !== undefined) ? a.active_sales : '—';
    const purch = (a.active_purchases !== null && a.active_purchases !== undefined) ? a.active_purchases : '—';
    document.getElementById('fp-balance').textContent  = bal;
    document.getElementById('fp-currency').textContent = cur;
    document.getElementById('fp-sales').textContent    = sales;
    document.getElementById('fp-purchases').textContent = purch;

    if (btn) btn.style.display = 'none';
  } else {
    ab.innerHTML =
      '<div class="acc-disconnected">' +
        '<div class="acc-dis-icon">&#9672;</div>' +
        '<div>' +
          '<div style="font-weight:600;margin-bottom:4px">Аккаунт не подключён</div>' +
          '<div style="color:var(--muted);font-size:11px">Укажите golden_key в Настройках и нажмите «Подключить»</div>' +
        '</div>' +
      '</div>';

    ['fp-balance','fp-sales','fp-purchases'].forEach(id => {
      document.getElementById(id).textContent = '—';
    });
    document.getElementById('fp-currency').textContent = '';
    if (btn) btn.style.display = '';
  }

  // FIX: таймер следующего поднятия
  const rs = d.raise_status || {};
  if (rs.running && rs.next_raise_in != null) {
    startRaiseCountdown(rs.next_raise_in);
  } else if (!rs.running) {
    stopRaiseCountdown();
  }
}

// ─── Raise countdown ─────────────────────────────────────────────────────────
function startRaiseCountdown(secondsFromNow) {
  stopRaiseCountdown();
  _nextRaiseAt = Date.now() + secondsFromNow * 1000;
  _raiseCountdownTimer = setInterval(tickRaiseCountdown, 1000);
  tickRaiseCountdown();
}

function stopRaiseCountdown() {
  if (_raiseCountdownTimer) { clearInterval(_raiseCountdownTimer); _raiseCountdownTimer = null; }
  _nextRaiseAt = null;
  const el = document.getElementById('next-raise-label');
  if (el) el.textContent = '';
}

function tickRaiseCountdown() {
  const el = document.getElementById('next-raise-label');
  if (!el || _nextRaiseAt == null) return;
  const sec = Math.max(0, Math.round((_nextRaiseAt - Date.now()) / 1000));
  const m = Math.floor(sec / 60), s = sec % 60;
  el.textContent = `следующий через ${m}:${String(s).padStart(2,'0')}`;
  if (sec === 0) stopRaiseCountdown();
}

// ─── WebSocket logs ───────────────────────────────────────────────────────────
function connectWS() {
  // FIX: обнуляем ws до проверки чтобы if(ws) return не блокировал переподключение
  if (ws && ws.readyState !== WebSocket.CLOSED) return;
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }

  try {
    ws = new WebSocket('ws://127.0.0.1:8765/ws/logs');
  } catch (e) {
    ws = null;
    _wsReconnectTimer = setTimeout(connectWS, 3000);
    return;
  }

  ws.onopen = () => {
    console.log('[WS] Connected');
    document.getElementById('ws-indicator')?.classList.add('active');
  };

  ws.onmessage = e => {
    try {
      const entry = JSON.parse(e.data);
      // Пропускаем heartbeat
      if (entry.type === 'ping') return;

      allLogs.push(entry);
      if (allLogs.length > 2000) allLogs.shift();
      appendLog(entry);

      // FIX: уведомление при новом заказе через WS
      if (entry.category === 'new_order') {
        try {
          const data = JSON.parse(entry.message);
          showDesktopNotif(
            '🛒 Новый заказ!',
            `От: ${data.buyer}\nСумма: ${data.price} ₽`
          );
          toast(`🛒 Заказ от ${data.buyer} — ${data.price} ₽`, 'ok');
          // Мигаем счётчик заказов
          flashEl('s-ord');
        } catch (_) {}
      }
    } catch (_) {}
  };

  ws.onclose = () => {
    ws = null;
    document.getElementById('ws-indicator')?.classList.remove('active');
    _wsReconnectTimer = setTimeout(connectWS, 3000);
  };

  ws.onerror = () => {
    ws?.close();
  };
}

function flashEl(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('flash');
  void el.offsetWidth; // reflow
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 900);
}

function appendLog(entry) {
  if (logFilter !== 'all' && entry.category !== logFilter && entry.level !== logFilter) return;
  if (entry.category === 'new_order') return; // служебная категория, в лог-лент не выводим
  const feed = document.getElementById('log-feed');
  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML =
    `<span class="log-time">${entry.time}</span>` +
    `<span class="log-cat ${esc(entry.category)}">[${esc(entry.category)}]</span>` +
    `<span class="log-msg ${esc(entry.level)}">${esc(entry.message)}</span>`;
  feed.appendChild(div);
  // Авто-скролл только если пользователь не скролит вверх
  if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80) {
    feed.scrollTop = feed.scrollHeight;
  }
}

function renderLogs() {
  const feed = document.getElementById('log-feed');
  const filtered = logFilter === 'all'
    ? allLogs.filter(l => l.category !== 'new_order')
    : allLogs.filter(l => (l.category === logFilter || l.level === logFilter) && l.category !== 'new_order');
  feed.innerHTML = filtered.map(l =>
    `<div class="log-line">` +
    `<span class="log-time">${l.time}</span>` +
    `<span class="log-cat ${esc(l.category)}">[${esc(l.category)}]</span>` +
    `<span class="log-msg ${esc(l.level)}">${esc(l.message)}</span>` +
    `</div>`
  ).join('');
  feed.scrollTop = feed.scrollHeight;
}

function setFilter(f, btn) {
  logFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  renderLogs();
}

async function clearLogs() {
  await api('/api/logs/clear', { method: 'POST' });
  allLogs = [];
  document.getElementById('log-feed').innerHTML = '';
  toast('Логи очищены');
}

// ─── Settings ─────────────────────────────────────────────────────────────────
async function loadSettings() {
  const d = await api('/api/config');
  if (!d) return;
  const gkInput = document.getElementById('cfg-gk');
  // Проверяем есть ли ключ в safeStorage
  const keyExists = await window.electron.keyExists();
  if (keyExists) {
    gkInput.placeholder = '🔒 Ключ сохранён в защищённом хранилище Windows — введите новый для замены';
  } else {
    gkInput.placeholder = 'Вставьте значение куки golden_key';
  }
  gkInput.value = '';
  document.getElementById('cfg-ua').value = d.user_agent || '';
}

async function saveSettings() {
  const patch = { user_agent: document.getElementById('cfg-ua').value };
  const gk = document.getElementById('cfg-gk').value.trim();

  if (gk) {
    // Сохраняем golden_key в Electron safeStorage (DPAPI/Keychain) — не в файл!
    await window.electron.keySave(gk);
    document.getElementById('cfg-gk').value = '';
    // Перезапускаем бэкенд чтобы он получил новый ключ через env
    toast('Ключ сохранён. Перезапускаю бэкенд...', '');
    await window.electron.backendRestart();
    await new Promise(r => setTimeout(r, 1500));
    await doConnect();
  }

  const d = await apiPost('/api/config', { data: patch });
  toast(d.ok ? 'Настройки сохранены' : (d.message || 'Ошибка'), d.ok ? 'ok' : 'err');
  loadSettings();
}

async function patchCfg(obj) {
  await apiPost('/api/config', { data: obj });
}

// ─── Auto-response ────────────────────────────────────────────────────────────
async function loadAR() {
  const d = await api('/api/config');
  document.getElementById('ar-on').checked = d?.auto_response?.enabled || false;
  _triggers = d?.auto_response?.triggers || [];
  renderTriggers();
}

function renderTriggers() {
  const list = document.getElementById('trigger-list');
  if (!_triggers.length) {
    list.innerHTML = '<div class="text-dim">Нет триггеров. Нажмите «Добавить».</div>';
    return;
  }
  list.innerHTML = _triggers.map((t, i) =>
    `<div class="trigger-item">` +
    `<button class="trigger-del" onclick="delTrigger(${i})">✕</button>` +
    `<div class="form-grid" style="grid-template-columns:1fr 1fr;">` +
      `<div class="form-group"><label>Ключевые слова (через запятую)</label>` +
        `<input type="text" class="tr-kw" value="${esc((t.keywords||[]).join(', '))}"></div>` +
      `<div class="form-group"><label>Текст ответа</label>` +
        `<input type="text" class="tr-resp" value="${esc(t.response||'')}"></div>` +
    `</div></div>`
  ).join('');
}

function addTrigger() {
  collectTriggers();
  _triggers.push({ keywords: [], response: '' });
  renderTriggers();
  // Фокус на последний инпут
  const inputs = document.querySelectorAll('.tr-kw');
  inputs[inputs.length - 1]?.focus();
}

function delTrigger(i) {
  collectTriggers();
  _triggers.splice(i, 1);
  renderTriggers();
}

function collectTriggers() {
  _triggers = Array.from(document.querySelectorAll('.trigger-item')).map(el => ({
    keywords: el.querySelector('.tr-kw').value.split(',').map(s => s.trim()).filter(Boolean),
    response: el.querySelector('.tr-resp').value,
  }));
}

async function saveAR() {
  collectTriggers();
  const d = await apiPost('/api/config', { data: {
    'auto_response.enabled': document.getElementById('ar-on').checked,
    'auto_response.triggers': _triggers,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

// ─── Auto raise ───────────────────────────────────────────────────────────────
async function loadRaise() {
  const d = await api('/api/config');
  document.getElementById('raise-on').checked    = d?.auto_raise?.enabled || false;
  document.getElementById('raise-int').value     = d?.auto_raise?.interval_minutes || 60;
  document.getElementById('rv-on').checked       = d?.auto_review?.enabled || false;
  document.getElementById('rv-text').value       = d?.auto_review?.text || 'Спасибо за покупку!';
  document.getElementById('rv-rating').value     = d?.auto_review?.rating || 5;
  document.getElementById('gr-on').checked       = d?.greeting?.enabled || false;
  document.getElementById('gr-text').value       = d?.greeting?.text || 'Привет! Чем могу помочь?';
  document.getElementById('gr-cool').value       = d?.greeting?.cooldown_hours || 24;

  // FIX: загружаем категории с сервера и рендерим чекбоксы
  await loadCategoryCheckboxes(d?.auto_raise?.categories || []);
}

async function loadCategoryCheckboxes(selectedIds) {
  const container = document.getElementById('cats-checkboxes');
  if (!container) return;

  const res = await api('/api/categories');
  const categories = res.categories || [];

  if (!categories.length) {
    // Если аккаунт не подключён — показываем ручной ввод
    container.innerHTML =
      '<div class="text-dim" style="margin-bottom:8px">Подключите аккаунт для автовыбора категорий, или введите ID вручную:</div>' +
      '<input type="text" id="raise-cats-manual" class="text-dim" style="' +
        'background:var(--bg3);border:1px solid var(--border2);color:var(--text);' +
        'padding:7px 11px;border-radius:6px;font-family:inherit;font-size:12px;width:100%' +
      '" placeholder="123, 456, 789" value="' + esc(selectedIds.join(', ')) + '">';
    return;
  }

  const selectedSet = new Set(selectedIds.map(String));
  container.innerHTML = categories.map(c =>
    `<label class="cat-checkbox-label">` +
    `<input type="checkbox" class="cat-cb" value="${c.id}" ${selectedSet.has(String(c.id)) ? 'checked' : ''}>` +
    `<span>${esc(c.name)}</span>` +
    `<span class="text-dim" style="margin-left:auto;font-size:10px">ID: ${c.id}</span>` +
    `</label>`
  ).join('');
}

function collectCategories() {
  // Из чекбоксов
  const cbs = document.querySelectorAll('.cat-cb:checked');
  if (cbs.length) return Array.from(cbs).map(cb => cb.value);
  // Из ручного ввода
  const manual = document.getElementById('raise-cats-manual');
  if (manual) return manual.value.split(',').map(s => s.trim()).filter(Boolean);
  return [];
}

async function saveRaise() {
  const cats = collectCategories();
  const d = await apiPost('/api/config', { data: {
    'auto_raise.enabled': document.getElementById('raise-on').checked,
    'auto_raise.interval_minutes': +document.getElementById('raise-int').value,
    'auto_raise.categories': cats,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

async function saveReview() {
  const d = await apiPost('/api/config', { data: {
    'auto_review.enabled': document.getElementById('rv-on').checked,
    'auto_review.text':    document.getElementById('rv-text').value,
    'auto_review.rating':  +document.getElementById('rv-rating').value,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

async function saveGreeting() {
  const d = await apiPost('/api/config', { data: {
    'greeting.enabled':        document.getElementById('gr-on').checked,
    'greeting.text':           document.getElementById('gr-text').value,
    'greeting.cooldown_hours': +document.getElementById('gr-cool').value,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

async function raiseNow() {
  const cats = collectCategories();
  if (!cats.length) { toast('Укажите или выберите категории', 'err'); return; }
  let ok = 0;
  for (const c of cats) {
    const d = await apiPost('/api/raise', { category_id: parseInt(c) });
    if (d.ok) ok++;
    else toast(d.message, 'err');
  }
  if (ok) toast(`Поднято ${ok} из ${cats.length} категорий`, 'ok');
}

// ─── Utility ──────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ─── Onboarding Screen ───────────────────────────────────────────────────────

let _obMode = 'vps';

function obSetMode(mode) {
  _obMode = mode;
  document.getElementById('tab-vps').classList.toggle('active', mode === 'vps');
  document.getElementById('tab-docker').classList.toggle('active', mode === 'docker');
  document.getElementById('ob-vps-panel').style.display    = mode === 'vps'    ? '' : 'none';
  document.getElementById('ob-docker-panel').style.display = mode === 'docker' ? '' : 'none';
}

function obToggleEye(btn) {
  const input = document.getElementById('ob-token');
  const isPass = input.type === 'password';
  input.type = isPass ? 'text' : 'password';
  btn.style.opacity = isPass ? '1' : '0.5';
}

function obOpenTelegram() {
  window.electronAPI && window.electronAPI.openExternal
    ? window.electronAPI.openExternal('https://t.me/FunPayPulseBot')
    : window.open('https://t.me/FunPayPulseBot', '_blank');
}

function obCopyCode(el) {
  const text = el.firstChild.textContent.trim();
  navigator.clipboard.writeText(text).then(() => {
    const hint = el.querySelector('.ob-copy-hint');
    hint.textContent = '✓ скопировано!';
    hint.style.color = '#00e676';
    setTimeout(() => {
      hint.textContent = 'нажмите чтобы скопировать';
      hint.style.color = '';
    }, 2000);
  });
}

async function obConnect() {
  const token = document.getElementById('ob-token').value.trim();
  const errEl = document.getElementById('ob-error');
  const btn   = document.getElementById('ob-connect-btn');

  if (!token) { errEl.textContent = 'Введите токен'; return; }
  if (!token.startsWith('fp_')) {
    errEl.textContent = 'Токен должен начинаться с fp_';
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Подключение...';
  errEl.textContent = '';

  // Вычисляем URL: токен fp_<hash> — хост берём из поля или определяем автоматически
  const hostInput = document.getElementById('ob-host').value.trim();
  let url = hostInput || '';

  // Пробуем подключиться через бэкенд
  const d = await apiPost('/api/update/connect', {
    url: url || 'auto',
    token: token
  });

  btn.disabled = false;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Подключиться к VPS';

  if (d.ok) {
    // Сохраняем и скрываем onboarding
    localStorage.setItem('ob_token', token);
    localStorage.setItem('ob_mode', 'vps');
    document.getElementById('onboarding').style.display = 'none';
    toast('VPS подключён!', 'ok');
    setTimeout(checkUpdates, 1000);
  } else {
    // Если сервер не настроен — просто сохраняем токен и входим
    // (connect endpoint может отсутствовать пока нет update-сервера)
    if (d.message && d.message.includes('не настроен')) {
      localStorage.setItem('ob_token', token);
      localStorage.setItem('ob_mode', 'vps');
      document.getElementById('onboarding').style.display = 'none';
      toast('Токен сохранён', 'ok');
    } else {
      errEl.textContent = d.message || 'Ошибка подключения';
    }
  }
}

async function obConnectDocker() {
  const host  = document.getElementById('ob-docker-host').value.trim() || 'http://localhost:8000';
  const token = document.getElementById('ob-docker-token').value.trim();
  const errEl = document.getElementById('ob-docker-error');

  if (!token) { errEl.textContent = 'Введите токен'; return; }

  const d = await apiPost('/api/update/connect', { url: host, token });
  if (d.ok) {
    localStorage.setItem('ob_token', token);
    localStorage.setItem('ob_mode', 'docker');
    localStorage.setItem('ob_host', host);
    document.getElementById('onboarding').style.display = 'none';
    toast('Docker подключён!', 'ok');
  } else {
    // Принимаем и сохраняем даже если сервер не ответил
    localStorage.setItem('ob_token', token);
    localStorage.setItem('ob_mode', 'docker');
    localStorage.setItem('ob_host', host);
    document.getElementById('onboarding').style.display = 'none';
    toast('Настройки сохранены', 'ok');
  }
}

function showOnboarding() {
  // Проверяем: уже был вход?
  const saved = localStorage.getItem('ob_token');
  if (saved) {
    document.getElementById('onboarding').style.display = 'none';
    // Показываем поле хоста если есть сохранённый
    const host = localStorage.getItem('ob_host');
    if (host) document.getElementById('ob-host').value = host;
    return;
  }
  // Показываем экран входа
  document.getElementById('onboarding').style.display = 'flex';
  setTimeout(() => document.getElementById('ob-token').focus(), 300);
}

// ─── Logout ──────────────────────────────────────────────────────────────────
async function logout() {
  if (!confirm('Выйти? VPS токен будет удалён, потребуется войти заново.')) return;
  // Останавливаем бота
  await api('/api/stop', { method: 'POST' });
  // Очищаем localStorage (VPS токен)
  localStorage.removeItem('ob_token');
  localStorage.removeItem('ob_mode');
  localStorage.removeItem('ob_host');
  // Показываем экран входа
  document.getElementById('onboarding').style.display = 'flex';
  setTimeout(() => document.getElementById('ob-token').focus(), 300);
  toast('Вы вышли. Введите новый токен.', '');
}

async function deleteGoldenKey() {
  if (!confirm('Удалить golden_key из защищённого хранилища?')) return;
  await window.electron.keyDelete();
  toast('Golden key удалён. Укажите новый в Настройках.', 'warn');
  loadSettings();
}

// ─── Update page logic ────────────────────────────────────────────────────────

async function checkUpdates() {
  const d = await api('/api/update/check');
  refreshUpdateUI(await api('/api/update/status'));
  if (d.has_update) {
    toast(`⬆ Доступно обновление v${d.remote_version}!`, 'ok');
    document.getElementById('update-nav-icon').classList.add('has-update');
  }
  return d;
}

function refreshUpdateUI(status) {
  if (!status) return;

  // Server status line
  const serverEl = document.getElementById('upd-server-status');
  if (status.configured) {
    const url = status.server_url || '';
    serverEl.innerHTML = `<span style="color:var(--green)">● Подключено</span> — <span style="color:var(--dim)">${esc(url)}</span>`;
  } else {
    serverEl.innerHTML = '<span style="color:var(--red)">● Не настроен</span>';
  }

  const localVer  = status.local_version  || '—';
  const meta      = status.meta || {};
  const remoteVer = meta.remote_version   || meta.version || '—';
  const hasUpdate = status.has_update;

  document.getElementById('upd-cur-ver').textContent          = localVer;
  document.getElementById('upd-new-ver').textContent          = remoteVer;
  document.getElementById('upd-local-ver-label').textContent  = `Версия: ${localVer}`;

  const changelog = meta.changelog || '';
  document.getElementById('upd-changelog').textContent = changelog;

  document.getElementById('upd-available-card').style.display = hasUpdate ? '' : 'none';
  document.getElementById('upd-latest-card').style.display    = (!hasUpdate && status.configured) ? '' : 'none';

  // Nav icon
  if (hasUpdate) {
    document.getElementById('update-nav-icon').classList.add('has-update');
  } else {
    document.getElementById('update-nav-icon').classList.remove('has-update');
  }
}

let _progressPoller = null;

async function applyUpdate() {
  const btn = document.getElementById('upd-apply-btn');
  btn.disabled = true;
  btn.textContent = 'Загрузка...';

  await api('/api/update/apply', { method: 'POST' });

  // Поллим прогресс
  _progressPoller = setInterval(async () => {
    const p = await api('/api/update/progress');
    const pct = p.pct || 0;

    document.getElementById('upd-bar').style.width   = pct + '%';
    document.getElementById('upd-pct').textContent   = pct + '%';

    if (p.status === 'downloading') {
      const done  = p.done  ? Math.round(p.done  / 1024) : 0;
      const total = p.total ? Math.round(p.total / 1024) : 0;
      document.getElementById('upd-progress-label').textContent =
        `Скачивание... ${done} / ${total} KB`;
    } else if (p.status === 'done') {
      clearInterval(_progressPoller);
      document.getElementById('upd-progress-label').textContent = 'Готово! Перезапустите приложение.';
      document.getElementById('upd-bar').style.background = 'var(--green)';
      btn.textContent = '✓ Установлено';
      toast('Обновление установлено. Перезапустите приложение.', 'ok');
      document.getElementById('update-nav-icon').classList.remove('has-update');
    } else if (p.status === 'error') {
      clearInterval(_progressPoller);
      document.getElementById('upd-progress-label').textContent = 'Ошибка: ' + (p.message || '');
      document.getElementById('upd-bar').style.background = 'var(--red)';
      btn.disabled = false;
      btn.textContent = '↻ Повторить';
      toast('Ошибка обновления: ' + (p.message || ''), 'err');
    }
  }, 800);
}

function go_updateSettings() {
  document.getElementById('upd-settings-card').style.display = '';
  // Заполняем текущими значениями
  api('/api/update/status').then(d => {
    if (d && d.server_url) document.getElementById('upd-url-input').value = d.server_url;
  });
}

async function saveUpdateServer() {
  const url   = document.getElementById('upd-url-input').value.trim();
  const token = document.getElementById('upd-token-input').value.trim();
  const resEl = document.getElementById('upd-connect-result');
  resEl.style.color = 'var(--dim)';
  resEl.textContent = 'Подключение...';

  const d = await apiPost('/api/update/connect', { url, token });
  resEl.textContent  = d.message;
  resEl.style.color  = d.ok ? 'var(--green)' : 'var(--red)';

  if (d.ok) {
    document.getElementById('upd-settings-card').style.display = 'none';
    toast(d.message, 'ok');
    const status = await api('/api/update/status');
    refreshUpdateUI(status);
  }
}

// ─── Backend splash ─────────────────────────────────────────────────────────────
electron.onBackendStatus(({ ready, error }) => {
  if (ready) {
    document.getElementById('splash').style.display = 'none';
    requestNotifPermission();
    connectWS();
    updateStatus();
    setTimeout(showOnboarding, 400);
  } else {
    const msg = error || 'Ошибка запуска бэкенда';
    document.getElementById('splash-msg').textContent = msg;
    document.getElementById('splash-msg').style.color = 'var(--red)';
  }
});

// FIX: убираем дублирующий py-log канал (логи уже идут через WS /ws/logs)
// electron.onPyLog убран намеренно

// ─── Init ─────────────────────────────────────────────────────────────────────
(async () => {
  const ver = await electron.version();
  document.getElementById('ver-label').textContent = 'v' + ver;

  const ready = await electron.backendReady();
  if (ready) {
    document.getElementById('splash').style.display = 'none';
    requestNotifPermission();
    connectWS();
    updateStatus();
    // Показываем модал подключения к VPS (скроется если уже настроен)
    setTimeout(showOnboarding, 400);
  }

  // FIX: поллинг каждые 8 секунд вместо 2
  _statusInterval = setInterval(updateStatus, 5000);
})();

// ─── Blacklist ────────────────────────────────────────────────────────────────
async function loadBlacklist() {
  const d = await api('/api/config');
  const bl = d?.blacklist || {};
  document.getElementById('bl-on').checked = !!bl.enabled;
  document.getElementById('bl-ids').value   = (bl.user_ids   || []).join(', ');
  document.getElementById('bl-names').value = (bl.usernames  || []).join(', ');
}

async function saveBlacklist() {
  const ids   = document.getElementById('bl-ids').value.split(',')
    .map(s => s.trim()).filter(Boolean).map(Number).filter(Boolean);
  const names = document.getElementById('bl-names').value.split(',')
    .map(s => s.trim()).filter(Boolean);
  const d = await apiPost('/api/config', { data: {
    'blacklist.enabled':   document.getElementById('bl-on').checked,
    'blacklist.user_ids':  ids,
    'blacklist.usernames': names,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

// ─── Telegram notifications ───────────────────────────────────────────────────
async function loadTgNotify() {
  const d = await api('/api/config');
  const tg = d?.telegram_notify || {};
  document.getElementById('tg-on').checked = !!tg.enabled;
  document.getElementById('tg-token').value = '';
  document.getElementById('tg-chat').value  = tg.chat_id || '';
  const ar = d?.auto_raise || {};
  document.getElementById('sch-on').checked   = !!ar.schedule_enabled;
  document.getElementById('sch-from').value   = ar.schedule_from || '09:00';
  document.getElementById('sch-to').value     = ar.schedule_to   || '23:00';
}

async function saveTgNotify() {
  const patch = {
    'telegram_notify.enabled': document.getElementById('tg-on').checked,
    'telegram_notify.chat_id': document.getElementById('tg-chat').value.trim(),
  };
  const tok = document.getElementById('tg-token').value.trim();
  if (tok) patch['telegram_notify.bot_token'] = tok;
  const d = await apiPost('/api/config', { data: patch });
  toast(d.message, d.ok ? 'ok' : 'err');
}

async function testTgNotify() {
  const d = await api('/api/notify/test', { method: 'POST' });
  toast(d?.message || (d?.ok ? 'Отправлено!' : 'Ошибка'), d?.ok ? 'ok' : 'err');
}

async function saveSchedule() {
  const d = await apiPost('/api/config', { data: {
    'auto_raise.schedule_enabled': document.getElementById('sch-on').checked,
    'auto_raise.schedule_from':    document.getElementById('sch-from').value,
    'auto_raise.schedule_to':      document.getElementById('sch-to').value,
  }});
  toast(d.message, d.ok ? 'ok' : 'err');
}

// ─── Backups ──────────────────────────────────────────────────────────────────
async function loadBackups() {
  const d = await api('/api/backups');
  const list = document.getElementById('backup-list');
  const backups = d?.backups || [];
  if (!backups.length) {
    list.innerHTML = '<div class="text-dim">Нет бэкапов</div>';
    return;
  }
  list.innerHTML = backups.map(b => `
    <div class="backup-row">
      <span class="backup-name">${esc(b)}</span>
      <button class="btn btn-sm btn-danger" onclick="restoreBackup('${esc(b)}')">↩ Восстановить</button>
    </div>
  `).join('');
}

async function createBackupNow() {
  const d = await apiPost('/api/backups/create', {});
  toast(d?.message || 'Бэкап создан', d?.ok ? 'ok' : 'err');
  loadBackups();
}

async function restoreBackup(filename) {
  if (!confirm(`Восстановить из ${filename}? Текущие настройки будут перезаписаны.`)) return;
  const d = await apiPost('/api/backups/restore', { filename });
  toast(d?.message || 'Восстановлено', d?.ok ? 'ok' : 'err');
  if (d?.ok) loadSettings();
}

// ─── Patch go() to load data for new pages ───────────────────────────────────
const _origGo = go;
window.go = function(page) {
  _origGo(page);
  if (page === 'blacklist') loadBlacklist();
  if (page === 'notify')    loadTgNotify();
  if (page === 'backup')    loadBackups();
};
