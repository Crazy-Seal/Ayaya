import assert from "node:assert/strict";
import { test } from "node:test";
import { fitChatBounds, stabilizeChatBounds } from "../electron/chat-window-state.js";
import { hideDesktopWindows, type DesktopWindow } from "../electron/desktop-window-visibility.js";

test("聊天默认尺寸在带偏移的显示器工作区居中", /** 验证多显示器坐标不假设从零开始。 */
() => {
  assert.deepEqual(fitChatBounds(undefined, { x: -1920, y: 40, width: 1920, height: 1040 }),
    { x: -1240, y: 200, width: 560, height: 720 });
});

test("离屏与损坏尺寸修正后保留在工作区", /** 验证拔掉显示器和损坏配置不会导致窗口无法找到。 */
() => {
  assert.deepEqual(fitChatBounds({ x: 5000, y: -1000, width: -1, height: NaN },
    { x: 0, y: 0, width: 800, height: 600 }), { x: 380, y: 0, width: 420, height: 600 });
});

test("小屏幕允许低于常规最小尺寸并完整显示", /** 验证小工作区不会被 420×480 的最小尺寸撑出屏幕。 */
() => {
  assert.deepEqual(fitChatBounds(undefined, { x: 0, y: 0, width: 360, height: 400 }),
    { x: 0, y: 0, width: 360, height: 400 });
});

test("显示缩放的一像素取整不累计到重启配置，真实缩放仍保存", /** 验证多次恢复不会把边框和取整误差逐次写回。 */
() => {
  const saved = { x: 49, y: 49, width: 621, height: 681 };
  let stable = saved;
  for (let restart = 0; restart < 10; restart++) {
    stable = stabilizeChatBounds({ x: 48, y: 48, width: 622, height: 682 }, stable);
  }
  assert.deepEqual(stable, saved);
  const resized = { x: 200, y: 100, width: 700, height: 800 };
  assert.deepEqual(stabilizeChatBounds(resized, stable), resized);
});

/** 模拟窗口的可见、最小化与释放状态，记录截图恢复是否抢焦点。 */
class TestWindow implements DesktopWindow {
  destroyed = false;
  hides = 0;
  shows = 0;
  /**
   * 建立截图前窗口状态。
   * Args:
   *     visible (boolean): 当前可见状态。
   *     minimized (boolean): 当前最小化状态。
   */
  constructor(public visible = true, public minimized = false) {}
  /** 返回窗口释放状态。 */
  isDestroyed(): boolean { return this.destroyed; }
  /** 返回窗口可见状态。 */
  isVisible(): boolean { return this.visible; }
  /** 返回窗口最小化状态。 */
  isMinimized(): boolean { return this.minimized; }
  /** 记录临时隐藏但不改变最小化状态。 */
  hide(): void { this.hides++; this.visible = false; }
  /** 记录不聚焦的可见状态恢复。 */
  showInactive(): void { this.shows++; this.visible = true; }
}

test("截图隐藏角色和聊天但不弹出最小化或原本隐藏的窗口", /** 验证同时处理两窗口且保持原有状态。 */
() => {
  const pet = new TestWindow(); const chat = new TestWindow();
  const minimized = new TestWindow(true, true); const invisible = new TestWindow(false);
  const visibility = hideDesktopWindows([pet, chat, pet, minimized, invisible, null]);
  assert.equal(visibility.hidden, true);
  assert.equal(pet.hides, 1); assert.equal(chat.hides, 1);
  visibility.restore(); visibility.restore();
  assert.equal(pet.shows, 1); assert.equal(chat.shows, 1);
  assert.equal(minimized.hides, 0); assert.equal(minimized.shows, 0);
  assert.equal(invisible.shows, 0);
});

test("截图失败时 finally 恢复存活窗口，已释放或后来最小化的窗口不恢复", /** 验证异常和用户中途操作不会错误显示窗口。 */
() => {
  const pet = new TestWindow(); const chat = new TestWindow(); const laterMinimized = new TestWindow();
  const visibility = hideDesktopWindows([pet, chat, laterMinimized]);
  try {
    chat.destroyed = true; laterMinimized.minimized = true;
    throw new Error("截图失败");
  } catch { /* 当前用例只检查窗口清理行为。 */ }
  finally { visibility.restore(); }
  assert.equal(pet.shows, 1); assert.equal(chat.shows, 0); assert.equal(laterMinimized.shows, 0);
});
