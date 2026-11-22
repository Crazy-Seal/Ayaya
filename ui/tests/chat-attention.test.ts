import assert from "node:assert/strict";
import { test } from "node:test";
import { ChatAttention } from "../electron/chat-attention.js";
import type { InteractionEvent } from "../shared-types.js";

/** 创建提醒处理所需的会话事件。 */
const event = (session: string, kind: InteractionEvent["kind"], request = "request", child: string | null = null): InteractionEvent => ({
  session_id: session, kind, stream_epoch: "epoch", event_sequence: 1, request_id: request,
  payload: { value: { type: "authorization_request", request_id: request }, subagent_id: child },
});

test("查看 B 时 A 的新消息仍未读，查看 B 仅清除 B，切到 A 展示完成后清除 A", () => {
  const attention = new ChatAttention();
  attention.beginView("B"); attention.setFocused(true);
  attention.report({ sessionId: "B", viewed: true });
  attention.receive(event("A", "text_chunk"));
  assert.equal(attention.summary().unread, true);
  attention.report({ sessionId: "B", viewed: true });
  assert.equal(attention.summary().unread, true);
  attention.beginView("A");
  assert.equal(attention.summary().unread, true);
  attention.report({ sessionId: "A", viewed: true });
  assert.equal(attention.summary().unread, false);
});

test("审批按会话与请求处理，切换及其他审批完成不会清除 A", () => {
  const attention = new ChatAttention();
  attention.beginView("B"); attention.setFocused(true);
  attention.receive(event("A", "interrupt", "a"));
  attention.receive(event("B", "subagent_interrupt", "b", "child"));
  attention.receive(event("B", "authorization_resolved", "b", "child"));
  assert.equal(attention.summary().approval, true);
  assert.equal(attention.summary().flash, true);
  attention.report({ sessionId: "B", viewed: true });
  assert.equal(attention.summary().approval, true);
  attention.receive(event("A", "authorization_resolved", "a"));
  assert.equal(attention.summary().approval, false);
});

test("故障按父子上下文清除审批，当前快照校正当前会话的审批状态", () => {
  const attention = new ChatAttention();
  attention.receive(event("A", "subagent_interrupt", "child-approval", "child"));
  attention.receive(event("A", "run_completed"));
  assert.equal(attention.summary().approval, true);
  attention.receive(event("A", "error", "unused", "child"));
  assert.equal(attention.summary().approval, false);
  attention.receive(event("B", "interrupt", "b"));
  attention.receive(event("A", "interrupt", "a"));
  attention.receive({ ...event("A", "session_snapshot"), payload: { interrupt: null } });
  assert.equal(attention.summary().approval, true);
  attention.receive(event("B", "authorization_resolved", "b"));
  assert.equal(attention.summary().approval, false);
});
