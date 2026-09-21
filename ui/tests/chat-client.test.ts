import assert from "node:assert/strict";
import { test } from "node:test";

import { ChatClient, type ChatClientOptions } from "../src/chat/chat-client.js";
import type { ApprovalDecision, ChatInterruptPayload, ChatResult, ControlScreenActionData } from "../shared-types.js";

const interrupt = (id: string): ChatInterruptPayload => ({
  value: {
    type: "authorization_request", request_id: id, message: "工具审批",
    data: {
      tool_call_id: "call-1", tool_identity: "mcp:test:read", tool_name: "read",
      source: "mcp", arguments: {}, contract_fingerprint: "test-contract",
    },
  },
});
type Step = { text?: string; next?: string; error?: string; tool?: boolean; responseOnly?: string };
type Bridge = NonNullable<ChatClientOptions["bridge"]>;

function setup(steps: Step[]) {
  const chunks = new Set<Parameters<Bridge["onChatChunk"]>[0]>();
  const tools = new Set<Parameters<NonNullable<Bridge["onToolCall"]>>[0]>();
  const completed: string[] = [];
  const errors: string[] = [];
  const motions: string[] = [];
  const approvals: boolean[] = [];
  const requests: { sessionId: string; requestId: string }[] = [];
  const bubbles: string[] = [];
  let draft = "";
  let pendingTools = 0;
  let completions = 0;
  let historyError = false;
  let onApproval: () => void | Promise<void> = () => {};
  let approvalDecision: boolean | ApprovalDecision = true;
  const input = { value: "开始", style: { height: "40px" }, focus() {} };
  const sendBtn = { disabled: false };
  const request = async (requestId: string, sessionId: string): Promise<ChatResult> => {
    requests.push({ requestId, sessionId });
    const step = steps.shift();
    assert.ok(step, "出现了多余请求");
    if (step.tool) for (const callback of tools) callback({ requestId, toolName: "read" });
    if (step.text) for (const callback of chunks) callback({ requestId, chunk: step.text, aggregated: step.text });
    if (step.error) throw new Error(step.error);
    return step.next
      ? { interrupted: true, interruptData: interrupt(step.next), response: step.text ?? "", model: "" }
      : { response: step.responseOnly ?? step.text ?? "", model: "" };
  };
  const bridge: Bridge = {
    chat: async (_message, sessionId, requestId) => request(requestId!, sessionId!),
    onChatChunk(callback) { chunks.add(callback); return () => { chunks.delete(callback); }; },
    onToolCall(callback) { tools.add(callback); return () => { tools.delete(callback); }; },
    getFrontendSettings: async () => ({ hide_on_screenshot: false }),
    claimToolContinuation: async () => {},
    toolAuthorizationRespond: async (payload) => {
      approvals.push(payload.decision.kind === "approve");
      return request(payload.streamRequestId!, payload.sessionId);
    },
  };
  const client = new ChatClient({
    sessionId: "session-a", bridge, input, sendBtn,
    bubble: { setText(text) { bubbles.push(text); } },
    confirmDialog: { isOpen: () => false, async open() { await onApproval(); return approvalDecision; } },
    toolCallToast: { show() {}, showError() {} },
    playMotion(tag) { motions.push(tag); },
    onChatComplete() { completions++; },
    chatHistory: {
      addMessage() {}, showTypingIndicator() {}, hideTypingIndicator() {},
      startStreaming() { draft = ""; },
      updateLastAiMessage(text) { draft = text; },
      finalizeStreamingMessage() { if (draft) completed.push(draft); draft = ""; },
      abortStreaming() { draft = ""; },
      showToolCallMessage() { pendingTools++; },
      finalizeToolCallIndicator() { pendingTools = 0; },
      showErrorMessage(message) { errors.push(message ?? ""); },
      async loadHistory() { if (historyError) throw new Error("历史离线"); },
    },
  });
  return {
    client, completed, errors, motions, approvals, requests, bubbles, input, sendBtn, bridge,
    chunks, tools,
    get pendingTools() { return pendingTools; },
    get completions() { return completions; },
    set historyError(value: boolean) { historyError = value; },
    set approved(value: boolean) { approvalDecision = value; },
    set approvalDecision(value: ApprovalDecision) { approvalDecision = value; },
    set onApproval(callback: () => void | Promise<void>) { onApproval = callback; },
  };
}

