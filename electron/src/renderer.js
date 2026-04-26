'use strict';
// ─── State ──────────────────────────────────────────────────────────────────
// API_BASE может указывать либо на локальный python-бэкенд (Docker-режим),
// либо на user-VPS воркер (VPS-режим). Конкретный URL сохранён в localStorage
// под ключом 'ob_host' после успешного onboarding'а. Если ничего не сохранено
// — fallback на локальный Electron-бэкенд для обратной совместимости.
function getApiBase() {
  try {
    const h = (localStorage.getItem('ob_host') || '').trim().replace(/\/+$/, '');
    if (h) return h;
  } catch (_) {}
  return 'http://127.0.0.1:8765';
}
function getApiToken() {
  try {
    return (localStorage.getItem('ob_token') || '').trim();
  } catch (_) { return ''; }
}
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
    const tok = getApiToken();
    const headers = Object.assign({}, opts.headers || {});
    if (tok) headers['X-Token'] = tok;
    const r = await fetch(getApiBase() + path, Object.assign({}, opts, { headers }));
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
    // Преобразуем http(s)://host:port → ws(s)://host:port и добавляем
    // токен query-параметром (бэкенд проверяет его в ws_logs).
    const base = getApiBase();
    const wsUrl = base.replace(/^http/, 'ws') + '/ws/logs';
    const tok = getApiToken();
    ws = new WebSocket(tok ? `${wsUrl}?token=${encodeURIComponent(tok)}` : wsUrl);
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

// ─── Theme ────────────────────────────────────────────────────────────────────
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('fpn_theme', t);
  const icon = document.getElementById('theme-icon');
  if (icon) icon.textContent = t === 'light' ? '☀' : '🌙';
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(cur === 'light' ? 'dark' : 'light');
}
// синхронизируем иконку при загрузке
document.addEventListener('DOMContentLoaded', () => {
  applyTheme(localStorage.getItem('fpn_theme') || 'dark');
});

// ─── Utility ──────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
// Экранирование значения, которое подставляется внутрь одинарных кавычек
// в inline-атрибутах вроде onclick="foo('${escJsAttr(x)}')". Покрывает то,
// что esc() не покрывает: backslash и одинарную кавычку.
function escJsAttr(s) {
  return String(s || '')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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

function obToggleEye(btn, inputId) {
  const input = document.getElementById(inputId || 'ob-token');
  if (!input) return;
  const isPass = input.type === 'password';
  input.type = isPass ? 'text' : 'password';
  btn.style.opacity = isPass ? '1' : '0.5';
}

function obOpenTelegram() {
  window.electronAPI && window.electronAPI.openExternal
    ? window.electronAPI.openExternal('https://t.me/FPNexusBot')
    : window.open('https://t.me/FPNexusBot', '_blank');
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

function obBackToConnect() {
  document.getElementById('ob-step-connect').style.display = '';
  document.getElementById('ob-step-key').style.display = 'none';
}

function _normHost(h) {
  h = (h || '').trim().replace(/\/+$/, '');
  if (h && !/^https?:\/\//i.test(h)) h = 'http://' + h;
  return h;
}

async function _probeWorker(url, token) {
  // Проверяем что VPS-воркер живой и токен правильный.
  // /ping публичный (без токена), /api/status — требует токена.
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(url + '/api/status', {
      headers: { 'X-Token': token },
      signal: ctrl.signal,
    });
    clearTimeout(t);
    if (r.status === 401 || r.status === 403) {
      return { ok: false, message: 'Неверный токен (HTTP ' + r.status + ')' };
    }
    if (!r.ok) {
      return { ok: false, message: 'VPS вернул HTTP ' + r.status };
    }
    return { ok: true, data: await r.json() };
  } catch (e) {
    return { ok: false, message: 'VPS не отвечает: ' + (e.message || e) };
  }
}

// URL центрального сервера обновлений — тут хранятся пары (fp-токен → URL VPS).
const CENTRAL_API = 'http://funpaybot.duckdns.org:9000';

async function _lookupWorkerUrl(token) {
  // Спрашиваем у центрального сервера: куда подключаться по этому токену.
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 8000);
    const r = await fetch(
      CENTRAL_API + '/api/vps/lookup?token=' + encodeURIComponent(token),
      { signal: ctrl.signal }
    );
    clearTimeout(t);
    if (r.status === 404) {
      return { ok: false, message: 'Токен не найден. Получите новый: установите worker на VPS через @FPNexusBot.' };
    }
    if (!r.ok) {
      return { ok: false, message: 'Сервер вернул HTTP ' + r.status };
    }
    const j = await r.json();
    const url = (j && j.worker_url || '').trim();
    if (!url) return { ok: false, message: 'У токена не записан адрес VPS — перезапустите install.sh.' };
    return { ok: true, url };
  } catch (e) {
    return { ok: false, message: 'Сервер обновлений недоступен: ' + (e.message || e) };
  }
}

