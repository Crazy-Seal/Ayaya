import { PageEventType } from "../types.js";
import type {
  ChatSettingsState,
  ISettingsPage,
  PageEditingData,
  PageEventCallback,
  PageRenderData,
  PluginItem,
} from "../types.js";

type PluginSettingsMap = NonNullable<ChatSettingsState["agent_plugins"]>;

const CONFIG_LABELS: Record<string, string> = {
  enable_diary: "启用日记",
  enable_episodic: "启用情景记忆",
  enable_semantic: "启用语义记忆",
  long_term_memory_every_human_messages: "每多少轮总结长期记忆",
};

const NUMBER_FIELDS = new Set([
  "long_term_memory_every_human_messages",
]);

const BOOLEAN_FIELDS = new Set([
  "enable_diary",
  "enable_episodic",
  "enable_semantic",
]);

type JsonSchemaProperty = {
  minimum?: unknown;
};

type JsonSchemaWithProperties = {
  properties?: Record<string, JsonSchemaProperty>;
};

/**
 * 检查插件配置结构是否包含字段定义，避免直接读取未知响应形状。
 *
 * Args:
 *     schema (unknown): 需要检查的配置结构定义。
 *
 * Returns:
 *     schema is JsonSchemaWithProperties: 对象包含 properties 时为 true。
 */
const hasSchemaProperties = (schema: unknown): schema is JsonSchemaWithProperties => (
  typeof schema === "object" && schema !== null && "properties" in schema
);

/**
 * 读取插件数字配置字段的最小值，没有合法定义时使用零。
 *
 * Args:
 *     plugin (PluginItem): 后端返回的插件目录项和配置定义。
 *     key (string): 当前配置字段或索引名称。
 *
 * Returns:
 *     number: 当前数字输入框应使用的最小值。
 */
export const getNumberFieldMinimum = (plugin: PluginItem, key: string): number => {
  if (!hasSchemaProperties(plugin.config_schema)) {
    return 0;
  }

  const minimum = plugin.config_schema.properties?.[key]?.minimum;
  return typeof minimum === "number" ? minimum : 0;
};

/**
 * 展示插件目录并编辑记忆能力配置，固有插件始终保持启用。
 */
export class PluginsPage implements ISettingsPage {
  private container: HTMLDivElement;
  private empty: HTMLDivElement;
  private confirmBtn: HTMLButtonElement;
  private eventCallback?: PageEventCallback;

  /**
   * 绑定插件卡片容器、空状态及保存按钮。
   *
   * Args:
   *     container (HTMLDivElement): 当前组件负责维护的页面容器。
   *     empty (HTMLDivElement): 没有可展示记录时显示的占位元素。
   *     confirmBtn (HTMLButtonElement): 用户点击后提交本页修改的按钮。
   */
  constructor(
    container: HTMLDivElement,
    empty: HTMLDivElement,
    confirmBtn: HTMLButtonElement
  ) {
    this.container = container;
    this.empty = empty;
    this.confirmBtn = confirmBtn;
    this.confirmBtn.addEventListener("click", /**
     * 用户点击保存后通知设置控制器收集并提交记忆插件配置。
     */
    () => {
      this.eventCallback?.({ type: PageEventType.SUBMIT, page: "memory" });
    });
  }

  /**
   * 登记主设置控制器的页面事件回调。
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void {
    this.eventCallback = callback;
  }

  /**
   * 合并目录默认值和已保存配置，逐项渲染插件卡片；没有目录时显示空提示。
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void {
    const availablePlugins = data.dependencies?.availablePlugins || [];
    const savedPlugins = data.saved.agent_plugins || {};
    this.container.innerHTML = "";

    if (availablePlugins.length === 0) {
      this.empty.hidden = false;
      return;
    }

    this.empty.hidden = true;
    for (const plugin of availablePlugins) {
      this.container.appendChild(this.renderPluginCard(plugin, savedPlugins));
    }
  }

  /**
   * 读取插件卡片的开关和数字值，固有插件不能通过页面禁用。
   *
   * Returns:
   *     PageEditingData: 尚未保存的插件配置。
   */
  getEditingData(): PageEditingData {
    const agent_plugins: PluginSettingsMap = {};
    this.container.querySelectorAll<HTMLElement>("[data-plugin-name]").forEach(/**
     * 逐张插件卡片收集启用状态和配置；固有插件的启用标记始终保持 true。
     *
     * Args:
     *     card (HTMLElement): 当前正在收集编辑数据的插件卡片。
     */
    (card) => {
      const pluginName = card.dataset.pluginName;
      if (!pluginName) return;

      const enabledInput = card.querySelector<HTMLInputElement>('input[data-plugin-enabled="true"]');
      const inherent = card.dataset.pluginInherent === "true";
      const config: Record<string, unknown> = {};

      card.querySelectorAll<HTMLInputElement>("input[data-config-key]").forEach(/**
       * 逐项按字段名读取开关布尔值或数字值，忽略缺少字段名的控件。
       *
       * Args:
       *     input (HTMLInputElement): 当前日志输入或配置输入框。
       */
      (input) => {
        const key = input.dataset.configKey;
        if (!key) return;
        config[key] = input.type === "checkbox" ? input.checked : Number(input.value);
      });

      agent_plugins[pluginName] = {
        enabled: inherent || Boolean(enabledInput?.checked),
        config,
      };
    });

    return { plugins: { agent_plugins } };
  }

