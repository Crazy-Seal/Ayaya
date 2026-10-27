import { PageEventType } from "../types.js";
import type { ChatSettingsState, ISettingsPage, PageEditingData, PageEventCallback, PageRenderData, ToolPolicyView } from "../types.js";
import { MCPServerStatus, ToolPolicy } from "../../../shared-types.js";
import { PluginConfigForm, type ConfigSchema, type ConfigError } from "../plugin-config.js";

type PluginSettingsMap = NonNullable<ChatSettingsState["agent_plugins"]>;

const MCP_STATUS_LABELS: Record<MCPServerStatus, string> = {
  [MCPServerStatus.AVAILABLE]: "已连接",
  [MCPServerStatus.DISCONNECTED]: "未连接",
  [MCPServerStatus.CONNECTING]: "连接中",
  [MCPServerStatus.DISABLED]: "未启用",
  [MCPServerStatus.ERROR]: "连接失败",
};

/** 在单个插件卡片中统一展示参数、工具权限及 MCP 状态。 */
export class PluginsPage implements ISettingsPage {
  private eventCallback?: PageEventCallback;
  private forms = new Map<string, PluginConfigForm>();
  private policies = new Map<string, Map<string, HTMLSelectElement>>();
  private saved: PluginSettingsMap = {};

  /**
   * 绑定页面与保存按钮，输入校验由读取编辑数据统一执行。
   *
   * Args:
   *     container (HTMLDivElement): 插件卡片容器。
   *     empty (HTMLDivElement): 无插件时的提示。
   *     confirmBtn (HTMLButtonElement): 当前会话插件保存按钮。
   */
  constructor(private container: HTMLDivElement, private empty: HTMLDivElement, confirmBtn: HTMLButtonElement) {
    confirmBtn.addEventListener("click", /** 将提交交给设置控制器。 */ () => {
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "memory" });
    });
  }

  /**
   * 登记页面事件处理函数。
   *
   * Args:
   *     callback (PageEventCallback): 设置窗口接收的事件入口。
   */
  onEvent(callback: PageEventCallback): void { this.eventCallback = callback; }

  /**
   * 用插件目录和已保存配置生成卡片，保留暂时加载失败插件的原设置。
   *
   * Args:
   *     data (PageRenderData): 当前会话及已查询的插件能力目录。
   */
  render(data: PageRenderData): void {
    this.container.innerHTML = "";
    this.forms.clear();
    this.policies.clear();
    this.saved = structuredClone(data.saved.agent_plugins || {});
    const plugins = data.dependencies?.availablePlugins || [];
    this.empty.hidden = plugins.length > 0;
    for (const message of data.dependencies?.pluginErrors || []) {
      const error = document.createElement("p");
      error.className = "plugin-config-error";
      error.textContent = message;
      this.container.appendChild(error);
    }
    for (const plugin of plugins) {
      const card = document.createElement("div");
      card.className = "plugin-card ui-surface";
      card.dataset.pluginName = plugin.name;
      const header = document.createElement("div");
      header.className = "plugin-card-header";
      const introduction = document.createElement("div");
      introduction.className = "plugin-card-introduction";
      const title = document.createElement("div");
      title.className = "plugin-card-title";
      title.textContent = plugin.name;
      const description = document.createElement("div");
      description.className = "plugin-card-desc";
      description.textContent = plugin.description;
      const label = document.createElement("label");
      label.className = "plugin-toggle";
      label.textContent = "启用";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.className = "ui-checkbox";
      toggle.dataset.pluginEnabled = "true";
      toggle.checked = this.saved[plugin.name]?.enabled === true;
      label.appendChild(toggle);
      introduction.append(title, description);
      header.append(introduction, label);
      card.appendChild(header);
      const config = { ...plugin.default_config, ...this.saved[plugin.name]?.config };
      const form = new PluginConfigForm(plugin.config_schema as ConfigSchema, config);
      this.forms.set(plugin.name, form);
      card.appendChild(form.render());
      const statuses = data.dependencies?.pluginMcpStatuses?.[plugin.name] || [];
      for (const server of statuses) {
        const status = document.createElement("p");
        status.className = "plugin-mcp-status";
        status.textContent = `${server.name}：${MCP_STATUS_LABELS[server.runtime.status]}${server.runtime.last_error ? `（${server.runtime.last_error}）` : ""}`;
        card.appendChild(status);
      }
      this.policies.set(plugin.name, new Map());
      for (const tool of data.dependencies?.toolPolicies || []) {
        if (tool.plugin_name === plugin.name) card.appendChild(this.renderPolicy(plugin.name, tool));
      }
      this.container.appendChild(card);
    }
  }

  /**
   * 生成插件本地或远端工具权限选择框。
   *
   * Args:
   *     pluginName (string): 当前工具所属插件。
   *     tool (ToolPolicyView): 后端提供的工具定义、权限和可用状态。
   *
   * Returns:
   *     HTMLElement: 工具说明及权限选择框。
   */
  private renderPolicy(pluginName: string, tool: ToolPolicyView): HTMLElement {
    const label = document.createElement("label");
    label.className = "plugin-config-field";
    const text = document.createElement("span");
    text.textContent = `${tool.server_name ? `${tool.server_name} / ` : "本地工具 / "}${tool.display_name}${tool.available ? "" : `（${tool.unavailable_reason || "不可用"}）`}`;
    const select = document.createElement("select");
    select.className = "ui-control";
    for (const [value, name] of [[ToolPolicy.ALLOW, "允许"], [ToolPolicy.ASK, "询问"], [ToolPolicy.DENY, "拒绝"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = name;
      select.appendChild(option);
    }
    select.value = tool.policy;
    const key = tool.policy_key || tool.stable_key;
    this.policies.get(pluginName)!.set(key, select);
    label.append(text, select);
    return label;
  }

  /**
   * 校验并收集插件参数和权限，输入错误会阻止保存请求。
   *
   * Returns:
   *     PageEditingData: 保留完整设置的会话插件记录。
   *
   * Raises:
   *     Error: 某插件输入不满足配置要求。
   */
  getEditingData(): PageEditingData {
    const agent_plugins = structuredClone(this.saved);
    this.container.querySelectorAll<HTMLElement>("[data-plugin-name]").forEach(/** 读取卡片并保留未编辑字段。 */ (card) => {
      const name = card.dataset.pluginName!;
      const enabled = Boolean(card.querySelector<HTMLInputElement>('input[data-plugin-enabled="true"]')?.checked);
      const config = this.forms.get(name)!.read(enabled);
      const tool_permissions = { ...agent_plugins[name]?.tool_permissions };
      for (const [key, select] of this.policies.get(name) || []) tool_permissions[key] = select.value as ToolPolicy;
      agent_plugins[name] = { ...agent_plugins[name], enabled, config, tool_permissions };
    });
    return { plugins: { agent_plugins } };
  }

  /**
   * 将后端自定义校验错误定位到当前插件表单。
   *
   * Args:
   *     errors (Array): 带完整插件字段路径的后端错误。
   */
  showConfigurationErrors(errors: Array<{ loc: Array<string | number>; msg: string }>): void {
    const grouped = new Map<string, ConfigError[]>();
    for (const error of errors) {
      const offset = error.loc.indexOf("agent_plugins");
      if (offset < 0) continue;
      const name = String(error.loc[offset + 1]);
      const path = "/" + error.loc.slice(offset + 3).map(/** 转义 JSON 路径中的字段名称。 */ (part) => String(part).replace(/~/g, "~0").replace(/\//g, "~1")).join("/");
      const list = grouped.get(name) || [];
      list.push({ path, message: error.msg });
      grouped.set(name, list);
    }
    for (const [name, errors] of grouped) this.forms.get(name)?.showErrors(errors);
  }
}
