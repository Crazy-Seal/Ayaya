import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendErrorMessage,
  SettingsToast,
  type SettingsToastTimer,
} from "../src/settings/components/settings-toast.js";

type FakeToastElement = {
  hidden: boolean;
  textContent: string;
  dataset: Record<string, string>;
  /**
   * 从用例预设的子元素集合返回匹配元素。
   *
   * Args:
   *     selector (string): 必须存在的页面元素选择器。
   */
  querySelector(selector: string): FakeTextElement | FakePathElement | null;
};

type FakeTextElement = { textContent: string };
type FakePathElement = { pathData: string; /**
 * 记录页面属性写入，供检查组件状态。
 *
 * Args:
 *     name (string): 当前对象的名称。
 *     value (string): 当前待检查或转换的值。
 */
setAttribute(name: string, value: string): void };

/**
 * 保存提示隐藏回调并允许用例手动触发，避免依赖真实时间。
 */
class FakeTimer implements SettingsToastTimer {
  private nextId = 1;
  private callbacks = new Map<number, () => void>();
  delays: number[] = [];

  /**
   * 登记延迟回调，供测试手动推进时间后执行。
   *
   * 此定义用于：提供 FakeTimer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     callback (() => void): 触发后交给调用方继续处理的回调。
   *     delayMs (number): 重试或等待的毫秒间隔。
   */
  setTimeout(callback: () => void, delayMs: number): unknown {
    const id = this.nextId++;
    this.callbacks.set(id, callback);
    this.delays.push(delayMs);
    return id;
  }

  /**
   * 移除尚未执行的测试定时器。
   *
   * 此定义用于：提供 FakeTimer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     handle (unknown): 需要取消的定时器编号。
   */
  clearTimeout(handle: unknown): void {
    this.callbacks.delete(Number(handle));
  }

  /**
   * 执行指定测试定时器，模拟时间到期。
   *
   * 此定义用于：提供 FakeTimer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   *
   * Args:
   *     handle (number): 需要取消的定时器编号。
   */
  run(handle: number): void {
    const callback = this.callbacks.get(handle);
    this.callbacks.delete(handle);
    callback?.();
  }

  /**
   * 提供当前未执行定时器编号，供检查组件资源清理。
   *
   * 此定义用于：提供 FakeTimer 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  get pendingHandles(): number[] {
    return [...this.callbacks.keys()];
  }
}

/**
 * 创建使用手动时钟和测试 DOM 的设置提示组件。
 */
const createToast = (): {
  toast: SettingsToast;
  element: FakeToastElement;
  messageElement: FakeTextElement;
  iconPath: FakePathElement;
  timer: FakeTimer;
} => {
  const messageElement: FakeTextElement = { textContent: "" };
  const iconPath: FakePathElement = {
    pathData: "",
    /**
     * 记录页面属性写入，供检查组件状态。
     *
     * 此定义用于：创建使用手动时钟和测试 DOM 的设置提示组件。
     *
     * Args:
     *     name (string): 当前对象的名称。
     *     value (string): 当前待检查或转换的值。
     */
    setAttribute(name, value) {
      if (name === "d") {
        this.pathData = value;
      }
    },
  };
  const element: FakeToastElement = {
    hidden: true,
    textContent: "",
    dataset: {},
    /**
     * 从用例预设的子元素集合返回匹配元素。
     *
     * 此定义用于：创建使用手动时钟和测试 DOM 的设置提示组件。
     *
     * Args:
     *     selector (string): 必须存在的页面元素选择器。
     */
    querySelector: (selector) => {
      if (selector === ".settings-toast-message") {
        return messageElement;
      }
      if (selector === ".settings-toast-icon path") {
        return iconPath;
      }
      return null;
    },
  };
  const timer = new FakeTimer();
  return {
    toast: new SettingsToast(element as unknown as HTMLDivElement, timer),
    element,
    messageElement,
    iconPath,
    timer,
  };
};

test("成功和失败提示带有对应标记并在 3 秒后隐藏", /**
 * 验证成功和失败提示带有对应标记并在 3 秒后隐藏。
 */
() => {
  const { toast, element, messageElement, iconPath, timer } = createToast();

  toast.success("修改成功");
  assert.equal(element.hidden, false);
  assert.equal(messageElement.textContent, "修改成功");
  const successPath = iconPath.pathData;
  assert.notEqual(successPath, "");
  assert.equal(element.dataset.variant, "success");
  assert.deepEqual(timer.delays, [3000]);

  timer.run(timer.pendingHandles[0]);
  assert.equal(element.hidden, true);
  assert.equal(messageElement.textContent, "");

  toast.error("修改失败：后端不可用");
  assert.equal(messageElement.textContent, "修改失败：后端不可用");
  assert.notEqual(iconPath.pathData, successPath);
  assert.equal(element.dataset.variant, "error");
});

test("新提示替换旧提示并重新开始计时", /**
 * 验证新提示替换旧提示并重新开始计时。
 */
() => {
  const { toast, element, messageElement, timer } = createToast();

  toast.error("第一次失败");
  const firstHandle = timer.pendingHandles[0];
  toast.success("第二次成功");

  assert.deepEqual(timer.pendingHandles, [firstHandle + 1]);
  assert.equal(messageElement.textContent, "第二次成功");
  assert.equal(element.dataset.variant, "success");

  timer.run(firstHandle);
  assert.equal(element.hidden, false);
  timer.run(timer.pendingHandles[0]);
  assert.equal(element.hidden, true);
});

test("失败信息仅在后端提供原因时追加详情", /**
 * 验证失败信息仅在后端提供原因时追加详情。
 */
() => {
  assert.equal(appendErrorMessage("修改失败", new Error("后端不可用")), "修改失败：后端不可用");
  assert.equal(appendErrorMessage("修改失败", undefined), "修改失败");
  assert.equal(appendErrorMessage("修改失败", "  "), "修改失败");
});

test("后端未启动时将 Electron fetch 错误转换为友好提示", /**
 * 验证后端未启动时将 Electron fetch 错误转换为友好提示。
 */
() => {
  const error = new Error(
    "Error invoking remote method 'desktop-pet:get-available-tools': TypeError: fetch failed"
  );

  assert.equal(appendErrorMessage("读取工具列表失败", error), "读取工具列表失败：未识别到后端");
});

test("dispose 清理提示和待执行计时器", /**
 * 验证dispose 清理提示和待执行计时器。
 */
() => {
  const { toast, element, messageElement, timer } = createToast();

  toast.success("保存成功");
  toast.dispose();

  assert.deepEqual(timer.pendingHandles, []);
  assert.equal(element.hidden, true);
  assert.equal(messageElement.textContent, "");
});
