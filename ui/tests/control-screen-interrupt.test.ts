import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isControlScreenCaptureInterrupt,
  isControlScreenExecuteInterrupt,
} from "../src/chat/control-screen-interrupt.js";

test("capture 中断只进入 VLM 截图响应流程", () => {
  assert.equal(isControlScreenCaptureInterrupt({ type: "control_screen_capture_request" }), true);
  assert.equal(isControlScreenCaptureInterrupt({ type: "control_screen_execute_request" }), false);
  assert.equal(isControlScreenCaptureInterrupt({ type: "unexpected_interrupt" }), false);
});

test("只有 execute 中断会进入屏幕操作批准流程", () => {
  assert.equal(isControlScreenExecuteInterrupt({ type: "control_screen_execute_request" }), true);
  assert.equal(isControlScreenExecuteInterrupt({ type: "control_screen_capture_request" }), false);
  assert.equal(isControlScreenExecuteInterrupt({ type: "unexpected_interrupt" }), false);
});
