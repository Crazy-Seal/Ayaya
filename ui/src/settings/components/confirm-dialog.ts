/**
 * 确认对话框组件
 */

/**
 * 确认对话框管理器
 */
export type ConfirmDialogVariant = "warning" | "danger";

export type ConfirmDialogOptions = {
  title: string;
  message: string;
  confirmText: string;
  cancelText: string;
  variant: ConfirmDialogVariant;
};

const DELETE_MODEL_DEFAULTS: ConfirmDialogOptions = {
  title: "删除模型确认",
  message: "将删除该模型所有的配置文件和会话记忆，请谨慎操作",
  confirmText: "确认删除",
  cancelText: "取消",
  variant: "danger",
};

/**
 * 管理设置页确认提示，每次打开返回 Promise，用户确认或取消后结束等待。
 */
export class ConfirmDialog {
  private dialog: HTMLDivElement;
  private cancelBtn: HTMLButtonElement;
  private okBtn: HTMLButtonElement;
  private title: HTMLElement;
  private message: HTMLElement;
  private pendingResolver: ((confirmed: boolean) => void) | null = null;

  /**
   * 绑定对话框元素及确认、取消按钮，初始化选择回调。
   *
   * Args:
   *     dialog (HTMLDivElement): 确认对话框的根元素。
   *     cancelBtn (HTMLButtonElement): 取消并结束等待的按钮。
   *     okBtn (HTMLButtonElement): 确认并结束对话框等待的按钮。
   *     title (HTMLElement): 需要展示的提示标题。
   *     message (HTMLElement): 当前需要显示或记录的文本。
   */
  constructor(
    dialog: HTMLDivElement,
    cancelBtn: HTMLButtonElement,
    okBtn: HTMLButtonElement,
    title: HTMLElement,
    message: HTMLElement
  ) {
    this.dialog = dialog;
    this.cancelBtn = cancelBtn;
    this.okBtn = okBtn;
    this.title = title;
    this.message = message;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    this.cancelBtn.addEventListener("click", /**
     * 用户点击取消时结束确认等待并返回 false。
     */
    () => {
      this.close(false);
    });

    this.okBtn.addEventListener("click", /**
     * 用户点击确认时结束确认等待并返回 true。
     */
    () => {
      this.close(true);
    });

    this.dialog.addEventListener("click", /**
     * 用户点击确认框背景时取消操作，点击内部内容不会关闭。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      if (event.target === this.dialog) {
        this.close(false);
      }
    });
  }

  /**
   * 关闭对话框
   *
   * Args:
   *     confirmed (boolean): 用户是否确认本次设置操作。
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
   *
   * Args:
   *     options (ConfirmDialogOptions): 调用方提供的配置或运行选项。
   */
  open(options: ConfirmDialogOptions = DELETE_MODEL_DEFAULTS): Promise<boolean> {
    this.title.textContent = options.title;
    this.message.textContent = options.message;
    this.cancelBtn.textContent = options.cancelText;
    this.okBtn.textContent = options.confirmText;
    this.okBtn.classList.remove(
      "confirm-btn-danger",
      "confirm-btn-warning",
      "ui-button--danger",
      "ui-button--warning",
    );
    this.okBtn.classList.add(`confirm-btn-${options.variant}`);
    this.okBtn.classList.add(`ui-button--${options.variant}`);
    this.dialog.hidden = false;
    this.dialog.setAttribute("aria-hidden", "false");
    return new Promise<boolean>(/**
     * Promise 创建时保存当前确认操作的完成函数，由用户选择结束等待。
     *
     * Args:
     *     resolve ((value: boolean | PromiseLike<boolean>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => {
      this.pendingResolver = resolve;
    });
  }
}
