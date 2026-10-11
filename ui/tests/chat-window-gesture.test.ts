import assert from "node:assert/strict";
import { test } from "node:test";
import { calculateGestureBounds, WindowGestureController, isWindowGesture, type GestureWindow } from "../electron/window-gesture.js";
import type { WindowBounds } from "../electron/chat-window-state.js";

test("左上角缩到最小尺寸后保持右边和底边，移回时恢复原尺寸", /** 验证连续跨越最小尺寸不会推动对边或累计误差。 */
() => {
  const initial = { x: -300, y: 80, width: 560, height: 720 };
  assert.deepEqual(calculateGestureBounds(initial, "nw", 500, 500, [420, 480]),
    { x: -160, y: 320, width: 420, height: 480 });
  assert.deepEqual(calculateGestureBounds(initial, "nw", 0, 0, [420, 480]), initial);
  assert.deepEqual(calculateGestureBounds(initial, "se", 50, 20, [420, 480]),
    { ...initial, width: 610, height: 740 });
});

test("水平缩放不改变高度，移动使用完整桌面坐标", /** 验证侧边不会把鼠标纵向移动带入缩放，多屏负坐标仍可使用。 */
() => {
  const initial = { x: -1200, y: 30, width: 560, height: 720 };
  assert.deepEqual(calculateGestureBounds(initial, "e", 80.4, 500, [420, 480]), { ...initial, width: 640 });
  assert.deepEqual(calculateGestureBounds(initial, "move", 1300, -40, [420, 480]), { ...initial, x: 100, y: -10 });
  assert.equal(isWindowGesture("nw"), true);
  assert.equal(isWindowGesture("set-position"), false);
  assert.equal(isWindowGesture({ x: 100 }), false);
});

/** 模拟窗口存活状态与实际坐标，只记录主进程设置尺寸的次数。 */
class TestGestureWindow implements GestureWindow {
  bounds: WindowBounds = { x: 100, y: 100, width: 560, height: 720 };
  minimized = false;
  writes = 0;
  /** 返回当前系统矩形的副本，避免测试共享对象掩盖起点累计错误。 */
  getBounds(): WindowBounds { return { ...this.bounds }; }
  /** 返回正常工作区的最小聊天尺寸。 */
  getMinimumSize(): number[] { return [420, 480]; }
  /**
   * 记录本次主进程计算的窗口矩形。
   * Args:
   *     bounds (WindowBounds): 本次位置和尺寸。
   */
  setBounds(bounds: WindowBounds): void { this.bounds = bounds; this.writes++; }
  /** 当前测试窗口始终存活。 */
  isDestroyed(): boolean { return false; }
  /** 返回测试指定的最小化状态。 */
  isMinimized(): boolean { return this.minimized; }
}

test("重复采样不重复设置，结束后鼠标离开不把窗口拖回，穿透状态只恢复一次", /**
 * 验证拖动期间保持对边固定，松开后无需继续读取鼠标；主进程及时停止计时器。
 * Args:
 *     context (TestContext): 隔离计时器的测试上下文。
 */
(context) => {
  context.mock.timers.enable({ apis: ["setInterval"] });
  const win = new TestGestureWindow(); let cursor = { x: 100, y: 200 };
  const activity: boolean[] = [];
  const controller = new WindowGestureController(win,
    /** 模拟系统桌面鼠标，页面局部坐标不参与计算。 */ () => cursor,
    /**
     * 记录角色穿透被固定与恢复的顺序。
     * Args:
     *     active (boolean): 当前是否按住聊天窗口。
     */
    (active) => activity.push(active));
  controller.begin("w");
  cursor = { x: 180, y: 500 }; context.mock.timers.tick(16);
  assert.deepEqual(win.bounds, { x: 180, y: 100, width: 480, height: 720 });
  context.mock.timers.tick(80); assert.equal(win.writes, 1);
  controller.end(); controller.end();
  cursor = { x: 0, y: 0 }; context.mock.timers.tick(1000);
  assert.equal(win.writes, 1); assert.deepEqual(activity, [true, false]);
});

test("操作期间最小化取消采样，后续拖动仍能重新开始", /**
 * 验证丢失页面松开事件时仍恢复角色穿透，恢复窗口后允许再次移动。
 * Args:
 *     context (TestContext): 隔离计时器的测试上下文。
 */
(context) => {
  context.mock.timers.enable({ apis: ["setInterval"] });
  const win = new TestGestureWindow(); let cursor = { x: 100, y: 100 };
  const activity: boolean[] = [];
  const controller = new WindowGestureController(win,
    /** 返回当前模拟桌面鼠标。 */ () => cursor,
    /**
     * 记录操作生命周期的角色穿透状态。
     * Args:
     *     active (boolean): 当前操作状态。
     */
    (active) => activity.push(active));
  controller.begin("move"); win.minimized = true;
  cursor = { x: 500, y: 500 }; context.mock.timers.tick(16);
  assert.equal(win.writes, 0); assert.deepEqual(activity, [true, false]);
  win.minimized = false; controller.begin("move");
  cursor = { x: 600, y: 550 }; context.mock.timers.tick(16); controller.end();
  assert.deepEqual(win.bounds, { x: 200, y: 150, width: 560, height: 720 });
  assert.deepEqual(activity, [true, false, true, false]);
});
