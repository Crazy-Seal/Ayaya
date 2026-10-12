import type { ConfirmDialog } from "./confirm-dialog.js";
import { lockApplicationShutdownUi } from "./application-shutdown.js";
import { DIALOG_PRIORITY } from "./dialog-focus.js";

/**
 * 聊天和设置页共用确认退出流程，用户确认后交给主进程停止对话、任务并关闭应用。
 *
 * Args:
 *     dialog (ConfirmDialog): 当前窗口的确认对话框，负责取消、确认和键盘焦点恢复。
 *
 * Returns:
 *     Promise<void>: 取消不修改应用状态；确认后等待主进程受理，未受理时显示失败原因。
 */
export const confirmApplicationExit = async (dialog: ConfirmDialog): Promise<void> => {
  // 1. 先等待用户选择，取消、Escape 或点击遮罩都不停止当前对话和后台任务。
  const confirmed = await dialog.open({
    title: "退出 Ayaya",
    message: "退出会停止当前对话和后台任务，是否继续？",
    confirmText: "退出",
    cancelText: "取消",
    variant: "danger",
    priority: DIALOG_PRIORITY.APPLICATION_EXIT,
  });
  if (!confirmed) return;

  // 2. 全局退出状态以主进程受理为准。它先锁定所有窗口、广播通知，再开始资源清理。
  try {
    const request = window.desktopPetApi.requestApplicationExit();
    if (typeof request?.then !== "function") throw new Error("退出接口尚未更新，请重新启动桌面开发环境。");
    await request;
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
