import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { bindPressToRevealSecret } from "../src/settings/components/press-to-reveal-secret.js";

test("LLM API Key 输入框默认使用密码掩码显示", /**
 * 验证LLM API Key 输入框默认使用密码掩码显示。
 */
() => {
  const settingsHtml = readFileSync("settings.html", "utf8");
  const apiKeyInput = settingsHtml.match(/<input\s+[^>]*id="llm-api-key"[^>]*>/)?.[0];

  assert.ok(apiKeyInput, "缺少 LLM API Key 输入框");
  assert.match(apiKeyInput, /\btype="password"/);
  assert.match(apiKeyInput, /\bname="openai_api_key"/);
  assert.match(
    settingsHtml,
    /<label\s+[^>]*for="llm-api-key"[^>]*>\s*API Key\s*<\/label>/
  );
  assert.match(settingsHtml, /<button\s+[^>]*id="llm-api-key-reveal"[^>]*>/);
});

test("仅在按住查看按钮时显示密钥，松开或失焦后立即恢复掩码", /**
 * 验证仅在按住查看按钮时显示密钥，松开或失焦后立即恢复掩码。
 */
() => {
  const input = { type: "password" };
  const button = new EventTarget();
  const windowTarget = new EventTarget();
  /**
   * 手动触发测试按压事件，供检查凭据显示与隐藏。
   *
   * 此定义用于：验证仅在按住查看按钮时显示密钥，松开或失焦后立即恢复掩码。
   *
   * Args:
   *     target (EventTarget): 本次操作目标。
   *     type (string): 本次手动触发的页面事件类型。
   *     mouseButton (number): 模拟按压事件的鼠标按钮编号。
   */
  const dispatchPointer = (target: EventTarget, type: string, mouseButton = 0): void => {
    const event = new Event(type);
    Object.defineProperty(event, "button", { value: mouseButton });
    target.dispatchEvent(event);
  };
  const unbind = bindPressToRevealSecret(
    input as HTMLInputElement,
    button as HTMLButtonElement,
    windowTarget
  );

  dispatchPointer(button, "pointerdown", 2);
  assert.equal(input.type, "password");

  dispatchPointer(button, "pointerdown");
  assert.equal(input.type, "text");
  dispatchPointer(windowTarget, "pointerup");
  assert.equal(input.type, "password");

  dispatchPointer(button, "pointerdown");
  button.dispatchEvent(new Event("pointerleave"));
  assert.equal(input.type, "password");

  dispatchPointer(button, "pointerdown");
  windowTarget.dispatchEvent(new Event("blur"));
  assert.equal(input.type, "password");

  unbind();
});

test("键盘按住显示密钥，松开与清理后恢复掩码", /** 验证键盘和鼠标采用相同的按住显示约束。 */
() => {
  const input = { type: "password" }; const button = new EventTarget(); const windowTarget = new EventTarget();
  /**
   * 触发带 key 的测试事件，不依赖浏览器全局 KeyboardEvent。
   * Args:
   *     target (EventTarget): 按钮或窗口测试目标。
   *     type (string): 按下或松开事件。
   *     key (string): 当前按键。
   */
  const dispatchKey = (target: EventTarget, type: string, key: string): void => {
    const event = new Event(type, { cancelable: true });
    Object.defineProperty(event, "key", { value: key }); target.dispatchEvent(event);
  };
  const unbind = bindPressToRevealSecret(input as HTMLInputElement, button as HTMLButtonElement, windowTarget);
  dispatchKey(button, "keydown", " "); assert.equal(input.type, "text");
  dispatchKey(windowTarget, "keyup", " "); assert.equal(input.type, "password");
  dispatchKey(button, "keydown", "Enter"); assert.equal(input.type, "text");
  unbind(); assert.equal(input.type, "password");
  dispatchKey(button, "keydown", "Enter"); assert.equal(input.type, "password");
});
