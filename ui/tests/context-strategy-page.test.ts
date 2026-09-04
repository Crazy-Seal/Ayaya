import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CONTEXT_STRATEGY_FIELDS,
  getContextStrategyMinimum,
} from "../src/settings/pages/context-strategy-page.js";

test("上下文策略页面直接配置压缩触发阈值", () => {
  assert.deepEqual(CONTEXT_STRATEGY_FIELDS, [
    "compression_trigger_tokens_k",
  ]);
});

test("压缩触发阈值最低为 10K token", () => {
  assert.equal(getContextStrategyMinimum("compression_trigger_tokens_k"), 10);
});
