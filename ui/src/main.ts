/**
 * 渲染进程主窗口入口
 */

import * as PIXI from "pixi.js";
import "./style.css";

import { getMainUiElements } from "./ui/dom.js";
import { Live2DModelLoader, clamp } from "./live2d/model-loader.js";
import { GazeTracker, createGazeUpdateFunction } from "./live2d/gaze-tracker.js";
import { PointerInteractiveManager } from "./live2d/pointer-interactive.js";
import { BubbleManager } from "./chat/bubble.js";
import { RuntimeChatClient as ChatClient } from "./chat/runtime-chat-client.js";
import { ChatHistoryManager } from "./chat/chat-history-manager.js";
import { ImageManager } from "./chat/image-manager.js";
import { setupDropHandler } from "./chat/drop-handler.js";
import type { MotionConfig } from "./types.js";

// 全局 PIXI 引用（Live2D 需要）
(globalThis as unknown as { PIXI: typeof PIXI }).PIXI = PIXI;

/**
 * 装配桌面 Renderer 的模型、输入、附件和运行时视图。
 */
class MainApp {
  private elements = getMainUiElements();
  private app: PIXI.Application;
  private modelLoader: Live2DModelLoader;
  private gazeTracker = new GazeTracker();
  private pointerManager: PointerInteractiveManager;
  private bubbleManager: BubbleManager;
  private chatHistoryManager: ChatHistoryManager;
  private chatClient: ChatClient;
  private imageManager = new ImageManager(5);
  private cleanupDropHandler: (() => void) | null = null;
  private modelInfo: Awaited<ReturnType<Live2DModelLoader["loadModel"]>> | null = null;
  private live2dModel: unknown = null;
  private motionConfig: MotionConfig[] = [];
  private currentSessionId = "";
  private activeModelId = "";
  private userScale = 1;
  private offsetX = 0;
  private offsetY = 0;
  private followCursor = true;
  private persistTimer: ReturnType<typeof setTimeout> | null = null;

  // 动作队列
  private motionQueue: string[] = [];
  private isPlayingMotion = false;

  /**
   * 装配 MainApp 的依赖并初始化本地视图状态。
   */
  constructor() {
    this.app = Live2DModelLoader.createApp(this.elements.stageHost);
    this.modelLoader = new Live2DModelLoader(this.app, this.elements.stageHost);
    this.pointerManager = new PointerInteractiveManager(this.app);
    this.bubbleManager = new BubbleManager(this.elements.bubble);
    this.chatHistoryManager = new ChatHistoryManager(this.elements.chatHistoryList);
    this.chatClient = new ChatClient({
      bubble: this.bubbleManager,
      chatHistory: this.chatHistoryManager,
      sendBtn: this.elements.sendBtn,
      input: this.elements.input,
      sessionId: "",
      /**
       * 检查图片附件是否构成有效草稿。
       *
       * Returns:
       *     boolean: 当前图片草稿至少有一项附件时为 true。
       */
      hasAttachments: () => this.imageManager.getDataUrls().length > 0,
      /**
       * 聊天轮次完成后清空本次图片草稿，附件变化监听随后刷新预览和发送按钮。
       */
      onChatComplete: () => {
        // 聊天完成后清空图片
        this.imageManager.clear();
      },
    });
  }

  /**
   * 初始化应用
   */
  async init(): Promise<void> {
    this.setupEventListeners();

    try {
      await this.loadModel();
    } catch (error) {
      this.handleModelLoadFailure(error);
    }
  }

