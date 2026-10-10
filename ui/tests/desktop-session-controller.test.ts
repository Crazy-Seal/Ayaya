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
 *     object: Promise 及完成、失败函数，用于查询或连接屏障。
 */
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>(/**
   * 保存测试等待的结束函数。
   *
   * Args:
   *     done ((value: T | PromiseLike<T>) => void): 成功完成函数。
   *     fail ((reason: unknown) => void): 失败完成函数。
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
 * 将测试继续安排到微任务处理之后。
 *
 * Args:
 *     resolve (() => void): 下一轮继续测试的函数。
 */
(resolve) => { setImmediate(resolve); });

/**
 * 构造带测试 epoch 的真实线协议事件。
 *
 * Args:
 *     kind (string): 事件类别。
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
    /** 取消连接时结束 pending reader，避免测试留下后台读取。 */
    const abort = (): void => {
      signal.removeEventListener("abort", abort);
      this.writer.error(new Error("测试连接已取消"));
    };
    const body = new ReadableStream<Uint8Array>({
      /**
       * 保存流写入端口并开始监听取消。
       *
       * Args:
       *     writer (ReadableStreamDefaultController<Uint8Array>): SSE 字节流写入端口。
       */
      start: (writer) => {
        this.writer = writer;
        signal.addEventListener("abort", abort, { once: true });
      },
      /** 读取方结束时移除取消监听。 */
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
      /** 测试不写真实应用日志。 */
      log: () => {},
      reconnectDelayMs: 1,
    });
    this.bridge = {
      subscribeDesktopSession: this.controller.subscribe.bind(this.controller),
      /**
       * 保存页面监听，并提供页面销毁时的移除函数。
       *
       * Args:
       *     listener ((value: InteractionEvent) => void): 页面事件处理入口。
       *
       * Returns:
       *     () => void: 移除该页面的监听。
       */
      onInteractionEvent: (listener) => {
        this.listeners.add(listener);
        /** 模拟页面销毁时解除 IPC 监听。 */
        return () => { this.listeners.delete(listener); };
      },
      /**
       * 记录实际发送的命令，不运行 Agent。
       *
       * Args:
       *     command (ChannelCommand): 页面决定提交的动作。
       *
       * Returns:
       *     Promise<object>: 成功受理回执。
       */
      submitInteractionCommand: async (command) => {
        this.commands.push(command);
        return { accepted: true, run_id: "run" };
      },
      /** 返回空任务列表，避免访问生产数据。 */
      getBackgroundTasks: async () => [],
      /** 返回空输出页，避免访问生产数据。 */
      getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
    };
  }
}

/** 提供无需执行动作的展示端口替身。 */
const noop = (): void => {};

/**
 * 创建真实聊天客户端，用简单展示端口记录回复、审批和历史查询。
 *
 * Args:
 *     bridge (RuntimeChatClientOptions["bridge"]): 主进程控制器桥接端口。
 *     history (Promise<void>): 历史读取完成屏障。
 *
 * Returns:
 *     object: 客户端、按钮和记录过的显示结果。
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
       *     text (string): 错误说明。
       */
      showErrorMessage: (text) => { errors.push(text ?? ""); },
      /**
       * 等待当前页面历史读取屏障并返回隔离历史。
       *
       * Returns:
       *     Promise<ChatHistoryItem[]>: 当前页面已应用的隔离历史列表。
       */
      loadHistory: async () => { await history; return []; },
    },
    confirmDialog: {
      /** 展示审批并保持未决定，用后续事件验证关闭。 */
      open: async () => {
        approvals += 1;
        return new Promise<ApprovalDecision>(/** 本测试不产生人工决定。 */
        () => {});
      },
      /** 记录后端已处理审批时关闭模态框。 */
      dismiss: () => { dismissed += 1; },
    },
  });
  return { client, button, replies, errors,
    /** 返回已打开的审批数。 */
    approvals: () => approvals,
    /** 返回后端事件关闭的审批数。 */
    dismissed: () => dismissed,
  };
};

test("同会话页面重载取得新快照，历史完成前不能提交，之后等待完整回答并恢复审批", /**
 * 保留主进程控制器，只替换页面监听，覆盖删除非当前模型导致重载的连接状态。
 *
 * Returns:
 *     Promise<void>: 页面重载、事件显示和连接清理均完成。
 */
async () => {
  const oldDocument = globalThis.document;
  globalThis.document = { /** 本测试不创建任务和排队容器。 */ getElementById: () => null } as unknown as Document;
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
 * 使用延迟 fetch 验证本地代次保护，不只检查 AbortSignal。
 *
 * Returns:
 *     Promise<void>: 新旧请求和订阅清理结束。
 */
async () => {
  const harness = new Harness(true);
  try {
    const old = harness.controller.subscribe("s");
    const current = harness.controller.subscribe("s", { refreshSnapshot: true });
    let completed = false;
    void current.then(/** 记录当前首次快照等待是否已完成。 */
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
 * 确认历史查询失败不让初始化永久等待。
 *
 * Returns:
 *     Promise<void>: 查询失败已显示且新页面仍可正常发送。
 */
async () => {
  const oldDocument = globalThis.document;
  globalThis.document = { /** 不提供额外页面容器。 */ getElementById: () => null } as unknown as Document;
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
