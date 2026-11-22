/**
 * Electron 主进程入口
 */
import { BackendRecoveryChoice } from "./backend-recovery.js";

import { app, dialog, type MessageBoxOptions } from "electron";

import { registerLive2DProtocol, initLive2DProtocolHandler } from "./live2d-protocol.js";
import {
  createMainWindow,
  getMainWindow,
  openChatWindow,
  beginWindowShutdown,
  finishWindowShutdown,
  getTrustedRendererPolicy,
  initWindowEventListeners,
  openLogWindow,
  sendLogBatch,
} from "./window-manager.js";
import { startCursorTracking, stopCursorTracking } from "./cursor-tracker.js";
import { registerIpcHandlers } from "./ipc-handlers.js";
import { startDesktopStream, stopDesktopSession } from "./interaction-adapter.js";
import { clearChatSettingsCache } from "./chat-settings.js";
import { initAyayaImageProtocolHandler } from "./ayaya-image-protocol.js";
import {
  onBackendUnexpectedExit,
  startBackend as startBackendProcess,
  stopBackend,
  forceStopBackend,
} from "./backend-process.js";
import {
  createBackendRecoveryCoordinator,
  type BackendRecoveryDialogModel,
} from "./backend-recovery.js";
import { createApplicationShutdownCoordinator } from "./application-shutdown.js";
import {
  appendFrontendLog,
  closeLogFiles,
  getRecentLogs,
  initializeLogFiles,
} from "./logging/app-logger.js";
import { registerLogIpcHandlers } from "./logging/log-ipc.js";

/**
 * 主进程显示后端退出或重连失败提示，等待用户决定重连还是退出。
 *
 * Args:
 *     model (BackendRecoveryDialogModel): 需要展示的后端恢复对话框信息。
 *
 * Returns:
 *     Promise<BackendRecoveryChoice>: 用户选择的恢复动作。
 */
const showBackendRecoveryDialog = async (
  model: BackendRecoveryDialogModel,
): Promise<BackendRecoveryChoice> => {
  const options: MessageBoxOptions = {
    type: "error",
    title: model.title,
    message: model.message,
    detail: model.detail,
    buttons: [...model.buttons],
    defaultId: model.defaultId,
    cancelId: model.cancelId,
    noLink: true,
  };
  const mainWindow = getMainWindow();
  const result = mainWindow && !mainWindow.isDestroyed()
    ? await dialog.showMessageBox(mainWindow, options)
    : await dialog.showMessageBox(options);
  return result.response === model.defaultId ? BackendRecoveryChoice.RECONNECT : BackendRecoveryChoice.EXIT;
};

const backendRecoveryCoordinator = createBackendRecoveryCoordinator({
  /** 按已有进程启动流程重新连接后端。 */
  startBackend: async () => {
    await startBackendProcess();
  },
  showDialog: showBackendRecoveryDialog,
  clearSettingsCache: clearChatSettingsCache,
  /**
   * 协调器完成其处理后调用 Electron 的应用退出入口。
   */
  quit: () => app.quit(),
  /**
   * 将应用退出或后端恢复错误记录到前端日志。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   *     error (unknown): 本次捕获的异常。
   */
  logError: (message, error) => appendFrontendLog("error", "backend-recovery", message, error),
});

const applicationShutdownCoordinator = createApplicationShutdownCoordinator({
  /**
   * 应用开始关闭时停止新的后端恢复操作。
   */
  beginBackendRecoveryShutdown: () => backendRecoveryCoordinator.beginShutdown(),
  stopCursorTracking,
  stopBackend,
  forceStopBackend,
  /** 用户明确强制退出时跳过剩余关闭等待。 */
  forceQuit: () => app.exit(),
  closeLogs: closeLogFiles,
  /**
   * 协调器完成其处理后调用 Electron 的应用退出入口。
   */
  quit: () => { finishWindowShutdown(); app.quit(); },
  /**
   * 将应用退出或后端恢复错误记录到前端日志。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   *     error (unknown): 本次捕获的异常。
   */
  logError: (message, error) => appendFrontendLog("error", "shutdown", message, error),
});

onBackendUnexpectedExit(/**
 * 后端成功就绪后意外退出时，把事件交给恢复协调器提示用户选择。
 *
 * Args:
 *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
 */
(event) => {
  backendRecoveryCoordinator.handleUnexpectedExit(event);
});

