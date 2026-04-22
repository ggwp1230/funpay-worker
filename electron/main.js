'use strict';

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage, shell } = require('electron');
const path  = require('path');
const { spawn, execSync } = require('child_process');
const http  = require('http');
const fs    = require('fs');

// ──────────────────────────────────────────────────
// Paths
// ──────────────────────────────────────────────────
const ROOT       = path.join(__dirname, '..');
const BACKEND    = path.join(ROOT, 'backend');
const PYTHON_WIN = path.join(ROOT, 'python', 'python.exe');
const PYTHON_SYS = process.platform === 'win32' ? 'python' : 'python3';

// ──────────────────────────────────────────────────
// State
// ──────────────────────────────────────────────────
let mainWindow = null;
let tray       = null;
let pyProc     = null;
let backendReady = false;
const API_PORT   = 8765;
const API_URL    = `http://127.0.0.1:${API_PORT}`;

// ──────────────────────────────────────────────────
// Python backend
// ──────────────────────────────────────────────────
function getPython() {
  if (fs.existsSync(PYTHON_WIN)) return PYTHON_WIN;

  if (process.platform === 'win32') {
    try {
      const whereOut = execSync('where python', { encoding: 'utf8', timeout: 5000 });
      const candidates = whereOut.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
      for (var i = 0; i < candidates.length; i++) {
        var candidate = candidates[i];
        try {
          execSync('"' + candidate + '" -c "import lxml"', { timeout: 5000 });
          console.log('[Electron] Using Python with lxml: ' + candidate);
          return candidate;
        } catch (e) {
          console.log('[Electron] Skipping ' + candidate + ' (no lxml)');
        }
      }
    } catch (e) {}
  }

  return PYTHON_SYS;
}

function startBackend() {
  const python = getPython();
  const script = path.join(BACKEND, 'main.py');

  console.log('[Electron] Starting backend: ' + python + ' ' + script);

  pyProc = spawn(python, [script], {
    cwd: BACKEND,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });

  pyProc.stdout.on('data', function(d) {
    const line = d.toString().trim();
    if (line) console.log('[PY] ' + line);
    if (mainWindow) mainWindow.webContents.send('py-log', { level: 'info', text: line });
  });

  pyProc.stderr.on('data', function(d) {
    const line = d.toString().trim();
    if (line) console.error('[PY-ERR] ' + line);
    if (mainWindow) mainWindow.webContents.send('py-log', { level: 'error', text: line });
  });

  pyProc.on('exit', function(code, signal) {
    console.log('[Electron] Python exited: code=' + code + ' signal=' + signal);
    backendReady = false;
    if (mainWindow) mainWindow.webContents.send('backend-status', { ready: false, code: code });
  });
}

function stopBackend() {
  if (pyProc) {
    try { pyProc.kill('SIGTERM'); } catch (e) {}
    pyProc = null;
  }
}

function waitForBackend(retries, delay) {
  retries = retries || 30;
  delay = delay || 500;
  return new Promise(function(resolve, reject) {
    let tries = 0;
    function check() {
      http.get(API_URL + '/api/ping', function(res) {
        if (res.statusCode === 200) {
          backendReady = true;
          resolve();
        } else {
          retry();
        }
      }).on('error', retry);
    }
    function retry() {
      tries++;
      if (tries >= retries) return reject(new Error('Backend timeout'));
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
    icon: path.join(__dirname, 'assets', 'icon.png'),
    show: false,
  });

  mainWindow.loadFile(path.join(__dirname, 'src', 'index.html'));

  mainWindow.once('ready-to-show', function() {
    mainWindow.show();
  });

  mainWindow.on('close', function(e) {
    e.preventDefault();
    mainWindow.hide();
  });

  mainWindow.on('closed', function() { mainWindow = null; });
}

function createTray() {
  const iconPath = path.join(__dirname, 'assets', 'tray.png');
  const img = fs.existsSync(iconPath)
    ? nativeImage.createFromPath(iconPath)
    : nativeImage.createEmpty();

  tray = new Tray(img);
  tray.setToolTip('FunPay Bot');
  const menu = Menu.buildFromTemplate([
    { label: 'Open', click: function() { if (mainWindow) mainWindow.show(); } },
    { type: 'separator' },
    { label: 'Quit', click: function() { app.quit(); } },
  ]);
  tray.setContextMenu(menu);
  tray.on('double-click', function() { if (mainWindow) mainWindow.show(); });
}

// ──────────────────────────────────────────────────
// IPC
// ──────────────────────────────────────────────────
ipcMain.handle('app-minimize', function() { if (mainWindow) mainWindow.minimize(); });
ipcMain.handle('app-maximize', function() {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.handle('app-close',   function() { if (mainWindow) mainWindow.hide(); });
ipcMain.handle('app-quit',    function() { app.quit(); });
ipcMain.handle('app-version', function() { return app.getVersion(); });
ipcMain.handle('open-logs-folder', function() {
  shell.openPath(path.join(BACKEND, 'logs'));
});
ipcMain.handle('backend-url',   function() { return API_URL; });
ipcMain.handle('backend-ready', function() { return backendReady; });

// ──────────────────────────────────────────────────
// App lifecycle
// ──────────────────────────────────────────────────
app.whenReady().then(async function() {
  createWindow();
  createTray();
  startBackend();

  try {
    await waitForBackend(40, 500);
    console.log('[Electron] Backend ready');
    if (mainWindow) mainWindow.webContents.send('backend-status', { ready: true });
  } catch (err) {
    console.error('[Electron] Backend failed: ' + err.message);
    if (mainWindow) mainWindow.webContents.send('backend-status', { ready: false, error: err.message });
  }
});

app.on('before-quit', function() {
  if (mainWindow) mainWindow.removeAllListeners('close');
  stopBackend();
});

app.on('window-all-closed', function() {
  if (process.platform !== 'darwin') {
    stopBackend();
    app.quit();
  }
});

app.on('activate', function() {
  if (!mainWindow) createWindow();
  else mainWindow.show();
});
