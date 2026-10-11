import type { ChannelSubscription } from "../../types.js";

/**
 * 展示每会话注册渠道并持久保存可选订阅，桌面项固定开启。
 */
export class ChannelSubscriptionsPage {
  /**
   * 装配 ChannelSubscriptionsPage 的依赖并初始化本地视图状态。
   *
   * Args:
   *     container (HTMLElement): 当前页面或聊天视图容器。
   */
  constructor(private readonly container: HTMLElement) {}

  /**
   * 查询当前会话数据并呈现页面，失败时显示错误说明。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  async render(sessionId: string): Promise<void> {
    try {
      this.show(sessionId, await window.desktopPetApi.getChannelSubscriptions(sessionId));
    } catch (error) {
      this.container.textContent = `读取渠道订阅失败: ${String(error)}`;
    }
  }

  /**
   * 重建订阅行，桌面项禁用编辑，可选渠道保存失败时回退勾选状态。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     channels (ChannelSubscription[]): 后端返回的渠道配置和可用状态。
   */
  private show(sessionId: string, channels: ChannelSubscription[]): void {
    this.container.replaceChildren();
    for (const channel of channels) {
      const row = document.createElement("label");
      row.className = "channel-subscription-row";
      const title = document.createElement("span");
      title.textContent = channel.display_name;
      const status = document.createElement("span");
      status.textContent = channel.ready ? "已连接" : "未连接";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "ui-checkbox";
      checkbox.checked = channel.enabled;
      // 1. 桌面订阅固定启用，可选渠道才允许发送持久配置变更。
      checkbox.disabled = channel.channel === "desktop";
      checkbox.setAttribute("aria-label", `订阅 ${channel.display_name}`);
      // 2. 保存成功用后端列表重绘，失败时回退本地勾选并显示原因。
      checkbox.addEventListener("change", /**
       * 用户修改渠道开关后等待后端保存，成功刷新列表，失败恢复原选择并显示原因。
       */
      () => {
        void window.desktopPetApi.setChannelSubscription(
          sessionId, channel.channel, checkbox.checked,
        ).then(/**
         * 订阅保存成功后使用后端返回的完整列表刷新当前会话显示。
         *
         * Args:
         *     updated (ChannelSubscription[]): 后端保存后返回的最新订阅或设置记录。
         */
        (updated) => this.show(sessionId, updated)).catch(/**
         * 订阅保存失败后回退用户刚改变的开关，并在状态区域显示错误。
         *
         * Args:
         *     error (unknown): 本次捕获的异常。
         */
        (error: unknown) => {
          checkbox.checked = !checkbox.checked;
          status.textContent = `保存失败: ${String(error)}`;
        });
      });
      row.append(title, status, checkbox);
      this.container.appendChild(row);
    }
  }
}