/**
 * 先关闭界面操作并通知页面，再停止两条桌面订阅，最后由统一协调器释放应用资源。
 */
const requestApplicationShutdown = (): void => {
  beginWindowShutdown();
  stopDesktopSession();
  void applicationShutdownCoordinator.requestShutdown();
};


process.on("SIGINT", requestApplicationShutdown);
process.on("SIGTERM", requestApplicationShutdown);
process.on("uncaughtException", /**
 * 主进程未捕获异常到达时记录原因、设置失败退出码并启动应用资源关闭。
 *
 * Args:
 *     error (Error): 本次捕获的异常。
 */
(error) => {
  appendFrontendLog("error", "process", "Electron 主进程发生未捕获异常", error);
  process.exitCode = 1;
  requestApplicationShutdown();
});
process.on("unhandledRejection", /**
 * 主进程收到未处理 Promise 拒绝时记录原因，便于定位异步失败。
 *
 * Args:
 *     reason (unknown): 当前捕获的 Promise 拒绝原因。
 */
(reason) => {
  appendFrontendLog("error", "process", "Electron 主进程发生未处理 Promise 拒绝", reason);
});

// 注册 live2d:// 协议（必须在 app ready 之前）
registerLive2DProtocol();

// 应用就绪后初始化
app.whenReady().then(/**
 * Electron 就绪后初始化日志、IPC 和窗口，等待后端启动再连接桌面运行事件。
 */
async () => {
  initializeLogFiles(app.getPath("logs"));
  registerLogIpcHandlers({
    policy: getTrustedRendererPolicy(),
    openLogWindow,
    sendBatch: sendLogBatch,
  });
  registerIpcHandlers({ requestApplicationExit: requestApplicationShutdown,
    /** 把等待界面的明确选择交给同一退出协调器。 */
    forceApplicationExit: () => applicationShutdownCoordinator.requestForceShutdown() });
  openLogWindow();
  appendFrontendLog("info", "startup", "日志控制台已启动，正在启动后端");

  try {
    await startBackendProcess();
  } catch (error) {
    if (applicationShutdownCoordinator.isShuttingDown()) return;
    const recent = getRecentLogs("backend", 50)
      .map(/**
       * 启动失败展示最近日志时，将记录整理为带时间和级别的诊断行。
       *
       * Args:
       *     record (LogRecord): 当前需要显示或筛选的日志记录。
       *
       * Returns:
       *     string: 错误提示使用的单行日志。
       */
      (record) => `${record.timestamp} ${record.level.toUpperCase()} ${record.message}`)
      .join("\n");
    const detail = error instanceof Error ? error.message : String(error);
    dialog.showErrorBox(
      "Ayaya 后端启动失败",
      recent ? `${detail}\n\n最近的后端日志：\n${recent}` : detail,
    );
    requestApplicationShutdown();
    return;
  }
  // 1. 用户可能在后端启动等待时确认退出，启动成功也不能再创建窗口或订阅。
  if (applicationShutdownCoordinator.isShuttingDown()) return;
  appendFrontendLog("info", "startup", "后端已就绪");
  startDesktopStream();
  // 创建主窗口
  const mainWindow = createMainWindow();
  mainWindow.once("closed", requestApplicationShutdown);

  // 初始化 Live2D 协议处理器
  initLive2DProtocolHandler();
  initAyayaImageProtocolHandler();

  openChatWindow();

  // 开始光标追踪
  startCursorTracking();

  // 初始化窗口事件监听
  initWindowEventListeners();
});

app.on("before-quit", /**
 * Electron 即将退出但资源未清理完成时阻止默认退出，交给关闭协调器统一停止资源。
 *
 * Args:
 *     event ({ preventDefault: () => void; readonly defaultPrevented: boolean; }): 调用方或浏览器触发的当前事件。
 */
(event) => {
  if (applicationShutdownCoordinator.isComplete()) return;
  event.preventDefault();
  requestApplicationShutdown();
});

// 所有窗口关闭时退出（macOS 除外）
app.on("window-all-closed", /**
 * 全部窗口关闭后按平台决定退出应用或停止光标跟踪。
 */
() => {
  if (process.platform !== "darwin") {
    app.quit();
  } else {
    stopCursorTracking();
  }
});
