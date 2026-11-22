/**
 * 设置窗口入口
 */
import { PageEventType } from "./types.js";
import { MotionSettingType } from "../../shared-types.js";

import "../settings.css";
import { initializeIcons } from "../ui/icons.js";
import { setupApplicationShutdownUi } from "../ui/application-shutdown.js";
import { confirmApplicationExit } from "../ui/application-exit.js";

import { ConfirmDialog } from "../ui/confirm-dialog.js";
import { SettingsToast, appendErrorMessage } from "./components/settings-toast.js";
import { buildSystemPrompt } from "./utils/prompt-builder.js";
import { ModelPage } from "./pages/model-page.js";
import { LlmPage } from "./pages/llm-page.js";
import { ToolsPage } from "./pages/tools-page.js";
import { PluginsPage } from "./pages/plugins-page.js";
import { ContextStrategyPage } from "./pages/context-strategy-page.js";
import { authorizeBaseUrlChange } from "./url-security.js";
import { MotionPage } from "./pages/motion-page.js";
import { McpPage } from "./pages/mcp-page.js";
import { BackgroundTasksPage } from "./pages/background-tasks-page.js";
import { SubagentPanel } from "./pages/subagent-panel.js";
import { InteractionEventKind } from "../../shared-types.js";
import type {
  ChatSettingsState,
  ModelConfig,
  EditingState,
  ISettingsPage,
  PageRenderData,
  PageEventCallback,
  PageEvent,
} from "./types.js";

/**
 * 装配模型设置和后台任务等设置页面。
 *
 * Attributes:
 *     tasksPage (BackgroundTasksPage): 当前会话的任务记录和输出设置页。
 */
class SettingsApp {
  private sidebar: HTMLDivElement;
  private minBtn: HTMLButtonElement;
  private closeBtn: HTMLButtonElement;
  private logWindowBtn: HTMLButtonElement;

  private modelPage: ModelPage;
  private llmPage: ISettingsPage;
  private toolsPage: ISettingsPage;
  private pluginsPage: ISettingsPage;
  private contextStrategyPage: ISettingsPage;
  private motionPage: ISettingsPage;
  private mcpPage: ISettingsPage;
  private tasksPage: BackgroundTasksPage;
  private subagentPanel: SubagentPanel;
  private confirmDialog: ConfirmDialog;
  private toast: SettingsToast;

  private currentPage = ["tasks", "subagents"].find(/** 只接受已有独立页面导航值。 */
    (page) => page === new URL(window.location.href).searchParams.get("page")) ?? "model";

  /** 已保存状态（来自后端，不可变） */
  private savedState: ChatSettingsState | null = null;
  /** 模型配置（已保存） */
  private modelConfig: ModelConfig | null = null;

  /** 页面映射 */
  private pages: Map<string, ISettingsPage | null> = new Map();

