/** 聊天与日志共用窗口拖动和缩放，避免 Windows 原生无边框拖边白条。 */
import type { WindowGesture } from "../shared-types.js";
import type { WindowBounds } from "./chat-window-state.js";

/** 主进程拖动只依赖窗口位置、最小尺寸和存活状态，供聊天和日志窗口复用。 */
export interface GestureWindow {
  /** 返回当前系统窗口的位置与尺寸。 */
  getBounds(): WindowBounds;
  /** 返回当前工作区允许的最小宽度和高度。 */
  getMinimumSize(): number[];
  /** 应用主进程计算的完整位置尺寸。 */
  setBounds(bounds: WindowBounds): void;
  /** 返回窗口是否已经释放。 */
  isDestroyed(): boolean;
  /** 返回窗口是否处于最小化状态。 */
  isMinimized(): boolean;
}

/**
 * 校验可信应用页面发送的操作方向，拒绝任意对象或未知操作。
 * Args:
 *     value (unknown): IPC 载荷。
 * Returns:
 *     boolean: 是否为允许的标题栏或边缘操作。
 */
export const isWindowGesture = (value: unknown): value is WindowGesture =>
  typeof value === "string" && ["move", "n", "s", "e", "w", "ne", "nw", "se", "sw"].includes(value);

/**
 * 从按下时的矩形和桌面鼠标位移计算新尺寸，缩放保持对边固定。
 * Args:
 *     initial (WindowBounds): 开始操作时的系统窗口矩形。
 *     kind (WindowGesture): 移动或缩放方向。
 *     dx, dy (number): 桌面鼠标的位移，使用 Electron 的 DIP 坐标。
 *     minimum (number[]): 当前工作区允许的最小宽度和高度。
 * Returns:
 *     WindowBounds: 满足最小尺寸、保持对边不动的整数矩形。
 */
export const calculateGestureBounds = (initial: WindowBounds, kind: WindowGesture,
  dx: number, dy: number, minimum: number[]): WindowBounds => {
  dx = Math.round(dx); dy = Math.round(dy);
  if (kind === "move") return { ...initial, x: initial.x + dx, y: initial.y + dy };
  const horizontal = kind.includes("w") ? -1 : kind.includes("e") ? 1 : 0;
  const vertical = kind.includes("n") ? -1 : kind.includes("s") ? 1 : 0;
  const width = horizontal ? Math.max(minimum[0], initial.width + dx * horizontal) : initial.width;
  const height = vertical ? Math.max(minimum[1], initial.height + dy * vertical) : initial.height;
  // 1. 左边或上边缩到最小值时重新算坐标，不能继续跟随鼠标移动对边。
  return { x: horizontal === -1 ? initial.x + initial.width - width : initial.x,
    y: vertical === -1 ? initial.y + initial.height - height : initial.y, width, height };
};

/**
 * 按页面捕获的按下与松开管理主进程鼠标采样，不进入系统的原生拖动循环。
 * Attributes:
 *     timer (NodeJS.Timeout | null): 拖动期间约每帧更新一次窗口，结束时释放。
 */
export class WindowGestureController {
  private timer: ReturnType<typeof setInterval> | null = null;
  private update: (() => void) | null = null;

  /**
   * 保存当前应用窗口、桌面坐标读取函数和角色穿透状态通知。
   * Args:
   *     win (GestureWindow): 当前聊天或日志窗口。
   *     cursor (() => { x: number; y: number }): 主进程读取系统鼠标的位置。
   *     onActive ((active: boolean) => void): 开始与结束时通知角色窗口固定或恢复穿透。
   */
  constructor(private readonly win: GestureWindow, private readonly cursor: () => { x: number; y: number },
    private readonly onActive: (active: boolean) => void) {}

  /**
   * 页面捕获左键按下后保存起点，后续只使用系统坐标避免页面大小变化造成反馈抖动。
   * Args:
   *     kind (WindowGesture): 当前操作方向。
   */
  begin(kind: WindowGesture): void {
    this.end();
    if (this.win.isDestroyed() || this.win.isMinimized()) return;
    const initial = this.win.getBounds(); const start = this.cursor();
    const minimum = this.win.getMinimumSize();
    let previous = initial;
    this.onActive(true);
    this.update = /** 定时器读取系统鼠标；窗口释放或最小化后停止操作。 */
    () => {
      if (this.win.isDestroyed() || this.win.isMinimized()) { this.end(); return; }
      const point = this.cursor();
      const next = calculateGestureBounds(initial, kind, point.x - start.x, point.y - start.y, minimum);
      // 1. 未移动时不重复设置；每次从按下起点计算，不累积显示缩放的取整误差。
      if (next.x !== previous.x || next.y !== previous.y || next.width !== previous.width || next.height !== previous.height) {
        this.win.setBounds(next); previous = next;
      }
    };
    this.timer = setInterval(this.update, 16);
  }

  /** 松开或取消捕获时停止采样；窗口卸载和最小化也调用此入口，允许重复清理。 */
  end(): void {
    if (this.timer) clearInterval(this.timer);
    const wasActive = this.update !== null;
    this.timer = null; this.update = null;
    if (wasActive) this.onActive(false);
  }
}
