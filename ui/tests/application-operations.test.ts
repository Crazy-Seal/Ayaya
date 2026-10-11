import assert from "node:assert/strict";
import { test } from "node:test";
import { createApplicationOperations, SHUTDOWN_STATE_CHANNEL } from "../electron/application-operations.js";
import { createApplicationShutdownCoordinator } from "../electron/application-shutdown.js";

test("正常运行允许操作，退出后有返回值请求拒绝、按钮事件忽略，状态查询仍可执行", /** 验证业务与日志请求在同一个入口被关闭。 */
() => {
  const gate = createApplicationOperations(); let executions = 0;
  /** 记录业务回调是否真正执行。 */
  const execute = (): number => ++executions;
  assert.equal(gate.invoke("desktop-pet:save-chat-settings", execute), 1);
  gate.send("desktop-pet:open-log-window", execute); assert.equal(executions, 2);
  assert.equal(gate.beginShutdown(), true); assert.equal(gate.beginShutdown(), false);
  for (const channel of ["desktop-pet:save-chat-settings", "desktop-pet:submit-command", "desktop-pet:set-chat-pinned", "desktop-pet:clear-log-buffer"]) {
    assert.throws(/** 当前处理器已注册，但退出门禁阻止它执行。 */
    () => gate.invoke(channel, execute), /应用正在退出/);
  }
  for (const channel of ["desktop-pet:open-log-window", "desktop-pet:begin-window-gesture", "desktop-pet:exit-application"]) gate.send(channel, execute);
  assert.equal(executions, 2);
  assert.equal(gate.invoke(SHUTDOWN_STATE_CHANNEL, /** 迟加载页面查询不可撤销的退出状态。 */ () => gate.isShuttingDown()), true);
});

test("等待后端退出期间不执行新操作，停止完成后只刷新日志和退出一次", /** 模拟用户确认后后端仍未释放的窗口，检查整个等待区间的门禁。 */
async () => {
  const gate = createApplicationOperations(); const order: string[] = [];
  let releaseBackend!: () => void;
  const stopped = new Promise<void>(/**
   * 保存模拟后端停止完成的通知，测试可在任意时点解除等待。
   * Args:
   *     resolve (() => void): 后端停止完成时调用。
   */
  (resolve) => { releaseBackend = resolve; });
  const coordinator = createApplicationShutdownCoordinator({
    /** 模拟主进程开始清理前已同步关闭门禁。 */
    beginBackendRecoveryShutdown: () => { gate.beginShutdown(); order.push("locked"); },
    /** 记录停止鼠标采样。 */
    stopCursorTracking: () => { order.push("cursor-stopped"); },
    /** 返回尚未停止的模拟后端，不启动真实进程。 */
    stopBackend: () => { order.push("backend-stopping"); return stopped; },
    /** 记录后端停止后才刷新日志。 */
    closeLogs: async () => { order.push("logs-flushed"); },
    /** 记录资源清理结束后的实际退出。 */
    quit: () => { order.push("quit"); },
    /** 成功路径不应产生清理错误。 */
    logError: () => assert.fail("不应发生退出错误"),
  });
  const shutdown = coordinator.requestShutdown();
  assert.equal(coordinator.isComplete(), false);
  assert.throws(/** 等待后端停止时，用户新发送的命令不能交付。 */
  () => gate.invoke("desktop-pet:submit-command", /** 业务不应执行。 */ () => order.push("new-command")), /应用正在退出/);
  gate.send("desktop-pet:open-log-window", /** 新窗口不应打开。 */ () => order.push("new-window"));
  assert.equal(coordinator.requestShutdown(), shutdown);
  assert.deepEqual(order, ["locked", "cursor-stopped", "backend-stopping"]);
  releaseBackend(); await shutdown;
  assert.deepEqual(order, ["locked", "cursor-stopped", "backend-stopping", "logs-flushed", "quit"]);
  assert.equal(gate.isShuttingDown(), true);
});
