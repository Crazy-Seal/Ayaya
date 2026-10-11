import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  SessionPhase, CommandKind, InteractionEventKind, InterruptKind,
  ApprovalDecisionKind, AuthorizationResolutionKind, TaskStatus,
} from "../shared-types.js";

test("前端运行时常量与后端共用的固定协议样例一致", /**
 * 验证前端运行时常量与后端共用的固定协议样例一致。
 */
() => {
  const expected = JSON.parse(readFileSync(
    new URL("../../../tests/fixtures/runtime-protocol-values.json", import.meta.url), "utf8",
  )) as Record<string, string[]>;
  const contracts = { SessionPhase, CommandKind, InteractionEventKind, InterruptKind,
    ApprovalDecisionKind, AuthorizationResolutionKind, TaskStatus };
  for (const [name, values] of Object.entries(contracts)) {
    assert.deepEqual(Object.values(values), expected[name]);
    assert.deepEqual(JSON.parse(JSON.stringify(Object.values(values))), expected[name]);
  }
});
