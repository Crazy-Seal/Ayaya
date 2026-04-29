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

  constructor(app: PIXI.Application, stageHost: HTMLDivElement) {
    this.app = app;
    this.stageHost = stageHost;
  }

  /**
   * 创建 PIXI Application
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

    const getLocalBounds = () => {
      return model.getLocalBounds();
    };

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

    const applyTransform = (options?: { userScale?: number; offsetX?: number; offsetY?: number }) => {
      // 更新内部变量（如果提供了参数）
      if (options?.userScale !== undefined) {
        userScale = options.userScale;
      }
      if (options?.offsetX !== undefined) {
        offsetX = options.offsetX;
      }
      if (options?.offsetY !== undefined) {
        offsetY = options.offsetY;
      }

      const localBounds = getLocalBounds();
      model.scale.set(baseScale * userScale);
      model.pivot.set(localBounds.x + localBounds.width / 2, localBounds.y + localBounds.height);
      model.x = this.stageHost.clientWidth * 0.5 + offsetX;
      model.y = this.stageHost.clientHeight * 0.96 + offsetY;
    };

    const fitModel = () => {
      const localBounds = getLocalBounds();
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
 */
export const clamp = (value: number, min: number, max: number): number => {
  return Math.max(min, Math.min(max, value));
};
