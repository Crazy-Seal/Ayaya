import assert from "node:assert/strict";
import { test } from "node:test";

import { ChatHistoryManager } from "../src/chat/chat-history-manager.js";

/**
 * 模拟当前用例需要的 DOM 文本、属性和子元素，避免依赖真实浏览器页面。
 */
class FakeElement {
  className = "";
  textContent = "";
  children: FakeElement[] = [];
  removed = false;

  /**
   * 模拟将子元素加入容器并建立父子引用。
   *
   * 此定义用于：提供 FakeElement 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     child (FakeElement): 本次管理的子进程。
   */
  appendChild(child: FakeElement): FakeElement {
    this.children.push(child);
    return child;
  }

  /**
   * 从测试元素或集合移除指定条目。
   *
   * 此定义用于：提供 FakeElement 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  remove(): void {
    this.removed = true;
  }
}

/**
 * 模拟聊天历史容器及滚动记录，供验证历史重绘与逐句输出。
 */
class FakeContainer extends FakeElement {
  scrollHeight = 100;
  scrollCalls: ScrollToOptions[] = [];
  classList = { /**
   * 把当前条目加入测试元素、集合或执行记录。
   *
   * 此定义用于：提供 FakeContainer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  add() {}, /**
   * 从测试元素或集合移除指定条目。
   *
   * 此定义用于：提供 FakeContainer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  remove() {} };

  /**
   * 模拟页面正文整体更新，供检查历史重绘。
   *
   * 此定义用于：提供 FakeContainer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     _value (string): 本次测试写入的页面值。
   */
  set innerHTML(_value: string) { this.children = []; }
  /**
   * 返回本用例指定的匹配元素集合。
   *
   * 此定义用于：提供 FakeContainer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  querySelectorAll(): FakeElement[] { return []; }

  /**
   * 记录聊天列表滚动操作，供检查增量显示是否跟随底部。
   *
   * 此定义用于：提供 FakeContainer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     options (ScrollToOptions): 调用方提供的配置或运行选项。
   */
  scrollTo(options: ScrollToOptions): void {
    this.scrollCalls.push(options);
  }
}

for (const succeeds of [true, false]) {
  test(`恢复后的历史同步${succeeds ? "成功不重复输出尾句" : "失败保留已完成尾句"}`, /**
   * 验证历史同步成功和失败时的回复显示与去重行为。
   *
   * Args:
   *     t (TestContext): Node 测试上下文，供登记用例结束后恢复全局依赖的清理函数。
   */
  async (t) => {
    const originalDocument = globalThis.document;
    const originalWindow = globalThis.window;
    const originalFrame = globalThis.requestAnimationFrame;
    t.mock.timers.enable({ apis: ["setInterval"] });
    globalThis.document = {
      /**
       * 创建用例所需的页面元素替身。
       *
       * 此定义用于：验证历史同步成功和失败时的回复显示与去重行为。
       */
      createElement: () => new FakeElement(),
      /**
       * 创建只保存正文的测试文本节点。
       *
       * 此定义用于：验证历史同步成功和失败时的回复显示与去重行为。
       *
       * Args:
       *     text (string): 需要显示、输入或转换的文本。
       */
      createTextNode: (text: string) => Object.assign(new FakeElement(), { textContent: text }),
    } as unknown as Document;
    globalThis.requestAnimationFrame = /**
     * 由浏览器在requestAnimationFrame时调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证历史同步成功和失败时的回复显示与去重行为。
     *
     * Args:
     *     callback (FrameRequestCallback): 触发后交给调用方继续处理的回调。
     */
    (callback) => { callback(0); return 0; };
    globalThis.window = { desktopPetApi: {
      /**
       * 返回测试历史快照或失败，供检查流式正文与历史同步。
       *
       * 此定义用于：验证历史同步成功和失败时的回复显示与去重行为。
       */
      async getChatHistoryLastN() {
        if (!succeeds) throw new Error("历史离线");
        return [{ role: "ai", content: "完成。", timestamp: "2026-09-08T00:00:00Z" }];
      },
    } } as unknown as Window & typeof globalThis;
    const container = new FakeContainer();
    const manager = new ChatHistoryManager(container as unknown as HTMLDivElement);
    try {
      manager.startStreaming();
      manager.updateLastAiMessage("完成。");
      manager.finalizeStreamingMessage();
      if (succeeds) await manager.loadHistory("test");
      else await assert.rejects(manager.loadHistory("test"), /历史离线/);
      t.mock.timers.tick(1000);
      assert.equal(container.children.length, 1);
    } finally {
      manager.abortStreaming();
      globalThis.document = originalDocument;
      globalThis.window = originalWindow;
      globalThis.requestAnimationFrame = originalFrame;
    }
  });
}

test("流结束后仍以 500ms 间隔逐条追加并滚动到底部", /**
 * 验证流结束后仍以 500ms 间隔逐条追加并滚动到底部。
 */
() => {
  const originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const container = new FakeContainer();
  let intervalCallback: (() => void) | undefined;
  globalThis.document = {
    /**
     * 创建用例所需的页面元素替身。
     *
     * 此定义用于：验证流结束后仍以 500ms 间隔逐条追加并滚动到底部。
     */
    createElement: () => new FakeElement(),
  } as unknown as Document;
  globalThis.setInterval = (/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证流结束后仍以 500ms 间隔逐条追加并滚动到底部。
   *
   * Args:
   *     callback (() => void): 触发后交给调用方继续处理的回调。
   *     delay (number): 用户滚动意图保持的毫秒数。
   */
  (callback: () => void, delay: number) => {
    assert.equal(delay, 500);
    intervalCallback = callback;
    return 1;
  }) as unknown as typeof setInterval;
  globalThis.clearInterval = (/**
   * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
   *
   * 此定义用于：验证流结束后仍以 500ms 间隔逐条追加并滚动到底部。
   */
  () => {}) as typeof clearInterval;

  try {
    const manager = new ChatHistoryManager(container as unknown as HTMLDivElement);
    manager.startStreaming();
    manager.updateLastAiMessage("第一句。第二句。第三句。");
    manager.finalizeStreamingMessage();

    assert.equal(container.children.length, 0);

    intervalCallback?.();
    assert.equal(container.children.length, 1);
    assert.equal(container.scrollCalls.length, 1);

    intervalCallback?.();
    assert.equal(container.children.length, 2);
    assert.equal(container.scrollCalls.length, 2);

    intervalCallback?.();
    assert.equal(container.children.length, 3);
    assert.equal(container.scrollCalls.length, 3);

    manager.startStreaming();
    assert.equal(container.children.some(/**
     * 由集合的 some 遍历每项数据时调用，读取 child.removed 供当前步骤继续处理。
     *
     * 此定义用于：验证流结束后仍以 500ms 间隔逐条追加并滚动到底部。
     *
     * Args:
     *     child (FakeElement): 本次管理的子进程。
     */
    (child) => child.removed), false);
    manager.abortStreaming();
  } finally {
    globalThis.document = originalDocument;
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test("上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复", /**
 * 验证上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复。
 */
() => {
  const originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const container = new FakeContainer();
  let intervalCallback: (() => void) | undefined;
  globalThis.document = {
    /**
     * 创建用例所需的页面元素替身。
     *
     * 此定义用于：验证上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复。
     */
    createElement: () => new FakeElement(),
  } as unknown as Document;
  globalThis.setInterval = (/**
   * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复。
   *
   * Args:
   *     callback (() => void): 触发后交给调用方继续处理的回调。
   */
  (callback: () => void) => {
    intervalCallback = callback;
    return 1;
  }) as unknown as typeof setInterval;
  globalThis.clearInterval = (/**
   * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
   *
   * 此定义用于：验证上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复。
   */
  () => {}) as typeof clearInterval;

  try {
    const manager = new ChatHistoryManager(container as unknown as HTMLDivElement);
    manager.startStreaming();
    manager.updateLastAiMessage("第一句。第二句。第三句。");
    manager.finalizeStreamingMessage();

    intervalCallback?.();
    assert.equal(container.children.length, 1);

    manager.startStreaming();

    assert.equal(container.children.length, 3);
    assert.equal(container.scrollCalls.length, 3);
    assert.equal(container.children.some(/**
     * 由集合的 some 遍历每项数据时调用，读取 child.removed 供当前步骤继续处理。
     *
     * 此定义用于：验证上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复。
     *
     * Args:
     *     child (FakeElement): 本次管理的子进程。
     */
    (child) => child.removed), false);
    manager.abortStreaming();
  } finally {
    globalThis.document = originalDocument;
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test("快照历史已经包含的真人消息不会被增量事件重复显示", /**
 * 验证快照历史已经包含的真人消息不会被增量事件重复显示。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalFrame = globalThis.requestAnimationFrame;
  globalThis.document = {
    /**
     * 创建隔离的测试元素。
     *
     * Returns:
     *     FakeElement: 当前用例的 DOM 元素替身。
     */
    createElement: () => new FakeElement(),
    /**
     * 创建用于断言历史内容的测试文本节点。
     *
     * Args:
     *     text (string): 用于构造测试文本节点的正文。
     *
     * Returns:
     *     FakeElement & { textContent: string; }: 包含测试正文的文本节点替身。
     */
    createTextNode: (text: string) => Object.assign(new FakeElement(), { textContent: text }),
  } as unknown as Document;
  globalThis.requestAnimationFrame = /**
   * 由浏览器在requestAnimationFrame时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证快照历史已经包含的真人消息不会被增量事件重复显示。
   *
   * Args:
   *     callback (FrameRequestCallback): 触发后交给调用方继续处理的回调。
   */
  (callback) => { callback(0); return 0; };
  globalThis.window = { desktopPetApi: {
    /**
     * 返回带领域消息标识的测试历史。
     *
     * Returns:
     *     Promise<{ id: string; role: string; content: string; timestamp: string; }[]>: 本用例准备的最近可见历史消息。
     */
    async getChatHistoryLastN() {
      return [{ id: "message-1", role: "human", content: "你好", timestamp: "2026-09-29" }];
    },
  } } as unknown as Window & typeof globalThis;
  try {
    const container = new FakeContainer();
    const manager = new ChatHistoryManager(container as unknown as HTMLDivElement);
    await manager.loadHistory("s");
    manager.addMessage({ id: "message-1", role: "human", content: "你好", timestamp: "2026-09-29" });
    assert.equal(container.children.length, 1);
  } finally {
    globalThis.document = originalDocument;
    globalThis.window = originalWindow;
    globalThis.requestAnimationFrame = originalFrame;
  }
});
