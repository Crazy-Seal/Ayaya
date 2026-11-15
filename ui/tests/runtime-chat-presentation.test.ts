import { PresentationElement } from "./presentation-dom.js";
import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as flushEvents } from "node:timers/promises";
import { RuntimeChatClient, type RuntimeChatClientOptions } from "../src/chat/runtime-chat-client.js";
import { ChatHistoryManager } from "../src/chat/chat-history-manager.js";
import { BubbleManager } from "../src/chat/bubble.js";
import { BackgroundTasksPage } from "../src/settings/pages/background-tasks-page.js";
import { PluginStatusNotices } from "../src/chat/plugin-status-notices.js";
import type { BackgroundTaskRecord, ChatAttentionUpdate, ChatHistoryItem, InteractionEvent } from "../shared-types.js";


test("用户消息先于输入提示，工具提示在同轮用户消息之后", /**
 * 验证用户消息先于输入提示，工具提示在同轮用户消息之后。
 */
async () => {
  const view = createPresentation();
  try {
    // 1. 按后端实际顺序接收启动和已提交输入，输入提示必须移动到消息后面。
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("human_message", { message: "请执行命令", message_id: "h1" }, 2);
    assert.deepEqual(view.root.children.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 item.textContent 供当前步骤继续处理。
     *
     * 此定义用于：验证用户消息先于输入提示，工具提示在同轮用户消息之后。
     *
     * Args:
     *     item (PresentationElement): 当前遍历的配置或目录条目。
     */
    (item) => item.textContent), ["请执行命令", "对方正在输入"]);
    // 2. 工具提示替换输入提示，不能跑到本轮用户消息前面。
    await view.emit("tool_call", { value: "run_ps" }, 3);
    assert.equal(view.root.textContent, "请执行命令正在调用工具: run_ps");
  } finally {
    view.close();
  }
});

