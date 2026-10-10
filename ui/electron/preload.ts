import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

import type {
  DesktopPetApi,
  ModelInfo,
  ModelTransformData,
  MotionConfig,
} from "../shared-types.js";

const subscribe = <T>(channel: string, listener: (payload: T) => void): (() => void) => {
  const handler = (_event: IpcRendererEvent, payload: T): void => listener(payload);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
};

const desktopPetApi: DesktopPetApi = {
  /**
   * 通过主进程查询当前会话任务列表，返回已经解包的记录数组。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  getBackgroundTasks: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-background-tasks", sessionId),
  /**
   * 通过主进程按字节游标查询任务输出页。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     taskId (string): 受管进程任务标识。
   *     offset (number): 完整输出文件的字节游标。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  getBackgroundTaskOutput: (sessionId, taskId, offset) =>
    ipcRenderer.invoke("desktop-pet:get-background-task-output", sessionId, taskId, offset),
  /**
   * 通过主进程读取每会话渠道订阅列表。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  getChannelSubscriptions: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-channel-subscriptions", sessionId),
  /**
   * 通过主进程保存渠道启用状态并返回更新后的列表。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     channel (string): 已注册渠道标识。
   *     enabled (boolean): 该渠道是否参与此会话的双向互通。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  setChannelSubscription: (sessionId, channel, enabled) =>
    ipcRenderer.invoke("desktop-pet:set-channel-subscription", sessionId, channel, enabled),
  /**
   * 提交统一渠道命令并返回受理结果，Agent 回复从独立事件流取得。
   *
   * Args:
   *     command (ChannelCommand): 带桌面来源的统一入站命令。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  submitInteractionCommand: (command) =>
    ipcRenderer.invoke("desktop-pet:interaction-command", command),
  /**
   * 订阅当前桌面会话，重连先取快照；返回首次快照到达的等待 Promise。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     options (DesktopSessionSubscriptionOptions | undefined): 新页面要求刷新快照，普通发送省略。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
   */
  subscribeDesktopSession: (sessionId, options) =>
    ipcRenderer.invoke("desktop-pet:subscribe-desktop-session", sessionId, options),
  /**
   * 监听主进程转发的渠道事件，返回只移除当前监听器的函数。
   *
   * Args:
   *     listener ((event: InteractionEvent) => void): 收到主进程事件时执行的 Renderer 回调。
   *
   * Returns:
   *     () => void: 当前查询、回调或视图操作的结果。
   */
  onInteractionEvent: (listener) =>
    subscribe("desktop-pet:interaction-event", listener),
  selectImages: () => ipcRenderer.invoke("desktop-pet:select-images"),
  setMousePassthrough: (enabled) => {
    ipcRenderer.send("desktop-pet:set-mouse-passthrough", Boolean(enabled));
  },
  setPointerInteractive: (enabled) => {
    ipcRenderer.send("desktop-pet:set-pointer-interactive", Boolean(enabled));
  },
  /**
   * 打开普通设置或直接进入任务页，页面参数由主进程再次校验。
   *
   * Args:
   *     page ("tasks" | undefined): 指定任务页或保留普通设置入口行为。
   */
  openSettingsWindow: (page) => {
    ipcRenderer.send("desktop-pet:open-settings-window", page);
  },
  /**
   * 监听已打开窗口的任务页导航。
   *
   * Args:
   *     listener ((page: "tasks") => void): 页面导航回调。
   *
   * Returns:
   *     () => void: 移除当前监听的清理函数。
   */
  onOpenSettingsPage: (listener) => subscribe("desktop-pet:open-settings-page", listener),
  openLogWindow: () => {
    ipcRenderer.send("desktop-pet:open-log-window");
  },
  minimizeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:minimize-current-window");
  },
  closeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:close-current-window");
  },
  openImagePreview: (imageSrc) => {
    ipcRenderer.send("desktop-pet:open-image-preview", imageSrc);
  },
  getActiveModel: () => ipcRenderer.invoke("desktop-pet:get-active-model"),
  getModelConfig: () => ipcRenderer.invoke("desktop-pet:get-model-config"),
  getChatSettings: () => ipcRenderer.invoke("desktop-pet:get-chat-settings"),
  getLatestAiMessage: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-latest-ai-message", sessionId),
  getChatHistory: (sessionId, start, limit) =>
    ipcRenderer.invoke("desktop-pet:get-chat-history", sessionId, start, limit),
  getChatHistoryLastN: (sessionId, n) =>
    ipcRenderer.invoke("desktop-pet:get-chat-history-last-n", sessionId, n),
  updateChatSettings: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-chat-settings", payload),
  getToolPermissions: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-tool-permissions", sessionId),
  updateToolPermissions: (sessionId, permissions) =>
    ipcRenderer.invoke("desktop-pet:update-tool-permissions", sessionId, permissions),
  getAvailableTools: () => ipcRenderer.invoke("desktop-pet:get-available-tools"),
  getAvailablePlugins: () => ipcRenderer.invoke("desktop-pet:get-available-plugins"),
  getMcpServers: () => ipcRenderer.invoke("desktop-pet:get-mcp-servers"),
  testMcpServer: (config) => ipcRenderer.invoke("desktop-pet:test-mcp-server", config),
  createMcpServer: (config) => ipcRenderer.invoke("desktop-pet:create-mcp-server", config),
  updateMcpServer: (serverId, config) =>
    ipcRenderer.invoke("desktop-pet:update-mcp-server", serverId, config),
  deleteMcpServer: (serverId) => ipcRenderer.invoke("desktop-pet:delete-mcp-server", serverId),
  reconnectMcpServer: (serverId) =>
    ipcRenderer.invoke("desktop-pet:reconnect-mcp-server", serverId),
  getMcpServerTools: (serverId) =>
    ipcRenderer.invoke("desktop-pet:get-mcp-server-tools", serverId),
  previewLive2DImport: () => ipcRenderer.invoke("desktop-pet:preview-live2d-import"),
  importLive2DModel: (payload) =>
    ipcRenderer.invoke("desktop-pet:import-live2d-model", payload),
  updateModelTransform: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-model-transform", payload),
  setActiveModel: (modelId) => ipcRenderer.invoke("desktop-pet:set-active-model", modelId),
  deleteModel: (modelId) => ipcRenderer.invoke("desktop-pet:delete-model", modelId),
  onCursor: (listener) => subscribe("desktop-pet:cursor", listener),
  onModelChanged: (listener) =>
    subscribe<ModelInfo>("desktop-pet:model-changed", listener),
  onModelTransformChanged: (listener) =>
    subscribe<ModelTransformData>("desktop-pet:model-transform-changed", listener),
  getFrontendSettings: () => ipcRenderer.invoke("desktop-pet:get-frontend-settings"),
  updateFrontendSettings: (settings) =>
    ipcRenderer.invoke("desktop-pet:update-frontend-settings", settings),
  getMotionConfig: (modelId) =>
    ipcRenderer.invoke("desktop-pet:get-motion-config", modelId),
  updateModelMotionConfig: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-model-motion-config", payload),
  playMotion: (motionName) => {
    ipcRenderer.send("desktop-pet:play-motion", motionName);
  },
  onPlayMotionRequest: (listener) =>
    subscribe<string>("desktop-pet:play-motion-request", listener),
  onMotionConfigChanged: (listener) =>
    subscribe<{ modelId: string; motionConfig: MotionConfig[] }>(
      "desktop-pet:motion-config-changed",
      listener
    ),
  getLogSnapshot: () => ipcRenderer.invoke("desktop-pet:get-log-snapshot"),
  onLogBatch: (listener) => subscribe("desktop-pet:log-batch", listener),
  clearLogBuffer: (side) => ipcRenderer.invoke("desktop-pet:clear-log-buffer", side),
  openLogDirectory: () => ipcRenderer.invoke("desktop-pet:open-log-directory"),
};

contextBridge.exposeInMainWorld("desktopPetApi", desktopPetApi);
