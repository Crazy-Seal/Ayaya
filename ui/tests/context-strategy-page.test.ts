import assert from "node:assert/strict";
import { test } from "node:test";

import type { ChatSettingsState } from "../src/settings/types.js";
import {
  CONTEXT_STRATEGY_FIELDS,
  ContextStrategyPage,
  getContextStrategyMinimum,
} from "../src/settings/pages/context-strategy-page.js";

test("上下文策略页面直接配置压缩触发阈值", /**
 * 验证上下文策略页面直接配置压缩触发阈值。
 */
() => {
  assert.deepEqual(CONTEXT_STRATEGY_FIELDS, [
    "compression_trigger_tokens_k",
  ]);
});

test("压缩触发阈值最低为 10K token", /**
 * 验证压缩触发阈值最低为 10K token。
 */
() => {
  assert.equal(getContextStrategyMinimum("compression_trigger_tokens_k"), 10);
});


test("上下文策略页面渲染并保存压缩阈值", /**
 * 验证上下文策略页面渲染并保存压缩阈值。
 */
() => {
  const inputs = {
    compression_trigger_tokens_k: { value: "" } as HTMLInputElement,
  };
  const confirmBtn = {
    /**
     * 保存页面事件监听函数，供用例模拟用户操作。
     *
     * 此定义用于：验证上下文策略页面渲染并保存压缩阈值。
     */
    addEventListener: () => undefined,
  } as unknown as HTMLButtonElement;
  const page = new ContextStrategyPage(inputs, confirmBtn);

  page.render({
    saved: {
      context_strategy: {
        compression_trigger_tokens_k: 100,
      },
    } as ChatSettingsState,
  });

  assert.deepEqual(page.getEditingData().contextStrategy?.context_strategy, {
    compression_trigger_tokens_k: 100,
  });
});
