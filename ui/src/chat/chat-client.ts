/**
 * 聊天客户端
 */

import { BubbleManager } from "./bubble.js";
import { ChatHistoryManager } from "./chat-history-manager.js";
import { ScreenshotConfirmDialog } from "./screenshot-confirm-dialog.js";
import { formatMcpApprovalMessage } from "./mcp-approval.js";
import { buildInterruptResponseMeta } from "./interrupt-response.js";
import { formatControlScreenApprovalMessage } from "./control-screen-approval.js";
import {
  isControlScreenCaptureInterrupt,
  isControlScreenExecuteInterrupt,
} from "./control-screen-interrupt.js";
import { ToolCallToastManager } from "./tool-call-toast.js";
import type { ChatInterruptPayload, ChatResult, ControlScreenActionData, DesktopPetApi } from "../types.js";

type ChatBridge = Pick<DesktopPetApi,
  "chat" | "onChatChunk" | "onToolCall" | "onChatInterrupt" | "getFrontendSettings" |
  "captureScreen" | "screenshotRespond" | "controlScreenRespond" | "mcpToolRespond" |
  "resolveScreenActionPoint" | "performScreenAction"
>;
type ChatHistory = Pick<ChatHistoryManager,
  "addMessage" | "showTypingIndicator" | "hideTypingIndicator" | "startStreaming" |
  "updateLastAiMessage" | "finalizeStreamingMessage" | "abortStreaming" |
  "showToolCallMessage" | "finalizeToolCallIndicator" | "showErrorMessage" | "loadHistory"
>;

/**
 * 聊天客户端选项
 */
export interface ChatClientOptions {
  bubble: Pick<BubbleManager, "setText">;
  chatHistory: ChatHistory;
  sendBtn: Pick<HTMLButtonElement, "disabled">;
  input: Pick<HTMLTextAreaElement, "value" | "focus"> & { style: Pick<CSSStyleDeclaration, "height"> };
  sessionId: string;
  onChatComplete?: () => void;
  bridge?: ChatBridge;
  confirmDialog?: Pick<ScreenshotConfirmDialog, "open" | "isOpen">;
  toolCallToast?: Pick<ToolCallToastManager, "show" | "showError">;
  playMotion?: (tag: string) => void;
}

/**
 * 聊天客户端
 */
export class ChatClient {
  private bubble: ChatClientOptions["bubble"];
  private chatHistory: ChatHistory;
  private sendBtn: ChatClientOptions["sendBtn"];
  private input: ChatClientOptions["input"];
  private sessionId: string;
  private onChatComplete?: () => void;
  private hasUserSubmittedMessage = false;
  private screenshotConfirmDialog: Pick<ScreenshotConfirmDialog, "open" | "isOpen">;
  private toolCallToast: Pick<ToolCallToastManager, "show" | "showError">;
  private bridge: ChatBridge;
  private playMotion: (tag: string) => void;
  private isRunning = false;
  private playedLabels: Set<string> = new Set(); // 追踪已播放的标签

  /**
   * 解析消息中的表情标签
   * 返回 { text: 清理后的文本, tags: 标签数组（去重后） }
   */
  private parseExpressionTags(text: string): { text: string; tags: string[] } {
    const tagRegex = /<([^<>]+)>/g;
    const tags: string[] = [];
    const seen = new Set<string>(); // 本次解析已见过的标签
    let match: RegExpExecArray | null;

    while ((match = tagRegex.exec(text)) !== null) {
      const tag = match[1];
      if (!seen.has(tag)) {
        seen.add(tag);
        tags.push(tag);
      }
    }

    // 移除标签后的文本
    const cleanText = text.replace(tagRegex, "").trim();
    return { text: cleanText, tags };
  }

  /**
   * 播放标签对应的动作
   */
  private playMotionForTag(tag: string): void {
    // 避免同一个标签重复播放
    if (this.playedLabels.has(tag)) {
      return;
    }
    this.playedLabels.add(tag);
    this.playMotion(tag);
  }

