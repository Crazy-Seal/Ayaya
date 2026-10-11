/** 应用确认退出后统一阻止新的界面操作，独立于 Electron 以便验证等待阶段。 */
export const SHUTDOWN_STATE_CHANNEL = "desktop-pet:get-shutdown-state";

/** 主进程退出标记与 IPC 操作门禁，退出状态一旦开始便不再恢复。 */
export interface ApplicationOperations {
  /** 进入退出状态，返回是否为第一次请求。 */
  beginShutdown(): boolean;
  /** 返回应用是否已经进入退出流程。 */
  isShuttingDown(): boolean;
  /** 验证退出状态后执行有返回值的可信请求。 */
  invoke<T>(channel: string, operation: () => T): T;
  /** 验证退出状态后执行无响应的可信事件，退出期间静默忽略。 */
  send(channel: string, operation: () => void): void;
}

/**
 * 创建不可撤销的退出门禁，所有业务与日志 IPC 在来源校验后共用此入口。
 * Returns:
 *     ApplicationOperations: 操作状态和执行入口，初始允许正常请求。
 */
export const createApplicationOperations = (): ApplicationOperations => {
  let shuttingDown = false;
  return {
    /**
     * 在停止后端之前同步记录退出状态，重复请求不重复发送窗口通知。
     * Returns:
     *     boolean: 本次是否首次进入退出状态。
     */
    beginShutdown(): boolean {
      if (shuttingDown) return false;
      shuttingDown = true; return true;
    },
    /** 返回是否已确认退出，后端停止失败也不会重新开放操作。 */
    isShuttingDown(): boolean { return shuttingDown; },
    /**
     * 在业务处理器执行前检查退出状态，拒绝新操作而不影响已开始的等待。
     * Args:
     *     channel (string): 可信 IPC 渠道。
     *     operation (() => T): 尚未执行的业务处理器。
     * Returns:
     *     T: 退出前的正常业务响应或页面启动时查询到的退出状态。
     * Raises:
     *     Error: 已退出且请求并非退出状态查询。
     */
    invoke<T>(channel: string, operation: () => T): T {
      // 1. 迟加载的页面必须能查询退出状态并锁定自己，其余请求不能再进入业务逻辑。
      if (shuttingDown && channel !== SHUTDOWN_STATE_CHANNEL) throw new Error("应用正在退出，无法执行新的操作");
      return operation();
    },
    /**
     * 退出前交付无响应事件，退出后忽略按钮、快捷键和重复退出请求。
     * Args:
     *     _channel (string): 可信 IPC 渠道，无响应事件在退出期间全部忽略。
     *     operation (() => void): 尚未执行的业务回调。
     */
    send(_channel: string, operation: () => void): void {
      if (!shuttingDown) operation();
    },
  };
};

/** 所有主进程窗口与 IPC 共享同一退出标记。 */
export const applicationOperations = createApplicationOperations();
