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

  constructor() {
    this.container = document.getElementById("tool-call-toasts") as HTMLDivElement;
  }

  /**
   * 显示工具调用提示
   * @param toolName 工具名称
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
    setTimeout(() => {
      this.fadeOut(toast);
    }, ToolCallToastManager.FADE_OUT_DELAY_MS);
  }

  /**
   * 显示错误提示
   */
  showError(): void {
    const toast = document.createElement("div");
    toast.className = "tool-call-toast error-toast";
    toast.textContent = "出现错误";

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
    setTimeout(() => {
      this.fadeOut(toast);
    }, ToolCallToastManager.FADE_OUT_DELAY_MS);
  }

  /**
   * 淡出并移除提示框
   */
  private fadeOut(toast: HTMLDivElement): void {
    toast.classList.add("fade-out");

    // 等待淡出动画完成后移除
    setTimeout(() => {
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