test("未知线协议事件仍被忽略，后续已知事件继续正常展示", /**
 * 验证未知线协议事件仍被忽略，后续已知事件继续正常展示。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    // 1. 测试替代原始 JSON 接收处；生产调用方只允许已知枚举，未来网络值仍可到达。
    await view.emit("future_event" as InteractionEvent["kind"], { value: "不应展示" }, 2);
    await view.emit("text_chunk", { value: "正常正文。" }, 3);
    assert.equal(view.bubble.textContent, "正常正文。");
    assert.equal(view.root.textContent.includes("不应展示"), false);
  } finally {
    view.close();
  }
});

/**
 * 装配真实聊天展示类，只替换主进程 bridge 和 DOM 环境。
 *
 * Args:
 *     withTaskEntry (boolean): 是否创建实际任务入口所需的标题栏控件。
 *
 * Returns:
 *     { root: PresentationElement; bubble: PresentationElement; motions: string[]; persisted: ChatHistoryItem[]; taskHeader: PresentationElement; ... 8 more ...; close(): void; }: 事件入口、可见容器、动作记录、历史替身及清理函数。
 */
function createPresentation(withTaskEntry = false) {
  const previousDocument = globalThis.document;
  const previousWindow = globalThis.window;
  const previousFrame = globalThis.requestAnimationFrame;
  const root = new PresentationElement();
  const statusRoot = new PresentationElement();
  const bubble = new PresentationElement();
  const motions: string[] = [];
  const persisted: ChatHistoryItem[] = [];
  const historyReads = {
    calls: 0, barrier: null as Promise<void> | null, failure: null as string | null,
    throughIds: [] as Array<string | undefined>, boundaries: new Map<string, number>(),
  };
  const approvals = { opened: 0, dismissed: 0, barrier: null as Promise<{ kind: "approve" }> | null };
  const attention: ChatAttentionUpdate[] = [];
  const taskHeader = new PresentationElement();
  const taskButton = new PresentationElement();
  const taskNotice = new PresentationElement();
  const tasks: import("../shared-types.js").BackgroundTaskRecord[] = [];
  const openedPages: string[] = [];
  /**
   * 在 RuntimeChatClient 登记监听前提供占位事件处理函数。
   *
   * Args:
   *     _event (InteractionEvent): 待投影的后端事件。
   */
  let listener = (_event: InteractionEvent): void => {};
  /**
   * 保存页面尚未订阅断线时的测试入口。
   *
   * Args:
   *     _sessionId (string): 断线所属会话。
   */
  let disconnectListener = (_sessionId: string): void => {};
  globalThis.document = {
    /**
     *  返回不存在的可选任务和排队 DOM。
     *
     * Args:
     *     id (string): 当前模型参数或日志记录的编号。
     */
    getElementById: (id: string) => withTaskEntry ? ({
      "chat-history-toolbar": taskHeader,
      "background-task-entry": taskButton,
      "background-task-notice": taskNotice,
    } as Record<string, PresentationElement>)[id] ?? null : null,
    /**
     * 创建用于真实展示器的隔离节点。
     *
     * Returns:
     *     PresentationElement: 新的空节点。
     */
    createElement: () => new PresentationElement(),
    /**
     * 创建保存正文的文本节点。
     *
     * Args:
     *     text (string): 需要展示的正文。
     *
     * Returns:
     *     PresentationElement & { textContent: string; }: 含该正文的节点。
     */
    createTextNode: (text: string) => Object.assign(new PresentationElement(), { textContent: text }),
  } as unknown as Document;
  globalThis.requestAnimationFrame = /**
   * 由浏览器在requestAnimationFrame时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：装配真实聊天展示类，只替换主进程 bridge 和 DOM 环境。
   *
   * Args:
   *     callback (FrameRequestCallback): 触发后交给调用方继续处理的回调。
   */
  (callback) => { callback(0); return 0; };
  globalThis.window = { desktopPetApi: {
    /**
     * 记录任务入口选择的设置页面。
     *
     * Args:
     *     page (string): 目标设置页。
     */
    openSettingsWindow: (page: string) => { openedPages.push(page); },
    /**
     * 返回当前测试预设的权威历史，不访问真实后端。
     *
     * Args:
     *     _sessionId (string): 当前测试会话。
     *     _n (number): 请求的历史条数。
     *     throughMessageId (string | undefined): 工具结果查询终点。
     * Returns:
     *     Promise<ChatHistoryItem[]>: 场景已经提交的历史列表。
     */
    getChatHistoryLastN: async (_sessionId: string, _n: number, throughMessageId?: string) => {
      historyReads.calls += 1;
      historyReads.throughIds.push(throughMessageId);
      const limit = throughMessageId === undefined ? persisted.length : historyReads.boundaries.get(throughMessageId);
      assert.notEqual(limit, undefined, "工具刷新必须指定已知的历史查询终点");
      const saved = persisted.slice(0, limit);
      await historyReads.barrier;
      if (historyReads.failure) throw new Error(historyReads.failure);
      return saved;
    },
  },
  /**
   * 记录解析出的表情动作。
   *
   * Args:
   *     label (string): 模型回复中的表情标签。
   */
  playMotionByLabel: (label: string) => { motions.push(label); },
  } as unknown as Window & typeof globalThis;
  const submissions = {
    commands: [] as Parameters<NonNullable<RuntimeChatClientOptions["bridge"]>["submitInteractionCommand"]>[0][],
    accepted: true, status: "accepted" as const,
    barrier: null as Promise<void> | null,
  };
  const input = {
    value: "", style: {},
    /** 保留用户当前输入，测试重发不能消耗正在编辑的草稿。 */
    focus(): void {},
  };
  const bridge: NonNullable<RuntimeChatClientOptions["bridge"]> = {
    /**
     * 保存 Electron 提供的断线通知端口，供场景控制连接结束。
     *
     * Args:
     *     callback ((sessionId: string) => void): 聊天客户端的监听器。
     *
     * Returns:
     *     () => void: 当前场景的监听移除函数。
     */
    onInteractionDisconnected: (callback) => {
      disconnectListener = callback;
      /** 测试结束时由全局环境恢复释放本次监听。 */
      return () => {};
    },
    /**
     *  模拟事件订阅已经就绪。
     */
    subscribeDesktopSession: async () => {},
    /**
     * 保存公共事件入口，返回监听清理占位函数。
     *
     * Args:
     *     callback ((event: InteractionEvent) => void): Renderer 监听器。
     *
     * Returns:
     *     () => void: 测试监听清理函数。
     */
    onInteractionEvent: (callback) => {
      listener = callback;
      /**
       * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
       *
       * 此定义用于：保存公共事件入口，返回监听清理占位函数。
       */
      return () => {};
    },
    /**
     * 记录公开命令，允许测试控制回执到达时机。
     *
     * Args:
     *     command (ChannelCommand): 真人输入或重发命令。
     * Returns:
     *     Promise<CommandReceipt>: 测试指定的受理结果。
     */
    submitInteractionCommand: async (command) => {
      submissions.commands.push(command);
      if (submissions.barrier) await submissions.barrier;
      return { accepted: submissions.accepted, status: submissions.accepted ? "accepted" as const : "failed" as const, detail: submissions.accepted ? undefined : "暂未受理" };
    },
    /**
     *  返回空任务列表。
     */
    getBackgroundTasks: async () => tasks,
    /**
     *  返回空任务输出页。
     */
    getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
  };
  const history = new ChatHistoryManager(root as unknown as HTMLDivElement);
  const client = new RuntimeChatClient({
    sessionId: "s", bridge, chatHistory: history,
    statusNotices: new PluginStatusNotices(statusRoot as unknown as HTMLElement),
    /**
     * 记录真实聊天客户端交给窗口的未读与审批提醒，不启动系统窗口。
     * Args:
     *     update (ChatAttentionUpdate): 本次提醒变化。
     */
    onAttention: (update) => { attention.push(update); },
    bubble: new BubbleManager(bubble as unknown as HTMLDivElement),
    sendBtn: { innerHTML: "send",
      /**
       *  提供发送按钮的属性设置占位方法。
       */
      setAttribute() {},
    } as unknown as HTMLButtonElement,
    input: input as unknown as HTMLTextAreaElement,
    confirmDialog: {
      /**
       *  当前测试不显示审批，返回预设决定。
       */
      open: async () => { approvals.opened += 1; return approvals.barrier ? await approvals.barrier : { kind: "approve" }; },
      /**
       *  提供跨端审批关闭占位方法。
       */
      dismiss() { approvals.dismissed += 1; },
    },
  });
  return {
    root, bubble, motions, persisted, taskHeader, taskButton, taskNotice, tasks, openedPages,
    statusRoot,
    client, history, historyReads, approvals, attention, input, submissions,
    /**
     * 从公共 bridge 注入事件并等待 Renderer 处理完成。
     *
     * Args:
     *     kind (InteractionEvent["kind"]): 已知后端事件类别，原始未知事件仅在兼容测试处转换。
     *     payload (Record<string, unknown>): 类别对应的载荷。
     *     sequence (number): 当前 epoch 的事件水位。
     *     runId (string): 当前运行标识。
     *
     * Returns:
     *     Promise<void>: 当前事件已得到处理。
     */
    async emit(kind: InteractionEvent["kind"], payload: Record<string, unknown>, sequence: number, runId = "r") {
      listener({ session_id: "s", kind, payload, origin_channel: "desktop",
        run_id: runId, stream_epoch: "epoch", event_sequence: sequence });
      await flushEvents();
    },
    /**
     * 向页面通知当前或过期会话的连接结束，等待既有事件链完成。
     *
     * Args:
     *     sessionId (string): 断线来源会话，默认使用当前测试会话。
     *
     * Returns:
     *     Promise<void>: 断线清理已经有机会执行。
     */
    async disconnect(sessionId = "s"): Promise<void> {
      disconnectListener(sessionId);
      await flushEvents();
    },
    /**
     * 从事件入口交付指定会话、epoch 和序号的原始网络事件。
     *
     * Args:
     *     event (InteractionEvent): 场景明确指定来源的事件。
     *
     * Returns:
     *     Promise<void>: 当前事件已经有机会完成处理。
     */
    async receive(event: InteractionEvent): Promise<void> {
      listener(event);
      await flushEvents();
    },
    /**
     *  停止展示计时器并恢复测试前的全局环境。
     */
    close(): void {
      history.abortStreaming();
      globalThis.document = previousDocument;
      globalThis.window = previousWindow;
      globalThis.requestAnimationFrame = previousFrame;
    },
  };
}

test("持续提示按插件和编号更新关闭，历史刷新不会删除提示", /**
 * 通过真实聊天客户端观察持续提示区域，验证其生命周期独立于历史列表。
 *
 * Returns:
 *     Promise<void>: 提示和历史事件已经处理。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: true, text: "连接中" }, 1);
    assert.equal(view.statusRoot.hidden, false);
    assert.equal(view.statusRoot.textContent, "[插件 demo] 连接中");
    const original = view.statusRoot.children[0];
    await view.emit("status_notice", { plugin_name: "other", id: "control", active: true, text: "另一个任务" }, 2);
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: true, text: "执行中" }, 3);
    assert.equal(view.statusRoot.children.length, 2);
    assert.equal(view.statusRoot.children[0], original);
    assert.equal(view.statusRoot.textContent, "[插件 demo] 执行中[插件 other] 另一个任务");
    await view.emit("history_restored", { detail: "恢复已保存历史" }, 4);
    assert.equal(view.statusRoot.children.length, 2);
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: false }, 5);
    assert.equal(view.statusRoot.textContent, "[插件 other] 另一个任务");
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: false }, 6);
    await view.emit("status_notice", { plugin_name: "other", id: "control", active: false }, 7);
    assert.equal(view.statusRoot.hidden, true);
    assert.equal(view.statusRoot.textContent, "");
    assert.equal(view.persisted.length, 0);
  } finally {
    view.close();
  }
});

test("断线清空持续提示，迟到的其他会话通知不清除新提示", /**
 * 从页面公开 bridge 触发断线，验证提示清理和重新连接后的增量。
 *
 * Returns:
 *     Promise<void>: 所有事件与断线通知已经处理。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: true, text: "执行中" }, 1);
    await view.disconnect();
    assert.equal(view.statusRoot.textContent, "");
    assert.equal(view.statusRoot.hidden, true);
    await view.emit("session_snapshot", { phase: "idle" }, 1);
    assert.equal(view.statusRoot.textContent, "");
    await view.emit("status_notice", { plugin_name: "demo", id: "control", active: true, text: "再次执行" }, 2);
    await view.disconnect("other");
    assert.equal(view.statusRoot.textContent, "[插件 demo] 再次执行");
  } finally {
    view.close();
  }
});

test("持续提示遵循会话和事件水位过滤，重新订阅后从空区域开始", /**
 * 通过公开事件入口验证旧 epoch、重复序号及跨会话事件不会改变提示。
 *
 * Returns:
 *     Promise<void>: 来源过滤和会话切换已经验证。
 */
async () => {
  const view = createPresentation();
  const active = { plugin_name: "demo", id: "control", active: true, text: "当前提示" };
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("status_notice", active, 2);
    await view.emit("status_notice", { ...active, text: "重复事件" }, 2);
    await view.receive({ session_id: "s", kind: "status_notice", payload: { ...active, active: false },
      stream_epoch: "old-epoch", event_sequence: 100 });
    await view.receive({ session_id: "other", kind: "status_notice", payload: { ...active, active: false },
      stream_epoch: "epoch", event_sequence: 100 });
    assert.equal(view.statusRoot.textContent, "[插件 demo] 当前提示");
    await view.emit("session_snapshot", { phase: "idle" }, 2);
    assert.equal(view.statusRoot.hidden, true);
    await view.emit("status_notice", active, 3);
    view.client.setSessionId("other");
    assert.equal(view.statusRoot.hidden, true);
    await view.emit("status_notice", active, 4);
    assert.equal(view.statusRoot.hidden, true);
    await view.receive({ session_id: "other", kind: "session_snapshot", payload: { phase: "idle" },
      stream_epoch: "new-epoch", event_sequence: 0 });
    await view.receive({ session_id: "other", kind: "status_notice", payload: { ...active, text: "另一会话" },
      stream_epoch: "new-epoch", event_sequence: 1 });
    await view.disconnect("s");
    assert.equal(view.statusRoot.textContent, "[插件 demo] 另一会话");
  } finally {
    view.close();
  }
});

test("重连历史中的新回复补发提醒，首次旧历史和重复快照不误报", /**
 * 验证没有收到流式分片时仍提醒新增的持久化回复，旧历史和重复恢复不会重复提醒。
 * Returns:
 *     Promise<void>: 所有快照已通过真实客户端与历史展示器处理。
 */
async () => {
  const view = createPresentation();
  try {
    view.persisted.push({ id: "old-reply", role: "ai", content: "启动前的回答", timestamp: "2026-10-11T00:00:00Z" });
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    assert.deepEqual(view.attention, [{ approvalId: null }]);
    view.attention.length = 0;

    // 1. 模拟连接中断期间后端保存回复，重连仅交付快照与完整历史。
    view.persisted.push({ id: "offline-reply", role: "ai", content: "断线期间完成的回答", timestamp: "2026-10-11T00:01:00Z" });
    await view.emit("session_snapshot", { phase: "idle" }, 1);
    assert.deepEqual(view.attention, [{ newMessage: true }, { approvalId: null }]);
    assert.ok(view.root.textContent.includes("断线期间完成的回答"));

    // 2. 相同历史再次恢复只同步审批状态，不把旧回复当作新消息。
    view.attention.length = 0;
    await view.emit("session_snapshot", { phase: "idle" }, 2);
    assert.deepEqual(view.attention, [{ approvalId: null }]);
  } finally { view.close(); }
});

