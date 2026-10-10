/**
 * 多模态配置页面
 */

import type { FrontendSettings } from "../types.js";

/**
 * 编辑截图时隐藏桌面宠物的前端选项，勾选变化后立即交给主进程保存。
 */
export class MultimodalPage {
  private hideOnScreenshotCheckbox: HTMLInputElement;
  private settings: FrontendSettings | null = null;

  /**
   * 绑定截图隐藏选项并登记即时保存事件。
   *
   * Args:
   *     hideOnScreenshotCheckbox (HTMLInputElement): 截图时是否隐藏桌面宠物的选项。
   */
  constructor(hideOnScreenshotCheckbox: HTMLInputElement) {
    this.hideOnScreenshotCheckbox = hideOnScreenshotCheckbox;
    this.setupEventListeners();
  }

  /**
   * 勾选变化时立即请求主进程保存截图隐藏设置。
   */
  private setupEventListeners(): void {
    // 实时保存：勾选框变化时立即保存
    this.hideOnScreenshotCheckbox.addEventListener("change", /**
     * 用户修改截图隐藏选项后立即交给主进程保存前端设置。
     */
    async () => {
      await window.desktopPetApi.updateFrontendSettings({
        hide_on_screenshot: this.hideOnScreenshotCheckbox.checked,
      });
    });
  }

  /**
   * 用已保存前端设置更新截图隐藏选项。
   */
  async render(): Promise<void> {
    this.settings = await window.desktopPetApi.getFrontendSettings();
    this.hideOnScreenshotCheckbox.checked = this.settings.hide_on_screenshot;
  }
}
