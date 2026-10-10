/**
 * 共享类型定义
 * 供主进程、主窗口、设置窗口共同使用
 */

/** 运行时协议的具名取值，Renderer 与 Electron 共享；类型由同一对象推导。 */
export const DESKTOP_CHANNEL = "desktop";

/**
 * 表示会话展示与调度阶段，与轮次内部执行阶段分别建模。
 *
 * Attributes:
 *     IDLE (string): 空闲，协议值为 idle。
 *     RUNNING (string): 轮次运行中，协议值为 running。
 *     PENDING_INTERRUPT (string): 等待人工审批，协议值为 pending_interrupt。
 *     PENDING_EXECUTION (string): 等待桌面执行结果，协议值为 pending_execution。
 *     ERROR (string): 本轮错误，协议值为 error。
 */
export const SessionPhase = {
  IDLE: "idle",
  RUNNING: "running",
  PENDING_INTERRUPT: "pending_interrupt",
  PENDING_EXECUTION: "pending_execution",
  ERROR: "error",
} as const;
export type SessionPhase = typeof SessionPhase[keyof typeof SessionPhase];

/**
 * 表示 Adapter 支持的已知用户命令，入口仍保留未知命令的业务拒绝。
 *
 * Attributes:
 *     HUMAN_TURN (string): 普通真人输入，协议值为 human_turn。
 *     HUMAN_STEER (string): 引导当前轮次，协议值为 human_steer。
 *     QUEUE_TURN (string): 排队到下一轮，协议值为 queue_turn。
 *     STOP_TURN (string): 停止当前轮次，协议值为 stop_turn。
 *     RESOLVE_AUTHORIZATION (string): 处理人工审批，协议值为 resolve_authorization。
 *     STOP_BACKGROUND_TASK (string): 停止独立后台任务，协议值为 stop_background_task。
 *     CANCEL_QUEUED (string): 撤销排队输入，协议值为 cancel_queued。
 */
export const CommandKind = {
  HUMAN_TURN: "human_turn",
  HUMAN_STEER: "human_steer",
  QUEUE_TURN: "queue_turn",
  STOP_TURN: "stop_turn",
  RESOLVE_AUTHORIZATION: "resolve_authorization",
  STOP_BACKGROUND_TASK: "stop_background_task",
  CANCEL_QUEUED: "cancel_queued",
} as const;
export type CommandKind = typeof CommandKind[keyof typeof CommandKind];

/**
 * 表示渠道可见事件，与 Agent 内部 EventType 通过显式映射衔接。
 *
 * Attributes:
 *     SESSION_SNAPSHOT (string): 当前会话快照，协议值为 session_snapshot。
 *     HUMAN_MESSAGE (string): 已提交真人消息，协议值为 human_message。
 *     QUEUED_MESSAGE (string): 输入已排队，协议值为 queued_message。
 *     QUEUED_CANCELLED (string): 排队输入已撤销，协议值为 queued_cancelled。
 *     TASK_STATUS (string): 后台任务状态，协议值为 task_status。
 *     RUN_STARTED (string): 轮次开始或恢复，协议值为 run_started。
 *     TEXT_CHUNK (string): 模型正文分片，协议值为 text_chunk。
 *     TOOL_CALL (string): 工具调用提示，协议值为 tool_call。
 *     INTERRUPT (string): 待处理审批或执行请求，协议值为 interrupt。
 *     AUTHORIZATION_RESOLVED (string): 审批首个有效决定，协议值为 authorization_resolved。
 *     RUN_COMPLETED (string): 轮次结束，协议值为 run_completed。
 *     ERROR (string): 运行错误，协议值为 error。
 */
export const InteractionEventKind = {
  SESSION_SNAPSHOT: "session_snapshot",
  HUMAN_MESSAGE: "human_message",
  QUEUED_MESSAGE: "queued_message",
  QUEUED_CANCELLED: "queued_cancelled",
  TASK_STATUS: "task_status",
  RUN_STARTED: "run_started",
  TEXT_CHUNK: "text_chunk",
  TOOL_CALL: "tool_call",
  INTERRUPT: "interrupt",
  AUTHORIZATION_RESOLVED: "authorization_resolved",
  RUN_COMPLETED: "run_completed",
  ERROR: "error",
} as const;
export type InteractionEventKind = typeof InteractionEventKind[keyof typeof InteractionEventKind];