test("终态历史新回复提醒且查询失败不提前消费回复编号", /**
 * 验证没有正文分片的完成事件补发提醒，失败的历史查询不丢失下次重连提醒。
 * Returns:
 *     Promise<void>: 完成与恢复场景已处理。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    view.attention.length = 0;
    await view.emit("run_started", {}, 1);
    view.persisted.push({ id: "completed-reply", role: "ai", content: "完整回答", timestamp: "2026-10-11T00:01:00Z" });
    await view.emit("run_completed", {}, 2);
    assert.deepEqual(view.attention, [{ approvalId: null }, { newMessage: true }]);

    view.attention.length = 0;
    view.persisted.push({ id: "later-reply", role: "assistant", content: "后续回答", timestamp: "2026-10-11T00:02:00Z" });
    view.historyReads.failure = "模拟历史读取失败";
    await view.emit("session_snapshot", { phase: "idle" }, 3);
    assert.deepEqual(view.attention, [{ approvalId: null }]);
    view.attention.length = 0;
    view.historyReads.failure = null;
    await view.emit("session_snapshot", { phase: "idle" }, 4);
    assert.deepEqual(view.attention, [{ newMessage: true }, { approvalId: null }]);
  } finally { view.close(); }
});

test("任务入口只计运行任务，点击打开详情，全部结束后短提示消失", /**
 * 验证任务入口只计运行任务，点击打开详情，全部结束后短提示消失。
 *
 * Args:
 *     t (TestContext): Node 测试上下文，供登记用例结束后恢复全局依赖的清理函数。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const view = createPresentation(true);
  try {
    // 1. 历史终态任务没有运行徽标；运行快照只显示正在执行的数量。
    const ended = { task_id: "old", command: "echo old", status: "completed" as const,
      created_at: "2026-10-09T00:00:00Z", ended_at: "2026-10-09T00:00:01Z",
    } as import("../shared-types.js").BackgroundTaskRecord;
    const running = { ...ended, task_id: "active", status: "running" as const, ended_at: null };
    view.tasks.push(ended);
    await view.emit("session_snapshot", { phase: "idle", tasks: view.tasks }, 0);
    assert.equal(view.taskHeader.hidden, true);
    view.tasks.push(running);
    await view.emit("session_snapshot", { phase: "idle", tasks: view.tasks }, 1);
    assert.equal(view.taskHeader.hidden, false);
    assert.equal(view.taskButton.textContent, "任务 · 1");
    // 2. 快捷入口直达已有任务页面，结束事件清除数量并显示有限时长的通知。
    view.taskButton.handlers.get("click")?.();
    assert.deepEqual(view.openedPages, ["tasks"]);
    view.tasks.splice(1, 1, { ...running, status: "completed" });
    await view.emit("task_status", { task_id: "active", status: "completed" }, 2);
    assert.equal(view.taskButton.hidden, true);
    assert.match(view.taskNotice.textContent, /已完成/);
    assert.equal(view.taskHeader.hidden, false);
    t.mock.timers.tick(4000);
    assert.equal(view.taskHeader.hidden, true);
  } finally {
    view.close();
  }
});

test("任务页可见时刷新运行状态和最近输出，终态或切页后停止轮询", /**
 * 验证任务页可见时刷新运行状态和最近输出，终态或切页后停止轮询。
 *
 * Args:
 *     t (TestContext): Node 测试上下文，供登记用例结束后恢复全局依赖的清理函数。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const view = createPresentation();
  const list = new PresentationElement();
  const page = new BackgroundTasksPage(list as unknown as HTMLElement,
    new PresentationElement() as unknown as HTMLButtonElement);
  let reads = 0;
  let status: "running" | "completed" = "running";
  window.desktopPetApi.getBackgroundTasks = /**
   * 由浏览器在getBackgroundTasks时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证任务页可见时刷新运行状态和最近输出，终态或切页后停止轮询。
   */
  async () => {
    reads += 1;
    return [{ task_id: "t", session_id: "s", tool_name: "exec_command", command: "echo test", status, background: true,
      pid: 1, exit_code: null, created_at: "2026-10-09T00:00:00Z", ended_at: null,
      recent_output: "最新输出", name: "terminal", title: "echo test", result: null, error: null,
      plugin_name: null, output_ref: "task-output://t", notify_agent: true,
    }];
  };
  try {
    // 1. 运行中可见页会刷新最近输出，完成后不再产生周期查询。
    page.setActive(true);
    await page.render("s");
    assert.match(list.textContent, /运行中/);
    assert.match(list.textContent, /最新输出/);
    assert.match(list.textContent, /\[exec_command\]/);
    status = "completed";
    t.mock.timers.tick(2000);
    await flushEvents();
    assert.match(list.textContent, /已完成/);
    assert.equal(reads, 2);
    t.mock.timers.tick(10000);
    await flushEvents();
    assert.equal(reads, 2);
    // 2. 再次运行时切到其他页，不能继续读取隐藏页记录。
    status = "running";
    await page.render("s");
    page.setActive(false);
    t.mock.timers.tick(10000);
    await flushEvents();
    assert.equal(reads, 3);
  } finally {
    page.setActive(false);
    view.close();
  }
});

/**
 * 创建后台任务记录，只使用测试内存中的输出和会话信息。
 *
 * Args:
 *     taskId (string): 测试任务编号。
 *     outputRef (string | null): 有终端输出时提供分页引用。
 *
 * Returns:
 *     BackgroundTaskRecord: 已完成的测试任务记录。
 */
function backgroundTask(taskId: string, outputRef: string | null): BackgroundTaskRecord {
  return {
    task_id: taskId, session_id: "s", tool_name: outputRef ? "exec_command" : null,
    command: null, status: "completed", background: true, pid: null, exit_code: 0,
    created_at: "2026-10-26T00:00:00Z", ended_at: "2026-10-26T00:00:12Z",
    recent_output: "最近输出", name: taskId, title: taskId, result: `${taskId}结果`, error: null,
    plugin_name: outputRef ? null : "memory", output_ref: outputRef,
    notify_agent: true,
  };
}

test("保存故障清除未提交正文，恢复已保存进展并保留错误，旧轮迟到事件失效", /**
 * 使用真实聊天展示器核对 FAULTED 的错误、已提交历史和后续明确输入。
 */
async () => {
  const view = createPresentation();
  try {
    view.persisted.push({ id: "saved-tool", role: "ai", content: "本轮已保存的进展", timestamp: "2026-11-11T00:00:00Z" });
    view.historyReads.boundaries.set("saved-tool", 1);
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1, "failed-run");
    await view.emit("text_chunk", { value: "尚未保存的最终回答" }, 2, "failed-run");
    await view.emit("error", { save_failed: true, detail: "执行结果保存失败" }, 3, "failed-run");
    assert.ok(view.root.textContent.includes("执行结果保存失败"));
    assert.equal(view.submissions.commands.length, 0);

    await view.emit("history_restored", { through_message_id: "saved-tool" }, 4, "failed-run");
    assert.ok(view.root.textContent.includes("本轮已保存的进展"));
    assert.ok(!view.root.textContent.includes("尚未保存的最终回答"));
    assert.ok(view.root.textContent.includes("执行结果保存失败"));
    await view.emit("text_chunk", { value: "迟到片段" }, 5, "failed-run");
    await view.emit("run_completed", {}, 6, "failed-run");
    assert.ok(!view.root.textContent.includes("迟到片段"));
    assert.ok(view.root.textContent.includes("执行结果保存失败"));
    assert.equal(view.submissions.commands.length, 0);

    view.input.value = "请继续";
    await view.client.sendMessage();
    assert.equal(view.submissions.commands.length, 1);
    assert.equal(view.submissions.commands[0].kind, "human_turn");
  } finally { view.close(); }
});

