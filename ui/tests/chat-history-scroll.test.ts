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
