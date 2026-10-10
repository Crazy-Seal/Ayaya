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

const reportWriterError = (side: LogSide, error: unknown): void => {
  rawStderrWrite(`[Ayaya Log] ${side} 日志写入失败: ${describeError(error)}\n`);
};

export const initializeLogFiles = (directory: string): void => {
  mkdirSync(directory, { recursive: true });
  logDirectory = directory;
  logHub.setWriter("frontend", new RollingLogWriter({
    directory,
    fileName: "frontend.log",
    maxBytes: MAX_LOG_BYTES,
    maxFiles: MAX_LOG_FILES,
    onError: (error) => reportWriterError("frontend", error),
  }));
  logHub.setWriter("backend", new RollingLogWriter({
    directory,
    fileName: "backend.log",
    maxBytes: MAX_LOG_BYTES,
    maxFiles: MAX_LOG_FILES,
    onError: (error) => reportWriterError("backend", error),
  }));
};

export const getLogDirectory = (): string => logDirectory;

export const registerSensitiveLogValue = (value: string): void => {
  logHub.addSensitiveValue(value);
};

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

export const getLogSnapshot = (): LogSnapshot => logHub.getSnapshot();

export const getRecentLogs = (side: LogSide, limit: number) =>
  logHub.getRecent(side, limit);

export const clearLogBuffer = (side: LogSide): void => logHub.clear(side);

export const subscribeLogBatches = (listener: (batch: LogBatch) => void): (() => void) =>
  logHub.subscribe(listener);

export const closeLogFiles = (): Promise<void> => logHub.close();

const rendererLevel = (level: "info" | "warning" | "error" | "debug"): LogLevel => {
  if (level === "warning") return "warn";
  return level;
};

/** 捕获应用 renderer 的控制台输出，并限制单窗口的瞬时日志速率。 */
export const captureRendererLogs = (win: BrowserWindow, scope: string): void => {
  let windowStartedAt = Date.now();
  let acceptedInWindow = 0;
  let rateWarningWritten = false;
  win.webContents.on("console-message", (details) => {
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
