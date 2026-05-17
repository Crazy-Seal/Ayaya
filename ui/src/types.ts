/**
 * 渲染进程类型定义
 */

/**
 * 模型信息
 */
export type ModelInfo = {
  id: string;
  name: string;
  sessionId: string;
  modelUrl: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
};

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
 * 聊天设置
 */
export type ChatSettingsData = {
  session_id: string;
  model_name: string;
  openai_api_key: string;
  openai_base_url: string;
  temperature: number;
  system_prompt: string;
  tools_list: string[];
  memory_plugins?: string[];
  name?: string | null;
  feature?: string | null;
  character?: string | null;
  address?: string | null;
  characteristic?: string | null;
  constraint?: string | null;
};

/**
 * 光标同步数据
 */
export type CursorSyncData = {
  localX: number;
  localY: number;
  screenX: number;
  screenY: number;
  windowX: number;
  windowY: number;
  windowWidth: number;
  windowHeight: number;
  displayX: number;
  displayY: number;
  displayWidth: number;
  displayHeight: number;
  insideWindow: boolean;
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
 * 聊天块数据
 */
export type ChatChunkData = {
  requestId: string;
  chunk: string;
  aggregated: string;
};

/**
 * 工具项
 */
export type ToolItem = {
  name: string;
};

/**
 * 聊天历史项
 */
export type ChatHistoryItem = {
  role: string;
  content: string;
  timestamp?: string;
  images?: string[];
};

/**
 * API 响应
 */
export type ApiResponse<T> = {
  data?: T;
  msg?: string;
  code?: number;
};

/**
 * 桌宠 API 接口
 */
export interface DesktopPetApi {
  chat: (
    message: string,
    sessionId?: string,
    requestId?: string,
    images?: string[]
  ) => Promise<{ response: string; model: string }>;
  selectImages: () => Promise<Array<{ path: string; dataUrl: string }> | null>;
  getActiveModel: () => Promise<ModelInfo>;
  getModelConfig: () => Promise<ModelConfig>;
  getChatSettings: () => Promise<ChatSettingsData>;
  updateChatSettings: (settings: ChatSettingsData) => Promise<ApiResponse<never>>;
  getAvailableTools: () => Promise<{ tools: ToolItem[] }>;
  updateModelTransform: (payload: {
    modelId: string;
    offsetX?: number;
    offsetY?: number;
    userScale?: number;
    followCursor?: boolean;
  }) => Promise<ModelTransformData>;
  previewLive2DImport: () => Promise<{
    selectedPath: string;
    sourceType: "directory";
    suggestedName: string;
    entryRelativePath: string;
  } | null>;
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
  getLatestAiMessage: (sessionId?: string) => Promise<{
    sessionId: string;
    latestAiMessage: string | null;
  }>;
  getChatHistory: (sessionId: string, start: number, limit: number) => Promise<ChatHistoryItem[]>;
  getChatHistoryLastN: (sessionId: string, n: number) => Promise<ChatHistoryItem[]>;
  openSettingsWindow: () => void;
  minimizeCurrentWindow: () => void;
  closeCurrentWindow: () => void;
  openImagePreview: (imageSrc: string) => void;
  setPointerInteractive: (enabled: boolean) => void;
  onModelChanged?: (callback: (model: ModelInfo) => void) => () => void;
  onModelTransformChanged?: (callback: (data: ModelTransformData) => void) => () => void;
  onCursor?: (callback: (data: CursorSyncData) => void) => () => void;
  onChatChunk: (callback: (data: ChatChunkData) => void) => () => void;
}

declare global {
  interface Window {
    desktopPetApi: DesktopPetApi;
  }
}