  /**
   * 用目录信息和已保存配置创建单个插件卡片及受支持配置字段。
   *
   * Args:
   *     plugin (PluginItem): 后端返回的插件目录项和配置定义。
   *     savedPlugins (PluginSettingsMap): 已经保存的插件配置，编辑时与默认值合并。
   *
   * Returns:
   *     HTMLElement: 带插件身份和固有标记的卡片元素。
   */
  private renderPluginCard(plugin: PluginItem, savedPlugins: PluginSettingsMap): HTMLElement {
    const saved = savedPlugins[plugin.name];
    const config = {
      ...plugin.default_config,
      ...(saved?.config || {}),
    };
    const enabled = plugin.inherent || saved?.enabled === true;

    const card = document.createElement("div");
    card.className = "plugin-card ui-surface";
    card.dataset.pluginName = plugin.name;
    card.dataset.pluginInherent = String(plugin.inherent);

    const header = document.createElement("div");
    header.className = "plugin-card-header";

    const titleWrap = document.createElement("div");
    const title = document.createElement("div");
    title.className = "plugin-card-title";
    title.textContent = plugin.name;
    const desc = document.createElement("div");
    desc.className = "plugin-card-desc";
    desc.textContent = plugin.description || "暂无说明";
    titleWrap.append(title, desc);

    const toggleLabel = document.createElement("label");
    toggleLabel.className = "plugin-toggle";
    const toggleText = document.createElement("span");
    toggleText.textContent = plugin.inherent ? "固有插件" : "启用";
    const toggle = document.createElement("input");
    toggle.type = "checkbox";
    toggle.className = "ui-checkbox";
    toggle.dataset.pluginEnabled = "true";
    toggle.checked = enabled;
    toggle.disabled = plugin.inherent;
    toggleLabel.append(toggleText, toggle);

    header.append(titleWrap, toggleLabel);
    card.appendChild(header);

    const body = document.createElement("div");
    body.className = "plugin-config-grid";
    for (const [key, value] of Object.entries(config)) {
      if (!NUMBER_FIELDS.has(key) && !BOOLEAN_FIELDS.has(key)) continue;
      body.appendChild(this.renderConfigField(plugin, key, value));
    }
    card.appendChild(body);

    return card;
  }

  /**
   * 按字段类别生成开关或数字控件，并设置后端声明的最小值。
   *
   * Args:
   *     plugin (PluginItem): 后端返回的插件目录项和配置定义。
   *     key (string): 当前配置字段或索引名称。
   *     value (unknown): 当前待检查或转换的值。
   *
   * Returns:
   *     HTMLElement: 配置字段标签及输入框。
   */
  private renderConfigField(plugin: PluginItem, key: string, value: unknown): HTMLElement {
    const label = document.createElement("label");
    label.className = "plugin-config-field";

    const text = document.createElement("span");
    text.textContent = CONFIG_LABELS[key] || key;

    const input = document.createElement("input");
    input.dataset.configKey = key;
    if (BOOLEAN_FIELDS.has(key)) {
      input.type = "checkbox";
      input.className = "ui-checkbox";
      input.checked = Boolean(value);
    } else {
      input.type = "number";
      input.className = "ui-control ui-control--small";
      input.min = String(getNumberFieldMinimum(plugin, key));
      input.step = "1";
      input.value = String(typeof value === "number" ? value : 0);
    }

    label.append(text, input);
    return label;
  }
}
