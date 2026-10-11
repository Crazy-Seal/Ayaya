/**
 * 多模态配置页面
 */

import type { DesktopSettings } from "../../../shared-types.js";
import type { SettingsNotifier } from "../components/settings-toast.js";

/**
 * 编辑后端截图与输入期间隐藏桌宠、聊天窗口的配置，通过主进程调用后端 YAML 接口。
 */
export class MultimodalPage {
  private hideOnScreenshotCheckbox: HTMLInputElement;
  private settings: DesktopSettings | null = null;

  /**
   * 绑定截图隐藏选项并登记即时保存事件。
   *
   * Args:
     *     hideOnScreenshotCheckbox (HTMLInputElement): 截图时是否隐藏桌面宠物的选项。
     *     notifier (SettingsNotifier): 显示后端读写错误，保存失败恢复已确认的勾选状态。
   */
  constructor(hideOnScreenshotCheckbox: HTMLInputElement, private notifier: SettingsNotifier) {
    this.hideOnScreenshotCheckbox = hideOnScreenshotCheckbox;
    this.setupEventListeners();
  }

  /**
   * 勾选变化时立即请求主进程保存截图隐藏设置。
   */
  private setupEventListeners(): void {
    // 实时保存：勾选框变化时立即保存
    this.hideOnScreenshotCheckbox.addEventListener("change", /**
     * 用户修改隐藏选项后通过主进程保存后端设置，等待期间禁止重复保存。
     */
    async () => {
      const previous = this.settings?.hide_on_screenshot ?? true;
      this.hideOnScreenshotCheckbox.disabled = true;
      try {
        this.settings = await window.desktopPetApi.updateDesktopSettings({
          hide_on_screenshot: this.hideOnScreenshotCheckbox.checked,
        });
        if (document.body.dataset.applicationShutdown !== "true") {
          this.hideOnScreenshotCheckbox.checked = this.settings.hide_on_screenshot;
        }
      } catch (error) {
        this.hideOnScreenshotCheckbox.checked = previous;
        this.notifier.error(`保存桌面设置失败：${String(error)}`);
      } finally {
        if (document.body.dataset.applicationShutdown !== "true") this.hideOnScreenshotCheckbox.disabled = false;
      }
    });
  }

  /**
   * 用后端 YAML 已确认的设置更新勾选框，退出后迟到响应不改变界面。
   */
  async render(): Promise<void> {
    try {
      const settings = await window.desktopPetApi.getDesktopSettings();
      if (document.body.dataset.applicationShutdown === "true") return;
      this.settings = settings;
      this.hideOnScreenshotCheckbox.checked = settings.hide_on_screenshot;
    } catch (error) {
      this.notifier.error(`读取桌面设置失败：${String(error)}`);
    }
  }
}
