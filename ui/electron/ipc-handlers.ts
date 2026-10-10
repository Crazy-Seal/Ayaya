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
          readText: () => clipboard.readText(),
          writeText: (text) => clipboard.writeText(text),
          availableFormats: () => clipboard.availableFormats(),
          readHTML: () => clipboard.readHTML(),
          readRTF: () => clipboard.readRTF(),
          readImage: () => clipboard.readImage(),
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
  trustedIpc.on("desktop-pet:set-mouse-passthrough", (event, enabled: boolean) => {
    const win = BrowserWindow.fromWebContents(event.sender) ?? getMainWindow();
    if (!win) {
      return;
    }

    win.setIgnoreMouseEvents(Boolean(enabled), { forward: true });
  });

  // 指针交互控制
  trustedIpc.on("desktop-pet:set-pointer-interactive", (event, enabled: boolean) => {
    const win = BrowserWindow.fromWebContents(event.sender) ?? getMainWindow();
    if (!win) {
      return;
    }

    win.setIgnoreMouseEvents(!Boolean(enabled), { forward: true });
  });

  // 获取当前激活的模型
  trustedIpc.handle("desktop-pet:get-active-model", () => {
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
  trustedIpc.handle("desktop-pet:get-model-config", () => {
    const config = loadModelConfig();
    return {
      activeModelId: config.activeModelId,
      models: config.models.map((item) => ({
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
  trustedIpc.handle("desktop-pet:get-chat-settings", async () => {
    const settings = await ensureChatSettingsLoaded();
    return settings;
  });

  // 获取最新 AI 消息
  trustedIpc.handle("desktop-pet:get-latest-ai-message", async (_event, sessionId?: string) => {
    const resolvedSessionId = sessionId || getActiveModelRecord().sessionId;
    return {
      sessionId: resolvedSessionId,
      latestAiMessage: await fetchLatestAiMessageBySessionId(resolvedSessionId),
    };
  });

  // 获取聊天历史
  trustedIpc.handle(
    "desktop-pet:get-chat-history",
    async (_event, sessionId: string, start: number, limit: number) => {
      return await fetchChatHistoryPageBySessionId(sessionId, start, limit);
    }
  );

  // 获取最后 N 条聊天历史
  trustedIpc.handle(
    "desktop-pet:get-chat-history-last-n",
    async (_event, sessionId: string, n: number) => {
      return await fetchChatHistoryLastN(sessionId, n);
    }
  );

  // 更新聊天设置
  trustedIpc.handle("desktop-pet:update-chat-settings", async (_event, payload: ChatSettingsData) => {
    return await updateChatSettings(payload);
  });

  // 获取可用工具
  trustedIpc.handle("desktop-pet:get-available-tools", async () => {
    const tools = await fetchAvailableTools();
    return { tools };
  });
  trustedIpc.handle("desktop-pet:get-tool-permissions", async (_event, sessionId: string) =>
    await fetchToolPermissions(sessionId));
  trustedIpc.handle(
    "desktop-pet:update-tool-permissions",
    async (_event, sessionId: string, permissions: Record<string, ToolPolicy>) =>
      await saveToolPermissions(sessionId, permissions)
  );

  // 获取可用插件
  trustedIpc.handle("desktop-pet:get-available-plugins", async () => {
    const plugins = await fetchAvailablePlugins();
    return { plugins };
  });

  trustedIpc.handle("desktop-pet:get-mcp-servers", async () => await fetchMcpServers());
  trustedIpc.handle("desktop-pet:test-mcp-server", async (_event, config: MCPServerConfig) =>
    await testMcpServer(config));
  trustedIpc.handle("desktop-pet:create-mcp-server", async (_event, config: MCPServerConfig) =>
    await runMcpMutationWithCacheInvalidation(() => createMcpServer(config)));
  trustedIpc.handle(
    "desktop-pet:update-mcp-server",
    async (_event, serverId: string, config: MCPServerConfig) =>
      await runMcpMutationWithCacheInvalidation(() => updateMcpServer(serverId, config))
  );
  trustedIpc.handle("desktop-pet:delete-mcp-server", async (_event, serverId: string) =>
    await runMcpMutationWithCacheInvalidation(() => deleteMcpServer(serverId)));
  trustedIpc.handle("desktop-pet:reconnect-mcp-server", async (_event, serverId: string) =>
    await runMcpMutationWithCacheInvalidation(() => reconnectMcpServer(serverId)));
  trustedIpc.handle("desktop-pet:get-mcp-server-tools", async (_event, serverId: string) =>
    await fetchMcpServerTools(serverId));

  // 更新模型变换
  trustedIpc.handle(
    "desktop-pet:update-model-transform",
    (_event, payload: ModelTransformPayload) => {
      const config = loadModelConfig();
      const target = config.models.find((item) => item.id === payload.modelId);
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
  trustedIpc.handle("desktop-pet:preview-live2d-import", async () => {
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
  trustedIpc.handle("desktop-pet:delete-model", async (_event, modelId: string) => {
    const config = loadModelConfig();
    const target = config.models.find((item) => item.id === modelId);
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

    config.models = config.models.filter((item) => item.id !== modelId);
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
        config.models.find((item) => item.id === "builtin-hiyori") ??
        config.models.find((item) => item.source === "builtin") ??
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
  trustedIpc.handle("desktop-pet:set-active-model", (_event, modelId: string) => {
    const config = loadModelConfig();
    if (!config.models.some((item) => item.id === modelId)) {
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
   * 校验快捷入口的页面参数，其他值保持原有普通设置行为。
   *
   * Args:
   *     _event (IpcMainEvent): 已通过来源校验的 IPC 事件。
   *     page (unknown): Renderer 提供的目标页面，仅允许任务页。
   */
  (_event, page: unknown) => {
    openSettingsWindow(page === "tasks" ? "tasks" : undefined);
  });

  // 最小化当前窗口
  trustedIpc.on("desktop-pet:minimize-current-window", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    win?.minimize();
  });

  // 关闭当前窗口
  trustedIpc.on("desktop-pet:close-current-window", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    win?.close();
  });

  // 打开图片预览窗口
  trustedIpc.on("desktop-pet:open-image-preview", (_event, imageSrc: string) => {
    openImagePreviewWindow(imageSrc);
  });

  // 选择图片
  trustedIpc.handle("desktop-pet:select-images", async () => {
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
   * 读取任务记录并解包 tasks 数组。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<unknown[]>: 当前查询、回调或视图操作的结果。
   */
  async (_event, sessionId: string) => {
    const response = await backendFetch(`/tasks/${encodeURIComponent(sessionId)}/records`);
    if (!response.ok) throw new Error(`读取后台任务失败: ${response.status}`);
    const data = await response.json() as { tasks: unknown[] };
    return data.tasks;
  });
  trustedIpc.handle("desktop-pet:get-background-task-output", /**
   * 按游标读取任务输出页。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     taskId (string): 受管进程任务标识。
   *     offset (number): 完整输出文件的字节游标。
   *
   * Returns:
   *     Promise<any>: 当前查询、回调或视图操作的结果。
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
   * 读取订阅配置并解包 channels 数组。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     sessionId (string): 共享 Ayaya 会话标识。
   *
   * Returns:
   *     Promise<unknown[]>: 当前查询、回调或视图操作的结果。
   */
  async (_event, sessionId: string) => {
    const response = await backendFetch(`/agent/channels/${encodeURIComponent(sessionId)}`);
    if (!response.ok) throw new Error(`读取渠道订阅失败: ${response.status}`);
    const data = await response.json() as { channels: unknown[] };
    return data.channels;
  });
  trustedIpc.handle("desktop-pet:set-channel-subscription", /**
   * 保存可选订阅并返回更新列表。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     channel (string): 已注册渠道标识。
   *     enabled (boolean): 该渠道是否参与此会话的双向互通。
   *
   * Returns:
   *     Promise<unknown[]>: 当前查询、回调或视图操作的结果。
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
   * 验证桌面来源后提交统一命令。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     command (ChannelCommand): 带桌面来源的统一入站命令。
   *
   * Returns:
   *     Promise<CommandReceipt>: 当前查询、回调或视图操作的结果。
   */
  async (_event, command: ChannelCommand) => {
    if (command.origin_channel !== DESKTOP_CHANNEL) throw new Error("桌面入口不能冒用其他渠道");
    return await submitInteractionCommand(command);
  });
  trustedIpc.handle("desktop-pet:subscribe-desktop-session", /**
   * 等待桌面会话的首次快照。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): 已由主进程信任校验的 IPC 事件。
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     options ({ refreshSnapshot?: boolean } | undefined): 新页面要求重建订阅并取得新快照。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  async (_event, sessionId: string, options?: { refreshSnapshot?: boolean }) => {
    await subscribeDesktopSession(sessionId, { refreshSnapshot: options?.refreshSnapshot === true });
  });

  // 获取前端设置
  trustedIpc.handle("desktop-pet:get-frontend-settings", () => {
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
  trustedIpc.handle("desktop-pet:update-frontend-settings", (_event, settings: Partial<FrontendSettings>) => {
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
  trustedIpc.handle("desktop-pet:get-motion-config", (_event, modelId: string) => {
    const config = loadModelConfig();
    const model = config.models.find((item) => item.id === modelId);
    return model?.motionConfig ?? [];
  });

  // 更新模型动作配置
  trustedIpc.handle(
    "desktop-pet:update-model-motion-config",
    (_event, payload: { modelId: string; motionConfig: import("./types.js").MotionConfig[] }) => {
      const config = loadModelConfig();
      const target = config.models.find((item) => item.id === payload.modelId);
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
  trustedIpc.on("desktop-pet:play-motion", (_event, motionName: string) => {
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
