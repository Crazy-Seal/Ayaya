/**
 * Electron 窗口管理
 */

import { app, BrowserWindow, screen } from "electron";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  WINDOW_WIDTH,
  WINDOW_HEIGHT,
  SETTINGS_WIDTH,
  SETTINGS_HEIGHT,
  LOG_WINDOW_WIDTH,
  LOG_WINDOW_HEIGHT,
  LOG_WINDOW_MIN_WIDTH,
  LOG_WINDOW_MIN_HEIGHT,
} from "./config.js";
import type { LogBatch, ChatAttentionUpdate, WindowGesture } from "../shared-types.js";
import { WindowGestureController } from "./window-gesture.js";
import { applicationOperations } from "./application-operations.js";
import { appendFrontendLog, captureRendererLogs } from "./logging/app-logger.js";
import {
  createTrustedRendererPolicy,
  describeRendererUrl,
  isTrustedRendererUrl,
} from "./renderer-security.js";
import { fitChatBounds, stabilizeChatBounds } from "./chat-window-state.js";
import { readFrontendSettings, saveFrontendSettings } from "./frontend-settings-store.js";
import { installNavigationGuards } from "./navigation-guards.js";
import { updateDesktopWindowReferences } from "./desktop-window-runtime.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const productionIndexPath = path.resolve(__dirname, "../../dist/index.html");
const productionSettingsPath = path.resolve(__dirname, "../../dist/settings.html");
const productionChatPath = path.resolve(__dirname, "../../dist/chat.html");
const productionPreviewPath = path.resolve(__dirname, "../../dist/image-preview.html");
const productionLogsPath = path.resolve(__dirname, "../../dist/logs.html");
const rendererPolicy = createTrustedRendererPolicy({
  devServerUrl: process.env.VITE_DEV_SERVER_URL,
  productionEntryUrls: [
    pathToFileURL(productionIndexPath).href,
    pathToFileURL(productionSettingsPath).href,
    pathToFileURL(productionLogsPath).href,
    pathToFileURL(productionChatPath).href,
    pathToFileURL(productionPreviewPath).href,
  ],
});

/**
 * 取得当前开发或生产环境允许加载的应用页面规则。
 *
 * Returns:
 *     TrustedRendererPolicy: 应用窗口共用的可信来源配置。
 */
export const getTrustedRendererPolicy = () => rendererPolicy;

/**
 * 记录应用窗口被阻止的导航类型和目标来源。
 *
 * Args:
 *     kind (string): 被阻止的导航类别。
 *     targetUrl (string): 本次导航准备访问的目标地址。
 */
const logBlockedNavigation = (kind: string, targetUrl: string): void => {
  appendFrontendLog(
    "warn",
    "renderer-security",
    `拒绝窗口${kind}: target=${describeRendererUrl(targetUrl)}`,
  );
};

/**
 * 为新应用窗口接入可信导航限制和渲染日志采集。
 *
 * Args:
 *     win (BrowserWindow): 需要保护来源与导航的应用窗口。
 *     scope (string): 日志所属模块或功能名称。
 */
const protectApplicationWindow = (win: BrowserWindow, scope: string): void => {
  installNavigationGuards(
    win.webContents,
    /**
     * 应用窗口导航前使用当前可信页面规则检查目标地址。
     *
     * Args:
     *     targetUrl (string): 本次导航准备访问的目标地址。
     *
     * Returns:
     *     boolean: 目标属于允许的应用页面时为 true。
     */
    (targetUrl) => isTrustedRendererUrl(targetUrl, rendererPolicy),
    logBlockedNavigation
  );
  captureRendererLogs(win, scope);
};

/**
 * 主窗口引用
 */
let mainWindow: BrowserWindow | null = null;

/**
 * 设置窗口引用
 */
let settingsWindow: BrowserWindow | null = null;

/** 日志控制台窗口引用。 */
let logWindow: BrowserWindow | null = null;

/**
 * 图片预览窗口引用
 */
let imagePreviewWindow: BrowserWindow | null = null;

/** 独立聊天窗口和窗口提醒状态，仅在主进程内保存。 */
let chatWindow: BrowserWindow | null = null;
/** 只登记聊天与日志窗口，IPC 发送者只能操作自己的控制器。 */
const windowGestures = new Map<BrowserWindow, WindowGestureController>();
const activeWindowGestures = new Set<BrowserWindow>();
let requestedPetInteractive = false;
let appliedPetInteractive = false;
let unread = false;
let approvalId: string | null = null;
let modelError: string | null = null;