  /**
   * 装配 SettingsApp 的依赖并初始化本地视图状态。
   */
  constructor() {
    initializeIcons();
    // 获取 DOM 元素
    this.sidebar = this.getElement("#settings-sidebar");
    this.minBtn = this.getElement("#settings-min-btn");
    this.closeBtn = this.getElement("#settings-close-btn");
    this.logWindowBtn = this.getElement("#open-log-window-btn");
    this.toast = new SettingsToast(this.getElement("#settings-toast"));

    // 初始化确认对话框
    this.confirmDialog = new ConfirmDialog(
      this.getElement("#delete-confirm-dialog"),
      this.getElement("#delete-confirm-cancel"),
      this.getElement("#delete-confirm-ok"),
      this.getElement("#delete-confirm-title"),
      this.getElement("#delete-confirm-message")
    );

    // 初始化模型管理页面
    this.modelPage = new ModelPage(
      this.getElement("#model-list"),
      this.getElement("#import-preview"),
      this.getElement("#preview-name"),
      this.getElement("#preview-type"),
      this.getElement("#preview-entry"),
      this.getElement("#confirm-import-btn"),
      this.getElement("#cancel-import-btn"),
      this.getElement("#import-model-btn"),
      this.getElement("#slider-offset-x"),
      this.getElement("#slider-offset-y"),
      this.getElement("#slider-offset-x-value"),
      this.getElement("#slider-offset-y-value"),
      this.getElement("#checkbox-follow-cursor"),
      this.confirmDialog,
      this.toast
    );

    // 初始化 LLM 配置页面
    this.llmPage = new LlmPage(
      this.getElement("#llm-base-url"),
      this.getElement("#llm-api-key"),
      this.getElement("#llm-api-key-reveal"),
      this.getElement("#llm-model-name"),
      this.getElement("#llm-temperature"),
      this.getElement("#llm-system-prompt"),
      this.getElement("#llm-confirm-btn"),
      this.getElement("#llm-name"),
      this.getElement("#llm-feature"),
      this.getElement("#llm-character"),
      this.getElement("#llm-address"),
      this.getElement("#llm-characteristic"),
      this.getElement("#llm-constraint")
    );

    // 初始化工具配置页面
    this.toolsPage = new ToolsPage(
      this.getElement("#tools-table-body"),
      this.getElement("#tools-empty"),
      this.getElement("#tools-confirm-btn")
    );

    // 初始化插件配置页面
    this.pluginsPage = new PluginsPage(
      this.getElement("#plugins-list"),
      this.getElement("#plugins-empty"),
      this.getElement("#plugins-confirm-btn")
    );

    this.contextStrategyPage = new ContextStrategyPage(
      {
        compression_trigger_tokens_k: this.getElement("#context-compression-trigger-tokens-k"),
      },
      this.getElement("#context-strategy-confirm-btn")
    );


    // 初始化动作控制页面
    this.motionPage = new MotionPage(
      this.getElement("#motion-table-body"),
      this.getElement("#motion-empty"),
      this.getElement("#motion-confirm-btn"),
      this.getElement("#motion-system-prompt")
    );
    this.mcpPage = new McpPage(this.getElement("#mcp-page"), this.confirmDialog, this.toast);
    this.tasksPage = new BackgroundTasksPage(
      this.getElement("#background-tasks-list"),
      this.getElement("#refresh-background-tasks"),
      this.toast,
    );

    this.subagentPanel = new SubagentPanel(this.getElement("#subagents-list"),
      this.getElement("#refresh-subagents"), this.toast);

    // 注册页面到映射
    this.pages.set("llm", this.llmPage);
    this.pages.set("tools", this.toolsPage);
    this.pages.set("memory", this.pluginsPage);
    this.pages.set("contextStrategy", this.contextStrategyPage);
    this.pages.set("motion", this.motionPage);
    this.pages.set("mcp", this.mcpPage);

    // 设置事件回调
    /**
     * 接收各设置页面的提交事件，并交给主控制器按页面类型保存。
     *
     * Args:
     *     event (PageEvent): 调用方或浏览器触发的当前事件。
     */
    const eventCallback: PageEventCallback = async (event) => {
      await this.handlePageEvent(event);
    };
    this.llmPage.onEvent(eventCallback);
    this.toolsPage.onEvent(eventCallback);
    this.pluginsPage.onEvent(eventCallback);
    this.contextStrategyPage.onEvent(eventCallback);
    this.motionPage.onEvent(eventCallback);
    this.mcpPage.onEvent(eventCallback);

    this.setupEventListeners();
  }

  /**
   * 获取 DOM 元素
   *
   * Args:
   *     selector (string): 必须存在的页面元素选择器。
   */
  private getElement<T extends HTMLElement>(selector: string): T {
    const element = document.querySelector<T>(selector);
    if (!element) {
      throw new Error(`设置窗口初始化失败：找不到元素 ${selector}`);
    }
    return element;
  }

