/** 独立聊天窗口入口，关闭与最小化不卸载草稿和消息订阅。 */
import { MotionSettingType } from "../../shared-types.js";
import "../style.css";
import { getMainUiElements } from "../ui/dom.js";
import { iconMarkup, initializeIcons } from "../ui/icons.js";
import { BubbleManager } from "./bubble.js";
import { RuntimeChatClient } from "./runtime-chat-client.js";
import { ChatHistoryManager } from "./chat-history-manager.js";
import { PluginStatusNotices } from "./plugin-status-notices.js";
import { ImageManager } from "./image-manager.js";
import { setupDropHandler } from "./drop-handler.js";
import { setupWindowGestures } from "../ui/window-gestures.js";
import { setupApplicationShutdownUi } from "../ui/application-shutdown.js";
import { confirmApplicationExit } from "../ui/application-exit.js";
import { ConfirmDialog } from "../ui/confirm-dialog.js";
import type { ModelInfo } from "../types.js";
import { DEFAULT_AI_NAME, resolveAiName } from "../../ai-identity.js";

/**
 * 装配独立聊天窗口的输入、附件、会话与标题栏。
 * Attributes:
 *     cleanup (() => void)[]: 页面卸载时释放的图片、模型和窗口监听。
 *     aiNameRevision (number): 当前 AI 名字查询次数；切换会话或保存新名字后，旧查询不能覆盖标题。
 *     exitDialog (ConfirmDialog): 聊天窗口独立的退出确认框，与工具审批框分别处理用户选择。
 */
class ChatApp {
  private readonly elements = getMainUiElements();
  private readonly images = new ImageManager(5);
  private readonly history = new ChatHistoryManager(this.elements.chatHistoryList);
  private readonly bubble = new BubbleManager(this.elements.bubble);
  private readonly client: RuntimeChatClient;
  private readonly exitDialog: ConfirmDialog;
  private readonly cleanup: Array<() => void> = [];
  private model: ModelInfo | null = null;
  private modelRevision = 0;
  private aiNameRevision = 0;
  private lastModelError = "";

  /** 初始化图标和聊天客户端，随后绑定输入；会话在 init 中独立查询。 */
  constructor() {
    initializeIcons();
    this.exitDialog = new ConfirmDialog(
      document.getElementById("chat-exit-dialog") as HTMLDivElement,
      document.getElementById("chat-exit-cancel") as HTMLButtonElement,
      document.getElementById("chat-exit-confirm") as HTMLButtonElement,
      document.getElementById("chat-exit-title")!,
      document.getElementById("chat-exit-message")!,
    );
    this.client = new RuntimeChatClient({
      bubble: this.bubble, chatHistory: this.history,
      sendBtn: this.elements.sendBtn, input: this.elements.input, sessionId: "",
      statusNotices: new PluginStatusNotices(document.getElementById("plugin-status-notices")!),
      /** 判断纯图片草稿是否可以发送。 */
      hasAttachments: () => this.images.getDataUrls().length > 0,
      /** 命令受理后清除本次图片，失败时客户端不调用此回调。 */
      onChatComplete: () => this.images.clear(),
      /**
       * 运行事件通过会话和序号核对后，将提醒送给主进程。
       * Args:
       *     update (ChatAttentionUpdate): 新正文或当前待审批请求。
       */
      onAttention: (update) => window.desktopPetApi.reportChatAttention(update),
    });
    this.bindEvents();
  }

  /** 查询当前模型身份和保存的置顶状态，聊天不等待模型绘制完成。 */
  async init(): Promise<void> {
    try {
      const model = await window.desktopPetApi.getActiveModel();
      if (!this.model) this.applyModel(model);
      const settings = await window.desktopPetApi.getFrontendSettings();
      this.showPinned(settings.chat_window_pinned === true);
    } catch (error) {
      this.history.showErrorMessage(`聊天初始化失败：${String(error)}`);
    }
  }

