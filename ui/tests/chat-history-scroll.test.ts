import assert from "node:assert/strict";
import { test } from "node:test";

import { ChatHistoryManager } from "../src/chat/chat-history-manager.js";

class FakeElement {
  className = "";
  textContent = "";
  children: FakeElement[] = [];
  removed = false;

  appendChild(child: FakeElement): FakeElement {
    this.children.push(child);
    return child;
  }

  remove(): void {
    this.removed = true;
  }
}

class FakeContainer extends FakeElement {
  scrollHeight = 100;
  scrollCalls: ScrollToOptions[] = [];
  classList = { add() {}, remove() {} };

  set innerHTML(_value: string) { this.children = []; }
  querySelectorAll(): FakeElement[] { return []; }

  scrollTo(options: ScrollToOptions): void {
    this.scrollCalls.push(options);
  }
}

for (const succeeds of [true, false]) {
  test(`恢复后的历史同步${succeeds ? "成功不重复输出尾句" : "失败保留已完成尾句"}`, async (t) => {
    const originalDocument = globalThis.document;
    const originalWindow = globalThis.window;
    const originalFrame = globalThis.requestAnimationFrame;
    t.mock.timers.enable({ apis: ["setInterval"] });
    globalThis.document = {
      createElement: () => new FakeElement(),
      createTextNode: (text: string) => Object.assign(new FakeElement(), { textContent: text }),
    } as unknown as Document;
    globalThis.requestAnimationFrame = (callback) => { callback(0); return 0; };
    globalThis.window = { desktopPetApi: {
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

test("流结束后仍以 500ms 间隔逐条追加并滚动到底部", () => {
  const originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const container = new FakeContainer();
  let intervalCallback: (() => void) | undefined;
  globalThis.document = {
    createElement: () => new FakeElement(),
  } as unknown as Document;
  globalThis.setInterval = ((callback: () => void, delay: number) => {
    assert.equal(delay, 500);
    intervalCallback = callback;
    return 1;
  }) as unknown as typeof setInterval;
  globalThis.clearInterval = (() => {}) as typeof clearInterval;

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
    assert.equal(container.children.some((child) => child.removed), false);
    manager.abortStreaming();
  } finally {
    globalThis.document = originalDocument;
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test("上一轮尾句尚未显示完时启动新一轮会先完整输出旧回复", () => {
  const originalDocument = globalThis.document;
  const originalSetInterval = globalThis.setInterval;
  const originalClearInterval = globalThis.clearInterval;
  const container = new FakeContainer();
  let intervalCallback: (() => void) | undefined;
  globalThis.document = {
    createElement: () => new FakeElement(),
  } as unknown as Document;
  globalThis.setInterval = ((callback: () => void) => {
    intervalCallback = callback;
    return 1;
  }) as unknown as typeof setInterval;
  globalThis.clearInterval = (() => {}) as typeof clearInterval;

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
    assert.equal(container.children.some((child) => child.removed), false);
    manager.abortStreaming();
  } finally {
    globalThis.document = originalDocument;
    globalThis.setInterval = originalSetInterval;
    globalThis.clearInterval = originalClearInterval;
  }
});

test("快照历史已经包含的真人消息不会被增量事件重复显示", /**
 * 验证快照历史已经包含的真人消息不会被增量事件重复显示。
 *
 * Returns:
 *     Promise<void>: 当前异步处理完成的等待结果。
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
     *     FakeElement: 当前查询、回调或视图操作的结果。
     */
    createElement: () => new FakeElement(),
    /**
     * 创建用于断言历史内容的测试文本节点。
     *
     * Args:
     *     text (string): 用于构造测试文本节点的正文。
     *
     * Returns:
     *     FakeElement & { textContent: string; }: 当前查询、回调或视图操作的结果。
     */
    createTextNode: (text: string) => Object.assign(new FakeElement(), { textContent: text }),
  } as unknown as Document;
  globalThis.requestAnimationFrame = /**
   * 提供当前测试场景使用的 callback 替身结果。
   *
   * Args:
   *     callback (FrameRequestCallback): 当前定义执行的回调。
   *
   * Returns:
   *     number: 当前查询、回调或视图操作的结果。
   */
  (callback) => { callback(0); return 0; };
  globalThis.window = { desktopPetApi: {
    /**
     * 返回带领域消息标识的测试历史。
     *
     * Returns:
     *     Promise<{ id: string; role: string; content: string; timestamp: string; }[]>: 当前查询、回调或视图操作的结果。
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
