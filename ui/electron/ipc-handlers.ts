/**
 * IPC 处理器注册
 */
import { IpcChannel } from "../ipc-channels.js";
import { EXIT_TASK_QUERY_TIMEOUT_MS } from "../runtime-constants.js";

import { BrowserWindow, dialog, screen, clipboard } from "electron";
import fs from "node:fs";
import path from "node:path";

import { FRONTEND_SETTINGS_PATH } from "./config.js";
import { readFrontendSettings, saveFrontendSettings } from "./frontend-settings-store.js";
import { getDesktopSettings, updateDesktopSettings } from "./desktop-settings.js";
import { backendFetch } from "./backend-client.js";
import { submitInteractionCommand, subscribeDesktopSession } from "./interaction-adapter.js";
import type { ChannelCommand, ChatAttentionUpdate, DesktopSettings } from "../shared-types.js";
import { DESKTOP_CHANNEL } from "../shared-types.js";
import type {
  ChatSettingsData,
  ModelTransformPayload,
  ToolItem,
  ModelChangedPayload,
  ModelTransformChangedPayload,
  FrontendSettings,
} from "./types.js";
import {
  loadModelConfig,
  saveModelConfig,
  getActiveModelRecord,
  resolveModelUrl,
  resolveRootDirAbsolute,
  inspectImportSource,
  resolveUniqueModelDir,
  copyDirectory,
  findModel3JsonRelativePath,
  createModelRecord,
  sanitizeModelName,
} from "./model-manager.js";
import {
  ensureChatSettingsLoaded,
  createEmptyChatSettings,
  deleteChatSettingsBySessionId,
  fetchLatestAiMessageBySessionId,
  fetchChatHistoryPageBySessionId,
  fetchChatHistoryLastN,
  updateChatSettings,
  clearChatSettingsCache,
  runMcpMutationWithCacheInvalidation,
  fetchAvailableTools,
  fetchToolPermissions,
  saveToolPermissions,
  fetchAvailablePlugins,
  fetchPluginMcpStatuses,
  PluginConfigurationError,
} from "./chat-settings.js";
import { getMainWindow, getChatWindow, getSettingsWindow, getTrustedRendererPolicy, openSettingsWindow, openImagePreviewWindow, setChatPinned, updateChatAttention, deliverModelError,
  beginWindowGesture, endWindowGesture, setPetPointerInteractive } from "./window-manager.js";
import { isWindowGesture } from "./window-gesture.js";
import { forgetAiName, readAiNames, rememberAiName } from "./ai-names.js";
import { applicationOperations, SHUTDOWN_STATE_CHANNEL } from "./application-operations.js";
import { createTrustedIpcRegistrar } from "./ipc-security.js";
import { appendFrontendLog } from "./logging/app-logger.js";
import {
  createMcpServer,
  deleteMcpServer,
  fetchMcpServers,
  fetchMcpServerTools,
  reconnectMcpServer,
  testMcpServer,
  updateMcpServer,
} from "./mcp-api.js";
import type {
  MCPServerConfig,
  ToolPolicy,
} from "../shared-types.js";

/**
 * 通知模型已更改（同时通知主窗口和设置窗口）
 */
const notifyModelChanged = (): void => {
  const active = getActiveModelRecord();
  deliverModelError("");
  const payload: ModelChangedPayload = {
    id: active.id,
    name: active.name,
    sessionId: active.sessionId,
    modelUrl: resolveModelUrl(active),
    offsetX: active.offsetX ?? 0,
    offsetY: active.offsetY ?? 0,
    userScale: active.userScale ?? 1,
    followCursor: active.followCursor ?? true,
  };

  // 1. 角色、聊天和设置共用同一活动模型身份。
  for (const window of [getMainWindow(), getChatWindow(), getSettingsWindow()]) {
    if (window && !window.isDestroyed()) window.webContents.send(IpcChannel.MODEL_CHANGED, payload);
  }
};

/**
 * 通知主窗口模型变换已更改
 *
 * Args:
 *     model ({ id: string; offsetX?: number; offsetY?: number; userScale?: number; followCursor?: boolean; }): 需要展示的后端恢复对话框信息。
 */
const notifyModelTransformChanged = (model: {
  id: string;
  offsetX?: number;
  offsetY?: number;
  userScale?: number;
  followCursor?: boolean;
}): void => {
  const mainWindow = getMainWindow();
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }

  const payload: ModelTransformChangedPayload = {
    id: model.id,
    offsetX: model.offsetX ?? 0,
    offsetY: model.offsetY ?? 0,
    userScale: model.userScale ?? 1,
    followCursor: model.followCursor ?? true,
  };

  mainWindow.webContents.send(IpcChannel.MODEL_TRANSFORM_CHANGED, payload);
};

/**
 * 注册受信任 Renderer 的 IPC 入口，将运行时命令、事件和任务查询接入主进程。
 * Args:
 *     options ({ requestApplicationExit: () => void }): 聊天或设置页确认退出后调用的统一应用清理入口。
 */