  /**
   * 应用活动模型身份，切换会话时清理原会话草稿，动作规则异步补齐。
   * Args:
   *     model (ModelInfo): 主进程返回的当前模型和会话记录。
   */
  private applyModel(model: ModelInfo): void {
    // 1. 已知会话真正改变时清除旧草稿；首次模型查询不清除用户在启动等待期间输入的内容。
    if (this.model && this.model.sessionId !== model.sessionId) {
      this.elements.input.value = "";
      this.images.clear();
    }
    if (this.model && this.model.id !== model.id) {
      this.lastModelError = "";
      document.getElementById("model-status")!.hidden = true;
    }
    // 2. 先更新界面身份，再让客户端查询对应会话快照和历史。
    this.model = model;
    this.showAiName(DEFAULT_AI_NAME);
    void this.refreshAiName(model.sessionId);
    this.client.setSessionId(model.sessionId);
    // 3. 动作规则读取可以晚于聊天初始化；旧模型的迟到结果不能覆盖新模型。
    const revision = ++this.modelRevision;
    void window.desktopPetApi.getMotionConfig(model.id).then(/**
     * 模型动作规则读取完成后只更新仍属于该模型的配置。
     * Args:
     *     motionConfig (MotionConfig[]): 当前模型表情规则。
     */
    (motionConfig) => { if (revision === this.modelRevision && this.model) this.model.motionConfig = motionConfig; }).catch(/** 动作配置读取失败不阻塞正文与输入。 */
    () => {});
  }

  /**
   * 刷新置顶按钮状态，使用共享按钮选中效果。
   * Args:
   *     pinned (boolean): 主进程应用的置顶状态。
   */
  private showPinned(pinned: boolean): void {
    const button = document.getElementById("chat-pin-btn")!;
    button.setAttribute("aria-pressed", String(pinned));
    button.setAttribute("aria-label", pinned ? "取消聊天窗口置顶" : "置顶聊天窗口");
    button.title = pinned ? "取消聊天窗口置顶" : "置顶聊天窗口";
  }

  /**
   * 更新聊天页面标题，使用 LLM 配置的 AI 名字，不使用 Live2D 资源名。
   *
   * Args:
   *     name (string): 当前会话已保存的 AI 名字。
   */
  private showAiName(name: string): void {
    const displayName = resolveAiName(name);
    document.getElementById("chat-title")!.textContent = displayName;
    document.title = `${displayName} 聊天`;
  }

  /**
   * 模型切换后读取会话对应的已保存 AI 名字，忽略切换会话或新名字保存后的迟到结果。
   *
   * Args:
   *     sessionId (string): 本次标题查询所属的会话编号。
   *
   * Returns:
   *     Promise<void>: 查询完成；失败时保留默认标题并显示错误提示。
   */
  private async refreshAiName(sessionId: string): Promise<void> {
    const revision = ++this.aiNameRevision;
    try {
      const names = await window.desktopPetApi.getAiNames([sessionId]);
      // 1. 保存名字的通知也会增加次数，因此晚到的旧查询不会把新名字改回去。
      if (this.model?.sessionId === sessionId && revision === this.aiNameRevision) {
        if (names[sessionId]) this.showAiName(names[sessionId]);
      }
    } catch (error) {
      if (this.model?.sessionId === sessionId && revision === this.aiNameRevision) {
        this.history.showErrorMessage(`读取 AI 名字失败：${String(error)}`);
      }
    }
  }

