import assert from "node:assert/strict";
import { test } from "node:test";
import { DesktopSessionController } from "../electron/desktop-session-controller.js";
import type { InteractionEvent, InteractionStreamReady } from "../shared-types.js";

/** 等待网络读取与事件转发完成一轮调度。 */
const flush = async (): Promise<void> => { await new Promise<void>((resolve) => { setImmediate(resolve); }); };

/** 等待一次受控重连完成，超时明确报告未出现目标连接。 */
const waitFor = async (condition: () => boolean): Promise<void> => {
  for (let attempt = 0; attempt < 100 && !condition(); attempt++) await new Promise<void>((resolve) => { setTimeout(resolve, 2); });
  assert.ok(condition(), "异步连接未按期推进");
};

/** 创建测试会话的快照或实时事件。 */
const event = (session: string, sequence: number, kind: InteractionEvent["kind"] = "text_chunk"): InteractionEvent => ({
  session_id: session, kind, stream_epoch: "epoch", event_sequence: sequence, payload: { value: String(sequence), phase: "idle" },
});

/** 用受控网络响应验证公开连接与视图操作。 */
class Harness {
  paths: string[] = [];
  streams: ReadableStreamDefaultController<Uint8Array>[] = [];
  snapshots: { path: string; signal: AbortSignal; resolve: (response: Response) => void }[] = [];
  events: InteractionEvent[] = [];
  observed: InteractionEvent[] = [];
  disconnected = 0;
  controller = new DesktopSessionController({
    /** 只替代网络，快照由测试明确放行。 */
    fetchBackend: async (path, init) => {
      this.paths.push(path);
      if (path.startsWith("/agent/snapshot/")) {
        return await new Promise<Response>((resolve) => {
          this.snapshots.push({ path, signal: init.signal as AbortSignal, resolve });
        });
      }
      return new Response(new ReadableStream<Uint8Array>({
        /** 保存 SSE 写入端，取消时结束待读取。 */
        start: (writer) => {
          this.streams.push(writer);
          init.signal?.addEventListener("abort", () => { try { writer.error(new Error("取消")); } catch {} }, { once: true });
        },
      }));
    },
    /** 收集页面实际收到的事件。 */
    deliverEvent: (value) => { this.events.push(value); },
    /** 提醒在当前页面等待快照期间也持续更新。 */
    observeEvent: (value) => { this.observed.push(value); },
    /** 测试不写日志。 */
    log: () => {},
    /** 记录全局断线通知。 */
    onDisconnected: () => { this.disconnected++; },
    reconnectDelayMs: 1,
  });