/**
 * 返回独立聊天窗口，供事件分发与屏幕操作共用。
 * Returns:
 *     BrowserWindow | null: 当前聊天窗口实例。
 */
export const getChatWindow = (): BrowserWindow | null => chatWindow;

/** 按角色命中结果和应用窗口拖动状态切换穿透，仅在实际状态改变时调用系统。 */
const applyPetPointerState = (): void => {
  const enabled = requestedPetInteractive && activeWindowGestures.size === 0 && !applicationOperations.isShuttingDown();
  if (mainWindow && !mainWindow.isDestroyed() && enabled !== appliedPetInteractive) {
    mainWindow.setIgnoreMouseEvents(!enabled, { forward: true });
    appliedPetInteractive = enabled;
  }
};

/**
 * 角色页面上报鼠标命中时保留最新结果；聊天或日志拖动期间始终穿透，避免切换透明窗口样式。
 * Args:
 *     enabled (boolean): 鼠标是否命中当前角色像素。
 */
export const setPetPointerInteractive = (enabled: boolean): void => {
  requestedPetInteractive = enabled; applyPetPointerState();
};

/**
 * 可信应用页面捕获鼠标后，只开始发送者所属窗口的移动或缩放。
 * Args:
 *     win (BrowserWindow): 主进程按 IPC 发送者取得的窗口。
 *     kind (WindowGesture): 已校验的操作方向。
 */
export const beginWindowGesture = (win: BrowserWindow, kind: WindowGesture): void => {
  if (!applicationOperations.isShuttingDown()) windowGestures.get(win)?.begin(kind);
};

/**
 * 页面松开鼠标后停止自己的采样，保留最后一次按住期间读取的位置。
 * Args:
 *     win (BrowserWindow): 主进程按 IPC 发送者取得的窗口。
 */
export const endWindowGesture = (win: BrowserWindow): void => { windowGestures.get(win)?.end(); };

/**
 * 为聊天或日志接入同一套鼠标采样和生命周期清理，不修改角色位置或显示状态。
 * Args:
 *     win (BrowserWindow): 新创建的聊天或日志窗口。
 */
const installWindowGestures = (win: BrowserWindow): void => {
  const controller = new WindowGestureController(win, screen.getCursorScreenPoint, /**
   * 当前窗口开始操作时固定角色穿透，所有操作结束后按最后一次角色命中恢复。
   * Args:
   *     active (boolean): 当前窗口是否正在移动或缩放。
   */
  (active) => {
    // 1. 每个窗口分别记录，旧窗口迟到的结束通知不能解除另一个窗口的穿透保护。
    if (active) activeWindowGestures.add(win);
    else activeWindowGestures.delete(win);
    applyPetPointerState();
  });
  windowGestures.set(win, controller);
  /** 最小化、隐藏、失焦或页面卸载时取消采样，避免收不到 pointerup 后继续拖动。 */
  const cancelGesture = (): void => controller.end();
  win.on("minimize", cancelGesture); win.on("hide", cancelGesture); win.on("blur", cancelGesture);
  win.webContents.on("did-start-navigation", cancelGesture);
  win.webContents.on("render-process-gone", cancelGesture);
  win.on("closed", /** 窗口实际关闭时释放采样与登记，日志窗口下次打开可重新装配。 */
  () => { controller.end(); windowGestures.delete(win); });
};

/** 应用清理开始前关闭操作门禁、通知全部页面、结束采样并允许聊天释放。 */
export const beginWindowShutdown = (): void => {
  // 1. 先记录主进程状态，通知尚未绘制完的页面时也不会接受新的业务 IPC。
  if (!applicationOperations.beginShutdown()) return;
  for (const controller of windowGestures.values()) controller.end();
  applyPetPointerState();
  // 2. 页面进入统一等待界面；尚未收到通知的迟加载页面通过退出状态查询补齐。
  for (const win of BrowserWindow.getAllWindows()) {
    if (win.isDestroyed() || win.webContents.isDestroyed()) continue;
    // 2.1. 单个页面崩溃或正在卸载时，通知失败不能中断其他窗口锁定或应用资源清理。
    try { win.webContents.send("desktop-pet:application-shutdown"); }
    catch (error) { appendFrontendLog("warn", "shutdown", "通知页面进入退出状态失败", error); }
  }
};

/**
 * 更新任务栏标题，待审批优先于普通未读消息。
 */
