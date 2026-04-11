const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('navi', {
  send: (channel, data) => ipcRenderer.send(channel, data),
  on: (channel, callback) => {
    const sub = (_event, ...args) => callback(...args);
    ipcRenderer.on(channel, sub);
    return () => ipcRenderer.removeListener(channel, sub);
  },
});