  /** 绑定窗口、输入法和附件操作，所有发送共用客户端命令入口。 */
  private bindEvents(): void {
    this.cleanup.push(setupWindowGestures(document.getElementById("chat-window")!));
    // 1. 标题栏动作只交给主进程，不重新加载聊天页面。
    this.elements.settingsBtn.addEventListener("click", /** 打开设置，保持聊天窗口实例。 */
    () => window.desktopPetApi.openSettingsWindow());
    document.getElementById("chat-min-btn")!.addEventListener("click", /** 请求主进程最小化当前聊天窗口。 */
    () => window.desktopPetApi.minimizeCurrentWindow());
    document.getElementById("chat-exit-btn")!.addEventListener("click", /**
     * 用户点击退出图标后执行与设置页相同的确认流程；确认才锁定窗口并停止整个应用。
     */
    async () => { await confirmApplicationExit(this.exitDialog); });
    document.getElementById("chat-pin-btn")!.addEventListener("click", /** 保存成功后刷新置顶状态，失败时保留原状态并展示原因。 */
    async () => {
      const button = document.getElementById("chat-pin-btn") as HTMLButtonElement;
      button.disabled = true;
      try { this.showPinned(await window.desktopPetApi.setChatPinned(button.getAttribute("aria-pressed") !== "true")); }
      catch (error) { this.history.showErrorMessage(`保存置顶失败：${String(error)}`); }
      finally { button.disabled = false; }
    });
    // 2. Enter、输入法组合、普通发送和排队共用同一份正文与附件。
    this.elements.input.addEventListener("input", /** 草稿变化后调整高度并刷新发送或停止按钮。 */
    () => { this.resizeInput(); this.client.updateSendButton(); });
    this.elements.input.addEventListener("keydown", /**
     * Enter 只在输入法组合结束后发送，Shift+Enter 保留换行。
     * Args:
     *     event (KeyboardEvent): 当前按键事件。
     */
    (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault(); this.elements.form.requestSubmit();
      }
    });
    this.elements.input.addEventListener("compositionstart", /** 输入法组合期间暂停发送模式切换。 */
    () => this.client.setComposing(true));
    this.elements.input.addEventListener("compositionend", /** 输入法组合结束后恢复草稿状态计算。 */
    () => this.client.setComposing(false));
    this.elements.form.addEventListener("submit", /**
     * 阻止页面刷新并提交文本和当前附件。
     * Args:
     *     event (SubmitEvent): 用户提交事件。
     */
    async (event) => { event.preventDefault(); await this.client.sendMessage(this.images.getDataUrls()); });
    document.getElementById("queue-btn")!.addEventListener("click", /** 将当前草稿排队到下一轮，受理后由客户端清除。 */
    async () => { await this.client.queueMessage(this.images.getDataUrls()); });
    // 3. 图片选择、拖入和粘贴只更新附件草稿，受理成功后才清除。
    this.elements.imageBtn.addEventListener("click", /** 用户选择图片成功后加入当前附件草稿，清空或切换会话后忽略旧选择结果。 */
    async () => {
      await this.images.addFromSelection(/**
       * 附件管理器记录当前草稿版本后，调用主进程打开原生图片选择框。
       * Returns:
       *     Promise<string[] | null>: 已读取图片地址，取消选择时为 null。
       */
      async () => {
        const images = await window.desktopPetApi.selectImages();
        return images?.map(/**
         * 提取已读取的图片正文，附件管理器核对草稿版本后才加入预览。
         * Args:
         *     image ({ path: string; dataUrl: string }): 图片选择结果。
         * Returns:
         *     string: 图片数据地址。
         */
        (image) => image.dataUrl) ?? null;
      });
    });
    this.elements.imageInput.addEventListener("change", /** 读取文件输入后清空选择值，允许再次选择同一张图片。 */
    async () => { if (this.elements.imageInput.files) await this.images.addFiles(this.elements.imageInput.files); this.elements.imageInput.value = ""; });
    this.elements.clearImagesBtn.addEventListener("click", /** 清空草稿附件，不影响已保存的历史图片。 */
    () => this.images.clear());
    this.elements.imagePreviewList.addEventListener("click", /**
     * 按稳定附件编号移除图片，SVG 内部点击也能找到按钮。
     * Args:
     *     event (MouseEvent): 当前图片按钮点击。
     */
    (event) => {
      const button = (event.target as Element).closest<HTMLButtonElement>(".remove-btn");
      if (button?.dataset.imageId) this.images.removeImage(button.dataset.imageId);
    });
    this.cleanup.push(this.images.subscribe(/** 附件变化后同步预览和发送状态。 */
    () => { this.renderImages(); this.client.updateSendButton(); }));
    this.elements.root.addEventListener("paste", /**
     * 剪贴板包含图片时加入附件，文字粘贴继续由输入框原生处理。
     * Args:
     *     event (ClipboardEvent): 当前剪贴板粘贴事件。
     */
    (event) => {
      if (event.clipboardData?.files.length) void this.images.addFiles(event.clipboardData.files);
    });
    this.cleanup.push(setupDropHandler({ root: this.elements.root, imageManager: this.images, dragOverlay: this.elements.dragOverlay }));
    // 4. 模型错误不写入消息历史，避免后续历史刷新清掉仍需处理的错误提示。
    this.cleanup.push(window.desktopPetApi.onModelError(/**
     * 模型绘制失败只在聊天中展示错误，输入与设置仍可使用。
     * Args:
     *     message (string): 模型页面发来的错误。
     */
    (message) => {
      if (!message) { this.lastModelError = ""; document.getElementById("model-status")!.hidden = true; }
      else if (message !== this.lastModelError) {
        this.lastModelError = message;
        const status = document.getElementById("model-status")!;
        status.textContent = message;
        status.hidden = false;
      }
    }));
    const unsubscribeModel = window.desktopPetApi.onModelChanged?.(/**
     * 模型切换后更新当前聊天身份与会话。
     * Args:
     *     model (ModelInfo): 新模型身份。
     */
    (model) => this.applyModel(model));
    if (unsubscribeModel) this.cleanup.push(unsubscribeModel);
    const unsubscribeName = window.desktopPetApi.onAiNameChanged?.(/**
     * LLM 设置保存成功后立即更新当前会话的聊天标题，并使较早的名字查询失效。
     *
     * Args:
     *     payload (AiNameChangedPayload): 已保存名字及其会话编号。
     */
    (payload) => {
      if (this.model?.sessionId !== payload.sessionId) return;
      ++this.aiNameRevision;
      this.showAiName(payload.name);
    });
    if (unsubscribeName) this.cleanup.push(unsubscribeName);
    const unsubscribeMotion = window.desktopPetApi.onMotionConfigChanged?.(/**
     * 设置修改当前模型动作规则后更新标签清单。
     * Args:
     *     payload ({ modelId: string; motionConfig: MotionConfig[] }): 保存后的配置。
     */
    (payload) => { if (this.model?.id === payload.modelId) this.model.motionConfig = payload.motionConfig; });
    if (unsubscribeMotion) this.cleanup.push(unsubscribeMotion);
    // 5. 表情通过主进程转发给角色页面，聊天不再持有 Live2D 对象。
    window.playMotionByLabel = /**
     * 把识别到的表情交给主进程，避免聊天窗口直接操作角色对象。
     * Args:
     *     label (string): 正文中的表情标签。
     */
    (label) => window.desktopPetApi.playExpression(label);
    window.getExpressionLabels = /**
     * 返回当前模型允许显示的表情标签。
     * Returns:
     *     string[]: 当前表情标签清单。
     */
    () => (this.model?.motionConfig ?? []).flatMap(/**
     * 只返回表情规则中的非空标签。
     * Args:
     *     rule (MotionConfig): 当前动作规则。
     * Returns:
     *     string[]: 当前规则的标签或空列表。
     */
    (rule) => rule.setting === MotionSettingType.EXPRESSION && rule.label ? [rule.label] : []);
    // 6. 只有页面实际卸载才清理监听，窗口最小化保留所有草稿和会话状态。
    window.addEventListener("beforeunload", /** 页面实际卸载时释放监听，最小化不触发此清理。 */
    () => { for (const cleanup of this.cleanup) cleanup(); });
  }

  /** 随草稿内容调整输入框高度，最小和最大高度由共享控件及聊天页面 CSS 决定。 */
  private resizeInput(): void {
    this.elements.input.style.height = "auto";
    const style = getComputedStyle(this.elements.input);
    this.elements.input.style.height = `${Math.min(parseFloat(style.maxHeight), Math.max(parseFloat(style.minHeight), this.elements.input.scrollHeight))}px`;
  }

  /** 按附件编号重建预览，单项删除统一使用 SVG 和危险操作样式。 */
  private renderImages(): void {
    const images = this.images.getImages();
    this.elements.imagePreviewContainer.hidden = images.length === 0;
    this.elements.imagePreviewList.replaceChildren();
    for (const image of images) {
      const item = document.createElement("div"); item.className = "image-preview-item";
      const preview = document.createElement("img"); preview.src = image.dataUrl; preview.alt = "待发送图片";
      const button = document.createElement("button");
      button.type = "button"; button.className = "remove-btn ui-button ui-button--danger ui-button--icon";
      button.dataset.imageId = image.id; button.innerHTML = iconMarkup("close");
      button.setAttribute("aria-label", "移除此图片"); button.title = "移除此图片";
      item.append(preview, button); this.elements.imagePreviewList.append(item);
    }
  }
}

setupApplicationShutdownUi();
void new ChatApp().init();
