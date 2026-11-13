import assert from "node:assert/strict";
import { test } from "node:test";
import { createApplicationShutdownUi } from "../src/ui/application-shutdown.js";

/** 为退出界面模拟必要的元素属性，记录禁用、焦点、子元素与无障碍状态。 */
class Element {
  children: Element[] = [];
  dataset: Record<string, string> = {};
  attributes: Record<string, string> = {};
  disabled = false;
  inert = false;
  focused = false;
  blurred = false;
  className = "";
  textContent = "";
  tabIndex = 0;
  /**
   * 保存退出提示新增的子元素。
   * Args:
   *     children (Element[]): 新增的提示或窗口内容。
   */
  append(...children: Element[]): void { this.children.push(...children); }
  /**
   * 保存提示的角色与实时播报属性。
   * Args:
   *     name, value (string): 属性名称与值。
   */
  setAttribute(name: string, value: string): void { this.attributes[name] = value; }
  /** 判断控件是否属于退出提示。 */
  contains(element: Element): boolean { return this === element || this.children.some(/** 递归查找当前提示内的控件。 */ (child) => child.contains(element)); }
  /** 记录退出提示取得焦点。 */
  focus(): void { this.focused = true; }
  /** 记录草稿输入结束占用焦点。 */
  blur(): void { this.blurred = true; }
}

/** 只模拟本应用的圆角窗口、标题栏和三个功能控件，不访问真实 DOM 或数据。 */
class Page {
  body = new Element(); root = new Element(); header = new Element();
  controls = [new Element(), new Element(), new Element()];
  activeElement = this.controls[0];
  /** 将模拟窗口和控件装配到页面。 */
  constructor() { this.root.append(this.header, ...this.controls); this.body.append(this.root); }
  /**
   * 返回退出界面使用的当前窗口或标题栏。
   * Args:
   *     selector (string): 退出组件查询的选择器。
   * Returns:
   *     Element: 模拟窗口或标题栏。
   */
  querySelector(selector: string): Element { return selector === ".ui-window-frame" ? this.root : this.header; }
  /** 返回需要禁用的功能控件。 */
  querySelectorAll(): Element[] { return this.controls; }
  /** 返回新创建的退出提示元素。 */
  createElement(): Element { return new Element(); }
}

/**
 * 创建真实事件分发与模拟页面，状态查询可以由测试延迟完成。
 * Args:
 *     initialState (Promise<boolean>): 首次主进程状态查询结果。
 * Returns:
 *     object: 页面、事件目标、退出控制器以及模拟通知入口。
 */
const fixture = (initialState = Promise.resolve(false), forceOperation = async (): Promise<void> => {}) => {
  const target = new EventTarget(); const page = new Page();
  Object.assign(target, { document: page });
  let forces = 0;
  let notify: (() => void) | null = null; let removals = 0;
  const ui = createApplicationShutdownUi(target as unknown as Window, {
    /** 记录用户主动要求的强制退出。 */
    forceApplicationExit: async () => { forces++; await forceOperation(); },
    /** 返回模拟主进程状态，不访问任何后端。 */
    getApplicationShutdownState: () => initialState,
    /**
     * 保存页面退出回调，模拟其他窗口收到主进程通知。
     * Args:
     *     callback (() => void): 页面退出回调。
     * Returns:
     *     (() => void): 取消监听并记录清理次数。
     */
    onApplicationShutdown: (callback) => {
      notify = callback;
      /** 记录卸载时取消监听。 */
      return () => { removals++; notify = null; };
    },
  });
  return { ui, page, target,
    /** 查询实际强制退出次数。 */
    forces: () => forces,
    /** 推送主进程退出通知。 */
    notify: () => notify?.(),
    /** 读取通知监听被移除的次数。 */
    removals: () => removals,
  };
};

test("确认后立即禁用控件，异步回调重新启用按钮也不能点击或按 Enter", /** 退出锁定拦截实际事件回调，确保用户操作不会进入旧业务监听。 */
async () => {
  const { ui, page, target } = fixture(); ui.install(); await Promise.resolve();
  let operations = 0;
  /** 模拟原页面的点击和键盘功能回调。 */
  const execute = (): void => { operations++; };
  target.addEventListener("click", execute); target.addEventListener("keydown", execute);
  target.dispatchEvent(new Event("click", { cancelable: true })); assert.equal(operations, 1);
  ui.lock();
  assert.equal(page.body.dataset.applicationShutdown, "true");
  assert.ok(page.controls.every(/**
   * 核对所有原控件都已禁用并退出焦点导航。
   * Args:
   *     element (Element): 当前控件。
   */
  (element) => element.disabled && element.inert));
  assert.equal(page.activeElement.blurred, true);
  page.controls[0].disabled = false;
  for (const name of ["click", "keydown"]) {
    const event = new Event(name, { cancelable: true }); target.dispatchEvent(event);
    assert.equal(event.defaultPrevented, true);
  }
  assert.equal(operations, 1); ui.dispose();
});

