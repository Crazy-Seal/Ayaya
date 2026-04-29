/**
 * 确认对话框组件
 */

/**
 * 确认对话框管理器
 */
export class ConfirmDialog {
  private dialog: HTMLDivElement;
  private cancelBtn: HTMLButtonElement;
  private okBtn: HTMLButtonElement;
  private pendingResolver: ((confirmed: boolean) => void) | null = null;

  constructor(dialog: HTMLDivElement, cancelBtn: HTMLButtonElement, okBtn: HTMLButtonElement) {
    this.dialog = dialog;
    this.cancelBtn = cancelBtn;
    this.okBtn = okBtn;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    this.cancelBtn.addEventListener("click", () => {
      this.close(false);
    });

    this.okBtn.addEventListener("click", () => {
      this.close(true);
    });

    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog) {
        this.close(false);
      }
    });
  }

  /**
   * 关闭对话框
   */
  private close(confirmed: boolean): void {
    this.dialog.hidden = true;
    this.dialog.setAttribute("aria-hidden", "true");

    const resolver = this.pendingResolver;
    this.pendingResolver = null;
    if (resolver) {
      resolver(confirmed);
    }
  }

  /**
   * 打开确认对话框
   */
  open(): Promise<boolean> {
    this.dialog.hidden = false;
    this.dialog.setAttribute("aria-hidden", "false");
    return new Promise<boolean>((resolve) => {
      this.pendingResolver = resolve;
    });
  }
}