  /** 在指定连接写入一条实际 SSE 帧。 */
  send(value: InteractionEvent | InteractionStreamReady, index = this.streams.length - 1): void {
    this.streams[index].enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`));
  }

  /** 标记当前全局连接已经在后端登记。 */
  ready(): void { this.send({ kind: "stream_ready", stream_epoch: "epoch" }); }

  /** 交付一次快照查询结果。 */
  snapshot(index: number, session: string, sequence: number): void {
    this.snapshots[index].resolve(Response.json(event(session, sequence, "session_snapshot")));
  }
}

test("启动只连接全局流，就绪前不查询快照，切换会话复用连接", async () => {
  const h = new Harness();
  try {
    h.controller.start();
    await flush();
    assert.deepEqual(h.paths, ["/agent/events?channel=desktop"]);
    const first = h.controller.initializeSession("A");
    await flush();
    assert.equal(h.snapshots.length, 0);
    h.ready();
    await flush();
    h.snapshot(0, "A", 0);
    await first;
    const second = h.controller.initializeSession("B");
    await flush();
    h.snapshot(1, "B", 0);
    await second;
    assert.equal(h.streams.length, 1);
    assert.deepEqual(h.snapshots.map((s) => s.path), ["/agent/snapshot/A?channel=desktop", "/agent/snapshot/B?channel=desktop"]);
  } finally { h.controller.stop(); }
});

test("快照查询期间缓冲当前会话，快照后只交付更新事件，其他会话独立处理", async () => {
  const h = new Harness();
  try {
    const first = h.controller.initializeSession("A");
    await flush(); h.ready(); await flush();
    h.send(event("A", 100)); h.send(event("B", 3)); h.send(event("A", 101));
    await flush();
    assert.deepEqual(h.events.map((e) => [e.session_id, e.event_sequence]), [["B", 3]]);
    assert.deepEqual(h.observed.map((e) => [e.session_id, e.event_sequence]), [["A", 100], ["B", 3], ["A", 101]]);
    h.snapshot(0, "A", 100); await first;
    assert.deepEqual(h.events.map((e) => [e.session_id, e.event_sequence]), [["B", 3], ["A", 100], ["A", 101]]);
    h.send(event("B", 4)); h.send(event("A", 101)); await flush();
    assert.equal(h.events.at(-1)?.event_sequence, 4);
  } finally { h.controller.stop(); }
});

test("快速切换取消旧快照，忽略取消的迟到响应无法进入新视图", async () => {
  const h = new Harness();
  try {
    const first = h.controller.initializeSession("A");
    await flush(); h.ready(); await flush();
    const second = h.controller.initializeSession("B"); await flush();
    assert.equal(h.snapshots[0].signal.aborted, true);
    h.snapshot(1, "B", 3); await second;
    h.snapshot(0, "A", 100); await first;
    assert.deepEqual(h.events.map((e) => e.session_id), ["B"]);
  } finally { h.controller.stop(); }
});

test("断线取消当前快照，重连只刷新最后选择的会话并拒绝旧响应", async () => {
  const h = new Harness();
  try {
    const first = h.controller.initializeSession("A");
    await flush(); h.ready(); await flush();
    h.snapshot(0, "A", 0); await first;
    const second = h.controller.initializeSession("B"); await flush();
    h.streams[0].close();
    await waitFor(() => h.streams.length === 2);
    assert.equal(h.disconnected, 1);
    assert.equal(h.snapshots[1].signal.aborted, true);
    h.ready(); await flush();
    assert.equal(h.snapshots[2].path, "/agent/snapshot/B?channel=desktop");
    h.snapshot(2, "B", 4); await flush();
    h.snapshot(1, "B", 2); await second;
    assert.deepEqual(h.events.map((e) => [e.session_id, e.event_sequence]), [["A", 0], ["B", 4]]);
  } finally { h.controller.stop(); }
});

test("后端重启更换 epoch，当前快照与新序号建立新的展示状态", async () => {
  const h = new Harness();
  try {
    const first = h.controller.initializeSession("A");
    await flush(); h.ready(); await flush(); h.snapshot(0, "A", 100); await first;
    h.streams[0].close(); await waitFor(() => h.streams.length === 2);
    h.send({ kind: "stream_ready", stream_epoch: "new-epoch" }); await flush();
    h.snapshots[1].resolve(Response.json({ ...event("A", 0, "session_snapshot"), stream_epoch: "new-epoch" }));
    await flush();
    h.send({ ...event("A", 1), stream_epoch: "new-epoch" }); await flush();
    assert.deepEqual(h.events.slice(-2).map((e) => [e.stream_epoch, e.event_sequence]), [["new-epoch", 0], ["new-epoch", 1]]);
  } finally { h.controller.stop(); }
});

test("关闭应用释放就绪等待，准备身份失败会重试且不创建多条连接", async () => {
  let identities = 0;
  const h = new Harness();
  const controller = new DesktopSessionController({
    /** 第一次身份查询失败，下一次进入实际受控网络。 */
    onConnected: async () => { if (++identities === 1) throw new Error("暂时不可用"); },
    /** 只接受全局订阅连接。 */
    fetchBackend: async (path) => { h.paths.push(path); return new Response(new ReadableStream({ start: () => {} })); },
    /** 此用例在就绪前停止。 */
    deliverEvent: () => {},
    /** 测试不写日志。 */
    log: () => {}, reconnectDelayMs: 1,
  });
  const waiting = controller.ensureReady();
  await waitFor(() => h.paths.length === 1);
  controller.stop();
  await assert.rejects(waiting, /连接已停止/);
  assert.equal(identities, 2);
});
