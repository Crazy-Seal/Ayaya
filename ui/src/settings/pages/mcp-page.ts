import type {
  MCPModelSettings,
  MCPServerConfig,
  MCPServerView,
  MCPToolInfo,
} from "../../../shared-types.js";
import type { ISettingsPage, PageEditingData, PageEventCallback, PageRenderData } from "../types.js";
import type { ConfirmDialog } from "../../ui/confirm-dialog.js";
import { appendErrorMessage, type SettingsToast } from "../components/settings-toast.js";
import { authorizeBaseUrlChange, classifyHttpEndpoint } from "../url-security.js";
import { buildEnabledServerPolicy, fingerprintMcpConfig } from "../mcp-config.js";

type SecretRow = { key: HTMLInputElement; value: HTMLInputElement; element: HTMLDivElement };

/**
 * 管理 MCP Server 定义和当前模型工具权限的设置页面。
 */
export class McpPage implements ISettingsPage {
  private servers: MCPServerView[] = [];
  private tools = new Map<string, MCPToolInfo[]>();
  private policies: MCPModelSettings = { servers: {} };
  private callback: PageEventCallback | null = null;
  private editingId: string | null = null;
  private testedFingerprint: string | null = null;
  private authorizedInsecureFingerprint: string | null = null;
  private envRows: SecretRow[] = [];
  private headerRows: SecretRow[] = [];

