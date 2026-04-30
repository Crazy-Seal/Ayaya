/**
 * 聊天历史管理器
 */

import type { ChatHistoryItem } from "../types.js";

/**
 * 去掉句尾的句号
 */
const removeTrailingPeriod = (sentence: string): string => {
  if (sentence.endsWith("。")) {
    return sentence.slice(0, -1);
  }
  return sentence;
};

/**
 * 提取完整的句子（以句号、问号、叹号、省略号结尾）
 * 返回 { complete: 完整句子数组, remaining: 剩余不完整的文本 }
 */
const extractCompleteSentences = (text: string): { complete: string[]; remaining: string } => {
  if (!text || text.trim().length === 0) {
    return { complete: [], remaining: "" };
  }

  const complete: string[] = [];
  const regex = /[^。！？…]+[。！？…]+/g;
  let match: RegExpExecArray | null;
  let lastIndex = 0;

  while ((match = regex.exec(text)) !== null) {
    complete.push(match[0].trim());
    lastIndex = match.index + match[0].length;
  }

  const remaining = text.slice(lastIndex).trim();
  return { complete, remaining };
};

/**
 * 聊天历史管理器
 */
export class ChatHistoryManager {
  private container: HTMLDivElement;
  private messages: ChatHistoryItem[] = [];
  private isStreaming = false;
  private streamingBuffer = "";
  private streamingBubble: HTMLDivElement | null = null;
  private outputSentenceCount = 0;

  // 句子队列和定时器（用于延迟输出）
  private sentenceQueue: string[] = [];
  private outputTimer: ReturnType<typeof setInterval> | null = null;
  private static readonly OUTPUT_INTERVAL_MS = 500;

  constructor(container: HTMLDivElement) {
    this.container = container;
  }

  /**
   * 加载聊天历史（获取10对消息）
   */
  async loadHistory(sessionId: string): Promise<void> {
    // 直接获取最后 100 条记录，足够包含 10 对消息
    const lastNHistory = await window.desktopPetApi.getChatHistoryLastN(sessionId, 100);

    // 从末尾向前找到第10条人类消息
    let humanCount = 0;
    let startIndex = 0;

    for (let i = lastNHistory.length - 1; i >= 0; i--) {
      const role = lastNHistory[i].role.toLowerCase();
      if (role === "human" || role === "user") {
        humanCount++;
        if (humanCount === 10) {
          startIndex = i;
          break;
        }
      }
    }

    // 如果不足10条人类消息，从头开始
    if (humanCount < 10) {
      startIndex = 0;
    }

    this.messages = lastNHistory.slice(startIndex);
    this.render();
    this.scrollToBottom();
  }

  /**
   * 添加新消息
   */
  addMessage(message: ChatHistoryItem): void {
    this.messages.push(message);
    const role = message.role.toLowerCase();
    const isAi = role === "ai" || role === "assistant";

    if (isAi) {
      // AI 消息开始流式响应
      this.isStreaming = true;
      this.streamingBuffer = "";
      this.streamingBubble = null;
      this.outputSentenceCount = 0;
      this.sentenceQueue = [];
      this.startOutputTimer();
    } else {
      // 人类消息直接分割渲染
      this.renderMessage(message);
    }
    this.scrollToBottom();
  }

  /**
   * 更新最后一条AI消息（流式响应）
   */
  updateLastAiMessage(content: string): void {
    if (this.messages.length === 0) {
      return;
    }

    const last = this.messages[this.messages.length - 1];
    const role = last.role.toLowerCase();
    if (role !== "ai" && role !== "assistant") {
      return;
    }

    last.content = content;

    if (!this.isStreaming) {
      this.render();
      return;
    }

    // 追加到缓冲区
    this.streamingBuffer = content;

    // 提取完整句子
    const { complete, remaining } = extractCompleteSentences(this.streamingBuffer);

    // 将新增的完整句子放入队列
    for (let i = this.outputSentenceCount; i < complete.length; i++) {
      const sentence = complete[i];
      if (sentence) {
        this.sentenceQueue.push(sentence);
      }
    }
    this.outputSentenceCount = complete.length;

    // 更新缓冲区为剩余内容
    this.streamingBuffer = remaining;

    // 更新流式气泡显示剩余内容
    if (remaining.length > 0) {
      this.updateStreamingBubble(remaining);
    } else if (this.streamingBubble) {
      // 没有剩余内容，移除流式气泡
      this.removeStreamingBubble();
    }

    this.scrollToBottom();
  }

  /**
   * 完成流式响应
   */
  finalizeStreamingMessage(): void {
    if (!this.isStreaming) {
      return;
    }

    this.isStreaming = false;

    // 输出缓冲区剩余内容
    if (this.streamingBuffer.trim().length > 0) {
      this.sentenceQueue.push(this.streamingBuffer.trim());
    }

    // 移除流式气泡
    this.removeStreamingBubble();

    this.streamingBuffer = "";
    // 不立即输出，让定时器继续运行直到队列为空
  }

