/**
 * 工具配置页面
 */

import type {
  ToolPolicyView,
  ISettingsPage,
  PageRenderData,
  PageEditingData,
  PageEventCallback,
} from "../types.js";

/**
 * 工具配置页面管理器（纯视图组件）
 */
export class ToolsPage implements ISettingsPage {
  private toolsTableBody: HTMLTableSectionElement;
  private toolsEmpty: HTMLDivElement;
  private confirmBtn: HTMLButtonElement;

  private eventCallback?: PageEventCallback;

  /**
   * 绑定工具权限表格和确认按钮，权限收集后交给设置控制器保存。
   *
   * Args:
   *     toolsTableBody (HTMLTableSectionElement): 显示工具目录与权限的表格正文。
   *     toolsEmpty (HTMLDivElement): 没有工具时显示的占位元素。
   *     confirmBtn (HTMLButtonElement): 用户点击后提交本页修改的按钮。
   */
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
   * 设置事件回调
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void {
    this.eventCallback = callback;
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    this.confirmBtn.addEventListener("click", /**
     * 用户点击权限确认后通知设置控制器保存当前工具策略。
     */
    () => {
      this.eventCallback?.({ type: "submit", page: "tools" });
    });
  }

  /**
   * 渲染页面
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    this.renderToolsTable(data.dependencies?.toolPolicies || []);
  }

  /**
   * 获取当前编辑数据
   */
  getEditingData(): PageEditingData {
    return {
      tools: {
        tool_permissions: this.collectPolicies(),
      },
    };
  }

  /**
   * 渲染工具表格
   *
   * Args:
   *     tools (ToolPolicyView[]): 后端返回的本地与 MCP 工具权限目录。
   */
  private renderToolsTable(tools: ToolPolicyView[]): void {
    this.toolsTableBody.innerHTML = "";

    if (tools.length === 0) {
      this.toolsEmpty.hidden = false;
      return;
    }

    this.toolsEmpty.hidden = true;
    let currentGroup = "";
    for (const tool of tools) {
      const group = tool.source === "local"
        ? "本地工具"
        : `MCP · ${tool.server_name || tool.server_id || "未知"}`;
      if (group !== currentGroup) {
        const groupRow = document.createElement("tr");
        groupRow.className = "tool-permission-group";
        const groupCell = document.createElement("th");
        groupCell.colSpan = 4;
        groupCell.scope = "rowgroup";
        groupCell.textContent = group;
        groupRow.append(groupCell);
        this.toolsTableBody.appendChild(groupRow);
        currentGroup = group;
      }
      const row = document.createElement("tr");

      const nameCell = document.createElement("td");
      nameCell.textContent = tool.display_name;
      const sourceCell = document.createElement("td");
      sourceCell.textContent = tool.source === "local" ? "本地" : "MCP";
      const policyCell = document.createElement("td");
      const select = document.createElement("select");
      select.className = "tool-policy-select ui-control ui-control--small ui-select";
      select.dataset.stableKey = tool.stable_key;
      select.setAttribute("aria-label", `${tool.display_name} 工具权限`);
      for (const [value, label] of [
        ["ask", "ASK"],
        ["allow", "ALLOW"],
        ["deny", "DENY"],
      ] as const) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.append(option);
      }
      select.value = tool.policy;
      select.disabled = !tool.available;
      policyCell.append(select);
      const statusCell = document.createElement("td");
      const description = document.createElement("details");
      description.className = "tool-description";
      const summary = document.createElement("summary");
      summary.textContent = tool.available ? "查看说明" : "不可用 · 查看原因";
      const descriptionText = document.createElement("div");
      descriptionText.className = "tool-description-text";
      descriptionText.textContent = tool.available
        ? tool.description || "暂无说明"
        : tool.unavailable_reason || "工具当前不可用";
      description.append(summary, descriptionText);
      statusCell.append(description);

      row.append(sourceCell, nameCell, policyCell, statusCell);
      this.toolsTableBody.appendChild(row);
    }
  }

  /**
   * 收集选中的工具
   */
  private collectPolicies(): Record<string, "deny" | "ask" | "allow"> {
    const policies: Record<string, "deny" | "ask" | "allow"> = {};
    this.toolsTableBody.querySelectorAll<HTMLSelectElement>('select[data-stable-key]').forEach(/**
     * 按每个下拉框的稳定工具标识收集 deny、ask 或 allow，缺少标识的控件不写入配置。
     *
     * Args:
     *     select (HTMLSelectElement): 当前动作配置的下拉选择框。
     */
    (select) => {
      if (select.dataset.stableKey) {
        policies[select.dataset.stableKey] = select.value as "deny" | "ask" | "allow";
      }
    });
    return policies;
  }
}
