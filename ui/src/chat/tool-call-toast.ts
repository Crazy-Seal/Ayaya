/**
 * 工具调用提示框组件
 * 在 Live2D 模型右侧显示工具调用提示，支持消息队列效果
 */

export class ToolCallToastManager {
  private container: HTMLDivElement;
  private toasts: HTMLDivElement[] = [];
  private static readonly FADE_OUT_DELAY_MS = 3000;
  private static readonly FADE_OUT_DURATION_MS = 500;
  private static readonly MAX_TOASTS = 5;

  /**
   * 绑定工具调用提示容器，之后按事件展示工具状态和错误。
   */
  constructor() {
    this.container = document.getElementById("tool-call-toasts") as HTMLDivElement;
  }

  /**
   * 显示工具调用提示
   *
   * Args:
   *     toolName (string): 工具名称
   */
  show(toolName: string): void {
    const toast = document.createElement("div");
    toast.className = "tool-call-toast";
    toast.textContent = `调用工具: ${toolName}`;

    // 添加到容器顶部（由于使用 flex-direction: column-reverse，新消息会在下方）
    this.container.appendChild(toast);
    this.toasts.push(toast);

    // 限制最大数量
    if (this.toasts.length > ToolCallToastManager.MAX_TOASTS) {
      const oldestToast = this.toasts.shift();
      if (oldestToast) {
        oldestToast.remove();
      }
    }

    // 设置淡出定时器
    setTimeout(/**
     * 工具调用提示显示期限到达后开始淡出。
     */
    () => {
      this.fadeOut(toast);
    }, ToolCallToastManager.FADE_OUT_DELAY_MS);
  }

  /**
   * 显示错误提示
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  showError(message?: string): void {
    const toast = document.createElement("div");
    toast.className = "tool-call-toast error-toast";
    toast.textContent = message || "出现错误";

    // 添加到容器顶部
    this.container.appendChild(toast);
    this.toasts.push(toast);

    // 限制最大数量
    if (this.toasts.length > ToolCallToastManager.MAX_TOASTS) {
      const oldestToast = this.toasts.shift();
      if (oldestToast) {
        oldestToast.remove();
      }
    }

    // 设置淡出定时器
    setTimeout(/**
     * 工具错误提示显示期限到达后开始淡出。
     */
    () => {
      this.fadeOut(toast);
    }, ToolCallToastManager.FADE_OUT_DELAY_MS);
  }

  /**
   * 淡出并移除提示框
   *
   * Args:
   *     toast (HTMLDivElement): 当前准备淡出的工具提示元素。
   */
  private fadeOut(toast: HTMLDivElement): void {
    toast.classList.add("fade-out");

    // 等待淡出动画完成后移除
    setTimeout(/**
     * 淡出动画结束后移除提示元素和内存记录，释放后续提示的显示空间。
     */
    () => {
      const index = this.toasts.indexOf(toast);
      if (index > -1) {
        this.toasts.splice(index, 1);
      }
      toast.remove();
    }, ToolCallToastManager.FADE_OUT_DURATION_MS);
  }

  /**
   * 清除所有提示框
   */
  clear(): void {
    for (const toast of this.toasts) {
      toast.remove();
    }
    this.toasts = [];
  }
}
