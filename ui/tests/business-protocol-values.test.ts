import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { MCPServerStatus, MCPTransport, MediaKind, ToolSource, HistoryRole } from "../shared-types.js";

test("前后端共享业务取值继续使用固定协议字符串", /**
 * 对照后端独立协议样例检查 MCP、图片、工具来源和历史角色。
 */
() => {
  const expected = JSON.parse(readFileSync(
    new URL("../../../tests/fixtures/business-protocol-values.json", import.meta.url), "utf8",
  )) as Record<string, string[]>;
  const contracts = { MCPServerStatus, MCPTransport, MediaKind, ToolSource };
  for (const [name, values] of Object.entries(contracts)) {
    assert.deepEqual(Object.values(values), expected[name]);
    assert.deepEqual(JSON.parse(JSON.stringify(values)), values);
  }
  assert.deepEqual([HistoryRole.HUMAN, HistoryRole.AI, HistoryRole.AI_TOOL_CALLING], expected.HistoryRole);
});
