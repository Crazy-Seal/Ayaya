import assert from "node:assert/strict";
import { test } from "node:test";

import { confirmApplicationExit } from "../src/ui/application-exit.js";
import type { ConfirmDialogOptions } from "../src/ui/confirm-dialog.js";

for (const [activeCount, bootCount] of [[0, 0], [3, 0], [3, 3], [5, 3]]) {
  test(`退出始终确认，按普通任务选择文案（活动 ${activeCount}，启动 ${bootCount}）`, /**
   * 通过页面公开退出方法验证提示选择，取消不会向主进程请求关闭。
   */
  async () => {
    const previousWindow = globalThis.window;
    const dialogs: ConfirmDialogOptions[] = [];
    let exits = 0;
    globalThis.window = {
      desktopPetApi: {
        /** 返回全应用任务数量，数量包含其他会话和静默任务。 */
        getActiveTaskSummary: async () => ({ active_count: activeCount, application_boot_active_count: bootCount }),
        /** 记录实际提交的退出请求。 */
        requestApplicationExit: async () => { exits += 1; },
      },
    } as unknown as Window & typeof globalThis;
    try {
      await confirmApplicationExit({
        /** 记录真实页面选择的提示内容，并模拟用户取消。 */
        open: async (options) => { dialogs.push(options); return false; },
      });
      assert.equal(exits, 0);
      assert.equal(dialogs.length, 1);
      assert.equal(dialogs[0].title, "退出 Ayaya");
      assert.equal(dialogs[0].confirmText, "退出");
      assert.equal(dialogs[0].cancelText, "取消");
      if (activeCount > bootCount) {
        assert.equal(dialogs[0].message, "当前有正在进行的后台任务，确认要退出吗？");
        assert.equal(dialogs[0].variant, "warning");
      } else {
        assert.equal(dialogs[0].message, "确认要退出吗？");
        assert.equal(dialogs[0].variant, "danger");
      }
    } finally {
      globalThis.window = previousWindow;
    }
  });
}

for (const lateResult of ["success", "error"] as const) {
  test(`查询到两秒时提示人工确认，迟到${lateResult === "success" ? "结果" : "错误"}不再改变选择`, /**
   * 通过真实页面方法核对等待上限，用户取消后迟到查询不会重新弹框或触发退出。
   * Args:
   *     context (TestContext): 提供模拟计时器的测试上下文。
   */
  async (context) => {
    context.mock.timers.enable({ apis: ["setTimeout"] });
    const previousWindow = globalThis.window;
    const dialogs: ConfirmDialogOptions[] = [];
    let exits = 0;
    let resolveQuery!: (value: { active_count: number; application_boot_active_count: number }) => void;
    let rejectQuery!: (reason: Error) => void;
    const query = new Promise<{ active_count: number; application_boot_active_count: number }>(/**
     * 保留查询的完成和失败回调，模拟超时后的迟到响应。
     * Args:
     *     resolve ((value: { active_count: number; application_boot_active_count: number }) => void): 返回活动任务数量。
     *     reject ((reason: Error) => void): 报告查询失败。
     */
    (resolve, reject) => { resolveQuery = resolve; rejectQuery = reject; });
    globalThis.window = {
      desktopPetApi: {
        /** 保持查询等待，由测试控制返回时间。 */
        getActiveTaskSummary: () => query,
        /** 记录页面是否真正请求退出。 */
        requestApplicationExit: async () => { exits += 1; },
      },
    } as unknown as Window & typeof globalThis;
    try {
      const flow = confirmApplicationExit({
        /** 记录确认文案，模拟用户取消退出。 */
        open: async (options) => { dialogs.push(options); return false; },
      });
      context.mock.timers.tick(1999);
      await Promise.resolve();
      assert.equal(dialogs.length, 0);
      context.mock.timers.tick(1);
      await flow;
      assert.equal(dialogs.length, 1);
      assert.equal(dialogs[0].title, "退出 Ayaya");
      assert.equal(dialogs[0].message, "当前后台任务状态查询出错，退出前请人工确认后台任务情况。");
      assert.equal(dialogs[0].cancelText, "取消");
      assert.equal(dialogs[0].confirmText, "退出");
      assert.equal(dialogs[0].variant, "warning");
      if (lateResult === "success") resolveQuery({ active_count: 3, application_boot_active_count: 0 });
      else rejectQuery(new Error("后端迟到的查询错误"));
      await Promise.resolve();
      context.mock.timers.tick(2000);
      await Promise.resolve();
      assert.equal(dialogs.length, 1);
      assert.equal(exits, 0);
    } finally {
      globalThis.window = previousWindow;
    }
  });
}

