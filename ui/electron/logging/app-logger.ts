import type { BrowserWindow } from "electron";
import { mkdirSync } from "node:fs";

import type {
  LogBatch,
  LogLevel,
  LogSide,
  LogSnapshot,
  LogSource,
} from "../../shared-types.js";
import { LogHub } from "./log-core.js";
import { RollingLogWriter } from "./rolling-log-writer.js";

const MAX_LOG_BYTES = 10 * 1024 * 1024;
const MAX_LOG_FILES = 5;
const rawStderrWrite = process.stderr.write.bind(process.stderr);
const logHub = new LogHub({ immediateSides: ["backend"] });
let logDirectory = "";

/**
 * 保留错误堆栈、底层原因和错误码，避免 fetch 的 terminated 包装掩盖断线原因。
 *
 * Args:
 *     error (unknown): 当前异常或它的底层 cause。
 *     seen (Set<unknown>): 已处理的原因对象，防止循环 cause 无限递归。
 *
 * Returns:
 *     string: 交给统一脱敏、限长及 UTF-8 写入的异常说明。
 */
const describeError = (error: unknown, seen = new Set<unknown>()): string => {
  if (error instanceof Error) {
    // 1. 原因链可能引用自己，最多展开八层，保留诊断信息而不无限遍历。
    if (seen.has(error) || seen.size >= 8) return "[错误原因链已截断]";
    seen.add(error);
    const code = (error as Error & { code?: unknown }).code;
    const details = (error.stack || `${error.name}: ${error.message}`)
      + (code === undefined ? "" : `\n错误码: ${String(code)}`);
    // 2. native fetch 把 BodyTimeoutError 放在 cause 中，必须随外层堆栈一起记录。
    return error.cause === undefined ? details : `${details}\n原因: ${describeError(error.cause, seen)}`;
  }
  return String(error);
};

/**
 * 记录日志文件写入错误，避免文件失败中断应用业务。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     error (unknown): 本次捕获的异常。
 */
const reportWriterError = (side: LogSide, error: unknown): void => {
  rawStderrWrite(`[Ayaya Log] ${side} 日志写入失败: ${describeError(error)}\n`);
};

/**
 * 为前端和后端分别创建滚动文件写入器，并接入进程日志中心。
 *
 * Args:
 *     directory (string): 当前环境的日志保存目录。
 */
export const initializeLogFiles = (directory: string): void => {
  mkdirSync(directory, { recursive: true });
  logDirectory = directory;
  logHub.setWriter("frontend", new RollingLogWriter({
    directory,
    fileName: "frontend.log",
    maxBytes: MAX_LOG_BYTES,
    maxFiles: MAX_LOG_FILES,
    /**
     * 日志写入器失败后报告对应类别的文件错误。
     *
     * Args:
     *     error (unknown): 本次捕获的异常。
     */
    onError: (error) => reportWriterError("frontend", error),
  }));
  logHub.setWriter("backend", new RollingLogWriter({
    directory,
    fileName: "backend.log",
    maxBytes: MAX_LOG_BYTES,
    maxFiles: MAX_LOG_FILES,
    /**
     * 日志写入器失败后报告对应类别的文件错误。
     *
     * Args:
     *     error (unknown): 本次捕获的异常。
     */
    onError: (error) => reportWriterError("backend", error),
  }));
};

/**
 * 提供桌面日志文件保存目录，供打开文件夹操作使用。
 *
 * Returns:
 *     string: 当前配置的日志根目录。
 */
export const getLogDirectory = (): string => logDirectory;

/**
 * 把当前通信令牌或凭据登记到日志脱敏集合。
 *
 * Args:
 *     value (string): 当前待检查或转换的值。
 */
export const registerSensitiveLogValue = (value: string): void => {
  logHub.addSensitiveValue(value);
};

/**
 * 将主进程或页面日志交给统一脱敏、缓存和分发流程。
 *
 * Args:
 *     level (LogLevel): 当前日志级别。
 *     scope (string): 日志所属模块或功能名称。
 *     message (string): 当前需要显示或记录的文本。
 *     error (unknown): 本次捕获的异常。
 */
