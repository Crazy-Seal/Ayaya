import {
  ipcMain,
  type IpcMainEvent,
  type IpcMainInvokeEvent,
} from "electron";

import {
  describeRendererUrl,
  isTrustedIpcSender,
  type TrustedRendererPolicy,
} from "./renderer-security.js";
import { appendFrontendLog } from "./logging/app-logger.js";

/**
 * 从 IPC 事件提取发送页面与主框架信息，供可信来源校验。
 *
 * Args:
 *     event (IpcMainEvent | IpcMainInvokeEvent): 调用方或浏览器触发的当前事件。
 *
 * Returns:
 *     { url: string; isMainFrame: boolean; } | null: 发送方来源描述。
 */
const senderDescriptor = (event: IpcMainEvent | IpcMainInvokeEvent) => {
  const frame = event.senderFrame;
  if (!frame) {
    return null;
  }
  return {
    url: frame.url,
    isMainFrame: frame === event.sender.mainFrame,
  };
};

/**
 * 记录被拒绝的 IPC 渠道和来源，便于排查非应用页面请求。
 *
 * Args:
 *     channel (string): 主进程与页面约定的 IPC 事件名称。
 *     event (IpcMainEvent | IpcMainInvokeEvent): 调用方或浏览器触发的当前事件。
 */
const logBlockedIpc = (channel: string, event: IpcMainEvent | IpcMainInvokeEvent): void => {
  const source = event.senderFrame ? describeRendererUrl(event.senderFrame.url) : "missing-frame";
  appendFrontendLog(
    "warn",
    "ipc-security",
    `拒绝不可信 IPC: channel=${channel} source=${source}`,
  );
};

/**
 * 创建统一 IPC 注册入口，在调用业务函数前检查发送方是否为可信应用页面。
 *
 * Args:
 *     policy (TrustedRendererPolicy): 允许访问的应用页面及来源规则。
 *
 * Returns:
 *     { handle<TArgs extends unknown[], TResult>(channel: string, handler: (event: IpcMainInvokeEvent, ...args: TArgs) => TResult): void; on<TArgs extends unknown[]>(channel: string, handler: (event: IpcMainEvent, ...args: TArgs) => void): void; }: 带可信来源校验的 handle 和 on 注册函数。
 */
export const createTrustedIpcRegistrar = (policy: TrustedRendererPolicy) => ({
  /**
   * 注册有返回值的 IPC 请求处理器，先校验来源再调用业务函数。
   *
   * Args:
   *     channel (string): 主进程与页面约定的 IPC 事件名称。
   *     handler ((event: IpcMainInvokeEvent, ...args: TArgs) => TResult): 来源验证成功后执行的 IPC 处理函数。
   */
  handle<TArgs extends unknown[], TResult>(
    channel: string,
    handler: (event: IpcMainInvokeEvent, ...args: TArgs) => TResult
  ): void {
    ipcMain.handle(channel, /**
     * Electron 收到有返回值的 IPC 请求后核对页面和框架来源，不可信则拒绝，可信才交给业务处理器。
     *
     * Args:
     *     event (IpcMainInvokeEvent): 调用方或浏览器触发的当前事件。
     *     args (any[]): 传递给子进程的命令行参数。
     *
     * Returns:
     *     TResult: 业务处理器的响应。
     */
    (event, ...args) => {
      if (!isTrustedIpcSender(senderDescriptor(event), policy)) {
        logBlockedIpc(channel, event);
        throw new Error("拒绝来自不可信页面的 IPC 请求");
      }
      return handler(event, ...(args as TArgs));
    });
  },

  /**
   * 注册无需响应的 IPC 事件监听器，只处理可信页面发送的事件。
   *
   * Args:
   *     channel (string): 主进程与页面约定的 IPC 事件名称。
   *     handler ((event: IpcMainEvent, ...args: TArgs) => void): 来源验证成功后执行的 IPC 处理函数。
   */
  on<TArgs extends unknown[]>(
    channel: string,
    handler: (event: IpcMainEvent, ...args: TArgs) => void
  ): void {
    ipcMain.on(channel, /**
     * Electron 收到无响应 IPC 事件后核对来源，不可信则记录并忽略，可信才调用业务函数。
     *
     * Args:
     *     event (IpcMainEvent): 调用方或浏览器触发的当前事件。
     *     args (any[]): 传递给子进程的命令行参数。
     */
    (event, ...args) => {
      if (!isTrustedIpcSender(senderDescriptor(event), policy)) {
        logBlockedIpc(channel, event);
        return;
      }
      handler(event, ...(args as TArgs));
    });
  },
});
