const { app, BrowserWindow, Tray, Menu, screen, ipcMain, nativeImage, session } = require('electron');
const path = require('path');
const WebSocket = require('ws');

const WS_PORT = 7373;

let overlayWin = null;
let controlWin = null;
let tray = null;
let wsConnection = null;

function sendOverlayBoundsToRenderer() {
  if (!overlayWin || overlayWin.isDestroyed()) return;
  overlayWin.webContents.send('overlay-bounds', overlayWin.getBounds());
}

function createOverlayWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { x, y, width, height } = primaryDisplay.bounds;

  overlayWin = new BrowserWindow({
    width,
    height,
    x,
    y,
    transparent: true,
    frame: false,
    focusable: false,
    resizable: false,
    movable: false,
    hasShadow: false,
    skipTaskbar: true,
    fullscreenable: false,
    webPreferences: {
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  overlayWin.setAlwaysOnTop(true, 'screen-saver');
  overlayWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWin.setIgnoreMouseEvents(true, { forward: true });
  overlayWin.loadFile(path.join(__dirname, 'renderer', 'overlay.html'));
  overlayWin.webContents.on('did-finish-load', sendOverlayBoundsToRenderer);
  overlayWin.on('move', sendOverlayBoundsToRenderer);
}

function createControlWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { x, y, width, height } = primaryDisplay.workArea;

  const initialWidth = Math.min(520, width - 40);
  const initialHeight = 248;
  const initialX = Math.round(x + (width - initialWidth) / 2);
  const initialY = Math.round(y + height - initialHeight - 20);

  controlWin = new BrowserWindow({
    width: initialWidth,
    height: initialHeight,
    minWidth: 400,
    minHeight: 220,
    x: initialX,
    y: initialY,
    frame: false,
    transparent: true,
    resizable: true,
    movable: true,
    fullscreenable: false,
    maximizable: false,
    minimizable: false,
    hasShadow: false,
    skipTaskbar: true,
    webPreferences: {
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  controlWin.setAlwaysOnTop(true, 'floating');
  controlWin.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  controlWin.loadFile(path.join(__dirname, 'renderer', 'control.html'));

  controlWin.on('close', (e) => {
    if (app.isQuitting) return;
    e.preventDefault();
    controlWin.hide();
    if (overlayWin && !overlayWin.isDestroyed()) overlayWin.hide();
  });

  controlWin.on('show', () => {
    if (overlayWin && !overlayWin.isDestroyed()) {
      overlayWin.showInactive();
    }
  });
}

function configureMediaPermissions() {
  const ses = session.defaultSession;
  ses.setPermissionCheckHandler((_webContents, permission, _origin, details) => {
    if (permission === 'media') {
      return details?.mediaType === 'audio' || details?.mediaType === 'unknown';
    }
    return true;
  });

  ses.setPermissionRequestHandler((_webContents, permission, callback, details) => {
    if (permission === 'media') {
      const mediaTypes = details?.mediaTypes || [];
      return callback(mediaTypes.length === 0 || mediaTypes.includes('audio'));
    }
    callback(true);
  });
}

function createTray() {
  const icon = nativeImage.createFromBuffer(
    Buffer.alloc(16 * 16 * 4, 0),
    { width: 16, height: 16 }
  );
  tray = new Tray(icon);
  tray.setTitle('N');
  tray.setToolTip('Navi');

  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: 'Show Navi',
      click: () => {
        if (controlWin) {
          controlWin.showInactive();
        }
        if (overlayWin) {
          overlayWin.showInactive();
        }
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]));
}

function sendToRenderers(channel, payload) {
  for (const win of [overlayWin, controlWin]) {
    if (win && !win.isDestroyed()) {
      win.webContents.send(channel, payload);
    }
  }
}

function connectWebSocket() {
  const url = `ws://localhost:${WS_PORT}`;
  let retryDelay = 500;
  const maxDelay = 8000;

  function attempt() {
    const ws = new WebSocket(url);

    ws.on('open', () => {
      console.log('[Navi] WebSocket connected to Python backend');
      wsConnection = ws;
      retryDelay = 500;

      const display = screen.getPrimaryDisplay();
      const { scaleFactor, bounds, workArea } = display;
      ws.send(JSON.stringify({
        event: 'dpr',
        payload: {
          scaleFactor,
          logicalWidth: bounds.width,
          logicalHeight: bounds.height,
          workAreaY: workArea.y,
          workAreaHeight: workArea.height,
        },
      }));

      sendToRenderers('ws-connected');
    });

    ws.on('message', (data) => {
      sendToRenderers('ws-message', data.toString());
    });

    ws.on('close', () => {
      console.log('[Navi] WebSocket disconnected, retrying...');
      wsConnection = null;
      sendToRenderers('ws-disconnected');
      setTimeout(attempt, retryDelay);
      retryDelay = Math.min(retryDelay * 2, maxDelay);
    });

    ws.on('error', () => {});
  }

  attempt();
}

app.whenReady().then(() => {
  if (app.dock) app.dock.hide();
  configureMediaPermissions();
  createOverlayWindow();
  createControlWindow();
  createTray();
  connectWebSocket();

  ipcMain.on('ws-send', (_event, data) => {
    if (wsConnection && wsConnection.readyState === WebSocket.OPEN) {
      wsConnection.send(data);
    }
  });

  ipcMain.on('control-blur', () => {
    if (controlWin && !controlWin.isDestroyed()) {
      controlWin.blur();
    }
  });
});

app.on('before-quit', () => {
  app.isQuitting = true;
  if (controlWin) {
    controlWin.removeAllListeners('close');
    controlWin.close();
  }
  if (overlayWin) {
    overlayWin.close();
  }
});

app.on('window-all-closed', () => {
  app.quit();
});
