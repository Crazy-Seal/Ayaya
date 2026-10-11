import assert from "node:assert/strict";
import { test } from "node:test";
import { fitChatBounds, stabilizeChatBounds } from "../electron/chat-window-state.js";

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