export const appendFrontendLog = (
  level: LogLevel,
  scope: string,
  message: string,
  error?: unknown,
): void => {
  logHub.append({
    side: "frontend",
    source: "electron-main",
    level,
    scope,
    message: error === undefined ? message : `${message}\n${describeError(error)}`,
  });
};

/**
 * 将 Python 子进程输出交给后端日志流。
 *
 * Args:
 *     source (Extract<LogSource, "stdout" | "stderr">): 日志产生的来源。
 *     level (LogLevel): 当前日志级别。
 *     message (string): 当前需要显示或记录的文本。
 */
export const appendBackendLog = (
  source: Extract<LogSource, "stdout" | "stderr">,
  level: LogLevel,
  message: string,
): void => {
  logHub.append({
    side: "backend",
    source,
    level,
    scope: "python",
    message,
  });
};

/**
 * 给新打开日志窗口提供当前缓存。
 *
 * Returns:
 *     LogSnapshot: 前端和后端日志数组副本。
 */
export const getLogSnapshot = (): LogSnapshot => logHub.getSnapshot();

/**
 * 按类别读取最近的日志，供错误提示或排查使用。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 *     limit (number): 最多保留或读取的记录数量。
 *
 * Returns:
 *     LogRecord[]: 该类别最近的记录列表。
 */
export const getRecentLogs = (side: LogSide, limit: number) =>
  logHub.getRecent(side, limit);

/**
 * 清空指定类别的内存日志，不删除已有日志文件。
 *
 * Args:
 *     side (LogSide): 前端或后端日志类别。
 */
export const clearLogBuffer = (side: LogSide): void => logHub.clear(side);

/**
 * 登记日志窗口的实时批次监听者。
 *
 * Args:
 *     listener ((batch: LogBatch) => void): 接收后续事件的回调，取消订阅后不再调用。
 *
 * Returns:
 *     (() => void): 移除监听者的取消订阅函数。
 */
export const subscribeLogBatches = (listener: (batch: LogBatch) => void): (() => void) =>
  logHub.subscribe(listener);

/**
 * 退出应用前提交待分发日志并等待文件写入器关闭。
 *
 * Returns:
 *     Promise<void>: 日志中心与文件写入器关闭后完成。
 */
export const closeLogFiles = (): Promise<void> => logHub.close();

/**
 * 将 Electron 页面控制台级别转换为统一日志级别。
 *
 * Args:
 *     level ("info" | "warning" | "error" | "debug"): 当前日志级别。
 *
 * Returns:
 *     LogLevel: 用于日志筛选的级别。
 */
const rendererLevel = (level: "info" | "warning" | "error" | "debug"): LogLevel => {
  if (level === "warning") return "warn";
  return level;
};

/**
 *  捕获应用 renderer 的控制台输出，并限制单窗口的瞬时日志速率。
 *
 * Args:
 *     win (BrowserWindow): 需要保护来源与导航的应用窗口。
 *     scope (string): 日志所属模块或功能名称。
 */
export const captureRendererLogs = (win: BrowserWindow, scope: string): void => {
  let windowStartedAt = Date.now();
  let acceptedInWindow = 0;
  let rateWarningWritten = false;
  win.webContents.on("console-message", /**
   * 渲染页面控制台消息到达时按时间窗口限流，登记来源和级别后交给统一日志脱敏流程。
   *
   * Args:
   *     details (Event<WebContentsConsoleMessageEventParams>): Electron 提供的渲染进程退出信息。
   */
  (details) => {
    const now = Date.now();
    if (now - windowStartedAt >= 1_000) {
      windowStartedAt = now;
      acceptedInWindow = 0;
      rateWarningWritten = false;
    }
    acceptedInWindow += 1;
    if (acceptedInWindow > 200) {
      if (!rateWarningWritten) {
        rateWarningWritten = true;
        logHub.append({
          side: "frontend",
          source: "renderer",
          level: "warn",
          scope,
          message: "renderer 日志速率超过每秒 200 条，当前时间窗口的后续日志已丢弃",
        });
      }
      return;
    }
    logHub.append({
      side: "frontend",
      source: "renderer",
      level: rendererLevel(details.level),
      scope,
      message: details.message,
    });
  });
};