  /**
   * 设置事件监听
   */
  private setupEventListeners(): void {
    const unsubscribeSettingsPage = window.desktopPetApi.onOpenSettingsPage(/**
     * 主进程请求打开指定设置页时交给页面切换流程。
     *
     * Args:
     *     page ("tasks"): 需要打开的设置页面名称。
     */
    (page) => { this.switchPage(page); });
    const unsubscribeTasks = window.desktopPetApi.onInteractionEvent(/**
     * 只在任务页可见且事件属于当前会话时刷新任务状态，其他运行事件不触发任务查询。
     *
     * Args:
     *     event (InteractionEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      if (this.currentPage === "tasks" && event.session_id === this.savedState?.session_id
          && (event.kind === InteractionEventKind.TASK_STATUS || event.kind === InteractionEventKind.SESSION_SNAPSHOT)) {
        void this.tasksPage.render(event.session_id);
      }
      if (this.currentPage === "subagents" && event.session_id === this.savedState?.session_id
          && (event.kind === InteractionEventKind.SUBAGENT_STATUS || event.kind === InteractionEventKind.SESSION_SNAPSHOT)) {
        void this.subagentPanel.render(event.session_id);
      }
    });
    // 侧边栏导航
    this.sidebar.addEventListener("click", /**
     * 用户点击侧栏页签时提取页面名称并切换，其他侧栏点击忽略。
     *
     * Args:
     *     event (PointerEvent): 调用方或浏览器触发的当前事件。
     */
    (event) => {
      const target = event.target as HTMLElement | null;
      const tab = target?.closest<HTMLButtonElement>(".settings-tab");
      if (!tab) {
        return;
      }

      const page = tab.dataset.page;
      if (!page || page === this.currentPage) {
        return;
      }

      this.switchPage(page);
    });

    // 最小化按钮
    this.minBtn.addEventListener("click", /**
     * 用户点击标题栏最小化时请求主进程操作当前设置窗口。
     */
    () => {
      window.desktopPetApi.minimizeCurrentWindow();
    });

    // 关闭按钮
    this.getElement<HTMLButtonElement>("#exit-application-btn").addEventListener("click", /**
     * 用户确认退出后停止整个应用；取消保留当前聊天与设置状态。
     */
    async () => {
      await confirmApplicationExit(this.confirmDialog);
    });

    this.closeBtn.addEventListener("click", /**
     * 用户点击标题栏关闭时请求主进程关闭设置窗口。
     */
    () => {
      window.desktopPetApi.closeCurrentWindow();
    });
    this.logWindowBtn.addEventListener("click", /**
     * 用户点击独立日志入口时请求主进程打开日志窗口。
     */
    () => {
      window.desktopPetApi.openLogWindow();
    });

    // 模型变化监听
    const unsubscribeModelChanged = window.desktopPetApi.onModelChanged?.(/**
     * 活动模型变化后重新读取当前会话配置和页面依赖。
     */
    () => {
      void this.refreshAfterModelChanged();
    });

    // 模型变换监听
    const unsubscribeTransformChanged = window.desktopPetApi.onModelTransformChanged?.(/**
     * 模型显示参数通知到达后更新模型设置页的缓存和控件。
     *
     * Args:
     *     payload (ModelTransformData): 已经过入口校验的桌面动作参数。
     */
    (payload) => {
      this.modelPage.updateTransformData(payload);
    });

    // 页面卸载清理
    window.addEventListener("beforeunload", /**
     * 设置页卸载时取消页面与任务订阅，并停止任务页轮询和模型参数监听。
     */
    () => {
      unsubscribeSettingsPage();
      unsubscribeTasks();
      this.tasksPage.setActive(false);
      this.subagentPanel.dispose();
      if (typeof unsubscribeTransformChanged === "function") {
        unsubscribeTransformChanged();
      }
      if (typeof unsubscribeModelChanged === "function") {
        unsubscribeModelChanged();
      }
      this.toast.dispose();
    });
  }

  /**
   * 切换设置页并只在任务页可见时启用运行任务的定时刷新。
   *
   * Args:
   *     newPage (string): 当前选择的设置页名称。
   */
  private switchPage(newPage: string): void {
    // 更新当前页面
    this.currentPage = newPage;
    this.tasksPage.setActive(newPage === "tasks");
    this.subagentPanel.setActive(newPage === "subagents");

    // 更新 UI
    document.querySelectorAll<HTMLButtonElement>(".settings-tab").forEach(/**
     * 页面切换后按页面名称更新侧栏选中样式。
     *
     * Args:
     *     item (HTMLButtonElement): 当前遍历的配置或目录条目。
     */
    (item) => {
      item.classList.toggle("active", item.dataset.page === newPage);
    });

    document.querySelectorAll<HTMLDivElement>(".settings-page").forEach(/**
     * 页面切换后按页面名称更新内容区显示状态。
     *
     * Args:
     *     item (HTMLDivElement): 当前遍历的配置或目录条目。
     */
    (item) => {
      item.classList.toggle("active", item.dataset.page === newPage);
    });

    // 渲染新页面（只显示已保存状态）
    void this.renderPage(newPage);
  }

