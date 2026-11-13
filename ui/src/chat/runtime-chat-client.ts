/** 桌面交互 Adapter 的 Renderer 视图；Agent 生命周期由后端管理。 */
import { HistoryRole, SubagentStatus, type SubagentSummary } from "../../shared-types.js";
import { iconMarkup } from "../ui/icons.js";
import { ToolApprovalDialog } from "./tool-approval-dialog.js";
import { BackgroundTaskEntry } from "./background-task-entry.js";
import {
  CommandKind, DESKTOP_CHANNEL, InteractionEventKind, InterruptKind, SessionPhase,
} from "../../shared-types.js";
import type { ChatAttentionUpdate, ChatHistoryItem, FailedInteractionInput, ToolCompletedData } from "../../shared-types.js";
import type { BubbleManager } from "./bubble.js";
import type { ChatHistoryManager } from "./chat-history-manager.js";
import type {
    ApprovalDecision, DesktopPetApi, InteractionEvent,
} from "../types.js";

/**
 * 限定聊天视图所需的主进程命令、订阅及任务查询端口。
 */
type RuntimeBridge = Pick<DesktopPetApi,
  "submitInteractionCommand" | "subscribeDesktopSession" | "onInteractionEvent" |
  "getBackgroundTasks" | "getBackgroundTaskOutput"
>;

/**
 * 向运行时聊天视图注入 DOM、展示器和测试可替换的主进程 bridge。
 *
 * Attributes:
 *     bubble (Pick<BubbleManager, "setText">): 桌面气泡文本显示端口。
 *     chatHistory (Pick<ChatHistoryManager,
 * "addMessage" | "showTypingIndicator" | "hideTypingIndicator" | "startStreaming" |
 * "updateLastAiMessage" | "finalizeStreamingMessage" | "abortStreaming" |
 * "showPluginNotice" | "showToolCallMessage" | "finalizeToolCallIndicator" | "showErrorMessage" | "loadHistory"
 * > & Partial<Pick<ChatHistoryManager,
 * "setSessionId" | "showPendingSteer" | "clearPendingSteer" |
 * "showFailedInput" | "clearFailedInput" | "setFailedInputRetrying"
 * >>): 历史、草稿、工具状态和错误展示端口。
 *     sendBtn (HTMLButtonElement): 当前发送或停止按钮。
 *     input (HTMLTextAreaElement): 真人草稿输入元素。
 *     sessionId (string): 当前桌面会话标识。
 *     hasAttachments (() => boolean): 判断纯图片草稿是否有效的回调。
 *     onChatComplete (() => void): 有效输入受理后清空附件的回调。
 *     bridge (RuntimeBridge): 可替换的主进程交互端口。
 *     confirmDialog (Pick<ToolApprovalDialog, "open" | "dismiss">): 可替换的工具审批视图。
 */
export interface RuntimeChatClientOptions {
  bubble: Pick<BubbleManager, "setText">;
  chatHistory: Pick<ChatHistoryManager,
    "addMessage" | "showTypingIndicator" | "hideTypingIndicator" | "startStreaming" |
    "updateLastAiMessage" | "finalizeStreamingMessage" | "abortStreaming" |
    "showPluginNotice" | "showToolCallMessage" | "finalizeToolCallIndicator" | "showErrorMessage" | "loadHistory"
  > & Partial<Pick<ChatHistoryManager,
    "setSessionId" | "showPendingSteer" | "clearPendingSteer" |
    "showFailedInput" | "clearFailedInput" | "setFailedInputRetrying"
  >>;
  sendBtn: HTMLButtonElement;
  input: HTMLTextAreaElement;
  sessionId: string;
  hasAttachments?: () => boolean;
  onChatComplete?: () => void;
  /** 已核对会话与事件水位后通知窗口更新未读或审批提醒。 */
  onAttention?: (update: ChatAttentionUpdate) => void;
  bridge?: RuntimeBridge;
  confirmDialog?: Pick<ToolApprovalDialog, "open" | "dismiss">;
}

/**
 * 把桌面动作归一化为命令，并按快照与增量事件更新对话、审批和任务视图。
 *
 * Attributes:
 *     bridge (RuntimeBridge): 主进程提供的运行时查询和命令端口。
 *     dialog (Pick<ToolApprovalDialog, "open" | "dismiss">): 当前工具审批对话框。
 *     originalSendIcon (string): 普通发送按钮的原始图标，停止状态结束后恢复。
 *     sessionId (string): 当前会话标识，用于丢弃跨会话迟到响应。
 *     phase (SessionPhase): 后端投影的会话阶段，原始快照字段仅在接收处转换。
 *     runId (string | null): 当前运行标识，停止和引导命令用它限定目标。
 *     runOrigin (string): 当前回复所属入口渠道。
 *     partial (string): 当前页面实时收到的回复片段，不从快照恢复，展示前清洗标签。
 *     playedLabels (Set<string>): 当前轮次已经处理的表情标签，重连恢复不重播。
 *     streaming (boolean): 是否已经建立本轮流式草稿。
 *     approvalId (string | null): 当前模态审批的 request_id。
 *     epoch (string): 已应用快照的后端启动标识。
 *     sequence (number): 同 epoch 中已应用的事件水位。
 *     submitting (boolean): 是否在提交命令，避免重复点击。
 *     composing (boolean): 输入法组合状态。
 *     currentToolActive (boolean): 是否仍在等待已启动工具，影响引导等待文案。
 *     pendingSteerMessage (string | null): 本地等待后端应用的引导正文。
 *     queuedInputs (Map<number, string>): 按 Inbox 输入序号保存的排队消息。
 *     taskEntry (BackgroundTaskEntry | null): 聊天标题栏任务快捷入口和终态短提示。
 *     eventTail (Promise<void>): 串行事件处理链，历史加载完成之前不处理下一事件。
 *     initialized (boolean): 新页面已应用首次快照和历史查询结果后，才允许提交输入。
 *     sessionRevision (number): 每次初始化递增，防止旧会话或旧初始化的异步结果更新当前页面。
 *     deferredRuns (Set<string>): 尚未取得完整历史的回答编号，在工具结果或轮末历史同步成功后继续展示。
 *     pendingEvents (Map<InteractionEvent, number>): 尚未处理的事件及页面代次，供历史查询识别积压正文。
 *     refreshingRevision (number | null): 正在读取最新历史的页面代次；限定工具结果终点的查询不设置此值。
 *     refreshedApprovals (Set<string>): 本启动代次中已成功刷新历史的审批编号，避免同审批重复查询。
 */
