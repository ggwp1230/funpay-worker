'use strict';

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell } = require('electron');
const path    = require('path');
const { spawn, execSync, spawnSync } = require('child_process');
const http    = require('http');
const net     = require('net');
const fs      = require('fs');

// ──────────────────────────────────────────────────
// Paths
// ──────────────────────────────────────────────────
const IS_PACKAGED = app.isPackaged;

// В собранном приложении backend лежит в resources/backend
// В dev режиме — рядом с electron/
const BACKEND = IS_PACKAGED
  ? path.join(process.resourcesPath, 'backend')
  : path.join(__dirname, '..', 'backend');

// ──────────────────────────────────────────────────
// State
// ──────────────────────────────────────────────────
let mainWindow   = null;
let tray         = null;
let pyProc       = null;
let backendReady = false;
const API_PORT   = 8765;
const API_URL    = `http://127.0.0.1:${API_PORT}`;

// ──────────────────────────────────────────────────
// Убиваем старый процесс на порту
// ──────────────────────────────────────────────────
function freePort(port) {
  try {
    const out = execSync(
      `netstat -ano | findstr "127.0.0.1:${port} " | findstr "LISTENING"`,
      { encoding: 'utf8', timeout: 5000 }
    );
    const lines = out.trim().split('\n').filter(Boolean);
    for (const line of lines) {
      const parts = line.trim().split(/\s+/);
      const pid = parts[parts.length - 1];
      if (pid && /^\d+$/.test(pid) && pid !== '0') {
        try { execSync(`taskkill /F /PID ${pid}`, { timeout: 3000 }); } catch (_) {}
      }
    }
  } catch (_) {}
}

function isPortFree(port) {
  return new Promise(resolve => {
    const s = net.createServer();
    s.once('error', () => resolve(false));
    s.once('listening', () => { s.close(); resolve(true); });
    s.listen(port, '127.0.0.1');
  });
}

// ──────────────────────────────────────────────────
// Python detection
// ──────────────────────────────────────────────────
function getPython() {
  // Список кандидатов для поиска
  const candidates = [];

  if (process.platform === 'win32') {
    // 1. Сначала пробуем найти через where python
    try {
      const whereOut = execSync('where python', { encoding: 'utf8', timeout: 5000 });
      const found = whereOut.split('\n').map(s => s.trim()).filter(Boolean);
      candidates.push(...found);
    } catch (_) {}

    // 2. Стандартные пути установки Python
    const pyVers = ['313', '312', '311', '310', '39', '38'];
    for (const v of pyVers) {
      candidates.push(`C:\\Python${v}\\python.exe`);
      candidates.push(`${process.env.LOCALAPPDATA}\\Programs\\Python\\Python${v}\\python.exe`);
    }
    candidates.push('python');
  } else {
    candidates.push('python3', 'python');
  }

  // Проверяем каждый кандидат — нужен Python с нужными модулями
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      // Проверяем что Python существует и работает
      const result = spawnSync(candidate, ['-c', 'import fastapi, uvicorn; print("ok")'], {
        timeout: 8000,
        encoding: 'utf8',
        windowsHide: true,
      });
      if (result.stdout && result.stdout.includes('ok')) {
        console.log('[Electron] Found Python with fastapi: ' + candidate);
        return candidate;
      }
    } catch (_) {}
  }

  // Fallback — просто python
  console.log('[Electron] Using fallback python');
  return process.platform === 'win32' ? 'python' : 'python3';
}

