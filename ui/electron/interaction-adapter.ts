/** 桌面交互 Adapter：提交统一命令，将订阅控制器的事件转发给窗口。 */
import { IpcChannel } from "../ipc-channels.js";
import { backendFetch } from "./backend-client.js";
import { getChatWindow, getSettingsWindow } from "./window-manager.js";
import { appendFrontendLog } from "./logging/app-logger.js";
import { DesktopSessionController } from "./desktop-session-controller.js";
import { refreshExternalBackendIdentity, checkExternalBackendExit } from "./backend-process.js";
import type {
  ChannelCommand, CommandReceipt, DesktopSessionSubscriptionOptions, InteractionEvent,
} from "../shared-types.js";

/**
 * 同步转发当前连接事件，聊天窗口和设置窗口共用一个订阅。
 *
 * Args:
 *     event (InteractionEvent): 控制器已经核对订阅代次的事件。
 */
const deliverDesktopEvent = (event: InteractionEvent): void => {
  for (const window of [getChatWindow(), getSettingsWindow()]) {
    if (window && !window.isDestroyed()) window.webContents.send(IpcChannel.INTERACTION_EVENT, event);
  }
};

/**
 * 将订阅断线和恢复记录到统一应用日志。
 *
 * Args:
 *     level ("info" | "warn"): 恢复或断线级别。
 *     message (string): 中文连接状态说明。
 *     error (unknown): 可选的底层错误。
 */
const logDesktopStream = (level: "info" | "warn", message: string, error?: unknown): void => {
  appendFrontendLog(level, "interaction-stream", message, error);
};

const desktopSession = new DesktopSessionController({
  fetchBackend: backendFetch,
  deliverEvent: deliverDesktopEvent,
  log: logDesktopStream,
  /** 每次连接前刷新外部后端进程身份，迟到请求受订阅取消信号约束。 */
  onConnected: (signal) => refreshExternalBackendIdentity(signal),
  /**
   * 断线时检查已连接的外部后端进程，并通知聊天页结束本连接的提示显示。
   *
   * Args:
   *     sessionId (string): 已由订阅控制器核对归属的会话编号。
   */
  onDisconnected: (sessionId) => {
    checkExternalBackendExit();
    const chatWindow = getChatWindow();
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send(IpcChannel.INTERACTION_DISCONNECTED, sessionId);
    }
  },
});

/**
 * 提交统一命令并返回受理结果，Agent 回复通过独立订阅送达。
 *
 * Args:
 *     command (ChannelCommand): 带桌面来源的入站命令。
 *
 * Returns:
 *     Promise<CommandReceipt>: 后端命令受理回执。
 */
export const submitInteractionCommand = async (command: ChannelCommand): Promise<CommandReceipt> => {
  const response = await backendFetch("/agent/commands", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(command),
  });
  if (!response.ok) throw new Error(`提交交互命令失败: ${response.status}`);
  return await response.json() as CommandReceipt;
};

/**
 * 建立或复用桌面会话订阅，新页面要求刷新时等待新的快照。
 *
 * Args:
 *     sessionId (string): 共享 Ayaya 会话标识。
 *     options (DesktopSessionSubscriptionOptions): 新页面的刷新要求，普通发送可省略。
 *
 * Returns:
 *     Promise<void>: 当前连接首次快照已转发，或旧连接被取消。
 */
export const subscribeDesktopSession = (
  sessionId: string, options?: DesktopSessionSubscriptionOptions,
): Promise<void> => desktopSession.subscribe(sessionId, options);

/**
 *  停止当前展示连接，释放快照和重试等待，不停止 Agent。
 */
export const stopDesktopSession = (): void => desktopSession.stop();
