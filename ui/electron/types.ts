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
