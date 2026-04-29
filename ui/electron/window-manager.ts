/**
 * Electron 窗口管理
 */

import { app, BrowserWindow, screen } from "electron";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  WINDOW_WIDTH,
  WINDOW_HEIGHT,
  SETTINGS_WIDTH,
  SETTINGS_HEIGHT,
} from "./config.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

/**
 * 主窗口引用
 */
let mainWindow: BrowserWindow | null = null;

/**
 * 设置窗口引用
 */
let settingsWindow: BrowserWindow | null = null;

/**
 * 获取主窗口
 */
export const getMainWindow = (): BrowserWindow | null => {
  return mainWindow;
};

/**
 * 获取设置窗口
 */
export const getSettingsWindow = (): BrowserWindow | null => {
  return settingsWindow;
};

/**
 * 设置主窗口引用
 */
export const setMainWindow = (win: BrowserWindow | null): void => {
  mainWindow = win;
};

/**
 * 设置设置窗口引用
 */
export const setSettingsWindow = (win: BrowserWindow | null): void => {
  settingsWindow = win;
};

/**
 * 创建主窗口
 */
export const createMainWindow = (): BrowserWindow => {
  const display = screen.getPrimaryDisplay();
  const { x, y, width, height } = display.workArea;
  const windowX = x + width - WINDOW_WIDTH - 20;
  const windowY = y + height - WINDOW_HEIGHT - 20;

  const win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    x: windowX,
    y: windowY,
    frame: false,
    transparent: true,
    resizable: false,
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    fullscreenable: false,
    webPreferences: {
      preload: path.resolve(__dirname, "../electron/preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true });
  mainWindow = win;

  const devServerUrl = process.env.VITE_DEV_SERVER_URL;
  if (devServerUrl) {
    win.loadURL(devServerUrl);
  } else {
    win.loadFile(path.resolve(__dirname, "../dist/index.html"));
  }

  return win;
};

/**
 * 打开设置窗口
 */
export const openSettingsWindow = (): void => {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.focus();
    return;
  }

  const parentBounds = mainWindow?.getBounds();
  const x = parentBounds ? Math.max(parentBounds.x - SETTINGS_WIDTH - 16, 0) : undefined;
  const y = parentBounds ? Math.max(parentBounds.y, 0) : undefined;

  const win = new BrowserWindow({
    width: SETTINGS_WIDTH,
    height: SETTINGS_HEIGHT,
    x,
    y,
    frame: false,
    transparent: false,
    resizable: false,
    show: false,
    fullscreenable: false,
    backgroundColor: "#141722",
    webPreferences: {
      preload: path.resolve(__dirname, "../electron/preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  const devServerUrl = process.env.VITE_DEV_SERVER_URL;
  if (devServerUrl) {
    win.loadURL(`${devServerUrl}/settings.html`);
  } else {
    win.loadFile(path.resolve(__dirname, "../dist/settings.html"));
  }

  win.once("ready-to-show", () => {
    win.show();
    win.focus();
  });

  win.on("closed", () => {
    if (settingsWindow === win) {
      settingsWindow = null;
    }
  });

  settingsWindow = win;
};

/**
 * 初始化窗口事件监听
 */
export const initWindowEventListeners = (): void => {
  app.on("browser-window-created", (_event, win) => {
    win.on("closed", () => {
      if (mainWindow === win) {
        mainWindow = null;
      }
      if (settingsWindow === win) {
        settingsWindow = null;
      }
    });
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
};