test("通知先于状态查询返回时，迟到的 false 不解锁，也不重复创建提示", /** 模拟另一窗口确认退出与初始查询交错的顺序。 */
async () => {
  let resolveState!: (value: boolean) => void;
  const state = new Promise<boolean>(/**
   * 保存首次状态查询的完成通知。
   * Args:
   *     resolve ((value: boolean) => void): 查询完成时调用。
   */
  (resolve) => { resolveState = resolve; });
  const { ui, page, notify, removals } = fixture(state); ui.install(); notify(); notify();
  resolveState(false); await state;
  assert.equal(page.root.children.filter(/**
   * 只统计共用退出提示。
   * Args:
   *     element (Element): 当前窗口子元素。
   */
  (element) => element.className === "ui-shutdown-overlay").length, 1);
  assert.equal(page.root.children.at(-1)?.focused, true);
  assert.equal(page.controls[0].disabled, true);
  ui.dispose(); ui.dispose(); assert.equal(removals(), 1);
});

test("迟加载页面通过状态查询锁定，已卸载页面忽略迟到状态", /** 验证主进程通知早于页面加载与页面已结束两条路径。 */
async () => {
  const late = fixture(Promise.resolve(true)); late.ui.install(); await Promise.resolve();
  assert.equal(late.page.controls[0].disabled, true); late.ui.dispose();
  const gone = fixture(Promise.resolve(true)); gone.ui.install(); gone.ui.dispose(); await Promise.resolve();
  assert.equal(gone.page.controls[0].disabled, false);
  assert.equal(gone.page.body.dataset.applicationShutdown, undefined);
});

test("开发模式旧 preload 不打断页面初始化，本窗口确认退出仍能立即锁定", /** 验证热更新期间缺少新接口时页面保持可用，重启后才接入主进程通知。 */
() => {
  const { page, target } = fixture();
  const ui = createApplicationShutdownUi(target as unknown as Window, {} as Parameters<typeof createApplicationShutdownUi>[1]);
  assert.doesNotThrow(/** 安装时不调用不存在的旧 preload 方法。 */ () => ui.install());
  const event = new Event("click", { cancelable: true }); target.dispatchEvent(event);
  assert.equal(event.defaultPrevented, false);
  ui.lock(); assert.equal(page.controls[0].disabled, true); ui.dispose();
});

for (const kind of ["click", "keydown"]) {
  test(`退出遮罩的强制按钮支持 ${kind}，其他业务仍被拦截且重复操作合并`, /** 通过真实捕获入口触发退出按钮，避免只检查 DOM 文案。 */
  async () => {
    const { ui, page, target, forces } = fixture(); ui.install();
    assert.equal(forces(), 0);
    ui.lock();
    const button = page.root.children.at(-1)!.children[0].children.at(-1)!;
    assert.equal(button.disabled, false);
    let operations = 0;
    target.addEventListener(kind, /** 原业务监听不应收到退出按钮的操作。 */ () => { operations++; });
    for (let i = 0; i < 2; i++) {
      const event = new Event(kind, { cancelable: true });
      Object.assign(event, { key: "Enter", /** 模拟浏览器为按钮事件提供的实际路径。 */ composedPath: () => [button, target] });
      target.dispatchEvent(event);
    }
    await Promise.resolve();
    assert.equal(forces(), 1);
    assert.equal(operations, 0);
    assert.equal(button.disabled, true);
    ui.dispose();
  });
}

test("强制终止失败后提示原因并允许用户再次点击", /** 保持等待遮罩及业务锁定，只恢复强制按钮。 */
async () => {
  let attempts = 0;
  const { ui, page, target, forces } = fixture(Promise.resolve(true), /** 第一次模拟进程终止失败。 */
    async () => { if (++attempts === 1) throw new Error("进程终止失败"); });
  ui.install(); await Promise.resolve();
  const card = page.root.children.at(-1)!.children[0];
  const button = card.children.at(-1)!;
  /** 通过退出捕获入口派发按钮点击。 */
  const click = (): void => {
    const event = new Event("click", { cancelable: true });
    Object.assign(event, { /** 返回按钮所在路径。 */ composedPath: () => [button, target] });
    target.dispatchEvent(event);
  };
  click();
  await new Promise<void>(/** 等待错误响应传播到实际界面处理器。 */ (resolve) => setImmediate(resolve));
  assert.equal(button.disabled, false);
  assert.match(card.children.at(-2)!.textContent, /进程终止失败/);
  assert.ok(page.controls.every(/** 失败不会重新开放业务。 */ (control) => control.disabled));
  click();
  assert.equal(forces(), 2);
  ui.dispose();
});
