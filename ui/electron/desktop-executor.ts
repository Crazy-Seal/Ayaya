/** Electron 主进程执行通道；请求与聊天 Renderer 无关。 */
import fs from "node:fs";
import { desktopCapturer, screen } from "electron";
import { backendFetch } from "./backend-client.js";
import { FRONTEND_SETTINGS_PATH } from "./config.js";
import { performScreenAction } from "./ipc-handlers.js";
import { selectPrimaryDisplaySource } from "./screen-capture-source.js";
import { readSseJson } from "./sse-json.js";
import { getMainWindow, getChatWindow } from "./window-manager.js";
import { hideDesktopWindows } from "./desktop-window-visibility.js";
import { appendFrontendLog } from "./logging/app-logger.js";
import type { ScreenActionRequest } from "./types.js";
import { DesktopExecutionKind } from "../shared-types.js";
import {
  DESKTOP_RESULT_RETRY_DELAY_MS, SCREEN_CAPTURE_HIDE_DELAY_MS, SUBSCRIPTION_RECONNECT_DELAY_MS,
} from "../runtime-constants.js";

/**
 * 携带独立桌面执行请求，动作发生前必须通过后端 claim。
 *
 * Attributes:
 *     session_id (string): 共享 Ayaya 会话标识。
 *     request_id (string): 持久中断标识，关联审批决定或屏幕执行结果。
 *     kind (DesktopExecutionKind): 截屏或已授权屏幕动作的执行类别。
 *     payload (Record<string, unknown>): 对应种类的专用数据，命令中省略时后端采用空对象。
 */
interface ExecutionRequest {
  session_id: string;
  request_id: string;
  kind: DesktopExecutionKind;
  payload: Record<string, unknown>;
}

/**
 * 等待重连或窗口显隐缓冲，计时结束只唤醒当前异步调用。
 *
 * Args:
 *     ms (number): 等待毫秒数。
 *
 * Returns:
 *     Promise<void>: 当前异步处理完成的等待结果。
 */
const pause = (ms: number): Promise<void> => new Promise(/**
 * Promise 创建时安排动作等待定时器，到期后继续桌面执行结果处理。
 *
 * Args:
 *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
 */
(resolve) => setTimeout(resolve, ms));

/**
 * 通过受管后端连接提交执行器 JSON 请求，并返回业务回执。
 *
 * Args:
 *     path (string): 后端相对接口路径。
 *     body (object): 需要 JSON 序列化的请求对象。
 *
 * Returns:
 *     Promise<Record<string, unknown>>: 后端确认桌面领取或结果请求的 JSON 回执。
 */
const post = async (path: string, body: object): Promise<Record<string, unknown>> => {
  const response = await backendFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`桌面执行器请求失败: ${response.status}`);
  return await response.json() as Record<string, unknown>;
};

/**
 * 选择主显示器截图，返回 data URL 和实际缩略图尺寸。
 *
 * Returns:
 *     Promise<{ dataUrl: string; width: number; height: number }>: 主显示器截图地址及实际图片宽高。
 */
const captureScreen = async (): Promise<{ dataUrl: string; width: number; height: number }> => {
  const sources = await desktopCapturer.getSources({
    types: ["screen"], thumbnailSize: { width: 1920, height: 1080 },
  });
  const display = screen.getPrimaryDisplay();
  const selected = selectPrimaryDisplaySource(sources, display.id);
  const size = selected.thumbnail.getSize();
  return { dataUrl: selected.thumbnail.toDataURL(), width: size.width, height: size.height };
};

/**
 * 领取一次屏幕请求，隐藏桌面窗口后执行，最终只重试结果回传。
 *
 * Args:
 *     request (ExecutionRequest): 由独立执行 SSE 接收的请求。
 *     signal (AbortSignal): 应用或订阅关闭的取消信号。
 *
 * Returns:
 *     Promise<void>: 当前异步处理完成的等待结果。
 */
