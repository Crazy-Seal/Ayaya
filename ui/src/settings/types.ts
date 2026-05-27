/**
 * 设置窗口类型定义
 */

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
}

declare global {
  interface Window {
    desktopPetApi: DesktopPetApi;
  }
}