test("统一审批把引导决定直接交给后端", async () => {
  const h = setup([]);
  h.approvalDecision = { kind: "steer", message: "先检查配置" };
  const decisions: ApprovalDecision[] = [];
  h.bridge.toolAuthorizationRespond = async (payload) => {
    decisions.push(payload.decision);
    return { response: "已调整", model: "" };
  };

  await h.client.restorePendingInterrupt({ value: {
    type: "authorization_request",
    request_id: "auth-1",
    message: "调用工具？",
    data: {
      tool_call_id: "call-1",
      tool_identity: "local:run_ps",
      tool_name: "run_ps",
      source: "local",
      arguments: { command: "pwd" },
      contract_fingerprint: "contract-v1",
    },
  } });

  assert.deepEqual(decisions, [{ kind: "steer", message: "先检查配置" }]);
  assert.deepEqual(h.completed, ["已调整"]);
});

test("continuation 自动截屏且不打开审批框", async () => {
  const h = setup([]);
  let approvalCount = 0;
  h.onApproval = () => { approvalCount++; };
  h.bridge.captureScreen = async () => ({
    dataUrl: "data:image/png;base64,test",
    width: 800,
    height: 600,
  });
  const responses: Record<string, unknown>[] = [];
  h.bridge.toolContinuationRespond = async (payload) => {
    responses.push(payload.response);
    return { response: "截图完成", model: "" };
  };

  await h.client.restorePendingInterrupt({ value: {
    type: "continuation_request",
    request_id: "continuation-1",
    message: "截屏",
    data: { kind: "capture_screen", payload: {} },
  } });

  assert.equal(approvalCount, 0);
  assert.equal(responses[0]?.screenshot_data, "data:image/png;base64,test");
});

test("continuation 必须先通过后端复核才执行前端动作", async () => {
  const h = setup([]);
  let captureCount = 0;
  h.bridge.claimToolContinuation = async () => {
    throw new Error("授权已失效");
  };
  h.bridge.captureScreen = async () => {
    captureCount++;
    return { dataUrl: "data:image/png;base64,test", width: 1, height: 1 };
  };

  await h.client.restorePendingInterrupt({ value: {
    type: "continuation_request",
    request_id: "continuation-stale",
    message: "截屏",
    data: { kind: "capture_screen", payload: {} },
  } });

  assert.equal(captureCount, 0);
  assert.match(h.errors[0] ?? "", /授权已失效/);
});

test("恢复聊天在每次中断前完成当前文本段", async () => {
  const h = setup([{ text: "第一段", next: "second" }, { text: "第二段" }]);
  let confirmations = 0;
  h.onApproval = () => {
    if (++confirmations === 2) assert.deepEqual(h.completed, ["第一段"]);
  };
  await h.client.restorePendingInterrupt(interrupt("first"));
  assert.deepEqual(h.completed, ["第一段", "第二段"]);
  assert.deepEqual(h.errors, []);
  assert.equal(h.bubbles.at(-1), "第二段");
  assert.equal(h.sendBtn.disabled, false);
  assert.equal(h.chunks.size + h.tools.size, 0);
});

