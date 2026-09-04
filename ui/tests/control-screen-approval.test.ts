import assert from "node:assert/strict";
import { test } from "node:test";

import { formatControlScreenApprovalMessage } from "../src/chat/control-screen-approval.js";
import type { ControlScreenActionData } from "../shared-types.js";

const action: ControlScreenActionData = {
  target: "确认按钮",
  operation: "click",
  text: "",
  press_enter: false,
  wait_seconds: 1,
  scroll_direction: "none",
  coordinates: {
    bbox: [450, 480, 580, 550],
    x_ratio: 0.515,
    y_ratio: 0.515,
  },
};

test("屏幕操作批准文案展示解析后的物理坐标", () => {
  const message = formatControlScreenApprovalMessage(action, { x: 989, y: 556 });

  assert.match(message, /屏幕坐标 \(989, 556\)/);
  assert.doesNotMatch(message, /450|480|580|550/);
});

test("屏幕操作批准文案保留操作和输入说明", () => {
  const message = formatControlScreenApprovalMessage(
    { ...action, operation: "double", text: "hello", press_enter: true },
    { x: 960, y: 540 },
  );

  assert.match(message, /双击/);
  assert.match(message, /输入“hello”后按回车/);
});