/**
 * 区分人工授权请求与独立桌面执行 continuation。
 *
 * Attributes:
 *     AUTHORIZATION_REQUEST (string): 人工审批，协议值为 authorization_request。
 *     CONTINUATION_REQUEST (string): 桌面执行续跑，协议值为 continuation_request。
 */
export const InterruptKind = {
  AUTHORIZATION_REQUEST: "authorization_request",
  CONTINUATION_REQUEST: "continuation_request",
} as const;
export type InterruptKind = typeof InterruptKind[keyof typeof InterruptKind];

/**
 * 表示 Electron 主进程支持的屏幕请求类别。
 *
 * Attributes:
 *     CAPTURE_SCREEN (string): 截取屏幕，协议值为 capture_screen。
 *     PERFORM_SCREEN_ACTION (string): 执行屏幕动作并截屏，协议值为 perform_screen_action。
 */
export const DesktopExecutionKind = {
  CAPTURE_SCREEN: "capture_screen",
  PERFORM_SCREEN_ACTION: "perform_screen_action",
} as const;
export type DesktopExecutionKind = typeof DesktopExecutionKind[keyof typeof DesktopExecutionKind];

/**
 * 表示用户能够提交的审批决定，不包含系统取消通知。
 *
 * Attributes:
 *     APPROVE (string): 批准，协议值为 approve。
 *     REJECT (string): 拒绝，协议值为 reject。
 *     STEER (string): 审批框引导，协议值为 steer。
 */
export const ApprovalDecisionKind = {
  APPROVE: "approve",
  REJECT: "reject",
  STEER: "steer",
} as const;
export type ApprovalDecisionKind = typeof ApprovalDecisionKind[keyof typeof ApprovalDecisionKind];

/**
 * 表示审批处理结果，取消来自后端而非可提交的用户决定。
 *
 * Attributes:
 *     APPROVE (string): 已批准，协议值为 approve。
 *     REJECT (string): 已拒绝，协议值为 reject。
 *     STEER (string): 被审批框引导取代，协议值为 steer。
 *     CANCELLED (string): 被其他输入取消，协议值为 cancelled。
 */
export const AuthorizationResolutionKind = {
  APPROVE: "approve",
  REJECT: "reject",
  STEER: "steer",
  CANCELLED: "cancelled",
} as const;
export type AuthorizationResolutionKind = typeof AuthorizationResolutionKind[keyof typeof AuthorizationResolutionKind];

/**
 * 表示受管进程状态，不与模型输入的汇总终态混用。
 *
 * Attributes:
 *     RUNNING (string): 进程运行中，协议值为 running。
 *     COMPLETED (string): 成功结束，协议值为 completed。
 *     FAILED (string): 执行失败，协议值为 failed。
 *     STOPPED (string): 用户停止，协议值为 stopped。
 *     ORPHANED (string): 重启后失去进程所有权，协议值为 orphaned。
 */
export const TaskStatus = {
  RUNNING: "running",
  COMPLETED: "completed",
  FAILED: "failed",
  STOPPED: "stopped",
  ORPHANED: "orphaned",
} as const;
export type TaskStatus = typeof TaskStatus[keyof typeof TaskStatus];

/**
 * 动作设置类型
 */
export type MotionSettingType = 'idle' | 'expression' | 'none';

/**
 * 动作配置
 */
export type MotionConfig = {
  motionName: string;
  setting: MotionSettingType;
  label?: string;
};

export type ModelInfo = {
  id: string;
  name: string;
  sessionId: string;
  modelUrl: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
  motionConfig?: MotionConfig[];
  entry?: string;
};

