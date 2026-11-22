import { IpcChannel } from "../ipc-channels.js";
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

import type {
  DesktopPetApi,
  AiNameChangedPayload,
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
    ipcRenderer.invoke(IpcChannel.GET_BACKGROUND_TASKS, sessionId),
  /** 按父会话读取当前子 Agent 摘要，不触发执行。 */
  getSubagents: (sessionId) => ipcRenderer.invoke(IpcChannel.GET_SUBAGENTS, sessionId),
  /** 按父子归属读取定义、交互及最终报告。 */
  getSubagentDetails: (sessionId, subagentId) => ipcRenderer.invoke(IpcChannel.GET_SUBAGENT_DETAILS, sessionId, subagentId),
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
    ipcRenderer.invoke(IpcChannel.GET_BACKGROUND_TASK_OUTPUT, sessionId, taskId, offset),
  /**
   * 提交统一交互命令并返回受理结果，Agent 回复从独立事件流取得。
   *
   * Args:
   *     command (InteractionCommand): 包含会话和业务内容的统一入站命令。
   *
   * Returns:
   *     Promise<any>: 后端对本次用户命令的受理回执。
   */
  submitInteractionCommand: (command) =>
    ipcRenderer.invoke(IpcChannel.INTERACTION_COMMAND, command),
  /**
   * 初始化当前会话视图，使用主进程持有的全局事件流。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<void>: 当前会话快照及缓冲事件已经转发。
   */
  initializeDesktopSession: (sessionId) =>
    ipcRenderer.invoke(IpcChannel.INITIALIZE_DESKTOP_SESSION, sessionId),
  /** 等待全局流就绪后允许提交用户命令。 */
  ensureDesktopStream: () => ipcRenderer.invoke(IpcChannel.ENSURE_DESKTOP_STREAM),
  /** 接收自动重连时的快照查询错误。 */
  onInteractionSnapshotFailed: (listener) => subscribe(IpcChannel.INTERACTION_SNAPSHOT_FAILED, listener),
  /**
   * 监听主进程转发的交互事件，返回只移除当前监听器的函数。
   *
   * Args:
   *     listener ((event: InteractionEvent) => void): 收到主进程事件时执行的 Renderer 回调。
   *
   * Returns:
   *     () => void: 移除本次运行事件监听的取消订阅函数。
   */
  onInteractionEvent: (listener) =>
    subscribe(IpcChannel.INTERACTION_EVENT, listener),
  /**
   * 监听当前订阅断线，将会话编号交给页面清空持续提示。
   *
   * Args:
   *     listener ((sessionId: string) => void): 页面断线清理入口。
   *
   * Returns:
   *     () => void: 移除本次监听的函数。
   */
  onInteractionDisconnected: (listener) =>
    subscribe(IpcChannel.INTERACTION_DISCONNECTED, listener),
  /**
   * 由聊天页调用主进程图片选择器，取得用户选择的附件。
   *
   * Returns:
   *     Promise<any>: 主进程返回的图片选择结果。
   */
  selectImages: () => ipcRenderer.invoke(IpcChannel.SELECT_IMAGES),
  /**
   * 页面请求主进程切换桌面窗口鼠标穿透。
   *
   * Args:
   *     enabled (boolean): 是否启用当前界面选项。
   */
  setMousePassthrough: (enabled) => {
    ipcRenderer.send(IpcChannel.SET_MOUSE_PASSTHROUGH, Boolean(enabled));
  },
  /**
   * 页面请求主进程更新当前模型区域的鼠标交互状态。
   *
   * Args:
   *     enabled (boolean): 是否启用当前界面选项。
   */
  setPointerInteractive: (enabled) => {
    ipcRenderer.send(IpcChannel.SET_POINTER_INTERACTIVE, Boolean(enabled));
  },
  /**
   * 打开普通设置或直接进入任务页，页面参数由主进程再次校验。
   *
   * Args:
   *     page ("tasks" | undefined): 指定任务页或保留普通设置入口行为。
   */
  openSettingsWindow: (page) => {
    ipcRenderer.send(IpcChannel.OPEN_SETTINGS_WINDOW, page);
  },
  /**
   * 监听已打开窗口的任务页导航。
   *
   * Args:
   *     listener ((page: "tasks" | "subagents") => void): 页面导航回调。
   *
   * Returns:
   *     () => void: 移除当前监听的清理函数。
   */
  onOpenSettingsPage: (listener) => subscribe(IpcChannel.OPEN_SETTINGS_PAGE, listener),
  /**
   * 由页面请求主进程打开或复用日志窗口。
   *
   * Returns:
   *     void: 主进程完成打开操作后的结果。
   */
  openLogWindow: () => {
    ipcRenderer.send(IpcChannel.OPEN_LOG_WINDOW);
  },
  /**
   * 由标题栏请求主进程最小化当前窗口。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  minimizeCurrentWindow: () => {
    ipcRenderer.send(IpcChannel.MINIMIZE_CURRENT_WINDOW);
  },
  /**
   * 由标题栏请求主进程关闭当前页面窗口。
   *
   * Returns:
   *     void: 主进程处理结果。
   */
  closeCurrentWindow: () => {
    ipcRenderer.send(IpcChannel.CLOSE_CURRENT_WINDOW);
  },
  /**
   * 保存并应用聊天置顶。
   * Args:
   *     pinned (boolean): 用户当前选择。
   * Returns:
   *     Promise<boolean>: 保存后的状态。
   */
  setChatPinned: (pinned) => ipcRenderer.invoke(IpcChannel.SET_CHAT_PINNED, pinned),
  /**
   * 只发送拖动方向，窗口位置与尺寸由主进程读取系统鼠标计算。
   * Args:
   *     kind (WindowGesture): 当前窗口标题栏移动或边缘缩放方向。
   */
  beginWindowGesture: (kind) => ipcRenderer.send(IpcChannel.BEGIN_WINDOW_GESTURE, kind),
  /** 松开鼠标、失去捕获或页面卸载时结束当前拖动。 */
  endWindowGesture: () => ipcRenderer.send(IpcChannel.END_WINDOW_GESTURE),
  /**
   * 转交已去重的聊天提醒。
   * Args:
   *     update (ChatAttentionUpdate): 当前消息与审批变化。
   */
  reportChatAttention: (update) => ipcRenderer.send(IpcChannel.CHAT_ATTENTION, update),
  /**
   * 请求主进程开始全应用退出，等待受理结果，避免只锁定当前页却未发起退出。
   *
   * Returns:
   *     Promise<void>: 主进程已关闭新业务并通知全部窗口进入退出状态。
   */
  requestApplicationExit: () => ipcRenderer.invoke(IpcChannel.EXIT_APPLICATION),
  /** 将用户点击强制退出交给主进程。 */
  forceApplicationExit: () => ipcRenderer.invoke(IpcChannel.FORCE_EXIT_APPLICATION),
  /** 查询全部活动任务和启动声明任务数量，页面据此选择退出确认文案。 */
  getActiveTaskSummary: () => ipcRenderer.invoke(IpcChannel.GET_ACTIVE_TASK_SUMMARY),
  /** 查询主进程退出状态，此查询在退出等待期间仍允许执行。 */
  getApplicationShutdownState: () => ipcRenderer.invoke(IpcChannel.GET_SHUTDOWN_STATE),
  /**
   * 主进程关闭业务门禁后通知页面停止交互，不暴露 Electron 事件对象。
   * Args:
   *     callback (() => void): 页面进入退出等待界面的回调。
   * Returns:
   *     (() => void): 页面卸载时取消监听的函数。
   */
  onApplicationShutdown: (callback) => subscribe(IpcChannel.APPLICATION_SHUTDOWN, callback),
  /**
   * 把聊天表情标签交给主进程核对并转发。
   * Args:
   *     label (string): 识别到的表情标签。
   */
  playExpression: (label) => ipcRenderer.send(IpcChannel.PLAY_EXPRESSION, label),
  /**
   * 将 Live2D 错误交给聊天页。
   * Args:
   *     message (string): 模型错误说明。
   */
  reportModelError: (message) => ipcRenderer.send(IpcChannel.MODEL_ERROR, message),
  /**
   * 订阅模型错误。
   * Args:
   *     listener ((message: string) => void): 错误展示回调。
   * Returns:
   *     () => void: 取消订阅函数。
   */
  onModelError: (listener) => subscribe(IpcChannel.MODEL_ERROR, listener),
  /**
   * 订阅聊天发来的表情请求。
   * Args:
   *     listener ((label: string) => void): Live2D 播放回调。
   * Returns:
   *     () => void: 取消订阅函数。
   */
  onExpressionRequest: (listener) => subscribe(IpcChannel.EXPRESSION_REQUEST, listener),
  /**
   * 聊天页请求主进程打开图片预览，预览窗口复用应用共享样式。
   * Args:
   *     imageSrc (string): 历史或附件图片地址。
   */
  openImagePreview: (imageSrc) => {
    ipcRenderer.send(IpcChannel.OPEN_IMAGE_PREVIEW, imageSrc);
  },
  /**
   * 页面向主进程读取当前使用的 Live2D 模型。
   *
   * Returns:
   *     Promise<any>: 当前模型记录。
   */
  getActiveModel: () => ipcRenderer.invoke(IpcChannel.GET_ACTIVE_MODEL),
  /**
   * 页面读取模型目录和活动模型配置。
   *
   * Returns:
   *     Promise<any>: 主进程保存的模型配置。
   */
  getModelConfig: () => ipcRenderer.invoke(IpcChannel.GET_MODEL_CONFIG),
  /**
   * 查询本地模型各会话的 AI 名字，不向页面传递其他会话的凭据。
   *
   * Returns:
   *     Promise<Record<string, string>>: 会话编号与显示名字的对应关系。
   */
  getAiNames: (sessionIds) => ipcRenderer.invoke(IpcChannel.GET_AI_NAMES, sessionIds),
  /**
   * 页面按会话读取后端模型与插件设置。
   *
   * Returns:
   *     Promise<any>: 已规范化的会话设置。
   */
  getChatSettings: () => ipcRenderer.invoke(IpcChannel.GET_CHAT_SETTINGS),
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
    ipcRenderer.invoke(IpcChannel.GET_LATEST_AI_MESSAGE, sessionId),
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
    ipcRenderer.invoke(IpcChannel.GET_CHAT_HISTORY, sessionId, start, limit),
  /**
   * 页面读取最近聊天历史，工具完成刷新时传入结果消息编号作为查询终点。
   *
   * Args:
   *     sessionId (string): 当前操作所属的会话编号。
   *     n (number): 最多读取的历史消息数量。
   *     throughMessageId (string | undefined): 工具完成后的查询终点，省略时读取最新历史。
   *
   * Returns:
   *     Promise<any>: 按原对话顺序排列的最近消息。
   */
  getChatHistoryLastN: (sessionId, n, throughMessageId) =>
    ipcRenderer.invoke(IpcChannel.GET_CHAT_HISTORY_LAST_N, sessionId, n, throughMessageId),
  /**
   * 页面把会话模型、角色和插件设置交给主进程保存。
   *
   * Args:
   *     payload (ChatSettingsData): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<any>: 后端规范化并保存后的会话设置。
   */
  updateChatSettings: (payload) => ipcRenderer.invoke(IpcChannel.UPDATE_CHAT_SETTINGS, payload),
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
    ipcRenderer.invoke(IpcChannel.GET_TOOL_PERMISSIONS, sessionId),
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
    ipcRenderer.invoke(IpcChannel.UPDATE_TOOL_PERMISSIONS, sessionId, permissions),
  /**
   * 页面读取当前注册的本地工具目录。
   *
   * Returns:
   *     Promise<any>: 本地工具信息列表。
   */
  getAvailableTools: () => ipcRenderer.invoke(IpcChannel.GET_AVAILABLE_TOOLS),
  /**
   * 插件设置页读取插件说明及可编辑配置定义。
   *
   * Returns:
   *     Promise<any>: 插件目录、默认值及配置结构。
   */
  getAvailablePlugins: () => ipcRenderer.invoke(IpcChannel.GET_AVAILABLE_PLUGINS),
  /**
   * 插件设置页读取指定会话中各插件 MCP 服务的连接状态。
   *
   * Args:
   *     sessionId (string): 当前设置页正在查看的会话编号。
   *
   * Returns:
   *     Promise<Record<string, PluginMCPStatus[]>>: 按插件名称分组的服务状态、工具数量和错误说明。
   */
  getPluginMcpStatuses: (sessionId) => ipcRenderer.invoke(IpcChannel.GET_PLUGIN_MCP_STATUSES, sessionId),
  /**
   * MCP 设置页读取服务配置及运行状态。
   *
   * Returns:
   *     Promise<any>: 服务目录及当前连接状态。
   */
  getMcpServers: () => ipcRenderer.invoke(IpcChannel.GET_MCP_SERVERS),
  /**
   * MCP 设置页请求主进程测试当前服务配置。
   *
   * Args:
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<any>: 连接测试结果和工具目录。
   */
  testMcpServer: (config) => ipcRenderer.invoke(IpcChannel.TEST_MCP_SERVER, config),
  /**
   * MCP 设置页提交新服务配置。
   *
   * Args:
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<any>: 已保存的服务记录。
   */
  createMcpServer: (config) => ipcRenderer.invoke(IpcChannel.CREATE_MCP_SERVER, config),
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
    ipcRenderer.invoke(IpcChannel.UPDATE_MCP_SERVER, serverId, config),
  /**
   * MCP 设置页删除服务，并由后端同步清理会话引用。
   *
   * Args:
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<any>: 服务删除结果。
   */
  deleteMcpServer: (serverId) => ipcRenderer.invoke(IpcChannel.DELETE_MCP_SERVER, serverId),
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
    ipcRenderer.invoke(IpcChannel.RECONNECT_MCP_SERVER, serverId),
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
    ipcRenderer.invoke(IpcChannel.GET_MCP_SERVER_TOOLS, serverId),
  /**
   * 模型设置页选择模型后先请求导入预览，不直接启用模型。
   *
   * Returns:
   *     Promise<any>: 待导入模型的名称、类别和入口信息。
   */
  previewLive2DImport: () => ipcRenderer.invoke(IpcChannel.PREVIEW_LIVE2D_IMPORT),
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
    ipcRenderer.invoke(IpcChannel.IMPORT_LIVE2D_MODEL, payload),
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
    ipcRenderer.invoke(IpcChannel.UPDATE_MODEL_TRANSFORM, payload),
  /**
   * 模型设置页切换当前活动模型。
   *
   * Args:
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<any>: 切换后的模型配置。
   */
  setActiveModel: (modelId) => ipcRenderer.invoke(IpcChannel.SET_ACTIVE_MODEL, modelId),
  /**
   * 模型设置页确认删除后请求主进程删除指定模型。
   *
   * Args:
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<any>: 模型删除结果。
   */
  deleteModel: (modelId) => ipcRenderer.invoke(IpcChannel.DELETE_MODEL, modelId),
  /**
   * 页面订阅主进程提供的鼠标位置，供模型目光和交互判断使用。
   *
   * Args:
   *     listener ((data: CursorSyncData) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除鼠标位置监听的函数。
   */
  onCursor: (listener) => subscribe(IpcChannel.CURSOR, listener),
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
    subscribe<ModelInfo>(IpcChannel.MODEL_CHANGED, listener),
  /**
   * 配置已保存后订阅 AI 名字变化，供聊天和模型列表更新显示。
   *
   * Args:
   *     listener ((payload: AiNameChangedPayload) => void): 接收已保存会话名字的回调。
   *
   * Returns:
   *     () => void: 取消当前名字监听的函数。
   */
  onAiNameChanged: (listener) =>
    subscribe<AiNameChangedPayload>(IpcChannel.AI_NAME_CHANGED, listener),
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
    subscribe<ModelTransformData>(IpcChannel.MODEL_TRANSFORM_CHANGED, listener),
  /**
   * 页面读取桌面 UI 的本地配置。
   *
   * Returns:
   *     Promise<any>: 主进程保存的前端设置。
   */
  getFrontendSettings: () => ipcRenderer.invoke(IpcChannel.GET_FRONTEND_SETTINGS),
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
    ipcRenderer.invoke(IpcChannel.GET_MOTION_CONFIG, modelId),
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
    ipcRenderer.invoke(IpcChannel.UPDATE_MODEL_MOTION_CONFIG, payload),
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
    ipcRenderer.send(IpcChannel.PLAY_MOTION, motionName);
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
    subscribe<string>(IpcChannel.PLAY_MOTION_REQUEST, listener),
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
      IpcChannel.MOTION_CONFIG_CHANGED,
      listener
    ),
  /**
   * 日志窗口读取当前前后端内存日志。
   *
   * Returns:
   *     Promise<any>: 按类别组织的日志快照。
   */
  getLogSnapshot: () => ipcRenderer.invoke(IpcChannel.GET_LOG_SNAPSHOT),
  /**
   * 日志窗口订阅主进程已脱敏的实时日志批次。
   *
   * Args:
   *     listener ((batch: LogBatch) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 移除日志监听的函数。
   */
  onLogBatch: (listener) => subscribe(IpcChannel.LOG_BATCH, listener),
  /**
   * 日志窗口清空指定类别的内存展示记录。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *
   * Returns:
   *     Promise<any>: 主进程处理结果。
   */
  clearLogBuffer: (side) => ipcRenderer.invoke(IpcChannel.CLEAR_LOG_BUFFER, side),
  /**
   * 日志窗口请求在系统文件管理器中打开日志目录。
   *
   * Returns:
   *     Promise<any>: 主进程打开目录的结果。
   */
  openLogDirectory: () => ipcRenderer.invoke(IpcChannel.OPEN_LOG_DIRECTORY),
};

contextBridge.exposeInMainWorld("desktopPetApi", desktopPetApi);