for (const entry of ["send", "restore"] as const) {
  const run = (h: ReturnType<typeof setup>) => entry === "send"
    ? h.client.sendMessage()
    : h.client.restorePendingInterrupt(interrupt("initial"));

  test(`${entry}: 连续中断分段、工具完成和表情使用同一规则`, async (t) => {
    t.mock.timers.enable({ apis: ["setInterval"] });
    const h = setup([
      { text: "<开心>第一段", tool: true, next: "second" },
      { text: "<开心>第二段", next: "third" },
      { text: "<点头>最后一段" },
    ]);
    let interruptSubscriptions = 0;
    h.bridge.onChatInterrupt = () => { interruptSubscriptions++; return () => {}; };
    h.onApproval = () => {
      assert.equal(h.sendBtn.disabled, true);
      assert.equal(h.pendingTools, 0);
      if (h.requests.length > 0) assert.deepEqual(h.motions, ["开心"]);
      const renders = h.bubbles.length;
      t.mock.timers.tick(1000);
      assert.equal(h.bubbles.length, renders, "等待审批时不应继续闪烁文本光标");
    };
    await run(h);
    assert.deepEqual(h.errors, []);
    assert.deepEqual(h.completed, ["第一段", "第二段", "最后一段"]);
    assert.deepEqual(h.motions, ["开心", "点头"]);
    assert.equal(h.completions, 1);
    assert.equal(h.requests.length, 3);
    assert.equal(interruptSubscriptions, 0);
    assert.equal(h.chunks.size + h.tools.size, 0);
    assert.equal(h.sendBtn.disabled, false);
    const renders = h.bubbles.length;
    t.mock.timers.tick(1000);
    assert.equal(h.bubbles.length, renders, "完成后不应有遗留计时器");
  });

  test(`${entry}: 后续请求失败只丢弃当前草稿`, async () => {
    const h = setup([{ text: "已完成", next: "second" }, { text: "未完成", error: "断线" }]);
    await run(h);
    assert.deepEqual(h.completed, ["已完成"]);
    assert.equal(h.errors.length, 1);
    assert.match(h.errors[0], /断线/);
    assert.equal(h.completions, 1);
    assert.equal(h.chunks.size + h.tools.size, 0);
    assert.equal(h.sendBtn.disabled, false);
  });

  test(`${entry}: 拒绝审批后仍让后端继续，跳过无文本阶段`, async () => {
    const h = setup([{ next: "second" }, { text: "已取消操作" }]);
    h.approved = false;
    await run(h);
    assert.deepEqual(h.errors, []);
    assert.ok(h.approvals.length > 0);
    assert.ok(h.approvals.every((approved) => !approved));
    assert.deepEqual(h.completed, ["已取消操作"]);
  });

  test(`${entry}: 忽略上一段迟到事件并保持会话标识`, async () => {
    const h = setup([{ text: "第一段", next: "second" }, { text: "第二段" }]);
    h.onApproval = () => {
      if (h.requests.length === 0) return;
      h.client.setSessionId("session-b");
      for (const callback of h.chunks) {
        callback({ requestId: h.requests[0].requestId, chunk: "迟到文本", aggregated: "迟到文本" });
      }
    };
    await run(h);
    assert.deepEqual(h.errors, []);
    assert.deepEqual(h.completed, ["第一段", "第二段"]);
    assert.ok(h.requests.every((request) => request.sessionId === "session-a"));
  });

  test(`${entry}: 只有返回文本时也保存回复，下一轮重置表情去重`, async () => {
    const h = setup([{ responseOnly: "<开心>完成" }, { responseOnly: "<开心>再次完成" }]);
    await run(h);
    h.input.value = "再来一次";
    await run(h);
    assert.deepEqual(h.errors, []);
    assert.deepEqual(h.completed, ["完成", "再次完成"]);
    assert.deepEqual(h.motions, ["开心", "开心"]);
    assert.equal(h.completions, 2);
  });
}

test("历史同步失败不撤销已完成回复，也不覆盖桌宠气泡", async () => {
  const h = setup([{ text: "完成" }]);
  h.historyError = true;
  await h.client.restorePendingInterrupt(interrupt("initial"));
  assert.deepEqual(h.completed, ["完成"]);
  assert.equal(h.bubbles.at(-1), "完成");
  assert.equal(h.errors.length, 1);
  assert.match(h.errors[0], /对话已完成.*历史记录同步失败/);
  assert.equal(h.completions, 1);
  assert.equal(h.sendBtn.disabled, false);
});

test("等待审批时拒绝重复启动，整轮完成前入口 Promise 不结束", async () => {
  const h = setup([{ text: "完成" }]);
  let approve!: () => void;
  let settled = false;
  h.onApproval = () => new Promise<void>((resolve) => { approve = resolve; });
  const running = h.client.restorePendingInterrupt(interrupt("initial")).then(() => { settled = true; });
  await h.client.sendMessage();
  await h.client.restorePendingInterrupt(interrupt("duplicate"));
  assert.equal(settled, false);
  assert.equal(h.requests.length, 0);
  approve();
  await running;
  assert.equal(h.requests.length, 1);
  assert.equal(h.completions, 1);
  assert.deepEqual(h.errors, []);
});

