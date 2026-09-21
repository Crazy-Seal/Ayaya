import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { createServer, type ServerResponse } from "node:http";
import { setImmediate } from "node:timers/promises";
import { createRequire } from "node:module";

// Undici 的传输时钟独立于 Date；使用其测试钩子推进时间，保留真实 HTTP。
const transportClock = createRequire(import.meta.url)("undici/lib/util/timers.js") as {
  tick(delay: number): void;
};
import { configureBackendClient } from "../electron/backend-client.js";
import { requestChatStream } from "../electron/chat-stream-request.js";

const originalFetch = globalThis.fetch;
const encoder = new TextEncoder();
afterEach(() => { globalThis.fetch = originalFetch; });

test("真实传输在等待响应头及静默正文超过 5 分钟后仍可完成", async (t) => {
  let respond!: (response: ServerResponse) => void;
  const received = new Promise<ServerResponse>((resolve) => { respond = resolve; });
  const server = createServer((_request, response) => respond(response));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  t.after(() => { server.closeAllConnections(); server.close(); });
  configureBackendClient("test-token");
  globalThis.fetch = async (_url, init) => originalFetch(`http://127.0.0.1:${address.port}/chat`, init);
  let failure: unknown;
  let firstChunk!: () => void;
  const chunkReceived = new Promise<void>((resolve) => { firstChunk = resolve; });
  const pending = requestChatStream("/chat", {}, { onChunk: () => firstChunk() });
  void pending.catch((error) => { failure = error; });
  const response = await received;
  const advance = async () => {
    transportClock.tick(1000);
    transportClock.tick(360_000);
    for (let i = 0; i < 10; i++) await setImmediate();
    assert.equal(failure, undefined);
  };
  await advance();
  response.writeHead(200, { "Content-Type": "text/event-stream" });
  response.write('data: {"response":"开始"}\n\n');
  await chunkReceived;
  await advance();
  response.end('data: [DONE]\n\n');
  assert.equal((await pending).response, "开始");
});

for (const path of ["/chat", "/tool-authorization/respond", "/tool-continuation/respond"]) {
  test(`${path}: 统一保留后端错误与传输失败原因`, async () => {
    configureBackendClient("test-token");
    globalThis.fetch = async () => new Response("模型请求超时", { status: 504 });
    await assert.rejects(requestChatStream(path, {}, {}), /模型请求超时/);
    globalThis.fetch = async () => new Response("", { status: 503 });
    await assert.rejects(requestChatStream(path, {}, {}), /请求失败: 503/);
    const disconnected = new TypeError("terminated");
    globalThis.fetch = async () => { throw disconnected; };
    await assert.rejects(requestChatStream(path, {}, {}), (error) => error === disconnected);
  });
}

test("请求保留认证与业务字段，并转发工具事件和恢复终态", async () => {
  configureBackendClient("test-token");
  const interrupt = { value: { type: "authorization_request", request_id: "persisted-id" } };
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(
        'event: tool_call\ndata: {"tool_name":"screenshot"}\n\n' +
        `event: interrupt\ndata: ${JSON.stringify(interrupt)}\n\n`,
      ));
    },
    cancel() { cancelled = true; },
  });
  globalThis.fetch = async (url, init) => {
    assert.ok(String(url).endsWith("/tool-authorization/respond"));
    assert.equal(init?.method, "POST");
    assert.equal(new Headers(init?.headers).get("Authorization"), "Bearer test-token");
    assert.deepEqual(JSON.parse(String(init?.body)), {
      session_id: "session", request_id: "persisted-id", kind: "reject",
    });
    return new Response(stream);
  };
  const tools: string[] = [];
  const interrupts: unknown[] = [];
  const result = await requestChatStream("/tool-authorization/respond", {
    session_id: "session", request_id: "persisted-id", kind: "reject",
  }, { onToolCall: (name) => tools.push(name), onInterrupt: (data) => interrupts.push(data) });
  assert.deepEqual(tools, ["screenshot"]);
  assert.deepEqual(interrupts, [interrupt]);
  assert.equal(result.interrupted, true);
  assert.deepEqual(result.interruptData, interrupt);
  assert.equal(cancelled, true);
  assert.equal(stream.locked, false);
});

test("流错误和非法数据释放资源且不会被清理失败覆盖", async () => {
  for (const data of ['event: error\ndata: {"detail":"工具执行失败"}\n\n', 'data: invalid\n\n']) {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(encoder.encode(data)); },
      cancel() { cancelled = true; return new Promise<void>(() => {}); },
    });
    globalThis.fetch = async () => new Response(stream);
    configureBackendClient("test-token");
    await assert.rejects(requestChatStream("/chat", {}, {}),
      data.startsWith("event") ? /工具执行失败/ : SyntaxError);
    assert.equal(cancelled, true);
    assert.equal(stream.locked, false);
  }
});

test("响应缺流或提前断流不能被当作成功", async () => {
  configureBackendClient("test-token");
  globalThis.fetch = async () => new Response(null);
  await assert.rejects(requestChatStream("/chat", {}, {}), /缺少数据流/);
  const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.close(); } });
  globalThis.fetch = async () => new Response(stream);
  await assert.rejects(requestChatStream("/chat", {}, {}), /明确终态前结束/);
  assert.equal(stream.locked, false);
});

test("聊天流超过 15 分钟仍可继续输出并正常完成", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  configureBackendClient("test-token");
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let signal: AbortSignal | null | undefined;
  const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
  globalThis.fetch = async (_url, init) => {
    signal = init?.signal;
    return new Response(stream);
  };
  const chunks: string[] = [];
  const pending = requestChatStream("/chat", { message: "长任务" }, {
    onChunk(chunk) { chunks.push(chunk); },
  });
  await Promise.resolve();
  t.mock.timers.tick(1_800_000);
  assert.ok(!signal?.aborted);
  controller.enqueue(encoder.encode('data: {"response":"完成"}\n\ndata: [DONE]\n\n'));
  assert.equal((await pending).response, "完成");
  assert.deepEqual(chunks, ["完成"]);
  assert.equal(stream.locked, false);
});
