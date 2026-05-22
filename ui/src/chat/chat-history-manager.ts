/**
 * 聊天历史管理器
 */

import type { ChatHistoryItem } from "../types.js";

// 后端静态文件服务地址
const BACKEND_BASE_URL = "http://127.0.0.1:8000";

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
  private outputSentenceCount = 0;
  private typingIndicator: HTMLDivElement | null = null;
  private toolCallIndicator: HTMLDivElement | null = null;

  // 句子队列和定时器（用于延迟输出）
  private sentenceQueue: string[] = [];
  private outputTimer: ReturnType<typeof setInterval> | null = null;
  private static readonly OUTPUT_INTERVAL_MS = 500;

  constructor(container: HTMLDivElement) {
    this.container = container;
  }

  /**
   * 显示"正在输入"提示
   */
  showTypingIndicator(): void {
    if (this.typingIndicator) {
      return;
    }

    const indicator = document.createElement("div");
    indicator.className = "typing-indicator";
    indicator.textContent = "对方正在输入";

    this.container.appendChild(indicator);
    this.typingIndicator = indicator;
    this.scrollToBottom();
  }

  /**
   * 隐藏"正在输入"提示
   */
  hideTypingIndicator(): void {
    if (this.typingIndicator) {
      this.typingIndicator.remove();
      this.typingIndicator = null;
    }
  }

  /**
   * 显示工具调用提示（作为消息记录）
   * 显示为"正在调用工具: xxx"，带闪烁效果
   */
  showToolCallMessage(toolName: string): void {
    // 先隐藏"对方正在输入中"提示
    this.hideTypingIndicator();

    const indicator = document.createElement("div");
    indicator.className = "typing-indicator";
    indicator.textContent = `正在调用工具: ${toolName}`;
    indicator.setAttribute("data-tool-call", "pending");

    this.container.appendChild(indicator);
    this.scrollToBottom();
  }

  /**
   * 将工具调用提示从"正在调用"改为"调用完成"状态
   * 停止闪烁，改为"调用工具: xxx"
   */
  finalizeToolCallIndicator(): void {
    const pendingIndicator = this.container.querySelector('.typing-indicator[data-tool-call="pending"]');
    if (pendingIndicator) {
      const text = pendingIndicator.textContent || "";
      // 从"正在调用工具: xxx" 改为 "调用工具: xxx"
      const toolName = text.replace("正在调用工具: ", "");
      pendingIndicator.textContent = `调用工具: ${toolName}`;
      pendingIndicator.setAttribute("data-tool-call", "true");
    }
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

    // 等待图片加载完成后再滚动到底部
    this.scrollToBottomAfterImagesLoad();
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

    // 提取完整句子
    const { complete, remaining } = extractCompleteSentences(content);

    // 将新增的完整句子放入队列
    for (let i = this.outputSentenceCount; i < complete.length; i++) {
      const sentence = complete[i];
      if (sentence) {
        this.sentenceQueue.push(sentence);
      }
    }
    this.outputSentenceCount = complete.length;
  }

  /**
   * 完成流式响应
   */
  finalizeStreamingMessage(): void {
    if (!this.isStreaming) {
      return;
    }

    this.isStreaming = false;

    // 处理剩余内容：如果有未输出的内容，直接作为最后一个气泡
    const last = this.messages[this.messages.length - 1];
    if (last && last.content) {
      // 提取所有完整句子
      const { complete, remaining } = extractCompleteSentences(last.content);

      // 将剩余的完整句子放入队列
      for (let i = this.outputSentenceCount; i < complete.length; i++) {
        const sentence = complete[i];
        if (sentence) {
          this.sentenceQueue.push(sentence);
        }
      }

      // 如果有不完整的剩余内容，也作为一个气泡
      if (remaining.trim().length > 0) {
        this.sentenceQueue.push(remaining.trim());
      }
    }

    this.outputSentenceCount = 0;
  }

  /**
   * 清空历史
   */
  clear(): void {
    this.messages = [];
    this.container.innerHTML = "";
    this.isStreaming = false;
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
   * 渲染所有消息
   */
  private render(): void {
    this.container.innerHTML = "";
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
   * 渲染单条消息（文本气泡在前，图片气泡在后）
   */
  private renderMessage(msg: ChatHistoryItem): void {
    const role = msg.role.toLowerCase();
    const isHuman = role === "human" || role === "user";
    const isToolCalling = role === "ai_tool_calling";

    // 工具调用消息使用特殊样式
    if (isToolCalling) {
      const indicator = document.createElement("div");
      indicator.className = "typing-indicator";
      // 确保 content 有效，否则显示默认文本
      indicator.textContent = msg.content || "调用工具中...";
      // 历史消息显示为完成状态（不闪烁）
      indicator.setAttribute("data-tool-call", "true");
      this.container.appendChild(indicator);
      return;
    }

    // 分割成句子
    const sentences = this.splitIntoSentences(msg.content);

    // 1. 先渲染文本气泡（如果有文本）
    for (const sentence of sentences) {
      if (!sentence) continue;

      const item = document.createElement("div");
      item.className = `message-item ${isHuman ? "human" : "ai"}`;

      const bubble = document.createElement("div");
      bubble.className = "message-bubble";

      // 渲染文本
      const textNode = document.createTextNode(removeTrailingPeriod(sentence));
      bubble.appendChild(textNode);

      item.appendChild(bubble);
      this.container.appendChild(item);
    }

    // 2. 再渲染图片气泡（如果有图片）
    if (msg.images && msg.images.length > 0) {
      const imagesContainer = document.createElement("div");
      imagesContainer.className = "message-images";

      for (const imageData of msg.images) {
        // 跳过空值
        if (!imageData) continue;

        const img = document.createElement("img");

        // 判断是 data URL 还是文件名
        if (imageData.startsWith("data:image/")) {
          img.src = imageData;
        } else {
          img.src = `${BACKEND_BASE_URL}/images/${imageData}`;
        }

        img.alt = "图片";
        img.loading = "lazy";
        img.style.cursor = "pointer";

        // 点击打开独立预览窗口
        img.addEventListener("click", () => {
          window.desktopPetApi.openImagePreview(img.src);
        });

        imagesContainer.appendChild(img);
      }

      // 如果有有效图片，创建独立的图片气泡
      if (imagesContainer.children.length > 0) {
        const item = document.createElement("div");
        item.className = `message-item ${isHuman ? "human" : "ai"}`;

        const bubble = document.createElement("div");
        bubble.className = "message-bubble";
        bubble.appendChild(imagesContainer);

        item.appendChild(bubble);
        this.container.appendChild(item);
      }
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
    // 使用 instant 避免 smooth 滚动动画冲突导致的跳动
    this.container.scrollTo({
      top: this.container.scrollHeight,
      behavior: "instant",
    });
  }

  /**
   * 等待图片加载完成后滚动到底部
   */
  private scrollToBottomAfterImagesLoad(): void {
    const images = this.container.querySelectorAll<HTMLImageElement>(".message-images img");

    if (images.length === 0) {
      // 没有图片，直接滚动
      this.scrollToBottom();
      return;
    }

    let loadedCount = 0;
    const totalCount = images.length;

    const checkAllLoaded = () => {
      loadedCount++;
      if (loadedCount >= totalCount) {
        // 所有图片加载完成，滚动到底部
        setTimeout(() => this.scrollToBottom(), 50);
      }
    };

    images.forEach((img) => {
      if (img.complete) {
        // 图片已经加载完成
        checkAllLoaded();
      } else {
        // 等待图片加载
        img.addEventListener("load", checkAllLoaded);
        img.addEventListener("error", checkAllLoaded); // 即使加载失败也继续
      }
    });

    // 设置超时，防止图片加载过慢导致永远不滚动
    setTimeout(() => {
      this.scrollToBottom();
    }, 1000);
  }
}