test("故障重连从快照显示保存失败，子交付错误保持父回答独立", /**
 * 快照与实时错误都可见，子报告错误不能结束父正在展示的回答。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle", save_failure: { run_id: "failed", detail: "提交失败", restored: true } }, 0);
    assert.ok(view.root.textContent.includes("提交失败"));
    await view.emit("run_started", {}, 1, "parent");
    await view.emit("text_chunk", { value: "父回答" }, 2, "parent");
    await view.emit("error", { subagent_id: "child", report_failed: true, detail: "结果交付失败" }, 3, "child-run");
    await view.emit("text_chunk", { value: "继续" }, 4, "parent");
    assert.ok(view.root.textContent.includes("结果交付失败"));
    assert.equal(view.submissions.commands.length, 0);
  } finally { view.close(); }
});

test("任务保存失败从实时事件和重连摘要显示，停止入口关闭并保留输出", /**
 * SAVE_FAILED 使用当前进程摘要，任务页仍提供已经保存的输出读取能力。
 */
async () => {
  const view = createPresentation(true);
  const list = new PresentationElement();
  const page = new BackgroundTasksPage(list as unknown as HTMLElement,
    new PresentationElement() as unknown as HTMLButtonElement);
  const task = { ...backgroundTask("failed-task", "output"), status: "save_failed" as const, error: "磁盘写入失败" };
  view.tasks.push(task);
  window.desktopPetApi.getBackgroundTasks = /** 返回当前进程的保存失败摘要。 */ async () => [task];
  try {
    await view.emit("session_snapshot", { phase: "idle", tasks: [task] }, 0);
    assert.ok(!view.taskButton.textContent.includes("运行中"));
    await view.emit("task_status", { task_id: task.task_id, status: "save_failed" }, 1);
    assert.ok(view.taskNotice.textContent.includes("结果保存失败"));
    await page.render("s");
    assert.ok(list.textContent.includes("结果保存失败"));
    assert.ok(!list.textContent.includes("停止任务"));
  } finally { view.close(); }
});

test("每个后台任务有独立卡片，详情和分页在刷新后仍分别保留", /**
 * 验证同时展开多个任务时结果与分页输出分别留在对应卡片中。
 */
async () => {
  const view = createPresentation();
  const list = new PresentationElement();
  const page = new BackgroundTasksPage(list as unknown as HTMLElement,
    new PresentationElement() as unknown as HTMLButtonElement);
  const records = [backgroundTask("memory", null), backgroundTask("shell-a", "a"), backgroundTask("shell-b", "b")];
  const reads: [string, number][] = [];
  window.desktopPetApi.getBackgroundTasks = /** 返回当前测试的三条独立任务。 */
    async () => records;
  window.desktopPetApi.getBackgroundTaskOutput = /**
   * 按任务和游标返回不同内容，检查各卡片是否独立读取下一页。
   *
   * Args:
   *     _sessionId (string): 当前测试会话。
   *     taskId (string): 请求输出的任务编号。
   *     offset (number): 请求输出的字节游标。
   */
    async (_sessionId, taskId, offset) => {
      reads.push([taskId, offset]);
      return { text: offset < 20 ? `${taskId}:${offset}\n` : "",
        next_offset: Math.min(offset + 10, 20), has_more: offset === 0 };
    };
  try {
    // 1. 每条记录各自创建卡片，详情初始收起。
    await page.render("s");
    assert.equal(list.children.length, 3);
    for (const card of list.children) {
      assert.match(card.className, /\bui-surface\b/);
      assert.equal(card.children[2].hidden, true);
      card.children[0].children[1].handlers.get("click")?.();
    }
    await flushEvents();
    const memoryDetails = list.children[0].children[2];
    const firstDetails = list.children[1].children[2];
    const secondDetails = list.children[2].children[2];
    assert.equal(memoryDetails.children[0].textContent, "memory结果");
    assert.equal(firstDetails.children[0].textContent, "shell-a:0\n");
    assert.equal(secondDetails.children[0].textContent, "shell-b:0\n");

    // 2. 刷新保留展开和分页位置；重复点击加载只发送一次请求。
    await page.render("s");
    assert.equal(list.children[1].children[2], firstDetails);
    assert.equal(firstDetails.hidden, false);
    firstDetails.children[1].handlers.get("click")?.();
    firstDetails.children[1].handlers.get("click")?.();
    await flushEvents();
    assert.equal(firstDetails.children[0].textContent, "shell-a:0\nshell-a:10\n");
    assert.equal(secondDetails.children[0].textContent, "shell-b:0\n");
    assert.equal(firstDetails.children[1].hidden, true);
    assert.equal(secondDetails.children[1].hidden, false);
    assert.deepEqual(reads, [["shell-a", 0], ["shell-b", 0], ["shell-a", 10]]);

    // 3. 收起一张卡片不影响另一张，重新展开保留已加载的正文。
    list.children[1].children[0].children[1].handlers.get("click")?.();
    assert.equal(firstDetails.hidden, true);
    assert.equal(secondDetails.hidden, false);
    list.children[1].children[0].children[1].handlers.get("click")?.();
    await flushEvents();
    assert.equal(firstDetails.hidden, false);
    assert.equal(firstDetails.children[0].textContent, "shell-a:0\nshell-a:10\n");
    assert.equal(secondDetails.children[0].textContent, "shell-b:0\n");
    assert.deepEqual(reads, [["shell-a", 0], ["shell-b", 0], ["shell-a", 10], ["shell-a", 20]]);
  } finally {
    page.setActive(false);
    view.close();
  }
});

for (const expanded of [false, true]) {
  test(`终端日志准备完成后${expanded ? "已展开详情自动" : "首次展开详情"}读取真实日志`, /**
   * 任务先登记、后提供日志引用时，占位提示不能代替真实日志的加载状态。
   */
  async () => {
    const view = createPresentation();
    const list = new PresentationElement();
    const page = new BackgroundTasksPage(list as unknown as HTMLElement,
      new PresentationElement() as unknown as HTMLButtonElement);
    const record: BackgroundTaskRecord = {
      ...backgroundTask("shell", null), status: "running", tool_name: "exec_command",
      result: null, error: null, plugin_name: null,
    };
    const offsets: number[] = [];
    window.desktopPetApi.getBackgroundTasks = /** 返回终端启动前后同一条任务记录。 */
      async () => [{ ...record }];
    window.desktopPetApi.getBackgroundTaskOutput = /**
     * 返回准备完成后的真实日志，并记录首次读取位置。
     *
     * Args:
     *     _sessionId (string): 当前测试会话。
     *     _taskId (string): 当前终端任务编号。
     *     offset (number): 读取日志的起始字节位置。
     */
      async (_sessionId, _taskId, offset) => {
        offsets.push(offset);
        return { text: "start\n", next_offset: 6, has_more: false };
      };
    try {
      // 1. 任务已登记但终端尚未准备完成，此时可以先展开结果占位提示。
      await page.render("s");
      const details = list.children[0].children[2];
      if (expanded) list.children[0].children[0].children[1].handlers.get("click")?.();
      assert.deepEqual(offsets, []);

      // 2. 日志引用出现后，已经展开或此时首次展开的详情都从零读取日志。
      record.output_ref = "output";
      await page.render("s");
      if (!expanded) list.children[0].children[0].children[1].handlers.get("click")?.();
      await flushEvents();
      assert.equal(list.children[0].children[2], details);
      assert.equal(details.children[0].textContent, "start\n");
      assert.deepEqual(offsets, [0]);
    } finally {
      page.setActive(false);
      view.close();
    }
  });
}

for (const initialRead of [true, false]) {
  test(`任务结束时${initialRead ? "首次" : "续读"}日志请求尚未返回，响应后补读最终日志`, /**
   * 终态刷新发生于日志请求期间时，保留补读需求并在原请求结束后继续读取。
   */
  async () => {
    const view = createPresentation();
    const list = new PresentationElement();
    const page = new BackgroundTasksPage(list as unknown as HTMLElement,
      new PresentationElement() as unknown as HTMLButtonElement);
    const record: BackgroundTaskRecord = {
      ...backgroundTask("shell", "output"), status: "running", ended_at: null,
    };
    let output = "start\n";
    const offsets: number[] = [];
    let release: (() => void) | undefined;
    window.desktopPetApi.getBackgroundTasks = /** 返回终态刷新时最新的任务状态。 */
      async () => [{ ...record }];
    window.desktopPetApi.getBackgroundTaskOutput = /**
     * 先取得文件当前内容，再延迟指定响应，模拟任务在响应返回之前结束。
     *
     * Args:
     *     _sessionId (string): 当前测试会话。
     *     _taskId (string): 当前终端任务编号。
     *     offset (number): 请求开始时已读取的日志字节数。
     */
      async (_sessionId, _taskId, offset) => {
        offsets.push(offset);
        const snapshot = { text: output.slice(offset), next_offset: output.length, has_more: false };
        if (offsets.length === (initialRead ? 1 : 2)) {
          await new Promise<void>(/** 保留旧响应，等待测试安排终态刷新。 */
            (resolve) => { release = resolve; });
        }
        return snapshot;
      };
    try {
      // 1. 详情开始读取；续读场景先显示初始日志，再发起一个等待返回的请求。
      page.setActive(true);
      await page.render("s");
      list.children[0].children[0].children[1].handlers.get("click")?.();
      await flushEvents();
      if (!initialRead) await page.render("s");

      // 2. 任务在旧响应返回前写入错误并结束，连续刷新不能并发读取相同游标。
      record.status = "failed";
      record.ended_at = "2026-10-26T00:00:20Z";
      output += "error\n";
      await page.render("s");
      await page.render("s");
      assert.equal(offsets.length, initialRead ? 1 : 2);

      // 3. 旧响应返回后补读一次，保留旧日志并补上最后错误，随后停止请求。
      release?.();
      await flushEvents();
      const details = list.children[0].children[2];
      assert.equal(details.children[0].textContent, "start\nerror\n");
      assert.deepEqual(offsets, initialRead ? [0, 6] : [0, 6, 6]);
      assert.equal(details.children[1].hidden, true);
    } finally {
      release?.();
      page.setActive(false);
      view.close();
    }
  });
}