export class RuntimeChatClient {
  private readonly bridge: RuntimeBridge;
  private readonly dialog: Pick<ToolApprovalDialog, "open" | "dismiss">;
  private readonly originalSendIcon: string;
  private sessionId: string;
  private phase: SessionPhase = SessionPhase.IDLE;
  private runId: string | null = null;
  private runOrigin = DESKTOP_CHANNEL;
  private partial = "";
  private playedLabels = new Set<string>();
  private streaming = false;
  private approvalId: string | null = null;
  /** 当前任务栏提示对应的审批编号，用户点击后仍保留到后端确认处理。 */
  private attentionApprovalId: string | null = null;
  /** 当前会话上次成功读取的回复编号；null 表示尚未建立旧历史基线。 */
  private historyReplyIds: Set<string> | null = null;
  private epoch = "";
  private sequence = -1;
  private submitting = false;
  private composing = false;
  private currentToolActive = false;
  private pendingSteerMessage: string | null = null;
  private queuedInputs = new Map<number, string>();
  private readonly taskEntry: BackgroundTaskEntry | null;
  private readonly subagents = new Map<string, SubagentSummary>();
  private approvalSubagentId: string | null = null;
  private eventTail: Promise<void> = Promise.resolve();
  private initialized = false;
  private sessionRevision = 0;
  private deferredRuns = new Set<string>();
  private pendingEvents = new Map<InteractionEvent, number>();
  private refreshingRevision: number | null = null;
  private refreshedApprovals = new Set<string>();
  /** 当前会话唯一待重发输入，不属于已保存历史；新输入受理后覆盖。 */
  private failedInput: FailedInteractionInput | null = null;
  /** 本进程已失败的回答编号，迟到分片通过 epoch 和 run 一起拒绝。 */
  private failedRuns = new Set<string>();
  /** 保存故障在已提交历史刷新后继续显示，新的明确输入由用户主动发送。 */
  private saveFailure: { run_id: string | null; detail: string } | null = null;

