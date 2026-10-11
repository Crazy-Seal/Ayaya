import assert from "node:assert/strict";
import { test } from "node:test";

import { ConfirmDialog } from "../src/settings/components/confirm-dialog.js";

/**
 * 模拟元素的样式类集合，供检查显示、隐藏及状态切换。
 */
class FakeClassList {
  private readonly values = new Set<string>();

  /**
   * 把当前条目加入测试元素、集合或执行记录。
   *
   * 此定义用于：提供 FakeClassList 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     tokens (string[]): 需要添加或移除的测试样式类名称。
   */
  add(...tokens: string[]): void {
    tokens.forEach(/**
     * 由集合的 forEach 遍历每项数据时调用，登记当前条目。
     *
     * 此定义用于：把当前条目加入测试元素、集合或执行记录。
     *
     * Args:
     *     token (string): 只用于本机后端通信的认证令牌。
     */
    (token) => this.values.add(token));
  }

  /**
   * 从测试元素或集合移除指定条目。
   *
   * 此定义用于：提供 FakeClassList 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     tokens (string[]): 需要添加或移除的测试样式类名称。
   */
  remove(...tokens: string[]): void {
    tokens.forEach(/**
     * 由集合的 forEach 遍历每项数据时调用，移除该项已登记记录。
     *
     * 此定义用于：从测试元素或集合移除指定条目。
     *
     * Args:
     *     token (string): 只用于本机后端通信的认证令牌。
     */
    (token) => this.values.delete(token));
  }

  /**
   * 查询测试样式集合是否包含当前类名。
   *
   * 此定义用于：提供 FakeClassList 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     token (string): 只用于本机后端通信的认证令牌。
   */
  contains(token: string): boolean {
    return this.values.has(token);
  }
}

/**
 * 模拟当前用例需要的 DOM 文本、属性和子元素，避免依赖真实浏览器页面。
 */
class FakeElement extends EventTarget {
  /** 模拟确认框打开时聚焦取消按钮。 */
  focus(): void {}
  hidden = true;
  textContent = "";
  readonly classList = new FakeClassList();
  private readonly attributes = new Map<string, string>();

  /**
   * 记录页面属性写入，供检查组件状态。
   *
   * 此定义用于：提供 FakeElement 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     name (string): 当前对象的名称。
   *     value (string): 当前待检查或转换的值。
   */
  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }

  /**
   * 读取当前测试元素保存的属性。
   *
   * 此定义用于：提供 FakeElement 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     name (string): 当前对象的名称。
   */
  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null;
  }
}

// 1. 确认框焦点管理读取浏览器当前焦点；本文件只测试文案和选择，初始没有焦点。
Object.defineProperty(globalThis, "document", { value: { activeElement: null }, configurable: true });
Object.defineProperty(globalThis, "HTMLElement", { value: FakeElement, configurable: true });

/**
 * 创建带可替换页面元素的确认框，供测试等待用户选择。
 */
const createDialog = () => {
  const dialog = new FakeElement();
  const cancel = new FakeElement();
  const ok = new FakeElement();
  const title = new FakeElement();
  const message = new FakeElement();
  const instance = new ConfirmDialog(
    dialog as unknown as HTMLDivElement,
    cancel as unknown as HTMLButtonElement,
    ok as unknown as HTMLButtonElement,
    title as unknown as HTMLElement,
    message as unknown as HTMLElement
  );
  return { instance, dialog, cancel, ok, title, message };
};

test("确认框可动态展示 HTTP 风险警告", /**
 * 验证确认框可动态展示 HTTP 风险警告。
 */
async () => {
  const { instance, dialog, ok, title, message } = createDialog();
  const result = instance.open({
    title: "未加密连接警告",
    message: "API Key 将发送到 http://example.com/v1",
    confirmText: "仍然保存",
    cancelText: "取消",
    variant: "warning",
  });

  assert.equal(dialog.hidden, false);
  assert.equal(dialog.getAttribute("aria-hidden"), "false");
  assert.equal(title.textContent, "未加密连接警告");
  assert.match(message.textContent, /http:\/\/example\.com\/v1/);
  assert.equal(ok.textContent, "仍然保存");
  assert.equal(ok.classList.contains("confirm-btn-warning"), true);
  assert.equal(ok.classList.contains("ui-button--warning"), true);

  ok.dispatchEvent(new Event("click"));
  assert.equal(await result, true);
  assert.equal(dialog.hidden, true);
});

test("确认框取消时返回 false，并切换回危险操作样式", /**
 * 验证确认框取消时返回 false，并切换回危险操作样式。
 */
async () => {
  const { instance, cancel, ok } = createDialog();
  const result = instance.open({
    title: "删除模型确认",
    message: "删除后无法恢复",
    confirmText: "确认删除",
    cancelText: "取消",
    variant: "danger",
  });

  assert.equal(ok.classList.contains("confirm-btn-danger"), true);
  assert.equal(ok.classList.contains("confirm-btn-warning"), false);
  assert.equal(ok.classList.contains("ui-button--danger"), true);
  assert.equal(ok.classList.contains("ui-button--warning"), false);
  cancel.dispatchEvent(new Event("click"));
  assert.equal(await result, false);
});