  /**
   * 初始化 MCP 设置页并绑定用户交互事件。
   *
   * Args:
   *     root (HTMLDivElement): MCP 页面根容器。
   *     confirmDialog (ConfirmDialog): 通用确认对话框。
   *     toast (SettingsToast): 设置页消息提示器。
   */
  constructor(
    private readonly root: HTMLDivElement,
    private readonly confirmDialog: ConfirmDialog,
    private readonly toast: SettingsToast
  ) {
    this.q<HTMLButtonElement>("#mcp-add-btn").addEventListener("click", /**
     * 用户点击新增服务时打开空配置编辑器。
     */
    () => this.openEditor());
    this.q<HTMLButtonElement>("#mcp-policy-save-btn").addEventListener("click", /**
     * 用户点击权限确认时通知主设置控制器保存本页服务启用策略。
     */
    () =>
      this.callback?.({ type: "submit", page: "mcp" }));
    this.q<HTMLSelectElement>("#mcp-transport").addEventListener("change", /**
     * 用户切换传输方式后显示该方式需要的地址、命令和凭据字段。
     */
    () => this.updateTransportFields());
    this.q<HTMLButtonElement>("#mcp-add-env").addEventListener("click", /**
     * 用户添加环境变量时创建新的敏感字段编辑行。
     */
    () => this.addSecretRow("env"));
    this.q<HTMLButtonElement>("#mcp-add-header").addEventListener("click", /**
     * 用户添加请求头时创建新的敏感字段编辑行。
     */
    () => this.addSecretRow("header"));
    this.q<HTMLButtonElement>("#mcp-cancel-btn").addEventListener("click", /**
     * 用户关闭服务编辑器时清理当前编辑与测试状态。
     */
    () => this.closeEditor());
    this.q<HTMLButtonElement>("#mcp-test-btn").addEventListener("click", /**
     * 用户点击测试时校验当前编辑配置并请求连接测试。
     */
    () => void this.testCurrent());
    this.q<HTMLFormElement>("#mcp-editor").addEventListener("submit", /**
     * 用户提交编辑表单时阻止默认刷新，校验后保存当前服务配置。
     *
     * Args:
     *     event (SubmitEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      event.preventDefault();
      void this.saveCurrent();
    });
  }

  /**
   * 使用已保存权限和 MCP 依赖数据渲染页面。
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    this.servers = data.dependencies?.mcpServers ?? [];
    this.tools = data.dependencies?.mcpTools ?? new Map();
    this.policies = structuredClone(data.saved.mcp ?? { servers: {} });
    this.renderServers();
  }

  /**
   * 获取当前页面编辑中的模型级 MCP 权限。
   *
   * Returns:
   *     PageEditingData: 可提交给设置管理器的深拷贝数据。
   */
  getEditingData(): PageEditingData {
    return { mcp: structuredClone(this.policies) };
  }

  /**
   * 注册设置页事件回调。
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void {
    this.callback = callback;
  }

  /**
   * 在 MCP 页面根节点内查询必需元素。
   *
   * @throws {Error} 页面结构中不存在指定元素时抛出。
   *
   * Args:
   *     selector (string): CSS 选择器。
   *
   * Returns:
   *     T: 匹配的页面元素。
   */
  private q<T extends HTMLElement>(selector: string): T {
    const value = this.root.querySelector<T>(selector);
    if (!value) throw new Error(`MCP 页面缺少元素 ${selector}`);
    return value;
  }

  /**
   * 根据当前 Server 数据重新渲染卡片列表。
   */
  private renderServers(): void {
    const list = this.q<HTMLDivElement>("#mcp-server-list");
    list.replaceChildren();
    for (const server of this.servers) list.append(this.createServerCard(server));
    this.syncPageVisibility();
  }

  /**
   * 根据服务和编辑器状态同步空状态及模型权限按钮。
   */
  private syncPageVisibility(): void {
    const hasServers = this.servers.length > 0;
    const editorOpen = !this.q<HTMLFormElement>("#mcp-editor").hidden;
    this.q<HTMLDivElement>("#mcp-empty").hidden = hasServers || editorOpen;
    this.q<HTMLButtonElement>("#mcp-policy-save-btn").hidden = !hasServers;
  }

  /**
   * 创建包含状态、操作和权限面板的 Server 卡片。
   *
   * Args:
   *     server (MCPServerView): 待展示的 MCP Server 视图。
   *
   * Returns:
   *     HTMLDivElement: 完整的 Server 卡片元素。
   */
  private createServerCard(server: MCPServerView): HTMLDivElement {
    const card = document.createElement("div");
    card.className = "mcp-server-card ui-surface";
    const header = document.createElement("div");
    header.className = "mcp-server-header";
    const title = document.createElement("div");
    title.textContent = `${server.config.name}  ·  ${server.config.transport}`;
    const status = document.createElement("span");
    status.className = `mcp-status mcp-status-${server.runtime.status}`;
    status.textContent = `${server.runtime.status} · ${server.runtime.tool_count} 个工具`;
    header.append(title, status);
    card.append(header);
    if (server.runtime.last_error) {
      const error = document.createElement("div");
      error.className = "mcp-error";
      error.textContent = server.runtime.last_error;
      card.append(error);
    }
    if (server.runtime.instructions) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Server Instructions（仅诊断展示）";
      const instructions = document.createElement("pre");
      instructions.textContent = server.runtime.instructions;
      details.append(summary, instructions);
      card.append(details);
    }
    const actions = document.createElement("div");
    actions.className = "mcp-card-actions";
    actions.append(
      this.actionButton("编辑", /**
       * 用户点击已有服务的编辑入口时加载其配置。
       */
      () => this.openEditor(server)),
      this.actionButton("测试", /**
       * 用户测试已保存服务时使用该服务当前配置。
       */
      () => void this.testSaved(server))
    );
    if (server.config.enabled) {
      actions.append(this.actionButton("重连", /**
       * 用户请求重连时刷新指定服务的连接。
       */
      () => void this.reconnect(server.config.id)));
    }
    actions.append(
      this.actionButton(
        "删除",
        /**
         * 用户删除服务时先确认目标名称，再请求后端删除并同步页面。
         */
        () => void this.remove(server.config.id, server.config.name),
        "danger",
      ),
    );
    card.append(actions, this.createPolicyPanel(server));
    return card;
  }

  /**
   * 创建当前模型对指定 Server 的工具权限面板。
   *
   * Args:
   *     server (MCPServerView): 权限面板对应的 MCP Server。
   *
   * Returns:
   *     HTMLDivElement: 包含启用开关和工具策略选择器的元素。
   */
  private createPolicyPanel(server: MCPServerView): HTMLDivElement {
    const panel = document.createElement("div");
    panel.className = "mcp-policy-panel";
    const toggleLabel = document.createElement("label");
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "ui-checkbox";
    toggle.checked = Boolean(this.policies.servers[server.config.id]?.enabled);
    toggleLabel.append(toggle, document.createTextNode("当前模型启用此服务"));
    panel.append(toggleLabel);
    toggle.addEventListener("change", /**
     * 用户切换服务启用开关时保留已有身份绑定，只修改启用标记或创建默认策略。
     */
    () => {
      if (toggle.checked) {
        this.policies.servers[server.config.id] = buildEnabledServerPolicy(
          this.policies.servers[server.config.id]
        );
      } else {
        const current = this.policies.servers[server.config.id];
        this.policies.servers[server.config.id] = {
          ...current,
          enabled: false,
        };
      }
    });
    return panel;
  }

  /**
   * 创建统一样式和行为的操作按钮。
   *
   * Args:
   *     label (string): 按钮显示文本。
   *     action (() => void): 点击按钮时执行的操作。
   *     variant ("secondary" | "danger"): 成功或失败提示的显示类型。
   *
   * Returns:
   *     HTMLButtonElement: 已绑定点击事件的按钮元素。
   */
  private actionButton(
    label: string,
    action: () => void,
    variant: "secondary" | "danger" = "secondary",
  ): HTMLButtonElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `ui-button ui-button--${variant} ui-button--small`;
    button.textContent = label;
    button.addEventListener("click", action);
    return button;
  }

