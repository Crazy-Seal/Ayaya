/**
 * 聊天历史管理器
 */

import type { ChatHistoryItem } from "../types.js";
import { DESKTOP_CHANNEL } from "../../shared-types.js";
import { iconMarkup } from "../ui/icons.js";

// 后端静态文件服务地址

/**
 * 去掉句尾的句号
 *
 * Args:
 *     sentence (string): 等待显示的一句回复。
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
 *
 * Args:
 *     text (string): 本次需要展示或输入的文本。
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
 * 管理历史与流式气泡，使用消息身份和加载版本防止快照覆盖实时增量。
 *
 * Attributes:
 *     streamingOrigin (string): 流式回复的渠道来源。
 *     streamLabelShown (boolean): 当前流式气泡是否已显示来源标签。
 *     pendingSteerIndicator (HTMLDivElement | null): 已提交引导的本地等待元素。
 *     historyRevision (number): 历史和增量视图版本，防止旧查询覆盖新内容。
 *     historyRequest (number): 历史请求代次，clear 或新查询使旧响应失效。
 *     sessionId (string): 当前会话标识，用于丢弃跨会话迟到响应。
 */
export class ChatHistoryManager {
  private container: HTMLDivElement;
  private messages: ChatHistoryItem[] = [];
  private isStreaming = false;
  private streamingContent = "";
  private streamingOrigin = DESKTOP_CHANNEL;
  private streamLabelShown = false;
  private streamingDraftItems: HTMLElement[] = [];
  private outputSentenceCount = 0;
  private typingIndicator: HTMLDivElement | null = null;
  private toolCallIndicator: HTMLDivElement | null = null;
  private pendingSteerIndicator: HTMLDivElement | null = null;
  private historyRevision = 0;
  private historyRequest = 0;
  private sessionId = "";
  /** 当前会话唯一未交付失败展示；不加入 messages 或正式聊天历史。 */
  private failedInput: {
    message: ChatHistoryItem; detail: string; onRetry: () => Promise<void>; submitting: boolean;
  } | null = null;
  /** 当前渲染的临时失败节点，历史重绘时重新建立。 */
  private failedInputElement: HTMLElement | null = null;
  /** 当前失败展示的按钮，发送期间禁用。 */
  private retryInputButton: HTMLButtonElement | null = null;

  // 句子队列和定时器（用于延迟输出）
  private sentenceQueue: string[] = [];
  private outputTimer: ReturnType<typeof setInterval> | null = null;
  private static readonly OUTPUT_INTERVAL_MS = 500;

  /**
   * 绑定聊天列表容器，后续历史加载和流式回复均更新此容器。
   *
   * Args:
   *     container (HTMLDivElement): 当前组件负责维护的页面容器。
   */
  constructor(container: HTMLDivElement) {
    this.container = container;
  }

  /**
   * 切换会话并清理上一会话的临时视图，异步结果必须再核对会话归属。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   */
  setSessionId(sessionId: string): void {
    if (sessionId === this.sessionId) return;
    this.sessionId = sessionId;
    this.clear();
  }

  /**
   * 展示引导已提交的本地等待提示，并区分是否在等待已启动工具。
   *
   * Args:
   *     waitingForTool (boolean): 是否仍等待已启动工具完成。
   */
  showPendingSteer(waitingForTool: boolean): void {
    this.clearPendingSteer();
    const indicator = document.createElement("div");
    indicator.className = "typing-indicator pending-steer-indicator";
    indicator.textContent = waitingForTool
      ? "已收到引导，等待当前工具完成" : "引导已收到，等待应用";
    this.container.appendChild(indicator);
    this.pendingSteerIndicator = indicator;
    this.scrollToBottom();
  }

  /**
   * 移除本地引导等待提示，不改变后端轮次状态。
   */
  clearPendingSteer(): void {
    this.pendingSteerIndicator?.remove();
    this.pendingSteerIndicator = null;
  }