  /**
   * 连接草稿、附件、IME 和排队动作，Enter 只在非组合输入期间提交。
   */
  private setupEventListeners(): void {
    // 设置按钮
    this.elements.settingsBtn.addEventListener("click", /**
     * 用户点击设置入口时请求主进程打开设置窗口。
     */
    () => {
      window.desktopPetApi.openSettingsWindow();
    });

    // 输入框自动调整高度
    this.elements.input.addEventListener("input", /**
     * 聊天草稿变化后调整输入框高度并刷新发送按钮状态。
     */
    () => {
      this.autoResizeInput();
      this.chatClient.updateSendButton();
    });

    // 输入框键盘事件：Enter 发送，Shift+Enter 换行
    this.elements.input.addEventListener("keydown", /**
     * 用户按 Enter 且不在输入法组合中时提交表单，Shift+Enter 仍保留换行。
     *
     * Args:
     *     event (KeyboardEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        this.elements.form.requestSubmit();
      }
    });

    // 滚轮缩放
    window.addEventListener(
      "wheel",
      /**
       * 聊天历史和输入框上的滚轮交给原控件；其他区域按模型交互规则处理滚动。
       *
       * Args:
       *     event (WheelEvent): 调用方或浏览器触发的当前事件。
       */
      (event) => {
        const hoveredElement = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
        // 如果在聊天历史列表或输入框上，不拦截事件，让它正常滚动
        if (hoveredElement?.closest("#chat-history-region, #chat-input")) {
          return;
        }

        if (!this.pointerManager.isCursorOnChatControls(event.clientX, event.clientY)) {
          return;
        }

        event.preventDefault();
        const factor = event.deltaY < 0 ? 1.08 : 0.92;
        this.userScale = clamp(this.userScale * factor, 0.5, 3.0);
        this.modelInfo?.applyTransform({ userScale: this.userScale });
        this.persistTransform();
      },
      { passive: false }
    );

    // 窗口大小变化
    window.addEventListener("resize", /**
     * 窗口尺寸变化后重新适配当前模型显示。
     */
    () => {
      this.modelInfo?.fitModel();
    });

    // 图片选择按钮
    this.elements.imageBtn.addEventListener("click", /**
     * 用户选择图片后只把有效返回图片加入草稿附件。
     */
    async () => {
      const images = await window.desktopPetApi.selectImages();
      if (images && images.length > 0) {
        this.imageManager.addDataUrls(images.map(/**
         * 提取图片选择结果的 data URL，供附件管理器使用。
         *
         * Args:
         *     img ({ path: string; dataUrl: string; }): 当前等待加载的聊天图片元素。
         *
         * Returns:
         *     string: 图片地址。
         */
        (img) => img.dataUrl));
      }
    });

    // 图片输入框变化
    this.elements.imageInput.addEventListener("change", /**
     * 文件输入框选择变化后读取附件，结束后清空选择值，允许再次选择同一文件。
     */
    async () => {
      const files = this.elements.imageInput.files;
      if (files && files.length > 0) {
        await this.imageManager.addFiles(files);
      }
      this.elements.imageInput.value = "";
    });

    // 清空图片按钮
    this.elements.clearImagesBtn.addEventListener("click", /**
     * 用户清空附件时同步清空草稿图片集合。
     */
    () => {
      this.imageManager.clear();
    });

    // 图片状态变化监听
    this.imageManager.subscribe(/**
     * 草稿附件变化后刷新图片预览和发送按钮。
     */
    () => {
      this.updateImagePreview();
      this.chatClient.updateSendButton();
    });
    // 1. 输入法组合事件只同步发送状态，Enter 处理另检查 isComposing。
    this.elements.input.addEventListener("compositionstart", /**
     * 输入法组合开始时禁止把 Enter 当作发送命令。
     */
    () => this.chatClient.setComposing(true));
    this.elements.input.addEventListener("compositionend", /**
     * 输入法组合结束时恢复正常发送按钮及按键处理。
     */
    () => this.chatClient.setComposing(false));

    // 使用事件委托处理图片删除按钮点击
    this.elements.imagePreviewList.addEventListener("click", /**
     * 用户点击图片预览中的移除按钮时按附件编号删除该图。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const target = event.target as HTMLElement;
      const removeBtn = target.closest(".remove-btn") as HTMLButtonElement | null;
      if (removeBtn) {
        const id = removeBtn.dataset.imageId;
        if (id) {
          this.imageManager.removeImage(id);
        }
      }
    });

    this.elements.form.addEventListener("submit", /**
     * 用户提交聊天表单时阻止默认刷新，并将当前正文与附件交给统一发送命令。
     *
     * Args:
     *     event (SubmitEvent): 调用方或浏览器触发的当前事件。
     */
    async (event) => {
      event.preventDefault();
      const images = this.imageManager.getDataUrls();
      await this.chatClient.sendMessage(images.length > 0 ? images : undefined);
    });

    // 2. 排队提交与普通发送共用文本和附件来源，由后端决定交付时机。
    document.getElementById("queue-btn")?.addEventListener("click", /**
     * 用户点击排队发送时提交当前正文及附件，等待后端返回受理回执。
     */
    async () => {
      const images = this.imageManager.getDataUrls();
      await this.chatClient.queueMessage(images.length > 0 ? images : undefined);
    });

    // 模型变化监听
    const unsubscribeModelChanged = window.desktopPetApi?.onModelChanged?.(/**
     * 活动模型变化后重新加载主页面，切换到新模型及其会话。
     */
    () => {
      globalThis.location.reload();
    });

    // 光标位置监听
    const unsubscribeCursor = window.desktopPetApi?.onCursor?.(/**
     * 鼠标位置事件到达后更新目光目标，并按当前模型区域判断窗口交互。
     *
     * Args:
     *     cursorData (CursorSyncData): 主进程转发的鼠标位置和显示器信息。
     */
    (cursorData) => {
      if (this.followCursor && this.modelInfo) {
        this.gazeTracker.updateTarget(cursorData, this.elements.stageHost, this.followCursor);
      }
      if (this.modelInfo) {
        this.pointerManager.updatePointerInteractive(
          cursorData.localX,
          cursorData.localY,
          this.modelInfo.getModelBounds()
        );
      }
    });

    // 模型变换监听
    const unsubscribeModelTransformChanged = window.desktopPetApi?.onModelTransformChanged?.(
      /**
       * 模型显示参数事件到达后仅更新当前活动模型，其他模型的迟到事件忽略。
       *
       * Args:
       *     payload (ModelTransformData): 已经过入口校验的桌面动作参数。
       */
      (payload) => {
        if (payload.id !== this.activeModelId) {
          return;
        }

        this.offsetX = payload.offsetX;
        this.offsetY = payload.offsetY;
        this.userScale = payload.userScale;
        this.followCursor = payload.followCursor;
        this.modelInfo?.applyTransform({
          offsetX: this.offsetX,
          offsetY: this.offsetY,
          userScale: this.userScale,
        });
      }
    );

    // 动作播放请求监听（从设置窗口发来）
    const unsubscribePlayMotion = window.desktopPetApi?.onPlayMotionRequest?.(/**
     * 收到设置页动作预览请求后按名称播放当前模型动作。
     *
     * Args:
     *     motionName (string): 需要播放或保存配置的动作名称。
     */
    (motionName) => {
      this.playMotionByName(motionName);
    });

    // 动作配置变化监听
    const unsubscribeMotionConfigChanged = window.desktopPetApi?.onMotionConfigChanged?.(
      /**
       * 收到当前模型动作配置后更新本地规则并重新选择空闲动作。
       *
       * Args:
       *     payload ({ modelId: string; motionConfig: MotionConfig[]; }): 已经过入口校验的桌面动作参数。
       */
      (payload) => {
        if (payload.modelId !== this.activeModelId) {
          return;
        }
        this.motionConfig = payload.motionConfig;
        this.setIdleMotion();
      }
    );

    window.addEventListener("beforeunload", /**
     * 主页面卸载时逐项取消模型、鼠标、显示参数、动作请求及配置订阅。
     */
    () => {
      if (typeof unsubscribeModelChanged === "function") {
        unsubscribeModelChanged();
      }
      if (typeof unsubscribeCursor === "function") {
        unsubscribeCursor();
      }
      if (typeof unsubscribeModelTransformChanged === "function") {
        unsubscribeModelTransformChanged();
      }
      if (typeof unsubscribePlayMotion === "function") {
        unsubscribePlayMotion();
      }
      if (typeof unsubscribeMotionConfigChanged === "function") {
        unsubscribeMotionConfigChanged();
      }
      if (this.persistTimer) {
        clearTimeout(this.persistTimer);
      }
      if (this.cleanupDropHandler) {
        this.cleanupDropHandler();
      }
    });
  }