  /**
   * 打开新增或编辑 MCP Server 的表单。
   *
   * Args:
   *     server (MCPServerView): 可选的已有 Server；未提供时进入新增模式。
   */
  private openEditor(server?: MCPServerView): void {
    this.editingId = server?.config.id ?? null;
    this.testedFingerprint = server?.runtime.status === "available"
      ? fingerprintMcpConfig(server.config)
      : null;
    this.authorizedInsecureFingerprint = null;
    this.q<HTMLFormElement>("#mcp-editor").hidden = false;
    this.syncPageVisibility();
    this.q<HTMLElement>("#mcp-editor-title").textContent = server ? "编辑 MCP 服务" : "添加 MCP 服务";
    this.q<HTMLInputElement>("#mcp-id").value = server?.config.id ?? "";
    this.q<HTMLInputElement>("#mcp-id").disabled = Boolean(server);
    this.q<HTMLInputElement>("#mcp-name").value = server?.config.name ?? "";
    this.q<HTMLInputElement>("#mcp-enabled").checked = server?.config.enabled ?? false;
    this.q<HTMLInputElement>("#mcp-connect-timeout").value = String(server?.config.connect_timeout_seconds ?? 15);
    this.q<HTMLInputElement>("#mcp-call-timeout").value = String(server?.config.call_timeout_seconds ?? 60);
    this.q<HTMLSelectElement>("#mcp-transport").value = server?.config.transport ?? "stdio";
    this.q<HTMLInputElement>("#mcp-command").value = server?.config.transport === "stdio" ? server.config.command : "";
    this.q<HTMLTextAreaElement>("#mcp-args").value = server?.config.transport === "stdio" ? server.config.args.join("\n") : "";
    this.q<HTMLInputElement>("#mcp-cwd").value = server?.config.transport === "stdio" ? server.config.cwd ?? "" : "";
    this.q<HTMLInputElement>("#mcp-url").value = server?.config.transport === "streamable_http" ? server.config.url : "";
    this.replaceSecretRows("env", server?.config.transport === "stdio" ? server.config.env : {});
    this.replaceSecretRows("header", server?.config.transport === "streamable_http" ? server.config.headers : {});
    this.q<HTMLDivElement>("#mcp-test-result").hidden = true;
    this.updateTransportFields();
  }

  /**
   * 关闭编辑表单并清理本次测试和授权状态。
   */
  private closeEditor(): void {
    this.q<HTMLFormElement>("#mcp-editor").hidden = true;
    this.syncPageVisibility();
    this.editingId = null;
    this.testedFingerprint = null;
    this.authorizedInsecureFingerprint = null;
  }

  /**
   * 根据传输类型切换 stdio 和 HTTP 配置字段。
   */
  private updateTransportFields(): void {
    const isHttp = this.q<HTMLSelectElement>("#mcp-transport").value === "streamable_http";
    this.q<HTMLDivElement>("#mcp-stdio-fields").hidden = isHttp;
    this.q<HTMLDivElement>("#mcp-http-fields").hidden = !isHttp;
  }

