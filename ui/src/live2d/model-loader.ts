/**
 * Live2D 模型加载和渲染
 */

import * as PIXI from "pixi.js";

import type { ModelInfo } from "../types.js";

/**
 * Live2D 模型加载器
 */
export class Live2DModelLoader {
  private app: PIXI.Application;
  private stageHost: HTMLDivElement;

  /**
   * 保存 Pixi 应用和模型容器，加载后按可用尺寸设置模型显示。
   *
   * Args:
   *     app (PIXI.Application): 渲染桌面模型的 Pixi 应用。
   *     stageHost (HTMLDivElement): 决定模型可用绘制尺寸的页面容器。
   */
  constructor(app: PIXI.Application, stageHost: HTMLDivElement) {
    this.app = app;
    this.stageHost = stageHost;
  }

  /**
   * 创建 PIXI Application
   *
   * Args:
   *     stageHost (HTMLDivElement): 模型绘制和交互所在的页面容器。
   */
  static createApp(stageHost: HTMLDivElement): PIXI.Application {
    const app = new PIXI.Application({
      resizeTo: stageHost,
      transparent: true,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(globalThis.devicePixelRatio || 1, 2),
      autoStart: true,
    });

    stageHost.appendChild(app.view as HTMLCanvasElement);
    return app;
  }

  /**
   * 加载 Live2D 模型
   *
   * Args:
   *     modelInfo (ModelInfo): 模型身份、资源地址和已经保存的显示参数。
   */
  async loadModel(modelInfo: ModelInfo): Promise<{
    model: unknown;
    applyTransform: (options?: { userScale?: number; offsetX?: number; offsetY?: number }) => void;
    fitModel: () => void;
    getLocalBounds: () => PIXI.Rectangle;
    getModelBounds: () => { x: number; y: number; width: number; height: number };
  }> {
    const { Live2DModel } = await import("pixi-live2d-display/cubism4");
    const model = await Live2DModel.from(modelInfo.modelUrl);
    model.interactive = true;
    this.app.stage.addChild(model);

    let baseScale = 1;
    let userScale = modelInfo.userScale;
    let offsetX = modelInfo.offsetX;
    let offsetY = modelInfo.offsetY;

    /**
     * 读取模型当前本地边界，供缩放和居中计算。
     *
     * Returns:
     *     Rectangle: 模型本地矩形边界。
     */
    const getLocalBounds = () => {
      return model.getLocalBounds();
    };

    /**
     * 根据本地边界、缩放和当前位置计算模型在画布中的矩形范围。
     *
     * Returns:
     *     { x: number; y: number; width: number; height: number; }: 模型布局使用的矩形边界。
     */
    const getModelBounds = () => {
      const localBounds = getLocalBounds();
      const scale = model.scale.x;
      return {
        x: model.x + (localBounds.x - model.pivot.x) * scale,
        y: model.y + (localBounds.y - model.pivot.y) * scale,
        width: localBounds.width * scale,
        height: localBounds.height * scale,
      };
    };

    /**
     * 把已保存的缩放与偏移应用到当前加载模型。
     *
     * Args:
     *     options ({ userScale?: number; offsetX?: number; offsetY?: number }): 调用方提供的配置或运行选项。
     */
    const applyTransform = (options?: { userScale?: number; offsetX?: number; offsetY?: number }) => {
      // 1. 只覆盖调用方显式改变的参数，窗口重排时保留用户设置的缩放和偏移。
      if (options?.userScale !== undefined) {
        userScale = options.userScale;
      }
      if (options?.offsetX !== undefined) {
        offsetX = options.offsetX;
      }
      if (options?.offsetY !== undefined) {
        offsetY = options.offsetY;
      }

      // 2. 基础缩放乘上用户缩放，以模型底部中心定位，避免改尺寸时模型脚部跳动。
      const localBounds = getLocalBounds();
      model.scale.set(baseScale * userScale);
      model.pivot.set(localBounds.x + localBounds.width / 2, localBounds.y + localBounds.height);
      model.x = this.stageHost.clientWidth * 0.5 + offsetX;
      model.y = this.stageHost.clientHeight * 0.96 + offsetY;
    };

    /**
     * 容器尺寸或模型设置变化时重新适配模型大小和位置。
     */
    const fitModel = () => {
      const localBounds = getLocalBounds();
      // 1. 模型暂无有效边界时使用保底尺寸，按宽高较小缩放适配容器，再应用用户偏移。
      const sourceWidth = localBounds.width > 0 ? localBounds.width : 1000;
      const sourceHeight = localBounds.height > 0 ? localBounds.height : 1400;
      const widthScale = (this.stageHost.clientWidth * 0.8) / sourceWidth;
      const heightScale = (this.stageHost.clientHeight * 0.78) / sourceHeight;
      baseScale = Math.min(widthScale, heightScale);
      applyTransform();
    };

    return {
      model,
      applyTransform,
      fitModel,
      getLocalBounds,
      getModelBounds,
    };
  }
}

/**
 * 限制值在指定范围内
 *
 * Args:
 *     value (number): 当前需要显示、比较或保存的值。
 *     min (number): 允许返回的最小值。
 *     max (number): 允许返回的最大值。
 */
export const clamp = (value: number, min: number, max: number): number => {
  return Math.max(min, Math.min(max, value));
};