  /**
   * 根据选中的设置页加载当前会话数据和后台任务。
   *
   * Args:
   *     pageName (string): 当前定义使用的 pageName 参数。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async renderPage(pageName: string): Promise<void> {
    if (!this.savedState) {
      return;
    }

    const page = this.pages.get(pageName);
    if (!page) {
      // model 页面特殊处理
      if (pageName === "model") {
        void this.modelPage.refreshModelConfig();
      // 1. 任务页按当前保存会话查询，不修改模型设置的保存流程。
      } else if (pageName === "tasks") {
        await this.tasksPage.render(this.savedState.session_id);
      } else if (pageName === "subagents") {
        await this.subagentPanel.render(this.savedState.session_id);
      }
      return;
    }

    const renderData: PageRenderData = {
      saved: this.savedState,
      dependencies: await this.getPageDependencies(pageName),
    };

    page.render(renderData);
  }

  /**
   * 获取页面依赖数据
   *
   * Args:
   *     pageName (string): 本次切换、加载或提交的设置页面名称。
   */
  private async getPageDependencies(pageName: string): Promise<PageRenderData["dependencies"]> {
    const deps: PageRenderData["dependencies"] = {};

    if (pageName === "llm" || pageName === "motion") {
      deps.expressionLabels = this.getExpressionLabels();
    }

    if (pageName === "motion") {
      deps.modelConfig = this.modelConfig || undefined;
      if (this.modelConfig) {
        const activeModel = this.modelConfig.models.find(
          /**
           * 加载页面依赖时查找当前活动模型。
           *
           * Args:
           *     m ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前模型动作的配置记录。
           *
           * Returns:
           *     boolean: 模型编号等于活动编号时为 true。
           */
          (m) => m.id === this.modelConfig!.activeModelId
        );
        if (activeModel) {
          deps.availableMotions = await this.loadMotionsFromModel(
            activeModel.modelUrl || activeModel.entry || ""
          );
        }
      }
    }

    if (pageName === "tools") {
      try {
        // 1. 工具权限与服务开关同时加载，工具分组按启用状态决定首次是否展开。
        [deps.toolPolicies, deps.mcpServers] = await Promise.all([
          window.desktopPetApi.getToolPermissions(this.savedState?.session_id ?? ""),
          window.desktopPetApi.getMcpServers(),
        ]);
      } catch (error) {
        this.toast.error(appendErrorMessage("读取工具权限失败", error));
        deps.toolPolicies = [];
      }
    }

    if (pageName === "memory") {
      try {
        const result = await window.desktopPetApi.getAvailablePlugins();
        deps.availablePlugins = result.plugins;
        deps.pluginErrors = result.errors;
        [deps.toolPolicies, deps.pluginMcpStatuses] = await Promise.all([
          window.desktopPetApi.getToolPermissions(this.savedState?.session_id ?? ""),
          window.desktopPetApi.getPluginMcpStatuses(this.savedState?.session_id ?? ""),
        ]);
      } catch (error) {
        this.toast.error(appendErrorMessage("读取插件列表失败", error));
        deps.availablePlugins = [];
      }
    }

    if (pageName === "mcp") {
      try {
        deps.mcpServers = await window.desktopPetApi.getMcpServers();
        const toolEntries = await Promise.all(deps.mcpServers.map(/**
         * 并行查询当前会话各服务工具，单个失败仅使用该服务空目录。
         *
         * Args:
         *     server (MCPServerView): 当前待校验 HTTP 地址的 MCP 服务配置。
         *
         * Returns:
         *     Promise<readonly [string, MCPToolInfo[]]>: 服务编号与工具列表的键值对。
         */
        async (server) => {
          try {
            return [server.config.id, await window.desktopPetApi.getMcpServerTools(server.config.id)] as const;
          } catch {
            return [server.config.id, [] as import("../../shared-types.js").MCPToolInfo[]] as const;
          }
        }));
        deps.mcpTools = new Map(toolEntries);
      } catch (error) {
        this.toast.error(appendErrorMessage("读取 MCP 服务失败", error));
        deps.mcpServers = [];
        deps.mcpTools = new Map();
      }
    }

    return deps;
  }

