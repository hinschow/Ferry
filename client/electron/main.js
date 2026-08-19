// Ferry 的 Electron 壳。
//
// 它只做一件事：把 Python agent 拉起来，然后开个窗口加载 agent 的地址。
// 界面和全部逻辑都在 Python 那边（ferry_core.py / ferry_agent.py）——
// 换界面不该重写那些踩坑换来的平台逻辑，所以壳保持极薄。
//
// 不装 Electron 也能用：python ferry_agent.py --open 就是同一个界面。
const { app, BrowserWindow, Menu, Tray, nativeImage, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// 开发时 agent 和 ui 在上一层；打包后它们作为 extra-resource 放在
// resources/ 下（和 aTimes 把 atimes-agent.exe 放 resources/bin 一个道理）。
const ROOT = () => (app.isPackaged ? process.resourcesPath : path.join(__dirname, '..'));
let agent = null, win = null, tray = null, agentUrl = '';

function pickPython() {
  // 打包后优先用随包的解释器；否则用系统的
  const cands = process.platform === 'win32'
    ? ['pythonw.exe', 'python.exe', 'py']
    : ['python3', 'python'];
  return cands[0];
}

function startAgent() {
  return new Promise((resolve, reject) => {
    const py = pickPython();
    agent = spawn(py, [path.join(ROOT(), 'ferry_agent.py')], {
      cwd: ROOT(), windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('agent 启动超时（15 秒）')), 15000);
    agent.stdout.on('data', d => {
      buf += d.toString();
      const m = buf.match(/http:\/\/127\.0\.0\.1:\d+\/\?t=\S+/);
      if (m) { clearTimeout(timer); agentUrl = m[0]; resolve(m[0]); }
    });
    agent.stderr.on('data', d => process.stderr.write('[agent] ' + d));
    agent.on('exit', code => {
      clearTimeout(timer);
      if (!agentUrl) reject(new Error('agent 退出，code=' + code));
    });
    agent.on('error', e => { clearTimeout(timer); reject(e); });
  });
}

function createWindow(url) {
  win = new BrowserWindow({
    width: 1180, height: 780, minWidth: 900, minHeight: 560,
    backgroundColor: '#23262d',          // 和界面底色一致，避免开窗白闪
    autoHideMenuBar: true,
    icon: path.join(ROOT(), 'assets', 'ferry.png'),
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  win.loadURL(url);
  // 外链走系统浏览器，别在应用窗口里打开
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.on('close', e => {
    // 关窗口只是收进托盘 —— 隧道还得留着
    if (!app.isQuitting) { e.preventDefault(); win.hide(); }
  });
}

function createTray() {
  const p = path.join(ROOT(), 'assets', 'ferry.png');
  if (!fs.existsSync(p)) return;
  tray = new Tray(nativeImage.createFromPath(p).resize({ width: 16, height: 16 }));
  tray.setToolTip('Ferry 桥接控制台');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开控制台', click: () => { win.show(); win.focus(); } },
    { label: '在浏览器中打开', click: () => shell.openExternal(agentUrl) },
    { type: 'separator' },
    { label: '退出（隧道一并停止）', click: () => { app.isQuitting = true; app.quit(); } },
  ]));
  tray.on('click', () => { win.isVisible() ? win.hide() : win.show(); });
}

// Electron 默认把 userData 放在 %APPDATA%\<productName>，正好和我们放配置的
// 目录撞上 —— 缓存目录会和 bridge-config.json、status/ 混在一起，用户"清理
// 应用数据"就可能把配置一起删了。挪进子目录隔开。
app.setPath('userData', path.join(app.getPath('appData'), 'Ferry', 'electron'));

app.whenReady().then(async () => {
  try {
    const url = await startAgent();
    createWindow(url);
    createTray();
  } catch (e) {
    dialog.showErrorBox('Ferry 启动失败',
      `${e.message}\n\n请确认这台机器装了 Python 3，且 ferry_agent.py 在：\n${ROOT()}`);
    app.quit();
  }
});

app.on('before-quit', () => { app.isQuitting = true; if (agent) agent.kill(); });
app.on('window-all-closed', () => { /* 留在托盘，不退出 */ });
app.on('activate', () => { if (win) win.show(); });
