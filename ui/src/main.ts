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
import { ChatClient, startLatestAiMessageBootstrap } from "./chat/chat-client.js";
import { ChatHistoryManager } from "./chat/chat-history-manager.js";
import { ImageManager } from "./chat/image-manager.js";
import { setupDropHandler } from "./chat/drop-handler.js";

// 全局 PIXI 引用（Live2D 需要）
(globalThis as unknown as { PIXI: typeof PIXI }).PIXI = PIXI;

/**
 * 主应用
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
  private stopLatestAiMessageBootstrap: (() => void) | null = null;
  private currentSessionId = "";
  private activeModelId = "";
  private userScale = 1;
  private offsetX = 0;
  private offsetY = 0;
  private followCursor = true;
  private persistTimer: ReturnType<typeof setTimeout> | null = null;

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
   * 设置事件监听
   */
  private setupEventListeners(): void {
    // 设置按钮
    this.elements.settingsBtn.addEventListener("click", () => {
      window.desktopPetApi.openSettingsWindow();
    });

    // 输入框自动调整高度
    this.elements.input.addEventListener("input", () => {
      this.autoResizeInput();
    });

    // 输入框键盘事件：Enter 发送，Shift+Enter 换行
    this.elements.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        this.elements.form.requestSubmit();
      }
    });

    // 滚轮缩放
    window.addEventListener(
      "wheel",
      (event) => {
        const hoveredElement = document.elementFromPoint(event.clientX, event.clientY) as HTMLElement | null;
        // 如果在聊天历史列表或输入框上，不拦截事件，让它正常滚动
        if (hoveredElement?.closest("#chat-history-list, #chat-input")) {
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
    window.addEventListener("resize", () => {
      this.modelInfo?.fitModel();
    });

    // 图片选择按钮
    this.elements.imageBtn.addEventListener("click", async () => {
      const images = await window.desktopPetApi.selectImages();
      if (images && images.length > 0) {
        this.imageManager.addDataUrls(images.map((img) => img.dataUrl));
      }
    });

    // 图片输入框变化
    this.elements.imageInput.addEventListener("change", async () => {
      const files = this.elements.imageInput.files;
      if (files && files.length > 0) {
        await this.imageManager.addFiles(files);
      }
      this.elements.imageInput.value = "";
    });

    // 清空图片按钮
    this.elements.clearImagesBtn.addEventListener("click", () => {
      this.imageManager.clear();
    });

    // 图片状态变化监听
    this.imageManager.subscribe(() => {
      this.updateImagePreview();
    });

    // 使用事件委托处理图片删除按钮点击
    this.elements.imagePreviewList.addEventListener("click", (event) => {
      const target = event.target as HTMLElement;
      const removeBtn = target.closest(".remove-btn") as HTMLButtonElement | null;
      if (removeBtn) {
        const id = removeBtn.dataset.imageId;
        if (id) {
          this.imageManager.removeImage(id);
        }
      }
    });

    // 表单提交
    this.elements.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (this.stopLatestAiMessageBootstrap) {
        this.stopLatestAiMessageBootstrap();
        this.stopLatestAiMessageBootstrap = null;
      }
      const images = this.imageManager.getDataUrls();
      await this.chatClient.sendMessage(images.length > 0 ? images : undefined);
    });

    // 模型变化监听
    const unsubscribeModelChanged = window.desktopPetApi?.onModelChanged?.(() => {
      globalThis.location.reload();
    });

    // 光标位置监听
    const unsubscribeCursor = window.desktopPetApi?.onCursor?.((cursorData) => {
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

    // 页面卸载清理
    window.addEventListener("beforeunload", () => {
      if (this.stopLatestAiMessageBootstrap) {
        this.stopLatestAiMessageBootstrap();
        this.stopLatestAiMessageBootstrap = null;
      }

      if (typeof unsubscribeModelChanged === "function") {
        unsubscribeModelChanged();
      }
      if (typeof unsubscribeCursor === "function") {
        unsubscribeCursor();
      }
      if (typeof unsubscribeModelTransformChanged === "function") {
        unsubscribeModelTransformChanged();
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
   * 加载模型
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

    // 初始缩放
    this.modelInfo.fitModel();
    setTimeout(() => this.modelInfo?.fitModel(), 120);

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

    // 尝试加载聊天历史（后端未开启时会失败）
    let historyLoaded = false;
    try {
      await this.chatHistoryManager.loadHistory(activeModel.sessionId);
      historyLoaded = true;
    } catch {
      // 后端未开启，等待轮询成功后重试
    }

    // 启动最新消息加载（失败后会自动重试，成功后会加载聊天历史）
    this.stopLatestAiMessageBootstrap = startLatestAiMessageBootstrap(
      activeModel.sessionId,
      this.bubbleManager,
      () => this.chatClient.hasUserSubmitted(),
      () => {
        // 首次成功连接后端时，如果历史未加载则尝试加载
        if (!historyLoaded) {
          void this.chatHistoryManager.loadHistory(activeModel.sessionId);
        }
      }
    );
  }

  /**
   * 处理模型加载失败
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

    this.persistTimer = setTimeout(() => {
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
 * 截屏时需要隐藏的元素选择器
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
 * 隐藏界面元素（截屏前调用）
 */
window.hideElementsForScreenshot = () => {
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
 * 恢复界面元素（截屏后调用）
 */
window.restoreElementsAfterScreenshot = () => {
  for (const el of hiddenElementsByScreenshot) {
    el.hidden = false;
  }
  hiddenElementsByScreenshot = [];
};

// 启动应用
void new MainApp().init();