for (const entry of ["send", "restore"] as const) {
  const begin = async (h: ReturnType<typeof setup>, initial: ChatInterruptPayload) => {
    if (entry === "restore") return h.client.restorePendingInterrupt(initial);
    h.bridge.chat = async () => ({ interrupted: true, interruptData: initial, response: "", model: "" });
    await h.client.sendMessage();
  };
  for (const approved of [true, false]) {
    test(`${entry}: 截图授权${approved ? "批准" : "拒绝"}后才允许 continuation 截屏`, async () => {
      const h = setup([]);
      h.approved = approved;
      let captures = 0;
      const decisions: ApprovalDecision[] = [];
      const responses: Record<string, unknown>[] = [];
      h.bridge.captureScreen = async () => {
        captures++;
        return { dataUrl: "data:image/png;base64,test", width: 800, height: 600 };
      };
      h.bridge.toolAuthorizationRespond = async (payload) => {
        decisions.push(payload.decision);
        if (approved) return {
          interrupted: true, response: "", model: "",
          interruptData: { value: {
            type: "continuation_request", request_id: "capture-id", message: "截屏",
            data: { kind: "capture_screen", payload: {} },
          } },
        };
        return { response: "已拒绝", model: "" };
      };
      h.bridge.toolContinuationRespond = async (payload) => {
        responses.push(payload.response);
        return { response: "完成", model: "" };
      };
      await begin(h, interrupt("screenshot-id"));
      assert.deepEqual(h.errors, []);
      assert.equal(captures, approved ? 1 : 0);
      assert.deepEqual(decisions, [{ kind: approved ? "approve" : "reject" }]);
      assert.equal(responses.length, approved ? 1 : 0);
      if (approved) assert.equal(responses[0].screenshot_data, "data:image/png;base64,test");
      assert.deepEqual(h.completed, [approved ? "完成" : "已拒绝"]);
    });
  }
  for (const outcome of ["approved", "denied", "failed"] as const) {
    test(`${entry}: 屏幕操作 ${outcome} 通过统一授权和 continuation`, async () => {
      const h = setup([]);
      h.approved = outcome !== "denied";
      let actions = 0;
      let captures = 0;
      let claims = 0;
      const decisions: ApprovalDecision[] = [];
      const responses: Record<string, unknown>[] = [];
      h.bridge.captureScreen = async () => {
        captures++;
        return { dataUrl: "data:image/png;base64,test", width: 800, height: 600 };
      };
      h.bridge.claimToolContinuation = async () => { claims++; };
      h.bridge.performScreenAction = async () => {
        actions++;
        return outcome === "failed" ? { executed: false, error: "执行失败" } : { executed: true };
      };
      const action: ControlScreenActionData = {
        target: "按钮", operation: "click", text: "", press_enter: false,
        wait_seconds: 0, scroll_direction: "none",
        coordinates: { bbox: [400, 400, 600, 600], x_ratio: 0.5, y_ratio: 0.5 },
      };
      h.bridge.toolAuthorizationRespond = async (payload) => {
        decisions.push(payload.decision);
        if (outcome === "denied") return { response: "已拒绝", model: "" };
        return { interrupted: true, response: "", model: "", interruptData: { value: {
          type: "continuation_request", request_id: "execute-id", message: "执行",
          data: { kind: "perform_screen_action", payload: action },
        } } };
      };
      h.bridge.toolContinuationRespond = async (payload) => {
        responses.push(payload.response);
        return { response: "处理完成", model: "" };
      };
      await begin(h, interrupt("approval-id"));
      assert.deepEqual(h.errors, []);
      assert.deepEqual(decisions, [{ kind: outcome === "denied" ? "reject" : "approve" }]);
      assert.equal(claims, outcome === "denied" ? 0 : 1);
      assert.equal(actions, claims);
      assert.equal(captures, outcome === "approved" ? 1 : 0);
      assert.equal(responses.length, claims);
      if (claims) {
        assert.equal(responses[0].executed, outcome === "approved");
        assert.equal(responses[0].error, outcome === "failed" ? "执行失败" : undefined);
      }
      assert.deepEqual(h.completed, [outcome === "denied" ? "已拒绝" : "处理完成"]);
    });
  }
}
