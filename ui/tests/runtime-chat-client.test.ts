import { PresentationElement } from "./presentation-dom.js";
import { PluginStatusNotices } from "../src/chat/plugin-status-notices.js";
import assert from "node:assert/strict";
import { test } from "node:test";

import { RuntimeChatClient, type RuntimeChatClientOptions } from "../src/chat/runtime-chat-client.js";
import type { InteractionEvent } from "../shared-types.js";

/**
 * 为不涉及网络断线的场景提供完整的页面 bridge。
 *
 * Args:
 *     _callback ((sessionId: string) => void): 当前客户端的断线监听。
 *
 * Returns:
 *     () => void: 当前场景的监听清理函数。
 */
const subscribeWithoutDisconnect = (_callback: (sessionId: string) => void): (() => void) => {
  /** 当前场景没有创建断线源，结束时直接完成。 */
  return () => {};
};

test("桌面 Adapter 接收会话事件，执行失败后显示错误并恢复发送", /**
 * 验证桌面 Adapter 接收会话事件、提交输入，以及失败后的历史同步和发送恢复。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let listener: (event: InteractionEvent) => void = () => {};
  const commands: Array<{ kind: string; }> = [];
  const added: Array<{ content: string; images?: string[] }> = [];
  const errors: string[] = [];
  let historyLoads = 0;
  const bridge = {
    /**
     * 模拟桌面订阅建立，不连接真实后端。
     *
     * Args:
     *     _sessionId (string): 场景传入的共享会话标识。
     *
     * Returns:
     *     Promise<void>: 当前异步处理完成的等待结果。
     */
    ensureDesktopStream: async () => {},
    onInteractionSnapshotFailed: () => () => {},
    initializeDesktopSession: async (_sessionId: string) => {},
    onInteractionDisconnected: subscribeWithoutDisconnect,
    /**
     * 保存事件注入回调，并返回测试清理占位函数。
     *
     * Args:
     *     callback ((event: InteractionEvent) => void): 当前定义执行的回调。
     *
     * Returns:
     *     () => void: 移除本次运行事件监听的取消订阅函数。
     */
    onInteractionEvent: (callback: (event: InteractionEvent) => void) => {
      listener = callback;
      /**
       * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
       *
       * 此定义用于：保存事件注入回调，并返回测试清理占位函数。
       */
      return () => {};
    },
    /**
     * 记录测试命令并按场景返回预设受理回执。
     *
     * Args:
     *     command ({ kind: string; }): 包含会话和业务内容的统一入站命令。
     *
     * Returns:
     *     Promise<{ accepted: boolean; run_id: string; }>: 后端对本次用户命令的受理回执。
     */
    submitInteractionCommand: async (command: { kind: string; }) => {
      commands.push(command);
      return { accepted: true, status: "accepted" as const, run_id: "r1" };
    },
    /**
     * 返回测试配置的任务列表，不查询真实存储。
     *
     * Returns:
     *     Promise<never[]>: 当前会话的后台任务记录列表。
     */
    getBackgroundTasks: async () => [],
    /**
     * 返回测试配置的任务输出页。
     *
     * Returns:
     *     Promise<{ text: string; next_offset: number; has_more: boolean; }>: 任务输出内容及后续读取位置。
     */
    getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
  };
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
   */
  getElementById: () => null } as unknown as Document;
  try {
    const input = { value: "桌面消息", style: { height: "40px" }, /**
     * 记录输入框焦点恢复。
     *
     * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
     */
    focus: () => {} };
    const sendButton = {
      innerHTML: "send", disabled: false, title: "",
      /**
       * 接收按钮属性更新，测试只观察按钮标题。
       */
      setAttribute: () => {},
    } as unknown as HTMLButtonElement;
    const client = new RuntimeChatClient({
      statusNotices: new PluginStatusNotices(new PresentationElement() as unknown as HTMLElement),
      sessionId: "s", bridge,
      bubble: { /**
       * 记录气泡正文更新，供检查流式和历史显示顺序。
       *
       * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
       */
      setText: () => {} },
      chatHistory: {
        /**
         * 记录测试收到的真人消息内容和来源。
         *
         * Args:
         *     message ({ content: string; }): 当前真人草稿正文。
         */
        addMessage: (message: { content: string; images?: string[] }) => { added.push(message); },
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showTypingIndicator: () => {}, /**
         * 记录正在输入提示被隐藏。
         *
         * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
         */
        hideTypingIndicator: () => {},
        /**
         * 提供测试流式展示入口，不创建真实界面。
         */
        startStreaming: () => {}, /**
         * 记录最新助手消息显示。
         *
         * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
         */
        updateLastAiMessage: () => {},
        /**
         * 提供测试流式收尾替身，不持久保存真实回复。
         */
        finalizeStreamingMessage: () => {}, /**
         * 记录流式显示中止，供检查轮次切换。
         *
         * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
         */
        abortStreaming: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showToolCallMessage: () => {}, /**
         * 记录工具提示完成，供检查轮次收尾。
         *
         * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
         */
        finalizeToolCallIndicator: () => {},
        /**
         * 记录实际显示的错误，检查事件去重及错误提示是否保留。
         *
         * Args:
         *     message (string): 界面要显示的错误正文。
         */
        showPluginNotice: () => {},
      showErrorMessage: (message: string) => { errors.push(message); }, /**
         * 按脚本返回历史同步结果，并记录历史读取时机。
         *
         * Returns:
         *     Promise<never[]>: 当前测试没有已保存的历史消息。
         */
        loadHistory: async () => { historyLoads += 1; return []; },
      },
      sendBtn: sendButton,
      input: input as unknown as HTMLTextAreaElement,
      confirmDialog: {
        /**
         * 按场景返回人工审批决定。
         *
         * Returns:
         *     Promise<{ kind: "approve"; }>: 用户选择后的审批决定，或当前用例准备的决定。
         */
        open: async () => ({ kind: "approve" as const }),
        /**
         * 关闭已由其他入口处理的审批视图，调用方核对 request_id 后不再提交决定。
         */
        dismiss: () => {}, /**
         * 返回测试审批框当前是否打开。
         *
         * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
         */
        isOpen: () => false,
      } as RuntimeChatClientOptions["confirmDialog"],
    });
    listener({ session_id: "s", kind: "session_snapshot", payload: { phase: "idle" },
      stream_epoch: "epoch", event_sequence: 0 });
    listener({ session_id: "s", kind: "human_message", payload: { message: "QQ 消息", images: ["attachment.png"] },
      stream_epoch: "epoch", event_sequence: 1 });
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证桌面 Adapter 接收会话事件并用统一命令提交输入。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(added.length, 1);
    assert.equal(added[0].content, "QQ 消息");
    assert.deepEqual(added[0].images, ["attachment.png"]);
    await client.sendMessage();
    assert.equal(commands.length, 1);
    assert.equal(commands[0].kind, "human_turn");
    assert.equal(input.value, "");
    // 2. 开始后空草稿按钮用于停止；错误到达后应读取历史并恢复普通发送。
    listener({ session_id: "s", kind: "run_started", payload: {}, run_id: "r1",
      stream_epoch: "epoch", event_sequence: 2 });
    await new Promise<void>(/**
     * 等待开始事件处理完毕，再检查按钮的实际展示。
     *
     * Args:
     *     resolve ((value: void) => void): 下一事件循环中结束等待的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(sendButton.title, "停止当前回答");
    const beforeError = historyLoads;
    const failure: InteractionEvent = {
      session_id: "s", kind: "error", payload: { detail: "执行失败" }, run_id: "r1",
      stream_epoch: "epoch", event_sequence: 3,
    };
    listener(failure);
    listener(failure);
    await new Promise<void>(/**
     * 等待错误事件及历史刷新，重复序号不能再次显示错误。
     *
     * Args:
     *     resolve ((value: void) => void): 下一事件循环中结束等待的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.deepEqual(errors, ["执行失败"]);
    assert.equal(historyLoads, beforeError + 1);
    assert.equal(sendButton.title, "发送消息");
    input.value = "重试";
    await client.sendMessage();
    assert.equal(commands.length, 2);
    assert.equal(commands[1].kind, "human_turn");
  } finally {
    globalThis.document = oldDocument;
  }
});

test("重连快照已无审批时关闭过期审批框", /**
 * 验证重连快照已无审批时关闭过期审批框。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let listener: (event: InteractionEvent) => void = () => {};
  let dismissed = 0;
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证重连快照已无审批时关闭过期审批框。
   */
  getElementById: () => null } as unknown as Document;
  try {
    new RuntimeChatClient({
      statusNotices: new PluginStatusNotices(new PresentationElement() as unknown as HTMLElement),
      sessionId: "s",
      bridge: {
        /**
         * 模拟桌面订阅建立，不连接真实后端。
         *
         * Returns:
         *     Promise<void>: 当前异步处理完成的等待结果。
         */
        ensureDesktopStream: async () => {},
    onInteractionSnapshotFailed: () => () => {},
    initializeDesktopSession: async () => {},
        onInteractionDisconnected: subscribeWithoutDisconnect,
        /**
         * 保存事件注入回调，并返回测试清理占位函数。
         *
         * Args:
         *     callback ((event: InteractionEvent) => void): 当前定义执行的回调。
         *
         * Returns:
         *     () => void: 移除本次运行事件监听的取消订阅函数。
         */
        onInteractionEvent: (callback) => { listener = callback; /**
         * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：保存事件注入回调，并返回测试清理占位函数。
         */
        return () => {}; },
        /**
         * 记录测试命令并按场景返回预设受理回执。
         *
         * Returns:
         *     Promise<{ accepted: true; }>: 后端对本次用户命令的受理回执。
         */
        submitInteractionCommand: async () => ({ accepted: true, status: "accepted" as const }),
        /**
         * 返回测试配置的任务列表，不查询真实存储。
         *
         * Returns:
         *     Promise<never[]>: 当前会话的后台任务记录列表。
         */
        getBackgroundTasks: async () => [],
        /**
         * 返回测试配置的任务输出页。
         *
         * Returns:
         *     Promise<{ text: string; next_offset: number; has_more: false; }>: 任务输出内容及后续读取位置。
         */
        getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
      },
      bubble: { /**
       * 记录气泡正文更新，供检查流式和历史显示顺序。
       *
       * 此定义用于：验证重连快照已无审批时关闭过期审批框。
       */
      setText: () => {} },
      chatHistory: {
        /**
         * 记录测试收到的真人消息内容和来源。
         */
        addMessage: () => {}, /**
         * 记录正在输入提示被显示。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        showTypingIndicator: () => {}, /**
         * 记录正在输入提示被隐藏。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        hideTypingIndicator: () => {},
        /**
         * 提供测试流式展示入口，不创建真实界面。
         */
        startStreaming: () => {}, /**
         * 记录最新助手消息显示。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        updateLastAiMessage: () => {},
        /**
         * 提供测试流式收尾替身，不持久保存真实回复。
         */
        finalizeStreamingMessage: () => {}, /**
         * 记录流式显示中止，供检查轮次切换。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        abortStreaming: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showToolCallMessage: () => {}, /**
         * 记录工具提示完成，供检查轮次收尾。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        finalizeToolCallIndicator: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showPluginNotice: () => {},
      showErrorMessage: () => {}, /**
         * 按脚本返回历史同步结果，并记录历史读取时机。
         *
         * 此定义用于：验证重连快照已无审批时关闭过期审批框。
         */
        loadHistory: async () => [],
      },
      sendBtn: { innerHTML: "send", /**
       * 记录页面属性写入，供检查组件状态。
       *
       * 此定义用于：验证重连快照已无审批时关闭过期审批框。
       */
      setAttribute: () => {} } as unknown as HTMLButtonElement,
      input: { value: "", style: {}, /**
       * 记录输入框焦点恢复。
       *
       * 此定义用于：验证重连快照已无审批时关闭过期审批框。
       */
      focus: () => {} } as unknown as HTMLTextAreaElement,
      confirmDialog: {
        /**
         * 按场景返回人工审批决定。
         *
         * Returns:
         *     Promise<ApprovalDecision>: 用户选择后的审批决定，或当前用例准备的决定。
         */
        open: async () => await new Promise(/**
         * 由 Promise 构造器立即调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：按场景返回人工审批决定。
         */
        () => {}),
        /**
         * 关闭已由其他入口处理的审批视图，调用方核对 request_id 后不再提交决定。
         */
        dismiss: () => { dismissed += 1; },
      },
    });
    listener({ session_id: "s", kind: "session_snapshot", payload: {
      phase: "pending_interrupt",
      interrupt: { type: "authorization_request", request_id: "approval-1" },
    }, stream_epoch: "epoch", event_sequence: 1 });
    listener({ session_id: "s", kind: "session_snapshot", payload: {
      phase: "idle", interrupt: null,
    }, stream_epoch: "epoch", event_sequence: 2 });
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证重连快照已无审批时关闭过期审批框。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(dismissed, 1);
  } finally {
    globalThis.document = oldDocument;
  }
});