  /**
   * 清空历史
   */
  clear(): void {
    this.messages = [];
    this.container.innerHTML = "";
    this.isStreaming = false;
    this.streamingBuffer = "";
    this.streamingBubble = null;
    this.outputSentenceCount = 0;
    this.stopOutputTimer();
    this.sentenceQueue = [];
  }

  /**
   * 启动输出定时器
   */
  private startOutputTimer(): void {
    if (this.outputTimer) {
      return;
    }

    this.outputTimer = setInterval(() => {
      this.outputNextSentence();
    }, ChatHistoryManager.OUTPUT_INTERVAL_MS);
  }

  /**
   * 停止输出定时器
   */
  private stopOutputTimer(): void {
    if (this.outputTimer) {
      clearInterval(this.outputTimer);
      this.outputTimer = null;
    }
  }

  /**
   * 输出下一个句子
   */
  private outputNextSentence(): void {
    const sentence = this.sentenceQueue.shift();
    if (sentence) {
      this.appendSentenceBubble(sentence);
      this.scrollToBottom();
    }

    // 如果队列已空且流式已结束，停止定时器
    if (this.sentenceQueue.length === 0 && !this.isStreaming) {
      this.stopOutputTimer();
    }
  }

  /**
   * 立即输出队列中所有句子
   */
  private flushQueue(): void {
    while (this.sentenceQueue.length > 0) {
      const sentence = this.sentenceQueue.shift();
      if (sentence) {
        this.appendSentenceBubble(sentence);
      }
    }
  }

  /**
   * 追加一个句子气泡
   */
  private appendSentenceBubble(sentence: string): void {
    const item = document.createElement("div");
    item.className = "message-item ai";

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.textContent = removeTrailingPeriod(sentence);

    item.appendChild(bubble);
    this.container.appendChild(item);
  }

  /**
   * 更新流式气泡内容
   */
  private updateStreamingBubble(content: string): void {
    if (!this.streamingBubble) {
      // 创建流式气泡
      const item = document.createElement("div");
      item.className = "message-item ai";

      const bubble = document.createElement("div");
      bubble.className = "message-bubble";
      bubble.textContent = removeTrailingPeriod(content);

      item.appendChild(bubble);
      this.container.appendChild(item);

      this.streamingBubble = bubble;
    } else {
      this.streamingBubble.textContent = removeTrailingPeriod(content);
    }
  }

  /**
   * 移除流式气泡
   */
  private removeStreamingBubble(): void {
    if (this.streamingBubble) {
      const parent = this.streamingBubble.parentElement;
      if (parent) {
        parent.remove();
      }
      this.streamingBubble = null;
    }
  }

  /**
   * 渲染所有消息
   */
  private render(): void {
    this.container.innerHTML = "";
    this.streamingBubble = null;
    // 渲染历史消息时禁用动画
    this.container.classList.add("no-animation");
    for (const msg of this.messages) {
      this.renderMessage(msg);
    }
    // 渲染完成后移除禁用动画的类
    requestAnimationFrame(() => {
      this.container.classList.remove("no-animation");
    });
  }

  /**
   * 渲染单条消息（分割句子）
   */
  private renderMessage(msg: ChatHistoryItem): void {
    const role = msg.role.toLowerCase();
    const isHuman = role === "human" || role === "user";

    // 分割成句子
    const sentences = this.splitIntoSentences(msg.content);

    // 每个句子创建一个气泡
    for (const sentence of sentences) {
      if (!sentence) continue;

      const item = document.createElement("div");
      item.className = `message-item ${isHuman ? "human" : "ai"}`;

      const bubble = document.createElement("div");
      bubble.className = "message-bubble";
      bubble.textContent = removeTrailingPeriod(sentence);

      item.appendChild(bubble);
      this.container.appendChild(item);
    }
  }

  /**
   * 将文本分割成句子
   */
  private splitIntoSentences(text: string): string[] {
    if (!text || text.trim().length === 0) {
      return [];
    }

    const sentences: string[] = [];
    const regex = /[^。！？…]+[。！？…]+/g;
    let match: RegExpExecArray | null;
    let lastIndex = 0;

    while ((match = regex.exec(text)) !== null) {
      sentences.push(match[0].trim());
      lastIndex = match.index + match[0].length;
    }

    const remaining = text.slice(lastIndex).trim();
    if (remaining.length > 0) {
      sentences.push(remaining);
    }

    return sentences.length > 0 ? sentences : [text.trim()];
  }

  /**
   * 滚动到底部
   */
  scrollToBottom(): void {
    this.container.scrollTo({
      top: this.container.scrollHeight,
      behavior: "smooth",
    });
  }
}