test("两秒内返回任务数量时使用查询结果，不显示查询出错提示", /**
 * 查询接近等待上限时完成，随后到期的计时器不会追加确认框。
 * Args:
 *     context (TestContext): 提供模拟计时器的测试上下文。
 */
async (context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  const previousWindow = globalThis.window;
  const dialogs: ConfirmDialogOptions[] = [];
  let resolveQuery!: (value: { active_count: number; application_boot_active_count: number }) => void;
  globalThis.window = {
    desktopPetApi: {
      /** 由测试在等待预算耗尽前返回数量。 */
      getActiveTaskSummary: () => new Promise<{ active_count: number; application_boot_active_count: number }>(/**
       * 保存本次查询的完成回调。
       * Args:
       *     resolve ((value: { active_count: number; application_boot_active_count: number }) => void): 返回查询数量。
       */
      (resolve) => { resolveQuery = resolve; }),
    },
  } as unknown as Window & typeof globalThis;
  try {
    const flow = confirmApplicationExit({
      /** 记录查询成功后的确认框并取消退出。 */
      open: async (options) => { dialogs.push(options); return false; },
    });
    context.mock.timers.tick(1999);
    resolveQuery({ active_count: 3, application_boot_active_count: 0 });
    await flow;
    context.mock.timers.tick(2000);
    assert.equal(dialogs.length, 1);
    assert.equal(dialogs[0].message, "当前有正在进行的后台任务，确认要退出吗？");
  } finally {
    globalThis.window = previousWindow;
  }
});

for (const [queryFails, confirmed] of [[true, false], [false, false], [false, true]]) {
  test(`${queryFails ? "查询报错" : "只有启动任务"}时保留确认，用户${confirmed ? "确认才请求退出" : "取消保留应用"}`, /**
   * 检查查询失败或只有启动任务时的确认文案，确认后使用真实页面锁定流程。
   */
  async () => {
    const previousWindow = globalThis.window;
    const dialogs: ConfirmDialogOptions[] = [];
    let exits = 0;
    const body = { dataset: {}, children: [] };
    const target = Object.assign(new EventTarget(), {
      document: {
        body,
        /** 当前页面无需额外退出提示元素，使用 body 锁定交互。 */
        querySelector: () => null,
        /** 当前测试页面没有其他输入控件。 */
        querySelectorAll: () => [],
      },
      desktopPetApi: {
        /** 模拟后端不可达，或仅剩启动监听任务。 */
        getActiveTaskSummary: async () => {
          if (queryFails) throw new Error("后端不可达");
          return { active_count: 3, application_boot_active_count: 3 };
        },
        /** 主进程仍未收到其他退出请求。 */
        getApplicationShutdownState: async () => false,
        /** 记录用户确认后实际提交的退出请求。 */
        requestApplicationExit: async () => { exits += 1; },
      },
    });
    globalThis.window = target as unknown as Window & typeof globalThis;
    try {
      await confirmApplicationExit({
        /** 记录真实页面文案，并返回本次测试指定的用户选择。 */
        open: async (options) => { dialogs.push(options); return confirmed; },
      });
      assert.equal(dialogs.length, 1);
      assert.equal(dialogs[0].title, "退出 Ayaya");
      assert.equal(dialogs[0].message, queryFails
        ? "当前后台任务状态查询出错，退出前请人工确认后台任务情况。"
        : "确认要退出吗？");
      assert.equal(dialogs[0].cancelText, "取消");
      assert.equal(dialogs[0].confirmText, "退出");
      assert.equal(exits, confirmed ? 1 : 0);
      assert.equal((body.dataset as Record<string, string>).applicationShutdown, confirmed ? "true" : undefined);
    } finally {
      target.dispatchEvent(new Event("beforeunload"));
      globalThis.window = previousWindow;
    }
  });
}
