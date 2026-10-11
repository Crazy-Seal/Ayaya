/**
 * 为模态框提供焦点约束、Escape 取消与关闭后焦点恢复。
 *
 * Args:
 *     overlay (HTMLElement): 包含 role=dialog 内容的遮罩。
 *     cancel (() => void): 用户按 Escape 时执行的既有取消操作。
 * Returns:
 *     { activate: () => void; deactivate: () => void }: 显示和隐藏时调用的焦点管理入口。
 */
export const createDialogFocus = (overlay: HTMLElement, cancel: () => void) => {
  let previous: HTMLElement | null = null;
  /**
   * 显示模态框后处理键盘，Tab 只能在当前可用控件之间移动。
   *
   * Args:
   *     event (KeyboardEvent): 当前模态框收到的键盘事件。
   */
  const onKey = (event: KeyboardEvent): void => {
    if (overlay.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopImmediatePropagation();
      cancel();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(overlay.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]',
    )).filter(/**
     * 排除 hidden 属性或隐藏父容器中的控件，避免焦点进入不可见输入框。
     * Args:
     *     element (HTMLElement): 候选焦点控件。
     * Returns:
     *     boolean: 控件实际显示时为 true。
     */
    (element) => !element.closest("[hidden]") && element.getClientRects().length > 0);
    if (!controls.length) { event.preventDefault(); return; }
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || !overlay.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !overlay.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  };
  return {
    /** 显示对话框前记住调用控件，并安装键盘焦点约束。 */
    activate(): void {
      previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      overlay.addEventListener("keydown", onKey, true);
    },
    /** 隐藏后移除约束，调用控件仍可用时恢复焦点。 */
    deactivate(): void {
      overlay.removeEventListener("keydown", onKey, true);
      if (previous?.isConnected && !previous.closest("[hidden]")) previous.focus();
      previous = null;
    },
  };
};