const execute = async (request: ExecutionRequest, signal: AbortSignal): Promise<void> => {
  // 1. 先向后端领取并复核请求，未领取时不能产生屏幕副作用。
  const identity = { session_id: request.session_id, request_id: request.request_id };
  const claim = await post("/desktop-executor/claim", identity);
  if (claim.claimed !== true) return;

  // 2. 记住窗口原可见状态，按设置隐藏桌面 UI 后再截图或执行动作。
  let hide = true;
  try {
    const settings = JSON.parse(fs.readFileSync(FRONTEND_SETTINGS_PATH, "utf-8")) as {
      hide_on_screenshot?: boolean;
    };
    hide = settings.hide_on_screenshot !== false;
  } catch { /* 首次启动使用默认设置。 */ }
  const visibility = hideDesktopWindows(hide ? [getMainWindow(), getChatWindow()] : []);
  if (visibility.hidden) await pause(SCREEN_CAPTURE_HIDE_DELAY_MS);
  // 3. 动作只执行一次；成功或失败均构造真实结果，finally 恢复原窗口可见状态。
  let result: Record<string, unknown>;
  try {
    if (request.kind === DesktopExecutionKind.CAPTURE_SCREEN) {
      const image = await captureScreen();
      result = { screenshot_data: image.dataUrl, width: image.width, height: image.height };
    } else if (request.kind === DesktopExecutionKind.PERFORM_SCREEN_ACTION) {
      const action = request.payload as unknown as ScreenActionRequest;
      const performed = await performScreenAction(action);
      if (!performed.executed) {
        result = { executed: false, error: performed.error || "屏幕操作未完成" };
      } else {
        await pause(Math.max(0, Number(action.wait_seconds || 0)) * 1000);
        const image = await captureScreen();
        result = {
          executed: true, screenshot_data: image.dataUrl,
          width: image.width, height: image.height,
        };
      }
    } else {
      result = { executed: false, error: `未知桌面执行类型: ${request.kind}` };
    }
  } catch (error) {
    result = { executed: false, error: String(error) };
  } finally {
    visibility.restore();
  }
  // claim 后绝不重做屏幕动作；连接暂断时只重试同一个结果。
  // 4. 已领取后只重试同一结果 POST，不能因断线重做点击或按键。
  let retryingResult = false;
  while (!signal.aborted) {
    try {
      const receipt = await post("/desktop-executor/result", { ...identity, result });
      if (receipt.accepted !== true) {
        appendFrontendLog("warn", "desktop-executor", `桌面执行结果已过期: ${request.request_id}`);
      } else if (retryingResult) {
        appendFrontendLog("info", "desktop-executor", `桌面执行结果回传已恢复: ${request.request_id}`);
      }
      return;
    } catch (error) {
      if (!retryingResult) appendFrontendLog("warn", "desktop-executor", "桌面执行结果回传失败，稍后重试", error);
      retryingResult = true;
      await pause(DESKTOP_RESULT_RETRY_DELAY_MS);
    }
  }
};

/**
 * 在 Electron 主进程持续订阅独立执行流，返回应用关闭时的停止函数。
 *
 * Returns:
 *     (() => void): 取消独立桌面执行订阅的函数，应用关闭时调用。
 */
export const startDesktopExecutor = (): (() => void) => {
  const controller = new AbortController();
  /**
   * 持续消费执行请求，断线后重连但不重做已领取动作。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  const run = async (): Promise<void> => {
    let reconnecting = false;
    // 1. 持续订阅专用执行 SSE，与聊天 Renderer 和会话展示连接分别管理。
    while (!controller.signal.aborted) {
      try {
        const response = await backendFetch("/desktop-executor/requests", {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`执行流连接失败: ${response.status}`);
        if (reconnecting) appendFrontendLog("info", "desktop-executor", "桌面执行流已重新连接");
        reconnecting = false;
        // 2. 串行执行收到的请求，重复请求仍由后端 claim 保证不会重复动作。
        for await (const request of readSseJson<ExecutionRequest>(response, controller.signal)) {
          await execute(request, controller.signal);
        }
        if (!controller.signal.aborted) throw new Error("桌面执行订阅意外结束");
      } catch (error) {
        // 3. 同一断线期间只记录首个错误，统一进入应用日志而不绕过到启动终端。
        if (!controller.signal.aborted && !reconnecting) {
          appendFrontendLog("warn", "desktop-executor", "桌面执行流断开，将重新订阅", error);
        }
        reconnecting = true;
      }
      // 3. 断线只延迟重连，未领取请求由后端 pending 快照重新提供。
      if (!controller.signal.aborted) await pause(SUBSCRIPTION_RECONNECT_DELAY_MS);
    }
  };
  void run();
  /**
   * 调用方停止桌面执行器时取消当前领取轮询和等待。
   */
  return () => controller.abort();
};
