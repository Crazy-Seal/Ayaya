import { WINDOW_PALETTE } from "../../window-palette.js";

/**
 * 页面初始化图标前安装共享配色，使 CSS 和原生窗口读取同一份基础颜色。
 *
 * Args:
 *     root (HTMLElement): 当前文档根元素，基础色作为 CSS 变量提供给所有页面组件。
 */
export const initializeWindowTheme = (root: HTMLElement = document.documentElement): void => {
  for (const [name, color] of Object.entries(WINDOW_PALETTE)) {
    root.style.setProperty(`--palette-${name}`, color);
  }
};
