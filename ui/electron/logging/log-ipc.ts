import { IpcChannel } from "../../ipc-channels.js";
import { shell } from "electron";

import type { LogBatch, LogSide } from "../../shared-types.js";
import { createTrustedIpcRegistrar } from "../ipc-security.js";
import type { TrustedRendererPolicy } from "../renderer-security.js";
import {
  clearLogBuffer,
  getLogDirectory,
  getLogSnapshot,
  subscribeLogBatches,
} from "./app-logger.js";

/**
 * 约定可信来源规则、日志窗口打开和批次发送接口。
 */
interface LogIpcOptions {
  policy: TrustedRendererPolicy;
  openLogWindow: () => void;
  sendBatch: (batch: LogBatch) => void;
}

/**
 * 检查页面请求的日志类别是否为前端或后端。
 *
 * Args:
 *     value (unknown): 当前待检查或转换的值。
 *
 * Returns:
 *     value is LogSide: 受支持的日志类别为 true。
 */
const isLogSide = (value: unknown): value is LogSide =>
  value === "frontend" || value === "backend";

/**
 * 注册可信页面的快照、清空和日志目录操作，并连接实时日志批次分发。
 *
 * Args:
 *     policy (TrustedRendererPolicy): 允许访问的应用页面及来源规则。
 *     openLogWindow (() => void): 在来源验证成功后打开日志窗口的函数。
 *     sendBatch ((batch: LogBatch) => void): 把日志批次发送给日志窗口的函数。
 */
export const registerLogIpcHandlers = ({
  policy,
  openLogWindow,
  sendBatch,
}: LogIpcOptions): (() => void) => {
  const trustedIpc = createTrustedIpcRegistrar(policy);
  trustedIpc.on(IpcChannel.OPEN_LOG_WINDOW, /**
   * 可信页面请求日志入口时打开或复用日志窗口。
   */
  () => openLogWindow());
  trustedIpc.handle(IpcChannel.GET_LOG_SNAPSHOT, /**
   * 新日志窗口请求初始化时取得当前内存日志快照。
   *
   * Returns:
   *     LogSnapshot: 前端和后端缓存记录。
   */
  () => getLogSnapshot());
  trustedIpc.handle(IpcChannel.CLEAR_LOG_BUFFER, /**
   * 页面清空日志前校验类别，合法时只清空该类内存记录。
   *
   * Args:
   *     _event (IpcMainInvokeEvent): Electron 传入的事件对象，不作为业务载荷使用。
   *     side (unknown): 前端或后端日志类别。
   */
  (_event, side: unknown) => {
    if (!isLogSide(side)) throw new Error("无效的日志分区");
    clearLogBuffer(side);
  });
  trustedIpc.handle(IpcChannel.OPEN_LOG_DIRECTORY, /**
   * 页面请求日志目录时确认目录已初始化，再交给系统文件管理器；打开失败明确报错。
   */
  async () => {
    const directory = getLogDirectory();
    if (!directory) throw new Error("日志目录尚未初始化");
    const failure = await shell.openPath(directory);
    if (failure) throw new Error("无法打开日志目录");
  });
  return subscribeLogBatches(sendBatch);
};