  /**
   * 清理消息内容中的表情标签
   *
   * Args:
   *     content (string): 当前需要展示的正文。
   */
  private cleanExpressionTags(content: string): string {
    return content.replace(/<[^<>]+>/g, "").trim();
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
   *
   * Args:
   *     toolName (string): 当前需要显示调用状态的工具名称。
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
   * 处理所有 pending 状态的指示器（支持一轮对话中多次工具调用）
   */
  finalizeToolCallIndicator(): void {
    const pendingIndicators = this.container.querySelectorAll('.typing-indicator[data-tool-call="pending"]');
    pendingIndicators.forEach(/**
     * 一轮工具执行结束时把所有待完成提示改为完成状态，保留工具名称并停止闪烁。
     *
     * Args:
     *     indicator (Element): 当前需要更新显示状态的提示元素。
     */
    (indicator) => {
      const text = indicator.textContent || "";
      // 从"正在调用工具: xxx" 改为 "调用工具: xxx"
      const toolName = text.replace("正在调用工具: ", "");
      indicator.textContent = `调用工具: ${toolName}`;
      indicator.setAttribute("data-tool-call", "true");
    });
  }

  /**
   * 显示错误提示
   * 显示为"出现错误"或具体错误信息，带错误样式
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  showErrorMessage(message?: string): void {
    // 先隐藏"对方正在输入中"提示
    this.hideTypingIndicator();

    const indicator = document.createElement("div");
    indicator.className = "typing-indicator error-indicator";
    indicator.textContent = message || "出现错误";

    this.container.appendChild(indicator);
    this.scrollToBottom();
  }

  /**
   * 读取历史并校验请求版本，返回实际显示的记录供聊天气泡复用。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<ChatHistoryItem[] | null>: 已应用的历史；查询被会话切换或新内容作废时返回 null。
   */
  async loadHistory(sessionId: string): Promise<ChatHistoryItem[] | null> {
    // 1. 捕获请求代次和视图版本，新增量或会话切换使旧历史加载失效。
    const request = ++this.historyRequest;
    const revision = this.historyRevision;
    // 1.1. 调用方已结束流式草稿；先显示剩余句子并停止动画，查询失败也保留已有正文。
    if (!this.isStreaming) {
      this.flushFinalizedQueue();
      this.stopOutputTimer();
    }
    // 直接获取最后 100 条记录，足够包含 10 对消息
    const lastNHistory = await window.desktopPetApi.getChatHistoryLastN(sessionId, 100);
    // 2. 加载结果仍属于当前视图时才应用，避免用旧列表覆盖实时文本。
    if (request !== this.historyRequest || revision !== this.historyRevision
        || (this.sessionId && sessionId !== this.sessionId)) return null;

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

    // 成功取得历史后，已完成的流式尾句由历史重绘接管，避免计时器重复追加。
    if (!this.isStreaming) this.abortStreaming();
    this.messages = lastNHistory.slice(startIndex);
    this.historyRevision += 1;
    this.render();
    if (this.pendingSteerIndicator) this.container.appendChild(this.pendingSteerIndicator);

    // 等待图片加载完成后再滚动到底部
    this.scrollToBottomAfterImagesLoad();
    return this.messages;
  }

  /**
   * 按领域消息标识去重，追加历史项并保持输入提示位于时间线末尾。
   *
   * Args:
   *     message (ChatHistoryItem): 已提交的可见消息，包含身份、正文及附件。
   */
  addMessage(message: ChatHistoryItem): void {
    // 1. 历史快照与真人增量可能包含同一消息，先按稳定身份去重。
    if (message.id && this.messages.some(/**
     * 按稳定消息编号检查历史是否已有当前真人消息，避免快照和增量重复显示。
     *
     * Args:
     *     item (ChatHistoryItem): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 历史编号匹配时为 true。
     */
    (item) => item.id === message.id)) return;
    this.historyRevision += 1;
    this.messages.push(message);
    // 2. 消息按提交顺序追加；run_started 可能先到，已有输入提示必须移到消息之后。
    this.renderMessage(message);
    if (this.typingIndicator) this.container.appendChild(this.typingIndicator);
    this.scrollToBottom();
  }

  /**
   * 显示原始失败输入和重试入口，不把它当作已保存消息。
   *
   * Args:
   *     message (ChatHistoryItem): 未交付的真人正文和图片。
   *     detail (string): 输入交付失败的说明。
   *     onRetry (() => Promise<void>): 用户点击按钮后，由客户端重新提交原输入。
   */
  showFailedInput(message: ChatHistoryItem, detail: string, onRetry: () => Promise<void>): void {
    this.clearFailedInput();
    this.failedInput = { message, detail, onRetry, submitting: false };
    this.renderFailedInput();
    this.scrollToBottom();
  }

  /** 清除已被重发或新消息覆盖的临时失败展示。 */
  clearFailedInput(): void {
    this.failedInputElement?.remove();
    this.failedInputElement = null;
    this.retryInputButton = null;
    this.failedInput = null;
  }

  /**
   * 同步重试命令等待回执时的按钮状态，历史重绘后也保持禁用。
   *
   * Args:
   *     submitting (boolean): 是否正在提交这条失败输入。
   */
  setFailedInputRetrying(submitting: boolean): void {
    if (this.failedInput) this.failedInput.submitting = submitting;
    if (this.retryInputButton) this.retryInputButton.disabled = submitting;
  }

  /** 将当前失败消息渲染到对话区，正文和图片直接使用原始输入。 */
  private renderFailedInput(): void {
    // 1. 移除上一轮渲染的节点；失败内容独立保存，历史重绘后仍可显示。
    this.failedInputElement?.remove();
    if (!this.failedInput) return;
    const failure = this.failedInput;
    const item = document.createElement("div");
    item.className = "message-item human failed-input";
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    const text = document.createElement("span");
    text.textContent = failure.message.content;
    bubble.appendChild(text);
    const images = document.createElement("div");
    // 2. 原始输入并未进入聊天历史，正文与附件直接从失败事件展示。
    images.className = "message-images";
    for (const image of failure.message.images ?? []) {
      const img = document.createElement("img");
      img.src = image;
      img.alt = "未发送的图片";
      images.appendChild(img);
    }
    if (images.children.length) bubble.appendChild(images);
    const detail = document.createElement("div");
    detail.className = "error-indicator failed-input-error";
    detail.textContent = failure.detail;
    bubble.appendChild(detail);
    const button = document.createElement("button");
    // 3. 圆形按钮只通知客户端重发；禁用状态随展示数据保留，不创建第二套请求流程。
    button.type = "button";
    button.className = "ui-button ui-button--secondary retry-input-button";
    button.title = "重试";
    button.setAttribute("aria-label", "重试");
    button.innerHTML = iconMarkup("retry");
    button.disabled = failure.submitting;
    button.addEventListener("click", /** 用户点击时仅提交当前失败输入，等待回执期间不重复触发。 */
    () => { if (!button.disabled) void failure.onRetry(); });
    item.appendChild(bubble);
    item.appendChild(button);
    this.container.appendChild(item);
    this.failedInputElement = item;
    this.retryInputButton = button;
  }

  /**
   * 为当前回复来源启动流式视图，避免重复创建草稿。
   *
   * Args:
   *     originChannel (string): 本轮回复所属渠道。
   */
  startStreaming(originChannel = DESKTOP_CHANNEL): void {
    this.historyRevision += 1;
    if (!this.isStreaming && this.sentenceQueue.length > 0) {
      this.flushFinalizedQueue();
    }
    this.abortStreaming();
    this.streamingContent = "";
    this.streamingOrigin = originChannel;
    this.streamLabelShown = false;
    this.isStreaming = true;
    this.outputSentenceCount = 0;
    this.sentenceQueue = [];
    this.startOutputTimer();
  }

  /**
   * 停止流式响应状态
   */
  stopStreaming(): void {
    this.abortStreaming();
  }

  /**
   * 丢弃本次未完成的流式草稿。
   */
  abortStreaming(): void {
    this.isStreaming = false;
    this.stopOutputTimer();
    this.sentenceQueue = [];
    this.outputSentenceCount = 0;
    this.streamingContent = "";
    for (const item of this.streamingDraftItems) {
      item.remove();
    }
    this.streamingDraftItems = [];
  }

  /**
   * 更新当前流式正文，携带所属回复渠道。
   *
   * Args:
   *     content (string): 完整的当前流式正文。
   */
  updateLastAiMessage(content: string): void {
    this.historyRevision += 1;
    this.streamingContent = content;

    if (!this.isStreaming) {
      // 非流式模式，直接添加消息
      this.addMessage({
        role: "ai",
        content,
        timestamp: new Date().toISOString(),
        origin_channel: this.streamingOrigin,
      });
      return;
    }

    // 提取完整句子放入队列
    const { complete } = extractCompleteSentences(content);
    for (let i = this.outputSentenceCount; i < complete.length; i++) {
      const sentence = complete[i];
      if (sentence) {
        this.sentenceQueue.push(sentence);
      }
    }
    this.outputSentenceCount = complete.length;
  }

  /**
   * 收尾句子队列并保存本地回复项，保留跨端来源标识。
   */
  finalizeStreamingMessage(): void {
    if (!this.isStreaming) {
      return;
    }

    this.isStreaming = false;
    this.historyRevision += 1;

    // 处理剩余内容
    const { complete, remaining } = extractCompleteSentences(this.streamingContent);
    for (let i = this.outputSentenceCount; i < complete.length; i++) {
      const sentence = complete[i];
      if (sentence) {
        this.sentenceQueue.push(sentence);
      }
    }
    if (remaining.trim().length > 0) {
      this.sentenceQueue.push(remaining.trim());
    }

    // 存入 messages 数组（用于历史记录和重新渲染）
    if (this.streamingContent) {
      this.messages.push({
        role: "ai",
        content: this.streamingContent,
        timestamp: new Date().toISOString(),
        origin_channel: this.streamingOrigin,
      });
    }

    this.streamingContent = "";
    this.outputSentenceCount = 0;
    this.streamingDraftItems = [];
  }

  /**
   * 作废历史加载版本并清空当前会话视图。
   */
  clear(): void {
    this.historyRequest += 1;
    this.historyRevision += 1;
    this.clearPendingSteer();
    this.clearFailedInput();
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

    this.outputTimer = setInterval(/**
     * 句子输出定时器到期时追加下一句，让最终回复继续按间隔显示。
     */
    () => {
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
   * 新一轮开始前完整显示上一轮已经结束但尚未输出的句子。
   */
  private flushFinalizedQueue(): void {
    while (this.sentenceQueue.length > 0) {
      const sentence = this.sentenceQueue.shift();
      if (sentence) {
        this.appendSentenceBubble(sentence);
        this.scrollToBottom();
      }
    }
  }

  /**
   * 追加流式句子气泡，仅首个可见气泡展示外部渠道来源。
   *
   * Args:
   *     sentence (string): 已经切分好的可见句子。
   */
  private appendSentenceBubble(sentence: string): void {
    const item = document.createElement("div");
    item.className = "message-item ai";

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    // 1. 外部渠道来源只在本轮首个句子气泡显示，后续句子沿用同一来源。
    if (!this.streamLabelShown && this.streamingOrigin !== DESKTOP_CHANNEL) {
      const label = document.createElement("span");
      label.className = "message-origin-label";
      label.textContent = `Ayaya · ${this.streamingOrigin}`;
      bubble.appendChild(label);
      this.streamLabelShown = true;
    }
    const text = document.createElement("span");
    text.textContent = removeTrailingPeriod(sentence);
    bubble.appendChild(text);

    item.appendChild(bubble);
    this.container.appendChild(item);
    if (this.isStreaming) {
      this.streamingDraftItems.push(item);
    }
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
    this.renderFailedInput();
    // 渲染完成后移除禁用动画的类
    requestAnimationFrame(/**
     * 历史整体重绘后恢复气泡动画，避免历史消息入场动画干扰初始布局。
     */
    () => {
      this.container.classList.remove("no-animation");
    });
  }

  /**
   * 根据角色、文本和附件渲染历史项，并标记非桌面来源。
   *
   * Args:
   *     msg (ChatHistoryItem): 历史消息正文、角色和附件。
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

    // 清理表情标签后再分割成句子
    const cleanContent = this.cleanExpressionTags(msg.content);
    const sentences = this.splitIntoSentences(cleanContent);

    // 1. 先渲染文本气泡（如果有文本）
    for (const [index, sentence] of sentences.entries()) {
      if (!sentence) continue;

      const item = document.createElement("div");
      item.className = `message-item ${isHuman ? "human" : "ai"}`;

      const bubble = document.createElement("div");
      bubble.className = "message-bubble";

      if (index === 0 && msg.origin_channel && msg.origin_channel !== DESKTOP_CHANNEL) {
        const label = document.createElement("span");
        label.className = "message-origin-label";
        label.textContent = isHuman
          ? `你 · ${msg.origin_channel}`
          : `Ayaya · ${msg.origin_channel}`;
        bubble.appendChild(label);
      }

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
          img.src = `ayaya-image://local/${encodeURIComponent(imageData)}`;
        }

        img.alt = "图片";
        img.loading = "lazy";
        img.style.cursor = "pointer";

        // 点击打开独立预览窗口
        img.addEventListener("click", /**
         * 用户点击聊天图片时请求主进程打开该图片预览。
         */
        () => {
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
   *
   * Args:
   *     text (string): 本次需要展示或输入的文本。
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

    /**
     * 图片加载回调触发时检查本批图片是否均已结束加载，再滚动到最新消息。
     */
    const checkAllLoaded = () => {
      loadedCount++;
      if (loadedCount >= totalCount) {
        // 所有图片加载完成，滚动到底部
        setTimeout(/**
         * 本批图片全部加载或失败后重新滚到最新消息。
         */
        () => this.scrollToBottom(), 50);
      }
    };

    images.forEach(/**
     * 逐张检查历史图片，已完成的立即计数，其余同时监听成功与失败，任一都结束该图等待。
     *
     * Args:
     *     img (HTMLImageElement): 聊天列表中需要等待加载或预览的图片元素。
     */
    (img) => {
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
    setTimeout(/**
     * 图片等待的兜底定时器到期后再次滚到底部，避免长期未完成图片阻止显示最新消息。
     */
    () => {
      this.scrollToBottom();
    }, 1000);
  }
}
