/**
 * Electron 主进程入口
 */

import { app } from "electron";

import { registerLive2DProtocol, initLive2DProtocolHandler } from "./live2d-protocol.js";
import { createMainWindow, initWindowEventListeners } from "./window-manager.js";
import { startCursorTracking, stopCursorTracking } from "./cursor-tracker.js";
import { registerIpcHandlers } from "./ipc-handlers.js";
import { ensureChatSettingsLoaded, clearChatSettingsCache } from "./chat-settings.js";

// 注册 live2d:// 协议（必须在 app ready 之前）
registerLive2DProtocol();

// 应用就绪后初始化
app.whenReady().then(() => {
  // 创建主窗口
  createMainWindow();

  // 初始化 Live2D 协议处理器
  initLive2DProtocolHandler();

  // 注册 IPC 处理器
  registerIpcHandlers();

  // 开始光标追踪
  startCursorTracking();

  // 初始化窗口事件监听
  initWindowEventListeners();
});

// 所有窗口关闭时退出（macOS 除外）
app.on("window-all-closed", () => {
  stopCursorTracking();

  if (process.platform !== "darwin") {
    app.quit();
  }
});