  /**
   * 使用配置值重建环境变量或 Header 密钥行。
   *
   * Args:
   *     kind ("env" | "header"): 密钥行类型。
   *     values (Record<string, string>): 键值形式的配置内容。
   */
  private replaceSecretRows(kind: "env" | "header", values: Record<string, string>): void {
    const rows = kind === "env" ? this.envRows : this.headerRows;
    rows.splice(0).forEach(/**
     * 替换敏感字段集合前移除旧行的 DOM 元素。
     *
     * Args:
     *     row (SecretRow): 当前服务配置的一行可编辑字段。
     */
    (row) => row.element.remove());
    for (const [key, value] of Object.entries(values)) this.addSecretRow(kind, key, value);
  }

  /**
   * 添加一行支持按住显示的密钥输入控件。
   *
   * Args:
   *     kind ("env" | "header"): 环境变量或 HTTP Header。
   *     initialKey (string): 初始键名。
   *     initialValue (string): 初始密钥值。
   */
  private addSecretRow(kind: "env" | "header", initialKey = "", initialValue = ""): void {
    const element = document.createElement("div");
    element.className = "mcp-secret-row";
    const key = document.createElement("input");
    key.className = "ui-control ui-control--small";
    key.placeholder = kind === "env" ? "VARIABLE" : "Header-Name";
    key.value = initialKey;
    const value = document.createElement("input");
    value.className = "ui-control ui-control--small";
    value.type = "password";
    value.placeholder = "值";
    value.value = initialValue;
    const reveal = this.actionButton("按住显示", /**
     * 该字段行没有额外的更改回调，不对输入变化执行额外动作。
     */
    () => undefined);
    reveal.addEventListener("pointerdown", /**
     * 用户开始按住查看按钮时临时将敏感输入框改为明文。
     */
    () => { value.type = "text"; });
    /**
     *  在按住显示结束后重新遮蔽密钥值。
     */
    const hide = (): void => { value.type = "password"; };
    reveal.addEventListener("pointerup", hide);
    reveal.addEventListener("pointerleave", hide);
    reveal.addEventListener("pointercancel", hide);
    reveal.addEventListener("blur", hide);
    const rows = kind === "env" ? this.envRows : this.headerRows;
    const row: SecretRow = { key, value, element };
    const remove = this.actionButton("删除", /**
     * 用户删除敏感字段行后同步移除内存记录，并让之前连接测试结果失效。
     */
    () => {
      element.remove();
      rows.splice(rows.indexOf(row), 1);
      this.testedFingerprint = null;
    }, "danger");
    element.append(key, value, reveal, remove);
    this.q<HTMLDivElement>(kind === "env" ? "#mcp-env-rows" : "#mcp-header-rows").append(element);
    rows.push(row);
  }

  /**
   * 收集有效密钥输入行并转换为键值对象。
   *
   * Args:
   *     rows (SecretRow[]): 待收集的密钥输入行。
   *
   * Returns:
   *     Record<string, string>: 已忽略空键名的配置对象。
   */
  private collectRows(rows: SecretRow[]): Record<string, string> {
    return Object.fromEntries(rows.filter(/**
     * 收集敏感配置时跳过未填写字段名称的行。
     *
     * Args:
     *     row (SecretRow): 当前服务配置的一行可编辑字段。
     *
     * Returns:
     *     string: 字段名去掉空白后的文本，空名不会被保留。
     */
    (row) => row.key.value.trim()).map(/**
     * 把已命名的环境变量或请求头行转成配置键值对。
     *
     * Args:
     *     row (SecretRow): 当前服务配置的一行可编辑字段。
     *
     * Returns:
     *     [string, string]: 去除名称空白后的字段名和原值。
     */
    (row) => [row.key.value.trim(), row.value.value]));
  }

