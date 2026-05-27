/**
 * 多模态配置页面
 */

import type { FrontendSettings } from "../types.js";

export class MultimodalPage {
  private hideOnScreenshotCheckbox: HTMLInputElement;
  private settings: FrontendSettings | null = null;

  constructor(hideOnScreenshotCheckbox: HTMLInputElement) {
    this.hideOnScreenshotCheckbox = hideOnScreenshotCheckbox;
    this.setupEventListeners();
  }

  private setupEventListeners(): void {
    // 实时保存：勾选框变化时立即保存
    this.hideOnScreenshotCheckbox.addEventListener("change", async () => {
      await window.desktopPetApi.updateFrontendSettings({
        hide_on_screenshot: this.hideOnScreenshotCheckbox.checked,
      });
    });
  }

  async render(): Promise<void> {
    this.settings = await window.desktopPetApi.getFrontendSettings();
    this.hideOnScreenshotCheckbox.checked = this.settings.hide_on_screenshot;
  }
}
