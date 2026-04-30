/**
 * 聊天客户端
 */

import { BubbleManager } from "./bubble.js";
import { ChatHistoryManager } from "./chat-history-manager.js";

/**
 * 聊天客户端选项
 */
export interface ChatClientOptions {
  bubble: BubbleManager;
  chatHistory: ChatHistoryManager;
  sendBtn: HTMLButtonElement;
  input: HTMLInputElement;
  sessionId: string;
  onChatComplete?: () => void;
}

/**
 * 聊天客户端
 */
export class ChatClient {
  private bubble: BubbleManager;
  private chatHistory: ChatHistoryManager;
  private sendBtn: HTMLButtonElement;
  private input: HTMLInputElement;
  private sessionId: string;
  private onChatComplete?: () => void;
  private hasUserSubmittedMessage = false;

  constructor(options: ChatClientOptions) {
    this.bubble = options.bubble;
    this.chatHistory = options.chatHistory;
    this.sendBtn = options.sendBtn;
    this.input = options.input;
    this.sessionId = options.sessionId;
    this.onChatComplete = options.onChatComplete;
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

  /**
   * 发送聊天消息
   */
  async sendMessage(): Promise<void> {
    this.markUserSubmitted();

    const message = this.input.value.trim();
    if (!message) {
      return;
    }

    // 添加用户消息到历史
    this.chatHistory.addMessage({
      role: "human",
      content: message,
      timestamp: new Date().toISOString(),
    });

    this.sendBtn.disabled = true;

    const requestId = `chat-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let streamedText = "";
    let cursorVisible = true;
    let cursorTimer: ReturnType<typeof setInterval> | null = null;

    const renderStreamingBubble = () => {
      const baseText = streamedText || "思考中...";
      this.bubble.setText(`${baseText}${cursorVisible ? "▋" : ""}`);
      // 更新聊天历史中的 AI 消息
      this.chatHistory.updateLastAiMessage(baseText);
    };

    const stopCursor = () => {
      if (cursorTimer) {
        clearInterval(cursorTimer);
        cursorTimer = null;
      }
    };

    cursorTimer = setInterval(() => {
      cursorVisible = !cursorVisible;
      renderStreamingBubble();
    }, 380);

    // 先添加一条空的 AI 消息占位，用于流式更新
    this.chatHistory.addMessage({
      role: "ai",
      content: "思考中...",
      timestamp: new Date().toISOString(),
    });

    renderStreamingBubble();

    const unsubscribeChatChunk = window.desktopPetApi.onChatChunk(({ requestId: chunkRequestId, chunk }) => {
      if (chunkRequestId !== requestId) {
        return;
      }

      streamedText += chunk;
      renderStreamingBubble();
    });

    try {
      if (!window.desktopPetApi || typeof window.desktopPetApi.chat !== "function") {
        throw new Error("桌宠桥接未就绪，请重启桌宠程序");
      }

      const result = await window.desktopPetApi.chat(message, this.sessionId || undefined, requestId);
      stopCursor();
      const finalResponse = streamedText || result.response;
      this.bubble.setText(finalResponse);
      // 更新聊天历史中的最终 AI 消息
      this.chatHistory.updateLastAiMessage(finalResponse);
      // 完成流式响应，分割句子渲染
      this.chatHistory.finalizeStreamingMessage();
      this.input.value = "";
    } catch (error) {
      stopCursor();
      const errorMessage = `请求失败: ${String(error)}`;
      this.bubble.setText(errorMessage);
      this.chatHistory.updateLastAiMessage(errorMessage);
      this.chatHistory.finalizeStreamingMessage();
    } finally {
      stopCursor();

      unsubscribeChatChunk();
      this.sendBtn.disabled = false;
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
  hasUserSubmitted: () => boolean
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