  /**
   * 读取并执行前端基础校验后构造 Server 配置。
   *
   * @throws {Error} ID、名称、Endpoint 或 stdio 命令不合法时抛出。
   *
   * Returns:
   *     MCPServerConfig: 与所选传输类型匹配的 MCP Server 配置。
   */
  private readConfig(): MCPServerConfig {
    const common = {
      id: this.q<HTMLInputElement>("#mcp-id").value.trim(),
      name: this.q<HTMLInputElement>("#mcp-name").value.trim(),
      enabled: this.q<HTMLInputElement>("#mcp-enabled").checked,
      connect_timeout_seconds: Number(this.q<HTMLInputElement>("#mcp-connect-timeout").value),
      call_timeout_seconds: Number(this.q<HTMLInputElement>("#mcp-call-timeout").value),
    };
    if (!common.id || !/^[A-Za-z0-9_-]{1,32}$/.test(common.id)) throw new Error("ID 只能包含字母、数字、下划线和连字符");
    if (!common.name) throw new Error("名称不能为空");
    if (this.q<HTMLSelectElement>("#mcp-transport").value === "streamable_http") {
      const url = this.q<HTMLInputElement>("#mcp-url").value.trim();
      if (classifyHttpEndpoint(url).kind === "invalid") throw new Error("Endpoint 必须是有效的 HTTP 或 HTTPS 地址");
      return { ...common, transport: "streamable_http", url, headers: this.collectRows(this.headerRows) };
    }
    const command = this.q<HTMLInputElement>("#mcp-command").value.trim();
    if (!command) throw new Error("stdio 命令不能为空");
    return {
      ...common,
      transport: "stdio",
      command,
      args: this.q<HTMLTextAreaElement>("#mcp-args").value.split(/\r?\n/).map(/**
       * 逐项清理命令参数开头和末尾空白。
       *
       * Args:
       *     value (string): 当前待检查或转换的值。
       *
       * Returns:
       *     string: 规范化后的参数文本。
       */
      (value) => value.trim()).filter(Boolean),
      cwd: this.q<HTMLInputElement>("#mcp-cwd").value.trim() || null,
      env: this.collectRows(this.envRows),
    };
  }

  /**
   * 在 HTTP Endpoint 变化时执行不安全连接确认。
   *
   * Args:
   *     config (MCPServerConfig): 当前准备测试或保存的 Server 配置。
   *
   * Returns:
   *     Promise<boolean>: 用户是否允许继续使用该 Endpoint。
   */
  private async authorizeHttp(config: MCPServerConfig): Promise<boolean> {
    if (config.transport !== "streamable_http") return true;
    const fingerprint = fingerprintMcpConfig(config);
    if (this.authorizedInsecureFingerprint === fingerprint) return true;
    const previous = this.servers.find(/**
     * 查找当前编辑的已保存服务，供比较配置是否变化。
     *
     * Args:
     *     server (MCPServerView): 当前待校验 HTTP 地址的 MCP 服务配置。
     *
     * Returns:
     *     boolean: 服务编号等于当前编辑编号时为 true。
     */
    (server) => server.config.id === this.editingId)?.config;
    const approved = await authorizeBaseUrlChange({
      previousUrl: previous?.transport === "streamable_http" ? previous.url : "",
      nextUrl: config.url,
      /**
       * 用户填写远程 HTTP 地址时显示风险确认提示，等待用户明确选择。
       *
       * Args:
       *     url (string): 待检查或打开的地址。
       *
       * Returns:
       *     Promise<boolean>: 用户允许该地址时为 true。
       */
      confirmInsecure: (url) => this.confirmDialog.open({
        title: "未加密 MCP 连接警告",
        message: `MCP Headers 可能通过未加密 HTTP 发送到 ${url}。`,
        confirmText: "仍然继续",
        cancelText: "取消",
        variant: "warning",
      }),
    });
    if (approved && classifyHttpEndpoint(config.url).kind === "insecure_http") {
      this.authorizedInsecureFingerprint = fingerprint;
    }
    return approved;
  }

  /**
   * 测试编辑表单中的 MCP Server 配置并记录测试指纹。
   */
  private async testCurrent(): Promise<void> {
    try {
      const config = this.readConfig();
      if (!await this.authorizeHttp(config)) return;
      const result = await window.desktopPetApi.testMcpServer(config);
      this.testedFingerprint = fingerprintMcpConfig(config);
      const element = this.q<HTMLDivElement>("#mcp-test-result");
      element.hidden = false;
      element.textContent = `连接成功，发现 ${result.tools.length} 个工具：${result.tools.map(/**
       * 连接测试成功后提取工具名称，供权限目录同步。
       *
       * Args:
       *     tool (MCPToolInfo): 当前服务返回的工具目录项。
       *
       * Returns:
       *     string: 当前工具名称。
       */
      (tool) => tool.name).join("、") || "无"}`;
      this.toast.success("MCP 连接测试成功");
    } catch (error) {
      this.toast.error(appendErrorMessage("MCP 连接测试失败", error));
    }
  }

