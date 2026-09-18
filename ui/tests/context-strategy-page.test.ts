import assert from "node:assert/strict";
import { test } from "node:test";

import type { ChatSettingsState } from "../src/settings/types.js";
import {
  CONTEXT_STRATEGY_FIELDS,
  ContextStrategyPage,
  getContextStrategyMinimum,
} from "../src/settings/pages/context-strategy-page.js";

test("上下文策略页面直接配置压缩触发阈值", () => {
  assert.deepEqual(CONTEXT_STRATEGY_FIELDS, [
    "compression_trigger_tokens_k",
    "max_screenshots_in_context",
    "screenshot_ttl_human_messages",
  ]);
});

test("压缩触发阈值最低为 10K token", () => {
  assert.equal(getContextStrategyMinimum("compression_trigger_tokens_k"), 10);
});

test("截图数量与 TTL 最低为 0", () => {
  assert.equal(getContextStrategyMinimum("max_screenshots_in_context"), 0);
  assert.equal(getContextStrategyMinimum("screenshot_ttl_human_messages"), 0);
});

test("上下文策略页面渲染并保存全部截图字段", () => {
  const inputs = {
    compression_trigger_tokens_k: { value: "" } as HTMLInputElement,
    max_screenshots_in_context: { value: "" } as HTMLInputElement,
    screenshot_ttl_human_messages: { value: "" } as HTMLInputElement,
  };
  const confirmBtn = {
    addEventListener: () => undefined,
  } as unknown as HTMLButtonElement;
  const page = new ContextStrategyPage(inputs, confirmBtn);

  page.render({
    saved: {
      context_strategy: {
        compression_trigger_tokens_k: 100,
        max_screenshots_in_context: 3,
        screenshot_ttl_human_messages: 4,
      },
    } as ChatSettingsState,
  });

  assert.equal(inputs.max_screenshots_in_context.value, "3");
  assert.equal(inputs.screenshot_ttl_human_messages.value, "4");
  assert.deepEqual(page.getEditingData().contextStrategy?.context_strategy, {
    compression_trigger_tokens_k: 100,
    max_screenshots_in_context: 3,
    screenshot_ttl_human_messages: 4,
  });
});
