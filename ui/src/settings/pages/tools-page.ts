/**
 * 工具配置页面
 */

import type { ChatSettingsState, ToolItem } from "../types.js";

/**
 * 工具配置页面管理器
 */
export class ToolsPage {
  private toolsTableBody: HTMLTableSectionElement;
  private toolsEmpty: HTMLDivElement;
  private confirmBtn: HTMLButtonElement;

  private chatSettingsState: ChatSettingsState | null = null;
  private availableTools: ToolItem[] = [];

  constructor(
    toolsTableBody: HTMLTableSectionElement,
    toolsEmpty: HTMLDivElement,
    confirmBtn: HTMLButtonElement
  ) {
    this.toolsTableBody = toolsTableBody;
    this.toolsEmpty = toolsEmpty;
    this.confirmBtn = confirmBtn;

    this.setupEventListeners();
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    this.confirmBtn.addEventListener("click", async () => {
      if (!this.chatSettingsState) {
        return;
      }

      this.chatSettingsState = {
        ...this.chatSettingsState,
        tools_list: this.collectSelectedTools(),
      };

      await window.desktopPetApi.updateChatSettings(this.chatSettingsState);
    });
  }

  /**
   * 渲染工具表格
   */
  private renderToolsTable(tools: ToolItem[]): void {
    this.toolsTableBody.innerHTML = "";

    if (tools.length === 0) {
      this.toolsEmpty.hidden = false;
      return;
    }

    this.toolsEmpty.hidden = true;
    for (const tool of tools) {
      const row = document.createElement("tr");

      const checkCell = document.createElement("td");
      checkCell.className = "col-check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.toolName = tool.name;
      checkbox.checked = Boolean(this.chatSettingsState?.tools_list.includes(tool.name));
      checkCell.appendChild(checkbox);

      const nameCell = document.createElement("td");
      nameCell.textContent = tool.name;

      row.appendChild(checkCell);
      row.appendChild(nameCell);
      this.toolsTableBody.appendChild(row);
    }
  }

  /**
   * 收集选中的工具
   */
  private collectSelectedTools(): string[] {
    const selected: string[] = [];
    this.toolsTableBody.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-tool-name]').forEach((input) => {
      if (input.checked && input.dataset.toolName) {
        selected.push(input.dataset.toolName);
      }
    });
    return selected;
  }

  /**
   * 刷新工具列表
   */
  async refreshTools(): Promise<void> {
    const result = await window.desktopPetApi.getAvailableTools();
    this.availableTools = result.tools;
    this.renderToolsTable(this.availableTools);
  }

  /**
   * 渲染工具选择
   * @param state 聊天设置状态
   * @param forceUpdate 是否强制更新内部状态（模型切换时需要）
   */
  render(state: ChatSettingsState | null, forceUpdate = false): void {
    // 首次加载或强制更新时设置内部状态
    if (state && (!this.chatSettingsState || forceUpdate)) {
      this.chatSettingsState = state;
    }
    this.renderToolsTable(this.availableTools);
  }

  /**
   * 获取聊天设置状态
   */
  getChatSettingsState(): ChatSettingsState | null {
    return this.chatSettingsState;
  }
}
