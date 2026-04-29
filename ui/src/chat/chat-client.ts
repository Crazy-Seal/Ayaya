/**
 * 聊天客户端
 */

import { BubbleManager } from "./bubble.js";

/**
 * 聊天客户端选项
 */
export interface ChatClientOptions {
  bubble: BubbleManager;
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
  private sendBtn: HTMLButtonElement;
  private input: HTMLInputElement;
  private sessionId: string;
  private onChatComplete?: () => void;
  private hasUserSubmittedMessage = false;

  constructor(options: ChatClientOptions) {
    this.bubble = options.bubble;
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

    this.sendBtn.disabled = true;
    this.bubble.setText("思考中...");

    const requestId = `chat-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    let streamedText = "";
    let cursorVisible = true;
    let cursorTimer: ReturnType<typeof setInterval> | null = null;

    const renderStreamingBubble = () => {
      const baseText = streamedText || "思考中...";
      this.bubble.setText(`${baseText}${cursorVisible ? "▋" : ""}`);
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
      this.bubble.setText(streamedText || result.response);
      this.input.value = "";
    } catch (error) {
      stopCursor();
      this.bubble.setText(`请求失败: ${String(error)}`);
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