const refreshChatTitle = (): void => {
  if (!chatWindow || chatWindow.isDestroyed()) return;
  chatWindow.setTitle(approvalId ? "Ayaya · 等待审批" : unread ? "Ayaya · 有新消息" : "Ayaya 聊天");
};

/**
 * 接收已经通过会话和事件水位检查的提醒，不主动显示或聚焦窗口。
 * Args:
 *     update (ChatAttentionUpdate): 当前回复或审批变化。
 */
export const updateChatAttention = (update: ChatAttentionUpdate): void => {
  if (!chatWindow || chatWindow.isDestroyed()) return;
  // 1. 会话切换只清除旧会话的窗口提醒，不提交停止或审批命令。
  if (update.reset) { unread = false; approvalId = null; chatWindow.flashFrame(false); }
  // 2. 回复正文到达但用户未查看时记录未读，连续分片不会累计多个提示。
  if (update.newMessage && (!chatWindow.isFocused() || chatWindow.isMinimized())) unread = true;
  // 3. 同一审批编号只启动一次闪烁；处理完成后关闭闪烁但不改变窗口可见状态。
  if (update.approvalId !== undefined) {
    const changed = update.approvalId !== approvalId;
    approvalId = update.approvalId;
    if (!approvalId) chatWindow.flashFrame(false);
    else if (changed && (!chatWindow.isFocused() || chatWindow.isMinimized())) chatWindow.flashFrame(true);
  }
  refreshChatTitle();
};

/**
 * 保存 Live2D 错误并送给聊天，窗口首次加载后可重送，避免初始化竞争丢提示。
 * Args:
 *     message (string): 已限制长度的模型错误说明。
 */
export const deliverModelError = (message: string): void => {
  modelError = message;
  if (chatWindow && !chatWindow.isDestroyed()) chatWindow.webContents.send("desktop-pet:model-error", message);
};

/**
 * 保存聊天置顶设置并应用到窗口。
 * Args:
 *     pinned (boolean): 用户选择的置顶状态。
 * Returns:
 *     boolean: 保存并应用后的状态。
 */
export const setChatPinned = (pinned: boolean): boolean => {
  saveFrontendSettings({ chat_window_pinned: pinned });
  chatWindow?.setAlwaysOnTop(pinned);
  return pinned;
};

/**
 * 创建可移动、可缩放的聊天窗口；保持一个实例和一个页面草稿。
 * Returns:
 *     BrowserWindow: 当前独立聊天窗口。
 */