export type ModelConfigView = {
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

export type ChatSettingsData = {
  session_id: string;
  model_name: string;
  openai_api_key: string;
  openai_base_url: string;
  temperature: number;
  system_prompt: string;
  tool_permissions: Record<string, ToolPolicy>;
  tool_permissions_schema_version: number;
  mcp: MCPModelSettings;
  context_strategy: ContextStrategyConfig;
  agent_plugins?: Record<string, AgentPluginSettings> | null;
  name?: string | null;
  feature?: string | null;
  character?: string | null;
  address?: string | null;
  characteristic?: string | null;
  constraint?: string | null;
};

export type ToolPolicy = "allow" | "ask" | "deny";

export type MCPModelSettings = {
  servers: Record<string, {
    enabled: boolean;
    identity_fingerprint?: string | null;
  }>;
};

export type MCPServerStatus = "disabled" | "disconnected" | "connecting" | "available" | "error";

export type MCPStdioServerConfig = {
  id: string;
  name: string;
  enabled: boolean;
  transport: "stdio";
  connect_timeout_seconds: number;
  call_timeout_seconds: number;
  command: string;
  args: string[];
  cwd?: string | null;
  env: Record<string, string>;
};

export type MCPHttpServerConfig = {
  id: string;
  name: string;
  enabled: boolean;
  transport: "streamable_http";
  connect_timeout_seconds: number;
  call_timeout_seconds: number;
  url: string;
  headers: Record<string, string>;
};

export type MCPServerConfig = MCPStdioServerConfig | MCPHttpServerConfig;

export type MCPToolInfo = {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  annotations: Record<string, unknown>;
};

export type MCPServerView = {
  config: MCPServerConfig;
  affected_model_count: number;
  runtime: {
    status: MCPServerStatus;
    tool_count: number;
    last_error: string | null;
    instructions: string | null;
  };
};

export type MCPTestResult = {
  status: "available";
  instructions: string | null;
  tools: MCPToolInfo[];
};

export type ContextStrategyConfig = {
  compression_trigger_tokens_k: number;
  max_screenshots_in_context: number;
  screenshot_ttl_human_messages: number;
};

export type AgentPluginSettings = {
  enabled: boolean;
  config: Record<string, unknown>;
};

export type PluginItem = {
  name: string;
  description: string;
  inherent: boolean;
  default_config: Record<string, unknown>;
  config_schema: Record<string, unknown>;
};

export type ToolItem = {
  name: string;
  description: string;
};

export type ToolPolicyView = {
  stable_key: string;
  source: "local" | "mcp";
  name: string;
  display_name: string;
  description: string;
  policy: ToolPolicy;
  server_id?: string | null;
  server_name?: string | null;
  available: boolean;
  unavailable_reason?: string | null;
};

export type ChatHistoryItem = {
  id?: string;
  role: string;
  content: string;
  timestamp: string;
  images?: string[];
  /**
   * 保存实际输入或回复来源，旧历史缺省为 desktop。
   */
  origin_channel?: string;
};

/**
 * 提交渠道归一化命令，受理结果与后续 Agent 事件分别取得。
 *
 * Attributes:
 *     session_id (string): 共享 Ayaya 会话标识。
 *     origin_channel (string): 真人输入或回复所属渠道，旧记录缺省为 desktop。
 *     kind (CommandKind): 已知统一命令类别，原始未知命令仍由后端业务拒绝。
 *     target_run_id (string): 停止或引导针对的运行轮次，防止过期动作影响另一轮。
 *     request_id (string): 持久中断标识，关联审批决定或屏幕执行结果。
 *     task_id (string): 受管进程任务标识。
 *     payload (Record<string, unknown>): 对应种类的专用数据，命令中省略时后端采用空对象。
 */
export type ChannelCommand = {
  session_id: string;
  origin_channel: string;
  kind: CommandKind;
  target_run_id?: string;
  request_id?: string;
  task_id?: string;
  payload?: Record<string, unknown>;
};

/**
 * 描述命令是否受理，不代表工具或对话已经完成。
 *
 * Attributes:
 *     accepted (boolean): 是否已受理，不能解释为操作已经完成。
 *     reason (string): 未受理的原因，可为空。
 *     run_id (string): 本次受理或事件关联的运行轮次。
 */
export type CommandReceipt = { accepted: boolean; reason?: string; run_id?: string };

/**
 * 表示共享会话中已注册渠道的启用配置和可用状态。
 *
 * Attributes:
 *     channel (string): 稳定注册渠道标识。
 *     display_name (string): 设置页展示名称。
 *     enabled (boolean): 渠道是否参与该会话双向互通。
 *     ready (boolean): 渠道注册描述中的可用状态，和订阅 enabled 分别保存。
 */
export type ChannelSubscription = {
  channel: string;
  display_name: string;
  enabled: boolean;
  ready: boolean;
};

/**
 * 表示可查询的后台任务记录，终态与输出由后端持久保存。
 *
 * Attributes:
 *     task_id (string): 受管进程任务标识。
 *     session_id (string): 共享 Ayaya 会话标识。
 *     command (string): 发起任务的原 PowerShell 命令。
 *     status (TaskStatus): 受管进程的运行或终态状态，与模型通知终态分别定义。
 *     pid (number | null): 受管进程 PID，不能用作任务稳定身份。
 *     exit_code (number | null): 实际进程退出码，未知时为空。
 *     created_at (string): 任务持久创建时间。
 *     ended_at (string | null): 任务终态时间，运行中为空。
 *     recent_output (string): 任务完整输出的有限尾部摘要。
 *     background (boolean): 显式或超时移交后台的标记，任务列表只包含后台记录。
 */
export type BackgroundTaskRecord = {
  task_id: string;
  session_id: string;
  command: string;
  status: TaskStatus;
  pid: number | null;
  exit_code: number | null;
  created_at: string;
  ended_at: string | null;
  recent_output?: string;
  background: boolean;
};

/**
 * 表示按字节游标读取的输出页，后续读取使用 next_offset。
 *
 * Attributes:
 *     text (string): 当前页已闭合 UTF-8 边界的正文。
 *     next_offset (number): 下一次读取的字节游标，不是字符下标。
 *     has_more (boolean): 当前输出文件中是否还有已存在内容。
 */
export type BackgroundTaskOutput = {
  text: string;
  next_offset: number;
  has_more: boolean;
};

/**
 * 承载当前接收渠道可见的会话状态，快照建立 epoch 和事件水位。
 *
 * Attributes:
 *     session_id (string): 共享 Ayaya 会话标识。
 *     kind (InteractionEventKind): 共享渠道的已知观察事件类别。
 *     payload (Record<string, unknown>): 对应种类的专用数据，命令中省略时后端采用空对象。
 *     origin_channel (string | null): 真人输入或回复所属渠道，旧记录缺省为 desktop。
 *     run_id (string | null): 本次受理或事件关联的运行轮次。
 *     request_id (string | null): 持久中断标识，关联审批决定或屏幕执行结果。
 *     task_id (string | null): 受管进程任务标识。
 *     stream_epoch (string): 后端启动生成的水位命名空间，新 epoch 先应用新快照。
 *     event_sequence (number): 同 epoch 按会话递增的观察事件序号，不用于授权或持久交付。
 */
export type InteractionEvent = {
  session_id: string;
  kind: InteractionEventKind;
  payload: Record<string, unknown>;
  origin_channel?: string | null;
  run_id?: string | null;
  request_id?: string | null;
  task_id?: string | null;
  stream_epoch: string;
  event_sequence: number;
};

export type ApiResponse<T> = {
  data?: T;
  msg?: string;
  code?: number;
};

export type ModelTransformData = {
  id: string;
  offsetX: number;
  offsetY: number;
  userScale: number;
  followCursor: boolean;
};

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

export type ControlScreenOperation = "click" | "double" | "right" | "scroll";
export type ControlScreenScrollDirection = "none" | "down" | "up";

export type ControlScreenCoordinates = {
  bbox: [number, number, number, number];
  x_ratio: number;
  y_ratio: number;
};

export type ControlScreenActionData = {
  target: string;
  operation: ControlScreenOperation;
  text: string;
  press_enter: boolean;
  wait_seconds: number;
  scroll_direction: ControlScreenScrollDirection;
  coordinates: ControlScreenCoordinates;
};

export type ApprovalDecision =
  | { kind: typeof ApprovalDecisionKind.APPROVE }
  | { kind: typeof ApprovalDecisionKind.REJECT }
  | { kind: typeof ApprovalDecisionKind.STEER; message: string };

export type FrontendSettings = {
  hide_on_screenshot: boolean;
};

export type LogSide = "frontend" | "backend";

export type LogSource = "electron-main" | "renderer" | "stdout" | "stderr";

export type LogLevel = "debug" | "info" | "warn" | "error";

export type LogRecord = {
  id: number;
  timestamp: string;
  side: LogSide;
  source: LogSource;
  level: LogLevel;
  scope: string;
  message: string;
};

export type LogSnapshot = {
  frontend: LogRecord[];
  backend: LogRecord[];
};

export type LogBatch = {
  records: LogRecord[];
};

export type ScreenActionRequest = ControlScreenActionData;

export type ScreenActionPoint = {
  x: number;
  y: number;
};

export type ScreenActionResult = {
  executed: boolean;
  error?: string;
};

export type ImportPreview = {
  selectedPath: string;
  sourceType: "directory";
  suggestedName: string;
  entryRelativePath: string;
};

/**
 * 指定桌面页面是否需要新的初始化快照，不改变后端事件协议。
 *
 * Attributes:
 *     refreshSnapshot (boolean | undefined): 新页面传 true，普通命令省略以复用连接。
 */
export interface DesktopSessionSubscriptionOptions {
  refreshSnapshot?: boolean;
}

export interface DesktopPetApi {
  /**
   * 读取会话任务记录。
   *
   * Args:
   *     sessionId (string): 共享会话标识。
   *
   * Returns:
   *     Promise<BackgroundTaskRecord[]>: 后端返回的持久任务列表。
   */
  getBackgroundTasks: (sessionId: string) => Promise<BackgroundTaskRecord[]>;
  /**
   * 按字节游标读取任务输出。
   *
   * Args:
   *     sessionId (string): 共享会话标识。
   *     taskId (string): 受管任务标识。
   *     offset (number): 输出起始字节游标。
   *
   * Returns:
   *     Promise<BackgroundTaskOutput>: 输出页和下一游标。
   */
  getBackgroundTaskOutput: (
    sessionId: string, taskId: string, offset: number,
  ) => Promise<BackgroundTaskOutput>;
  /**
   * 查询会话注册渠道及启用状态。
   *
   * Args:
   *     sessionId (string): 共享会话标识。
   *
   * Returns:
   *     Promise<ChannelSubscription[]>: 桌面固定开启的渠道列表。
   */
  getChannelSubscriptions: (sessionId: string) => Promise<ChannelSubscription[]>;
  /**
   * 持久修改可选渠道的会话启用状态。
   *
   * Args:
   *     sessionId (string): 共享会话标识。
   *     channel (string): 注册渠道标识。
   *     enabled (boolean): 是否参与双向互通。
   *
   * Returns:
   *     Promise<ChannelSubscription[]>: 后端确认的更新列表。
   */
  setChannelSubscription: (
    sessionId: string, channel: string, enabled: boolean,
  ) => Promise<ChannelSubscription[]>;
  /**
   * 通过主进程提交桌面来源命令。
   *
   * Args:
   *     command (ChannelCommand): 统一入站动作。
   *
   * Returns:
   *     Promise<CommandReceipt>: 受理结果，实际运行状态通过事件取得。
   */
  submitInteractionCommand: (command: ChannelCommand) => Promise<CommandReceipt>;
  /**
   * 建立或复用桌面会话事件订阅。
   *
   * Args:
   *     sessionId (string): 共享会话标识。
   *     options (DesktopSessionSubscriptionOptions | undefined): 新页面传入刷新选项，普通发送省略以复用连接。
   *
   * Returns:
   *     Promise<void>: 首次会话快照到达后完成。
   */
  subscribeDesktopSession: (sessionId: string, options?: DesktopSessionSubscriptionOptions) => Promise<void>;
  /**
   * 注册当前 Renderer 的统一事件监听。
   *
   * Args:
   *     callback ((event: InteractionEvent) => void): 可见事件处理函数。
   *
   * Returns:
   *     () => void: 只移除该监听器的清理函数。
   */
  onInteractionEvent: (callback: (event: InteractionEvent) => void) => () => void;
  selectImages: () => Promise<Array<{ path: string; dataUrl: string }> | null>;
  setMousePassthrough: (enabled: boolean) => void;
  setPointerInteractive: (enabled: boolean) => void;
  /**
   * 打开设置窗口，任务快捷入口可指定直接进入任务页。
   *
   * Args:
   *     page ("tasks" | undefined): 指定任务页；省略时保留普通设置入口行为。
   */
  openSettingsWindow: (page?: "tasks") => void;
  /**
   * 监听已打开设置窗口的任务页导航，返回监听清理函数。
   *
   * Args:
   *     callback ((page: "tasks") => void): 导航事件处理函数。
   *
   * Returns:
   *     () => void: 移除当前窗口监听的函数。
   */
  onOpenSettingsPage: (callback: (page: "tasks") => void) => () => void;
  openLogWindow: () => void;
  minimizeCurrentWindow: () => void;
  closeCurrentWindow: () => void;
  openImagePreview: (imageSrc: string) => void;
  getActiveModel: () => Promise<ModelInfo>;
  getModelConfig: () => Promise<ModelConfigView>;
  getChatSettings: () => Promise<ChatSettingsData>;
  getLatestAiMessage: (sessionId?: string) => Promise<{
    sessionId: string;
    latestAiMessage: string | null;
  }>;
  getChatHistory: (sessionId: string, start: number, limit: number) => Promise<ChatHistoryItem[]>;
  getChatHistoryLastN: (sessionId: string, n: number) => Promise<ChatHistoryItem[]>;
  updateChatSettings: (settings: ChatSettingsData) => Promise<ChatSettingsData>;
  getToolPermissions: (sessionId: string) => Promise<ToolPolicyView[]>;
  updateToolPermissions: (
    sessionId: string,
    permissions: Record<string, ToolPolicy>
  ) => Promise<ToolPolicyView[]>;
  getAvailableTools: () => Promise<{ tools: ToolItem[] }>;
  getAvailablePlugins: () => Promise<{ plugins: PluginItem[] }>;
  getMcpServers: () => Promise<MCPServerView[]>;
  testMcpServer: (config: MCPServerConfig) => Promise<MCPTestResult>;
  createMcpServer: (config: MCPServerConfig) => Promise<MCPServerView>;
  updateMcpServer: (serverId: string, config: MCPServerConfig) => Promise<MCPServerView>;
  deleteMcpServer: (serverId: string) => Promise<{ affected_sessions: string[] }>;
  reconnectMcpServer: (serverId: string) => Promise<MCPServerView>;
  getMcpServerTools: (serverId: string) => Promise<MCPToolInfo[]>;
  previewLive2DImport: () => Promise<ImportPreview | null>;
  importLive2DModel: (payload: {
    selectedPath: string;
    suggestedName?: string;
  }) => Promise<{ id: string; name: string; sessionId: string; source: string }>;
  updateModelTransform: (payload: {
    modelId: string;
    offsetX?: number;
    offsetY?: number;
    userScale?: number;
    followCursor?: boolean;
  }) => Promise<{
    modelId: string;
    offsetX: number;
    offsetY: number;
    userScale: number;
    followCursor: boolean;
  }>;
  setActiveModel: (modelId: string) => Promise<{ activeModelId: string }>;
  deleteModel: (modelId: string) => Promise<{ activeModelId: string }>;
  onCursor?: (callback: (data: CursorSyncData) => void) => () => void;
  onModelChanged?: (callback: (model: ModelInfo) => void) => () => void;
  onModelTransformChanged?: (callback: (data: ModelTransformData) => void) => () => void;
  getFrontendSettings: () => Promise<FrontendSettings>;
  updateFrontendSettings: (settings: Partial<FrontendSettings>) => Promise<FrontendSettings>;
  getMotionConfig: (modelId: string) => Promise<MotionConfig[]>;
  updateModelMotionConfig: (payload: { modelId: string; motionConfig: MotionConfig[] }) => Promise<void>;
  playMotion: (motionName: string) => void;
  onPlayMotionRequest?: (callback: (motionName: string) => void) => () => void;
  onMotionConfigChanged?: (
    callback: (payload: { modelId: string; motionConfig: MotionConfig[] }) => void
  ) => () => void;
  getLogSnapshot: () => Promise<LogSnapshot>;
  onLogBatch: (callback: (batch: LogBatch) => void) => () => void;
  clearLogBuffer: (side: LogSide) => Promise<void>;
  openLogDirectory: () => Promise<void>;
}