  /**
   * 从模型加载动作列表
   *
   * Args:
   *     modelUrl (string): 当前活动模型入口的资源地址。
   */
  private async loadMotionsFromModel(modelUrl: string): Promise<string[]> {
    if (!modelUrl) {
      return [];
    }
    try {
      const response = await fetch(modelUrl);
      if (!response.ok) {
        return [];
      }
      const model3Json = await response.json();
      const motions = model3Json.FileReferences?.Motions;
      if (!motions) {
        return [];
      }
      return Object.keys(motions);
    } catch {
      return [];
    }
  }

  /**
   * 获取表情标签列表（从已保存状态）
   */
  private getExpressionLabels(): string[] {
    if (this.modelConfig) {
      const activeModel = this.modelConfig.models.find(
        /**
         * 构造表情提示词前查找当前活动模型。
         *
         * Args:
         *     m ({ id: string; name: string; sessionId: string; source: "builtin" | "custom"; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig?: MotionConfig[] | undefined; entry?: string | undefined; modelUrl?: string | undefined; }): 当前模型动作的配置记录。
         *
         * Returns:
         *     boolean: 当前模型为活动模型时为 true。
         */
        (m) => m.id === this.modelConfig!.activeModelId
      );
      if (activeModel?.motionConfig) {
        return activeModel.motionConfig
          .filter(/**
           * 仅保留带标签的表情规则，避免普通动作进入模型表情提示。
           *
           * Args:
           *     c (MotionConfig): 当前动作的配置项。
           *
           * Returns:
           *     string | false | undefined: 规则类型为表情且标签非空时为 true。
           */
          (c) => c.setting === MotionSettingType.EXPRESSION && c.label)
          .map(/**
           * 提取已筛选表情规则的标签。
           *
           * Args:
           *     c (MotionConfig): 当前动作的配置项。
           *
           * Returns:
           *     string: 表情名称。
           */
          (c) => c.label!);
      }
    }
    return [];
  }

  /**
   * 处理页面事件
   *
   * Args:
   *     event (PageEvent): 调用方或浏览器触发的当前事件。
   */
  private async handlePageEvent(event: PageEvent): Promise<void> {
    if (event.type === PageEventType.SUBMIT) {
      await this.handleSubmit(event.page);
    } else if (event.type === PageEventType.SETTINGS_INVALIDATED) {
      const loaded = await this.initChatSettings(true);
      if (loaded) await this.renderPage(event.page);
    }
  }

  /**
   * 提交保存
   *
   * Args:
   *     pageName (string): 本次切换、加载或提交的设置页面名称。
   */
  private async handleSubmit(pageName: string): Promise<void> {
    const page = this.pages.get(pageName);
    if (!page || !this.savedState) {
      return;
    }

    // 获取当前表单数据
    try {
      const editingData = page.getEditingData();
      let mcpAdjusted = false;
      if (pageName === "llm" && editingData.llm) {
        const saved = await this.saveLlmSettings(editingData.llm);
        if (!saved) {
          return;
        }
      } else if (pageName === "motion" && editingData.motion) {
        await this.saveMotionSettings(editingData.motion);
      } else if (pageName === "tools" && editingData.tools) {
        await this.saveToolsSettings(editingData.tools);
      } else if (pageName === "memory" && editingData.plugins) {
        await this.savePluginSettings(editingData.plugins);
      } else if (pageName === "contextStrategy" && editingData.contextStrategy) {
        await this.saveContextStrategy(editingData.contextStrategy);
      } else if (pageName === "mcp" && editingData.mcp) {
        mcpAdjusted = await this.saveMcpSettings(editingData.mcp);
      } else {
        return;
      }

      if (mcpAdjusted) {
        this.toast.error("部分 MCP 权限已由服务端安全调整");
      } else {
        this.toast.success("修改成功");
      }
      await this.renderPage(pageName);
    } catch (error) {
      this.toast.error(appendErrorMessage("修改失败", error));
    }
  }

