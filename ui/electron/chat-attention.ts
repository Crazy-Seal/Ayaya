import { InteractionEventKind, InterruptKind } from "../shared-types.js";
import type { ChatAttentionUpdate, InteractionEvent } from "../shared-types.js";

/** 保存一个会话尚未查看的正文和按请求区分的审批。 */
interface SessionAttention {
  unread: boolean;
  approvals: Map<string, string | null>;
}

/**
 * 维护所有会话的提醒，窗口根据汇总结果显示标题和闪烁。
 *
 * Attributes:
 *     sessions (Map<string, SessionAttention>): 各会话在当前应用进程中的提醒。
 *     currentSession (string): 页面当前选择的主会话。
 *     viewReady (boolean): 页面已经完成当前会话的历史和状态展示。
 */
export class ChatAttention {
  private readonly sessions = new Map<string, SessionAttention>();
  private currentSession = "";
  private viewReady = false;
  private focused = false;

  /** 开始初始化所选会话，保留各会话已有提醒。
   * Args:
   *     sessionId (string): 页面选择展示的主会话。
   */
  beginView(sessionId: string): void { this.currentSession = sessionId; this.viewReady = false; }

  /** 全局断线后等待当前页面重新完成展示。 */
  invalidateView(): void { this.viewReady = false; }

  /** 更新窗口可见性，已完成展示的当前会话获得焦点时标记为已读。
   * Args:
   *     focused (boolean): 聊天窗口获得焦点且处于可见状态。
   */
  setFocused(focused: boolean): void {
    this.focused = focused;
    if (focused && this.viewReady) this.state(this.currentSession).unread = false;
  }

  /** 接收页面已经展示完成或历史恢复发现新回复的事实。
   * Args:
   *     update (ChatAttentionUpdate): 带会话标识的展示结果。
   */
  report(update: ChatAttentionUpdate): void {
    if (update.newMessage) this.markMessage(update.sessionId);
    if (update.viewed && update.sessionId === this.currentSession) {
      this.viewReady = true;
      if (this.focused) this.state(update.sessionId).unread = false;
    }
  }

  /** 根据会话事件维护未读及审批，快照校正其所属会话。
   * Args:
   *     event (InteractionEvent): 全局连接接收的事件或当前会话快照。
   */
  receive(event: InteractionEvent): void {
    const state = this.state(event.session_id);
    const child = typeof event.payload.subagent_id === "string" ? event.payload.subagent_id : null;
    if (event.kind === InteractionEventKind.TEXT_CHUNK) this.markMessage(event.session_id);
    else if (event.kind === InteractionEventKind.SESSION_SNAPSHOT) {
      state.approvals.clear();
      const prompt = event.payload.approval_prompt as { value?: Record<string, unknown>; subagent_id?: string } | null;
      const value = prompt?.value ?? event.payload.interrupt as Record<string, unknown> | null;
      this.addApproval(state, value, prompt?.subagent_id ?? null);
    } else if (event.kind === InteractionEventKind.INTERRUPT || event.kind === InteractionEventKind.SUBAGENT_INTERRUPT) {
      this.addApproval(state, event.payload.value as Record<string, unknown>, child);
    } else if (event.kind === InteractionEventKind.AUTHORIZATION_RESOLVED) {
      if (event.request_id) state.approvals.delete(event.request_id);
    } else if ([InteractionEventKind.RUN_COMPLETED, InteractionEventKind.ERROR, InteractionEventKind.HISTORY_RESTORED]
      .some(/** 判断会终结该上下文审批展示的事件。 */ (kind) => kind === event.kind)) {
      for (const [requestId, context] of state.approvals) if (context === child) state.approvals.delete(requestId);
    }
  }

  /** 汇总全部会话提醒，未展示的审批需要任务栏闪烁。
   * Returns:
   *     { unread: boolean; approval: boolean; flash: boolean }: 窗口标题和闪烁所需状态。
   */
  summary(): { unread: boolean; approval: boolean; flash: boolean } {
    let unread = false; let approval = false; let flash = false;
    for (const [sessionId, state] of this.sessions) {
      unread ||= state.unread;
      if (state.approvals.size) {
        approval = true;
        flash ||= sessionId !== this.currentSession || !this.focused || !this.viewReady;
      }
    }
    return { unread, approval, flash };
  }

  /** 取得或建立一个会话的进程内提醒状态。
   * Args:
   *     sessionId (string): 提醒所属主会话。
   * Returns:
   *     SessionAttention: 此会话的普通未读和审批记录。
   */
  private state(sessionId: string): SessionAttention {
    let state = this.sessions.get(sessionId);
    if (!state) { state = { unread: false, approvals: new Map() }; this.sessions.set(sessionId, state); }
    return state;
  }

  /** 当前会话已在焦点窗口展示时直接视为已读，其余情况保留提醒。
   * Args:
   *     sessionId (string): 新回复所属主会话。
   */
  private markMessage(sessionId: string): void {
    this.state(sessionId).unread = sessionId !== this.currentSession || !this.focused || !this.viewReady;
  }

  /** 记录有效的人工审批请求及实际父子上下文。
   * Args:
   *     state (SessionAttention): 请求所属会话的提醒状态。
   *     value (Record<string, unknown> | null): 中断中用于人工审批的材料。
   *     child (string | null): 子 Agent 编号，主 Agent 审批时为空。
   */
  private addApproval(state: SessionAttention, value: Record<string, unknown> | null, child: string | null): void {
    if (value?.type === InterruptKind.AUTHORIZATION_REQUEST && typeof value.request_id === "string") {
      state.approvals.set(value.request_id, child);
    }
  }
}
