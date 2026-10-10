import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createBackendRecoveryCoordinator,
  type BackendRecoveryDialogModel,
} from "../electron/backend-recovery.js";
import type { BackendUnexpectedExit } from "../electron/backend-process.js";

const EXIT_EVENT: BackendUnexpectedExit = {
  exitCode: 1,
  signal: null,
  message: "后端进程意外退出（退出码 1）",
};

/**
 * 轮询用例给定条件，等待异步状态变化后继续断言。
 *
 * Args:
 *     predicate (() => boolean): 判断当前剪贴板格式是否符合目标类型的函数。
 */
const waitUntil = async (predicate: () => boolean): Promise<void> => {
  const deadline = Date.now() + 1_000;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("等待恢复协调器状态超时");
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：轮询用例给定条件，等待异步状态变化后继续断言。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
  }
};

test("异常退出对话框提供重连和退出并默认重连", /**
 * 验证异常退出对话框提供重连和退出并默认重连。
 */
async () => {
  const dialogs: BackendRecoveryDialogModel[] = [];
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
     */
    startBackend: async () => undefined,
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
     *
     * Args:
     *     model (BackendRecoveryDialogModel): 需要展示的后端恢复对话框信息。
     */
    showDialog: async (model) => {
      dialogs.push(model);
      return "exit";
    },
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
     */
    clearSettingsCache: () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
     */
    quit: () => undefined,
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证异常退出对话框提供重连和退出并默认重连。
   */
  () => dialogs.length === 1);

  assert.deepEqual(dialogs[0], {
    title: "Ayaya 后端意外退出",
    message: "后端进程已停止，聊天、设置和 MCP 功能暂时不可用。",
    detail: "后端进程意外退出（退出码 1）",
    buttons: ["重连", "退出"],
    defaultId: 0,
    cancelId: 1,
  });
});

test("选择退出不会重连且只退出一次", /**
 * 验证选择退出不会重连且只退出一次。
 */
async () => {
  let starts = 0;
  let quits = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证选择退出不会重连且只退出一次。
     */
    startBackend: async () => { starts += 1; },
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证选择退出不会重连且只退出一次。
     */
    showDialog: async () => "exit",
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证选择退出不会重连且只退出一次。
     */
    clearSettingsCache: () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证选择退出不会重连且只退出一次。
     */
    quit: () => { quits += 1; },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证选择退出不会重连且只退出一次。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证选择退出不会重连且只退出一次。
   */
  () => quits === 1);
  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await new Promise<void>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证选择退出不会重连且只退出一次。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setImmediate(resolve));

  assert.equal(starts, 0);
  assert.equal(quits, 1);
});

test("选择重连成功后清理设置缓存并保留应用", /**
 * 验证选择重连成功后清理设置缓存并保留应用。
 */
async () => {
  let starts = 0;
  let cacheClears = 0;
  let quits = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
     */
    startBackend: async () => { starts += 1; },
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
     */
    showDialog: async () => "reconnect",
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
     */
    clearSettingsCache: () => { cacheClears += 1; },
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
     */
    quit: () => { quits += 1; },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证选择重连成功后清理设置缓存并保留应用。
   */
  () => cacheClears === 1);

  assert.equal(starts, 1);
  assert.equal(quits, 0);
});

test("重连失败后展示最新错误并继续提供选择", /**
 * 验证重连失败后展示最新错误并继续提供选择。
 */
async () => {
  const dialogs: BackendRecoveryDialogModel[] = [];
  const choices = ["reconnect", "exit"] as const;
  let choiceIndex = 0;
  let quits = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
     */
    startBackend: async () => { throw new Error("spawn python EACCES"); },
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
     *
     * Args:
     *     model (BackendRecoveryDialogModel): 需要展示的后端恢复对话框信息。
     */
    showDialog: async (model) => {
      dialogs.push(model);
      return choices[choiceIndex++] ?? "exit";
    },
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
     */
    clearSettingsCache: () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
     */
    quit: () => { quits += 1; },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证重连失败后展示最新错误并继续提供选择。
   */
  () => quits === 1);

  assert.equal(dialogs.length, 2);
  assert.deepEqual(dialogs[1], {
    title: "Ayaya 后端重连失败",
    message: "无法重新启动后端。",
    detail: "spawn python EACCES",
    buttons: ["重连", "退出"],
    defaultId: 0,
    cancelId: 1,
  });
});