test("历史刷新期间积压的下一轮正文等待完成后显示", /**
 * 验证历史刷新期间积压的下一轮正文等待完成后显示。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let listener: (event: InteractionEvent) => void = () => {};
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let releaseHistory: () => void = () => {};
  let chunks = 0;
  let historyCalls = 0;
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
   */
  getElementById: () => null } as unknown as Document;
  try {
    new RuntimeChatClient({
      statusNotices: new PluginStatusNotices(new PresentationElement() as unknown as HTMLElement),
      sessionId: "s",
      bridge: {
        /**
         * 模拟桌面订阅建立，不连接真实后端。
         *
         * Returns:
         *     Promise<void>: 当前异步处理完成的等待结果。
         */
        ensureDesktopStream: async () => {},
    onInteractionSnapshotFailed: () => () => {},
    initializeDesktopSession: async () => {},
        onInteractionDisconnected: subscribeWithoutDisconnect,
        /**
         * 保存事件注入回调，并返回测试清理占位函数。
         *
         * Args:
         *     callback ((event: InteractionEvent) => void): 当前定义执行的回调。
         *
         * Returns:
         *     () => void: 移除本次运行事件监听的取消订阅函数。
         */
        onInteractionEvent: (callback) => { listener = callback; /**
         * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：保存事件注入回调，并返回测试清理占位函数。
         */
        return () => {}; },
        /**
         * 记录测试命令并按场景返回预设受理回执。
         *
         * Returns:
         *     Promise<{ accepted: true; }>: 后端对本次用户命令的受理回执。
         */
        submitInteractionCommand: async () => ({ accepted: true, status: "accepted" as const }),
        /**
         * 返回测试配置的任务列表，不查询真实存储。
         *
         * Returns:
         *     Promise<never[]>: 当前会话的后台任务记录列表。
         */
        getBackgroundTasks: async () => [],
        /**
         * 返回测试配置的任务输出页。
         *
         * Returns:
         *     Promise<{ text: string; next_offset: number; has_more: false; }>: 任务输出内容及后续读取位置。
         */
        getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
      },
      bubble: { /**
       * 记录气泡正文更新，供检查流式和历史显示顺序。
       *
       * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
       */
      setText: () => {} },
      chatHistory: {
        /**
         * 记录测试收到的真人消息内容和来源。
         */
        addMessage: () => {}, /**
         * 记录正在输入提示被显示。
         *
         * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
         */
        showTypingIndicator: () => {}, /**
         * 记录正在输入提示被隐藏。
         *
         * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
         */
        hideTypingIndicator: () => {},
        /**
         * 提供测试流式展示入口，不创建真实界面。
         */
        startStreaming: () => {}, /**
         * 记录最新助手消息显示。
         *
         * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
         */
        updateLastAiMessage: () => { chunks += 1; },
        /**
         * 提供测试流式收尾替身，不持久保存真实回复。
         */
        finalizeStreamingMessage: () => {}, /**
         * 记录流式显示中止，供检查轮次切换。
         *
         * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
         */
        abortStreaming: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showToolCallMessage: () => {}, /**
         * 记录工具提示完成，供检查轮次收尾。
         *
         * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
         */
        finalizeToolCallIndicator: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showPluginNotice: () => {},
      showErrorMessage: () => {},
        /**
         * 模拟历史加载及场景所需的完成屏障。
         *
         * Returns:
         *     Promise<never[]>: 已应用的隔离历史列表。
         */
        loadHistory: async () => {
          historyCalls += 1;
          if (historyCalls > 1) {
            await new Promise<void>(/**
             * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
             *
             * 此定义用于：模拟历史加载及场景所需的完成屏障。
             *
             * Args:
             *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
             */
            (resolve) => { releaseHistory = resolve; });
          }
          return [];
        },
      },
      sendBtn: { innerHTML: "send", /**
       * 记录页面属性写入，供检查组件状态。
       *
       * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
       */
      setAttribute: () => {} } as unknown as HTMLButtonElement,
      input: { value: "", style: {}, /**
       * 记录输入框焦点恢复。
       *
       * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
       */
      focus: () => {} } as unknown as HTMLTextAreaElement,
      confirmDialog: { /**
       * 记录审批框打开并按脚本等待选择。
       *
       * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
       */
      open: async () => ({ kind: "approve" }), /**
       * 记录过期审批框关闭。
       *
       * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
       */
      dismiss: () => {} },
    });
    listener({ session_id: "s", kind: "session_snapshot", payload: { phase: "running" },
      stream_epoch: "epoch", event_sequence: 0 });
    listener({ session_id: "s", kind: "run_completed", payload: { value: null },
      stream_epoch: "epoch", event_sequence: 1 });
    listener({ session_id: "s", kind: "run_started", payload: {},
      run_id: "next", stream_epoch: "epoch", event_sequence: 2 });
    listener({ session_id: "s", kind: "text_chunk", payload: { value: "新回复" },
      run_id: "next", stream_epoch: "epoch", event_sequence: 3 });
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(chunks, 0);
    releaseHistory();
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证历史刷新期间积压的下一轮正文等待完成后显示。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(chunks, 0);
  } finally {
    globalThis.document = oldDocument;
  }
});

