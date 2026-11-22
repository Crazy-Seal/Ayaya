/** 桌面交互 Adapter：提交统一命令，将订阅控制器的事件转发给窗口。 */
import { IpcChannel } from "../ipc-channels.js";
import { backendFetch } from "./backend-client.js";
import { getChatWindow, getSettingsWindow, beginChatView, invalidateChatView, receiveChatEvent } from "./window-manager.js";
import { appendFrontendLog } from "./logging/app-logger.js";
import { DesktopSessionController } from "./desktop-session-controller.js";
import { refreshExternalBackendIdentity, checkExternalBackendExit } from "./backend-process.js";
import type {
  ChannelCommand, CommandReceipt, InteractionEvent,
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
  observeEvent: receiveChatEvent,
  log: logDesktopStream,
  /** 每次连接前刷新外部后端进程身份，迟到请求受订阅取消信号约束。 */
  onConnected: (signal) => refreshExternalBackendIdentity(signal),
  /**
   * 断线时检查已连接的外部后端进程，并通知聊天页结束本连接的提示显示。
   */
  onDisconnected: () => {
    invalidateChatView();
    checkExternalBackendExit();
    const chatWindow = getChatWindow();
    if (chatWindow && !chatWindow.isDestroyed()) {
      chatWindow.webContents.send(IpcChannel.INTERACTION_DISCONNECTED);
    }
  },
  /** 自动重连的快照失败送达当前聊天页面。 */
  onSnapshotError: (sessionId, error) => {
    getChatWindow()?.webContents.send(IpcChannel.INTERACTION_SNAPSHOT_FAILED, { sessionId, detail: String(error) });
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

/** 后端就绪后启动应用拥有的唯一桌面事件流。 */
export const startDesktopStream = (): void => desktopSession.start();

/** 等待后端登记全局订阅，供用户提交命令前复用。
 * Returns:
 *     Promise<void>: 当前全局连接已就绪。
 */
export const ensureDesktopStream = (): Promise<void> => desktopSession.ensureReady();

/** 为当前页面查询快照并衔接查询期间的会话事件。
 * Args:
 *     sessionId (string): 当前页面选择展示的主会话。
 * Returns:
 *     Promise<void>: 快照和缓冲事件已经转发，或页面已经切换。
 */
export const initializeDesktopSession = (sessionId: string): Promise<void> => {
  beginChatView(sessionId);
  return desktopSession.initializeSession(sessionId);
};

/** 应用退出时释放全局连接和当前快照查询。 */
export const stopDesktopSession = (): void => desktopSession.stop();
