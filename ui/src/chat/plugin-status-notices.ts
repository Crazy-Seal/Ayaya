import type { PluginStatusNotice } from "../../shared-types.js";

/**
 * 在聊天列表外显示当前连接的插件持续提示，历史重绘保留正在显示的内容。
 *
 * Attributes:
 *     container (HTMLElement): 页面为持续提示保留的独立区域。
 *     notices (Map<string, HTMLDivElement>): 按插件及编号定位的显示节点。
 */
export class PluginStatusNotices {
  private readonly notices = new Map<string, HTMLDivElement>();

  /**
   * 绑定页面上的提示区域，初始保持隐藏。
   *
   * Args:
   *     container (HTMLElement): ChatApp 创建时提供的独立提示容器。
   */
  constructor(private readonly container: HTMLElement) {
    this.clear();
  }

  /**
   * 根据已受理事件新增、更新或关闭提示，更新时保留原有显示位置。
   *
   * Args:
   *     notice (PluginStatusNotice): 当前会话中已绑定插件来源的展示请求。
   */
  update(notice: PluginStatusNotice): void {
    const key = JSON.stringify([notice.plugin_name, notice.id]);
    let element = this.notices.get(key);
    if (notice.active) {
      if (!element) {
        element = document.createElement("div");
        element.className = "plugin-notice";
        this.notices.set(key, element);
        this.container.appendChild(element);
      }
      element.textContent = `[插件 ${notice.plugin_name}] ${notice.text}`;
    } else {
      element?.remove();
      this.notices.delete(key);
    }
    this.container.hidden = this.notices.size === 0;
  }

  /** 清空当前连接的所有提示，在断线、切换会话或新快照开始时调用。 */
  clear(): void {
    this.notices.clear();
    this.container.textContent = "";
    this.container.hidden = true;
  }
}
