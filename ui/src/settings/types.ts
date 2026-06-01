/**
 * 设置窗口类型定义
 */

// 从共享类型导入并重新导出
import type { MotionConfig as MotionConfigType, MotionSettingType as MotionSettingTypeEnum } from '../../shared-types.js';
export type MotionConfig = MotionConfigType;
export type MotionSettingType = MotionSettingTypeEnum;

/**
 * 模型配置
 */
export type ModelConfig = {
  activeModelId: string;
  models: Array<{
    id: string;
    name: string;
    sessionId: string;
    source: "builtin" | "custom";
    deletable: boolean;
    offsetX: number;
    offsetY: number;
    userScale: number;
    followCursor: boolean;
    motionConfig?: MotionConfig[];
    entry?: string;
    modelUrl?: string;
  }>;
};

/**
 * 导入预览
 */
export type ImportPreview = {
  selectedPath: string;
  sourceType: "directory";
  suggestedName: string;
  entryRelativePath: string;
};

/**
 * 聊天设置状态
 */
export type ChatSettingsState = {
  session_id: string;
  model_name: string;
  openai_api_key: string;
  openai_base_url: string;
  temperature: number;
  system_prompt: string;
  tools_list: string[];
  memory_plugins?: string[];
  name?: string;
  feature?: string;
  character?: string;
  address?: string;
  characteristic?: string;
  constraint?: string;
};

/**
 * 工具项
 */
export type ToolItem = {
  name: string;
};

/**
 * 前端设置
 */
export type FrontendSettings = {
  hide_on_screenshot: boolean;
};

/**
 * 模型变换数据
 */
export type ModelTransformData = {
  id: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
};

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
    tools_list: string[];
  };
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
    expressionLabels?: string[];
  };
}

/**
 * 页面编辑数据（由 getEditingData 返回）
 */
export interface PageEditingData {
  llm?: EditingState["llm"];
  motion?: EditingState["motion"];
  tools?: EditingState["tools"];
}

/**
 * 页面事件类型
 */
export type PageEventType = "submit";

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
export type PageEventCallback = (event: PageEvent) => void;

/**
 * 设置页面接口（纯视图组件）
 */
export interface ISettingsPage {
  /**
   * 渲染页面
   * @param data 渲染数据（来自 editingState 或 savedState）
   */
  render(data: PageRenderData): void;

  /**
   * 获取当前编辑数据
   * 用于切换页面前保存编辑状态
   */
  getEditingData(): PageEditingData;

  /**
   * 设置事件回调
   */
  onEvent(callback: PageEventCallback): void;
}

/**
 * 桌宠 API 接口（设置窗口使用的部分）
 */
export interface DesktopPetApi {
  getModelConfig: () => Promise<ModelConfig>;
  previewLive2DImport: () => Promise<ImportPreview | null>;
  importLive2DModel: (payload: {
    selectedPath: string;
    suggestedName?: string;
  }) => Promise<{
    id: string;
    name: string;
    sessionId: string;
    source: string;
  }>;
  deleteModel: (modelId: string) => Promise<{ activeModelId: string }>;
  setActiveModel: (modelId: string) => Promise<{ activeModelId: string }>;
  updateModelTransform: (payload: {
    modelId: string;
    offsetX?: number;
    offsetY?: number;
    userScale?: number;
    followCursor?: boolean;
  }) => Promise<ModelTransformData>;
  getChatSettings: () => Promise<ChatSettingsState>;
  updateChatSettings: (settings: ChatSettingsState) => Promise<{ msg?: string; code?: number }>;
  getAvailableTools: () => Promise<{ tools: ToolItem[] }>;
  minimizeCurrentWindow: () => void;
  closeCurrentWindow: () => void;
  onModelChanged?: (callback: (model: unknown) => void) => () => void;
  onModelTransformChanged?: (callback: (data: ModelTransformData) => void) => () => void;
  getFrontendSettings: () => Promise<FrontendSettings>;
  updateFrontendSettings: (settings: Partial<FrontendSettings>) => Promise<FrontendSettings>;
  // 动作控制
  getMotionConfig: (modelId: string) => Promise<MotionConfig[]>;
  updateModelMotionConfig: (payload: { modelId: string; motionConfig: MotionConfig[] }) => Promise<void>;
  playMotion: (motionName: string) => void;
}

declare global {
  interface Window {
    desktopPetApi: DesktopPetApi;
  }
}
