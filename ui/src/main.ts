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
  private chatClient: ChatClient;
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
    this.chatClient = new ChatClient({
      bubble: this.bubbleManager,
      sendBtn: this.elements.sendBtn,
      input: this.elements.input,
      sessionId: "",
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

    // 滚轮缩放
    window.addEventListener(
      "wheel",
      (event) => {
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

    // 表单提交
    this.elements.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (this.stopLatestAiMessageBootstrap) {
        this.stopLatestAiMessageBootstrap();
        this.stopLatestAiMessageBootstrap = null;
      }
      await this.chatClient.sendMessage();
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

    // 更新会话 ID
    this.currentSessionId = activeModel.sessionId;
    this.chatClient.setSessionId(activeModel.sessionId);

    // 启动最新消息加载
    this.stopLatestAiMessageBootstrap = startLatestAiMessageBootstrap(
      activeModel.sessionId,
      this.bubbleManager,
      () => this.chatClient.hasUserSubmitted()
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
}

// 启动应用
void new MainApp().init();
