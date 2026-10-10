import assert from "node:assert/strict";
import { test } from "node:test";

import { installNavigationGuards } from "../electron/navigation-guards.js";

type NavigationListener = (event: { /**
 * 记录浏览器默认操作已被阻止。
 */
preventDefault(): void }, url: string) => void;

/**
 * 保存测试导航与新窗口处理器，供用例触发并验证可信来源限制。
 */
class FakeWebContents {
  readonly listeners = new Map<string, NavigationListener>();
  openHandler: ((details: { url: string }) => { action: "deny" }) | null = null;

  /**
   * 记录导航事件监听函数，供用例手动触发。
   *
   * 此定义用于：提供 FakeWebContents 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     event (string): 调用方或浏览器触发的当前事件。
   *     listener (NavigationListener): 接收后续事件的回调，取消订阅后不再调用。
   */
  on(event: string, listener: NavigationListener): void {
    this.listeners.set(event, listener);
  }

  /**
   * 保存测试窗口打开处理器，供用例检查拒绝规则。
   *
   * 此定义用于：提供 FakeWebContents 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     handler ((details: { url: string }) => { action: "deny" }): 来源验证成功后执行的 IPC 处理函数。
   */
  setWindowOpenHandler(handler: (details: { url: string }) => { action: "deny" }): void {
    this.openHandler = handler;
  }
}

test("导航守卫阻止不可信导航和重定向", /**
 * 验证导航守卫阻止不可信导航和重定向。
 */
() => {
  const webContents = new FakeWebContents();
  const blocked: Array<{ kind: string; url: string }> = [];
  installNavigationGuards(
    webContents,
    /**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证导航守卫阻止不可信导航和重定向。
     *
     * Args:
     *     url (string): 待检查或打开的地址。
     */
    (url) => url.startsWith("file:///trusted/"),
    /**
     * 由外层操作在对应处理阶段调用，追加当前记录。
     *
     * 此定义用于：验证导航守卫阻止不可信导航和重定向。
     *
     * Args:
     *     kind (BlockedNavigationKind): 被阻止的导航类别。
     *     url (string): 待检查或打开的地址。
     */
    (kind, url) => blocked.push({ kind, url })
  );

  let prevented = false;
  webContents.listeners.get("will-navigate")?.({ /**
   * 记录浏览器默认操作已被阻止。
   *
   * 此定义用于：验证导航守卫阻止不可信导航和重定向。
   */
  preventDefault: () => { prevented = true; } }, "https://evil.example/");
  assert.equal(prevented, true);

  prevented = false;
  webContents.listeners.get("will-redirect")?.({ /**
   * 记录浏览器默认操作已被阻止。
   *
   * 此定义用于：验证导航守卫阻止不可信导航和重定向。
   */
  preventDefault: () => { prevented = true; } }, "file:///trusted/index.html");
  assert.equal(prevented, false);
  assert.deepEqual(blocked, [{ kind: "导航", url: "https://evil.example/" }]);
});

test("导航守卫拒绝 renderer 创建新窗口", /**
 * 验证导航守卫拒绝 renderer 创建新窗口。
 */
() => {
  const webContents = new FakeWebContents();
  const blocked: Array<{ kind: string; url: string }> = [];
  installNavigationGuards(
    webContents,
    /**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证导航守卫拒绝 renderer 创建新窗口。
     */
    () => true,
    /**
     * 由外层操作在对应处理阶段调用，追加当前记录。
     *
     * 此定义用于：验证导航守卫拒绝 renderer 创建新窗口。
     *
     * Args:
     *     kind (BlockedNavigationKind): 被阻止的导航类别。
     *     url (string): 待检查或打开的地址。
     */
    (kind, url) => blocked.push({ kind, url })
  );

  assert.deepEqual(webContents.openHandler?.({ url: "https://example.com/" }), { action: "deny" });
  assert.deepEqual(blocked, [{ kind: "新窗口", url: "https://example.com/" }]);
});