for (const outcome of ["还有分页", "读取失败"] as const) {
  test(`日志请求期间的刷新遇到${outcome}时保留手动加载入口`, /**
   * 合并刷新后仍由用户继续翻页或重试，避免自动翻页和连续失败重试。
   */
  async () => {
    const view = createPresentation();
    const list = new PresentationElement();
    const page = new BackgroundTasksPage(list as unknown as HTMLElement,
      new PresentationElement() as unknown as HTMLButtonElement);
    let release: (() => void) | undefined;
    const offsets: number[] = [];
    window.desktopPetApi.getBackgroundTasks = /** 返回当前终端任务。 */
      async () => [backgroundTask("shell", "output")];
    window.desktopPetApi.getBackgroundTaskOutput = /**
     * 首次请求等待刷新发生后再返回分页或错误，后续手动请求返回最后一页。
     *
     * Args:
     *     _sessionId (string): 当前测试会话。
     *     _taskId (string): 当前终端任务编号。
     *     offset (number): 当前请求的日志字节位置。
     */
      async (_sessionId, _taskId, offset) => {
        offsets.push(offset);
        if (offsets.length === 1) {
          await new Promise<void>(/** 首次请求保持等待，使页面刷新与之交错。 */
            (resolve) => { release = resolve; });
          if (outcome === "读取失败") throw new Error("temporary failure");
          return { text: "start\n", next_offset: 6, has_more: true };
        }
        return { text: offset === 0 ? "start\nend\n" : "end\n", next_offset: 10, has_more: false };
      };
    try {
      // 1. 首次请求期间连续刷新，合并刷新需求并保持一个在途请求。
      await page.render("s");
      list.children[0].children[0].children[1].handlers.get("click")?.();
      await page.render("s");
      await page.render("s");
      release?.();
      await flushEvents();
      const details = list.children[0].children[2];
      assert.deepEqual(offsets, [0]);
      assert.equal(details.children[1].hidden, false);
      assert.equal(details.children[1].disabled, false);

      // 2. 用户继续翻页或重试时，保留正确游标，最终正文只包含真实日志。
      details.children[1].handlers.get("click")?.();
      await flushEvents();
      assert.equal(details.children[0].textContent, "start\nend\n");
      assert.deepEqual(offsets, [0, outcome === "读取失败" ? 0 : 6]);
      assert.equal(details.children[1].hidden, true);
    } finally {
      release?.();
      page.setActive(false);
      view.close();
    }
  });
}

for (const mode of ["轮询", "手动刷新", "重新展开"] as const) {
  test(`任务读到当前末尾后通过${mode}继续显示新增日志和最终错误`, /**
   * 模拟运行中日志持续追加，验证详情保留旧正文并从已有字节游标继续读取。
   *
   * Args:
   *     t (TestContext): 提供可手动推进的任务轮询定时器。
   */
  async (t) => {
    t.mock.timers.enable({ apis: ["setInterval"] });
    const view = createPresentation();
    const list = new PresentationElement();
    const refresh = new PresentationElement();
    const page = new BackgroundTasksPage(list as unknown as HTMLElement,
      refresh as unknown as HTMLButtonElement);
    const record: BackgroundTaskRecord = {
      ...backgroundTask("shell", "output"), status: "running", ended_at: null,
    };
    let output = "start\n";
    const offsets: number[] = [];
    window.desktopPetApi.getBackgroundTasks = /** 返回同一任务当前的运行或失败状态。 */
      async () => [record];
    window.desktopPetApi.getBackgroundTaskOutput = /**
     * 返回当前日志的剩余正文；读到末尾时仍允许任务随后追加内容。
     *
     * Args:
     *     _sessionId (string): 当前测试会话。
     *     _taskId (string): 当前终端任务编号。
     *     offset (number): 上次已读取的日志字节数，测试正文使用 ASCII。
     */
      async (_sessionId, _taskId, offset) => {
        offsets.push(offset);
        return { text: output.slice(offset), next_offset: output.length, has_more: false };
      };
    /** 根据用户操作或轮询时机，触发详情继续读取新增日志。 */
    async function refreshDetails(): Promise<void> {
      if (mode === "轮询") {
        t.mock.timers.tick(2000);
      } else if (mode === "手动刷新") {
        refresh.handlers.get("click")?.();
      } else {
        list.children[0].children[0].children[1].handlers.get("click")?.();
        list.children[0].children[0].children[1].handlers.get("click")?.();
      }
      await flushEvents();
    }
    try {
      // 1. 首次展开读到当前末尾，详情保留初始正文。
      page.setActive(mode === "轮询");
      await page.render("s");
      list.children[0].children[0].children[1].handlers.get("click")?.();
      await flushEvents();
      const details = list.children[0].children[2];
      assert.equal(details.children[0].textContent, "start\n");
      assert.equal(details.children[1].hidden, true);

      // 2. 文件继续增长时从旧游标读取，原详情和正文继续保留。
      output += "progress\n";
      await refreshDetails();
      assert.equal(list.children[0].children[2], details);
      assert.equal(details.children[0].textContent, "start\nprogress\n");
      assert.deepEqual(offsets, [0, 6]);

      // 3. 任务结束时仍能补读最后的错误，不因终态停止轮询而遗漏尾部。
      record.status = "failed";
      record.ended_at = "2026-10-26T00:00:20Z";
      output += "error\n";
      await refreshDetails();
      assert.equal(details.children[0].textContent, "start\nprogress\nerror\n");
      assert.deepEqual(offsets, [0, 6, 15]);
      assert.equal(details.children[1].hidden, true);
    } finally {
      page.setActive(false);
      view.close();
    }
  });
}

test("任务输出迟到时保留刷新后的卡片，切换会话后丢弃旧输出", /**
 * 验证读取输出期间刷新和切换会话时，响应只进入原会话对应的卡片。
 */
async () => {
  const view = createPresentation();
  const list = new PresentationElement();
  const page = new BackgroundTasksPage(list as unknown as HTMLElement,
    new PresentationElement() as unknown as HTMLButtonElement);
  let release: (() => void) | undefined;
  let barrier = Promise.resolve();
  window.desktopPetApi.getBackgroundTasks = /** 返回相同编号以检查跨会话隔离。 */
    async () => [backgroundTask("shell", "output")];
  window.desktopPetApi.getBackgroundTaskOutput = /**
   * 等待测试放行后按字节游标返回输出，已经读完的页返回空正文。
   *
   * Args:
   *     _sessionId (string): 当前测试会话。
   *     _taskId (string): 当前终端任务编号。
   *     offset (number): 请求读取的字节位置，完整输出占十二字节。
   */
    async (_sessionId, _taskId, offset) => {
      await barrier;
      return { text: offset === 0 ? "完整输出" : "", next_offset: 12, has_more: false };
    };
  try {
    // 1. 请求期间刷新，输出应进入刷新后仍显示的同一卡片详情。
    await page.render("s");
    barrier = new Promise<void>(/** 保存输出请求的放行函数。 */ (resolve) => { release = resolve; });
    list.children[0].children[0].children[1].handlers.get("click")?.();
    const details = list.children[0].children[2];
    await page.render("s");
    release?.();
    await flushEvents();
    assert.equal(list.children[0].children[2], details);
    assert.equal(details.children[0].textContent, "完整输出");

    // 2. 新会话复用任务编号时，也不能接收上个会话尚未结束的输出。
    await page.render("other");
    barrier = new Promise<void>(/** 保存旧会话输出请求的放行函数。 */ (resolve) => { release = resolve; });
    list.children[0].children[0].children[1].handlers.get("click")?.();
    const oldDetails = list.children[0].children[2];
    await page.render("s");
    release?.();
    await flushEvents();
    assert.equal(oldDetails.children[0].textContent, "");
    assert.equal(list.children[0].children[2].hidden, true);
    assert.equal(list.children[0].children[2].children[0].textContent, "");
  } finally {
    release?.();
    page.setActive(false);
    view.close();
  }
});

