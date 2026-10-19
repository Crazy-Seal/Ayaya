import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as flushEvents } from "node:timers/promises";
import { RuntimeChatClient, type RuntimeChatClientOptions } from "../src/chat/runtime-chat-client.js";
import { ChatHistoryManager } from "../src/chat/chat-history-manager.js";
import { BubbleManager } from "../src/chat/bubble.js";
import { BackgroundTasksPage } from "../src/settings/pages/background-tasks-page.js";
import type { ChatAttentionUpdate, ChatHistoryItem, InteractionEvent } from "../shared-types.js";

/**
 * 提供真实展示类所需的最小 DOM 行为，文本可从公开容器读取。
 *
 * Attributes:
 *     children (PresentationElement[]): 当前子节点。
 *     parent (PresentationElement | null): 用于移除提示及草稿的父节点。
 *     value (string): 节点自己的正文。
 */
class PresentationElement {
  children: PresentationElement[] = [];
  parent: PresentationElement | null = null;
  private value = "";
  className = "";
  hidden = true;
  disabled = false;
  title = "";
  src = "";
  attributes = new Map<string, string>();
  handlers = new Map<string, () => void>();
  scrollHeight = 100;
  classList = {
    /**
     *  提供展示器切换动画样式所需的占位方法。
     */
    add(): void {},
    /**
     *  提供展示器恢复动画样式所需的占位方法。
     */
    remove(): void {},
  };

  /**
   * 返回本节点与子节点的实际可见文字。
   *
   * Returns:
   *     string: 实际可见正文。
   */
  get textContent(): string {
    return this.value + this.children.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 child.textContent 供当前步骤继续处理。
     *
     * 此定义用于：返回本节点与子节点的实际可见文字。
     *
     * Args:
     *     child (PresentationElement): 本次管理的子进程。
     */
    (child) => child.textContent).join("");
  }

  /**
   * 按 DOM 语义替换节点文字及子节点。
   *
   * Args:
   *     text (string): 新正文。
   */
  set textContent(text: string) {
    this.value = text;
    this.children = [];
  }

  /**
   * 清空历史重绘的容器。
   *
   * Args:
   *     _html (string): 展示器设置的 HTML，测试仅需要清空行为。
   */
  set innerHTML(_html: string) {
    this.value = "";
    this.children = [];
  }

  /**
   * 追加子节点并保存移除关系。
   *
   * Args:
   *     child (PresentationElement): 新节点。
   *
   * Returns:
   *     PresentationElement: 已追加节点。
   */
  appendChild(child: PresentationElement): PresentationElement {
    // 1. 与真实 DOM 一致，追加已有节点表示移动，不能复制状态提示。
    child.remove();
    child.parent = this;
    this.children.push(child);
    return child;
  }

  /**
   *  从父节点移除提示或草稿。
   */
  remove(): void {
    if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
  }

  /**
   * 返回空图片集合，当前测试只检查文字。
   *
   * Returns:
   *     PresentationElement[]: 没有图片的查询结果。
   */
  querySelectorAll(): PresentationElement[] { return []; }

  /**
   * 记录公开 DOM 属性，供测试核对控件的无障碍名称。
   * Args:
   *     name (string): DOM 属性名称。
   *     value (string): 属性值。
   */
  setAttribute(name: string, value: string): void { this.attributes.set(name, value); }

  /**
   *  提供展示器滚动容器所需的占位方法。
   */
  scrollTo(): void {}

  /**
   * 登记公开控件回调，测试可模拟真实点击。
   *
   * Args:
   *     kind (string): DOM 事件类别。
   *     callback (() => void): 当前控件处理函数。
   */
  addEventListener(kind: string, callback: () => void): void { this.handlers.set(kind, callback); }

  /**
   * 按真实 DOM 顺序追加任务行中的节点。
   *
   * Args:
   *     children (PresentationElement[]): 当前任务行的节点。
   */
  append(...children: PresentationElement[]): void { for (const child of children) this.appendChild(child); }

  /**
   *  清空任务列表，让刷新后的记录替换旧行。
   */
  replaceChildren(): void { this.children = []; this.value = ""; }
}

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
  const bubble = new PresentationElement();
  const motions: string[] = [];
  const persisted: ChatHistoryItem[] = [];
  const historyReads = { calls: 0, barrier: null as Promise<void> | null, failure: null as string | null };
  const approvals = { opened: 0 };
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
     * Returns:
     *     Promise<ChatHistoryItem[]>: 场景已经提交的历史列表。
     */
    getChatHistoryLastN: async () => {
      historyReads.calls += 1;
      const saved = [...persisted];
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
    accepted: true,
    barrier: null as Promise<void> | null,
  };
  const input = {
    value: "", style: {},
    /** 保留用户当前输入，测试重发不能消耗正在编辑的草稿。 */
    focus(): void {},
  };
  const bridge: NonNullable<RuntimeChatClientOptions["bridge"]> = {
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
      return { accepted: submissions.accepted, reason: submissions.accepted ? undefined : "暂未受理" };
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
      open: async () => { approvals.opened += 1; return { kind: "approve" }; },
      /**
       *  提供跨端审批关闭占位方法。
       */
      dismiss() {},
    },
  });
  return {
    root, bubble, motions, persisted, taskHeader, taskButton, taskNotice, tasks, openedPages,
    client, historyReads, approvals, attention, input, submissions,
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
    new PresentationElement() as unknown as HTMLElement,
    new PresentationElement() as unknown as HTMLButtonElement,
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
      recent_output: "最新输出",
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
    await view.emit("text_chunk", { value: "讶>真有趣。<惊" }, 3);
    t.mock.timers.tick(500);
    assert.equal(view.bubble.textContent, "真有趣。");
    assert.equal(view.root.textContent, "真有趣");
    assert.deepEqual(view.motions, ["惊讶"]);

    // 2. 同轮重复标签不重复动作，后续句子和最终历史仍与流式正文一致。
    await view.emit("text_chunk", { value: "讶>后一句。" }, 4);
    t.mock.timers.tick(500);
    assert.equal(view.bubble.textContent, "真有趣。后一句。");
    assert.equal(view.root.textContent, "真有趣后一句");
    assert.deepEqual(view.motions, ["惊讶"]);
    view.persisted.push({ id: "ai-1", role: "AI", content: "<惊讶>真有趣。<惊讶>后一句。", timestamp: "2026-10-08" });
    await view.emit("run_completed", { value: null }, 5);
    assert.equal(view.root.textContent, "真有趣后一句");
    assert.equal(view.bubble.textContent, "真有趣。后一句。");
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

test("历史只在初始化、人工审批和终态读取，重复审批及后端工具操作不重复查询", /**
 * 验证后端直接执行工具期间不额外读取聊天历史。
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
    assert.equal(view.root.textContent, "工具说明");
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
    assert.equal(view.root.textContent, "工具说明完整结果");
    assert.equal(view.bubble.textContent, "完整结果。");
    await view.emit("run_completed", { value: null }, 14);
    assert.equal(view.historyReads.calls, 3);
  } finally {
    view.close();
  }
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
    assert.equal(view.root.textContent, "上一轮下一轮前半段后半段");
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
    assert.equal(view.root.textContent, "需要审批的完整说明");
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
    assert.equal(view.root.textContent, "此前已保存模型调用失败");
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
