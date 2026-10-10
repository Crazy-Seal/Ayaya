import assert from "node:assert/strict";
import { test } from "node:test";
import { appendFrontendLog, closeLogFiles, getLogSnapshot } from "../electron/logging/app-logger.js";

test("统一前端日志保留流断线的底层错误码和原因", /**
 * 验证统一前端日志保留流断线的底层错误码和原因。
 */
async () => {
  try {
    const cause = Object.assign(new Error("Body Timeout Error"), { code: "UND_ERR_BODY_TIMEOUT" });
    appendFrontendLog("warn", "interaction-stream", "交互事件流断开", new TypeError("terminated", { cause }));
    const message = getLogSnapshot().frontend.at(-1)?.message || "";
    assert.match(message, /terminated/);
    assert.match(message, /Body Timeout Error/);
    assert.match(message, /UND_ERR_BODY_TIMEOUT/);
  } finally {
    await closeLogFiles();
  }
});