// ──────────────────────────────────────────────────
// Backend
// ──────────────────────────────────────────────────
async function startBackend() {
  // Освобождаем порт если занят
  const free = await isPortFree(API_PORT);
  if (!free) {
    console.log(`[Electron] Port ${API_PORT} busy, killing...`);
    freePort(API_PORT);
    await new Promise(r => setTimeout(r, 500));
  }

  const python = getPython();
  const script = path.join(BACKEND, 'main.py');

  console.log('[Electron] Backend path: ' + BACKEND);
  console.log('[Electron] Script: ' + script);
  console.log('[Electron] Python: ' + python);

  if (!fs.existsSync(script)) {
    console.error('[Electron] main.py not found at: ' + script);
    if (mainWindow) mainWindow.webContents.send('backend-status', {
      ready: false,
      error: 'main.py не найден: ' + script
    });
    return;
  }

  pyProc = spawn(python, [script], {
    cwd: BACKEND,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  pyProc.stdout.on('data', d => {
    const line = d.toString().trim();
    if (line) console.log('[PY] ' + line);
  });

  pyProc.stderr.on('data', d => {
    const line = d.toString().trim();
    if (!line) return;
    if (line.includes('INFO:') && !line.includes('ERROR')) return;
    console.error('[PY-ERR] ' + line);
  });

  pyProc.on('exit', (code, signal) => {
    console.log(`[Electron] Python exited: code=${code} signal=${signal}`);
    backendReady = false;
    if (mainWindow) mainWindow.webContents.send('backend-status', { ready: false, code });
  });

  pyProc.on('error', err => {
    console.error('[Electron] Failed to start Python: ' + err.message);
    if (mainWindow) mainWindow.webContents.send('backend-status', {
      ready: false,
      error: `Не удалось запустить Python.\n\nУбедитесь что Python установлен и выполните:\npip install fastapi uvicorn requests beautifulsoup4 lxml\n\nОшибка: ${err.message}`
    });
  });
}

function stopBackend() {
  if (!pyProc) return;
  try {
    if (process.platform === 'win32') {
      spawnSync('taskkill', ['/F', '/T', '/PID', String(pyProc.pid)], { timeout: 3000 });
    } else {
      pyProc.kill('SIGTERM');
    }
  } catch (_) {}
  pyProc = null;
}

function waitForBackend(retries = 40, delay = 500) {
  return new Promise((resolve, reject) => {
    let tries = 0;
    function check() {
      const req = http.get(API_URL + '/api/ping', res => {
        if (res.statusCode === 200) { backendReady = true; resolve(); }
        else retry();
        res.resume();
      });
      req.on('error', retry);
      req.setTimeout(1000, () => { req.destroy(); retry(); });
    }
    function retry() {
      if (++tries >= retries) return reject(new Error('Backend не запустился. Убедитесь что Python установлен с зависимостями:\npip install fastapi uvicorn requests beautifulsoup4 lxml requests-toolbelt'));
      setTimeout(check, delay);
    }
    check();
  });
}

// ──────────────────────────────────────────────────
// Window
// ──────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 780,
    minWidth: 900,
    minHeight: 600,
    frame: false,
    backgroundColor: '#080c10',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  mainWindow.loadFile(path.join(__dirname, 'src', 'index.html'));
  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.on('close', e => { e.preventDefault(); mainWindow.hide(); });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function createTray() {
  const iconPath = path.join(__dirname, 'assets', 'tray.png');
  const img = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath)
    : nativeImage.createEmpty();

  tray = new Tray(img);
  tray.setToolTip('FunPay Bot');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Открыть', click: () => mainWindow?.show() },
    { type: 'separator' },
    { label: 'Выход', click: () => app.quit() },
  ]));
  tray.on('double-click', () => mainWindow?.show());
}

// ──────────────────────────────────────────────────
// IPC
// ──────────────────────────────────────────────────
ipcMain.handle('app-minimize', () => mainWindow?.minimize());
ipcMain.handle('app-maximize', () => {
  if (!mainWindow) return;
  mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
});
ipcMain.handle('app-close',   () => mainWindow?.hide());
ipcMain.handle('app-quit',    () => app.quit());
ipcMain.handle('app-version', () => app.getVersion());
ipcMain.handle('open-logs-folder', () => shell.openPath(path.join(BACKEND, 'logs')));
ipcMain.handle('backend-url',   () => API_URL);
ipcMain.handle('backend-ready', () => backendReady);

// ──────────────────────────────────────────────────
// App lifecycle
// ──────────────────────────────────────────────────
app.whenReady().then(async () => {
  createWindow();
  createTray();
  await startBackend();

  try {
    await waitForBackend(40, 500);
    console.log('[Electron] Backend ready');
    mainWindow?.webContents.send('backend-status', { ready: true });
  } catch (err) {
    console.error('[Electron] Backend failed: ' + err.message);
    mainWindow?.webContents.send('backend-status', { ready: false, error: err.message });
  }
});

app.on('before-quit', () => {
  mainWindow?.removeAllListeners('close');
  stopBackend();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') { stopBackend(); app.quit(); }
});

app.on('activate', () => {
  if (!mainWindow) createWindow();
  else mainWindow.show();
});
