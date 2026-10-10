import type { DesktopPetApi } from "../shared-types.js";

declare global {
  /**
   * 向页面声明 preload 注入的桌面 API，避免渲染进程直接访问主进程资源。
   */
  interface Window {
    desktopPetApi: DesktopPetApi;
    hideElementsForScreenshot: () => void;
    restoreElementsAfterScreenshot: () => void;
    playMotionByLabel?: (label: string) => void;
    getExpressionLabels?: () => string[];
  }
}

export {};
