import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

import type {
  DesktopPetApi,
  ModelInfo,
  ModelTransformData,
  MotionConfig,
} from "../shared-types.js";

/**
 * 页面订阅主进程事件时登记包装监听，移除时使用同一函数引用。
 *
 * Args:
 *     channel (string): 主进程与页面约定的 IPC 事件名称。
 *     listener ((payload: T) => void): 接收后续事件的回调，取消订阅后不再调用。
 *
 * Returns:
 *     (() => void): 取消本次事件订阅的函数。
 */
const subscribe = <T>(channel: string, listener: (payload: T) => void): (() => void) => {
  /**
   * 主进程事件到达后只把业务载荷交给页面回调，不暴露 Electron 事件对象。
   *
   * Args:
   *     _event (IpcRendererEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     payload (T): 已经过入口校验的桌面动作参数。
   */
  const handler = (_event: IpcRendererEvent, payload: T): void => listener(payload);
  ipcRenderer.on(channel, handler);
  /**
   * 页面取消订阅时从 ipcRenderer 移除当时登记的同一包装监听函数。
   */
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
   *     Promise<any>: 当前会话的后台任务记录列表。
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
   *     Promise<any>: 任务输出内容及后续读取位置。
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
   *     Promise<any>: 当前会话的渠道启用状态列表。
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
   *     Promise<any>: 后端保存后的渠道订阅列表。
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
   *     Promise<any>: 后端对本次用户命令的受理回执。
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
   *     Promise<any>: 当前连接首次快照已交付后的完成结果。
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
   *     () => void: 移除本次运行事件监听的取消订阅函数。
   */
  onInteractionEvent: (listener) =>
    subscribe("desktop-pet:interaction-event", listener),
  /**
   * 由聊天页调用主进程图片选择器，取得用户选择的附件。
   *
   * Returns:
   *     Promise<any>: 主进程返回的图片选择结果。
   */
  selectImages: () => ipcRenderer.invoke("desktop-pet:select-images"),
  /**
   * 页面请求主进程切换桌面窗口鼠标穿透。
   *
   * Args:
   *     enabled (boolean): 是否启用当前界面选项。
   */
  setMousePassthrough: (enabled) => {
    ipcRenderer.send("desktop-pet:set-mouse-passthrough", Boolean(enabled));
  },
  /**
   * 页面请求主进程更新当前模型区域的鼠标交互状态。
   *
   * Args:
   *     enabled (boolean): 是否启用当前界面选项。
   */
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
  /**
   * 由页面请求主进程打开或复用日志窗口。
   *
   * Returns:
   *     void: 主进程完成打开操作后的结果。
   */
  openLogWindow: () => {
    ipcRenderer.send("desktop-pet:open-log-window");
  },
  /**
   * 由标题栏请求主进程最小化当前窗口。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  minimizeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:minimize-current-window");
  },
  /**
   * 由标题栏请求主进程关闭当前页面窗口。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  closeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:close-current-window");
  },
  /**
   * 由聊天页请求主进程打开指定历史图片的预览窗口。
   *
   * Args:
   *     imageSrc (string): 要预览的图片地址。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  openImagePreview: (imageSrc) => {
    ipcRenderer.send("desktop-pet:open-image-preview", imageSrc);
  },
  /**
   * 页面向主进程读取当前使用的 Live2D 模型。
   *
   * Returns:
   *     Promise<any>: 当前模型记录。
   */
  getActiveModel: () => ipcRenderer.invoke("desktop-pet:get-active-model"),
  /**
   * 页面读取模型目录和活动模型配置。
   *
   * Returns:
   *     Promise<any>: 主进程保存的模型配置。
   */
  getModelConfig: () => ipcRenderer.invoke("desktop-pet:get-model-config"),
  /**
   * 页面按会话读取后端模型与插件设置。
   *
   * Returns:
   *     Promise<any>: 已规范化的会话设置。
   */
  getChatSettings: () => ipcRenderer.invoke("desktop-pet:get-chat-settings"),
  /**
   * 页面按会话读取最近一条助手回复，供气泡恢复。
   *
   * Args:
   *     sessionId (string | undefined): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<any>: 最近助手消息或空结果。
   */
  getLatestAiMessage: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-latest-ai-message", sessionId),
  /**
   * 页面按会话和分页条件读取可见聊天历史。
   *
   * Args:
   *     sessionId (string): 当前操作所属的会话编号。
   *     start (number): 可见聊天历史的起始位置，从零开始。
   *     limit (number): 最多保留或读取的记录数量。
   *
   * Returns:
   *     Promise<any>: 主进程取得的历史消息列表。
   */
  getChatHistory: (sessionId, start, limit) =>
    ipcRenderer.invoke("desktop-pet:get-chat-history", sessionId, start, limit),
  /**
   * 页面读取会话最近若干条历史，供聊天列表恢复。
   *
   * Args:
   *     sessionId (string): 当前操作所属的会话编号。
   *     n (number): 最多读取的历史消息数量。
   *
   * Returns:
   *     Promise<any>: 按原对话顺序排列的最近消息。
   */
  getChatHistoryLastN: (sessionId, n) =>
    ipcRenderer.invoke("desktop-pet:get-chat-history-last-n", sessionId, n),
  /**
   * 页面把会话模型、角色和插件设置交给主进程保存。
   *
   * Args:
   *     payload (ChatSettingsData): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<any>: 后端规范化并保存后的会话设置。
   */
  updateChatSettings: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-chat-settings", payload),
  /**
   * 工具设置页读取当前会话的工具目录及权限。
   *
   * Args:
   *     sessionId (string): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<any>: 本地和 MCP 工具权限列表。
   */
  getToolPermissions: (sessionId) =>
    ipcRenderer.invoke("desktop-pet:get-tool-permissions", sessionId),
  /**
   * 工具设置页提交按稳定标识组织的权限修改。
   *
   * Args:
   *     sessionId (string): 当前操作所属的会话编号。
   *     permissions (Record<string, ToolPolicy>): 按工具稳定标识保存的权限。
   *
   * Returns:
   *     Promise<any>: 保存后的工具权限目录。
   */
  updateToolPermissions: (sessionId, permissions) =>
    ipcRenderer.invoke("desktop-pet:update-tool-permissions", sessionId, permissions),
  /**
   * 页面读取当前注册的本地工具目录。
   *
   * Returns:
   *     Promise<any>: 本地工具信息列表。
   */
  getAvailableTools: () => ipcRenderer.invoke("desktop-pet:get-available-tools"),
  /**
   * 插件设置页读取插件说明及可编辑配置定义。
   *
   * Returns:
   *     Promise<any>: 插件目录、默认值及配置结构。
   */
  getAvailablePlugins: () => ipcRenderer.invoke("desktop-pet:get-available-plugins"),
  /**
   * MCP 设置页读取服务配置及运行状态。
   *
   * Returns:
   *     Promise<any>: 服务目录及当前连接状态。
   */
  getMcpServers: () => ipcRenderer.invoke("desktop-pet:get-mcp-servers"),
  /**
   * MCP 设置页请求主进程测试当前服务配置。
   *
   * Args:
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<any>: 连接测试结果和工具目录。
   */
  testMcpServer: (config) => ipcRenderer.invoke("desktop-pet:test-mcp-server", config),
  /**
   * MCP 设置页提交新服务配置。
   *
   * Args:
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<any>: 已保存的服务记录。
   */
  createMcpServer: (config) => ipcRenderer.invoke("desktop-pet:create-mcp-server", config),
  /**
   * MCP 设置页更新指定服务配置。
   *
   * Args:
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<any>: 已保存的最新服务记录。
   */
  updateMcpServer: (serverId, config) =>
    ipcRenderer.invoke("desktop-pet:update-mcp-server", serverId, config),
  /**
   * MCP 设置页删除服务，并由后端同步清理会话引用。
   *
   * Args:
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<any>: 服务删除结果。
   */
  deleteMcpServer: (serverId) => ipcRenderer.invoke("desktop-pet:delete-mcp-server", serverId),
  /**
   * MCP 设置页请求断开并重新连接指定服务。
   *
   * Args:
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<any>: 重新连接后的状态。
   */
  reconnectMcpServer: (serverId) =>
    ipcRenderer.invoke("desktop-pet:reconnect-mcp-server", serverId),
  /**
   * MCP 设置页读取指定服务目前公开的工具。
   *
   * Args:
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<any>: 该服务当前工具目录。
   */
  getMcpServerTools: (serverId) =>
    ipcRenderer.invoke("desktop-pet:get-mcp-server-tools", serverId),
  /**
   * 模型设置页选择模型后先请求导入预览，不直接启用模型。
   *
   * Returns:
   *     Promise<any>: 待导入模型的名称、类别和入口信息。
   */
  previewLive2DImport: () => ipcRenderer.invoke("desktop-pet:preview-live2d-import"),
  /**
   * 模型设置页确认预览后请求保存模型资源。
   *
   * Args:
   *     payload ({ selectedPath: string; suggestedName?: string | undefined; }): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<any>: 导入后的模型记录。
   */
  importLive2DModel: (payload) =>
    ipcRenderer.invoke("desktop-pet:import-live2d-model", payload),
  /**
   * 模型设置页保存缩放及偏移，并通知桌面更新显示。
   *
   * Args:
   *     payload ({ modelId: string; offsetX?: number | undefined; offsetY?: number | undefined; userScale?: number | undefined; followCursor?: boolean | undefined; }): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<any>: 保存后的显示参数。
   */
  updateModelTransform: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-model-transform", payload),
  /**
   * 模型设置页切换当前活动模型。
   *
   * Args:
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<any>: 切换后的模型配置。
   */
  setActiveModel: (modelId) => ipcRenderer.invoke("desktop-pet:set-active-model", modelId),
  /**
   * 模型设置页确认删除后请求主进程删除指定模型。
   *
   * Args:
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<any>: 模型删除结果。
   */
  deleteModel: (modelId) => ipcRenderer.invoke("desktop-pet:delete-model", modelId),
  /**
   * 页面订阅主进程提供的鼠标位置，供模型目光和交互判断使用。
   *
   * Args:
   *     listener ((data: CursorSyncData) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除鼠标位置监听的函数。
   */
  onCursor: (listener) => subscribe("desktop-pet:cursor", listener),
  /**
   * 页面订阅活动模型变化，供重新加载模型。
   *
   * Args:
   *     listener ((model: ModelInfo) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除模型切换监听的函数。
   */
  onModelChanged: (listener) =>
    subscribe<ModelInfo>("desktop-pet:model-changed", listener),
  /**
   * 页面订阅模型显示参数变化，供更新缩放与位置。
   *
   * Args:
   *     listener ((data: ModelTransformData) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除参数监听的函数。
   */
  onModelTransformChanged: (listener) =>
    subscribe<ModelTransformData>("desktop-pet:model-transform-changed", listener),
  /**
   * 页面读取桌面 UI 的本地配置。
   *
   * Returns:
   *     Promise<any>: 主进程保存的前端设置。
   */
  getFrontendSettings: () => ipcRenderer.invoke("desktop-pet:get-frontend-settings"),
  /**
   * 页面提交前端选项，由主进程保存并同步相关窗口。
   *
   * Args:
   *     settings (Partial<FrontendSettings>): 当前会话或桌面界面的设置。
   *
   * Returns:
   *     Promise<any>: 保存后的前端设置。
   */
  updateFrontendSettings: (settings) =>
    ipcRenderer.invoke("desktop-pet:update-frontend-settings", settings),
  /**
   * 动作设置页读取当前模型的动作规则。
   *
   * Args:
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<any>: 模型动作配置。
   */
  getMotionConfig: (modelId) =>
    ipcRenderer.invoke("desktop-pet:get-motion-config", modelId),
  /**
   * 动作设置页提交模型动作及表情配置。
   *
   * Args:
   *     payload ({ modelId: string; motionConfig: MotionConfig[]; }): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<any>: 已保存的动作设置。
   */
  updateModelMotionConfig: (payload) =>
    ipcRenderer.invoke("desktop-pet:update-model-motion-config", payload),
  /**
   * 页面请求主进程把动作播放命令转发给桌面模型。
   *
   * Args:
   *     motionName (string): 需要播放或保存配置的动作名称。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  playMotion: (motionName) => {
    ipcRenderer.send("desktop-pet:play-motion", motionName);
  },
  /**
   * 桌面页订阅设置页发起的动作播放请求。
   *
   * Args:
   *     listener ((motionName: string) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除播放请求监听的函数。
   */
  onPlayMotionRequest: (listener) =>
    subscribe<string>("desktop-pet:play-motion-request", listener),
  /**
   * 页面订阅已保存动作规则的变化。
   *
   * Args:
   *     listener ((payload: { modelId: string; motionConfig: MotionConfig[]; }) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除动作配置监听的函数。
   */
  onMotionConfigChanged: (listener) =>
    subscribe<{ modelId: string; motionConfig: MotionConfig[] }>(
      "desktop-pet:motion-config-changed",
      listener
    ),
  /**
   * 日志窗口读取当前前后端内存日志。
   *
   * Returns:
   *     Promise<any>: 按类别组织的日志快照。
   */
  getLogSnapshot: () => ipcRenderer.invoke("desktop-pet:get-log-snapshot"),
  /**
   * 日志窗口订阅主进程已脱敏的实时日志批次。
   *
   * Args:
   *     listener ((batch: LogBatch) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除日志监听的函数。
   */
  onLogBatch: (listener) => subscribe("desktop-pet:log-batch", listener),
  /**
   * 日志窗口清空指定类别的内存展示记录。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *
   * Returns:
   *     Promise<any>: 主进程处理结果。
   */
  clearLogBuffer: (side) => ipcRenderer.invoke("desktop-pet:clear-log-buffer", side),
  /**
   * 日志窗口请求在系统文件管理器中打开日志目录。
   *
   * Returns:
   *     Promise<any>: 主进程打开目录的结果。
   */
  openLogDirectory: () => ipcRenderer.invoke("desktop-pet:open-log-directory"),
};

contextBridge.exposeInMainWorld("desktopPetApi", desktopPetApi);