async function obConnect() {
  const token = document.getElementById('ob-token').value.trim();
  const errEl = document.getElementById('ob-error');
  const btn   = document.getElementById('ob-connect-btn');

  if (!token) { errEl.textContent = 'Введите fp-токен'; return; }
  if (!token.startsWith('fp_')) {
    errEl.textContent = 'Токен должен начинаться с fp_';
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Ищу адрес VPS...';
  errEl.textContent = '';

  // 1) Lookup адреса по токену на центральном сервере
  const lookup = await _lookupWorkerUrl(token);
  if (!lookup.ok) {
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Подключиться к VPS';
    errEl.textContent = lookup.message;
    return;
  }
  const host = _normHost(lookup.url);
  document.getElementById('ob-host').value = host;

  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Проверяю VPS...';

  // 2) Проверяем что VPS жив и токен принимается
  const probe = await _probeWorker(host, token);

  btn.disabled = false;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M5 12h14M12 5l7 7-7 7"/></svg> Подключиться к VPS';

  if (!probe.ok) {
    errEl.textContent = probe.message;
    return;
  }

  // Сохраняем — теперь api()/apiPost()/connectWS будут ходить на этот VPS.
  localStorage.setItem('ob_host', host);
  localStorage.setItem('ob_token', token);
  localStorage.setItem('ob_mode', 'vps');
  toast('VPS подключён', 'ok');

  // Проверяем — может быть golden_key уже сохранён на VPS (например после
  // переустановки приложения). В таком случае скипаем шаг ввода ключа.
  if (probe.data && probe.data.has_key) {
    document.getElementById('onboarding').style.display = 'none';
    setTimeout(connectWS, 500);
    setTimeout(updateStatus, 800);
    return;
  }

  // Иначе — переход к шагу ввода golden_key.
  document.getElementById('ob-step-connect').style.display = 'none';
  document.getElementById('ob-step-key').style.display = '';
  setTimeout(() => document.getElementById('ob-gk')?.focus(), 200);
}

async function obSaveKey() {
  const gk    = document.getElementById('ob-gk').value.trim();
  const errEl = document.getElementById('ob-key-error');
  const btn   = document.getElementById('ob-key-btn');

  if (!gk || gk.length < 16) {
    errEl.textContent = 'golden_key слишком короткий — должно быть 32 символа';
    return;
  }

  btn.disabled = true;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="animation:spin 1s linear infinite"><path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg> Запускаю бота...';
  errEl.textContent = '';

  // POST /api/config с golden_key — VPS-бэкенд сохранит ключ в data-volume
  // и автоматически вызовет bot.start() (если был остановлен).
  const d = await apiPost('/api/config', { data: { golden_key: gk } });

  btn.disabled = false;
  btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg> Запустить бота';

  if (!d || d.ok === false) {
    errEl.textContent = (d && d.message) || 'Ошибка сохранения ключа';
    return;
  }

  toast('Бот запускается на VPS', 'ok');
  document.getElementById('onboarding').style.display = 'none';
  setTimeout(connectWS, 500);
  setTimeout(updateStatus, 1500);
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

async function showOnboarding() {
  const ob = document.getElementById('onboarding');
  if (!ob) return;
  // Если в localStorage уже есть VPS-URL и токен, и VPS-воркер на них живой —
  // не показываем онбординг (юзер уже подключён). Иначе показываем.
  let configured = false;
  const host  = (localStorage.getItem('ob_host')  || '').trim();
  const token = (localStorage.getItem('ob_token') || '').trim();
  if (host && token) {
    try {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 5000);
      const r = await fetch(host.replace(/\/+$/, '') + '/api/status', {
        headers: { 'X-Token': token },
        signal: ctrl.signal,
      });
      clearTimeout(t);
      configured = r.ok;
    } catch (_) { /* VPS недоступен — попросим юзера ввести заново */ }
  }
  if (configured) {
    ob.style.display = 'none';
    return;
  }
  ob.style.display = 'flex';
  setTimeout(() => {
    const t = document.getElementById('ob-token');
    if (t) t.focus();
  }, 300);
}

// ─── Logout ──────────────────────────────────────────────────────────────────
async function logout() {
  if (!confirm('Выйти? Бот остановится на VPS, golden_key и адрес будут забыты. Чтобы вернуться — введите токен и адрес снова.')) return;
  // Останавливаем бота на VPS и стираем golden_key с него.
  try { await api('/api/stop', { method: 'POST' }); } catch(_){}
  try { await apiPost('/api/config', { data: { golden_key: '' } }); } catch(_){}
  // Чистим локальное состояние Electron-приложения.
  try { if (window.electron?.keyDelete) await window.electron.keyDelete(); } catch(_){}
  localStorage.removeItem('ob_token');
  localStorage.removeItem('ob_mode');
  localStorage.removeItem('ob_host');
  // Закрываем активный WebSocket — иначе при подключении к другому VPS
  // connectWS() видит ws.readyState !== CLOSED и не переподключается,
  // и логи продолжают идти со старого хоста.
  if (ws) { try { ws.close(); } catch(_){} ws = null; }
  if (_wsReconnectTimer) { clearTimeout(_wsReconnectTimer); _wsReconnectTimer = null; }
  document.getElementById('ws-indicator')?.classList.remove('active');
  toast('Вы вышли. Введите адрес VPS и токен заново.', '');
  // Возвращаем onboarding-экран
  obBackToConnect();
  const ob = document.getElementById('onboarding');
  if (ob) {
    ob.style.display = 'flex';
    setTimeout(() => {
      ['ob-host', 'ob-token', 'ob-gk'].forEach(id => {
        const e = document.getElementById(id);
        if (e) e.value = '';
      });
      document.getElementById('ob-token')?.focus();
    }, 200);
  }
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

  // Server status line (URL не показываем — прячем адрес VPS)
  const serverEl = document.getElementById('upd-server-status');
  if (status.configured) {
    serverEl.innerHTML = `<span style="color:var(--green)">● Подключено</span>`;
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
    } else if (p.status === 'backing_up') {
      document.getElementById('upd-progress-label').textContent =
        `Создаю резервную копию... ${p.done || 0} / ${p.total || 0}`;
    } else if (p.status === 'extracting') {
      document.getElementById('upd-progress-label').textContent =
        `Распаковка... ${p.done || 0} / ${p.total || 0}`;
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
      <button class="btn btn-sm btn-danger" onclick="restoreBackup('${escJsAttr(b)}')">↩ Восстановить</button>
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
  if (page === 'update')    checkUpdates();
  if (page === 'plugins')   { loadInstalledPlugins(); loadPluginStore(); }
};

// ─── Plugins ─────────────────────────────────────────────────────────────────
let _plPendingPluginId = null;
let _plPendingPluginMeta = null;
const _plInstalledById = {};  // id → meta для callback'ов

async function loadInstalledPlugins() {
  const wrap = document.getElementById('pl-installed-list');
  wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0">Загрузка...</div>';
  let d;
  try {
    d = await api('/api/plugins/installed');
  } catch (e) {
    wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0;color:var(--red)">Подсистема плагинов недоступна</div>';
    return;
  }
  if (!d || !Array.isArray(d.plugins)) {
    wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0;color:var(--red)">' +
      esc(d?.detail || 'Не удалось получить список') + '</div>';
    return;
  }
  // обновляем кэш
  Object.keys(_plInstalledById).forEach(k => delete _plInstalledById[k]);
  d.plugins.forEach(p => { _plInstalledById[p.id] = p; });
  if (d.plugins.length === 0) {
    wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0">Пока ничего не установлено</div>';
    return;
  }
  wrap.innerHTML = d.plugins.map(renderInstalledRow).join('');
}

function renderInstalledRow(p) {
  // id для отображения в HTML и id для безопасной подстановки в JS-строку
  // в inline-обработчиках. Они РАЗНЫЕ — последний экранирует одинарные кавычки.
  const idHtml = esc(p.id);
  const idJs = escJsAttr(p.id);
  let badge = '<span class="pl-badge pl-badge-off">выкл</span>';
  if (p.error) badge = '<span class="pl-badge pl-badge-err">ошибка</span>';
  else if (p.enabled && p.loaded) badge = '<span class="pl-badge pl-badge-on">вкл</span>';
  const errBlock = p.error
    ? `<div class="pl-error">⚠ ${esc(p.error)}</div>`
    : '';
  const desc = p.description
    ? `<div class="pl-desc">${esc(p.description)}</div>` : '';
  const author = p.author ? ` · ${esc(p.author)}` : '';
  const hasConfigSchema = Array.isArray(p.config_schema) && p.config_schema.length > 0;
  return `
    <div class="plugin-row" data-id="${idHtml}">
      <div class="pl-main">
        <div class="pl-name">
          ${esc(p.name || p.id)}
          <span class="pl-ver">v${esc(p.version || '?')}</span>
          ${badge}
        </div>
        <div class="pl-meta">${idHtml}${author}</div>
        ${desc}
        ${errBlock}
      </div>
      <div class="pl-actions">
        <label class="pl-toggle ${p.enabled ? 'is-on' : ''}">
          <input type="checkbox" ${p.enabled ? 'checked' : ''}
                 onchange="togglePlugin('${idJs}', this.checked)"> ${p.enabled ? 'Включен' : 'Выключен'}
        </label>
        ${hasConfigSchema
          ? `<button class="btn btn-sm" onclick="openPluginSettingsById('${idJs}')" style="background:var(--bg3);color:var(--text);border:1px solid var(--border)">⚙ Настройки</button>`
          : ''}
        <button class="btn btn-sm" onclick="uninstallPlugin('${idJs}')" style="background:var(--bg3);color:var(--red);border:1px solid var(--border)">🗑 Удалить</button>
      </div>
    </div>`;
}

async function togglePlugin(id, enabled) {
  const d = await apiPost('/api/plugins/toggle', { id, enabled });
  if (d && d.ok) {
    toast(enabled ? `Плагин ${id} включён` : `Плагин ${id} отключён`, 'ok');
  } else {
    toast('Не удалось переключить: ' + (d?.detail || 'ошибка'), 'err');
  }
  loadInstalledPlugins();
}

async function uninstallPlugin(id) {
  if (!confirm(`Удалить плагин ${id}? Его данные и настройки тоже будут стёрты.`)) return;
  const d = await apiPost('/api/plugins/uninstall', { id });
  if (d && d.ok) {
    toast(`Плагин ${id} удалён`, 'ok');
  } else {
    toast('Не удалось удалить: ' + (d?.detail || 'ошибка'), 'err');
  }
  loadInstalledPlugins();
}

async function loadPluginStore() {
  const wrap = document.getElementById('pl-store-list');
  const errEl = document.getElementById('pl-store-error');
  errEl.style.display = 'none';
  wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0">Загрузка...</div>';
  const d = await api('/api/plugins/store');
  if (!d || !Array.isArray(d.plugins)) {
    wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0">Магазин недоступен</div>';
    errEl.style.display = 'block';
    errEl.textContent = d?.detail || 'Проверь подключение к серверу обновлений';
    return;
  }
  if (d.plugins.length === 0) {
    wrap.innerHTML = '<div class="text-dim" style="font-size:12px;text-align:center;padding:18px 0">В магазине пока пусто</div>';
    return;
  }
  wrap.innerHTML = d.plugins.map(renderStoreRow).join('');
}

function renderStoreRow(p) {
  const idHtml = esc(p.id);
  const idJs = escJsAttr(p.id);
  const desc = p.description
    ? `<div class="pl-desc">${esc(p.description)}</div>` : '';
  const author = p.author ? ` · ${esc(p.author)}` : '';
  const sizeKb = p.size ? ` · ${(p.size / 1024).toFixed(1)} КБ` : '';
  return `
    <div class="plugin-row">
      <div class="pl-main">
        <div class="pl-name">
          ${esc(p.name || p.id)}
          <span class="pl-ver">v${esc(p.version || '?')}</span>
        </div>
        <div class="pl-meta">${idHtml}${author}${sizeKb}</div>
        ${desc}
      </div>
      <div class="pl-actions">
        <button class="btn btn-sm btn-primary" onclick="installPlugin('${idJs}')">⬇ Установить</button>
      </div>
    </div>`;
}

async function installPlugin(id) {
  toast(`Загружаю ${id}...`, '');
  const d = await apiPost('/api/plugins/install', { id });
  if (d && d.ok) {
    toast(`Плагин ${id} установлен`, 'ok');
    loadInstalledPlugins();
  } else {
    toast('Не удалось установить: ' + (d?.detail || 'ошибка'), 'err');
  }
}

// ── Settings modal ──
function openPluginSettingsById(id) {
  const p = _plInstalledById[id];
  if (!p) {
    toast('Плагин не найден в кэше — обнови список', 'err');
    return;
  }
  return openPluginSettings(p);
}

async function openPluginSettings(plugin) {
  _plPendingPluginId = plugin.id;
  _plPendingPluginMeta = plugin;
  document.getElementById('pl-settings-title').textContent =
    'Настройки — ' + (plugin.name || plugin.id);
  document.getElementById('pl-settings-result').textContent = '';
  document.getElementById('pl-settings-modal').style.display = 'flex';

  // Получаем актуальные значения
  const d = await api(`/api/plugins/${encodeURIComponent(plugin.id)}/config`);
  const values = (d && d.config) || {};

  const schema = plugin.config_schema || [];
  document.getElementById('pl-settings-form').innerHTML =
    schema.map(f => renderField(f, values[f.key])).join('');
}

function renderField(field, value) {
  const key = esc(field.key);
  const label = esc(field.label || field.key);
  const help = field.help ? `<div class="pl-help">${esc(field.help)}</div>` : '';
  const def = field.default;
  const v = (value !== undefined && value !== null) ? value : (def !== undefined ? def : '');

  switch (field.type) {
    case 'number':
      return `
        <div class="pl-field">
          <label>${label}</label>
          <input type="number" data-pl-key="${key}" value="${esc(String(v ?? ''))}"
                 ${field.min !== undefined ? `min="${esc(String(field.min))}"` : ''}
                 ${field.max !== undefined ? `max="${esc(String(field.max))}"` : ''}
                 ${field.step !== undefined ? `step="${esc(String(field.step))}"` : ''}>
          ${help}
        </div>`;
    case 'checkbox':
    case 'bool':
      return `
        <div class="pl-field">
          <label class="pl-toggle ${v ? 'is-on' : ''}" style="text-transform:none;letter-spacing:0">
            <input type="checkbox" data-pl-key="${key}" ${v ? 'checked' : ''}> ${label}
          </label>
          ${help}
        </div>`;
    case 'textarea':
      return `
        <div class="pl-field">
          <label>${label}</label>
          <textarea data-pl-key="${key}" rows="${field.rows || 5}">${esc(String(v ?? ''))}</textarea>
          ${help}
        </div>`;
    case 'password':
      return `
        <div class="pl-field">
          <label>${label}</label>
          <input type="password" data-pl-key="${key}" value="${esc(String(v ?? ''))}">
          ${help}
        </div>`;
    case 'text':
    default:
      return `
        <div class="pl-field">
          <label>${label}</label>
          <input type="text" data-pl-key="${key}" value="${esc(String(v ?? ''))}">
          ${help}
        </div>`;
  }
}

function closePluginSettings() {
  document.getElementById('pl-settings-modal').style.display = 'none';
  _plPendingPluginId = null;
  _plPendingPluginMeta = null;
}

async function savePluginSettings() {
  if (!_plPendingPluginId) return;
  const values = {};
  document.querySelectorAll('#pl-settings-form [data-pl-key]').forEach(el => {
    const key = el.getAttribute('data-pl-key');
    if (el.type === 'checkbox') values[key] = el.checked;
    else if (el.type === 'number') {
      const n = el.value === '' ? null : Number(el.value);
      values[key] = (n === null || Number.isNaN(n)) ? null : n;
    } else values[key] = el.value;
  });
  const id = _plPendingPluginId;
  const d = await apiPost(`/api/plugins/${encodeURIComponent(id)}/config`, values);
  const res = document.getElementById('pl-settings-result');
  if (d && d.ok) {
    res.style.color = 'var(--green)';
    res.textContent = '✓ Сохранено';
    setTimeout(closePluginSettings, 600);
    setTimeout(loadInstalledPlugins, 700);
  } else {
    res.style.color = 'var(--red)';
    res.textContent = '✗ ' + (d?.detail || 'Ошибка сохранения');
  }
}