  /**
   * 装配 RuntimeChatClient 的依赖并初始化本地视图状态。
   *
   * Args:
   *     options (RuntimeChatClientOptions): 桌面 UI 依赖和可替换 bridge。
   */
  constructor(private readonly options: RuntimeChatClientOptions) {
    this.bridge = options.bridge ?? window.desktopPetApi;
    this.dialog = options.confirmDialog ?? new ToolApprovalDialog();
    this.originalSendIcon = options.sendBtn.innerHTML;
    this.sessionId = options.sessionId;
    document.getElementById("subagent-entry")?.addEventListener("click", /** 打开独立子 Agent 面板。 */
      () => window.desktopPetApi.openSettingsWindow("subagents"));
    const taskRoot = document.getElementById("chat-history-toolbar");
    const taskButton = document.getElementById("background-task-entry") as HTMLButtonElement | null;
    const taskNotice = document.getElementById("background-task-notice");
    this.taskEntry = taskRoot && taskButton && taskNotice
      ? new BackgroundTaskEntry(taskRoot, taskButton, taskNotice, this.bridge, /**
       * 用户点击后台任务入口时请求主进程打开任务设置页。
       */
        () => window.desktopPetApi.openSettingsWindow("tasks"))
      : null;
    // 1. 事件通过 Promise 尾链串行处理，快照加载历史结束前不能应用下一增量。
    this.bridge.onInteractionEvent(/**
     * 运行事件到达时记录所属会话版本，历史刷新期间先暂存回复，再按顺序加入事件处理链。
     *
     * Args:
     *     event (InteractionEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const revision = this.sessionRevision;
      this.pendingEvents.set(event, revision);
      if (this.refreshingRevision === revision) this.deferReceivedReply(event);
      this.eventTail = this.eventTail.then(/**
       * 前一事件处理结束后移除待处理标记，仅当会话版本仍匹配时应用当前事件。
       */
      () => {
        this.pendingEvents.delete(event);
        return revision === this.sessionRevision ? this.handleEvent(event) : undefined;
      }).catch(/**
       * 事件处理链失败后在聊天列表显示错误，后续事件仍可继续进入处理链。
       *
       * Args:
       *     error (unknown): 本次捕获的异常。
       */
      (error: unknown) => {
        this.options.chatHistory.showErrorMessage(`处理运行事件失败: ${String(error)}`);
      });
    });
    if (this.sessionId) this.setSessionId(this.sessionId);
  }

  /**
   * 切换会话并清理上一会话的临时视图，异步结果必须再核对会话归属。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   */
  setSessionId(sessionId: string): void {
    // 1. 新页面或会话切换都需要新的快照；在历史与状态应用完成前不能发送或排队。
    this.sessionRevision += 1;
    this.initialized = false;
    this.deferredRuns.clear();
    this.refreshedApprovals.clear();
    this.refreshingRevision = null;
    // 1.1. 切换会话清理上一轮临时状态和审批，避免迟到事件串入新视图。
    if (this.sessionId !== sessionId) {
      this.failedRuns.clear();
      this.saveFailure = null;
      this.clearFailedInput(this.failedInput);
      this.options.onAttention?.({ reset: true });
      this.attentionApprovalId = null;
      this.historyReplyIds = null;
      this.phase = SessionPhase.IDLE;
      this.runId = null;
      this.partial = "";
      this.playedLabels.clear();
      this.streaming = false;
      this.currentToolActive = false;
      this.epoch = "";
      this.sequence = -1;
      this.queuedInputs.clear();
      this.renderQueuedInputs();
      this.pendingSteerMessage = null;
      this.options.chatHistory.clearPendingSteer?.();
      if (this.approvalId) {
        this.approvalId = null;
        this.dialog.dismiss();
      }
    }
    // 2. 同步历史及任务子视图，再由主进程建立新的会话订阅。
    this.sessionId = sessionId;
    this.options.chatHistory.setSessionId?.(sessionId);
    this.taskEntry?.setSessionId(sessionId);
    this.subagents.clear();
    this.updateSubagentEntry();
    this.approvalSubagentId = null;
    this.updateSendButton();
    const revision = this.sessionRevision;
    void this.bridge.subscribeDesktopSession(sessionId, { refreshSnapshot: true }).catch(/**
     * 会话订阅失败时只向仍属于该版本的页面显示错误，旧会话失败不污染新会话。
     *
     * Args:
     *     error (unknown): 本次捕获的异常。
     */
    (error: unknown) => {
      if (revision === this.sessionRevision) {
        this.options.chatHistory.showErrorMessage(`事件订阅失败: ${String(error)}`);
      }
    });
  }

  /**
   * 返回当前桌面交互会话标识。
   *
   * Returns:
   *     string: 当前聊天页所显示的会话编号。
   */
  getSessionId(): string { return this.sessionId; }
  /**
   * 同步输入法组合状态，组合期间保持发送动作稳定。
   *
   * Args:
   *     composing (boolean): 是否正在输入法组合输入。
   */
  setComposing(composing: boolean): void {
    this.composing = composing;
    if (!composing) this.updateSendButton();
  }

  /**
   * 根据草稿、图片、运行阶段和 IME 状态更新发送、停止及排队入口。
   */
  updateSendButton(): void {
    // 1. 初始化和提交等待必须禁用按钮，IME 期间也不能绕过；组合输入只暂停图标切换。
    this.options.sendBtn.disabled = !this.initialized || this.submitting || this.phase === SessionPhase.PENDING_INTERRUPT;
    const queueButton = document.getElementById("queue-btn") as HTMLButtonElement | null;
    if (queueButton) queueButton.disabled = !this.initialized || this.submitting;
    // 1.1. IME 组合输入期间保留按钮模式，避免候选字确认误触发送。
    if (this.composing) return;
    const draft = this.options.input.value.trim();
    // 2. 文本或图片都构成有效草稿；运行中空草稿表示停止，有草稿表示引导。
    const hasDraft = Boolean(draft || this.options.hasAttachments?.());
    const stop = this.phase === SessionPhase.RUNNING && !hasDraft;
    this.options.sendBtn.innerHTML = stop ? iconMarkup("stop") : this.originalSendIcon;
    this.options.sendBtn.setAttribute("aria-label", stop ? "停止当前回答" : "发送消息");
    this.options.sendBtn.title = stop ? "停止当前回答" : "发送消息";
    if (queueButton) {
      queueButton.hidden = this.phase !== SessionPhase.RUNNING || !hasDraft;
    }
  }

  /**
   * 为当前回复来源启动流式视图，避免重复创建草稿。
   */
  private startStreaming(): void {
    if (this.streaming) return;
    this.streaming = true;
    this.options.chatHistory.startStreaming(this.runOrigin);
    this.options.chatHistory.showTypingIndicator();
  }

  /**
   * 将原始流式片段投影为正文，并按轮次去重表情动作。
   */
  private renderReply(): void {
    // 1. 完整标签只作为动作信号；重连和累计正文中的重复标签不能重复播放。
    const tagPattern = /<([^<>]+)>/g;
    for (const match of this.partial.matchAll(tagPattern)) {
      const label = match[1];
      if (this.playedLabels.has(label)) continue;
      this.playedLabels.add(label);
      window.playMotionByLabel?.(label);
    }

    // 2. 暂存末尾尚未闭合的标签，防止分片中的 <惊 或单独 < 短暂进入 DOM。
    // 2.1. 带空白的比较正文（例如 2 < 3）不属于标签前缀，应正常展示。
    const text = this.partial.replace(tagPattern, "").replace(/<[^<>\s]*$/, "");
    // 3. 聊天列表保留末尾换行，两个换行跨片段到达时也能立即确认段落结束。
    if (text.trim()) this.options.chatHistory.updateLastAiMessage(text);
    // 4. 桌面气泡沿用同一份已清理标签的正文，显示时去掉首尾空白。
    this.options.bubble.setText(text.trim());
  }

  /**
   * 结束本地流式视图并清理输入提示，后端 run 生命周期由事件决定。
   */
  private finishStreaming(): void {
    if (!this.streaming) return;
    this.options.chatHistory.hideTypingIndicator();
    this.options.chatHistory.finalizeToolCallIndicator();
    if (this.partial.trim()) this.options.chatHistory.finalizeStreamingMessage();
    else this.options.chatHistory.abortStreaming();
    this.streaming = false;
  }

  /**
   * 生成包含后端启动标识的运行键，避免重启后运行编号与旧记录混淆。
   *
   * Args:
   *     runId (string | null): 当前或事件携带的运行编号，缺失时使用空编号兼容旧事件。
   *
   * Returns:
   *     string: 当前启动代次内的运行键。
   */
  private replyKey(runId: string | null): string {
    return `${this.epoch}:${runId ?? ""}`;
  }

  /**
   * 历史查询开始前及查询期间，标记已经到达的正文所属回答，完成后再展示。
   *
   * Args:
   *     event (InteractionEvent): 主进程已经交付但可能仍在串行队列中的事件。
   */
  private deferReceivedReply(event: InteractionEvent): void {
    if (event.session_id !== this.sessionId || event.stream_epoch !== this.epoch
        || event.event_sequence <= this.sequence) return;
    if (event.kind === InteractionEventKind.RUN_STARTED || event.kind === InteractionEventKind.TEXT_CHUNK) {
      this.deferredRuns.add(this.replyKey(event.run_id || this.runId));
    }
  }

  /**
   * 在初始化、审批、工具完成和终态读取历史；工具完成时限定到已保存结果，后续正文继续实时显示。
   *
   * 本方法由串行事件处理调用；后端完成或审批事件已经先保存对应状态。
   * 错误事件只保证此前保存仍在。查询失败保留已经显示的正文，不自动重试。
   *
   * Args:
   *     throughMessageId (string | undefined): 工具完成后的查询终点；省略时读取最新历史并延后期间到达的正文。
   *
   * Returns:
   *     Promise<boolean>: 历史仍属于当前页面且成功应用时为 true，否则为 false。
   */
  private async refreshHistory(throughMessageId?: string): Promise<boolean> {
    const revision = this.sessionRevision;
    const sessionId = this.sessionId;
    // 1. 结束本地草稿，历史管理器在查询前显示剩余段落并停止输出动画。
    this.finishStreaming();
    this.options.chatHistory.hideTypingIndicator();
    this.options.chatHistory.finalizeToolCallIndicator();
    this.partial = "";
    // 1.1. 最新历史可能包含排队正文；工具结果限定查询排除了后续回复，可以继续按事件顺序显示。
    const deferReplies = throughMessageId === undefined;
    if (deferReplies) {
      this.refreshingRevision = revision;
      for (const [event, queuedRevision] of this.pendingEvents) {
        if (queuedRevision === revision) this.deferReceivedReply(event);
      }
    }
    try {
      const history = await this.options.chatHistory.loadHistory(sessionId, throughMessageId);
      if (revision !== this.sessionRevision || sessionId !== this.sessionId || history === null) return false;
      this.reportHistoryAttention(history);
      // 2. 历史与实时正文使用相同标签清洗规则，但读取历史不能重播模型表情。
      let latestText = "";
      for (let index = history.length - 1; index >= 0; index -= 1) {
        const item = history[index];
        if (item.role.toLowerCase() !== HistoryRole.AI.toLowerCase() && item.role.toLowerCase() !== HistoryRole.ASSISTANT) continue;
        if (!item.content.trim()) continue;
        latestText = item.content.replace(/<([^<>]+)>/g, "").replace(/<[^<>\s]*$/, "").trim();
        break;
      }
      this.options.bubble.setText(latestText);
      return true;
    } catch (error) {
      if (revision === this.sessionRevision && sessionId === this.sessionId) {
        this.options.chatHistory.showErrorMessage(`历史读取失败: ${String(error)}`);
      }
      return false;
    } finally {
      // 3. 会话切换后不能解除新页面的查询保护，也不能用旧查询更新其显示。
      if (deferReplies && this.refreshingRevision === revision) this.refreshingRevision = null;
    }
  }

  /**
   * 根据保存的回复编号补发断线期间的新消息提醒，首次加载和重复历史不提醒。
   *
   * Args:
   *     history (ChatHistoryItem[]): 已核对当前会话归属且成功展示的完整历史。
   */
  private reportHistoryAttention(history: ChatHistoryItem[]): void {
    const current = new Set<string>();
    let hasNewReply = false;
    // 1. 只记录有正文或图片的模型回复，真人消息和空工具记录不触发回复提醒。
    for (const item of history) {
      const role = item.role.toLowerCase();
      if ((role !== HistoryRole.AI.toLowerCase() && role !== HistoryRole.ASSISTANT) || (!item.content.trim() && !item.images?.length)) continue;
      const key = item.id ?? `${item.timestamp}:${item.content}`;
      current.add(key);
      if (this.historyReplyIds && !this.historyReplyIds.has(key)) hasNewReply = true;
    }
    // 2. 首次成功读取只建立旧历史基线；重连、审批或终态刷新发现新编号时才通知窗口。
    this.historyReplyIds = current;
    if (hasNewReply) this.options.onAttention?.({ newMessage: true });
  }

  /**
   * 打开同一 request_id 的模态审批，过期或跨会话决定不再提交。
   *
   * Args:
   *     value (Record<string, unknown>): 中断展示对象或当前回调值。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async showApproval(value: Record<string, unknown>, subagentId: string | null = null): Promise<void> {
    // 1. 只打开有效审批且同 request_id 不重复创建模态框。
    if (value.type !== InterruptKind.AUTHORIZATION_REQUEST || typeof value.request_id !== "string") return;
    if (this.approvalId === value.request_id) return;
    const requestId = value.request_id;
    const sessionId = this.sessionId;
    this.approvalId = requestId;
    this.approvalSubagentId = subagentId;
    const data = value.data as Record<string, unknown> | undefined;
    const decision = await this.dialog.open({
      title: subagentId ? `子 Agent ${subagentId} 工具调用确认` : "工具调用确认",
      message: String(value.message || "是否允许此工具操作？"),
      details: JSON.stringify(data?.prepared_action ?? data?.arguments ?? {}, null, 2),
      allowSteer: true,
    });
    // 2. 对话框等待期间请求可能被跨端处理或切换会话，提交前必须再次核对。
    if (this.approvalId !== requestId || this.sessionId !== sessionId) return;
    this.approvalId = null;
    this.approvalSubagentId = null;
    await this.submitApproval(requestId, decision, subagentId);
  }

  /**
   * 执行故障后关闭该上下文的旧审批，迟到的弹窗决定会在提交前失效。
   * Args:
   *     subagentId (string | null): 故障所属子编号，父会话为空。
   * Returns:
   *     boolean: 本次关闭了仍在显示的对应审批时为 true。
   */
  private dismissFailedApproval(subagentId: string | null): boolean {
    if (this.approvalSubagentId !== subagentId || (!this.approvalId && !this.attentionApprovalId)) return false;
    this.approvalId = null;
    this.approvalSubagentId = null;
    this.attentionApprovalId = null;
    this.options.onAttention?.({ approvalId: null });
    this.dialog.dismiss();
    return true;
  }

  /**
   * 提交当前请求的人工决定，业务拒绝时显示已处理或过期原因。
   *
   * Args:
   *     requestId (string): 持久审批标识。
   *     decision (ApprovalDecision): 批准、拒绝或审批框引导决定。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async submitApproval(requestId: string, decision: ApprovalDecision, subagentId: string | null = null): Promise<void> {
    try {
      const receipt = await this.bridge.submitInteractionCommand({
        session_id: this.sessionId,
        origin_channel: DESKTOP_CHANNEL,
        kind: CommandKind.RESOLVE_AUTHORIZATION,
        request_id: requestId,
        ...(subagentId ? { subagent_id: subagentId } : {}),
        payload: decision,
      });
      if (!receipt.accepted) this.options.chatHistory.showErrorMessage(receipt.detail || "审批已由其他入口处理");
    } catch (error) {
      this.options.chatHistory.showErrorMessage(`提交审批失败: ${String(error)}`);
    }
  }

  /**
   * 按持久输入序号渲染待交付列表，并提供同来源撤销入口。
   */
  private renderQueuedInputs(): void {
    const status = document.getElementById("queue-status");
    if (!status) return;
    status.replaceChildren();
    for (const [sequence, message] of this.queuedInputs) {
      const row = document.createElement("div");
      row.className = "queued-message";
      const label = document.createElement("span");
      label.textContent = message || "已排队的图片消息";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "撤销";
      cancel.className = "ui-button ui-button--secondary ui-button--small";
      cancel.setAttribute("aria-label", `撤销排队消息 ${message || sequence}`);
      cancel.addEventListener("click", /**
       * 用户点击排队项撤销按钮后提交指定输入编号的撤销命令。
       */
      () => { void this.cancelQueued(sequence); });
      row.append(label, cancel);
      status.append(row);
    }
    status.hidden = this.queuedInputs.size === 0;
  }

  /**
   * 请求撤销尚未领取的排队输入，视图移除由后端事件确认。
   *
   * Args:
   *     sequence (number): Inbox 持久输入序号。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async cancelQueued(sequence: number): Promise<void> {
    const sessionId = this.sessionId;
    try {
      const receipt = await this.bridge.submitInteractionCommand({
        session_id: sessionId, origin_channel: DESKTOP_CHANNEL,
        kind: CommandKind.CANCEL_QUEUED, payload: { input_sequence: sequence },
      });
      if (!receipt.accepted) throw new Error(receipt.detail || "排队消息无法撤销");
    } catch (error) {
      this.options.chatHistory.showErrorMessage(String(error));
    }
  }

  /**
   * 把当前草稿映射为新输入、引导或停止，模态审批与 IME 期间不提交。
   *
   * Args:
   *     images (string[]): 附带的图片 data URL 数组。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  async sendMessage(images?: string[]): Promise<void> {
    if (!this.initialized || this.submitting || this.composing || this.phase === SessionPhase.PENDING_INTERRUPT) return;
    const message = this.options.input.value.trim();
    if (!message && !images?.length && this.phase !== SessionPhase.RUNNING) return;
    const kind = this.phase === SessionPhase.RUNNING
      ? message || images?.length ? CommandKind.HUMAN_STEER : CommandKind.STOP_TURN
      : CommandKind.HUMAN_TURN;
    await this.submit(kind, message, images);
  }

  /**
   * 在运行中把有效文本或图片草稿提交为下一轮排队输入。
   *
   * Args:
   *     images (string[]): 附带的图片 data URL 数组。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  async queueMessage(images?: string[]): Promise<void> {
    if (!this.initialized || this.submitting || this.composing || this.phase !== SessionPhase.RUNNING) return;
    const message = this.options.input.value.trim();
    if (!message && !images?.length) return;
    await this.submit(CommandKind.QUEUE_TURN, message, images);
  }

  /**
   * 仅清除调用开始时的失败消息，迟到回执不能覆盖随后到达的新失败。
   * Args:
   *     input (FailedInteractionInput | null): 发起命令前看到的失败输入。
   */
  private clearFailedInput(input: FailedInteractionInput | null): void {
    if (!input || this.failedInput !== input) return;
    this.failedInput = null;
    this.options.chatHistory.clearFailedInput?.();
  }

  /**
   * 用户点击圆形按钮后按新真人输入重发原正文和图片，不消耗当前草稿。
   * Args:
   *     input (FailedInteractionInput): 点击时对应的失败输入。
   * Returns:
   *     Promise<void>: 回执已处理，失败时保留入口供用户选择。
   */
  private async retryFailedInput(input: FailedInteractionInput): Promise<void> {
    // 1. 确认点击仍对应当前失败输入，先禁用按钮，再等待订阅和命令回执。
    if (!this.initialized || this.submitting || this.failedInput !== input) return;
    const sessionId = this.sessionId;
    this.submitting = true;
    this.options.chatHistory.setFailedInputRetrying?.(true);
    this.updateSendButton();
    try {
      await this.bridge.subscribeDesktopSession(sessionId);
      // 2. 等待期间会话可能切换；只向发起点击时的会话重发原正文和图片。
      if (sessionId !== this.sessionId) return;
      const receipt = await this.bridge.submitInteractionCommand({
        session_id: sessionId, origin_channel: DESKTOP_CHANNEL, kind: CommandKind.HUMAN_TURN,
        payload: { message: input.message, images: input.images },
      });
      if (sessionId !== this.sessionId) return;
      if (!receipt.accepted) throw new Error(receipt.detail || "命令未被受理");
      // 3. 只清除本次点击对应的失败展示，期间到达的新失败由其自身按钮处理。
      this.clearFailedInput(input);
    } catch (error) {
      if (sessionId === this.sessionId) this.options.chatHistory.showErrorMessage(`重试失败: ${String(error)}`);
    } finally {
      // 4. 任何回执都释放提交状态；仍显示同一失败输入时重新启用其按钮。
      this.submitting = false;
      if (this.failedInput === input) this.options.chatHistory.setFailedInputRetrying?.(false);
      this.updateSendButton();
    }
  }

  /**
   * 先确保事件订阅可用再提交命令，受理后清空草稿并按事件处理真实运行状态。
   *
   * Args:
   *     kind (CommandKind): 已知统一命令种类。
   *     message (string): 当前真人草稿正文。
   *     images (string[]): 附带的图片 data URL 数组。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async submit(kind: CommandKind, message: string, images?: string[]): Promise<void> {
    // 1. 捕获会话并标记提交中，先等待首次快照以取得当前 run 身份。
    const sessionId = this.sessionId;
    const replacedFailure = this.failedInput;
    this.submitting = true;
    this.updateSendButton();
    try {
      await this.bridge.subscribeDesktopSession(sessionId);
      if (this.sessionId !== sessionId || !this.initialized) return;
      // 2. 提交之前设置引导等待标记，SSE 可能先于命令回执到达。
      if (kind === CommandKind.HUMAN_STEER) {
        this.pendingSteerMessage = message;
        this.options.chatHistory.showPendingSteer?.(this.currentToolActive);
      }
      // 3. HTTP 只确认受理，真实消息和运行终态统一由事件更新。
      const receipt = await this.bridge.submitInteractionCommand({
        session_id: sessionId,
        origin_channel: DESKTOP_CHANNEL,
        kind,
        target_run_id: kind === CommandKind.STOP_TURN || kind === CommandKind.HUMAN_STEER
          ? this.runId ?? undefined : undefined,
        payload: { message, images: images ?? [] },
      });
      if (!receipt.accepted) throw new Error(receipt.detail || "命令未被受理");
      if (this.sessionId !== sessionId) return;
      // 4. 受理有效输入后清空草稿和附件；停止动作不消耗用户草稿。
      if (kind !== CommandKind.STOP_TURN) {
        this.clearFailedInput(replacedFailure);
        this.options.input.value = "";
        this.options.input.style.height = "auto";
        this.options.onChatComplete?.();
      }
    } catch (error) {
      if (kind === CommandKind.HUMAN_STEER) {
        this.pendingSteerMessage = null;
        this.options.chatHistory.clearPendingSteer?.();
      }
      this.options.chatHistory.showErrorMessage(`提交命令失败: ${String(error)}`);
    // 5. 无论业务拒绝或网络失败都释放本地提交锁并恢复输入焦点。
    } finally {
      this.submitting = false;
      this.updateSendButton();
      this.options.input.focus();
    }
  }

  /** 按实际子摘要更新独立入口，空闲可复用身份也计入总数。 */
  private updateSubagentEntry(): void {
    const button = document.getElementById("subagent-entry");
    if (!button) return;
    const running = [...this.subagents.values()].filter(/** 等待父回复仍属于活动任务。 */
      (item) => item.status !== SubagentStatus.IDLE).length;
    button.textContent = `子 Agent · ${running}/${this.subagents.size}`;
    button.hidden = this.subagents.size === 0;
  }

  /**
   * 串行应用会话快照及增量，按 epoch 和序号过滤重复或过期事件。
   *
   * Args:
   *     event (InteractionEvent): 渠道事件或 DOM 事件，由所属定义决定。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async handleEvent(event: InteractionEvent): Promise<void> {
    const revision = this.sessionRevision;
    // 1. 拒绝其他会话事件，当前 epoch 和水位只从快照建立。
    if (event.session_id !== this.sessionId) return;
    // 2. 重连建立事件水位和运行编号，再读历史；当前回答不恢复半段正文。
    if (event.kind === InteractionEventKind.SESSION_SNAPSHOT) {
      this.initialized = false;
      this.updateSendButton();
      const epochChanged = this.epoch !== event.stream_epoch;
      if (epochChanged) {
        this.epoch = event.stream_epoch;
        this.sequence = -1;
        this.deferredRuns.clear();
        this.refreshedApprovals.clear();
        this.failedRuns.clear();
      }
      this.sequence = event.event_sequence;
      const snapshot = event.payload;
      const failure = snapshot.save_failure as { run_id?: string; detail?: string } | null;
      this.saveFailure = failure ? { run_id: failure.run_id || null, detail: failure.detail || "执行结果保存失败" } : null;
      if (this.saveFailure?.run_id) this.failedRuns.add(this.replyKey(this.saveFailure.run_id));
      // 2.1. 此处仅标注既有线协议；仍沿用缺省空闲和未知值不匹配已知分支的行为。
      this.phase = String(snapshot.phase || SessionPhase.IDLE) as SessionPhase;
      const restoredRun = typeof snapshot.run_id === "string" ? snapshot.run_id : null;
      if (epochChanged || this.runId !== restoredRun) this.playedLabels.clear();
      this.runId = restoredRun;
      this.runOrigin = String(snapshot.origin_channel || DESKTOP_CHANNEL);
      if (this.phase === SessionPhase.RUNNING || this.phase === SessionPhase.PENDING_INTERRUPT) {
        this.deferredRuns.add(this.replyKey(this.runId));
      }
      const historyLoaded = await this.refreshHistory();
      if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
      this.queuedInputs.clear();
      const queued = Array.isArray(snapshot.queued_inputs) ? snapshot.queued_inputs : [];
      for (const item of queued) {
        if (typeof item === "object" && item !== null) {
          const entry = item as Record<string, unknown>;
          if (typeof entry.input_sequence === "number") {
            this.queuedInputs.set(entry.input_sequence, String(entry.message || ""));
          }
        }
      }
      this.renderQueuedInputs();
      this.taskEntry?.setSnapshot(snapshot.tasks);
      this.subagents.clear();
      if (Array.isArray(snapshot.subagents)) {
        for (const item of snapshot.subagents as SubagentSummary[]) this.subagents.set(item.subagent_id, item);
      }
      this.updateSubagentEntry();
      const prompt = snapshot.approval_prompt && typeof snapshot.approval_prompt === "object"
        ? snapshot.approval_prompt as Record<string, unknown> : null;
      const pendingInterrupt = prompt?.value && typeof prompt.value === "object"
        ? prompt.value as Record<string, unknown>
        : snapshot.interrupt && typeof snapshot.interrupt === "object"
          ? snapshot.interrupt as Record<string, unknown> : null;
      if (this.approvalId && pendingInterrupt?.request_id !== this.approvalId) {
        this.approvalId = null;
        this.dialog.dismiss();
      }
      if (this.phase === SessionPhase.RUNNING) {
        this.options.chatHistory.showTypingIndicator();
        this.options.bubble.setText("正在回答…");
      }
      this.attentionApprovalId = pendingInterrupt?.type === InterruptKind.AUTHORIZATION_REQUEST ? String(pendingInterrupt.request_id) : null;
      this.options.onAttention?.({ approvalId: this.attentionApprovalId });
      if (pendingInterrupt) {
        if (historyLoaded && pendingInterrupt.type === InterruptKind.AUTHORIZATION_REQUEST) {
          this.refreshedApprovals.add(`${this.epoch}:${String(pendingInterrupt.request_id || "")}`);
        }
        void this.showApproval(pendingInterrupt, typeof prompt?.subagent_id === "string" ? prompt.subagent_id : null);
      }
      // 2.2. 主进程收到快照不等于页面已初始化；此处历史查询和当前状态应用都已结束。
      this.initialized = true;
      if (this.saveFailure) this.options.chatHistory.showErrorMessage(this.saveFailure.detail);
      this.updateSendButton();
      return;
    }
    // 3. 仅接受同 epoch 且序号递增的增量，序号空隙不代表消息丢失。
    if (event.stream_epoch !== this.epoch || event.event_sequence <= this.sequence) return;
    this.sequence = event.event_sequence;
    if (event.run_id && this.failedRuns.has(this.replyKey(event.run_id))
        && [InteractionEventKind.RUN_STARTED, InteractionEventKind.TEXT_CHUNK,
          InteractionEventKind.TOOL_CALL, InteractionEventKind.TOOL_COMPLETED, InteractionEventKind.RUN_COMPLETED]
          .some(/** 判断增量是否会重新生成失败轮次的临时回答。 */
            (kind) => kind === event.kind)) return;
    if (event.kind === InteractionEventKind.HISTORY_RESTORED) {
      const subagentId = typeof event.payload.subagent_id === "string" ? event.payload.subagent_id : null;
      this.dismissFailedApproval(subagentId);
      if (subagentId) return;
      this.options.chatHistory.abortStreaming();
      this.streaming = false;
      this.partial = "";
      this.phase = SessionPhase.IDLE;
      this.runId = null;
      await this.refreshHistory(typeof event.payload.through_message_id === "string" ? event.payload.through_message_id : undefined);
      if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
      if (this.saveFailure) this.options.chatHistory.showErrorMessage(this.saveFailure.detail);
      this.updateSendButton();
      return;
    }
    // 4. 真人事件已随 checkpoint 交付，用 message_id 与刚载入历史去重。
    if (event.kind === InteractionEventKind.SUBAGENT_STATUS) {
      const identifier = String(event.payload.subagent_id);
      if (event.payload.removed) this.subagents.delete(identifier);
      else this.subagents.set(identifier, event.payload as unknown as SubagentSummary);
      this.updateSubagentEntry();
      return;
    }
    if (event.kind === InteractionEventKind.SUBAGENT_INTERRUPT) {
      const value = event.payload.value as Record<string, unknown>;
      this.attentionApprovalId = String(value.request_id);
      this.options.onAttention?.({ approvalId: this.attentionApprovalId });
      void this.showApproval(value, String(event.payload.subagent_id));
      return;
    }
    if (event.kind === InteractionEventKind.HUMAN_MESSAGE) {
      if (event.origin_channel === DESKTOP_CHANNEL
          && String(event.payload.message || "") === this.pendingSteerMessage) {
        this.pendingSteerMessage = null;
        this.options.chatHistory.clearPendingSteer?.();
      }
      if (typeof event.payload.input_sequence === "number") {
        this.queuedInputs.delete(event.payload.input_sequence);
        this.renderQueuedInputs();
      }
      this.options.chatHistory.addMessage({
        id: typeof event.payload.message_id === "string" ? event.payload.message_id : undefined,
        role: HistoryRole.HUMAN.toLowerCase() as HistoryRole, content: String(event.payload.message || ""),
        images: event.payload.images as string[] | undefined,
        origin_channel: event.origin_channel || DESKTOP_CHANNEL,
        timestamp: new Date().toISOString(),
      });
    // 5. 排队受理与撤销更新输入序号列表，尚未交付的输入不伪装成历史。
    } else if (event.kind === InteractionEventKind.QUEUED_MESSAGE) {
      if (event.origin_channel === DESKTOP_CHANNEL
          && String(event.payload.message || "") === this.pendingSteerMessage) {
        this.pendingSteerMessage = null;
        this.options.chatHistory.clearPendingSteer?.();
      }
      const sequence = event.payload.input_sequence;
      if (typeof sequence === "number") {
        this.queuedInputs.set(sequence, String(event.payload.message || ""));
        this.renderQueuedInputs();
      }
    } else if (event.kind === InteractionEventKind.QUEUED_CANCELLED) {
      const sequence = event.payload.input_sequence;
      if (typeof sequence === "number") {
        this.queuedInputs.delete(sequence);
        this.renderQueuedInputs();
      }
    } else if (event.kind === InteractionEventKind.NOTICE) {
      this.options.chatHistory.showPluginNotice(String(event.payload.plugin_name), String(event.payload.text));
    } else if (event.kind === InteractionEventKind.TASK_STATUS) {
      this.taskEntry?.onStatus(String(event.payload.task_id || ""), String(event.payload.status || ""),
          typeof event.payload.tool_name === "string" ? event.payload.tool_name : undefined);
    // 6. 新 run 建立独立草稿；文本、工具和中断只更新该轮可见状态。
    } else if (event.kind === InteractionEventKind.RUN_STARTED) {
      this.currentToolActive = false;
      this.phase = SessionPhase.RUNNING;
      const nextRun = event.run_id || null;
      // 6.1. 新轮可再次播放相同表情；同 run 的工具恢复沿用已处理标签集合。
      if (this.runId !== nextRun || event.payload.resumed !== true) this.playedLabels.clear();
      this.runId = nextRun;
      this.runOrigin = event.origin_channel || DESKTOP_CHANNEL;
      this.partial = "";
      if (this.deferredRuns.has(this.replyKey(this.runId))) {
        this.options.chatHistory.showTypingIndicator();
        this.options.bubble.setText("正在回答…");
      } else {
        this.startStreaming();
      }
    } else if (event.kind === InteractionEventKind.TEXT_CHUNK) {
      this.options.onAttention?.({ newMessage: true });
      if (this.deferredRuns.has(this.replyKey(event.run_id || this.runId))) return;
      this.currentToolActive = false;
      this.runOrigin = event.origin_channel || DESKTOP_CHANNEL;
      this.startStreaming();
      this.partial += String(event.payload.value || "");
      this.options.chatHistory.hideTypingIndicator();
      this.renderReply();
    } else if (event.kind === InteractionEventKind.TOOL_CALL) {
      this.currentToolActive = true;
      this.options.chatHistory.showToolCallMessage(String(event.payload.value || "工具"));
    } else if (event.kind === InteractionEventKind.TOOL_COMPLETED) {
      // 1. 工具结果已保存，先收尾当前文字和调用提示，再查询截至该结果的历史。
      const completed = event.payload.value as ToolCompletedData;
      this.currentToolActive = false;
      const loaded = await this.refreshHistory(completed.message_id);
      if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
      // 2. 有了当前工具结果前的完整历史，重连期间延后的本轮正文也可以从下一片段继续显示。
      if (loaded) this.deferredRuns.delete(this.replyKey(event.run_id || this.runId));
      this.options.chatHistory.showTypingIndicator();
    } else if (event.kind === InteractionEventKind.INTERRUPT) {
      this.finishStreaming();
      const value = event.payload.value as Record<string, unknown>;
      this.phase = SessionPhase.PENDING_INTERRUPT;
      // 6.2. 自动桌面操作只更新状态；人工审批先显示已保存说明，同请求成功刷新一次。
      if (this.phase === SessionPhase.PENDING_INTERRUPT) {
        this.attentionApprovalId = String(value.request_id);
        this.options.onAttention?.({ approvalId: this.attentionApprovalId });
        const approvalKey = `${this.epoch}:${String(value.request_id || "")}`;
        if (!this.refreshedApprovals.has(approvalKey)) {
          const loaded = await this.refreshHistory();
          if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
          if (loaded) this.refreshedApprovals.add(approvalKey);
        }
        void this.showApproval(value);
      }
    // 7. 跨端首个决定按 request_id 关闭本地审批，不能提交第二个决定。
    } else if (event.kind === InteractionEventKind.AUTHORIZATION_RESOLVED) {
      // 1. 用户提交决定后本地弹窗已关闭，但提醒要等后端确认同一请求处理完成才清除。
      if (this.attentionApprovalId === event.request_id) {
        this.attentionApprovalId = null;
        this.options.onAttention?.({ approvalId: null });
      }
      if (this.approvalId === event.request_id) {
        this.approvalId = null;
        this.dialog.dismiss();
      }
    // 8. 完成含用户停止：收到保存后的事件才刷新；错误只恢复此前保存，最后展示错误。
    } else if (event.kind === InteractionEventKind.RUN_COMPLETED) {
      if (!this.approvalSubagentId) {
        this.attentionApprovalId = null;
        this.options.onAttention?.({ approvalId: null });
      }
      const completedKey = this.replyKey(event.run_id || this.runId);
      this.phase = SessionPhase.IDLE;
      this.runId = null;
      await this.refreshHistory();
      if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
      this.deferredRuns.delete(completedKey);
      this.options.chatHistory.hideTypingIndicator();
    } else if (event.kind === InteractionEventKind.ERROR) {
      const detail = String(event.payload.detail || event.payload.value || "运行失败");
      const dismissedApproval = this.dismissFailedApproval(typeof event.payload.subagent_id === "string" ? event.payload.subagent_id : null);
      if (event.payload.subagent_id) {
        this.options.chatHistory.showErrorMessage(`子 Agent ${String(event.payload.subagent_id)}：${detail}`);
        return;
      }
      if (event.payload.save_failed === true) {
        this.saveFailure = { run_id: event.run_id || this.runId, detail };
        if (this.saveFailure.run_id) this.failedRuns.add(this.replyKey(this.saveFailure.run_id));
        this.options.chatHistory.abortStreaming();
        this.streaming = false;
        this.partial = "";
        this.phase = SessionPhase.IDLE;
        this.runId = null;
        this.pendingSteerMessage = null;
        this.options.chatHistory.clearPendingSteer?.();
        this.options.chatHistory.hideTypingIndicator();
        this.options.chatHistory.showErrorMessage(detail);
        this.updateSendButton();
        return;
      }
      if (!this.approvalSubagentId && !dismissedApproval) {
        this.attentionApprovalId = null;
        this.options.onAttention?.({ approvalId: null });
      }
      const failedKey = this.replyKey(event.run_id || this.runId);
      this.pendingSteerMessage = null;
      this.options.chatHistory.clearPendingSteer?.();
      this.phase = SessionPhase.IDLE;
      this.runId = null;
      await this.refreshHistory();
      if (event.session_id !== this.sessionId || revision !== this.sessionRevision) return;
      this.deferredRuns.delete(failedKey);
      this.options.chatHistory.hideTypingIndicator();
      const failed = event.payload.failed_input as FailedInteractionInput | undefined;
      if (failed) {
        // 8.1. 失败输入没有进入历史，只在当前页面显示；已删除的排队项也立即移除。
        this.failedInput = failed;
        this.queuedInputs.delete(failed.input_sequence);
        this.renderQueuedInputs();
        this.options.chatHistory.showFailedInput?.({
          role: HistoryRole.HUMAN.toLowerCase() as HistoryRole, content: failed.message, images: failed.images,
          origin_channel: event.origin_channel || DESKTOP_CHANNEL, timestamp: new Date().toISOString(),
        }, detail, /** 用户点击后重发当前展示的原始输入。 */
        () => this.retryFailedInput(failed));
      } else {
        this.options.chatHistory.showErrorMessage(detail);
      }
    }
    this.updateSendButton();
  }
}
