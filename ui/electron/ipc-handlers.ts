/**
 * IPC 处理器注册
 */

import { BrowserWindow, dialog, screen, clipboard } from "electron";
import fs from "node:fs";
import path from "node:path";

import { FRONTEND_SETTINGS_PATH } from "./config.js";
import { backendFetch } from "./backend-client.js";
import { submitInteractionCommand, subscribeDesktopSession } from "./interaction-adapter.js";
import type { ChannelCommand } from "../shared-types.js";
import { DESKTOP_CHANNEL } from "../shared-types.js";
import type {
  ChatSettingsData,
  ModelTransformPayload,
  ToolItem,
  ModelChangedPayload,
  ModelTransformChangedPayload,
  ScreenActionRequest,
  ScreenActionResult,
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
} from "./chat-settings.js";
import { getMainWindow, getSettingsWindow, getTrustedRendererPolicy, openSettingsWindow, openImagePreviewWindow } from "./window-manager.js";
import { createTrustedIpcRegistrar } from "./ipc-security.js";
import { resolveScreenActionPoint } from "./screen-action-coordinates.js";
import { pasteScreenActionText } from "./screen-action-input.js";
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

  // 通知主窗口
  const mainWindow = getMainWindow();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("desktop-pet:model-changed", payload);
  }

  // 通知设置窗口
  const settingsWindow = getSettingsWindow();
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.webContents.send("desktop-pet:model-changed", payload);
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

  mainWindow.webContents.send("desktop-pet:model-transform-changed", payload);
};

/**
 * 按已校验参数执行桌面鼠标及文字输入，再等待界面稳定。
 *
 * Args:
 *     payload (ScreenActionRequest): 已经过入口校验的桌面动作参数。
 *
 * Returns:
 *     Promise<ScreenActionResult>: 桌面动作执行结束后的完成结果。
 */
export const performScreenAction = async (payload: ScreenActionRequest): Promise<ScreenActionResult> => {
  const coordinates = payload.coordinates;
  if (!coordinates || typeof coordinates.x_ratio !== "number" || typeof coordinates.y_ratio !== "number") {
    return { executed: false, error: "缺少有效的屏幕坐标" };
  }

  try {
    const nut = await import("@nut-tree-fork/nut-js");
    const display = screen.getPrimaryDisplay();
    const bounds = display.bounds;
    const { x, y } = resolveScreenActionPoint(coordinates, bounds, display.scaleFactor);
    const point = new nut.Point(x, y);

    await nut.mouse.setPosition(point);
    if (payload.operation === "click") {
      await nut.mouse.click(nut.Button.LEFT);
    } else if (payload.operation === "double") {
      await nut.mouse.doubleClick(nut.Button.LEFT);
    } else if (payload.operation === "right") {
      await nut.mouse.click(nut.Button.RIGHT);
    } else if (payload.operation === "scroll") {
      if (payload.scroll_direction === "up") {
        await nut.mouse.scrollUp(6);
      } else if (payload.scroll_direction === "down") {
        await nut.mouse.scrollDown(6);
      } else {
        return { executed: false, error: "滚动操作缺少方向" };
      }
    } else {
      return { executed: false, error: `不支持的操作方式: ${payload.operation}` };
    }

    if (payload.text) {
      await pasteScreenActionText({
        text: payload.text,
        pressEnter: payload.press_enter,
        clipboard: {
          /**
           * 通过 Electron 读取当前剪贴板纯文本，供执行前保存。
           *
           * Returns:
           *     string: 剪贴板原文本。
           */
          readText: () => clipboard.readText(),
          /**
           * 临时向剪贴板写入需要粘贴的文本，或恢复原纯文本。
           *
           * Args:
           *     text (string): 需要显示、输入或转换的文本。
           */
          writeText: (text) => clipboard.writeText(text),
          /**
           * 读取剪贴板所有格式，供判断是否可以可靠恢复。
           *
           * Returns:
           *     string[]: 当前剪贴板格式名称列表。
           */
          availableFormats: () => clipboard.availableFormats(),
          /**
           * 读取剪贴板 HTML，供动作结束后恢复富文本。
           *
           * Returns:
           *     string: 原 HTML 内容。
           */
          readHTML: () => clipboard.readHTML(),
          /**
           * 读取剪贴板 RTF，供动作结束后恢复富文本。
           *
           * Returns:
           *     string: 原 RTF 内容。
           */
          readRTF: () => clipboard.readRTF(),
          /**
           * 读取剪贴板图片，供动作结束后恢复。
           *
           * Returns:
           *     NativeImage: 原剪贴板图片。
           */
          readImage: () => clipboard.readImage(),
          /**
           * 将动作前保存的文字、富文本和图片一并恢复到剪贴板。
           *
           * Args:
           *     data (ClipboardWriteData): 调用方提供的页面或接口数据。
           */
          write: (data) => clipboard.write(data as Electron.Data),
        },
        keyboard: nut.keyboard,
        keys: {
          leftControl: nut.Key.LeftControl,
          v: nut.Key.V,
          enter: nut.Key.Enter,
        },
      });
    }

    return { executed: true };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    appendFrontendLog("error", "control-screen", "屏幕操作失败", error);
    return { executed: false, error: message };
  }
};

