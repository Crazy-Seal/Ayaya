import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import {
  SessionPhase, CommandKind, InteractionEventKind, InterruptKind, DesktopExecutionKind,
  ApprovalDecisionKind, AuthorizationResolutionKind, TaskStatus,
} from "../shared-types.js";

test("前端运行时常量与后端共用的固定协议样例一致", /**
 * 检查两端实际共享的取值，独立样例防止代码和测试同步误改线协议。
 */
() => {
  const expected = JSON.parse(readFileSync(
    new URL("../../../tests/fixtures/runtime-protocol-values.json", import.meta.url), "utf8",
  )) as Record<string, string[]>;
  const contracts = { SessionPhase, CommandKind, InteractionEventKind, InterruptKind, DesktopExecutionKind,
    ApprovalDecisionKind, AuthorizationResolutionKind, TaskStatus };
  for (const [name, values] of Object.entries(contracts)) {
    assert.deepEqual(Object.values(values), expected[name]);
    assert.deepEqual(JSON.parse(JSON.stringify(Object.values(values))), expected[name]);
  }
});
