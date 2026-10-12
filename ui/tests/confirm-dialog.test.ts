import assert from "node:assert/strict";
import { test } from "node:test";

import { ConfirmDialog } from "../src/ui/confirm-dialog.js";
import { DIALOG_PRIORITY } from "../src/ui/dialog-focus.js";

/** 提供同一页面的焦点和事件分发，让多个真实确认组件共享一个测试文档。 */
class FakeDocument extends EventTarget {
  activeElement: FakeElement | null = null;
  body = { dataset: {} as Record<string, string> };
}

const testDocument = new FakeDocument();

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
  /** 模拟浏览器焦点变化，禁止聚焦被隐藏、禁用或置于底层的控件。 */
  focus(): void {
    if (this.closest("[hidden], [inert]") || this.disabled) return;
    testDocument.activeElement = this;
    const event = new Event("focusin");
    Object.defineProperty(event, "target", { value: this });
    testDocument.dispatchEvent(event);
  }
  readonly ownerDocument = testDocument;
  readonly dataset: Record<string, string> = {};
  readonly children: FakeElement[] = [];
  parent: FakeElement | null = null;
  hidden = false;
  inert = false;
  disabled = false;
  isConnected = true;
  tabIndex = 0;
  textContent = "";
  readonly classList = new FakeClassList();
  private readonly attributes = new Map<string, string>();

  /**
   * 检查自身或父容器的隐藏及底层模态状态。
   *
   * Args:
   *     selector (string): 组件要求排除的隐藏与 inert 条件。
   *
   * Returns:
   *     FakeElement | null: 命中的不可操作容器。
   */
  closest(selector: string): FakeElement | null {
    if ((selector.includes("hidden") && this.hidden) || (selector.includes("inert") && this.inert)) return this;
    return this.parent?.closest(selector) ?? null;
  }

  /**
   * 检查候选元素是否属于当前确认框。
   *
   * Args:
   *     node (unknown): 当前焦点或事件目标。
   *
   * Returns:
   *     boolean: 位于自身或子控件中时为 true。
   */
  contains(node: unknown): boolean {
    if (node === this) return true;
    for (const child of this.children) if (child.contains(node)) return true;
    return false;
  }

  /**
   * 提供禁用选择器所需状态。
   *
   * Args:
   *     selector (string): 组件查询的控件状态。
   *
   * Returns:
   *     boolean: 控件被禁用时为 true。
   */
  matches(selector: string): boolean { return selector === ":disabled" && this.disabled; }

  /** 返回确认框内可供 Tab 使用的按钮。 */
  querySelectorAll(): FakeElement[] { return this.children; }

  /** 返回当前控件可见时的矩形占位，供模态焦点过滤。 */
  getClientRects(): Array<object> { return this.closest("[hidden]") ? [] : [{}]; }

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
Object.defineProperty(globalThis, "document", { value: testDocument, configurable: true });
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
  dialog.hidden = true;
  cancel.parent = dialog; ok.parent = dialog;
  dialog.children.push(cancel, ok);
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


test("退出确认打开后新到达的普通确认不能抢焦点，Escape 只取消退出", /**
 * 验证两个真实确认组件共用焦点管理：退出优先，取消后恢复底层确认。
 */
async () => {
  const exit = createDialog();
  const approval = createDialog();
  const exiting = exit.instance.open({
    title: "退出", message: "确认退出", confirmText: "退出", cancelText: "取消", variant: "danger",
    priority: DIALOG_PRIORITY.APPLICATION_EXIT,
  });
  const deciding = approval.instance.open({
    title: "审批", message: "确认操作", confirmText: "批准", cancelText: "拒绝", variant: "warning",
  });
  assert.equal(testDocument.activeElement, exit.cancel);
  assert.equal(approval.dialog.inert, true);
  approval.ok.focus();
  assert.equal(testDocument.activeElement, exit.cancel);
  const escape = new Event("keydown", { cancelable: true });
  Object.defineProperty(escape, "key", { value: "Escape" });
  testDocument.dispatchEvent(escape);
  assert.equal(await exiting, false);
  assert.equal(approval.dialog.hidden, false);
  assert.equal(approval.dialog.inert, false);
  assert.equal(testDocument.activeElement, approval.cancel);
  approval.cancel.dispatchEvent(new Event("click"));
  assert.equal(await deciding, false);
});