/**
 * 注册受信任 Renderer 的 IPC 入口，将运行时命令、事件和任务查询接入主进程。
 */
export const registerIpcHandlers = (): void => {
  const trustedIpc = createTrustedIpcRegistrar(getTrustedRendererPolicy());
  // 鼠标穿透控制
  trustedIpc.on("desktop-pet:set-mouse-passthrough", /**
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
  trustedIpc.on("desktop-pet:set-pointer-interactive", /**
   * 可信页面更新模型可交互状态时，反向设置窗口穿透，允许模型区域接收鼠标。
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

    win.setIgnoreMouseEvents(!Boolean(enabled), { forward: true });
  });

  // 获取当前激活的模型
  trustedIpc.handle("desktop-pet:get-active-model", /**
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
  trustedIpc.handle("desktop-pet:get-model-config", /**
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

  // 获取聊天设置
  trustedIpc.handle("desktop-pet:get-chat-settings", /**
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
  trustedIpc.handle("desktop-pet:get-latest-ai-message", /**
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
    "desktop-pet:get-chat-history",
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
    "desktop-pet:get-chat-history-last-n",
    /**
     * 页面恢复聊天时请求指定会话最近若干条历史。
     *
     * Args:
     *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
     *     sessionId (string): 当前操作所属的会话编号。
     *     n (number): 最多读取的历史消息数量。
     *
     * Returns:
     *     Promise<ChatHistoryItem[]>: 最近可见消息列表。
     */
    async (_event, sessionId: string, n: number) => {
      return await fetchChatHistoryLastN(sessionId, n);
    }
  );

  // 更新聊天设置
  trustedIpc.handle("desktop-pet:update-chat-settings", /**
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
    return await updateChatSettings(payload);
  });

  // 获取可用工具
  trustedIpc.handle("desktop-pet:get-available-tools", /**
   * 页面请求工具目录时读取当前本地工具名称和说明。
   *
   * Returns:
   *     Promise<{ tools: ToolItem[]; }>: 包装为 tools 的工具列表。
   */
  async () => {
    const tools = await fetchAvailableTools();
    return { tools };
  });
  trustedIpc.handle("desktop-pet:get-tool-permissions", /**
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
    "desktop-pet:update-tool-permissions",
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
  trustedIpc.handle("desktop-pet:get-available-plugins", /**
   * 插件设置页读取后端插件默认值和配置结构。
   *
   * Returns:
   *     Promise<{ plugins: PluginItem[]; }>: 包装为 plugins 的插件列表。
   */
  async () => {
    const plugins = await fetchAvailablePlugins();
    return { plugins };
  });

  trustedIpc.handle("desktop-pet:get-mcp-servers", /**
   * MCP 设置页读取服务配置和运行状态。
   *
   * Returns:
   *     Promise<MCPServerView[]>: 后端服务目录。
   */
  async () => await fetchMcpServers());
  trustedIpc.handle("desktop-pet:test-mcp-server", /**
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
  trustedIpc.handle("desktop-pet:create-mcp-server", /**
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
    "desktop-pet:update-mcp-server",
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
  trustedIpc.handle("desktop-pet:delete-mcp-server", /**
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
  trustedIpc.handle("desktop-pet:reconnect-mcp-server", /**
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
  trustedIpc.handle("desktop-pet:get-mcp-server-tools", /**
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
    "desktop-pet:update-model-transform",
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
  trustedIpc.handle("desktop-pet:preview-live2d-import", /**
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
    "desktop-pet:import-live2d-model",
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
  trustedIpc.handle("desktop-pet:delete-model", /**
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
  trustedIpc.handle("desktop-pet:set-active-model", /**
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
  trustedIpc.on("desktop-pet:open-settings-window", /**
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
  trustedIpc.on("desktop-pet:minimize-current-window", /**
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
  trustedIpc.on("desktop-pet:close-current-window", /**
   * 可信标题栏请求关闭时只关闭发送请求的窗口。
   *
   * Args:
   *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
   */
  (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    win?.close();
  });

  // 打开图片预览窗口
  trustedIpc.on("desktop-pet:open-image-preview", /**
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
  trustedIpc.handle("desktop-pet:select-images", /**
   * 聊天页请求附件时打开图片多选器，读取并准备用户选中的图片。
   *
   * Returns:
   *     Promise<{ path: string; dataUrl: string; }[] | null>: 图片选择结果，取消时为空。
   */
  async () => {
    const chooser = getMainWindow();
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
  trustedIpc.handle("desktop-pet:get-background-tasks", /**
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
  trustedIpc.handle("desktop-pet:get-background-task-output", /**
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
  trustedIpc.handle("desktop-pet:get-channel-subscriptions", /**
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
  trustedIpc.handle("desktop-pet:set-channel-subscription", /**
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
  trustedIpc.handle("desktop-pet:interaction-command", /**
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
  trustedIpc.handle("desktop-pet:subscribe-desktop-session", /**
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
  trustedIpc.handle("desktop-pet:get-frontend-settings", /**
   * 页面读取前端配置时使用本地 JSON，文件缺失或读取失败则返回默认截图隐藏设置。
   *
   * Returns:
   *     FrontendSettings: 当前前端设置或默认值。
   */
  () => {
    const defaultSettings: FrontendSettings = {
      hide_on_screenshot: true,
    };

    try {
      if (!fs.existsSync(FRONTEND_SETTINGS_PATH)) {
        return defaultSettings;
      }
      const content = fs.readFileSync(FRONTEND_SETTINGS_PATH, "utf-8");
      const settings = JSON.parse(content) as Partial<FrontendSettings>;
      return { ...defaultSettings, ...settings };
    } catch {
      return defaultSettings;
    }
  });

  // 更新前端设置
  trustedIpc.handle("desktop-pet:update-frontend-settings", /**
   * 页面修改前端配置时合并原设置并写入本地文件。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     settings (Partial<FrontendSettings>): 当前会话或桌面界面的设置。
   *
   * Returns:
   *     { hide_on_screenshot: boolean; }: 保存后的前端设置。
   */
  (_event, settings: Partial<FrontendSettings>) => {
    let current: FrontendSettings;
    try {
      if (fs.existsSync(FRONTEND_SETTINGS_PATH)) {
        const content = fs.readFileSync(FRONTEND_SETTINGS_PATH, "utf-8");
        current = JSON.parse(content) as FrontendSettings;
      } else {
        current = { hide_on_screenshot: true };
      }
    } catch {
      current = { hide_on_screenshot: true };
    }

    const updated = { ...current, ...settings };
    fs.mkdirSync(path.dirname(FRONTEND_SETTINGS_PATH), { recursive: true });
    fs.writeFileSync(FRONTEND_SETTINGS_PATH, JSON.stringify(updated, null, 2), "utf-8");
    return updated;
  });

  // 获取模型动作配置
  trustedIpc.handle("desktop-pet:get-motion-config", /**
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
    "desktop-pet:update-model-motion-config",
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
      const mainWindow = getMainWindow();
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("desktop-pet:motion-config-changed", {
          modelId: payload.modelId,
          motionConfig: payload.motionConfig,
        });
      }
    }
  );

  // 播放动作（转发到主窗口）
  trustedIpc.on("desktop-pet:play-motion", /**
   * 设置页预览动作时，只向存活的桌面主窗口转发动作名称。
   *
   * Args:
   *     _event (IpcMainEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     motionName (string): 需要播放或保存配置的动作名称。
   */
  (_event, motionName: string) => {
    const mainWindow = getMainWindow();
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send("desktop-pet:play-motion-request", motionName);
    }
  });
};

/**
 * 清理聊天设置缓存的辅助函数
 */
export { clearChatSettingsCache };
