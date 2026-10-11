/** 将登记控制器接到 Electron 的准确窗口引用和本机后端，页面不访问此模块。 */
import { backendFetch } from "./backend-client.js";
import { applicationOperations } from "./application-operations.js";
import { appendFrontendLog } from "./logging/app-logger.js";
import { DesktopWindowRegistrationController, type RegisteredDesktopWindow } from "./desktop-window-registration.js";

let windows: { pet: RegisteredDesktopWindow | null; chat: RegisteredDesktopWindow | null } = { pet: null, chat: null };
let backendExitListener: (() => void) | null = null;

/**
 * 应用装配时接入原有恢复对话框，不另建进程管理链。
 * Args:
 *     listener (() => void): 已确认本机后端退出、匹配窗口已尝试恢复后的回调。
 */
export const setDesktopBackendExitListener = (listener: () => void): void => { backendExitListener = listener; };

/** 共享身份登记对象仅由主进程使用，不包含工具动作或结果。 */
export const desktopWindowRegistry = new DesktopWindowRegistrationController({
  ownerPid: process.pid,
  /** 返回现存两个角色的原生窗口引用。 */ getWindows: () => windows,
  /** 沿用现有不可撤销的退出门禁。 */ isShuttingDown: () => applicationOperations.isShuttingDown(),
  fetchBackend: backendFetch,
  /** 恢复已经尝试完成后通知应用现有恢复协调器。 */
  onBackendExited: () => backendExitListener?.(),
  /**
   * 恢复或登记错误写入现有主进程日志。
   * Args:
   *     message (string): 当前登记或窗口恢复失败的中文说明。
   *     error (unknown): 捕获的 HTTP、文件或原生窗口异常。
   */
  logError: (message, error) => appendFrontendLog("error", "desktop-windows", message, error),
});

/**
 * 窗口创建或销毁后更新精确引用，正常退出期间不重新登记。
 * Args:
 *     next (typeof windows): 当前主进程已经更新的两个窗口引用。
 */
export const updateDesktopWindowReferences = (next: typeof windows): void => {
  windows = next;
  if (applicationOperations.isShuttingDown()) return;
  void desktopWindowRegistry.refresh().catch(/** 登记失败保持命令入口关闭，恢复连接后重新发送。 */
  (error) => appendFrontendLog("warn", "desktop-windows", "更新窗口登记失败", error));
};