/**
 * 读取实际聊天列表中的文字气泡，忽略输入提示和其他临时节点。
 *
 * Args:
 *     container (PresentationElement): 当前用例的聊天列表。
 *
 * Returns:
 *     string[]: 按显示顺序排列的每个气泡正文。
 */
function messageBubbleTexts(container: PresentationElement): string[] {
  return container.children.filter(/**
   * 根据消息节点样式选出已显示的聊天气泡。
   *
   * Args:
   *     item (PresentationElement): 聊天列表中的节点。
   *
   * Returns:
   *     boolean: 当前节点为消息时返回 true。
   */
  (item) => item.className.startsWith("message-item ")).map(/**
   * 读取每个消息节点的气泡正文。
   *
   * Args:
   *     item (PresentationElement): 已显示的消息节点。
   *
   * Returns:
   *     string: 当前气泡的完整正文。
   */
  (item) => item.children[0].textContent);
}

for (const [name, content, expected] of [
  ["双换行", "你好！\n\n再见！", ["你好！", "再见！"]],
  ["单换行", "你好！\n再见！", ["你好！\n再见！"]],
  ["只有标点", "你好。再见？好的！……Hello. Really? Yes!", ["你好。再见？好的！……Hello. Really? Yes!"]],
  ["无标点段落", "你好\n\n再见", ["你好", "再见"]],
  ["多余空行", "\n\n你好！\n\n\n\n再见！\n\n", ["你好！", "再见！"]],
  ["Windows单换行", "你好！\r\n再见！", ["你好！\n再见！"]],
  ["Windows双换行", "你好！\r\n\r\n再见！", ["你好！", "再见！"]],
] as const) {
  test(`历史消息按双换行分隔气泡：${name}`, /**
   * 验证真人和 Agent 的历史正文共用双换行规则，保留单换行和原始标点。
   */
  async () => {
    const view = createPresentation();
    try {
      view.persisted.push(
        { id: "human", role: "Human", content, timestamp: "2026-10-26" },
        { id: "ai", role: "AI", content, timestamp: "2026-10-26" },
      );
      await view.emit("session_snapshot", { phase: "idle" }, 0);
      assert.deepEqual(messageBubbleTexts(view.root), [...expected, ...expected]);
    } finally {
      view.close();
    }
  });
}

test("流式双换行跨片段时只分隔一次，末段在结束后与历史显示一致", /**
 * 验证双换行到达后即可输出完整段落，单换行和标点保留在同一气泡。
 *
 * Args:
 *     t (TestContext): 提供可手动推进的消息输出定时器。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    // 1. 标点和单换行继续累积，第二个换行到达后才输出第一段。
    await view.emit("text_chunk", { value: "你好！\n" }, 2);
    t.mock.timers.tick(500);
    assert.deepEqual(messageBubbleTexts(view.root), []);
    await view.emit("text_chunk", { value: "\n" }, 3);
    t.mock.timers.tick(500);
    assert.deepEqual(messageBubbleTexts(view.root), ["你好！"]);
    await view.emit("text_chunk", { value: "再见！\n明天见。" }, 4);
    t.mock.timers.tick(500);
    assert.deepEqual(messageBubbleTexts(view.root), ["你好！"]);

    // 2. 流式结束后显示剩余正文，随后历史重绘采用同样的两个气泡。
    view.history.finalizeStreamingMessage();
    t.mock.timers.tick(500);
    assert.deepEqual(messageBubbleTexts(view.root), ["你好！", "再见！\n明天见。"]);
    view.persisted.push({ id: "ai", role: "AI", content: "你好！\n\n再见！\n明天见。", timestamp: "2026-10-26" });
    await view.emit("run_completed", { value: null }, 5);
    assert.deepEqual(messageBubbleTexts(view.root), ["你好！", "再见！\n明天见。"]);
  } finally {
    view.close();
  }
});

test("流式表情标签跨分片时两处正文均不显示标签，动作每轮只播放一次", /**
 * 验证流式表情标签跨分片时两处正文均不显示标签，动作每轮只播放一次。
 *
 * Args:
 *     t (TestContext): Node 测试上下文，供登记用例结束后恢复全局依赖的清理函数。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const view = createPresentation();
  try {
    // 1. 半个标签不能进入气泡，模型完整标签到达后再播放对应动作。
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("text_chunk", { value: "<惊" }, 2);
    assert.equal(view.bubble.textContent, "");
    assert.deepEqual(view.motions, []);
    await view.emit("text_chunk", { value: "讶>真有趣。\n\n<惊" }, 3);
    t.mock.timers.tick(500);
    assert.equal(view.bubble.textContent, "真有趣。");
    assert.equal(view.root.textContent, "真有趣。");
    assert.deepEqual(view.motions, ["惊讶"]);

    // 2. 同轮重复标签不重复动作，后续句子和最终历史仍与流式正文一致。
    await view.emit("text_chunk", { value: "讶>后一句。\n\n" }, 4);
    t.mock.timers.tick(500);
    assert.equal(view.bubble.textContent, "真有趣。\n\n后一句。");
    assert.equal(view.root.textContent, "真有趣。后一句。");
    assert.deepEqual(view.motions, ["惊讶"]);
    view.persisted.push({ id: "ai-1", role: "AI", content: "<惊讶>真有趣。\n\n<惊讶>后一句。", timestamp: "2026-10-08" });
    await view.emit("run_completed", { value: null }, 5);
    assert.equal(view.root.textContent, "真有趣。后一句。");
    assert.equal(view.bubble.textContent, "真有趣。\n\n后一句。");
  } finally {
    view.close();
  }
});

test("重连等待完整回答且不重播表情，新轮恢复实时正文", /**
 * 验证重连等待完整回答且不重播表情，新轮恢复实时正文。
 *
 * Args:
 *     t (TestContext): Node 测试上下文，供登记用例结束后恢复全局依赖的清理函数。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const view = createPresentation();
  try {
    // 1. 快照只恢复运行状态，不显示后来到达的零散正文和表情。
    await view.emit("session_snapshot", {
      phase: "running", run_id: "r",
    }, 5);
    t.mock.timers.tick(500);
    assert.equal(view.bubble.textContent, "正在回答…");
    assert.equal(view.root.textContent.includes("<"), false);
    assert.deepEqual(view.motions, []);
    await view.emit("text_chunk", { value: "讶>2 < 3。<开心>继续。" }, 6);
    assert.equal(view.bubble.textContent, "正在回答…");
    assert.deepEqual(view.motions, []);

    // 2. 同 run 的工具恢复不重播旧标签，新 run 则允许再次播放。
    await view.emit("run_started", { resumed: true }, 8);
    await view.emit("text_chunk", { value: "<惊讶>恢复。" }, 9);
    assert.deepEqual(view.motions, []);
    view.persisted.push({ id: "ai-r", role: "AI", content: "<惊讶>已输出。2 < 3。<开心>继续。恢复。", timestamp: "2026-10-10" });
    await view.emit("run_completed", { value: null }, 10);
    assert.equal(view.bubble.textContent, "已输出。2 < 3。继续。恢复。");
    await view.emit("run_started", {}, 11, "next-run");
    await view.emit("text_chunk", { value: "<惊讶>新一轮。" }, 12, "next-run");
    assert.equal(view.bubble.textContent, "新一轮。");
    assert.deepEqual(view.motions, ["惊讶"]);
  } finally {
    view.close();
  }
});

test("历史在初始化、审批和终态读取，工具开始及重复审批不重复查询", /**
 * 验证工具开始仅更新提示，审批和终态按既有规则读取历史。
 */
async () => {
  const view = createPresentation();
  try {
    // 1. 普通连接实时显示，正文、工具、排队和任务通知不触发历史查询。
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("text_chunk", { value: "工具说明。" }, 2);
    await view.emit("tool_call", { value: "read_file" }, 3);
    await view.emit("queued_message", { input_sequence: 1, message: "下一条" }, 4);
    await view.emit("queued_cancelled", { input_sequence: 1 }, 5);
    await view.emit("task_status", { task_id: "t", status: "completed" }, 6);
    assert.equal(view.historyReads.calls, 1);
    assert.equal(view.bubble.textContent, "工具说明。");
    // 2. 人工审批说明已经保存，读取后只有一份；同审批新序号也不重复查询。
    view.persisted.push({ id: "tool-ai", role: "AI", content: "工具说明。", timestamp: "2026-10-10" });
    const approval = { value: { type: "authorization_request", request_id: "approval" } };
    await view.emit("interrupt", approval, 7);
    assert.equal(view.historyReads.calls, 2);
    assert.equal(view.approvals.opened, 1);
    assert.equal(view.root.textContent, "工具说明。");
    await view.emit("interrupt", approval, 8);
    await view.emit("authorization_resolved", {}, 9);
    assert.equal(view.historyReads.calls, 2);
    // 3. 审批恢复后工具直接在后端执行；完成统一载入保存的最终结果。
    await view.emit("run_started", { resumed: true }, 10);
    await view.emit("tool_call", { value: "control_screen" }, 11);
    assert.equal(view.historyReads.calls, 2);
    await view.emit("run_started", { resumed: true }, 12);
    await view.emit("text_chunk", { value: "完整结果。" }, 13);
    view.persisted.push({ id: "result-ai", role: "AI", content: "完整结果。", timestamp: "2026-10-10" });
    await view.emit("run_completed", { value: null }, 14);
    assert.equal(view.historyReads.calls, 3);
    assert.equal(view.root.textContent, "工具说明。完整结果。");
    assert.equal(view.bubble.textContent, "完整结果。");
    await view.emit("run_completed", { value: null }, 14);
    assert.equal(view.historyReads.calls, 3);
  } finally {
    view.close();
  }
});