  /**
   * 校验测试状态后创建或更新 MCP Server。
   */
  private async saveCurrent(): Promise<void> {
    try {
      const config = this.readConfig();
      if (config.enabled && this.testedFingerprint !== fingerprintMcpConfig(config)) {
        throw new Error("启用的服务必须在最后一次修改连接参数后通过测试");
      }
      if (!await this.authorizeHttp(config)) return;
      if (this.editingId) await window.desktopPetApi.updateMcpServer(this.editingId, config);
      else await window.desktopPetApi.createMcpServer(config);
      this.toast.success("MCP 服务已保存");
      this.closeEditor();
      await this.callback?.({ type: "settings-invalidated", page: "mcp" });
    } catch (error) {
      this.toast.error(appendErrorMessage("保存 MCP 服务失败", error));
    }
  }

  /**
   * 测试一个已保存的 MCP Server 配置。
   *
   * Args:
   *     server (MCPServerView): 待测试的已保存 Server。
   */
  private async testSaved(server: MCPServerView): Promise<void> {
    try {
      const result = await window.desktopPetApi.testMcpServer(server.config);
      this.toast.success(`连接成功，发现 ${result.tools.length} 个工具`);
    } catch (error) {
      this.toast.error(appendErrorMessage("测试 MCP 服务失败", error));
    }
  }

  /**
   * 请求后端重连指定 MCP Server 并刷新页面。
   *
   * Args:
   *     serverId (string): 待重连的 MCP Server ID。
   */
  private async reconnect(serverId: string): Promise<void> {
    try {
      await window.desktopPetApi.reconnectMcpServer(serverId);
      await this.callback?.({ type: "settings-invalidated", page: "mcp" });
      this.toast.success("MCP 服务已重连");
    } catch (error) {
      this.toast.error(appendErrorMessage("重连 MCP 服务失败", error));
    }
  }

  /**
   * 经用户确认后删除 MCP Server 及模型引用。
   *
   * Args:
   *     serverId (string): 待删除的 MCP Server ID。
   *     name (string): 用于确认对话框展示的 Server 名称。
   */
  private async remove(serverId: string, name: string): Promise<void> {
    const server = this.servers.find(/**
     * 服务操作完成后按编号查找需要更新的目录记录。
     *
     * Args:
     *     item (MCPServerView): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 当前服务编号匹配时为 true。
     */
    (item) => item.config.id === serverId);
    const policyCount = server?.affected_model_count ?? 0;
    const confirmed = await this.confirmDialog.open({
      title: "删除 MCP 服务",
      message: `将删除 ${name} 并清理所有模型引用。受影响模型：${policyCount} 个。`,
      confirmText: "确认删除",
      cancelText: "取消",
      variant: "danger",
    });
    if (!confirmed) return;
    try {
      const result = await window.desktopPetApi.deleteMcpServer(serverId);
      delete this.policies.servers[serverId];
      await this.callback?.({ type: "settings-invalidated", page: "mcp" });
      this.toast.success(`已删除，清理 ${result.affected_sessions.length} 个模型引用`);
    } catch (error) {
      this.toast.error(appendErrorMessage("删除 MCP 服务失败", error));
    }
  }

  /**
   * 重新读取全部 MCP Server 和工具目录并刷新卡片。
   */
  private async refresh(): Promise<void> {
    this.servers = await window.desktopPetApi.getMcpServers();
    const entries = await Promise.all(this.servers.map(/**
     * 并行读取每个服务的工具目录，单个失败返回该服务空目录，不阻断其他服务。
     *
     * Args:
     *     server (MCPServerView): 当前待校验 HTTP 地址的 MCP 服务配置。
     *
     * Returns:
     *     Promise<readonly [string, MCPToolInfo[]]>: 服务编号与工具列表组成的键值对。
     */
    async (server) => {
      try { return [server.config.id, await window.desktopPetApi.getMcpServerTools(server.config.id)] as const; }
      catch { return [server.config.id, [] as MCPToolInfo[]] as const; }
    }));
    this.tools = new Map(entries);
    this.renderServers();
  }
}