export const openChatWindow = (): BrowserWindow => {
  // 1. 重复打开恢复现有窗口，保留渲染进程中的草稿和正在接收的消息。
  if (chatWindow && !chatWindow.isDestroyed()) {
    if (chatWindow.isMinimized()) chatWindow.restore();
    chatWindow.show(); chatWindow.focus();
    return chatWindow;
  }
  // 2. 读取前端位置配置并按当前显示器修正，旧文件缺少这些字段时使用默认值。
  const settings = readFrontendSettings();
  const saved = settings.chat_window_bounds;
  const area = saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)
    && Number.isFinite(saved.width) && saved.width > 0 && Number.isFinite(saved.height) && saved.height > 0
    ? screen.getDisplayMatching(saved).workArea : screen.getPrimaryDisplay().workArea;
  const bounds = fitChatBounds(saved, area);
  // 3. 聊天窗口正常接收鼠标，最小化时继续处理订阅事件和流式消息。
  const win = new BrowserWindow({
    ...bounds, minWidth: Math.min(420, area.width), minHeight: Math.min(480, area.height),
    // 3.1. 透明窗口合成避开 Windows 不透明窗口缩放时的白条；页面绘制深色背景与统一圆角。
    // 3.2. 透明无边框窗口的原生拖边不可依赖，八个边缘由聊天页面捕获并交给主进程计算。
    frame: false, thickFrame: false, transparent: true, resizable: true, maximizable: false, useContentSize: true,
    fullscreenable: false, show: false, backgroundColor: "#00000000",
    alwaysOnTop: settings.chat_window_pinned === true,
    webPreferences: {
      preload: path.resolve(__dirname, "preload.cjs"),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
    },
  });
  chatWindow = win;
  updateDesktopWindowReferences({ pet: mainWindow, chat: chatWindow });
  installWindowGestures(win);
  protectApplicationWindow(win, "chat-window");
  // 4. 位置保存、关闭转最小化和工作区适配都由主进程处理，页面不自行模拟窗口。
  let persistTimer: ReturnType<typeof setTimeout> | null = null;
  let boundsReady = false;
  let savedBounds = bounds;
  /** 用户结束移动或缩放后保存普通状态尺寸；不把临时最小化状态写入配置。 */
  const persist = (): void => {
    if (!boundsReady || win.isDestroyed() || win.isMinimized()) return;
    // 1. 保存可见页面区域，不包含 Windows 的隐藏边框；创建和恢复也使用同一坐标含义。
    // 1.1. 125% 等显示缩放可能让系统 getter 与恢复目标差一像素，不能逐次写回放大窗口。
    savedBounds = stabilizeChatBounds(win.getContentBounds(), savedBounds);
    try { saveFrontendSettings({ chat_window_bounds: savedBounds }); }
    catch (error) { appendFrontendLog("error", "chat-window", "保存聊天窗口位置失败", error); }
  };
  /** 合并窗口连续移动和缩放事件，降低配置写入频率。 */
  const schedulePersist = (): void => {
    if (persistTimer) clearTimeout(persistTimer);
    persistTimer = setTimeout(persist, 200);
  };
  win.on("move", schedulePersist); win.on("resize", schedulePersist);
  win.on("close", /**
   * 用户关闭请求只最小化，应用明确进入退出流程后放行。
   * Args:
   *     event (Event): Electron 的关闭事件。
   */
  (event) => {
    persist();
    if (!applicationOperations.isShuttingDown()) { event.preventDefault(); win.minimize(); }
  });
  win.on("focus", /** 恢复焦点后清除普通未读和闪烁，待审批标题保留至处理完成。 */
  () => { unread = false; win.flashFrame(false); refreshChatTitle(); });
  /** 显示器工作区变化后修正离屏位置，保留用户最小化状态。 */
  const fitToDisplay = (): void => {
    if (win.isDestroyed()) return;
    const before = win.isMinimized()
      ? readFrontendSettings().chat_window_bounds ?? win.getNormalBounds() : win.getContentBounds();
    const area = screen.getDisplayMatching(before).workArea;
    const corrected = fitChatBounds(before, area);
    // 1. 最小化期间只保存修正结果，恢复时再调整窗口，避免系统改变最小化状态。
    if (win.isMinimized()) {
      savedBounds = corrected;
      try { saveFrontendSettings({ chat_window_bounds: corrected }); }
      catch (error) { appendFrontendLog("error", "chat-window", "保存工作区适配位置失败", error); }
      return;
    }
    win.setMinimumSize(Math.min(420, area.width), Math.min(480, area.height));
    // 2. 当前尺寸和位置已合法时不重复设置，避免不必要的原生窗口布局变化。
    if (before.x !== corrected.x || before.y !== corrected.y
      || before.width !== corrected.width || before.height !== corrected.height) {
      savedBounds = corrected;
      win.setContentBounds(corrected);
    }
  };
  win.on("restore", fitToDisplay);
  screen.on("display-removed", fitToDisplay); screen.on("display-metrics-changed", fitToDisplay);
  win.on("closed", /** 页面实际释放时清理窗口监听和定时器。 */
  () => {
    if (persistTimer) clearTimeout(persistTimer);
    screen.removeListener("display-removed", fitToDisplay); screen.removeListener("display-metrics-changed", fitToDisplay);
    if (chatWindow === win) chatWindow = null;
    updateDesktopWindowReferences({ pet: mainWindow, chat: chatWindow });
  });
  win.webContents.on("did-finish-load", /** 聊天监听安装完毕后补送模型加载错误。 */
  () => { if (modelError) win.webContents.send("desktop-pet:model-error", modelError); });
  if (rendererPolicy.mode === "development") void win.loadURL(`${rendererPolicy.origin}/chat.html`);
  else void win.loadFile(productionChatPath);
  win.once("ready-to-show", /** 首次启动始终显示聊天窗口，不恢复上次最小化状态。 */
  () => {
    // 1. Windows 首次显示完成后按页面区域定位，再允许移动监听保存配置，避免把隐藏边框累计进尺寸。
    win.show(); win.setContentBounds(bounds); boundsReady = true; persist();
    win.focus(); refreshChatTitle();
  });
  return win;
};

/**
 * 获取主窗口
 */
export const getMainWindow = (): BrowserWindow | null => {
  return mainWindow;
};

/**
 * 获取设置窗口
 */
export const getSettingsWindow = (): BrowserWindow | null => {
  return settingsWindow;
};

/**
 * 向日志分发方提供当前日志窗口。
 *
 * Returns:
 *     BrowserWindow | null: 存活的日志窗口，未打开或已关闭时为空。
 */