test("多次重连失败后仍可继续重试直到成功", /**
 * 验证多次重连失败后仍可继续重试直到成功。
 */
async () => {
  let starts = 0;
  let cacheClears = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
     */
    startBackend: async () => {
      starts += 1;
      if (starts < 3) throw new Error(`失败 ${starts}`);
    },
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
     */
    showDialog: async () => "reconnect",
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
     */
    clearSettingsCache: () => { cacheClears += 1; },
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
     */
    quit: () => undefined,
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证多次重连失败后仍可继续重试直到成功。
   */
  () => cacheClears === 1);

  assert.equal(starts, 3);
});

test("恢复期间的新退出事件排队且不会并发弹框", /**
 * 验证恢复期间的新退出事件排队且不会并发弹框。
 */
async () => {
  let resolveFirstDialog: ((choice: "reconnect" | "exit") => void) | undefined;
  let activeDialogs = 0;
  let maxActiveDialogs = 0;
  let dialogCount = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
     */
    startBackend: async () => undefined,
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
     */
    showDialog: async () => {
      dialogCount += 1;
      activeDialogs += 1;
      maxActiveDialogs = Math.max(maxActiveDialogs, activeDialogs);
      const choice = dialogCount === 1
        ? await new Promise<"reconnect" | "exit">(/**
         * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
         *
         * 此定义用于：记录恢复提示并返回测试指定的用户选择。
         *
         * Args:
         *     resolve ((value: "exit" | "reconnect" | PromiseLike<"exit" | "reconnect">) => void): 准备成功或等待结束时完成 Promise 的函数。
         */
        (resolve) => { resolveFirstDialog = resolve; })
        : "exit";
      activeDialogs -= 1;
      return choice;
    },
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
     */
    clearSettingsCache: () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
     */
    quit: () => undefined,
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
   */
  () => dialogCount === 1);
  coordinator.handleUnexpectedExit({
    exitCode: 2,
    signal: null,
    message: "后端进程意外退出（退出码 2）",
  });
  resolveFirstDialog?.("reconnect");
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证恢复期间的新退出事件排队且不会并发弹框。
   */
  () => dialogCount === 2);

  assert.equal(maxActiveDialogs, 1);
});

test("进入退出流程后忽略对话框结果和后续事件", /**
 * 验证进入退出流程后忽略对话框结果和后续事件。
 */
async () => {
  let resolveDialog: ((choice: "reconnect" | "exit") => void) | undefined;
  let starts = 0;
  let dialogCount = 0;
  const coordinator = createBackendRecoveryCoordinator({
    /**
     * 记录恢复流程发起的启动，并按脚本返回成功或失败。
     *
     * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
     */
    startBackend: async () => { starts += 1; },
    /**
     * 记录恢复提示并返回测试指定的用户选择。
     *
     * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
     */
    showDialog: async () => {
      dialogCount += 1;
      return new Promise<"reconnect" | "exit">(/**
       * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
       *
       * 此定义用于：记录恢复提示并返回测试指定的用户选择。
       *
       * Args:
       *     resolve ((value: "exit" | "reconnect" | PromiseLike<"exit" | "reconnect">) => void): 准备成功或等待结束时完成 Promise 的函数。
       */
      (resolve) => { resolveDialog = resolve; });
    },
    /**
     * 记录恢复成功后清理设置缓存的调用。
     *
     * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
     */
    clearSettingsCache: () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
     */
    quit: () => undefined,
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
     */
    logError: () => undefined,
  });

  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await waitUntil(/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
   */
  () => dialogCount === 1);
  coordinator.beginShutdown();
  resolveDialog?.("reconnect");
  coordinator.handleUnexpectedExit(EXIT_EVENT);
  await new Promise<void>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证进入退出流程后忽略对话框结果和后续事件。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setImmediate(resolve));

  assert.equal(starts, 0);
  assert.equal(dialogCount, 1);
});