/**
 * 从真实展示器创建的节点中读取图片地址，检查轮末重绘前后是否重复。
 * Args:
 *     element (PresentationElement): 聊天节点或子节点。
 * Returns:
 *     string[]: 当前子树里的图片地址。
 */
function imageSources(element: PresentationElement): string[] {
  return [...(element.src ? [element.src] : []), ...element.children.flatMap(imageSources)];
}

for (const restored of [false, true]) {
  test(`工具完成立即显示图片，刷新期间的后续正文继续实时显示（${restored ? "重连" : "普通"}）`, /**
   * 使用真实客户端和展示器，验证查询限定、事件积压、图片去重和后续流式正文。
   * Args:
   *     t (TestContext): 手动推进段落输出定时器。
   */
  async (t) => {
    t.mock.timers.enable({ apis: ["setInterval"] });
    const view = createPresentation();
    let release: (() => void) | undefined;
    try {
      await view.emit("session_snapshot", { phase: restored ? "running" : "idle", run_id: restored ? "r" : null }, 0);
      await view.emit("run_started", {}, 1);
      await view.emit("text_chunk", { value: "工具前说明" }, 2);
      await view.emit("tool_call", { value: "control_screen" }, 3);
      // 1. 模拟后端已经抢先保存下一步回答；工具完成刷新只能读到工具结果。
      view.persisted.push(
        { id: "before", role: "AI", content: "工具前说明", timestamp: "2026-10-29" },
        { id: "result:images", role: "AI", content: "", images: ["screenshot/one.png"], timestamp: "2026-10-29" },
        { id: "after", role: "AI", content: "工具后\n\n继续回答", timestamp: "2026-10-29" },
      );
      view.historyReads.boundaries.set("result", 2);
      view.historyReads.barrier = new Promise<void>(/** 暂停工具刷新，让后续正文在查询期间到达。 */
        (resolve) => { release = resolve; });
      await view.emit("tool_completed", { value: { tool_call_id: "call", tool_name: "control_screen", message_id: "result" } }, 4);
      await view.emit("text_chunk", { value: "工具后\n\n" }, 5);
      await view.emit("text_chunk", { value: "继续回答" }, 6);
      release?.();
      await flushEvents();
      assert.equal(view.historyReads.calls, 2);
      assert.equal(view.historyReads.throughIds[1], "result");
      assert.equal(imageSources(view.root).length, 1);
      assert.doesNotMatch(view.root.textContent, /\[系统消息\]|返回图片/);
      assert.deepEqual(messageBubbleTexts(view.root), ["工具前说明", ""]);
      // 2. 本轮尚未结束，排队正文已恢复逐段显示，图片位于工具前说明之后。
      t.mock.timers.tick(500);
      assert.deepEqual(messageBubbleTexts(view.root), ["工具前说明", "", "工具后"]);
      assert.equal(view.bubble.textContent, "工具后\n\n继续回答");
      // 3. 轮末按最新历史重绘，图片和正文均只出现一次。
      view.historyReads.barrier = null;
      await view.emit("run_completed", {}, 7);
      assert.equal(imageSources(view.root).length, 1);
      assert.deepEqual(messageBubbleTexts(view.root), ["工具前说明", "", "工具后", "继续回答"]);
    } finally {
      release?.();
      view.close();
    }
  });
}

test("工具完成刷新失败仍继续流式回答", /**
 * 查询失败保留已有说明，下一步模型正文继续显示，不等整轮结束。
 * Args:
 *     t (TestContext): 手动推进段落输出定时器。
 */
async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("text_chunk", { value: "工具前说明" }, 2);
    await view.emit("tool_call", { value: "read_file" }, 3);
    view.historyReads.boundaries.set("result", 0);
    view.historyReads.failure = "历史离线";
    await view.emit("tool_completed", { value: { tool_call_id: "call", tool_name: "read_file", message_id: "result" } }, 4);
    await view.emit("text_chunk", { value: "工具后回答\n\n" }, 5);
    t.mock.timers.tick(500);
    assert.deepEqual(messageBubbleTexts(view.root), ["工具前说明", "工具后回答"]);
    assert.match(view.root.textContent, /历史读取失败/);
  } finally { view.close(); }
});

test("历史查询期间到达的新回答先不显示，结束时完整显示且下一轮继续实时输出", /**
 * 验证历史查询期间到达的新回答先不显示，结束时完整显示且下一轮继续实时输出。
 */
async () => {
  const view = createPresentation();
  /**
   *  由测试放行历史读取，避免依赖执行速度。
   */
  let release = (): void => {};
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("text_chunk", { value: "上一轮。" }, 2);
    view.persisted.push({ id: "old", role: "AI", content: "上一轮。", timestamp: "2026-10-10" });
    view.historyReads.barrier = new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证历史查询期间到达的新回答先不显示，结束时完整显示且下一轮继续实时输出。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => { release = resolve; });
    // 1. 完成事件阻塞在查询，后续新轮和正文通过公共事件入口到达。
    await view.emit("run_completed", { value: null }, 3);
    await view.emit("run_started", {}, 4, "next");
    await view.emit("text_chunk", { value: "下一轮前半段。" }, 5, "next");
    release();
    view.historyReads.barrier = null;
    await flushEvents();
    assert.equal(view.root.textContent.includes("下一轮前半段"), false);
    await view.emit("text_chunk", { value: "后半段。" }, 6, "next");
    assert.equal(view.root.textContent.includes("后半段"), false);
    // 2. 新轮保存结束后完整替换；再下一轮没有查询交错，仍实时显示。
    view.persisted.push({ id: "next", role: "AI", content: "下一轮前半段。后半段。", timestamp: "2026-10-10" });
    await view.emit("run_completed", { value: null }, 7, "next");
    assert.equal(view.root.textContent, "上一轮。下一轮前半段。后半段。");
    assert.equal(view.bubble.textContent, "下一轮前半段。后半段。");
    await view.emit("run_started", {}, 8, "later");
    await view.emit("text_chunk", { value: "实时正文。" }, 9, "later");
    assert.equal(view.bubble.textContent, "实时正文。");
  } finally {
    release();
    view.close();
  }
});

test("审批历史读取失败保留原文且允许审批，下一次同审批成功刷新后不再重复查询", /**
 * 验证审批历史读取失败保留原文且允许审批，下一次同审批成功刷新后不再重复查询。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("run_started", {}, 1);
    await view.emit("text_chunk", { value: "需要审批的完整说明。" }, 2);
    view.historyReads.failure = "模拟离线";
    const approval = { value: { type: "authorization_request", request_id: "approval" } };
    await view.emit("interrupt", approval, 3);
    assert.equal(view.approvals.opened, 1);
    assert.match(view.root.textContent, /需要审批的完整说明/);
    assert.match(view.root.textContent, /历史读取失败/);
    // 1. 下一次触发恢复查询，成功历史替换本地草稿和旧错误提示。
    view.historyReads.failure = null;
    view.persisted.push({ id: "saved", role: "AI", content: "需要审批的完整说明。", timestamp: "2026-10-10" });
    await view.emit("interrupt", approval, 4);
    assert.equal(view.historyReads.calls, 3);
    assert.equal(view.root.textContent, "需要审批的完整说明。");
    await view.emit("interrupt", approval, 5);
    assert.equal(view.historyReads.calls, 3);
  } finally {
    view.close();
  }
});

test("错误刷新只显示已保存历史且错误提示位于重绘之后", /**
 * 验证错误刷新只显示已保存历史且错误提示位于重绘之后。
 */
async () => {
  const view = createPresentation();
  try {
    view.persisted.push({ id: "saved", role: "AI", content: "此前已保存。", timestamp: "2026-10-10" });
    await view.emit("session_snapshot", { phase: "running", run_id: "r" }, 0);
    await view.emit("text_chunk", { value: "未保存尾部。" }, 1);
    await view.emit("error", { detail: "模型调用失败" }, 2);
    assert.equal(view.historyReads.calls, 2);
    assert.equal(view.bubble.textContent, "此前已保存。");
    assert.equal(view.root.textContent, "此前已保存。模型调用失败");
    assert.equal(view.root.textContent.includes("未保存"), false);
  } finally {
    view.close();
  }
});

