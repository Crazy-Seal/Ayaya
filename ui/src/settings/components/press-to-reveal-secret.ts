/**
 * 绑定“按住显示，松开隐藏”的密钥查看交互。
 *
 * Args:
 *     input (HTMLInputElement): 当前日志输入或配置输入框。
 *     trigger (HTMLButtonElement): 按住时临时显示敏感值的按钮。
 *     globalTarget (EventTarget): 接收松开、取消等跨控件事件的目标。
 */
export const bindPressToRevealSecret = (
  input: HTMLInputElement,
  trigger: HTMLButtonElement,
  globalTarget: EventTarget = window
): (() => void) => {
  /**
   * 用户松开、失去焦点或取消按压时隐藏凭据并更新按钮状态。
   */
  const conceal = (): void => {
    input.type = "password";
  };

  /**
   * 用户按住显示按钮时临时显示凭据，结束按压后由隐藏回调恢复。
   *
   * Args:
   *     event (Event): 调用方或浏览器触发的当前事件。
   */
  const reveal = (event: Event): void => {
    if ((event as PointerEvent).button !== 0) {
      return;
    }
    input.type = "text";
  };

  trigger.addEventListener("pointerdown", reveal);
  trigger.addEventListener("pointerup", conceal);
  trigger.addEventListener("pointerleave", conceal);
  trigger.addEventListener("pointercancel", conceal);
  trigger.addEventListener("blur", conceal);
  globalTarget.addEventListener("pointerup", conceal);
  globalTarget.addEventListener("blur", conceal);

  /**
   * 组件清理时先隐藏凭据，再移除按压、取消和失焦监听，避免页面切换后保留显示状态。
   */
  return () => {
    conceal();
    trigger.removeEventListener("pointerdown", reveal);
    trigger.removeEventListener("pointerup", conceal);
    trigger.removeEventListener("pointerleave", conceal);
    trigger.removeEventListener("pointercancel", conceal);
    trigger.removeEventListener("blur", conceal);
    globalTarget.removeEventListener("pointerup", conceal);
    globalTarget.removeEventListener("blur", conceal);
  };
};