test("纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮", /**
 * 验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let listener: (event: InteractionEvent) => void = () => {};
  let hasAttachments = false;
  let commandKind = "";
  const queueButton = { hidden: true, disabled: false };
  const sendButton = {
    innerHTML: "send", title: "", disabled: false,
    /**
     * 提供测试占位回调，不产生额外副作用。
     */
    setAttribute: () => {},
  };
  const oldDocument = globalThis.document;
  globalThis.document = {
    /**
     * 提供测试指定的 DOM 元素。
     *
     * Args:
     *     id (string): 当前测试查询的 DOM 元素标识。
     *
     * Returns:
     *     { hidden: boolean; disabled: boolean; } | null: 当前用例为该编号准备的页面控件。
     */
    getElementById: (id: string) => id === "queue-btn" ? queueButton : null,
  } as unknown as Document;
  try {
    const client = new RuntimeChatClient({
      statusNotices: new PluginStatusNotices(new PresentationElement() as unknown as HTMLElement),
      sessionId: "s", /**
       * 返回当前测试草稿是否包含图片。
       *
       * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
       */
      hasAttachments: () => hasAttachments,
      bridge: {
        /**
         * 模拟桌面订阅建立，不连接真实后端。
         *
         * Returns:
         *     Promise<void>: 当前异步处理完成的等待结果。
         */
        ensureDesktopStream: async () => {},
    onInteractionSnapshotFailed: () => () => {},
    initializeDesktopSession: async () => {},
        onInteractionDisconnected: subscribeWithoutDisconnect,
        /**
         * 保存事件注入回调，并返回测试清理占位函数。
         *
         * Args:
         *     callback ((event: InteractionEvent) => void): 当前定义执行的回调。
         *
         * Returns:
         *     () => void: 移除本次运行事件监听的取消订阅函数。
         */
        onInteractionEvent: (callback) => { listener = callback; /**
         * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：保存事件注入回调，并返回测试清理占位函数。
         */
        return () => {}; },
        /**
         * 记录测试命令并按场景返回预设受理回执。
         *
         * Args:
         *     command (InteractionCommand): 包含会话和业务内容的统一入站命令。
         *
         * Returns:
         *     Promise<{ accepted: true; }>: 后端对本次用户命令的受理回执。
         */
        submitInteractionCommand: async (command) => {
          commandKind = command.kind;
          return { accepted: true, status: "accepted" as const };
        },
        /**
         * 返回测试配置的任务列表，不查询真实存储。
         *
         * Returns:
         *     Promise<never[]>: 当前会话的后台任务记录列表。
         */
        getBackgroundTasks: async () => [],
        /**
         * 返回测试配置的任务输出页。
         *
         * Returns:
         *     Promise<{ text: string; next_offset: number; has_more: false; }>: 任务输出内容及后续读取位置。
         */
        getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
      },
      bubble: { /**
       * 记录气泡正文更新，供检查流式和历史显示顺序。
       *
       * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
       */
      setText: () => {} },
      chatHistory: {
        /**
         * 记录测试收到的真人消息内容和来源。
         */
        addMessage: () => {}, /**
         * 记录正在输入提示被显示。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        showTypingIndicator: () => {}, /**
         * 记录正在输入提示被隐藏。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        hideTypingIndicator: () => {},
        /**
         * 提供测试流式展示入口，不创建真实界面。
         */
        startStreaming: () => {}, /**
         * 记录最新助手消息显示。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        updateLastAiMessage: () => {},
        /**
         * 提供测试流式收尾替身，不持久保存真实回复。
         */
        finalizeStreamingMessage: () => {}, /**
         * 记录流式显示中止，供检查轮次切换。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        abortStreaming: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showToolCallMessage: () => {}, /**
         * 记录工具提示完成，供检查轮次收尾。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        finalizeToolCallIndicator: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showPluginNotice: () => {},
      showErrorMessage: () => {}, /**
         * 按脚本返回历史同步结果，并记录历史读取时机。
         *
         * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
         */
        loadHistory: async () => [],
      },
      sendBtn: sendButton as unknown as HTMLButtonElement,
      input: { value: "", style: {}, /**
       * 记录输入框焦点恢复。
       *
       * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
       */
      focus: () => {} } as unknown as HTMLTextAreaElement,
      confirmDialog: { /**
       * 记录审批框打开并按脚本等待选择。
       *
       * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
       */
      open: async () => ({ kind: "approve" }), /**
       * 记录过期审批框关闭。
       *
       * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
       */
      dismiss: () => {} },
    });
    listener({ session_id: "s", kind: "session_snapshot", payload: { phase: "running", run_id: "r" },
      stream_epoch: "epoch", event_sequence: 0 });
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证纯图片草稿可引导并显示排队入口，IME 组合期间不改发送按钮。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    assert.equal(sendButton.title, "停止当前回答");
    hasAttachments = true;
    client.updateSendButton();
    assert.equal(sendButton.title, "发送消息");
    assert.equal(queueButton.hidden, false);
    client.setComposing(true);
    hasAttachments = false;
    client.updateSendButton();
    assert.equal(sendButton.title, "发送消息");
    client.setComposing(false);
    assert.equal(sendButton.title, "停止当前回答");
    hasAttachments = true;
    await client.sendMessage(["data:image/png;base64,a"]);
    assert.equal(commandKind, "human_steer");
  } finally {
    globalThis.document = oldDocument;
  }
});

