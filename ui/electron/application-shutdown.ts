/** 表示桌面应用关闭流程的执行阶段。 */
const ShutdownPhase = {
  IDLE: "idle",
  STOPPING: "stopping",
  COMPLETE: "complete",
} as const;
type ShutdownPhase = typeof ShutdownPhase[keyof typeof ShutdownPhase];

/**
 * 约定关闭桌面应用时的恢复停止、光标停止、后端退出、日志刷新及最终退出操作。
 */
interface ApplicationShutdownDependencies {
  beginBackendRecoveryShutdown: () => void;
  stopCursorTracking: () => void;
  stopBackend: () => Promise<void>;
  forceStopBackend: () => Promise<void>;
  forceQuit: () => void;
  closeLogs: () => Promise<void>;
  quit: () => void;
  logError: (message: string, error: unknown) => void;
}

/**
 *  Electron 应用关闭协调器。
 */
export interface ApplicationShutdownCoordinator {
  /**
   *  幂等地停止受管资源并退出应用。
   */
  requestShutdown(): Promise<void>;

  /** 用户主动跳过等待，强制结束后端及桌面应用。 */
  requestForceShutdown(): Promise<void>;

  /**
   *  返回应用是否已经进入关闭流程。
   */
  isShuttingDown(): boolean;

  /**
   *  返回受管资源清理是否已经完成。
   */
  isComplete(): boolean;
}

/**
 * 创建应用关闭协调器。
 *
 * Args:
 *     dependencies (ApplicationShutdownDependencies): 恢复状态、光标、后端进程和 Electron 退出依赖。
 *
 * Returns:
 *     ApplicationShutdownCoordinator: 将所有关闭入口合并到同一 Promise 的协调器。
 */
export const createApplicationShutdownCoordinator = (
  dependencies: ApplicationShutdownDependencies,
): ApplicationShutdownCoordinator => {
  let phase: ShutdownPhase = ShutdownPhase.IDLE;
  let shutdownPromise: Promise<void> | null = null;
  let forcePromise: Promise<void> | null = null;
  /** 每次异步等待返回后读取当前退出阶段，识别另一入口已完成退出的情况。 */
  const isComplete = (): boolean => phase === ShutdownPhase.COMPLETE;

  /**
   * 合并重复关闭请求，依次等待后端退出和日志刷新后退出 Electron。
   *
   * Returns:
   *     Promise<void>: 同一次关闭流程的完成结果。
   */
  const requestShutdown = (): Promise<void> => {
    if (shutdownPromise) return shutdownPromise;
    phase = ShutdownPhase.STOPPING;
    dependencies.beginBackendRecoveryShutdown();
    dependencies.stopCursorTracking();
    shutdownPromise = (/**
     * 由首次关闭请求启动，等待后端退出后刷新日志；任一步失败记日志，最后仍完成应用退出。
     */
    async () => {
      try {
        await dependencies.stopBackend();
      } catch (error) {
        dependencies.logError("停止后端进程失败", error);
      }
      if (isComplete()) return;
      try {
        await dependencies.closeLogs();
      } catch (error) {
        dependencies.logError("刷新日志文件失败", error);
      }
      if (isComplete()) return;
      phase = ShutdownPhase.COMPLETE;
      dependencies.quit();
    })();
    return shutdownPromise;
  };

  /** 合并多个窗口的强制退出请求；强制退出完成后，正常关闭的迟到回调结束等待。 */
  const requestForceShutdown = (): Promise<void> => {
    if (forcePromise) return forcePromise;
    forcePromise = (/** 强制结束受管后端，随后直接退出 Electron，跳过日志等剩余等待。 */
    async () => {
      await dependencies.forceStopBackend();
      if (isComplete()) return;
      phase = ShutdownPhase.COMPLETE;
      dependencies.forceQuit();
    })().finally(/** 释放本次请求，终止失败时界面可再次操作。 */
    () => { forcePromise = null; });
    return forcePromise;
  };

  return {
    requestShutdown,
    requestForceShutdown,
    /**
     * 检查应用是否已经进入关闭流程，供退出入口避免重复启动清理。
     *
     * Returns:
     *     boolean: 关闭中或已清理完成时为 true。
     */
    isShuttingDown: () => phase !== ShutdownPhase.IDLE,
    /**
     * 检查受管资源是否已完成清理，供最终退出事件决定是否放行。
     *
     * Returns:
     *     boolean: 关闭阶段为 complete 时为 true。
     */
    isComplete,
  };
};
