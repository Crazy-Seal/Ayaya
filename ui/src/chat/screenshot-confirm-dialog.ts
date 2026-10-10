/**
 * 截屏确认对话框组件
 */

/**
 * 截屏确认对话框管理器
 */
export class ScreenshotConfirmDialog {
  private dialog: HTMLDivElement;
  private messageEl: HTMLDivElement;
  private titleEl: HTMLDivElement;
  private denyBtn: HTMLButtonElement;
  private allowBtn: HTMLButtonElement;
  private pendingResolver: ((approved: boolean) => void) | null = null;

  /**
   * 绑定截图确认对话框和按钮，用户作出选择后结束打开操作的等待。
   */
  constructor() {
    this.dialog = document.getElementById("screenshot-confirm-dialog") as HTMLDivElement;
    this.messageEl = document.getElementById("screenshot-confirm-message") as HTMLDivElement;
    this.titleEl = document.getElementById("screenshot-confirm-title") as HTMLDivElement;
    this.denyBtn = document.getElementById("screenshot-deny-btn") as HTMLButtonElement;
    this.allowBtn = document.getElementById("screenshot-allow-btn") as HTMLButtonElement;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    this.denyBtn.addEventListener("click", /**
     * 用户取消截图确认时结束等待并返回拒绝。
     */
    () => {
      this.close(false);
    });

    this.allowBtn.addEventListener("click", /**
     * 用户确认截图时结束等待并返回允许。
     */
    () => {
      this.close(true);
    });

    // 点击遮罩层关闭（视为拒绝）
    this.dialog.addEventListener("click", /**
     * 用户点击对话框背景时按取消处理，点击内部控件不会误关。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      if (event.target === this.dialog) {
        this.close(false);
      }
    });

    // ESC 键关闭（视为拒绝）
    this.dialog.addEventListener("keydown", /**
     * 用户按 Escape 时取消截图确认并结束等待。
     *
     * Args:
     *     event (KeyboardEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      if (event.key === "Escape") {
        this.close(false);
      }
    });
  }

  /**
   * 关闭对话框
   *
   * Args:
   *     approved (boolean): 用户是否同意本次截图确认。
   */
  private close(approved: boolean): void {
    this.dialog.hidden = true;
    this.dialog.setAttribute("aria-hidden", "true");

    const resolver = this.pendingResolver;
    this.pendingResolver = null;
    if (resolver) {
      resolver(approved);
    }
  }

  /**
   * 打开确认对话框
   *
   * Args:
   *     message (string): 可选的自定义消息
   *     title (string): 对话框或按钮显示的标题。
   *     allowText (string): 确认截图按钮显示的文本。
   *     denyText (string): 拒绝截图按钮显示的文本。
   *
   * Returns:
   *     Promise<boolean>: Promise<boolean> 用户是否允许截屏
   */
  open(
    message?: string,
    title = "截屏请求",
    allowText = "允许",
    denyText = "拒绝"
  ): Promise<boolean> {
    this.titleEl.textContent = title;
    this.allowBtn.textContent = allowText;
    this.denyBtn.textContent = denyText;
    if (message) {
      this.messageEl.textContent = message;
    } else {
      this.messageEl.textContent = "Agent 请求截取屏幕，是否允许？";
    }

    this.dialog.hidden = false;
    this.dialog.setAttribute("aria-hidden", "false");

    // 聚焦到允许按钮
    this.allowBtn.focus();

    return new Promise<boolean>(/**
     * Promise 创建时保存本次用户选择的完成函数，按钮回调随后结束等待。
     *
     * Args:
     *     resolve ((value: boolean | PromiseLike<boolean>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => {
      this.pendingResolver = resolve;
    });
  }

  /**
   * 检查对话框是否打开
   */
  isOpen(): boolean {
    return !this.dialog.hidden;
  }
}