export const registerIpcHandlers = (options: { requestApplicationExit: () => void }): void => {
  const trustedIpc = createTrustedIpcRegistrar(getTrustedRendererPolicy());
  trustedIpc.handle(SHUTDOWN_STATE_CHANNEL, /** 页面首次加载时补查退出状态，退出门禁只保留此查询。 */
  () => applicationOperations.isShuttingDown());
  // 鼠标穿透控制
  trustedIpc.on(IpcChannel.SET_MOUSE_PASSTHROUGH, /**
   * 可信页面请求鼠标穿透时定位发送窗口，再更新窗口是否忽略鼠标事件。
   *
   * Args:
   *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
   *     enabled (boolean): 是否启用当前界面选项。
   */
  (event, enabled: boolean) => {
    const win = BrowserWindow.fromWebContents(event.sender) ?? getMainWindow();
    if (!win) {
      return;
    }

    win.setIgnoreMouseEvents(Boolean(enabled), { forward: true });
  });

  // 指针交互控制
  trustedIpc.on(IpcChannel.SET_POINTER_INTERACTIVE, /**
   * 可信页面更新模型可交互状态时，反向设置窗口穿透，允许模型区域接收鼠标。
   *
   * Args:
   *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
   *     enabled (boolean): 是否启用当前界面选项。
   */
  (event, enabled: boolean) => {
    // 1. 只有角色页面能改变角色穿透；应用窗口拖动时暂存结果而不切换窗口样式。
    if (event.sender === getMainWindow()?.webContents && typeof enabled === "boolean") setPetPointerInteractive(enabled);
  });

  trustedIpc.on(IpcChannel.BEGIN_WINDOW_GESTURE, /**
   * 可信页面以合法方向开始拖动自己的窗口，主进程只接受已登记聊天或日志窗口。
   * Args:
   *     event (IpcMainEvent): 已通过来源检查的发送者。
   *     kind (unknown): 页面请求的操作方向。
   */
  (event, kind: unknown) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win && isWindowGesture(kind)) beginWindowGesture(win, kind);
  });
  trustedIpc.on(IpcChannel.END_WINDOW_GESTURE, /**
   * 页面松开或取消鼠标捕获后只结束自己的采样，其他可信窗口不能中断操作。
   * Args:
   *     event (IpcMainEvent): 已通过来源检查的发送者。
   */
  (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (win) endWindowGesture(win);
  });

  // 获取当前激活的模型
  trustedIpc.handle(IpcChannel.GET_ACTIVE_MODEL, /**
   * 页面查询活动模型时返回身份、会话、资源地址和默认显示参数。
   *
   * Returns:
   *     { id: string; name: string; sessionId: string; modelUrl: string; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig: MotionConfig[]; entry: string; }: 当前活动模型的页面记录。
   */
  () => {
    const active = getActiveModelRecord();
    return {
      id: active.id,
      name: active.name,
      sessionId: active.sessionId,
      modelUrl: resolveModelUrl(active),
      offsetX: active.offsetX ?? 0,
      offsetY: active.offsetY ?? 0,
      userScale: active.userScale ?? 1,
      followCursor: active.followCursor ?? true,
      motionConfig: active.motionConfig ?? [],
      entry: active.entry,
    };
  });

  // 获取模型配置
  trustedIpc.handle(IpcChannel.GET_MODEL_CONFIG, /**
   * 页面查询模型列表时读取本地配置，并转换为页面可见的模型目录。
   *
   * Returns:
   *     { activeModelId: string; models: { id: string; name: string; sessionId: string; source: ModelSource; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig: MotionConfig[]; entry: string; modelUrl: string; }[]; }: 活动编号及模型列表。
   */
  () => {
    const config = loadModelConfig();
    return {
      activeModelId: config.activeModelId,
      models: config.models.map(/**
       * 将本地模型记录转换为页面目录项，内置模型明确标记不可删除。
       *
       * Args:
       *     item (ModelRecord): 当前遍历的配置或目录条目。
       *
       * Returns:
       *     { id: string; name: string; sessionId: string; source: ModelSource; deletable: boolean; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; motionConfig: MotionConfig[]; entry: string; modelUrl: string; }: 单个模型的显示与操作信息。
       */
      (item) => ({
        id: item.id,
        name: item.name,
        sessionId: item.sessionId,
        source: item.source,
        deletable: item.source !== "builtin",
        offsetX: item.offsetX ?? 0,
        offsetY: item.offsetY ?? 0,
        userScale: item.userScale ?? 1,
        followCursor: item.followCursor ?? true,
        motionConfig: item.motionConfig ?? [],
        entry: item.entry,
        modelUrl: resolveModelUrl(item),
      })),
    };
  });

  trustedIpc.handle(IpcChannel.GET_AI_NAMES, /**
   * 只读取本地模型已有会话的名字；聊天按当前会话查询，模型列表按需查询全部。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 来源已经通过验证的窗口请求。
   *     sessionIds (unknown): 可选会话编号列表，必须全部属于本地模型目录。
   *
   * Returns:
   *     Promise<Record<string, string>>: 查询成功的名字；单项失败不丢弃其他有效结果。
   */
  async (_event, sessionIds: unknown) => {
    // 1. 页面不能查询模型目录之外的会话；未指定时使用整个目录。
    const known = new Set(loadModelConfig().models.map(/**
     * 从本地模型目录提取允许查询的会话编号。
     *
     * Args:
     *     model (ModelRecord): 当前本地模型记录。
     *
     * Returns:
     *     string: 当前模型对应会话编号。
     */
    (model) => model.sessionId));
    if (sessionIds === undefined) return readAiNames([...known]);
    if (!Array.isArray(sessionIds) || sessionIds.some(/**
     * 拒绝类型错误或不属于本地模型的名字查询，避免页面扩大查询范围。
     *
     * Args:
     *     sessionId (unknown): 页面请求的单个会话编号。
     *
     * Returns:
     *     boolean: 查询不合法时为 true。
     */
    (sessionId) => typeof sessionId !== "string" || !known.has(sessionId))) throw new Error("无效的模型会话编号");
    return readAiNames([...new Set(sessionIds as string[])]);
  });

  // 获取聊天设置
  trustedIpc.handle(IpcChannel.GET_CHAT_SETTINGS, /**
   * 页面查询模型设置时等待主进程加载缓存或后端设置。
   *
   * Returns:
   *     Promise<ChatSettingsData>: 当前活动会话设置。
   */
  async () => {
    const settings = await ensureChatSettingsLoaded();
    return settings;
  });

  // 获取最新 AI 消息
  trustedIpc.handle(IpcChannel.GET_LATEST_AI_MESSAGE, /**
   * 页面查询最近回复时使用指定会话，未指定则使用活动模型会话。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<{ sessionId: string; latestAiMessage: string | null; }>: 会话编号和最近助手回复。
   */
  async (_event, sessionId?: string) => {
    const resolvedSessionId = sessionId || getActiveModelRecord().sessionId;
    return {
      sessionId: resolvedSessionId,
      latestAiMessage: await fetchLatestAiMessageBySessionId(resolvedSessionId),
    };
  });

  // 获取聊天历史
  trustedIpc.handle(
    IpcChannel.GET_CHAT_HISTORY,
    /**
     * 页面分页查询历史时按会话、起点和上限请求后端。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     sessionId (string): 当前操作所属的会话编号。
     *     start (number): 可见聊天历史的起始位置，从零开始。
     *     limit (number): 最多保留或读取的记录数量。
     *
     * Returns:
     *     Promise<ChatHistoryItem[]>: 该页可见历史消息。
     */
    async (_event, sessionId: string, start: number, limit: number) => {
      return await fetchChatHistoryPageBySessionId(sessionId, start, limit);
    }
  );

  // 获取最后 N 条聊天历史
  trustedIpc.handle(
    IpcChannel.GET_CHAT_HISTORY_LAST_N,
    /**
     * 页面恢复聊天时请求指定会话最近若干条历史。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     sessionId (string): 当前操作所属的会话编号。
     *     n (number): 最多读取的历史消息数量。
     *     throughMessageId (string | undefined): 工具完成后的历史终点，省略时读取最新消息。
     *
     * Returns:
     *     Promise<ChatHistoryItem[]>: 最近可见消息列表。
     */
    async (_event, sessionId: string, n: number, throughMessageId?: string) => {
      return await fetchChatHistoryLastN(sessionId, n, throughMessageId);
    }
  );

  // 更新聊天设置
  trustedIpc.handle(IpcChannel.UPDATE_CHAT_SETTINGS, /**
   * 页面保存模型设置时等待后端校验和持久保存，返回实际保存结果。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     payload (ChatSettingsData): 已经过入口校验的桌面动作参数。
   *
   * Returns:
   *     Promise<ChatSettingsData>: 规范化并保存后的会话设置。
   */
  async (_event, payload: ChatSettingsData) => {
    // 1. 后端校验和保存成功后才通知名字变化，失败时不把未保存名字显示给其他窗口。
    let settings: ChatSettingsData;
    try {
      settings = await updateChatSettings(payload);
    } catch (error) {
      if (error instanceof PluginConfigurationError) return { configuration_errors: error.issues };
      throw error;
    }
    const update = { sessionId: settings.session_id, name: rememberAiName(settings.session_id, settings.name) };
    // 2. 只广播显示所需的会话编号和名字，不把 API Key 等设置交给聊天页面。
    for (const window of [getChatWindow(), getSettingsWindow()]) {
      if (window && !window.isDestroyed()) window.webContents.send(IpcChannel.AI_NAME_CHANGED, update);
    }
    return settings;
  });

  // 获取可用工具
  trustedIpc.handle(IpcChannel.GET_AVAILABLE_TOOLS, /**
   * 页面请求工具目录时读取当前本地工具名称和说明。
   *
   * Returns:
   *     Promise<{ tools: ToolItem[]; }>: 包装为 tools 的工具列表。
   */
  async () => {
    const tools = await fetchAvailableTools();
    return { tools };
  });
  trustedIpc.handle(IpcChannel.GET_TOOL_PERMISSIONS, /**
   * 工具设置页读取当前会话最新的本地及 MCP 权限。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<ToolPolicyView[]>: 当前工具权限目录。
   */
  async (_event, sessionId: string) =>
    await fetchToolPermissions(sessionId));
  trustedIpc.handle(
    IpcChannel.UPDATE_TOOL_PERMISSIONS,
    /**
     * 工具设置页保存权限时等待后端校验和更新。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     sessionId (string): 当前操作所属的会话编号。
     *     permissions (Record<string, ToolPolicy>): 按工具稳定标识保存的权限。
     *
     * Returns:
     *     Promise<ToolPolicyView[]>: 保存后的最新工具权限目录。
     */
    async (_event, sessionId: string, permissions: Record<string, ToolPolicy>) =>
      await saveToolPermissions(sessionId, permissions)
  );

  // 获取可用插件
  trustedIpc.handle(IpcChannel.GET_AVAILABLE_PLUGINS, /**
   * 插件设置页读取后端插件默认值和配置结构。
   *
   * Returns:
   *     Promise<{ plugins: PluginItem[]; }>: 包装为 plugins 的插件列表。
   */
  async () => {
    return await fetchAvailablePlugins();
  });
  trustedIpc.handle(IpcChannel.GET_PLUGIN_MCP_STATUSES, /** 查询会话内插件 MCP 的只读状态。 */
    async (_event, sessionId: string) => await fetchPluginMcpStatuses(sessionId));

  trustedIpc.handle(IpcChannel.GET_MCP_SERVERS, /**
   * MCP 设置页读取服务配置和运行状态。
   *
   * Returns:
   *     Promise<MCPServerView[]>: 后端服务目录。
   */
  async () => await fetchMcpServers());
  trustedIpc.handle(IpcChannel.TEST_MCP_SERVER, /**
   * MCP 设置页测试尚未保存或修改中的服务配置。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<MCPTestResult>: 服务连接测试结果。
   */
  async (_event, config: MCPServerConfig) =>
    await testMcpServer(config));
  trustedIpc.handle(IpcChannel.CREATE_MCP_SERVER, /**
   * 页面新增 MCP 服务时等待保存成功，再由包装器清除受影响的模型设置缓存。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     config (MCPServerConfig): 当前模型、动作或服务的配置。
   *
   * Returns:
   *     Promise<MCPServerView>: 服务新增结果。
   */
  async (_event, config: MCPServerConfig) =>
    await runMcpMutationWithCacheInvalidation(/**
     * 缓存失效包装器执行本次新增服务请求。
     *
     * Returns:
     *     Promise<MCPServerView>: 后端保存的新服务记录。
     */
    () => createMcpServer(config)));
  trustedIpc.handle(
    IpcChannel.UPDATE_MCP_SERVER,
    /**
     * 页面更新 MCP 服务时等待后端保存和身份处理，成功后清除设置缓存。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     serverId (string): 当前 MCP 服务的稳定编号。
     *     config (MCPServerConfig): 当前模型、动作或服务的配置。
     *
     * Returns:
     *     Promise<MCPServerView>: 服务更新结果。
     */
    async (_event, serverId: string, config: MCPServerConfig) =>
      await runMcpMutationWithCacheInvalidation(/**
       * 缓存失效包装器执行指定服务的配置更新。
       *
       * Returns:
       *     Promise<MCPServerView>: 后端返回的服务更新结果。
       */
      () => updateMcpServer(serverId, config))
  );
  trustedIpc.handle(IpcChannel.DELETE_MCP_SERVER, /**
   * 页面删除 MCP 服务时等待后端级联清理，成功后清除设置缓存。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<{ affected_sessions: string[]; }>: 服务删除结果。
   */
  async (_event, serverId: string) =>
    await runMcpMutationWithCacheInvalidation(/**
     * 缓存失效包装器执行指定服务删除。
     *
     * Returns:
     *     Promise<{ affected_sessions: string[]; }>: 后端删除结果。
     */
    () => deleteMcpServer(serverId)));
  trustedIpc.handle(IpcChannel.RECONNECT_MCP_SERVER, /**
   * 页面重连 MCP 服务时等待连接刷新，成功后清除设置缓存。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<MCPServerView>: 重连后的服务状态。
   */
  async (_event, serverId: string) =>
    await runMcpMutationWithCacheInvalidation(/**
     * 缓存失效包装器执行指定服务重连。
     *
     * Returns:
     *     Promise<MCPServerView>: 后端重连结果。
     */
    () => reconnectMcpServer(serverId)));
  trustedIpc.handle(IpcChannel.GET_MCP_SERVER_TOOLS, /**
   * 页面请求指定 MCP 服务当前可用的工具目录。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     serverId (string): 当前 MCP 服务的稳定编号。
   *
   * Returns:
   *     Promise<MCPToolInfo[]>: 该服务的工具描述列表。
   */
  async (_event, serverId: string) =>
    await fetchMcpServerTools(serverId));

  // 更新模型变换
  trustedIpc.handle(
    IpcChannel.UPDATE_MODEL_TRANSFORM,
    /**
     * 页面保存模型缩放和偏移时先确认模型存在，再更新本地配置并通知桌面刷新。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     payload (ModelTransformPayload): 已经过入口校验的桌面动作参数。
     *
     * Returns:
     *     { modelId: string; offsetX: number; offsetY: number; userScale: number; followCursor: boolean; }: 保存后的模型参数。
     */
    (_event, payload: ModelTransformPayload) => {
      const config = loadModelConfig();
      const target = config.models.find(/**
       * 按请求中的模型编号查找需要更新显示参数的记录。
       *
       * Args:
       *     item (ModelRecord): 当前遍历的配置或目录条目。
       *
       * Returns:
       *     boolean: 编号匹配时为 true。
       */
      (item) => item.id === payload.modelId);
      if (!target) {
        throw new Error("Model not found");
      }

      if (typeof payload.offsetX === "number") {
        target.offsetX = payload.offsetX;
      }
      if (typeof payload.offsetY === "number") {
        target.offsetY = payload.offsetY;
      }
      if (typeof payload.userScale === "number") {
        target.userScale = payload.userScale;
      }
      if (typeof payload.followCursor === "boolean") {
        target.followCursor = payload.followCursor;
      }

      saveModelConfig(config);
      notifyModelTransformChanged(target);

      return {
        modelId: target.id,
        offsetX: target.offsetX ?? 0,
        offsetY: target.offsetY ?? 0,
        userScale: target.userScale ?? 1,
        followCursor: target.followCursor ?? true,
      };
    }
  );

  // 预览 Live2D 导入
  trustedIpc.handle(IpcChannel.PREVIEW_LIVE2D_IMPORT, /**
   * 页面开始导入时打开目录选择器，用户选中后检查模型入口并返回预览。
   *
   * Returns:
   *     Promise<ImportPreview | null>: 导入预览；用户取消或没有窗口时为空。
   */
  async () => {
    const chooser = getSettingsWindow() ?? getMainWindow();
    if (!chooser) {
      return null;
    }

    const result = await dialog.showOpenDialog(chooser, {
      title: "选择 Live2D 模型文件夹",
      properties: ["openDirectory"],
    });

    if (result.canceled || result.filePaths.length === 0) {
      return null;
    }

    return inspectImportSource(result.filePaths[0]);
  });

  // 导入 Live2D 模型
  trustedIpc.handle(
    IpcChannel.IMPORT_LIVE2D_MODEL,
    /**
     * 页面确认导入后检查来源并复制模型资源，保存新模型记录后返回最新配置。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     payload ({ selectedPath: string; suggestedName?: string }): 已经过入口校验的桌面动作参数。
     *
     * Returns:
     *     Promise<{ id: string; name: string; sessionId: string; source: ModelSource; }>: 模型导入结果。
     */
    async (_event, payload?: { selectedPath: string; suggestedName?: string }) => {
      if (!payload?.selectedPath) {
        throw new Error("Missing import path");
      }

      const preview = inspectImportSource(payload.selectedPath);
      const modelName = sanitizeModelName(payload.suggestedName || preview.suggestedName);
      const destDir = resolveUniqueModelDir(modelName);

      copyDirectory(preview.selectedPath, destDir);

      const entryName = findModel3JsonRelativePath(destDir);
      if (!entryName) {
        fs.rmSync(destDir, { recursive: true, force: true });
        throw new Error("Model3.json not found in the selected folder");
      }

      const record = createModelRecord(modelName, destDir, entryName);

      try {
        await createEmptyChatSettings(record.sessionId);
      } catch (error) {
        fs.rmSync(destDir, { recursive: true, force: true });
        throw error;
      }

      const config = loadModelConfig();
      config.models.push(record);
      config.activeModelId = record.id;
      saveModelConfig(config);

      // 清除聊天设置缓存，确保下次获取新模型的设置
      clearChatSettingsCache();

      notifyModelChanged();

      return {
        id: record.id,
        name: record.name,
        sessionId: record.sessionId,
        source: record.source,
      };
    }
  );

  // 删除模型
  trustedIpc.handle(IpcChannel.DELETE_MODEL, /**
   * 页面删除模型时拒绝内置模型，删除自定义资源并在必要时切回内置模型。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     Promise<{ activeModelId: string; }>: 删除后的模型配置。
   */
  async (_event, modelId: string) => {
    const config = loadModelConfig();
    const target = config.models.find(/**
     * 按删除请求编号查找目标模型。
     *
     * Args:
     *     item (ModelRecord): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 当前记录编号匹配时为 true。
     */
    (item) => item.id === modelId);
    if (!target) {
      throw new Error("Model not found");
    }

    if (target.source === "builtin") {
      throw new Error("Default model cannot be deleted");
    }

    await deleteChatSettingsBySessionId(target.sessionId);
    forgetAiName(target.sessionId);

    if (target.rootDir) {
      fs.rmSync(resolveRootDirAbsolute(target.rootDir), { recursive: true, force: true });
    }

    config.models = config.models.filter(/**
     * 生成删除后的模型列表，排除目标编号。
     *
     * Args:
     *     item (ModelRecord): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 当前记录属于需保留模型时为 true。
     */
    (item) => item.id !== modelId);
    if (config.models.length === 0) {
      const { createDefaultModelConfig } = await import("./model-manager.js");
      const fallback = createDefaultModelConfig();
      saveModelConfig(fallback);
      clearChatSettingsCache();
      notifyModelChanged();
      return {
        activeModelId: fallback.activeModelId,
      };
    }

    if (config.activeModelId === modelId) {
      const builtin =
        config.models.find(/**
         * 活动模型被删除后优先寻找内置 hiyori 作为替代。
         *
         * Args:
         *     item (ModelRecord): 当前遍历的配置或目录条目。
         *
         * Returns:
         *     boolean: 当前模型为指定内置默认模型时为 true。
         */
        (item) => item.id === "builtin-hiyori") ??
        config.models.find(/**
         * 指定默认模型不存在时查找其他内置模型。
         *
         * Args:
         *     item (ModelRecord): 当前遍历的配置或目录条目。
         *
         * Returns:
         *     boolean: 当前模型来源为 builtin 时为 true。
         */
        (item) => item.source === "builtin") ??
        config.models[0];
      config.activeModelId = builtin.id;
    }

    saveModelConfig(config);
    clearChatSettingsCache();
    notifyModelChanged();

    return {
      activeModelId: config.activeModelId,
    };
  });

  // 设置激活模型
  trustedIpc.handle(IpcChannel.SET_ACTIVE_MODEL, /**
   * 页面切换活动模型时先确认编号存在，保存活动编号并清理旧会话设置缓存。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   */
  (_event, modelId: string) => {
    const config = loadModelConfig();
    if (!config.models.some(/**
     * 确认请求切换的模型存在于配置列表中。
     *
     * Args:
     *     item (ModelRecord): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 模型编号匹配时为 true。
     */
    (item) => item.id === modelId)) {
      throw new Error("Model not found");
    }

    config.activeModelId = modelId;
    saveModelConfig(config);

    // 清除聊天设置缓存
    clearChatSettingsCache();

    notifyModelChanged();

    return {
      activeModelId: config.activeModelId,
    };
  });

  // 打开设置窗口
  trustedIpc.on(IpcChannel.OPEN_SETTINGS_WINDOW, /**
   * 页面请求打开设置时只允许指定的任务页入口，其他值使用默认设置页。
   *
   * Args:
   *     _event (IpcMainEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     page (unknown): 需要打开的设置页面名称。
   */
  (_event, page: unknown) => {
    openSettingsWindow(page === "tasks" ? "tasks" : undefined);
  });

  // 最小化当前窗口
  trustedIpc.on(IpcChannel.MINIMIZE_CURRENT_WINDOW, /**
   * 可信标题栏请求最小化时只操作发送请求的窗口。
   *
   * Args:
   *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
   */
  (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    win?.minimize();
  });

  // 关闭当前窗口
  trustedIpc.on(IpcChannel.CLOSE_CURRENT_WINDOW, /**
   * 可信标题栏请求关闭时只关闭发送请求的窗口。
   *
   * Args:
   *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
   */
  (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    win?.close();
  });

  trustedIpc.handle(IpcChannel.SET_CHAT_PINNED, /**
   * 仅聊天窗口可以修改自己的置顶状态。
   * Args:
   *     event (IpcMainInvokeEvent): 可信发送窗口。
   *     pinned (unknown): 用户选择的置顶状态。
   * Returns:
   *     boolean: 已保存的状态。
   */
  (event, pinned: unknown) => {
    if (BrowserWindow.fromWebContents(event.sender) !== getChatWindow() || typeof pinned !== "boolean") throw new Error("无效的聊天置顶请求");
    return setChatPinned(pinned);
  });
  trustedIpc.on(IpcChannel.CHAT_ATTENTION, /**
   * 仅接收聊天窗口的提醒状态，拒绝其他页面操作聊天任务栏。
   * Args:
   *     event (IpcMainEvent): 可信发送窗口。
   *     value (ChatAttentionUpdate | null): 去重后的提醒变化。
   */
  (event, value: ChatAttentionUpdate | null) => {
    if (BrowserWindow.fromWebContents(event.sender) !== getChatWindow() || !value || typeof value !== "object") return;
    const update: ChatAttentionUpdate = { newMessage: value.newMessage === true, reset: value.reset === true };
    if (value.approvalId === null || (typeof value.approvalId === "string" && value.approvalId.length <= 256)) update.approvalId = value.approvalId;
    updateChatAttention(update);
  });
  trustedIpc.handle(IpcChannel.GET_ACTIVE_TASK_SUMMARY, /**
   * 退出确认前查询整个后端的活动任务数量，查询本身不启动关闭。
   */
  async () => {
    // 1. 查询超过退出确认预算时中止 HTTP 请求，页面按查询出错提示用户人工确认。
    const response = await backendFetch("/internal/tasks/active-summary", {
      signal: AbortSignal.timeout(EXIT_TASK_QUERY_TIMEOUT_MS),
    });
    if (!response.ok) throw new Error("读取后台任务状态失败");
    return response.json();
  });
  trustedIpc.handle(IpcChannel.EXIT_APPLICATION, /**
   * 聊天或设置页面确认后进入统一应用退出流程，不把普通窗口关闭当作退出。
   * Args:
   *     event (IpcMainInvokeEvent): 来源验证已通过的页面请求，仍需核对它属于聊天或设置窗口。
   */
  (event) => {
    // 1. 只接受聊天或设置窗口的明确退出操作，其他辅助窗口仍不能退出整个应用。
    const sender = BrowserWindow.fromWebContents(event.sender);
    if (!sender || (sender !== getSettingsWindow() && sender !== getChatWindow())) throw new Error("当前窗口不允许退出应用");
    // 2. 两个入口共用原有应用清理流程，由主进程关闭新业务并停止对话和任务。
    options.requestApplicationExit();
  });
  trustedIpc.on(IpcChannel.PLAY_EXPRESSION, /**
   * 核对当前模型的表情标签后交给 Live2D 页面播放。
   * Args:
   *     event (IpcMainEvent): 可信聊天页面的请求。
   *     label (unknown): 正文中的表情标签。
   */
  (event, label: unknown) => {
    if (BrowserWindow.fromWebContents(event.sender) !== getChatWindow() || typeof label !== "string") return;
    const active = getActiveModelRecord();
    if (!active.motionConfig?.some(/**
     * 只允许当前模型明确配置的表情，不接受任意动作名称。
     * Args:
     *     rule (MotionConfig): 已保存的动作规则。
     * Returns:
     *     boolean: 规则标签与请求一致时为 true。
     */
    (rule) => rule.setting === "expression" && rule.label === label)) return;
    getMainWindow()?.webContents.send(IpcChannel.EXPRESSION_REQUEST, label);
  });
  trustedIpc.on(IpcChannel.MODEL_ERROR, /**
   * 只将角色页面的加载错误转发给聊天窗口。
   * Args:
   *     event (IpcMainEvent): 角色窗口发送请求。
   *     message (unknown): 模型错误说明。
   */
  (event, message: unknown) => {
    if (BrowserWindow.fromWebContents(event.sender) === getMainWindow() && typeof message === "string") deliverModelError(message.slice(0, 2000));
  });

  // 打开图片预览窗口
  trustedIpc.on(IpcChannel.OPEN_IMAGE_PREVIEW, /**
   * 聊天页点击历史图片后打开受限图片预览窗口。
   *
   * Args:
   *     _event (IpcMainEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     imageSrc (string): 要预览的图片地址。
   */
  (_event, imageSrc: string) => {
    openImagePreviewWindow(imageSrc);
  });

  // 选择图片
  trustedIpc.handle(IpcChannel.SELECT_IMAGES, /**
   * 聊天页请求附件时打开图片多选器，读取并准备用户选中的图片。
   *
   * Returns:
   *     Promise<{ path: string; dataUrl: string; }[] | null>: 图片选择结果，取消时为空。
   */
  async (event) => {
    const chooser = BrowserWindow.fromWebContents(event.sender);
    if (!chooser) {
      return null;
    }

    const result = await dialog.showOpenDialog(chooser, {
      title: "选择图片",
      properties: ["openFile", "multiSelections"],
      filters: [{ name: "图片", extensions: ["jpg", "jpeg", "png", "gif", "bmp", "webp"] }],
    });

    if (result.canceled || result.filePaths.length === 0) {
      return null;
    }

    // 读取文件并返回 data URL
    const images: Array<{ path: string; dataUrl: string }> = [];
    for (const filePath of result.filePaths) {
      try {
        const buffer = fs.readFileSync(filePath);
        const base64 = buffer.toString("base64");
        const ext = path.extname(filePath).toLowerCase().slice(1);
        const mimeType = ext === "jpg" ? "jpeg" : ext;
        images.push({
          path: filePath,
          dataUrl: `data:image/${mimeType};base64,${base64}`,
        });
      } catch (error) {
        appendFrontendLog("warn", "image-picker", `读取图片失败: ${filePath}`, error);
      }
    }

    return images;
  });

  // 聊天请求
  trustedIpc.handle(IpcChannel.GET_BACKGROUND_TASKS, /**
   * 任务页按会话读取已保存后台任务记录，HTTP 失败时明确抛错。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<unknown[]>: 当前会话任务列表。
   */
  async (_event, sessionId: string) => {
    const response = await backendFetch(`/tasks/${encodeURIComponent(sessionId)}/records`);
    if (!response.ok) throw new Error(`读取后台任务失败: ${response.status}`);
    const data = await response.json() as { tasks: unknown[] };
    return data.tasks;
  });
  trustedIpc.handle(IpcChannel.GET_BACKGROUND_TASK_OUTPUT, /**
   * 任务页按偏移读取指定任务输出，不把持续输出写进聊天正文。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *     taskId (string): 当前后台任务的编号。
   *     offset (number): 后台任务输出的读取起点。
   *
   * Returns:
   *     Promise<any>: 本次输出内容及继续读取位置。
   */
  async (
    _event, sessionId: string, taskId: string, offset: number,
  ) => {
    const response = await backendFetch(
      `/tasks/${encodeURIComponent(sessionId)}/${encodeURIComponent(taskId)}/output?offset=${offset}`,
    );
    if (!response.ok) throw new Error(`读取任务输出失败: ${response.status}`);
    return await response.json();
  });

  // 聊天请求
  trustedIpc.handle(IpcChannel.GET_CHANNEL_SUBSCRIPTIONS, /**
   * 渠道设置页读取当前会话的渠道订阅状态。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *
   * Returns:
   *     Promise<unknown[]>: 渠道订阅记录列表。
   */
  async (_event, sessionId: string) => {
    const response = await backendFetch(`/agent/channels/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error(`读取渠道订阅失败: ${response.status}`);
    const data = await response.json() as { channels: unknown[] };
    return data.channels;
  });
  trustedIpc.handle(IpcChannel.SET_CHANNEL_SUBSCRIPTION, /**
   * 渠道设置页保存一个渠道的启用状态，等待后端更新后返回结果。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *     channel (string): 主进程与页面约定的 IPC 事件名称。
   *     enabled (boolean): 是否启用当前界面选项。
   *
   * Returns:
   *     Promise<unknown[]>: 保存后的渠道订阅状态。
   */
  async (
    _event, sessionId: string, channel: string, enabled: boolean,
  ) => {
    const response = await backendFetch(
      `/agent/channels/${encodeURIComponent(sessionId)}/${encodeURIComponent(channel)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    );
    if (!response.ok) throw new Error(`更新渠道订阅失败: ${response.status}`);
    const data = await response.json() as { channels: unknown[] };
    return data.channels;
  });

  // 聊天请求
  trustedIpc.handle(IpcChannel.INTERACTION_COMMAND, /**
   * 桌面页提交统一命令时先禁止冒用其他渠道，再交给后端处理。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     command (ChannelCommand): 要启动的可执行程序。
   *
   * Returns:
   *     Promise<CommandReceipt>: 后端命令回执。
   */
  async (_event, command: ChannelCommand) => {
    if (command.origin_channel !== DESKTOP_CHANNEL) throw new Error("桌面入口不能冒用其他渠道");
    return await submitInteractionCommand(command);
  });
  trustedIpc.handle(IpcChannel.SUBSCRIBE_DESKTOP_SESSION, /**
   * 桌面页订阅会话时建立或复用事件连接，新页面请求刷新则等待新快照。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     sessionId (string): 当前操作所属的会话编号。
   *     options ({ refreshSnapshot?: boolean }): 调用方提供的配置或运行选项。
   */
  async (_event, sessionId: string, options?: { refreshSnapshot?: boolean }) => {
    await subscribeDesktopSession(sessionId, { refreshSnapshot: options?.refreshSnapshot === true });
  });

  // 获取前端设置
  trustedIpc.handle(IpcChannel.GET_FRONTEND_SETTINGS, /**
   * 页面读取前端配置时使用本地 JSON，文件缺失或读取失败则返回默认截图隐藏设置。
   *
   * Returns:
   *     FrontendSettings: 当前前端设置或默认值。
   */
  () => {
    return readFrontendSettings();
  });

  /** 可信设置页读取已保存后端配置，退出门禁由统一 IPC 入口检查。 */
  trustedIpc.handle(IpcChannel.GET_DESKTOP_SETTINGS, /** 返回后端 YAML 配置。 */
  () => getDesktopSettings());
  /** 可信设置页只提交后端定义的完整桌面设置。 */
  trustedIpc.handle(IpcChannel.UPDATE_DESKTOP_SETTINGS, /** 保存配置并返回后端确认值。 */
  (_event, settings: DesktopSettings) => updateDesktopSettings(settings));

  // 获取模型动作配置
  trustedIpc.handle(IpcChannel.GET_MOTION_CONFIG, /**
   * 动作设置页按模型编号读取保存的动作规则。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     modelId (string): 当前 Live2D 模型的稳定编号。
   *
   * Returns:
   *     MotionConfig[]: 该模型动作列表，模型或配置不存在时为空列表。
   */
  (_event, modelId: string) => {
    const config = loadModelConfig();
    const model = config.models.find(/**
     * 读取动作配置时按请求编号查找模型。
     *
     * Args:
     *     item (ModelRecord): 当前遍历的配置或目录条目。
     *
     * Returns:
     *     boolean: 模型编号匹配时为 true。
     */
    (item) => item.id === modelId);
    return model?.motionConfig ?? [];
  });

  // 更新模型动作配置
  trustedIpc.handle(
    IpcChannel.UPDATE_MODEL_MOTION_CONFIG,
    /**
     * 动作设置页保存规则时先确认模型存在，再写入配置并通知桌面更新。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     payload ({ modelId: string; motionConfig: import("./types.js").MotionConfig[] }): 已经过入口校验的桌面动作参数。
     */
    (_event, payload: { modelId: string; motionConfig: import("./types.js").MotionConfig[] }) => {
      const config = loadModelConfig();
      const target = config.models.find(/**
       * 动作规则保存前按请求编号查找目标模型。
       *
       * Args:
       *     item (ModelRecord): 当前遍历的配置或目录条目。
       *
       * Returns:
       *     boolean: 模型编号匹配时为 true。
       */
      (item) => item.id === payload.modelId);
      if (!target) {
        throw new Error("Model not found");
      }

      target.motionConfig = payload.motionConfig;
      saveModelConfig(config);

      // 通知主窗口动作配置已更新
      for (const mainWindow of [getMainWindow(), getChatWindow()]) {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send(IpcChannel.MOTION_CONFIG_CHANGED, {
            modelId: payload.modelId,
            motionConfig: payload.motionConfig,
          });
        }
      }
    }
  );

  // 播放动作（转发到主窗口）
  trustedIpc.on(IpcChannel.PLAY_MOTION, /**
   * 设置页预览动作时，只向存活的桌面主窗口转发动作名称。
   *
   * Args:
   *     _event (IpcMainEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     motionName (string): 需要播放或保存配置的动作名称。
   */
  (_event, motionName: string) => {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send(IpcChannel.PLAY_MOTION_REQUEST, motionName);
    }
  });
};

/**
 * 清理聊天设置缓存的辅助函数
 */
export { clearChatSettingsCache };
