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

  onBackendStatus: (cb) => ipcRenderer.on('backend-status', (_, v) => cb(v)),
  onPyLog:         (cb) => ipcRenderer.on('py-log', (_, v) => cb(v)),
});
