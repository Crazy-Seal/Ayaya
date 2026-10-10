import type { ApprovalDecision } from "../../shared-types.js";
import { ApprovalDecisionKind } from "../../shared-types.js";

export type ApprovalPrompt = {
  title: string;
  message: string;
  details?: string;
  allowSteer?: boolean;
};

/**
 * 管理当前模态审批的展示和本地决定，跨端处理通过 dismiss 关闭过期视图。
 */
export class ToolApprovalDialog {
  private readonly dialog = document.getElementById("screenshot-confirm-dialog") as HTMLDivElement;
  private readonly messageEl = document.getElementById("screenshot-confirm-message") as HTMLDivElement;
  private readonly titleEl = document.getElementById("screenshot-confirm-title") as HTMLDivElement;
  private readonly denyBtn = document.getElementById("screenshot-deny-btn") as HTMLButtonElement;
  private readonly allowBtn = document.getElementById("screenshot-allow-btn") as HTMLButtonElement;
  private readonly steerBtn = document.getElementById("tool-steer-btn") as HTMLButtonElement;
  private readonly steerInput = document.getElementById("tool-steer-input") as HTMLTextAreaElement;
  private pendingResolver: ((decision: ApprovalDecision) => void) | null = null;

  /** 绑定批准、拒绝和引导入口，所有决定共用同一具名协议取值。 */
  constructor() {
    // 1. 按钮只产生本地决定，实际审批的先到先得仍由后端仲裁。
    this.denyBtn.addEventListener("click", /** 提交拒绝决定并关闭当前视图。 */
    () => this.close({ kind: ApprovalDecisionKind.REJECT }));
    this.allowBtn.addEventListener("click", /** 提交批准决定并关闭当前视图。 */
    () => this.close({ kind: ApprovalDecisionKind.APPROVE }));
    this.steerBtn.addEventListener("click", /** 打开引导输入或提交非空引导。 */
    () => this.submitSteer());
    // 2. 保留 Enter 提交、Shift+Enter 换行，以及遮罩和 Escape 拒绝的既有行为。
    this.steerInput.addEventListener("keydown", /**
     * 处理引导框的提交快捷键。
     *
     * Args:
     *     event (KeyboardEvent): 当前键盘事件。
     */
    (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.submitSteer();
      }
    });
    this.dialog.addEventListener("click", /**
     * 点击遮罩时拒绝，模态内容中的点击不影响决定。
     *
     * Args:
     *     event (MouseEvent): 当前点击事件。
     */
    (event) => {
      if (event.target === this.dialog) this.close({ kind: ApprovalDecisionKind.REJECT });
    });
    this.dialog.addEventListener("keydown", /**
     * Escape 拒绝当前请求，不触发其他页面操作。
     *
     * Args:
     *     event (KeyboardEvent): 当前键盘事件。
     */
    (event) => {
      if (event.key === "Escape") this.close({ kind: ApprovalDecisionKind.REJECT });
    });
  }

  /** 展示引导输入框，已经展示时只提交有效文本。 */
  private submitSteer(): void {
    if (this.steerInput.hidden) {
      this.steerInput.hidden = false;
      this.steerBtn.textContent = "发送引导";
      this.steerInput.focus();
      return;
    }
    const message = this.steerInput.value.trim();
    if (message) this.close({ kind: ApprovalDecisionKind.STEER, message });
  }

  /**
   * 关闭视图并只完成当前等待决定的 Promise。
   *
   * Args:
   *     decision (ApprovalDecision): 批准、拒绝或引导决定。
   */
  private close(decision: ApprovalDecision): void {
    this.dialog.hidden = true;
    this.dialog.setAttribute("aria-hidden", "true");
    const resolver = this.pendingResolver;
    this.pendingResolver = null;
    resolver?.(decision);
  }

  /**
   * 展示审批正文、参数和可选引导，等待用户或跨端关闭。
   *
   * Args:
   *     prompt (ApprovalPrompt | string): 审批文案和展示选项。
   *     title (string): 简单字符串提示的默认标题。
   *
   * Returns:
   *     Promise<ApprovalDecision>: 本地用户决定；调用方仍须核对请求是否有效。
   */
  open(
    prompt: ApprovalPrompt | string,
    title = "工具调用确认",
  ): Promise<ApprovalDecision> {
    const normalized = typeof prompt === "string" ? { title, message: prompt } : prompt;
    this.titleEl.textContent = normalized.title;
    this.messageEl.textContent = normalized.details
      ? `${normalized.message}\n\n${normalized.details}`
      : normalized.message;
    this.steerInput.value = "";
    this.steerInput.hidden = true;
    this.steerBtn.hidden = normalized.allowSteer !== true;
    this.steerBtn.textContent = "引导";
    this.allowBtn.textContent = "批准";
    this.denyBtn.textContent = "拒绝";
    this.dialog.hidden = false;
    this.dialog.setAttribute("aria-hidden", "false");
    this.allowBtn.focus();
    return new Promise(/**
     * 登记当前审批的完成函数，不在 Renderer 执行工具。
     *
     * Args:
     *     resolve ((decision: ApprovalDecision) => void): 当前等待者的完成函数。
     */
    (resolve) => { this.pendingResolver = resolve; });
  }

  /**
   * 查询当前模态审批的可见状态。
   *
   * Returns:
   *     boolean: 当前视图是否可见。
   */
  isOpen(): boolean {
    return !this.dialog.hidden;
  }

  /**
   * 关闭已由其他入口处理的审批视图，调用方核对 request_id 后不再提交决定。
   */
  dismiss(): void {
    this.close({ kind: ApprovalDecisionKind.REJECT });
  }
}