  /**
   * 切换模型与会话，历史恢复交给运行时快照，最近回复只用于启动气泡。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async loadModel(): Promise<void> {
    this.pointerManager.forceWindowInteractive();
    const activeModel = await window.desktopPetApi.getActiveModel();

    this.activeModelId = activeModel.id;
    this.userScale = activeModel.userScale;
    this.offsetX = activeModel.offsetX;
    this.offsetY = activeModel.offsetY;
    this.followCursor = activeModel.followCursor;

    this.modelInfo = await this.modelLoader.loadModel(activeModel);
    this.live2dModel = this.modelInfo.model;

    // 初始缩放
    this.modelInfo.fitModel();
    setTimeout(/**
     * 模型容器尺寸变化后重新适配当前模型。
     */
    () => this.modelInfo?.fitModel(), 120);

    // 注册视线跟随到 ticker
    const gazeUpdate = createGazeUpdateFunction(this.modelInfo.model, this.gazeTracker);
    this.app.ticker.add(gazeUpdate, undefined, PIXI.UPDATE_PRIORITY.LOW);

    // 设置拖拽处理器
    this.cleanupDropHandler = setupDropHandler({
      stageHost: this.elements.stageHost,
      form: this.elements.form,
      imageManager: this.imageManager,
      dragOverlay: this.elements.dragOverlay,
    });

    // 更新会话 ID
    this.currentSessionId = activeModel.sessionId;
    this.chatClient.setSessionId(activeModel.sessionId);

    // 加载动作配置并设置空闲动作
    if (activeModel.motionConfig) {
      this.motionConfig = activeModel.motionConfig;
    } else {
      // 尝试从后端获取配置
      try {
        this.motionConfig = await window.desktopPetApi.getMotionConfig?.(this.activeModelId) ?? [];
      } catch {
        this.motionConfig = [];
      }
    }
    this.setIdleMotion();

    // 历史和气泡由桌面事件流统一装载，不再单独查询最后一条回答。
  }

  /**
   * 设置空闲动作
   */
  private setIdleMotion(): void {
    const idleConfig = this.motionConfig.find(/**
     * 从模型动作规则中寻找唯一空闲动作。
     *
     * Args:
     *     c (MotionConfig): 当前动作的配置项。
     *
     * Returns:
     *     boolean: 当前规则的设置为 idle 时为 true。
     */
    (c) => c.setting === "idle");
    const idleMotionName = idleConfig?.motionName || "Idle";

    // 设置模型的空闲动作组
    // 注意：需要修改 groups.idle 而不是 idleMotionGroup（后者只是初始化选项）
    if (this.live2dModel && (this.live2dModel as any).internalModel?.motionManager) {
      const motionManager = (this.live2dModel as any).internalModel.motionManager;
      if (motionManager.groups) {
        motionManager.groups.idle = idleMotionName;
      }
    }

    // 使用 IDLE 优先级播放空闲动作
    this.playMotionByName(idleMotionName, 1); // MotionPriority.IDLE = 1
  }

  /**
   * 通过名称播放动作
   *
   * Args:
   *     motionName (string): 动作名称（组名）
   *     priority (number): 优先级：1=IDLE, 2=NORMAL, 3=FORCE
   */
  private playMotionByName(motionName: string, priority: number = 2): void {
    if (!this.live2dModel || typeof (this.live2dModel as any).motion !== "function") {
      return;
    }
    try {
      (this.live2dModel as any).motion(motionName, undefined, priority);
    } catch (error) {
      console.error(`Failed to play motion ${motionName}:`, error);
    }
  }

  /**
   * 通过标签播放动作（加入队列）
   *
   * Args:
   *     label (string): 当前工具、表情或动作的显示名称。
   */
  playMotionByLabel(label: string): void {
    const config = this.motionConfig.find(/**
     * 按表情标签查找被声明为表情的对应动作。
     *
     * Args:
     *     c (MotionConfig): 当前动作的配置项。
     *
     * Returns:
     *     boolean: 规则类型和标签都匹配时为 true。
     */
    (c) => c.setting === "expression" && c.label === label);
    if (config) {
      this.queueMotion(config.motionName);
    }
  }

  /**
   * 添加动作到队列
   *
   * Args:
   *     motionName (string): 需要播放或保存规则的模型动作名称。
   */
  private queueMotion(motionName: string): void {
    this.motionQueue.push(motionName);
    if (!this.isPlayingMotion) {
      this.playNextMotion();
    }
  }

  /**
   * 播放下一个动作
   */
  private async playNextMotion(): Promise<void> {
    if (this.motionQueue.length === 0) {
      this.isPlayingMotion = false;
      // 队列结束后重新设置空闲动作
      this.setIdleMotion();
      return;
    }

    this.isPlayingMotion = true;
    const motionName = this.motionQueue.shift()!;

    // 播放动作并等待 Promise 完成
    // 使用 FORCE 优先级 (3) 确保覆盖空闲动作和其他状态
    try {
      await (this.live2dModel as any).motion(motionName, undefined, 3);
    } catch (error) {
      console.error(`Failed to play motion ${motionName}:`, error);
    }

    // 等待动作播放一段时间
    await new Promise(/**
     * Promise 创建时安排短暂状态恢复等待，到期后继续动作处理。
     *
     * Args:
     *     resolve ((value: unknown) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    resolve => setTimeout(resolve, 2000));

    this.playNextMotion();
  }

  /**
   * 获取表达式标签列表
   */
  getExpressionLabels(): string[] {
    return this.motionConfig
      .filter(/**
       * 只保留带有标签的表情动作，供模型提示词声明可用表情。
       *
       * Args:
       *     c (MotionConfig): 当前动作的配置项。
       *
       * Returns:
       *     string | false | undefined: 规则为表情且标签非空时为 true。
       */
      (c) => c.setting === "expression" && c.label)
      .map(/**
       * 提取已筛选表情动作的标签名称。
       *
       * Args:
       *     c (MotionConfig): 当前动作的配置项。
       *
       * Returns:
       *     string: 当前表情标签。
       */
      (c) => c.label!);
  }

  /**
   * 处理模型加载失败
   *
   * Args:
   *     error (unknown): 本次捕获的异常。
   */
  private handleModelLoadFailure(error: unknown): void {
    this.pointerManager.forceWindowInteractive();
    const message = error instanceof Error ? error.message : String(error);
    this.bubbleManager.setText(`模型加载失败，请在设置-模型管理中重选模型（${message}）`);
    this.elements.sendBtn.disabled = true;

    if (
      window.desktopPetApi &&
      typeof window.desktopPetApi.openSettingsWindow === "function"
    ) {
      window.desktopPetApi.openSettingsWindow();
    }
  }

  /**
   * 持久化变换
   */
  private persistTransform(): void {
    if (this.persistTimer) {
      clearTimeout(this.persistTimer);
    }

    this.persistTimer = setTimeout(/**
     * 模型拖动位置或缩放稳定后保存当前显示参数，再清空本次持久化定时标记。
     */
    () => {
      void window.desktopPetApi.updateModelTransform({
        modelId: this.activeModelId,
        offsetX: this.offsetX,
        offsetY: this.offsetY,
        userScale: this.userScale,
      });
      this.persistTimer = null;
    }, 120);
  }

  /**
   * 自动调整输入框高度
   */
  private autoResizeInput(): void {
    const input = this.elements.input;
    // 如果没有内容，重置为默认高度
    if (!input.value) {
      input.style.height = "44px";
      return;
    }
    // 先重置高度以获取正确的 scrollHeight
    input.style.height = "auto";
    // 设置新高度，限制在 44px 和 max-height 之间
    const newHeight = Math.min(input.scrollHeight, 120);
    input.style.height = `${newHeight}px`;
  }

  /**
   * 更新图片预览
   */
  private updateImagePreview(): void {
    const images = this.imageManager.getImages();
    const container = this.elements.imagePreviewContainer;
    const list = this.elements.imagePreviewList;

    if (images.length === 0) {
      container.hidden = true;
      list.innerHTML = "";
      return;
    }

    container.hidden = false;
    list.innerHTML = "";

    for (const image of images) {
      const item = document.createElement("div");
      item.className = "image-preview-item";

      const img = document.createElement("img");
      img.src = image.dataUrl;
      img.alt = "预览";

      const removeBtn = document.createElement("button");
      removeBtn.className = "remove-btn";
      removeBtn.type = "button";
      removeBtn.textContent = "×";
      removeBtn.dataset.imageId = image.id;

      item.appendChild(img);
      item.appendChild(removeBtn);
      list.appendChild(item);
    }
  }
}

/**
 * 截屏或屏幕操作时需要隐藏的元素选择器
 */
const HIDE_ELEMENTS_ON_SCREENSHOT = [
  "#live2d-stage",
  "#chat-form",
  "#bubble",
  "#chat-history-list",
  "#tool-call-toasts",
  "#image-preview-container",
];

/**
 * 被隐藏的元素列表（用于恢复）
 */
let hiddenElementsByScreenshot: HTMLElement[] = [];

/**
 * 隐藏界面元素（截屏或屏幕操作前调用）
 */
window.hideElementsForScreenshot = /**
 * 截图准备阶段只隐藏当前可见的桌面组件，并记录本次实际隐藏的元素。
 */
() => {
  hiddenElementsByScreenshot = [];
  for (const selector of HIDE_ELEMENTS_ON_SCREENSHOT) {
    const el = document.querySelector<HTMLElement>(selector);
    if (el && !el.hidden) {
      el.hidden = true;
      hiddenElementsByScreenshot.push(el);
    }
  }
};

/**
 * 恢复界面元素（截屏或屏幕操作后调用）
 */
window.restoreElementsAfterScreenshot = /**
 * 截图结束后恢复本次隐藏的元素，清空记录以免影响后续截图。
 */
() => {
  for (const el of hiddenElementsByScreenshot) {
    el.hidden = false;
  }
  hiddenElementsByScreenshot = [];
};

// 全局主应用实例引用
let mainAppInstance: MainApp | null = null;

/**
 * 通过标签播放动作（供聊天模块调用）
 */
window.playMotionByLabel = /**
 * 聊天回复中识别到表情标签后按标签播放当前模型动作。
 *
 * Args:
 *     label (string): 当前工具、表情或动作的显示名称。
 */
(label: string) => {
  mainAppInstance?.playMotionByLabel(label);
};

/**
 * 获取表达式标签列表（供聊天模块调用）
 */
window.getExpressionLabels = /**
 * 向聊天解析器提供当前模型允许使用的表情标签，没有模型时返回空列表。
 *
 * Returns:
 *     string[]: 当前模型表情标签列表。
 */
() => {
  return mainAppInstance?.getExpressionLabels() ?? [];
};

// 启动应用
mainAppInstance = new MainApp();
void mainAppInstance.init();