  constructor(options: ChatClientOptions) {
    this.bubble = options.bubble;
    this.chatHistory = options.chatHistory;
    this.sendBtn = options.sendBtn;
    this.input = options.input;
    this.sessionId = options.sessionId;
    this.onChatComplete = options.onChatComplete;
    this.screenshotConfirmDialog = options.confirmDialog ?? new ScreenshotConfirmDialog();
    this.toolCallToast = options.toolCallToast ?? new ToolCallToastManager();
    this.bridge = options.bridge ?? window.desktopPetApi;
    this.playMotion = options.playMotion ?? ((tag) => window.playMotionByLabel?.(tag));
  }

  /**
   * 设置会话 ID
   */
  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
  }

  /**
   * 获取会话 ID
   */
  getSessionId(): string {
    return this.sessionId;
  }

  /**
   * 标记用户已提交消息
   */
  markUserSubmitted(): void {
    this.hasUserSubmittedMessage = true;
  }

  /**
   * 检查用户是否已提交消息
   */
  hasUserSubmitted(): boolean {
    return this.hasUserSubmittedMessage;
  }

  private async captureScreenWithUserPreference(): Promise<{ dataUrl: string; width: number; height: number } | undefined> {
    const frontendSettings = await this.bridge.getFrontendSettings();
    const shouldHide = frontendSettings.hide_on_screenshot;
    try {
      if (shouldHide) {
        window.hideElementsForScreenshot?.();
        await this.delay(120);
      }
      return await this.bridge.captureScreen?.();
    } finally {
      if (shouldHide) {
        window.restoreElementsAfterScreenshot?.();
      }
    }
  }

  private async performControlScreenAction(action: ControlScreenActionData): Promise<{
    executed: boolean;
    error?: string;
    screenshot?: { dataUrl: string; width: number; height: number };
  }> {
    const frontendSettings = await this.bridge.getFrontendSettings();
    const shouldHide = frontendSettings.hide_on_screenshot;
    try {
      if (shouldHide) {
        window.hideElementsForScreenshot?.();
        await this.delay(120);
      }

      const actionResult = await this.bridge.performScreenAction?.(action);
      if (!actionResult?.executed) {
        return {
          executed: false,
          error: actionResult?.error || "屏幕操作未完成",
        };
      }

      await this.delay(Math.max(0, action.wait_seconds) * 1000);
      const screenshot = await this.bridge.captureScreen?.();
      return { executed: true, screenshot };
    } finally {
      if (shouldHide) {
        window.restoreElementsAfterScreenshot?.();
      }
    }
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  private async respondToInterrupt(
    interruptData: ChatInterruptPayload,
    streamRequestId: string,
    sessionId: string,
    startSegment: () => void
  ): Promise<ChatResult> {
    const submit = async (send: () => Promise<ChatResult> | undefined): Promise<ChatResult> => {
      startSegment();
      const result = await send();
      if (!result) throw new Error("中断响应桥接未就绪");
      return result;
    };
    const interruptValue = interruptData.value;
    const responseMeta = buildInterruptResponseMeta(interruptValue, streamRequestId);
    if (interruptValue.type === "screenshot_request") {
      const approved = await this.screenshotConfirmDialog.open(
        interruptValue.message || "Agent 请求截取屏幕，是否允许？"
      );
      if (!approved) {
        return await submit(() => this.bridge.screenshotRespond?.({
          sessionId,
          ...responseMeta,
          approved: false,
        }));
      }
      const screenshot = await this.captureScreenWithUserPreference();
      return await submit(() => this.bridge.screenshotRespond?.({
        sessionId,
        ...responseMeta,
        approved: true,
        screenshotData: screenshot?.dataUrl,
        width: screenshot?.width,
        height: screenshot?.height,
      }));
    }

    if (isControlScreenCaptureInterrupt(interruptValue)) {
      const screenshot = await this.captureScreenWithUserPreference();
      return await submit(() => this.bridge.controlScreenRespond?.({
        sessionId,
        ...responseMeta,
        screenshotData: screenshot?.dataUrl,
        width: screenshot?.width,
        height: screenshot?.height,
      }));
    }

    if (interruptValue.type === "mcp_tool_approval_request") {
      const approved = await this.screenshotConfirmDialog.open(
        formatMcpApprovalMessage(interruptValue),
        "MCP 工具调用确认",
        "允许本次调用",
        "拒绝"
      );
      return await submit(() => this.bridge.mcpToolRespond?.({
        sessionId,
        ...responseMeta,
        approved,
      }));
    }

    if (!isControlScreenExecuteInterrupt(interruptValue)) {
      throw new Error("无法处理未知的聊天中断类型");
    }

    const action = interruptValue.data;
    const screenPoint = await this.bridge.resolveScreenActionPoint?.(action.coordinates);
    if (!screenPoint) {
      throw new Error("无法解析屏幕操作的真实坐标");
    }
    const approved = await this.screenshotConfirmDialog.open(
      formatControlScreenApprovalMessage(action, screenPoint)
    );
    if (!approved) {
      return await submit(() => this.bridge.controlScreenRespond?.({
        sessionId,
        ...responseMeta,
        approved: false,
      }));
    }
    const actionResult = await this.performControlScreenAction(action);
    return await submit(() => this.bridge.controlScreenRespond?.({
      sessionId,
      ...responseMeta,
      approved: true,
      executed: actionResult.executed,
      error: actionResult.error,
      screenshotData: actionResult.screenshot?.dataUrl,
      width: actionResult.screenshot?.width,
      height: actionResult.screenshot?.height,
    }));
  }

  async restorePendingInterrupt(initialInterrupt: ChatInterruptPayload): Promise<void> {
    if (this.isRunning || this.screenshotConfirmDialog.isOpen()) return;
    await this.runTurn({ interrupt: initialInterrupt });
  }

  async sendMessage(images?: string[]): Promise<void> {
    if (this.isRunning) return;
    const message = this.input.value.trim();
    if (!message && !images?.length) return;
    this.chatHistory.addMessage({
      role: "human", content: message, timestamp: new Date().toISOString(), images,
    });
    await this.runTurn({ message, images });
  }

  /** 两个入口共用一轮生命周期；只有请求返回值能推进中断续聊。 */
  private async runTurn(start: { message: string; images?: string[] } | { interrupt: ChatInterruptPayload }): Promise<void> {
    this.isRunning = true;
    this.markUserSubmitted();
    this.sendBtn.disabled = true;
    this.playedLabels.clear();
    const sessionId = this.sessionId;
    const restoring = "interrupt" in start;
    let interrupt = restoring ? start.interrupt : undefined;
    let requestId = "";
    let streamedText = "";
    let lastCompletedText = "";
    let segmentOpen = false;
    let pendingTool = false;
    let cursorVisible = true;
    let cursorTimer: ReturnType<typeof setInterval> | undefined;
    const subscriptions: (() => void)[] = [];

    const stopCursor = () => {
      if (cursorTimer !== undefined) clearInterval(cursorTimer);
      cursorTimer = undefined;
    };
    const renderText = (cursor: boolean) => {
      const { text, tags } = this.parseExpressionTags(streamedText);
      for (const tag of tags) this.playMotionForTag(tag);
      this.bubble.setText(cursor ? `${text || "思考中..."}${cursorVisible ? "▋" : ""}` : text);
      if (text) this.chatHistory.updateLastAiMessage(text);
      return text;
    };
    const finishTools = () => {
      if (pendingTool) this.chatHistory.finalizeToolCallIndicator();
      pendingTool = false;
    };
    const startSegment = () => {
      streamedText = "";
      cursorVisible = true;
      segmentOpen = true;
      this.chatHistory.startStreaming();
      this.chatHistory.showTypingIndicator();
      renderText(true);
      cursorTimer = setInterval(() => {
        cursorVisible = !cursorVisible;
        renderText(true);
      }, 380);
    };
    const finishSegment = (response: string) => {
      stopCursor();
      this.chatHistory.hideTypingIndicator();
      streamedText ||= response;
      const text = renderText(false);
      if (text) {
        this.chatHistory.finalizeStreamingMessage();
        lastCompletedText = text;
      } else {
        this.chatHistory.abortStreaming();
        this.bubble.setText(lastCompletedText);
      }
      segmentOpen = false;
    };

    try {
      subscriptions.push(this.bridge.onChatChunk((data) => {
        if (!segmentOpen || data.requestId !== requestId) return;
        this.chatHistory.hideTypingIndicator();
        finishTools();
        streamedText += data.chunk;
        renderText(true);
      }));
      const unsubscribeTool = this.bridge.onToolCall?.((data) => {
        if (!segmentOpen || data.requestId !== requestId) return;
        this.chatHistory.showToolCallMessage(data.toolName);
        this.toolCallToast.show(data.toolName);
        pendingTool = true;
      });
      if (unsubscribeTool) subscriptions.push(unsubscribeTool);

      while (true) {
        // 每段独立关联 ID，上一段迟到的事件不能污染下一段。
        requestId = `chat-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
        let result: ChatResult;
        if (interrupt) {
          result = await this.respondToInterrupt(interrupt, requestId, sessionId, startSegment);
        } else if ("message" in start) {
          startSegment();
          result = await this.bridge.chat(start.message, sessionId || undefined, requestId, start.images);
        } else {
          throw new Error("缺少待恢复的中断");
        }
        finishSegment(result.response);
        if (!result.interrupted) break;
        interrupt = result.interruptData;
        if (!interrupt) throw new Error("中断响应缺少恢复信息");
      }
      finishTools();
      if (!restoring) {
        this.input.value = "";
        this.input.style.height = "auto";
      }
      if (restoring) {
        try {
          await this.chatHistory.loadHistory(sessionId);
        } catch (error) {
          const message = `对话已完成，但历史记录同步失败: ${String(error)}`;
          console.warn(message);
          this.chatHistory.showErrorMessage(message);
        }
      }
    } catch (error) {
      const message = `对话执行失败: ${String(error)}`;
      if (segmentOpen) this.chatHistory.abortStreaming();
      this.bubble.setText(message);
      this.chatHistory.showErrorMessage(message);
      this.toolCallToast.showError(message);
    } finally {
      stopCursor();
      segmentOpen = false;
      for (const unsubscribe of subscriptions) unsubscribe();
      finishTools();
      this.chatHistory.hideTypingIndicator();
      this.sendBtn.disabled = false;
      this.isRunning = false;
      this.input.focus();
      this.onChatComplete?.();
    }
  }

}

/**
 * 启动最新 AI 消息加载
 */
export const startLatestAiMessageBootstrap = (
  sessionId: string,
  bubble: BubbleManager,
  hasUserSubmitted: () => boolean,
  onConnect?: () => void
): (() => void) => {
  let stopped = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const stop = () => {
    stopped = true;
    if (retryTimer) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
  };

  const scheduleRetry = () => {
    if (stopped) {
      return;
    }

    retryTimer = setTimeout(() => {
      void run();
    }, 1000);
  };

  const run = async () => {
    if (stopped) {
      return;
    }

    try {
      const { latestAiMessage } = await window.desktopPetApi.getLatestAiMessage(sessionId);
      // 首次成功连接后端时触发回调（用于加载聊天历史）
      if (onConnect) {
        onConnect();
      }
      if (
        !hasUserSubmitted() &&
        typeof latestAiMessage === "string" &&
        latestAiMessage.trim().length > 0
      ) {
        bubble.setText(latestAiMessage.trim());
      }
      stop();
    } catch {
      scheduleRetry();
    }
  };

  void run();
  return stop;
};
