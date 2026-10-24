/**
 * 渲染进程主窗口入口
 */
import { MotionSettingType } from "../shared-types.js";

import * as PIXI from "pixi.js";
import "./live2d/live2d.css";
import { setupApplicationShutdownUi } from "./ui/application-shutdown.js";


import { Live2DModelLoader, clamp } from "./live2d/model-loader.js";
import { GazeTracker, createGazeUpdateFunction } from "./live2d/gaze-tracker.js";
import { PointerInteractiveManager } from "./live2d/pointer-interactive.js";
import type { MotionConfig } from "./types.js";

setupApplicationShutdownUi();
// 全局 PIXI 引用（Live2D 需要）
(globalThis as unknown as { PIXI: typeof PIXI }).PIXI = PIXI;

/**
 * 装配固定桌面窗口中的 Live2D 模型、目光和动作。
 */
class MainApp {
  private elements = { stageHost: document.getElementById("live2d-stage") as HTMLDivElement };
  private app: PIXI.Application;
  private modelLoader: Live2DModelLoader;
  private gazeTracker = new GazeTracker();
  private pointerManager: PointerInteractiveManager;
  private modelInfo: Awaited<ReturnType<Live2DModelLoader["loadModel"]>> | null = null;
  private live2dModel: unknown = null;
  private motionConfig: MotionConfig[] = [];
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
   * 连接模型目光、缩放和跨窗口动作，窗口本身保持固定。
   */
  private setupEventListeners(): void {
    // 滚轮缩放
    window.addEventListener(
      "wheel",
      /**
       * 光标命中角色像素时调整模型缩放；透明区域不拦截桌面滚轮。
       *
       * Args:
       *     event (WheelEvent): 调用方或浏览器触发的当前事件。
       */
      (event) => {
        if (this.pointerManager.sampleCanvasAlpha(event.clientX, event.clientY) < 12) {
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

    const unsubscribeExpression = window.desktopPetApi.onExpressionRequest(/**
     * 聊天窗口识别表情且主进程核对配置后，按标签加入当前模型动作队列。
     * Args:
     *     label (string): 当前模型允许的表情标签。
     */
    (label) => this.playMotionByLabel(label));

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
      unsubscribeExpression();
    });
  }

  /**
   * 加载活动角色及显示参数，再安装目光与空闲动作；聊天会话由独立窗口装配。
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
    // 1. 清除聊天窗口中的模型错误提示；本窗口不读取消息或建立聊天订阅。
    window.desktopPetApi.reportModelError("");
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
    (c) => c.setting === MotionSettingType.IDLE);
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
    (c) => c.setting === MotionSettingType.EXPRESSION && c.label === label);
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
      (c) => c.setting === MotionSettingType.EXPRESSION && c.label)
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
    // 1. 角色加载失败不阻塞独立聊天页；让透明窗口继续穿透，避免挡住桌面。
    window.desktopPetApi.setPointerInteractive(false);
    const message = error instanceof Error ? error.message : String(error);
    window.desktopPetApi.reportModelError(`模型加载失败，请在设置中重选模型：${message}`);
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

}

/** Live2D 页面启动时只装配角色，消息与输入由独立聊天窗口负责。 */
const mainAppInstance = new MainApp();
void mainAppInstance.init();
