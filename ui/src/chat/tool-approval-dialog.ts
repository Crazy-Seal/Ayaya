import type { ApprovalDecision } from "../../shared-types.js";

export type ApprovalPrompt = {
  title: string;
  message: string;
  details?: string;
  allowSteer?: boolean;
};

/** 统一工具审批框：批准、拒绝或用一条新消息引导 Agent。 */
export class ToolApprovalDialog {
  private readonly dialog = document.getElementById("screenshot-confirm-dialog") as HTMLDivElement;
  private readonly messageEl = document.getElementById("screenshot-confirm-message") as HTMLDivElement;
  private readonly titleEl = document.getElementById("screenshot-confirm-title") as HTMLDivElement;
  private readonly denyBtn = document.getElementById("screenshot-deny-btn") as HTMLButtonElement;
  private readonly allowBtn = document.getElementById("screenshot-allow-btn") as HTMLButtonElement;
  private readonly steerBtn = document.getElementById("tool-steer-btn") as HTMLButtonElement;
  private readonly steerInput = document.getElementById("tool-steer-input") as HTMLTextAreaElement;
  private pendingResolver: ((decision: ApprovalDecision) => void) | null = null;

  constructor() {
    this.denyBtn.addEventListener("click", () => this.close({ kind: "reject" }));
    this.allowBtn.addEventListener("click", () => this.close({ kind: "approve" }));
    this.steerBtn.addEventListener("click", () => this.submitSteer());
    this.steerInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.submitSteer();
      }
    });
    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog) this.close({ kind: "reject" });
    });
    this.dialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") this.close({ kind: "reject" });
    });
  }

  private submitSteer(): void {
    if (this.steerInput.hidden) {
      this.steerInput.hidden = false;
      this.steerBtn.textContent = "发送引导";
      this.steerInput.focus();
      return;
    }
    const message = this.steerInput.value.trim();
    if (message) this.close({ kind: "steer", message });
  }

  private close(decision: ApprovalDecision): void {
    this.dialog.hidden = true;
    this.dialog.setAttribute("aria-hidden", "true");
    const resolver = this.pendingResolver;
    this.pendingResolver = null;
    resolver?.(decision);
  }

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
    return new Promise((resolve) => { this.pendingResolver = resolve; });
  }

  isOpen(): boolean {
    return !this.dialog.hidden;
  }
}