test("停止命令回执不读取历史，保存后的完成事件才恢复被打断正文", /**
 * 验证停止命令回执不读取历史，保存后的完成事件才恢复被打断正文。
 */
async () => {
  const view = createPresentation();
  try {
    await view.emit("session_snapshot", { phase: "running", run_id: "r" }, 0);
    await view.client.sendMessage();
    assert.equal(view.historyReads.calls, 1);
    view.persisted.push({ id: "stopped", role: "AI", content: "已生成文字……（被用户打断）", timestamp: "2026-10-10" });
    await view.emit("run_completed", { value: { interrupted: true } }, 1);
    assert.equal(view.historyReads.calls, 2);
    assert.equal(view.bubble.textContent, "已生成文字……（被用户打断）");
  } finally {
    view.close();
  }
});

test("历史读取期间切换会话，旧查询不能更新新会话正文或气泡", /**
 * 验证历史读取期间切换会话，旧查询不能更新新会话正文或气泡。
 */
async () => {
  const view = createPresentation();
  /**
   *  由测试完成旧会话查询。
   */
  let release = (): void => {};
  try {
    view.persisted.push({ id: "old", role: "AI", content: "旧会话正文。", timestamp: "2026-10-10" });
    view.historyReads.barrier = new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证历史读取期间切换会话，旧查询不能更新新会话正文或气泡。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => { release = resolve; });
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    view.client.setSessionId("other");
    release();
    view.historyReads.barrier = null;
    await flushEvents();
    assert.equal(view.root.textContent.includes("旧会话正文"), false);
    assert.equal(view.bubble.textContent, "");
  } finally {
    release();
    view.close();
  }
});


test("未交付失败显示原输入和重试按钮，拒绝保留、受理移除且不改草稿", /**
 * 验证临时失败消息的正文、附件、按钮禁用和原内容重发。
 * Returns:
 *     Promise<void>: 所有公开事件和按钮操作已处理。
 */
async () => {
  const view = createPresentation();
  let release: (() => void) | undefined;
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("error", { detail: "交付失败", failed_input: {
      input_sequence: 8, message: "原正文", images: ["data:image/png;base64,YQ=="],
    } }, 1);
    const failed = view.root.children.find(/**
     * 从用户可见容器寻找失败消息，不依赖客户端内部字段。
     * Args:
     *     node (PresentationElement): 实际渲染的消息节点。
     * Returns:
     *     boolean: 是否为失败输入展示。
     */
    (node) => node.className.includes("failed-input"));
    assert.ok(failed);
    assert.match(failed.textContent, /原正文/);
    assert.match(failed.textContent, /交付失败/);
    const button = failed.children.find(/**
     * 寻找失败消息附带的公开重试控件。
     * Args:
     *     node (PresentationElement): 消息中的子节点。
     * Returns:
     *     boolean: 是否为重试按钮。
     */
    (node) => node.className.includes("retry-input-button"));
    assert.ok(button);
    assert.equal(button.attributes.get("aria-label"), "重试");
    assert.equal(button.title, "重试");
    const image = failed.children[0].children.find(/**
     * 从实际图片展示中找到原输入附件。
     * Args:
     *     node (PresentationElement): 气泡中的子元素。
     * Returns:
     *     boolean: 是否为图片集合。
     */
    (node) => node.className === "message-images")?.children[0];
    assert.equal(image?.src, "data:image/png;base64,YQ==");
    // 1. 慢回执期间只允许一次重发，不能改动当前正在输入的新草稿。
    view.input.value = "新草稿";
    view.submissions.accepted = false;
    view.submissions.barrier = new Promise<void>(/**
     * 暂存回执结束函数，由测试明确放行。
     * Args:
     *     resolve (() => void): 完成回执的函数。
     */
    (resolve) => { release = resolve; });
    button.handlers.get("click")?.();
    button.handlers.get("click")?.();
    await flushEvents();
    assert.equal(button.disabled, true);
    assert.equal(view.submissions.commands.length, 1);
    assert.deepEqual(view.submissions.commands[0], { session_id: "s", origin_channel: "desktop",
      kind: "human_turn", payload: { message: "原正文", images: ["data:image/png;base64,YQ=="] } });
    release!();
    await flushEvents();
    assert.equal(button.disabled, false);
    assert.equal(failed.parent, view.root);
    assert.equal(view.input.value, "新草稿");
    // 2. 成功受理后移除临时失败消息，正式消息仍由后端交付事件产生。
    view.submissions.barrier = null;
    view.submissions.accepted = true;
    button.handlers.get("click")?.();
    await flushEvents();
    assert.equal(view.root.children.includes(failed), false);
    assert.equal(view.input.value, "新草稿");
    assert.equal(view.persisted.length, 0);
  } finally {
    release?.();
    view.close();
  }
});

test("迟到重试回执保留新失败提示，新消息受理后覆盖失败内容", /**
 * 验证失败输入按身份清理，历史刷新保留临时展示，新消息不重发旧输入。
 * Returns:
 *     Promise<void>: 交错回执和前端事件已处理。
 */
async () => {
  const view = createPresentation();
  let release: (() => void) | undefined;
  try {
    await view.emit("session_snapshot", { phase: "idle" }, 0);
    await view.emit("error", { detail: "第一次失败", failed_input: {
      input_sequence: 1, message: "旧失败", images: [],
    } }, 1);
    const oldButton = view.root.children[0].children[1];
    view.submissions.barrier = new Promise<void>(/**
     * 暂停旧回执，允许新失败事件先到达。
     * Args:
     *     resolve (() => void): 结束回执等待的函数。
     */
    (resolve) => { release = resolve; });
    oldButton.handlers.get("click")?.();
    await flushEvents();
    // 1. 重发已失败并返回新输入编号，旧回执此时还没有返回。
    await view.emit("error", { detail: "再次失败", failed_input: {
      input_sequence: 2, message: "新失败", images: [],
    } }, 2);
    release!();
    await flushEvents();
    assert.match(view.root.textContent, /新失败/);
    assert.equal(view.root.textContent.includes("旧失败"), false);
    // 2. 正常历史重绘不应把未交付失败消息写进历史或丢掉重试入口。
    await view.emit("session_snapshot", { phase: "idle" }, 3);
    assert.match(view.root.textContent, /新失败/);
    view.submissions.barrier = null;
    view.input.value = "换个问题";
    await view.client.sendMessage();
    assert.equal(view.root.textContent.includes("新失败"), false);
    assert.equal(view.submissions.commands.at(-1)?.payload?.message, "换个问题");
    assert.equal(view.persisted.length, 0);
  } finally {
    release?.();
    view.close();
  }
});


for (const child of [false, true]) {
  test(`保存故障使对应${child ? "子" : "父"}审批失效，迟到确认不会提交`, /**
   * 让真实客户端保持模态审批等待，再通过故障和恢复验证失效后的提交防护。
   * Returns:
   *     Promise<void>: 故障事件及迟到决定已验证。
   */
  async () => {
    const view = createPresentation();
    let release: ((value: { kind: "approve" }) => void) | undefined;
    try {
      await view.emit("session_snapshot", { phase: "idle" }, 0);
      view.approvals.barrier = new Promise(/** 暂停用户决定，等待故障使原审批失效。 */
        (resolve) => { release = resolve; });
      await view.emit(child ? "subagent_interrupt" : "interrupt", {
        value: { type: "authorization_request", request_id: "old-approval", message: "允许执行？" },
        ...(child ? { subagent_id: "child" } : {}),
      }, 1);
      assert.equal(view.approvals.opened, 1);
      const dismissed = view.approvals.dismissed;
      await view.emit("error", { save_failed: true, detail: "审批整理保存失败",
        ...(child ? { subagent_id: "child" } : {}) }, 2);
      assert.equal(view.approvals.dismissed, dismissed + 1);
      release!({ kind: "approve" });
      await flushEvents();
      assert.equal(view.submissions.commands.length, 0);
      assert.deepEqual(view.attention.at(-1), { approvalId: null });
      await view.emit("history_restored", { ...(child ? { subagent_id: "child" } : {}) }, 3);
      assert.match(view.root.textContent, /审批整理保存失败/);
    } finally {
      release?.({ kind: "approve" });
      view.close();
    }
  });
}
