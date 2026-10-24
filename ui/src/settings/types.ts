/**
 * 设置窗口类型定义
 */

// 从共享类型导入并重新导出
import type {
  ChatSettingsData,
  FrontendSettings as SharedFrontendSettings,
  ImportPreview as SharedImportPreview,
  ModelConfigView,
  ModelTransformData as SharedModelTransformData,
  MotionConfig as SharedMotionConfig,
  MotionSettingType as SharedMotionSettingType,
  PluginItem as SharedPluginItem,
  ToolItem as SharedToolItem,
  MCPModelSettings,
  MCPServerView,
  MCPToolInfo,
  ToolPolicyView,
  ToolPolicy,
} from "../../shared-types.js";

export type MotionConfig = SharedMotionConfig;
export type MotionSettingType = SharedMotionSettingType;

/**
 * 模型配置
 */
export type ModelConfig = ModelConfigView;

/**
 * 导入预览
 */
export type ImportPreview = SharedImportPreview;

/**
 * 聊天设置状态
 */
export type ChatSettingsState = ChatSettingsData;

/**
 * 工具项
 */
export type ToolItem = SharedToolItem;
export type PluginItem = SharedPluginItem;
export type { ToolPolicyView, ToolPolicy };

/**
 * 前端设置
 */
export type FrontendSettings = SharedFrontendSettings;

/**
 * 模型变换数据
 */
export type ModelTransformData = SharedModelTransformData;

/**
 * 编辑状态（每个页面独立的编辑数据）
 */
export interface EditingState {
  llm?: {
    openai_base_url: string;
    openai_api_key: string;
    model_name: string;
    temperature: number;
    name: string;
    feature: string;
    character: string;
    address: string;
    characteristic: string;
    constraint: string;
  };
  motion?: {
    motionConfigs: MotionConfig[];
  };
  tools?: {
    tool_permissions: Record<string, ToolPolicy>;
  };
  contextStrategy?: {
    context_strategy: ChatSettingsState["context_strategy"];
  };
  plugins?: {
    agent_plugins: NonNullable<ChatSettingsState["agent_plugins"]>;
  };
  mcp?: MCPModelSettings;
}

/**
 * 页面渲染数据
 */
export interface PageRenderData {
  /** 来自已保存状态 */
  saved: ChatSettingsState;
  /** 来自编辑状态（优先使用） */
  editing?: EditingState;
  /** 其他依赖数据 */
  dependencies?: {
    modelConfig?: ModelConfig;
    availableMotions?: string[];
    availableTools?: ToolItem[];
    toolPolicies?: ToolPolicyView[];
    availablePlugins?: PluginItem[];
    expressionLabels?: string[];
    mcpServers?: MCPServerView[];
    mcpTools?: Map<string, MCPToolInfo[]>;
  };
}

/**
 * 页面编辑数据（由 getEditingData 返回）
 */
export interface PageEditingData {
  llm?: EditingState["llm"];
  motion?: EditingState["motion"];
  tools?: EditingState["tools"];
  contextStrategy?: EditingState["contextStrategy"];
  plugins?: EditingState["plugins"];
  mcp?: EditingState["mcp"];
}

/** 表示设置页请求保存或刷新已保存设置。 */
export const PageEventType = {
  SUBMIT: "submit",
  SETTINGS_INVALIDATED: "settings-invalidated",
} as const;
export type PageEventType = typeof PageEventType[keyof typeof PageEventType];

/**
 * 页面事件回调参数
 */
export interface PageEvent {
  type: PageEventType;
  page: string;
}

/**
 * 页面事件回调类型
 */
export type PageEventCallback = (event: PageEvent) => void | Promise<void>;

/**
 * 设置页面接口（纯视图组件）
 */
export interface ISettingsPage {
  /**
   * 渲染页面
   *
   * Args:
   *     data (PageRenderData): 当前已保存设置和本页所需模型、插件及工具目录。
   */
  render(data: PageRenderData): void;

  /**
   * 获取当前编辑数据
   * 用于切换页面前保存编辑状态
   */
  getEditingData(): PageEditingData;

  /**
   * 设置事件回调
   *
   * Args:
   *     callback (PageEventCallback): 设置页主控制器接收本页提交或编辑事件的回调。
   */
  onEvent(callback: PageEventCallback): void;
}