  /**
   * 保存 LLM 设置
   *
   * Args:
   *     llmData (NonNullable<EditingState["llm"]>): 模型设置页正在编辑的模型参数和角色字段。
   */
  private async saveLlmSettings(llmData: NonNullable<EditingState["llm"]>): Promise<boolean> {
    if (!this.savedState) {
      return false;
    }

    const shouldSave = await authorizeBaseUrlChange({
      previousUrl: this.savedState.openai_base_url,
      nextUrl: llmData.openai_base_url,
      /**
       * 模型服务地址改为远程 HTTP 时显示确认提示并等待用户选择。
       *
       * Args:
       *     url (string): 待检查或打开的地址。
       *
       * Returns:
       *     Promise<boolean>: 用户确认允许新地址时为 true。
       */
      confirmInsecure: (url) => this.confirmDialog.open({
        title: "未加密连接警告",
        message: `API Key 将通过未加密 HTTP 发送到 ${url}。仅在你信任目标服务器和当前网络时继续。`,
        confirmText: "仍然保存",
        cancelText: "取消",
        variant: "warning",
      }),
    });
    if (!shouldSave) {
      return false;
    }

    // 构建系统提示词
    const expressionLabels = this.getExpressionLabels();
    const system_prompt = buildSystemPrompt(llmData, expressionLabels);

    const newState: ChatSettingsState = {
      ...this.savedState,
      openai_base_url: llmData.openai_base_url,
      openai_api_key: llmData.openai_api_key,
      model_name: llmData.model_name,
      temperature: llmData.temperature,
      system_prompt,
      name: llmData.name || undefined,
      feature: llmData.feature || undefined,
      character: llmData.character || undefined,
      address: llmData.address || undefined,
      characteristic: llmData.characteristic || undefined,
      constraint: llmData.constraint || undefined,
    };

    this.savedState = await this.persistChatSettings(newState);
    return true;
  }

  /**
   * 保存动作设置
   *
   * Args:
   *     motionData (NonNullable<EditingState["motion"]>): 动作设置页正在编辑的模型动作与表情规则。
   */
  private async saveMotionSettings(motionData: NonNullable<EditingState["motion"]>): Promise<void> {
    if (!this.modelConfig) {
      throw new Error("模型配置尚未加载");
    }

    const activeModelId = this.modelConfig.activeModelId;
    const motionConfigs = motionData.motionConfigs;

    // 保存动作配置
    await window.desktopPetApi.updateModelMotionConfig?.({
      modelId: activeModelId,
      motionConfig: motionConfigs,
    });

    // 重新获取模型配置
    this.modelConfig = await window.desktopPetApi.getModelConfig();

    // 更新系统提示词
    if (this.savedState) {
      const expressionLabels = this.getExpressionLabels();
      const system_prompt = this.buildSystemPromptFromSaved(expressionLabels);

      const newState: ChatSettingsState = {
        ...this.savedState,
        system_prompt,
      };

      this.savedState = await this.persistChatSettings(newState);
    }
  }

  /**
   * 保存工具设置
   *
   * Args:
   *     toolsData (NonNullable<EditingState["tools"]>): 工具设置页正在编辑的权限策略。
   */
  private async saveToolsSettings(toolsData: NonNullable<EditingState["tools"]>): Promise<void> {
    if (!this.savedState) {
      return;
    }

    const views = await window.desktopPetApi.updateToolPermissions(
      this.savedState.session_id,
      toolsData.tool_permissions,
    );
    this.savedState = {
      ...this.savedState,
      tool_permissions: Object.fromEntries(
        views.map(/**
         * 后端权限保存成功后按稳定键重建本页权限映射。
         *
         * Args:
         *     item (ToolPolicyView): 当前遍历的配置或目录条目。
         *
         * Returns:
         *     [string, ToolPolicy]: 工具稳定标识与最新权限组成的键值对。
         */
        (item) => [item.stable_key, item.policy])
      ),
    };
  }

  /**
   * 保存插件设置
   *
   * Args:
   *     pluginsData (NonNullable<EditingState["plugins"]>): 插件设置页正在编辑的记忆能力开关和配置。
   */
  private async savePluginSettings(pluginsData: NonNullable<EditingState["plugins"]>): Promise<void> {
    if (!this.savedState) {
      return;
    }

    const newState: ChatSettingsState = {
      ...this.savedState,
      agent_plugins: pluginsData.agent_plugins,
    };

    this.savedState = await this.persistChatSettings(newState);
  }

