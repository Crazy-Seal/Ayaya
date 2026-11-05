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
 */
export const SessionPhase = {
  IDLE: "idle",
  RUNNING: "running",
  PENDING_INTERRUPT: "pending_interrupt",
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
  INTERACT_SUBAGENT: "interact_subagent",
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
  NOTICE: "notice",
  RUN_STARTED: "run_started",
  TEXT_CHUNK: "text_chunk",
  TOOL_CALL: "tool_call",
  TOOL_COMPLETED: "tool_completed",
  INTERRUPT: "interrupt",
  AUTHORIZATION_RESOLVED: "authorization_resolved",
  RUN_COMPLETED: "run_completed",
  ERROR: "error",
  SUBAGENT_STATUS: "subagent_status",
  SUBAGENT_INTERRUPT: "subagent_interrupt",
} as const;
export type InteractionEventKind = typeof InteractionEventKind[keyof typeof InteractionEventKind];

/**
 * 工具完成并保存后提供的调用标识和历史查询终点。
 *
 * Attributes:
 *     tool_call_id (string): 本次工具调用的编号。
 *     tool_name (string): 已执行完成的工具名称。
 *     message_id (string): 已保存结果消息的编号，历史查询包含该消息并排除其后的回复。
 */
export type ToolCompletedData = {
  tool_call_id: string;
  tool_name: string;
  message_id: string;
};

/**
 * 区分人工授权请求与独立桌面执行 continuation。
 *
 * Attributes:
 *     AUTHORIZATION_REQUEST (string): 人工审批，协议值为 authorization_request。
 */
export const InterruptKind = {
  AUTHORIZATION_REQUEST: "authorization_request",
} as const;
export type InterruptKind = typeof InterruptKind[keyof typeof InterruptKind];

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

/** 表示模型动作的播放规则。 */
export const MotionSettingType = {
  IDLE: "idle",
  EXPRESSION: "expression",
  NONE: "none",
} as const;
export type MotionSettingType = typeof MotionSettingType[keyof typeof MotionSettingType];

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
    source: ModelSource;
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

/**
 * 保存 LLM 配置成功后，通知聊天与模型卡片更新指定会话的 AI 名字。
 *
 * Attributes:
 *     sessionId (string): 已保存配置所属的会话编号。
 *     name (string): 已保存 AI 名字；未填写时使用默认称呼。
 */
export type AiNameChangedPayload = { sessionId: string; name: string };

/** 表示会话对工具的许可、审批或拒绝策略。 */
export const ToolPolicy = {
  ALLOW: "allow",
  ASK: "ask",
  DENY: "deny",
} as const;
export type ToolPolicy = typeof ToolPolicy[keyof typeof ToolPolicy];

export type MCPModelSettings = {
  servers: Record<string, {
    enabled: boolean;
    identity_fingerprint?: string | null;
  }>;
};

/** 表示后端管理的 MCP 服务连接状态。 */
export const MCPServerStatus = {
  DISABLED: "disabled",
  DISCONNECTED: "disconnected",
  CONNECTING: "connecting",
  AVAILABLE: "available",
  ERROR: "error",
} as const;
export type MCPServerStatus = typeof MCPServerStatus[keyof typeof MCPServerStatus];

export type MCPStdioServerConfig = {
  id: string;
  name: string;
  enabled: boolean;
  transport: typeof MCPTransport.STDIO;
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
  transport: typeof MCPTransport.STREAMABLE_HTTP;
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
  status: typeof MCPServerStatus.AVAILABLE;
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
  tool_permissions?: Record<string, ToolPolicy>;
  mcp_identities?: Record<string, string>;
};

/** 插件 MCP 的只读状态，不包含连接参数和启停开关。 */
export type PluginMCPStatus = {
  id: string;
  name: string;
  runtime: MCPServerView["runtime"];
};

export type PluginItem = {
  name: string;
  description: string;
  default_config: Record<string, unknown>;
  config_schema: Record<string, unknown>;
};

export type ToolItem = {
  name: string;
  description: string;
};

