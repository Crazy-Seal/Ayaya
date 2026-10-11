import assert from "node:assert/strict";
import { test } from "node:test";
import { MultimodalPage } from "../src/settings/pages/multimodal-page.js";

test("后端设置保存失败恢复旧值，退出后的迟到响应不能启用控件", /** 验证设置页真实保存事件的失败与退出行为。 */
async () => {
  class Checkbox extends EventTarget {
    /** 提供设置页使用的两个状态，不创建浏览器页面。 */
    checked = true;
    disabled = false;
  }
  const checkbox = new Checkbox();
  const errors: string[] = [];
  const dataset: Record<string, string> = {};
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  let release: (value: { hide_on_screenshot: boolean }) => void = () => {};
  let failing = true;
  globalThis.document = { body: { dataset } } as unknown as Document;
  globalThis.window = { desktopPetApi: {
    /** YAML 已确认迁移，返回当前设置。 */ getDesktopSettings: async () => ({ hide_on_screenshot: false }),
    /** 首次模拟存储失败；随后请求由测试安排在退出后到达。 */ updateDesktopSettings: async () => {
      if (failing) throw new Error("写入失败");
      return new Promise<{ hide_on_screenshot: boolean }>(/** 保存当前请求的迟到完成函数。 */ resolve => { release = resolve; });
    },
  } } as unknown as Window & typeof globalThis;
  try {
    const page = new MultimodalPage(checkbox as unknown as HTMLInputElement, {
      /** 本用例只观察失败消息。 */ success: () => {},
      /** 记录用户能看到的存储失败原因。 */ error: message => { errors.push(message); },
    });
    await page.render(); assert.equal(checkbox.checked, false);
    checkbox.checked = true; checkbox.dispatchEvent(new Event("change"));
    await Promise.resolve(); await Promise.resolve();
    assert.equal(checkbox.checked, false); assert.equal(checkbox.disabled, false); assert.match(errors[0], /写入失败/);
    failing = false; checkbox.checked = true; checkbox.dispatchEvent(new Event("change"));
    assert.equal(checkbox.disabled, true);
    dataset.applicationShutdown = "true";
    release({ hide_on_screenshot: true });
    await new Promise<void>(/** 等保存回调及 finally 结束，再恢复测试全局对象。 */ resolve => setImmediate(resolve));
    assert.equal(checkbox.disabled, true);
  } finally { globalThis.window = originalWindow; globalThis.document = originalDocument; }
});