  /**
   * 保存设置并显示后端字段错误，跨 preload 使用普通数据传递校验结果。
   *
   * Args:
   *     settings (ChatSettingsState): 需要保存的完整会话设置。
   *
   * Returns:
   *     Promise<ChatSettingsState>: 后端实际保存的设置。
   *
   * Raises:
   *     Error: 插件校验失败，字段错误已显示。
   */
  private async persistChatSettings(settings: ChatSettingsState): Promise<ChatSettingsState> {
    const result = await window.desktopPetApi.updateChatSettings(settings);
    if ("configuration_errors" in result) {
      (this.pluginsPage as PluginsPage).showConfigurationErrors(result.configuration_errors);
      throw new Error("插件参数校验失败，请检查标出的字段");
    }
    return result;
  }

  /**
   * 校验并保存会话压缩和截图保留配置，成功后刷新保存数据并显示结果提示。
   *
   * Args:
   *     contextData (NonNullable<EditingState["contextStrategy"]>): 用户当前编辑的压缩阈值和截图保留设置。
   *
   * Returns:
   *     Promise<void>: 保存和页面提示处理完成后结束。
   */
  private async saveContextStrategy(
    contextData: NonNullable<EditingState["contextStrategy"]>
  ): Promise<void> {
    if (!this.savedState) {
      return;
    }
    const newState: ChatSettingsState = {
      ...this.savedState,
      context_strategy: contextData.context_strategy,
    };
    this.savedState = await this.persistChatSettings(newState);
  }

  /**
   * 保存当前模型的 MCP Server 和工具权限。
   *
   * Args:
   *     mcp (NonNullable<EditingState["mcp"]>): MCP Server 启用状态和逐工具策略。
   */
  private async saveMcpSettings(mcp: NonNullable<EditingState["mcp"]>): Promise<boolean> {
    if (!this.savedState) return false;
    const newState: ChatSettingsState = { ...this.savedState, mcp };
    const saved = await this.persistChatSettings(newState);
    const adjusted = JSON.stringify(saved.mcp) !== JSON.stringify(mcp);
    this.savedState = saved;
    return adjusted;
  }

  /**
   * 从已保存状态构建系统提示词
   *
   * Args:
   *     expressionLabels (string[]): 当前模型允许使用的表情标签名称。
   */
  private buildSystemPromptFromSaved(expressionLabels: string[]): string {
    return this.savedState ? buildSystemPrompt(this.savedState, expressionLabels) : "";
  }

  /**
   * 初始化聊天设置
   *
   * Args:
   *     clearCurrent (boolean): 切换设置来源前是否清除现有页面数据。
   */
  private async initChatSettings(clearCurrent = false): Promise<boolean> {
    if (clearCurrent) {
      this.savedState = null;
    }
    try {
      this.savedState = await window.desktopPetApi.getChatSettings();
      return true;
    } catch (error) {
      this.toast.error(appendErrorMessage("读取聊天设置失败", error));
      return false;
    }
  }

  /**
   * 模型变化后刷新
   */
  private async refreshAfterModelChanged(): Promise<void> {
    await Promise.all([this.modelPage.refreshModelConfig(), this.initChatSettings(true)]);
    // 获取最新的模型配置
    this.modelConfig = await window.desktopPetApi.getModelConfig();
    // 渲染当前页面
    await this.renderPage(this.currentPage);
  }

  /**
   * 初始化保存配置后应用初始导航，使任务快捷入口同时选中对应页签。
   *
   * Returns:
   *     Promise<void>: 设置配置已加载且初始页面已选择。
   */
  async init(): Promise<void> {
    await Promise.all([
      this.modelPage.refreshModelConfig(),
      this.initChatSettings(),
    ]);

    // 获取模型配置
    this.modelConfig = await window.desktopPetApi.getModelConfig();

    // 渲染初始页面
    this.switchPage(this.currentPage);
  }
}

// 启动应用
setupApplicationShutdownUi();
void new SettingsApp().init();