export type ToolPolicyView = {
  stable_key: string;
  source: ToolSource;
  name: string;
  display_name: string;
  description: string;
  policy: ToolPolicy;
  server_id?: string | null;
  server_name?: string | null;
  available: boolean;
  unavailable_reason?: string | null;
  plugin_name?: string | null;
  policy_key?: string;
};

export type ChatHistoryItem = {
  id?: string;
  role: HistoryRole;
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
  subagent_id?: string;
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
 *     tool_name (string | null): 创建任务的实际工具名，旧记录为空。
 *     recent_output (string): 任务完整输出的有限尾部摘要。
 *     background (boolean): 显式或超时移交后台的标记，任务列表只包含后台记录。
 */
export type BackgroundTaskRecord = {
  task_id: string;
  tool_name: string | null;
  session_id: string;
  command: string | null;
  name: string;
  title: string;
  result: string | null;
  error: string | null;
  plugin_name: string | null;
  output_ref: string | null;
  notify_agent: boolean;
  status: TaskStatus;
  pid: number | null;
  exit_code: number | null;
  created_at: string;
  ended_at: string | null;
  recent_output?: string;
  background: boolean;
  subagent_id?: string | null;
};

/** 子 Agent 的可见状态，与真人审批展示状态分别管理。 */
export const SubagentStatus = { IDLE: "idle", RUNNING: "running", WAITING_PARENT: "waiting_parent" } as const;
export type SubagentStatus = typeof SubagentStatus[keyof typeof SubagentStatus];

/** 父会话当前可用子身份及其实际运行摘要。 */
export type SubagentSummary = {
  subagent_id: string;
  name: string;
  if_retain_context: boolean;
  status: SubagentStatus;
  started_at: string | null;
  created_at: string;
  error?: string | null;
  waiting_for_approval?: boolean;
};

/** 按 ID 查询的定义、最近显式交互及已保存最终报告。 */
export type SubagentDetail = {
  subagent_id: string;
  name: string;
  system_prompt: string;
  if_retain_context: boolean;
  context_available: boolean;
  created_at: string;
  tool_whitelist: string[];
  status: SubagentStatus | null;
  recent_interactions: Array<{ interaction_id: number; kind: string; text: string; created_at: string }>;
  results: Array<{ task_id: string; status: TaskStatus; result: string | null; error: string | null; created_at: string; ended_at: string | null }>;
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

/**
 * 未交付失败时返回的原始输入，仅用于当前页面重发。
 * Attributes:
 *     input_sequence (number): 已从 Inbox 删除的原输入编号。
 *     message (string): 用户原正文。
 *     images (string[]): 原始图片 data URL。
 */
export type FailedInteractionInput = {
  input_sequence: number;
  message: string;
  images: string[];
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

export type ApprovalDecision =
  | { kind: typeof ApprovalDecisionKind.APPROVE }
  | { kind: typeof ApprovalDecisionKind.REJECT }
  | { kind: typeof ApprovalDecisionKind.STEER; message: string };

export type FrontendSettings = {
  /** 聊天窗口普通状态的可见页面区域坐标和尺寸，不包含系统隐藏边框，不保存最小化状态。 */
  chat_window_bounds?: { x: number; y: number; width: number; height: number };
  /** 用户是否要求聊天窗口置顶，默认关闭。 */
  chat_window_pinned?: boolean;
};

/** 全部会话共享的后端桌面 YAML 配置。 */
export type DesktopSettings = { hide_on_screenshot: boolean };

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

/**
 * 约定页面可通过 preload 调用的桌面接口，统一聊天、设置、模型、日志和事件订阅能力。
 */
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
  getSubagents: (sessionId: string) => Promise<SubagentSummary[]>;
  getSubagentDetails: (sessionId: string, subagentId: string) => Promise<SubagentDetail>;
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
  openSettingsWindow: (page?: "tasks" | "subagents") => void;
  /**
   * 监听已打开设置窗口的任务页导航，返回监听清理函数。
   *
   * Args:
   *     callback ((page: "tasks" | "subagents") => void): 导航事件处理函数。
   *
   * Returns:
   *     () => void: 移除当前窗口监听的函数。
   */
  onOpenSettingsPage: (callback: (page: "tasks" | "subagents") => void) => () => void;
  openLogWindow: () => void;
  minimizeCurrentWindow: () => void;
  closeCurrentWindow: () => void;
  /**
   * 设置聊天窗口置顶，仅聊天窗口调用。
   * Args:
   *     pinned (boolean): 用户选择的置顶状态。
   * Returns:
   *     Promise<boolean>: 主进程保存并应用后的置顶状态。
   */
  setChatPinned: (pinned: boolean) => Promise<boolean>;
  /** 页面按住当前窗口标题栏或边缘后，主进程开始采样真实桌面鼠标。 */
  beginWindowGesture: (kind: WindowGesture) => void;
  /** 页面收到松开、取消或失去鼠标捕获后停止移动和缩放。 */
  endWindowGesture: () => void;
  /**
   * 更新聊天窗口提醒，窗口最小化不改变会话或审批状态。
   * Args:
   *     update (ChatAttentionUpdate): 已通过事件水位校验的消息与审批变化。
   */
  reportChatAttention: (update: ChatAttentionUpdate) => void;
  /**
   * 聊天或设置页确认退出后，请求主进程统一锁定窗口并清理应用。
   *
   * Returns:
   *     Promise<void>: 主进程已受理退出请求；窗口来源无效或接口未更新时拒绝。
   */
  requestApplicationExit: () => Promise<void>;
  /** 页面首次加载时查询是否正在退出，补齐早于页面加载完成的通知。 */
  getApplicationShutdownState: () => Promise<boolean>;
  /** 退出确认前查询全应用执行或清理中的任务数量。 */
  getActiveTaskSummary: () => Promise<{ active_count: number }>;
  /** 监听应用开始退出，调用方立即锁定本页面的功能操作。 */
  onApplicationShutdown: (callback: () => void) => () => void;
  /**
   * 聊天识别表情后通知 Live2D 页面，主进程只接受已配置的标签。
   * Args:
   *     label (string): 当前模型允许的表情标签。
   */
  playExpression: (label: string) => void;
  /**
   * Live2D 加载失败后通知聊天页面，不影响聊天初始化。
   * Args:
   *     message (string): 模型加载错误说明。
   */
  reportModelError: (message: string) => void;
  /**
   * 监听 Live2D 页模型加载错误。
   * Args:
   *     callback ((message: string) => void): 聊天中的错误展示回调。
   * Returns:
   *     () => void: 移除监听的清理函数。
   */
  onModelError: (callback: (message: string) => void) => () => void;
  /**
   * 监听跨窗口表情播放请求。
   * Args:
   *     callback ((label: string) => void): Live2D 的标签播放回调。
   * Returns:
   *     () => void: 移除监听的清理函数。
   */
  onExpressionRequest: (callback: (label: string) => void) => () => void;
  openImagePreview: (imageSrc: string) => void;
  getActiveModel: () => Promise<ModelInfo>;
  getModelConfig: () => Promise<ModelConfigView>;
  /**
   * 查询本地模型对应会话的 AI 名字，仅返回显示字段。
   *
   * Args:
   *     sessionIds (string[] | undefined): 需要查询的模型会话；未指定时查询整个本地模型列表。
   *
   * Returns:
   *     Promise<Record<string, string>>: 会话编号到已保存 AI 名字的对应关系。
   */
  getAiNames: (sessionIds?: string[]) => Promise<Record<string, string>>;
  getChatSettings: () => Promise<ChatSettingsData>;
  getLatestAiMessage: (sessionId?: string) => Promise<{
    sessionId: string;
    latestAiMessage: string | null;
  }>;
  getChatHistory: (sessionId: string, start: number, limit: number) => Promise<ChatHistoryItem[]>;
  getChatHistoryLastN: (sessionId: string, n: number, throughMessageId?: string) => Promise<ChatHistoryItem[]>;
  updateChatSettings: (settings: ChatSettingsData) => Promise<ChatSettingsData | { configuration_errors: Array<{ loc: Array<string | number>; msg: string }> }>;
  getToolPermissions: (sessionId: string) => Promise<ToolPolicyView[]>;
  updateToolPermissions: (
    sessionId: string,
    permissions: Record<string, ToolPolicy>
  ) => Promise<ToolPolicyView[]>;
  getAvailableTools: () => Promise<{ tools: ToolItem[] }>;
  getAvailablePlugins: () => Promise<{ plugins: PluginItem[]; errors?: string[] }>;
  getPluginMcpStatuses: (sessionId: string) => Promise<Record<string, PluginMCPStatus[]>>;
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
  /**
   * 订阅 AI 名字保存结果，供聊天标题和模型卡片及时更新。
   *
   * Args:
   *     callback ((payload: AiNameChangedPayload) => void): 配置已保存后接收会话编号和名字的回调。
   *
   * Returns:
   *     () => void: 取消当前订阅的函数。
   */
  onAiNameChanged?: (callback: (payload: AiNameChangedPayload) => void) => () => void;
  onModelTransformChanged?: (callback: (data: ModelTransformData) => void) => () => void;
  getFrontendSettings: () => Promise<FrontendSettings>;
  getDesktopSettings: () => Promise<DesktopSettings>;
  updateDesktopSettings: (settings: DesktopSettings) => Promise<DesktopSettings>;
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

/**
 * 表示聊天页经过事件去重后提交的窗口提醒变化。
 * Attributes:
 *     newMessage (boolean | undefined): 本次出现新的回复正文。
 *     approvalId (string | null | undefined): 当前待审批请求，null 表示已处理。
 *     reset (boolean | undefined): 会话切换时清除上一会话提醒。
 */
export type ChatAttentionUpdate = { newMessage?: boolean; approvalId?: string | null; reset?: boolean };

/** 当前应用窗口标题栏移动或八个边缘方向的缩放，不接受页面提供的窗口坐标。 */
export type WindowGesture = "move" | "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

/** 区分本地工具和 MCP 工具。 */
export const ToolSource = {
  LOCAL: "local",
  MCP: "mcp",
} as const;
export type ToolSource = typeof ToolSource[keyof typeof ToolSource];

/** 区分应用内置模型和用户导入模型。 */
export const ModelSource = {
  BUILTIN: "builtin",
  CUSTOM: "custom",
} as const;
export type ModelSource = typeof ModelSource[keyof typeof ModelSource];

/** 表示 MCP 配置采用的传输方式。 */
export const MCPTransport = {
  STDIO: "stdio",
  STREAMABLE_HTTP: "streamable_http",
} as const;
export type MCPTransport = typeof MCPTransport[keyof typeof MCPTransport];

/** 表示后端历史角色，同时保留旧记录使用的角色名称。 */
export const HistoryRole = {
  HUMAN: "Human",
  AI: "AI",
  AI_TOOL_CALLING: "AI_Tool_Calling",
  PLUGIN: "Plugin",
  USER: "user",
  ASSISTANT: "assistant",
} as const;
export type HistoryRole = typeof HistoryRole[keyof typeof HistoryRole] | Lowercase<typeof HistoryRole[keyof typeof HistoryRole]>;

/** 区分真人附件、工具图片、可压缩截图和临时环境背景图片的来源。 */
export const MediaKind = {
  HUMAN: "human",
  TOOL: "tool",
  SCREENSHOT: "screenshot",
  MCP: "mcp",
  ENVIRONMENT: "environment",
} as const;
export type MediaKind = typeof MediaKind[keyof typeof MediaKind];

/** 历史图片在 Electron 中使用的受控协议名称。 */
export const AYAYA_IMAGE_SCHEME = "ayaya-image";
/** 聊天界面访问本地历史图片时使用的地址前缀。 */
export const AYAYA_IMAGE_URI_PREFIX = `${AYAYA_IMAGE_SCHEME}://local/`;
