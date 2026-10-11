import { iconPathData } from "../../ui/icons.js";
export type SettingsToastVariant = "success" | "error";

/**
 * 约定设置页面使用的成功和失败提示接口。
 */
export interface SettingsNotifier {
  /**
   * 设置保存成功时显示操作结果。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  success(message: string): void;
  /**
   * 设置保存失败时显示可读错误。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  error(message: string): void;
}

/**
 * 约定提示自动隐藏使用的定时器，测试可替换为手动时钟。
 */
export interface SettingsToastTimer {
  /**
   * 安排提示延迟隐藏回调。
   *
   * Args:
   *     callback (() => void): 触发后交给调用方继续处理的回调。
   *     delayMs (number): 重试或等待的毫秒间隔。
   *
   * Returns:
   *     unknown: 供后续取消等待使用的定时器标识。
   */
  setTimeout(callback: () => void, delayMs: number): unknown;
  /**
   * 取消尚未执行的提示隐藏回调。
   *
   * Args:
   *     handle (unknown): 需要取消的定时器编号。
   */
  clearTimeout(handle: unknown): void;
}

const TOAST_DURATION_MS = 3000;
const SUCCESS_ICON_PATH = iconPathData("check");
const ERROR_ICON_PATH = iconPathData("close");

const defaultTimer: SettingsToastTimer = {
  /**
   * 使用页面定时器安排提示隐藏。
   *
   * Args:
   *     callback (() => void): 触发后交给调用方继续处理的回调。
   *     delayMs (number): 重试或等待的毫秒间隔。
   *
   * Returns:
   *     Timeout: 页面定时器编号。
   */
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  /**
   * 取消页面提示定时器。
   *
   * Args:
   *     handle (unknown): 需要取消的定时器编号。
   */
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/**
 * 把未知异常转成用户可读文本，连接失败使用明确的后端不可用提示。
 *
 * Args:
 *     error (unknown): 本次捕获的异常。
 *
 * Returns:
 *     string: 可显示的错误正文，没有错误时为空。
 */
export const getErrorMessage = (error: unknown): string => {
  if (error === null || error === undefined) {
    return "";
  }
  const message = (error instanceof Error ? error.message : String(error)).trim();
  if (/\bfetch failed\b/i.test(message) || /\bECONNREFUSED\b/i.test(message)) {
    return "未识别到后端";
  }
  return message;
};

/**
 * 给操作失败前缀附加清理后的错误细节。
 *
 * Args:
 *     prefix (string): 错误提示的固定前缀。
 *     error (unknown): 本次捕获的异常。
 *
 * Returns:
 *     string: 包含操作名称及可选异常正文的提示。
 */
export const appendErrorMessage = (prefix: string, error: unknown): string => {
  const detail = getErrorMessage(error);
  return detail ? `${prefix}：${detail}` : prefix;
};

/**
 * 显示设置保存结果，后来的提示替换旧内容并重新计算自动隐藏时间。
 */
export class SettingsToast implements SettingsNotifier {
  private timeoutHandle: unknown = null;
  private readonly messageElement: HTMLSpanElement;
  private readonly iconPath: SVGPathElement;

  /**
   * 定位提示正文和图标，保存可替换定时器；元素不完整时立即报错。
   *
   * Args:
   *     element (HTMLDivElement): 当前需要操作的页面元素。
   *     timer (SettingsToastTimer): 可注入的定时器接口，测试可手动推进。
   */
  constructor(
    private readonly element: HTMLDivElement,
    private readonly timer: SettingsToastTimer = defaultTimer
  ) {
    const messageElement = element.querySelector<HTMLSpanElement>(".settings-toast-message");
    const iconPath = element.querySelector<SVGPathElement>(".settings-toast-icon path");
    if (!messageElement || !iconPath) {
      throw new Error("设置提示框缺少消息或图标元素");
    }
    this.messageElement = messageElement;
    this.iconPath = iconPath;
  }

  /**
   * 显示成功图标和保存结果，并安排自动隐藏。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  success(message: string): void {
    this.show("success", message);
  }

  /**
   * 显示错误图标和失败原因，并安排自动隐藏。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   */
  error(message: string): void {
    this.show("error", message);
  }

  /**
   * 取消自动隐藏定时器并清空提示内容，重复调用也可安全结束显示。
   */
  hide(): void {
    this.clearTimer();
    this.element.hidden = true;
    this.messageElement.textContent = "";
    delete this.element.dataset.variant;
  }

  /**
   * 页面释放提示组件时取消等待并隐藏当前提示。
   */
  dispose(): void {
    this.hide();
  }

  /**
   * 先取消旧定时器，再显示新图标与正文，避免旧回调提前隐藏新提示。
   *
   * Args:
   *     variant (SettingsToastVariant): 成功或失败提示的显示类型。
   *     message (string): 当前需要显示或记录的文本。
   */
  private show(variant: SettingsToastVariant, message: string): void {
    this.clearTimer();
    this.element.dataset.variant = variant;
    this.iconPath.setAttribute("d", variant === "success" ? SUCCESS_ICON_PATH : ERROR_ICON_PATH);
    this.messageElement.textContent = message;
    this.element.hidden = false;
    this.timeoutHandle = this.timer.setTimeout(/**
     * 当前提示显示期限到达后隐藏内容并清理定时器编号。
     */
    () => this.hide(), TOAST_DURATION_MS);
  }

  /**
   * 当前提示有定时器时取消它并清空编号。
   */
  private clearTimer(): void {
    if (this.timeoutHandle === null) {
      return;
    }
    this.timer.clearTimeout(this.timeoutHandle);
    this.timeoutHandle = null;
  }
}
