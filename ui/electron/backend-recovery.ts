import type { BackendUnexpectedExit } from "./backend-process.js";

/** 后端恢复对话框允许的用户选择。 */
export type BackendRecoveryChoice = "reconnect" | "exit";

/**
 *  与 Electron API 解耦的后端恢复对话框模型。
 */
export interface BackendRecoveryDialogModel {
  title: string;
  message: string;
  detail: string;
  buttons: [string, string];
  defaultId: number;
  cancelId: number;
}

/**
 * 约定重新连接后端、显示恢复选择、清理缓存和退出应用的操作。
 */
interface BackendRecoveryDependencies {
  startBackend: () => Promise<void>;
  showDialog: (model: BackendRecoveryDialogModel) => Promise<BackendRecoveryChoice>;
  clearSettingsCache: () => void;
  quit: () => void;
  logError: (message: string, error: unknown) => void;
}

/**
 *  后端异常退出后的交互恢复协调器。
 */
export interface BackendRecoveryCoordinator {
  /**
   *  串行处理一个成功就绪后的非预期退出事件。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  handleUnexpectedExit(event: BackendUnexpectedExit): void;

  /**
   *  标记应用已进入退出流程，忽略尚未完成的恢复操作。
   */
  beginShutdown(): void;
}

type RecoveryPhase = "idle" | "recovering" | "quitting";

const commonDialogFields = {
  buttons: ["重连", "退出"] as [string, string],
  defaultId: 0,
  cancelId: 1,
};

/**
 * 根据意外退出事件构造首次恢复提示。
 *
 * Args:
 *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
 *
 * Returns:
 *     BackendRecoveryDialogModel: 包含重连和退出选择的对话框数据。
 */
const createUnexpectedExitDialog = (
  event: BackendUnexpectedExit,
): BackendRecoveryDialogModel => ({
  title: "Ayaya 后端意外退出",
  message: "后端进程已停止，聊天、设置和 MCP 功能暂时不可用。",
  detail: event.message,
  ...commonDialogFields,
});

/**
 * 根据重连错误构造再次尝试或退出的提示。
 *
 * Args:
 *     error (unknown): 本次捕获的异常。
 *
 * Returns:
 *     BackendRecoveryDialogModel: 可供 Electron 展示的恢复失败对话框数据。
 */
const createReconnectFailureDialog = (error: unknown): BackendRecoveryDialogModel => ({
  title: "Ayaya 后端重连失败",
  message: "无法重新启动后端。",
  detail: error instanceof Error ? error.message : String(error),
  ...commonDialogFields,
});

/**
 * 创建后端异常退出恢复协调器。
 *
 * Args:
 *     dependencies (BackendRecoveryDependencies): 后端启动、对话框、缓存和应用退出依赖。
 *
 * Returns:
 *     BackendRecoveryCoordinator: 串行管理重连或退出选择的恢复协调器。
 */
export const createBackendRecoveryCoordinator = (
  dependencies: BackendRecoveryDependencies,
): BackendRecoveryCoordinator => {
  let phase: RecoveryPhase = "idle";
  let pendingExit: BackendUnexpectedExit | null = null;
  /**
   * 检查应用是否已停止接受恢复动作。
   *
   * Returns:
   *     boolean: 关闭标记已设置时为 true。
   */
  const isQuitting = (): boolean => phase === "quitting";

  /**
   * 等待用户选择，重连成功后清除旧配置缓存，失败则继续提示或按选择退出。
   *
   * Args:
   *     initialEvent (BackendUnexpectedExit): 本次恢复流程收到的首次后端退出事件。
   *
   * Returns:
   *     Promise<void>: 重连成功、用户退出或关闭流程取消恢复后完成。
   */
  const runRecovery = async (initialEvent: BackendUnexpectedExit): Promise<void> => {
    let dialogModel = createUnexpectedExitDialog(initialEvent);
    while (phase === "recovering") {
      const choice = await dependencies.showDialog(dialogModel);
      if (isQuitting()) return;
      if (choice === "exit") {
        phase = "quitting";
        dependencies.quit();
        return;
      }

      try {
        await dependencies.startBackend();
        if (isQuitting()) return;
        dependencies.clearSettingsCache();
      } catch (error) {
        if (isQuitting()) return;
        dependencies.logError("后端重连失败", error);
        dialogModel = createReconnectFailureDialog(error);
        continue;
      }

      if (pendingExit) {
        const nextExit = pendingExit;
        pendingExit = null;
        dialogModel = createUnexpectedExitDialog(nextExit);
        continue;
      }
      phase = "idle";
    }
  };

  /**
   * 收到后端异常退出通知时合并已有恢复流程，避免同时显示多个恢复对话框。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   *
   * Returns:
   *     void: 当前恢复流程的完成结果。
   */
  const handleUnexpectedExit = (event: BackendUnexpectedExit): void => {
    if (phase === "quitting") return;
    if (phase === "recovering") {
      pendingExit = event;
      return;
    }
    phase = "recovering";
    void runRecovery(event).catch(/**
     * 恢复流程意外失败后记录错误，并在尚未退出时将状态切为退出，避免继续弹出恢复提示。
     *
     * Args:
     *     error (any): 本次捕获的异常。
     */
    (error) => {
      dependencies.logError("处理后端异常退出失败", error);
      if (phase !== "quitting") {
        phase = "quitting";
        dependencies.quit();
      }
    });
  };

  /**
   * 关闭应用时禁止启动新的恢复，并让已有恢复在检查关闭状态时结束。
   */
  const beginShutdown = (): void => {
    phase = "quitting";
    pendingExit = null;
  };

  return { handleUnexpectedExit, beginShutdown };
};
