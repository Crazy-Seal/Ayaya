import assert from "node:assert/strict";
import { test } from "node:test";
import { DesktopSessionController } from "../electron/desktop-session-controller.js";
import { RuntimeChatClient } from "../src/chat/runtime-chat-client.js";
import type { RuntimeChatClientOptions } from "../src/chat/runtime-chat-client.js";
import type { ApprovalDecision, ChannelCommand, InteractionEvent } from "../shared-types.js";

/**
 * 建立可由测试显式结束的等待，不连接真实后端。
 *
 * Returns:
 *     { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void; }: Promise 及完成、失败函数，用于查询或连接屏障。
 */
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：建立可由测试显式结束的等待，不连接真实后端。
   *
   * Args:
   *     done ((value: T | PromiseLike<T>) => void): 让测试等待以成功结果结束的函数。
   *     fail ((reason?: any) => void): 让测试等待以脚本异常结束的函数。
   */
  (done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

/**
 * 等待当前流读取和页面事件链完成一轮调度。
 *
 * Returns:
 *     Promise<void>: 本轮异步回调已获得执行机会。
 */
const flush = (): Promise<void> => new Promise<void>(/**
 * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
 *
 * 此定义用于：等待当前流读取和页面事件链完成一轮调度。
 *
 * Args:
 *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
 */
(resolve) => { setImmediate(resolve); });

/**
 * 构造带测试 epoch 的真实线协议事件。
 *
 * Args:
 *     kind (InteractionEvent["kind"]): 事件类别。
 *     sequence (number): 会话发布编号。
 *     payload (Record<string, unknown>): 对应事件内容。
 *
 * Returns:
 *     InteractionEvent: 可写入 SSE 测试流的事件。
 */
const event = (kind: InteractionEvent["kind"], sequence: number, payload: Record<string, unknown> = {}): InteractionEvent => ({
  session_id: "s", kind, payload, stream_epoch: "epoch", event_sequence: sequence, run_id: "run",
});

/**
 * 提供受控 SSE 响应，取消信号会让等待读取结束。
 *
 * Attributes:
 *     response (Response): 控制器读取的响应。
 *     writer (ReadableStreamDefaultController<Uint8Array>): 测试写入帧的端口。
 */
class TestConnection {
  readonly response: Response;
  private writer!: ReadableStreamDefaultController<Uint8Array>;

  /**
   * 创建测试响应并绑定连接取消。
   *
   * Args:
   *     signal (AbortSignal): 本次后端请求的取消信号。
   */
  constructor(signal: AbortSignal) {
    /**
     *  取消连接时结束 pending reader，避免测试留下后台读取。
     */
    const abort = (): void => {
      signal.removeEventListener("abort", abort);
      this.writer.error(new Error("测试连接已取消"));
    };
    const body = new ReadableStream<Uint8Array>({
      /**
       * 保存流写入端口并开始监听取消。
       *
       * Args:
       *     writer (ReadableStreamDefaultController<Uint8Array<ArrayBufferLike>>): SSE 字节流写入端口。
       */
      start: (writer) => {
        this.writer = writer;
        signal.addEventListener("abort", abort, { once: true });
      },
      /**
       *  读取方结束时移除取消监听。
       */
      cancel: () => { signal.removeEventListener("abort", abort); },
    });
    this.response = new Response(body);
  }

  /**
   * 写入一条完整 JSON SSE 帧。
   *
   * Args:
   *     value (InteractionEvent): 当前连接应交付的事件。
   */
  send(value: InteractionEvent): void {
    this.writer.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`));
  }
}

/**
 * 使用真实订阅控制器，替换网络、窗口转发和日志。
 *
 * Attributes:
 *     connections (TestConnection[]): 真实控制器发起的受控连接。
 *     listeners (Set<(event: InteractionEvent) => void>): 当前页面的事件监听。
 *     received (InteractionEvent[]): 主进程转发过的事件。
 *     commands (ChannelCommand[]): 页面实际提交的命令。
 *     controller (DesktopSessionController): 待验证的生产订阅控制器。
 */
class Harness {
  readonly connections: TestConnection[] = [];
  readonly listeners = new Set<(value: InteractionEvent) => void>();
  readonly received: InteractionEvent[] = [];
  readonly commands: ChannelCommand[] = [];
  readonly controller: DesktopSessionController;
  readonly bridge: NonNullable<RuntimeChatClientOptions["bridge"]>;
  readonly firstResponse = deferred<Response>();

  /**
   * 创建主进程替身，允许第一条请求故意延迟返回。
   *
   * Args:
   *     delayFirst (boolean): 是否忽略首个 fetch 的取消，模拟旧请求迟到。
   */
  constructor(delayFirst = false) {
    this.controller = new DesktopSessionController({
      /**
       * 创建测试 SSE，第一条请求可由外部屏障放行。
       *
       * Args:
       *     path (string): 后端请求路径。
       *     init (RequestInit): 订阅取消信号。
       *
       * Returns:
       *     Promise<Response>: 真实解码器读取的 SSE 响应。
       */
      fetchBackend: async (path, init) => {
        assert.match(path, /\/agent\/events\/s\?channel=desktop/);
        const connection = new TestConnection(init.signal as AbortSignal);
        this.connections.push(connection);
        return delayFirst && this.connections.length === 1 ? this.firstResponse.promise : connection.response;
      },
      /**
       * 将当前连接事件交给活着的页面监听。
       *
       * Args:
       *     value (InteractionEvent): 控制器确认可以转发的事件。
       */
      deliverEvent: (value) => {
        this.received.push(value);
        for (const listener of this.listeners) listener(value);
      },
      /**
       *  测试不写真实应用日志。
       */
      log: () => {},
      reconnectDelayMs: 1,
    });
    this.bridge = {
      subscribeDesktopSession: this.controller.subscribe.bind(this.controller),
      /**
       * 保存页面监听，并提供页面销毁时的移除函数。
       *
       * Args:
       *     listener ((event: InteractionEvent) => void): 页面事件处理入口。
       *
       * Returns:
       *     () => void: 移除该页面的监听。
       */
      onInteractionEvent: (listener) => {
        this.listeners.add(listener);
        /**
         * 由外层操作在对应处理阶段调用，移除该项已登记记录。
         *
         * 此定义用于：保存页面监听，并提供页面销毁时的移除函数。
         */
        return () => { this.listeners.delete(listener); };
      },
      /**
       * 记录实际发送的命令，不运行 Agent。
       *
       * Args:
       *     command (ChannelCommand): 页面决定提交的动作。
       *
       * Returns:
       *     Promise<{ accepted: true; run_id: string; }>: 成功受理回执。
       */
      submitInteractionCommand: async (command) => {
        this.commands.push(command);
        return { accepted: true, run_id: "run" };
      },
      /**
       *  返回空任务列表，避免访问生产数据。
       */
      getBackgroundTasks: async () => [],
      /**
       *  返回空输出页，避免访问生产数据。
       */
      getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
    };
  }
}

/**
 *  提供无需执行动作的展示端口替身。
 */
const noop = (): void => {};

/**
 * 创建真实聊天客户端，用简单展示端口记录回复、审批和历史查询。
 *
 * Args:
 *     bridge (RuntimeChatClientOptions["bridge"]): 主进程控制器桥接端口。
 *     history (Promise<void>): 历史读取完成屏障。
 *
 * Returns:
 *     { client: RuntimeChatClient; button: { disabled: boolean; innerHTML: string; title: string; setAttribute: () => void; }; replies: string[]; errors: string[]; approvals: () => number; dismissed: () => number; }: 客户端、按钮和记录过的显示结果。
 */
const makePage = (bridge: RuntimeChatClientOptions["bridge"], history = Promise.resolve()) => {
  const replies: string[] = [];
  const errors: string[] = [];
  let approvals = 0;
  let dismissed = 0;
  const button = { disabled: false, innerHTML: "send", title: "", setAttribute: noop };
  const input = { value: "新指令", style: { height: "" }, focus: noop };
  const client = new RuntimeChatClient({
    sessionId: "s", bridge, sendBtn: button as unknown as HTMLButtonElement,
    input: input as unknown as HTMLTextAreaElement,
    bubble: { setText: noop },
    chatHistory: {
      addMessage: noop, showTypingIndicator: noop, hideTypingIndicator: noop, startStreaming: noop,
      finalizeStreamingMessage: noop, abortStreaming: noop, showToolCallMessage: noop,
      finalizeToolCallIndicator: noop,
      /**
       * 保存本次正文显示结果。
       *
       * Args:
       *     text (string): 应显示的累计正文。
       */
      updateLastAiMessage: (text) => { replies.push(text); },
      /**
       * 保存查询失败的可见错误。
       *
       * Args:
       *     text (string | undefined): 错误说明。
       */
      showPluginNotice: () => {},
      showErrorMessage: (text) => { errors.push(text ?? ""); },
      /**
       * 等待当前页面历史读取屏障并返回隔离历史。
       *
       * Returns:
       *     Promise<never[]>: 当前页面已应用的隔离历史列表。
       */
      loadHistory: async () => { await history; return []; },
    },
    confirmDialog: {
      /**
       *  展示审批并保持未决定，用后续事件验证关闭。
       */
      open: async () => {
        approvals += 1;
        return new Promise<ApprovalDecision>(/**
         * 由 Promise 构造器立即调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：展示审批并保持未决定，用后续事件验证关闭。
         */
        () => {});
      },
      /**
       *  记录后端已处理审批时关闭模态框。
       */
      dismiss: () => { dismissed += 1; },
    },
  });
  return { client, button, replies, errors,
    /**
     *  返回已打开的审批数。
     */
    approvals: () => approvals,
    /**
     *  返回后端事件关闭的审批数。
     */
    dismissed: () => dismissed,
  };
};

test("同会话页面重载取得新快照，历史完成前不能提交，之后等待完整回答并恢复审批", /**
 * 验证同会话页面重载取得新快照，历史完成前不能提交，之后等待完整回答并恢复审批。
 */
async () => {
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证同会话页面重载取得新快照，历史完成前不能提交，之后等待完整回答并恢复审批。
   */
  getElementById: () => null } as unknown as Document;
  const harness = new Harness();
  const history = deferred<void>();
  try {
    // 1. 旧页面初始化后，普通发送复用原连接。
    const old = makePage(harness.bridge);
    harness.connections[0].send(event("session_snapshot", 0, { phase: "idle" }));
    await flush();
    await old.client.sendMessage();
    assert.equal(harness.commands.length, 1);
    assert.equal(harness.connections.length, 1);
    // 2. 页面销毁并重建，主进程仍活着；必须建立第二条连接而非复用旧快照完成标记。
    harness.listeners.clear();
    const page = makePage(harness.bridge, history.promise);
    assert.equal(harness.connections.length, 2);
    assert.equal(page.button.disabled, true);
    await page.client.sendMessage();
    harness.connections[1].send(event("session_snapshot", 1, { phase: "running", run_id: "run" }));
    await flush();
    await page.client.sendMessage();
    assert.equal(harness.commands.length, 1);
    harness.connections[1].send(event("text_chunk", 2, { value: "天气" }));
    await flush();
    assert.deepEqual(page.replies, []);
    // 3. 历史完成后仍不显示半段回答，发送继续复用新连接。
    history.resolve();
    await flush();
    assert.deepEqual(page.replies, []);
    assert.equal(page.button.disabled, false);
    await page.client.sendMessage();
    assert.equal(harness.commands.at(-1)?.kind, "human_steer");
    assert.equal(harness.commands.at(-1)?.target_run_id, "run");
    assert.equal(harness.connections.length, 2);
    harness.connections[1].send(event("interrupt", 3, { value: { type: "authorization_request", request_id: "approval" } }));
    await flush();
    assert.equal(page.approvals(), 1);
    assert.equal(page.button.disabled, true);
    harness.connections[1].send({ ...event("authorization_resolved", 4), request_id: "approval" });
    harness.connections[1].send(event("run_completed", 5));
    await flush();
    assert.equal(page.dismissed(), 1);
    assert.equal(page.button.disabled, false);
  } finally {
    history.resolve();
    harness.controller.stop();
    globalThis.document = oldDocument;
    await flush();
  }
});

test("旧请求忽略取消后迟到，其快照不会进入当前连接或完成新等待", /**
 * 验证旧请求忽略取消后迟到，其快照不会进入当前连接或完成新等待。
 */
async () => {
  const harness = new Harness(true);
  try {
    const old = harness.controller.subscribe("s");
    const current = harness.controller.subscribe("s", { refreshSnapshot: true });
    let completed = false;
    void current.then(/**
     * 由上一步异步操作成功完成后调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证旧请求忽略取消后迟到，其快照不会进入当前连接或完成新等待。
     */
    () => { completed = true; });
    harness.firstResponse.resolve(harness.connections[0].response);
    await old;
    await flush();
    assert.equal(completed, false);
    assert.equal(harness.received.length, 0);
    harness.connections[1].send(event("session_snapshot", 9, { phase: "idle" }));
    await current;
    assert.equal(harness.received.length, 1);
    assert.equal(harness.received[0].event_sequence, 9);
    assert.strictEqual(harness.controller.subscribe("s"), current);
  } finally {
    harness.controller.stop();
    await flush();
  }
});

test("历史读取失败仍应用快照并允许之后发送", /**
 * 验证历史读取失败仍应用快照并允许之后发送。
 */
async () => {
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证历史读取失败仍应用快照并允许之后发送。
   */
  getElementById: () => null } as unknown as Document;
  const harness = new Harness();
  const history = deferred<void>();
  try {
    const page = makePage(harness.bridge, history.promise);
    harness.connections[0].send(event("session_snapshot", 0, { phase: "idle" }));
    await flush();
    history.reject(new Error("模拟历史读取失败"));
    await flush();
    assert.match(page.errors[0], /模拟历史读取失败/);
    assert.equal(page.button.disabled, false);
    await page.client.sendMessage();
    assert.equal(harness.commands.length, 1);
  } finally {
    harness.controller.stop();
    globalThis.document = oldDocument;
    await flush();
  }
});

test("每次 SSE 重连先登记窗口，登记失败不创建额外订阅", /** 验证窗口身份更新与订阅重试的顺序。 */
async () => {
  let registrations = 0, requests = 0, disconnections = 0;
  const controller = new DesktopSessionController({
    /** 首次登记失败，下一次及重连登记成功。 */ onConnected: async () => {
      registrations++;
      if (registrations === 1) throw new Error("登记失败");
    },
    /** 断线后仅通知检查恢复记录。 */ onDisconnected: () => { disconnections++; },
    /** 首个流立即结束，第二个保持到测试取消，观察自动重连先重新登记。 */ fetchBackend: async (_path, init) => {
      requests++;
      if (requests === 1) return new Response("");
      const connection = new TestConnection(init.signal as AbortSignal);
      connection.send(event("session_snapshot", 0, { phase: "idle" }));
      return connection.response;
    },
    /** 本用例只验证连接和登记数量。 */ deliverEvent: () => {},
    /** 模拟失败不写生产日志。 */ log: () => {},
    reconnectDelayMs: 1,
  });
  try {
    await controller.subscribe("s");
    assert.equal(registrations, 3); assert.equal(requests, 2); assert.equal(disconnections, 2);
  } finally { controller.stop(); await flush(); }
});
