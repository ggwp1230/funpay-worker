'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electron', {
  minimize:        () => ipcRenderer.invoke('app-minimize'),
  maximize:        () => ipcRenderer.invoke('app-maximize'),
  close:           () => ipcRenderer.invoke('app-close'),
  quit:            () => ipcRenderer.invoke('app-quit'),
  version:         () => ipcRenderer.invoke('app-version'),
  openLogsFolder:  () => ipcRenderer.invoke('open-logs-folder'),
  backendUrl:      () => ipcRenderer.invoke('backend-url'),
  backendReady:    () => ipcRenderer.invoke('backend-ready'),

  // Безопасное хранение golden_key
  keySave:         (key) => ipcRenderer.invoke('key-save', key),
  keyLoad:         ()    => ipcRenderer.invoke('key-load'),
  keyDelete:       ()    => ipcRenderer.invoke('key-delete'),
  keyExists:       ()    => ipcRenderer.invoke('key-exists'),

  // Перезапуск бэкенда
  backendRestart:  ()    => ipcRenderer.invoke('backend-restart'),

  onBackendStatus: (cb) => ipcRenderer.on('backend-status', (_, v) => cb(v)),
  onPyLog:         (cb) => ipcRenderer.on('py-log', (_, v) => cb(v)),
});
