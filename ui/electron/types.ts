/**
 * Electron 主进程类型定义
 */

export type ModelSource = "builtin" | "custom";

export type ModelRecord = {
  id: string;
  name: string;
  sessionId: string;
  source: ModelSource;
  entry: string;
  rootDir?: string;
  offsetX?: number;
  offsetY?: number;
  userScale?: number;
  followCursor?: boolean;
};

export type ModelConfig = {
  activeModelId: string;
  models: ModelRecord[];
};

export type ImportPreview = {
  selectedPath: string;
  sourceType: "directory";
  suggestedName: string;
  entryRelativePath: string;
};

export type ToolItem = {
  name: string;
};

export type ChatSettingsData = {
  session_id: string;
  model_name: string;
  openai_api_key: string;
  openai_base_url: string;
  temperature: number;
  system_prompt: string;
  tools_list: string[];
  name?: string | null;
  feature?: string | null;
  character?: string | null;
  address?: string | null;
  characteristic?: string | null;
  constraint?: string | null;
};

export type ChatHistoryItem = {
  role: string;
  content: string;
  timestamp: string;
  images?: string[];
};

export type ApiResponse<T> = {
  data?: T;
  msg?: string;
  code?: number;
};

export type ModelTransformPayload = {
  modelId: string;
  offsetX?: number;
  offsetY?: number;
  userScale?: number;
  followCursor?: boolean;
};

export type CursorSyncPayload = {
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

export type ModelChangedPayload = {
  id: string;
  name: string;
  sessionId: string;
  modelUrl: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
};

export type ModelTransformChangedPayload = {
  id: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
};

export type ChatChunkPayload = {
  requestId: string;
  chunk: string;
  aggregated: string;
};

/**
 * 截屏中断数据（内层数据）
 */
export type ScreenshotInterruptData = {
  type: "screenshot_request";
  request_id: string;
  message: string;
};

/**
 * SSE interrupt 事件载荷（外层有 value 包装）
 */
export type ScreenshotInterruptPayload = {
  value: ScreenshotInterruptData;
};

/**
 * Chat 返回结果类型
 */
export type ChatResult =
  | { response: string; model: string; interrupted?: false }
  | { interrupted: true; interruptData: ScreenshotInterruptPayload };

/**
 * 工具调用事件载荷
 */
export type ToolCallPayload = {
  tool_name: string;
  error_message?: string;
};

/**
 * 工具调用 IPC 事件载荷
 */
export type ToolCallEventPayload = {
  requestId: string;
  toolName: string;
};

/**
 * 前端设置
 */
export type FrontendSettings = {
  hide_on_screenshot: boolean;
};
