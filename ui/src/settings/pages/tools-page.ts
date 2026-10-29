/**
 * 工具配置页面
 */
import { PageEventType } from "../types.js";
import { ToolSource, ToolPolicy } from "../../../shared-types.js";
import { iconMarkup } from "../../ui/icons.js";

import type {
  ToolPolicyView,
  ISettingsPage,
  PageRenderData,
  PageEditingData,
  PageEventCallback,
} from "../types.js";

/**
 * 显示工具权限，按 MCP 服务分组并保留用户在当前会话中的展开选择。
 *
 * Attributes:
 *     mcpExpansionOverrides (Map<string, boolean>): 用户手动切换过的服务编号和展开状态。
 *     sessionId (string | null): 当前显示的会话，切换会话时重新采用默认展开状态。
 */
export class ToolsPage implements ISettingsPage {
  private toolsTableBody: HTMLTableSectionElement;
  private toolsEmpty: HTMLDivElement;
  private confirmBtn: HTMLButtonElement;

  private eventCallback?: PageEventCallback;
  private mcpExpansionOverrides = new Map<string, boolean>();
  private sessionId: string | null = null;

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
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "tools" });
    });
  }

  /**
   * 渲染页面
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    // 1. 会话切换后重新采用各服务的启用状态，同一会话重绘时保留手动选择。
    if (this.sessionId !== data.saved.session_id) {
      this.mcpExpansionOverrides.clear();
      this.sessionId = data.saved.session_id;
    }
    const mcp = data.editing?.mcp ?? data.saved.mcp;
    const enabledServers = new Set(
      (data.dependencies?.mcpServers ?? []).filter(/** 只有全局和当前会话均启用的服务默认展开。 */
        (server) => server.config.enabled && mcp.servers[server.config.id]?.enabled,
      ).map(/** 使用稳定服务编号关联工具分组。 */ (server) => server.config.id),
    );
    // 2. 插件工具由插件页面管理，本页显示本地工具和独立配置的 MCP 工具。
    this.renderToolsTable(
      (data.dependencies?.toolPolicies ?? []).filter(/** 保留本页管理的工具。 */ (tool) => !tool.plugin_name),
      enabledServers,
    );
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
   * 按服务编号分组显示工具，默认展开已启用服务，并允许独立切换每组。
   *
   * Args:
   *     tools (ToolPolicyView[]): 后端返回的本地与 MCP 工具权限目录。
   *     enabledServers (ReadonlySet<string>): 全局和当前会话均启用的 MCP 服务编号。
   */
  private renderToolsTable(tools: ToolPolicyView[], enabledServers: ReadonlySet<string>): void {
    this.toolsTableBody.innerHTML = "";

    if (tools.length === 0) {
      this.toolsEmpty.hidden = false;
      return;
    }

    this.toolsEmpty.hidden = true;
    // 1. 按稳定编号归组，服务重名或目录条目顺序变化时仍分别控制。
    const groups = new Map<string, ToolPolicyView[]>();
    for (const tool of tools) {
      const key = tool.source === ToolSource.LOCAL ? ToolSource.LOCAL : `${ToolSource.MCP}:${tool.server_id}`;
      const group = groups.get(key) ?? [];
      group.push(tool);
      groups.set(key, group);
    }
    // 2. 本地工具保持展开；每个 MCP 分组的按钮只控制本组工具行。
    for (const group of groups.values()) {
      const tool = group[0];
      const groupRow = document.createElement("tr");
      groupRow.className = "tool-permission-group";
      const groupCell = document.createElement("th");
      groupCell.colSpan = 4;
      groupCell.scope = "rowgroup";
      const rows = group.map(/** 为每个工具创建权限控件，折叠时保留当前编辑值。 */
        (entry) => this.createToolRow(entry),
      );
      if (tool.source === ToolSource.LOCAL) {
        groupCell.textContent = "本地工具";
      } else {
        const serverId = tool.server_id ?? "";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tool-group-toggle";
        button.innerHTML = iconMarkup("chevron");
        const label = document.createElement("span");
        label.textContent = `MCP · ${tool.server_name || serverId || "未知"}（${group.length} 个工具）`;
        button.append(label);
        /**
         * 同步本组按钮状态和工具行可见性，保留折叠行中的权限控件供保存。
         *
         * Args:
         *     expanded (boolean): 是否展开当前 MCP 服务的工具列表。
         */
        const setExpanded = (expanded: boolean): void => {
          button.setAttribute("aria-expanded", String(expanded));
          for (const row of rows) row.hidden = !expanded;
        };
        setExpanded(this.mcpExpansionOverrides.get(serverId) ?? enabledServers.has(serverId));
        button.addEventListener("click", /** 用户通过鼠标或键盘切换本组，重绘时沿用当前选择。 */
        () => {
          const expanded = button.getAttribute("aria-expanded") !== "true";
          this.mcpExpansionOverrides.set(serverId, expanded);
          setExpanded(expanded);
        });
        groupCell.append(button);
      }
      groupRow.append(groupCell);
      this.toolsTableBody.append(groupRow, ...rows);
    }
  }

  /**
   * 创建单个工具的名称、权限下拉框和说明，工具可用性决定能否修改权限。
   *
   * Args:
   *     tool (ToolPolicyView): 当前工具的标识、说明、权限和可用状态。
   *
   * Returns:
   *     HTMLTableRowElement: 加入对应分组的工具行。
   */
  private createToolRow(tool: ToolPolicyView): HTMLTableRowElement {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.textContent = tool.display_name;
    const sourceCell = document.createElement("td");
    sourceCell.textContent = tool.source === ToolSource.LOCAL ? "本地" : "MCP";
    const policyCell = document.createElement("td");
    const select = document.createElement("select");
    select.className = "tool-policy-select ui-control ui-control--small ui-select";
    select.dataset.stableKey = tool.stable_key;
    select.setAttribute("aria-label", `${tool.display_name} 工具权限`);
    for (const [value, label] of [
      [ToolPolicy.ASK, "ASK"],
      [ToolPolicy.ALLOW, "ALLOW"],
      [ToolPolicy.DENY, "DENY"],
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
    return row;
  }

  /**
   * 收集选中的工具
   */
  private collectPolicies(): Record<string, ToolPolicy> {
    const policies: Record<string, ToolPolicy> = {};
    this.toolsTableBody.querySelectorAll<HTMLSelectElement>('select[data-stable-key]').forEach(/**
     * 按每个下拉框的稳定工具标识收集 deny、ask 或 allow，缺少标识的控件不写入配置。
     *
     * Args:
     *     select (HTMLSelectElement): 当前动作配置的下拉选择框。
     */
    (select) => {
      if (select.dataset.stableKey) {
        policies[select.dataset.stableKey] = select.value as ToolPolicy;
      }
    });
    return policies;
  }
}