test("引导的 SSE 增量先于命令回执到达时清除等待提示", /**
 * 验证引导的 SSE 增量先于命令回执到达时清除等待提示。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  /**
   * 提供测试占位回调，不产生额外副作用。
   */
  let listener: (event: InteractionEvent) => void = () => {};
  let shown = 0;
  let cleared = 0;
  const oldDocument = globalThis.document;
  globalThis.document = { /**
   * 返回当前用例预设的聊天控件。
   *
   * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
   */
  getElementById: () => null } as unknown as Document;
  try {
    const client = new RuntimeChatClient({
      statusNotices: new PluginStatusNotices(new PresentationElement() as unknown as HTMLElement),
      sessionId: "s",
      bridge: {
        /**
         * 模拟桌面订阅建立，不连接真实后端。
         *
         * Returns:
         *     Promise<void>: 当前异步处理完成的等待结果。
         */
        ensureDesktopStream: async () => {},
    onInteractionSnapshotFailed: () => () => {},
    initializeDesktopSession: async () => {},
        onInteractionDisconnected: subscribeWithoutDisconnect,
        /**
         * 保存事件注入回调，并返回测试清理占位函数。
         *
         * Args:
         *     callback ((event: InteractionEvent) => void): 当前定义执行的回调。
         *
         * Returns:
         *     () => void: 移除本次运行事件监听的取消订阅函数。
         */
        onInteractionEvent: (callback) => { listener = callback; /**
         * 由外层操作在对应处理阶段调用；当前用例无需使用此通知内容。
         *
         * 此定义用于：保存事件注入回调，并返回测试清理占位函数。
         */
        return () => {}; },
        /**
         * 记录测试命令并按场景返回预设受理回执。
         *
         * Returns:
         *     Promise<{ accepted: true; }>: 后端对本次用户命令的受理回执。
         */
        submitInteractionCommand: async () => {
          listener({ session_id: "s", kind: "human_message", payload: { message: "改做这个" },
            stream_epoch: "epoch", event_sequence: 1 });
          await new Promise<void>(/**
           * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
           *
           * 此定义用于：记录测试命令并按场景返回预设受理回执。
           *
           * Args:
           *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
           */
          (resolve) => setImmediate(resolve));
          return { accepted: true, status: "accepted" as const };
        },
        /**
         * 返回测试配置的任务列表，不查询真实存储。
         *
         * Returns:
         *     Promise<never[]>: 当前会话的后台任务记录列表。
         */
        getBackgroundTasks: async () => [],
        /**
         * 返回测试配置的任务输出页。
         *
         * Returns:
         *     Promise<{ text: string; next_offset: number; has_more: false; }>: 任务输出内容及后续读取位置。
         */
        getBackgroundTaskOutput: async () => ({ text: "", next_offset: 0, has_more: false }),
      },
      bubble: { /**
       * 记录气泡正文更新，供检查流式和历史显示顺序。
       *
       * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
       */
      setText: () => {} },
      chatHistory: {
        /**
         * 记录测试收到的真人消息内容和来源。
         */
        addMessage: () => {}, /**
         * 记录正在输入提示被显示。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        showTypingIndicator: () => {}, /**
         * 记录正在输入提示被隐藏。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        hideTypingIndicator: () => {},
        /**
         * 提供测试流式展示入口，不创建真实界面。
         */
        startStreaming: () => {}, /**
         * 记录最新助手消息显示。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        updateLastAiMessage: () => {},
        /**
         * 提供测试流式收尾替身，不持久保存真实回复。
         */
        finalizeStreamingMessage: () => {}, /**
         * 记录流式显示中止，供检查轮次切换。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        abortStreaming: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showToolCallMessage: () => {}, /**
         * 记录工具提示完成，供检查轮次收尾。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        finalizeToolCallIndicator: () => {},
        /**
         * 提供测试占位回调，不产生额外副作用。
         */
        showPluginNotice: () => {},
      showErrorMessage: () => {}, /**
         * 按脚本返回历史同步结果，并记录历史读取时机。
         *
         * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
         */
        loadHistory: async () => [],
        /**
         * 展示引导已提交的本地等待提示，并区分是否在等待已启动工具。
         */
        showPendingSteer: () => { shown += 1; },
        /**
         * 记录引导等待提示清除次数，供增量先到场景断言。
         */
        clearPendingSteer: () => { cleared += 1; },
      },
      sendBtn: { innerHTML: "send", /**
       * 记录页面属性写入，供检查组件状态。
       *
       * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
       */
      setAttribute: () => {} } as unknown as HTMLButtonElement,
      input: { value: "改做这个", style: {}, /**
       * 记录输入框焦点恢复。
       *
       * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
       */
      focus: () => {} } as unknown as HTMLTextAreaElement,
      confirmDialog: { /**
       * 记录审批框打开并按脚本等待选择。
       *
       * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
       */
      open: async () => ({ kind: "approve" }), /**
       * 记录过期审批框关闭。
       *
       * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
       */
      dismiss: () => {} },
    });
    listener({ session_id: "s", kind: "session_snapshot", payload: { phase: "running", run_id: "r" },
      stream_epoch: "epoch", event_sequence: 0 });
    await new Promise<void>(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证引导的 SSE 增量先于命令回执到达时清除等待提示。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => setImmediate(resolve));
    await client.sendMessage();
    assert.equal(shown, 1);
    assert.equal(cleared, 1);
  } finally {
    globalThis.document = oldDocument;
  }
});
