import assert from "node:assert/strict";
import { test } from "node:test";

import { createApplicationShutdownCoordinator } from "../electron/application-shutdown.js";

test("重复关闭请求只停止后端并退出一次", /**
 * 验证重复关闭请求只停止后端并退出一次。
 */
async () => {
  let beginShutdownCalls = 0;
  let stopCursorCalls = 0;
  let stopBackendCalls = 0;
  let quitCalls = 0;
  let closeLogsCalls = 0;
  let releaseBackend: (() => void) | undefined;
  const backendStopped = new Promise<void>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证重复关闭请求只停止后端并退出一次。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => {
    releaseBackend = resolve;
  });
  const coordinator = createApplicationShutdownCoordinator({
    /**
     * 记录恢复流程已经停止接收新操作，供检查关闭顺序。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    beginBackendRecoveryShutdown: () => { beginShutdownCalls += 1; },
    /**
     * 记录鼠标位置采样停止，供检查资源关闭顺序。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    stopCursorTracking: () => { stopCursorCalls += 1; },
    /**
     * 记录后端关闭请求，并按用例返回成功或失败。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    stopBackend: async () => {
      stopBackendCalls += 1;
      await backendStopped;
    },
    /**
     * 记录日志关闭时机，并按用例返回成功或失败。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    closeLogs: async () => { closeLogsCalls += 1; },
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    quit: () => { quitCalls += 1; },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证重复关闭请求只停止后端并退出一次。
     */
    logError: () => undefined,
  });

  const first = coordinator.requestShutdown();
  const second = coordinator.requestShutdown();
  assert.equal(first, second);
  assert.equal(stopBackendCalls, 1);
  releaseBackend?.();
  await first;

  assert.equal(beginShutdownCalls, 1);
  assert.equal(stopCursorCalls, 1);
  assert.equal(stopBackendCalls, 1);
  assert.equal(quitCalls, 1);
  assert.equal(closeLogsCalls, 1);
  assert.equal(coordinator.isComplete(), true);
});

test("后端关闭失败仍完成应用退出", /**
 * 验证后端关闭失败仍完成应用退出。
 */
async () => {
  const logged: unknown[] = [];
  let quitCalls = 0;
  const coordinator = createApplicationShutdownCoordinator({
    /**
     * 记录恢复流程已经停止接收新操作，供检查关闭顺序。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     */
    beginBackendRecoveryShutdown: () => undefined,
    /**
     * 记录鼠标位置采样停止，供检查资源关闭顺序。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     */
    stopCursorTracking: () => undefined,
    /**
     * 记录后端关闭请求，并按用例返回成功或失败。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     */
    stopBackend: async () => {
      throw new Error("shutdown failed");
    },
    /**
     * 记录日志关闭时机，并按用例返回成功或失败。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     */
    closeLogs: async () => undefined,
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     */
    quit: () => { quitCalls += 1; },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证后端关闭失败仍完成应用退出。
     *
     * Args:
     *     _message (string): 当前需要显示或记录的文本。
     *     error (unknown): 本次捕获的异常。
     */
    logError: (_message, error) => logged.push(error),
  });

  await coordinator.requestShutdown();

  assert.equal(quitCalls, 1);
  assert.equal(logged.length, 1);
});

test("后端停止后再刷新日志，日志失败仍允许退出", /**
 * 验证后端停止后再刷新日志，日志失败仍允许退出。
 */
async () => {
  const order: string[] = [];
  const coordinator = createApplicationShutdownCoordinator({
    /**
     * 记录恢复流程已经停止接收新操作，供检查关闭顺序。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    beginBackendRecoveryShutdown: () => undefined,
    /**
     * 记录鼠标位置采样停止，供检查资源关闭顺序。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    stopCursorTracking: () => undefined,
    /**
     * 记录后端关闭请求，并按用例返回成功或失败。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    stopBackend: async () => { order.push("backend"); },
    /**
     * 记录日志关闭时机，并按用例返回成功或失败。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    closeLogs: async () => {
      order.push("logs");
      throw new Error("log flush failed");
    },
    /**
     * 记录应用退出，供检查只退出一次。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    quit: () => { order.push("quit"); },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：验证后端停止后再刷新日志，日志失败仍允许退出。
     */
    logError: () => undefined,
  });

  await coordinator.requestShutdown();

  assert.deepEqual(order, ["backend", "logs", "quit"]);
});
