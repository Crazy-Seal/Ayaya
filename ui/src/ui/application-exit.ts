import type { ConfirmDialog } from "./confirm-dialog.js";
import { lockApplicationShutdownUi } from "./application-shutdown.js";
import { DIALOG_PRIORITY } from "./dialog-focus.js";
import { EXIT_TASK_QUERY_TIMEOUT_MS } from "../../runtime-constants.js";

/**
 * 退出前有限等待任务数量，查询失败或超时后让用户人工确认任务情况。
 *
 * Returns:
 *     Promise<number | null>: 全应用活动任务数量，查询失败或超时时为空。
 */
const readActiveTaskCount = async (): Promise<number | null> => {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    // 1. 页面等待包含 IPC 和后端查询，超过预算后立即使用状态未知的确认文案。
    const summary = await Promise.race([
      window.desktopPetApi.getActiveTaskSummary(),
      new Promise<null>(/**
       * 安排查询等待上限，到期后返回状态未知。
       * Args:
       *     resolve ((value: null) => void): 结束本次页面等待的回调。
       */
      (resolve) => {
        timer = setTimeout(/** 查询超过等待预算，转入人工确认。 */
        () => resolve(null), EXIT_TASK_QUERY_TIMEOUT_MS);
      }),
    ]);
    return summary?.active_count ?? null;
  } catch {
    // 2. 后端查询报错时同样提示人工确认，让用户继续选择是否退出。
    return null;
  } finally {
    // 3. 查询提前完成或失败时释放计时器；迟到结果由本次等待忽略。
    clearTimeout(timer);
  }
};

/**
 * 聊天和设置页共用确认退出流程，用户确认后交给主进程停止对话、任务并关闭应用。
 *
 * Args:
 *     dialog (ConfirmDialog): 当前窗口的确认对话框，负责取消、确认和键盘焦点恢复。
 *
 * Returns:
 *     Promise<void>: 取消不修改应用状态；确认后等待主进程受理，未受理时显示失败原因。
 */
export const confirmApplicationExit = async (dialog: Pick<ConfirmDialog, "open">): Promise<void> => {
  try {
    // 1. 只读取全应用任务状态，用户取消之前保持对话和任务原样运行。
    const activeCount = await readActiveTaskCount();
    const message = activeCount === null
      ? "当前后台任务状态查询出错，退出前请人工确认后台任务情况。"
      : activeCount > 0
        ? "当前有正在进行的后台任务，确认要退出吗？"
        : "退出会停止当前对话和后台任务，是否继续？";
    const confirmed = await dialog.open({
      title: "退出 Ayaya",
      message,
      confirmText: "退出",
      cancelText: "取消",
      variant: activeCount === 0 ? "danger" : "warning",
      priority: DIALOG_PRIORITY.APPLICATION_EXIT,
    });
    if (!confirmed) return;
    // 2. 用户确认后才向主进程请求退出，继续沿用现有清理和等待时间。
    await window.desktopPetApi.requestApplicationExit();
    lockApplicationShutdownUi();
  } catch (error) {
    // 3. 另一窗口可能已经先发起退出；此时维持锁定，不把重复请求误报为退出失败。
    let started = false;
    try { started = await window.desktopPetApi.getApplicationShutdownState(); }
    catch { /* 主进程不可达时没有受理依据，继续显示本次请求失败。 */ }
    if (started) { lockApplicationShutdownUi(); return; }
    // 4. 未受理的请求不能让当前窗口永久停在“正在退出”，保留可操作界面并给出明确原因。
    await dialog.open({
      title: "退出请求失败",
      message: error instanceof Error ? error.message : String(error),
      confirmText: "知道了",
      cancelText: "返回",
      variant: "danger",
      priority: DIALOG_PRIORITY.APPLICATION_EXIT,
    });
  }
};
