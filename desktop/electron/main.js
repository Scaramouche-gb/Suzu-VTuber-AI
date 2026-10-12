// Transparent Wayland window wrapper using Electron
const { app, BrowserWindow } = require('electron');

app.commandLine.appendSwitch('ozone-platform', 'wayland');
app.commandLine.appendSwitch('enable-features', 'UseOzonePlatform');

function createWindow() {
  const win = new BrowserWindow({
    width: 420,
    height: 720,
    transparent: true,
    frame: false,
    hasShadow: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    webPreferences: {
      autoplayPolicy: 'no-user-gesture-required',
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  win.loadURL('http://127.0.0.1:8765/?fit=bust');
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});