export const getLogWindow = (): BrowserWindow | null => logWindow;

/**
 * 设置主窗口引用
 *
 * Args:
 *     win (BrowserWindow | null): 需要保护来源与导航的应用窗口。
 */
export const setMainWindow = (win: BrowserWindow | null): void => {
  mainWindow = win;
  updateDesktopWindowReferences({ pet: mainWindow, chat: chatWindow });
};

/**
 * 设置设置窗口引用
 *
 * Args:
 *     win (BrowserWindow | null): 需要保护来源与导航的应用窗口。
 */
export const setSettingsWindow = (win: BrowserWindow | null): void => {
  settingsWindow = win;
};

/**
 * 创建固定透明角色窗口并初始化鼠标穿透，聊天记录和输入由独立窗口负责。
 *
 * Returns:
 *     BrowserWindow: 接收角色加载、目光与动作事件的桌面窗口。
 */
export const createMainWindow = (): BrowserWindow => {
  const display = screen.getPrimaryDisplay();
  const { x, y, width, height } = display.workArea;
  const windowX = x + width - WINDOW_WIDTH;
  const windowY = y + height - WINDOW_HEIGHT;

  const win = new BrowserWindow({
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    x: windowX,
    y: windowY,
    frame: false,
    transparent: true,
    resizable: false,
    hasShadow: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    fullscreenable: false,
    webPreferences: {
      preload: path.resolve(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  protectApplicationWindow(win, "main-window");

  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(true, { forward: true });
  requestedPetInteractive = false; appliedPetInteractive = false;
  mainWindow = win;
  updateDesktopWindowReferences({ pet: mainWindow, chat: chatWindow });

  if (rendererPolicy.mode === "development") {
    win.loadURL(rendererPolicy.origin);
    // 开发模式下打开开发者工具
    win.webContents.openDevTools({ mode: "detach" });
  } else {
    win.loadFile(productionIndexPath);
  }

  return win;
};

/**
 * 打开或聚焦设置窗口，任务快捷入口在新窗口和已有窗口都直达任务页。
 *
 * Args:
 *     page ("tasks"): 指定任务页；省略时保持当前页或默认模型页。
 */
export const openSettingsWindow = (page?: "tasks"): void => {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    // 1. 已有窗口使用 IPC 导航，避免重新加载丢失其他设置页的状态。
    if (page) {
      const target = settingsWindow;
      /**
       *  在页面监听就绪后导航，避免首次加载期间连续点击丢失目标页。
       */
      const navigate = (): void => {
        if (!target.isDestroyed()) target.webContents.send("desktop-pet:open-settings-page", page);
      };
      if (target.webContents.isLoadingMainFrame()) target.webContents.once("did-finish-load", navigate);
      else navigate();
    }
    if (settingsWindow.isMinimized()) settingsWindow.restore();
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }

  // 居中显示
  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workArea;
  const x = Math.floor((width - SETTINGS_WIDTH) / 2);
  const y = Math.floor((height - SETTINGS_HEIGHT) / 2);

  const win = new BrowserWindow({
    width: SETTINGS_WIDTH,
    height: SETTINGS_HEIGHT,
    x,
    y,
    frame: false,
    transparent: false,
    resizable: false,
    show: false,
    fullscreenable: false,
    backgroundColor: "#1c1917",
    webPreferences: {
      preload: path.resolve(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  protectApplicationWindow(win, "settings-window");

  if (rendererPolicy.mode === "development") {
    win.loadURL(`${rendererPolicy.origin}/settings.html${page ? `?page=${page}` : ""}`);
  } else {
    win.loadFile(productionSettingsPath, { query: page ? { page } : {} });
  }

  win.once("ready-to-show", /**
   * 设置窗口内容准备完成后显示窗口并获得焦点。
   */
  () => {
    win.show();
    win.focus();
  });

  win.on("closed", /**
   * 设置窗口关闭时仅清除仍指向该窗口的缓存引用。
   */
  () => {
    if (settingsWindow === win) {
      settingsWindow = null;
    }
  });

  settingsWindow = win;
};

/**
 * 创建或聚焦前后端实时日志控制台，复用聊天的透明合成、圆角与窗口拖动。
 *
 * Returns:
 *     BrowserWindow: 当前日志控制台实例，允许正常关闭并重新创建。
 */
export const openLogWindow = (): BrowserWindow => {
  if (logWindow && !logWindow.isDestroyed()) {
    if (logWindow.isMinimized()) logWindow.restore();
    logWindow.show();
    logWindow.focus();
    return logWindow;
  }

  const win = new BrowserWindow({
    width: LOG_WINDOW_WIDTH,
    height: LOG_WINDOW_HEIGHT,
    minWidth: LOG_WINDOW_MIN_WIDTH,
    minHeight: LOG_WINDOW_MIN_HEIGHT,
    center: true,
    frame: false,
    // 1. 与聊天共用透明合成和应用边缘缩放，避免原生客户区重绘时出现白边。
    thickFrame: false,
    transparent: true,
    resizable: true,
    show: false,
    fullscreenable: false,
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.resolve(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  installWindowGestures(win);
  protectApplicationWindow(win, "log-window");
  if (rendererPolicy.mode === "development") {
    void win.loadURL(`${rendererPolicy.origin}/logs.html`);
  } else {
    void win.loadFile(productionLogsPath);
  }
  win.once("ready-to-show", /**
   * 日志窗口内容准备完成后显示窗口并获得焦点。
   */
  () => {
    win.show();
    win.focus();
  });
  win.on("closed", /**
   * 日志窗口关闭时清除该窗口的缓存引用，后续日志入口可重新创建。
   */
  () => {
    if (logWindow === win) logWindow = null;
  });
  logWindow = win;
  return win;
};

/**
 * 日志窗口存在时把已脱敏日志批次发送给页面。
 *
 * Args:
 *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
 */
export const sendLogBatch = (batch: LogBatch): void => {
  if (!logWindow || logWindow.isDestroyed()) return;
  logWindow.webContents.send("desktop-pet:log-batch", batch);
};

/**
 * 打开图片预览窗口
 *
 * Args:
 *     imageSrc (string): 要预览的图片地址。
 */
export const openImagePreviewWindow = (imageSrc: string): void => {
  // 如果已有预览窗口，先关闭
  if (imagePreviewWindow && !imagePreviewWindow.isDestroyed()) {
    imagePreviewWindow.close();
  }

  const display = screen.getPrimaryDisplay();
  const { width, height } = display.workArea;

  // 窗口尺寸：最大 800x600，但不超过屏幕工作区域减去 100px 边距
  const winWidth = Math.min(800, width - 100);
  const winHeight = Math.min(600, height - 100);
  const x = Math.floor((width - winWidth) / 2);
  const y = Math.floor((height - winHeight) / 2);

  const win = new BrowserWindow({
    width: winWidth,
    height: winHeight,
    x,
    y,
    frame: false,
    transparent: false,
    resizable: true,
    show: false,
    fullscreenable: false,
    backgroundColor: "#1c1917",
    webPreferences: {
      preload: path.resolve(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  protectApplicationWindow(win, "image-preview-window");
  const query = { image: imageSrc };
  if (rendererPolicy.mode === "development") {
    void win.loadURL(`${rendererPolicy.origin}/image-preview.html?${new URLSearchParams(query)}`);
  } else { void win.loadFile(productionPreviewPath, { query }); }

  win.once("ready-to-show", /**
   * 图片预览内容准备完成后显示并聚焦窗口。
   */
  () => {
    win.show();
    win.focus();
  });

  win.on("closed", /**
   * 图片预览窗口关闭后清除缓存引用。
   */
  () => {
    imagePreviewWindow = null;
  });

  imagePreviewWindow = win;
};

/**
 * 初始化窗口事件监听
 */
export const initWindowEventListeners = (): void => {
  app.on("browser-window-created", /**
   * Electron 创建窗口后安装关闭监听，关闭时释放对应缓存引用。
   *
   * Args:
   *     _event ({ preventDefault: () => void; readonly defaultPrevented: boolean; }): Electron 传入的事件对象，不作为业务载荷使用。
   *     win (BrowserWindow): 需要保护来源与导航的应用窗口。
   */
  (_event, win) => {
    win.on("closed", /**
     * 窗口关闭时逐项核对是否为当前主窗口、设置窗口或日志窗口，仅移除匹配引用。
     */
    () => {
      if (mainWindow === win) {
        mainWindow = null;
        updateDesktopWindowReferences({ pet: mainWindow, chat: chatWindow });
      }
      if (settingsWindow === win) {
        settingsWindow = null;
      }
      if (logWindow === win) {
        logWindow = null;
      }
    });
  });

  app.on("activate", /**
   * 应用被重新激活且没有窗口时重新创建主窗口，退出等待期间禁止重新创建。
   */
  () => {
    if (!applicationOperations.isShuttingDown() && BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    }
  });
};
