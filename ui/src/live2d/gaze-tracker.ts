/**
 * 视线跟随算法
 */

import * as PIXI from "pixi.js";

import { clamp } from "./model-loader.js";
import type { CursorSyncData } from "../types.js";

/**
 * 视线跟随控制器
 */
export class GazeTracker {
  private targetGazeX = 0;
  private targetGazeY = 0;
  private smoothGazeX = 0;
  private smoothGazeY = 0;
  private targetHeadX = 0;
  private targetHeadY = 0;
  private smoothHeadX = 0;
  private smoothHeadY = 0;

  /**
   * 根据屏幕坐标更新注视目标
   */
  updateTarget(
    cursorData: CursorSyncData,
    stageHost: HTMLDivElement,
    followCursor: boolean
  ): void {
    if (!followCursor) {
      this.targetGazeX = 0;
      this.targetGazeY = 0;
      this.targetHeadX = 0;
      this.targetHeadY = 0;
      return;
    }

    const rect = stageHost.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return;
    }

    const centerX = cursorData.windowX + rect.left + rect.width * 0.5;
    const centerY = cursorData.windowY + rect.top + rect.height * 0.42;

    const halfRangeX = Math.max(cursorData.displayWidth * 0.45, 1);
    const halfRangeY = Math.max(cursorData.displayHeight * 0.45, 1);
    const dx = (cursorData.screenX - centerX) / halfRangeX;
    const dy = (centerY - cursorData.screenY) / halfRangeY;

    const displayCenterX = cursorData.displayX + cursorData.displayWidth * 0.5;
    const displayCenterY = cursorData.displayY + cursorData.displayHeight * 0.5;
    const globalBiasX = clamp(
      (cursorData.screenX - displayCenterX) / Math.max(cursorData.displayWidth * 0.8, 1),
      -0.35,
      0.35
    );
    const globalBiasY = clamp(
      (displayCenterY - cursorData.screenY) / Math.max(cursorData.displayHeight * 0.8, 1),
      -0.25,
      0.25
    );

    this.targetGazeX = clamp(dx + globalBiasX, -1.2, 1.2);
    this.targetGazeY = clamp(dy + globalBiasY, -1.2, 1.2);

    const outsideBoost = cursorData.insideWindow ? 1 : 1.9;
    this.targetHeadX = clamp(this.targetGazeX * outsideBoost, -1.5, 1.5);
    this.targetHeadY = clamp(this.targetGazeY * (cursorData.insideWindow ? 1 : 1.5), -1.4, 1.4);
  }

  /**
   * 应用平滑注视（每帧调用）
   */
  applySmoothGaze(model: unknown): void {
    this.smoothGazeX += (this.targetGazeX - this.smoothGazeX) * 0.2;
    this.smoothGazeY += (this.targetGazeY - this.smoothGazeY) * 0.2;
    this.smoothHeadX += (this.targetHeadX - this.smoothHeadX) * 0.22;
    this.smoothHeadY += (this.targetHeadY - this.smoothHeadY) * 0.22;
    this.applyFocusByParams(model, this.smoothGazeX, this.smoothGazeY, this.smoothHeadX, this.smoothHeadY);
  }

  /**
   * 设置模型参数值
   */
  private applyFocusByParams(
    model: unknown,
    eyeX: number,
    eyeY: number,
    headX: number,
    headY: number
  ): void {
    const modelLike = model as {
      internalModel?: {
        coreModel?: {
          setParameterValueById?: (id: string, value: number, weight?: number) => void;
          setParamFloat?: (id: string, value: number, weight?: number) => void;
        };
      };
    };

    const coreModel = modelLike.internalModel?.coreModel;
    if (!coreModel) {
      return;
    }

    const setParam = (id: string, value: number, weight = 1) => {
      if (typeof coreModel.setParameterValueById === "function") {
        coreModel.setParameterValueById(id, value, weight);
        return;
      }

      if (typeof coreModel.setParamFloat === "function") {
        coreModel.setParamFloat(id, value, weight);
      }
    };

    setParam("ParamEyeBallX", eyeX);
    setParam("ParamEyeBallY", eyeY);
    setParam("ParamAngleX", headX * 32, 1);
    setParam("ParamAngleY", headY * 24, 1);
    setParam("ParamBodyAngleX", headX * 14, 0.85);
    setParam("ParamAngleZ", -headX * headY * 10, 0.5);
  }

  /**
   * 注册到 PIXI ticker
   */
  registerToTicker(app: PIXI.Application): void {
    // 需要绑定 model，这里只是占位
  }

  /**
   * 从 PIXI ticker 移除
   */
  removeFromTicker(app: PIXI.Application): void {
    // 需要绑定 model，这里只是占位
  }
}

/**
 * 创建视线跟随的帧更新函数
 */
export const createGazeUpdateFunction = (
  model: unknown,
  tracker: GazeTracker
): (() => void) => {
  return () => {
    tracker.applySmoothGaze(model);
  };
};
