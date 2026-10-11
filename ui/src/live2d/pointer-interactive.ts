/**
 * 交互穿透检测
 */

import * as PIXI from "pixi.js";

/**
 * 交互穿透管理器
 */
export class PointerInteractiveManager {
  private app: PIXI.Application;
  private lastPointerInteractive: boolean | null = null;

  /**
   * 保存模型交互依赖和参数，后续按鼠标位置切换窗口穿透状态。
   *
   * Args:
   *     app (PIXI.Application): 渲染桌面模型的 Pixi 应用。
   */
  constructor(app: PIXI.Application) {
    this.app = app;
  }

  /**
   * 采样 Canvas Alpha 值
   *
   * Args:
   *     windowX (number): 鼠标在当前窗口中的横向位置。
   *     windowY (number): 鼠标在当前窗口中的纵向位置。
   */
  sampleCanvasAlpha(windowX: number, windowY: number): number {
    const canvas = this.app.view as HTMLCanvasElement;
    const rect = canvas.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return 0;
    }

    if (windowX < rect.left || windowX > rect.right || windowY < rect.top || windowY > rect.bottom) {
      return 0;
    }

    const pixelX = Math.floor(((windowX - rect.left) / rect.width) * canvas.width);
    const pixelY = Math.floor(((windowY - rect.top) / rect.height) * canvas.height);
    if (pixelX < 0 || pixelX >= canvas.width || pixelY < 0 || pixelY >= canvas.height) {
      return 0;
    }

    const renderer = this.app.renderer as PIXI.Renderer;
    const gl = renderer.gl;
    const sampleRadius = 1;
    let maxAlpha = 0;

    for (let offsetY = -sampleRadius; offsetY <= sampleRadius; offsetY += 1) {
      for (let offsetX = -sampleRadius; offsetX <= sampleRadius; offsetX += 1) {
        const sx = pixelX + offsetX;
        const sy = pixelY + offsetY;
        if (sx < 0 || sx >= canvas.width || sy < 0 || sy >= canvas.height) {
          continue;
        }

        const pixel = new Uint8Array(4);
        gl.readPixels(sx, canvas.height - sy - 1, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
        if (pixel[3] > maxAlpha) {
          maxAlpha = pixel[3];
        }
      }
    }

    return maxAlpha;
  }

  /**
   * 根据角色像素决定透明窗口是否捕获鼠标，聊天窗口独立接收交互。
   *
   * Args:
   *     windowX (number): 光标在窗口内的横坐标。
   *     windowY (number): 光标在窗口内的纵坐标。
   *     modelBounds ({ x: number; y: number; width: number; height: number }): 模型命中范围。
   */
  updatePointerInteractive(
    windowX: number,
    windowY: number,
    modelBounds: { x: number; y: number; width: number; height: number }
  ): void {
    if (!window.desktopPetApi || typeof window.desktopPetApi.setPointerInteractive !== "function") {
      return;
    }

    const onModelBounds =
      windowX >= modelBounds.x &&
      windowX <= modelBounds.x + modelBounds.width &&
      windowY >= modelBounds.y &&
      windowY <= modelBounds.y + modelBounds.height;

    const modelAlpha = onModelBounds ? this.sampleCanvasAlpha(windowX, windowY) : 0;
    const onModelPixel = modelAlpha >= 12;
    const shouldCapture = onModelBounds && onModelPixel;

    if (this.lastPointerInteractive !== shouldCapture) {
      window.desktopPetApi.setPointerInteractive(shouldCapture);
      this.lastPointerInteractive = shouldCapture;
    }
  }

  /**
   * 强制设置窗口可交互
   */
  forceWindowInteractive(): void {
    if (!window.desktopPetApi || typeof window.desktopPetApi.setPointerInteractive !== "function") {
      return;
    }

    window.desktopPetApi.setPointerInteractive(true);
  }
}
