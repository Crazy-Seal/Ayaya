/**
 * 使用真实 Electron 窗口、preload 和页面验证聊天拆分，所有配置与接口都隔离。
 * 先运行 npm run build，再用 electron scripts/verify-desktop-windows.mjs 启动。
 */
import { app, BrowserWindow, screen } from "electron";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { setTimeout as pause } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const uiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.join(uiRoot, ".test-dist", "desktop-smoke");
const restoreCheck = process.argv.includes("--restore-check");
const focusedCheck = process.argv.includes("--native-drag") || process.argv.includes("--composer-check")
  || process.argv.includes("--message-paragraphs") || process.argv.includes("--plugin-capabilities") || process.argv.includes("--subagents")
  || process.argv.includes("--status-notices");
const previousRun = restoreCheck ? JSON.parse(fs.readFileSync(path.join(output, "results.json"), "utf8")) : null;
const isolated = fs.mkdtempSync(path.join(os.tmpdir(), "ayaya-ui-windows-"));
let originalSavedBounds = null;
if (previousRun) {
  // 1. 只复制本验证创建的临时配置，拒绝把生产目录作为重启测试来源。
  const relative = path.relative(os.tmpdir(), previousRun.isolated);
  assert.ok(!relative.startsWith("..") && relative.startsWith("ayaya-ui-windows-"));
  fs.copyFileSync(path.join(previousRun.isolated, "frontend_settings.json"), path.join(isolated, "frontend_settings.json"));
  originalSavedBounds = JSON.parse(fs.readFileSync(path.join(isolated, "frontend_settings.json"), "utf8")).chat_window_bounds;
}
process.env.AYAYA_UI_DATA_DIR = isolated;
process.env.AYAYA_ENV = "test";
process.env.AYAYA_DATA_DIR = isolated;
const developmentCheck = process.argv.includes("--dev");
if (developmentCheck) process.env.VITE_DEV_SERVER_URL = "http://127.0.0.1:5174";
else delete process.env.VITE_DEV_SERVER_URL;
app.setPath("userData", path.join(isolated, "electron"));
fs.mkdirSync(output, { recursive: true });
// 1. 专项检查不删除完整桌面回归的结果，重启验证仍可读取上次完整运行的隔离配置。
for (const name of restoreCheck || focusedCheck ? [] : ["failure.json", "results.json"]) {
  const previous = path.join(output, name);
  if (fs.existsSync(previous)) fs.unlinkSync(previous);
}
const errors = [];
const windowEvents = [];
const commands = [];
const requests = [];
let sequence = 0;
let subscriptions = 0;
let stream = null;
let settings = null;
let exited = false;
let currentSession = "";
let developmentServer = null;
let demoTaskStatus = "running";
let taskSummaryMode = "success";
let taskSummaryAborted = false;
const subagents = [
  { subagent_id: "child-running", name: "代码探索助手", if_retain_context: true, status: "running", started_at: new Date().toISOString(), created_at: new Date().toISOString() },
  { subagent_id: "child-idle", name: "空闲测试助手", if_retain_context: true, status: "idle", started_at: null, created_at: new Date().toISOString() },
];

const history = [
  { id: "human-1", role: "human", content: "把聊天记录和输入框放在一起，看起来像聊天软件。", timestamp: "2026-10-11T04:00:00Z" },
  { id: "ai-1", role: "ai", content: "聊天窗口可以移动和最小化。Live2D 会继续留在桌面。", timestamp: "2026-10-11T04:00:01Z" },
];

/**
 * 生成符合现有协议的事件，事件水位由当前隔离会话推进。
 * Args:
 *     kind (string): 当前事件类型。
 *     payload (object): 当前事件内容。
 *     extra (object): 运行或请求编号。
 * Returns:
 *     object: 完整交互事件。
 */
const event = (kind, payload, extra = {}) => ({
  kind, payload, session_id: currentSession, stream_epoch: "desktop-test",
  event_sequence: sequence++, origin_channel: "desktop", ...extra,
});

/**
 * 向实际主进程订阅推送模拟后端事件。
 * Args:
 *     value (object): 当前交互事件。
 */
const emit = (value) => stream.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`));

/**
 * 构造 JSON 响应，保持后端原来的包装格式。
 * Args:
 *     value (unknown): 模拟接口响应正文。
 * Returns:
 *     Response: 内存响应，不访问网络。
 */
const json = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });

globalThis.fetch = /**
 * 拦截所有主进程后端请求，只返回内存数据；任何未知接口使测试失败。
 * Args:
 *     input (string): 实际 backendFetch 拼接的地址。
 *     init (RequestInit): 方法、正文和取消信号。
 * Returns:
 *     Promise<Response>: 内存 JSON 或事件流。
 */
async (input, init = {}) => {
  const url = new URL(String(input));
  const route = url.pathname;
  requests.push(route);
  if (route === "/internal/ready") return json({ ready: true, pid: process.pid });
  if (route.startsWith("/agent/events/")) {
    subscriptions++;
    currentSession = decodeURIComponent(route.split("/").at(-1));
    return new Response(new ReadableStream({
      /**
       * 实际订阅建立后先交付快照，后续事件由 emit 推送。
       * Args:
       *     controller (ReadableStreamDefaultController): 本次流控制器。
       */
      start(controller) {
        stream = controller;
        emit(event("session_snapshot", { phase: "idle", queued_inputs: [], tasks: [], interrupt: null,
          ...(process.argv.includes("--subagents") ? { subagents } : {}) }));
        init.signal?.addEventListener("abort", /** 取消订阅只关闭测试流，不访问任何后台进程。 */
        () => { try { controller.close(); } catch { /* 流可能已经关闭。 */ } }, { once: true });
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  }
  if (route.startsWith("/chat_history")) return json({ code: 200, data: history });
  if (route === "/chat_settings" && init.method === "POST") { settings = JSON.parse(init.body); return json({ code: 200, data: settings }); }
  if (route === "/chat_settings" && init.method === "PUT") {
    const candidate = JSON.parse(init.body);
    if (candidate.agent_plugins?.demo?.config?.endpoint === "backend-invalid") {
      return new Response(JSON.stringify({ detail: [{ loc: ["agent_plugins", "demo", "config", "endpoint"], msg: "连接地址被插件自定义规则拒绝" }] }), { status: 422 });
    }
    settings = candidate;
    return json({ code: 200, data: settings });
  }
  if (route.startsWith("/chat_settings")) return json({ code: 200, data: settings });
  if (route === "/agent/commands") {
    const command = JSON.parse(init.body); commands.push(command);
    if (command.kind === "interact_subagent") subagents.find(item => item.subagent_id === command.subagent_id).status = "idle";
    return json({ accepted: true, input_sequence: commands.length });
  }
  if (route.startsWith("/agent/subagents/")) {
    const child = subagents.find(item => item.subagent_id === route.split("/").at(-1));
    return json(child ? { ...child, system_prompt: "检查项目代码并提供结论", tool_whitelist: ["local:read_file"],
      context_available: true, recent_interactions: [{ interaction_id: 1, kind: "task", text: "检查实现", created_at: child.created_at }], results: [] }
      : { subagents });
  }
  if (route === "/internal/tasks/active-summary") {
    if (taskSummaryMode === "error") return new Response("查询失败", { status: 503 });
    if (taskSummaryMode === "timeout") {
      return new Promise(/**
       * 模拟后端一直等待，验证实际主进程请求会在预算耗尽时中止。
       * Args:
       *     _resolve ((value: Response) => void): 本场景保持响应等待。
       *     reject ((reason: unknown) => void): 请求被中止后报告失败。
       */
      (_resolve, reject) => {
        init.signal.addEventListener("abort", /** 记录主进程实际取消 HTTP 请求，结束模拟等待。 */
        () => { taskSummaryAborted = true; reject(init.signal.reason); }, { once: true });
      });
    }
    return json({ active_count: 2 });
  }
  if (route.startsWith("/tasks/") && route.endsWith("/output")) {
    return json({ text: "完整终端输出：资料已读取。", next_offset: 42, has_more: false });
  }
  if (route.startsWith("/tasks/")) {
    const base = { session_id: currentSession, command: null, pid: null, exit_code: null, output_path: null,
      created_at: new Date().toISOString(), ended_at: null, origin_run_id: null, origin_tool_call_id: null,
      output_ref: null, background: true, tool_name: null, name: "terminal", title: "", result: null,
      error: null, plugin_name: null, notify_agent: true };
    return json({ tasks: [
      { ...base, task_id: "task-demo", tool_name: "exec_command", command: "整理项目资料", title: "整理项目资料", pid: 123,
        ...(process.argv.includes("--subagents") ? { subagent_id: "child-running" } : {}),
        output_path: "demo.log", output_ref: "task-output://task-demo", status: demoTaskStatus, recent_output: "正在读取资料…" },
      { ...base, task_id: "task-other", tool_name: "other_tool", command: "后台分析资料", title: "后台分析资料", pid: 124,
        output_path: "other.log", output_ref: "task-output://task-other", status: "completed", exit_code: 0, recent_output: "处理完成" },
      { ...base, task_id: "task-memory", plugin_name: "memory", name: "memory.check_diary", title: "整理历史日记",
        notify_agent: false, result: "日记整理完成", status: "completed" },
    ] });
  }
  if (route.startsWith("/agent/channels/")) return json({ channels: [
    { channel: "desktop", display_name: "桌面", ready: true, enabled: true },
    { channel: "test-channel", display_name: "测试渠道", ready: false, enabled: false },
  ] });
  if (route === "/tools") return json({ code: 200, data: { tools: [] } });
  if (route.startsWith("/tool-permissions")) return json({ code: 200, data: { tools: process.argv.includes("--plugin-capabilities") ? [
    { stable_key: "plugin:demo:local:read", policy_key: "local:read", source: "local", name: "read", display_name: "读取游戏", description: "本地读取", policy: "ask", plugin_name: "demo", available: true },
    { stable_key: "plugin:demo:mcp:game:read", policy_key: "mcp:game:read", source: "mcp", name: "read", display_name: "远端读取", description: "远端读取", policy: "ask", plugin_name: "demo", server_id: "game", server_name: "游戏服务", available: true },
  ] : [] } });
  if (route.startsWith("/plugins/mcp-status/")) return json({ code: 200, data: { plugins: { demo: [
    { id: "game", name: "游戏服务", runtime: { status: "available", tool_count: 1, instructions: null, last_error: null } },
  ] } } });
  if (route === "/plugins") return json({ code: 200, data: { plugins: process.argv.includes("--plugin-capabilities") ? [{
    name: "memory", description: "检索和沉淀长期记忆。",
    default_config: { enable_diary: true, enable_episodic: true, enable_semantic: true, long_term_memory_every_human_messages: 10 },
    config_schema: { type: "object", properties: {
      enable_diary: { type: "boolean", title: "日记记忆", description: "是否启用日记记忆。", default: true },
      enable_episodic: { type: "boolean", title: "情景记忆", description: "是否启用情景记忆。", default: true },
      enable_semantic: { type: "boolean", title: "语义记忆", description: "是否启用语义记忆。", default: true },
      long_term_memory_every_human_messages: { type: "integer", title: "长期记忆提取间隔", minimum: 1, default: 10,
        description: "每多少轮真实用户消息触发长期记忆总结/抽取。" },
    } },
  }, {
    name: "demo", description: "独立插件配置和权限验证", default_config: { endpoint: "local-game", options: { count: 2 } },
    config_schema: { type: "object", required: ["endpoint", "watch"], properties: {
      watch: { type: "boolean", title: "监视游戏" },
      endpoint: { type: "string", minLength: 2, title: "游戏地址", description: "由插件参数生成 MCP 连接" },
      options: { type: "object", title: "复杂参数", properties: { count: { type: "integer", minimum: 1 } } },
    } },
  }] : [] } });
  if (route.startsWith("/mcp/")) return json({ code: 200, data: [] });
  throw new Error(`桌面验证禁止未知请求：${route}`);
};

const windows = await import("../dist-electron/electron/window-manager.js");
const { registerIpcHandlers } = await import("../dist-electron/electron/ipc-handlers.js");
const { configureBackendClient } = await import("../dist-electron/electron/backend-client.js");
const { stopDesktopSession } = await import("../dist-electron/electron/interaction-adapter.js");
const { createEmptyChatSettings } = await import("../dist-electron/electron/chat-settings.js");
const { getActiveModelRecord, loadModelConfig, saveModelConfig } = await import("../dist-electron/electron/model-manager.js");
const { registerLive2DProtocol, initLive2DProtocolHandler } = await import("../dist-electron/electron/live2d-protocol.js");
const { initAyayaImageProtocolHandler } = await import("../dist-electron/electron/ayaya-image-protocol.js");
const { registerLogIpcHandlers } = await import("../dist-electron/electron/logging/log-ipc.js");

registerLive2DProtocol();
app.on("browser-window-created", /**
 * 收集真实页面错误；启动脚本不允许隐藏页面异常。
 * Args:
 *     _event (Event): Electron 事件。
 *     win (BrowserWindow): 新窗口。
 */
(_event, win) => {
  for (const name of ["ready-to-show", "show", "hide"]) {
    win.on(name, /**
     * 记录原生首次绘制和显示事件，定位隐藏窗口启动问题。
     * Args:
     *     _event (Event): Electron 的窗口事件。
     */
    () => { windowEvents.push({ id: win.id, name, time: Date.now() }); });
  }
  win.webContents.on("console-message", /**
   * 记录页面 error 级别日志；字体网络失败不属于页面脚本异常。
   * Args:
   *     details (WebContentsConsoleMessageEventParams): Electron 提供的页面日志级别和正文。
   */
  (details) => { if (details.level === "error") errors.push(details.message); });
});

/**
 * 等待页面的真实条件，超时输出当前页面正文帮助定位失败。
 * Args:
 *     win (BrowserWindow): 待验证的窗口。
 *     expression (string): 不含用户数据的固定测试表达式。
 */
const waitFor = async (win, expression) => {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (await win.webContents.executeJavaScript(expression)) return;
    await pause(100);
  }
  throw new Error(`页面条件未满足：${expression}\n${await win.webContents.executeJavaScript("document.body.innerText")}`);
};

/**
 * 保存窗口实际渲染截图。
 * Args:
 *     win (BrowserWindow): 待截取窗口。
 *     name (string): 当前验收场景名称。
 */
const capture = async (win, name) => {
  await pause(150);
  fs.writeFileSync(path.join(output, `${name}.png`), (await win.webContents.capturePage()).toPNG());
};

/** 等 Electron 就绪后运行验证，避免顶层 await 阻止 ready 事件。 */
const run = async () => {
try {
  await app.whenReady();
  // 1. 开发模式使用单独端口的 Vite，不连接用户正在运行的服务或生产存储。
  if (developmentCheck) {
    const { createServer } = await import("vite");
    developmentServer = await createServer({ root: uiRoot, server: { host: "127.0.0.1", port: 5174, strictPort: true } });
    await developmentServer.listen();
  }
  configureBackendClient("isolated-desktop-test");
  await createEmptyChatSettings(getActiveModelRecord().sessionId);
  const config = loadModelConfig();
  config.models[0].motionConfig = [{ motionName: "Idle", setting: "expression", label: "开心" }];
  saveModelConfig(config);
  initLive2DProtocolHandler(); initAyayaImageProtocolHandler();
  registerIpcHandlers({ /** 设置确认后只记录退出请求，验证清理前不关闭窗口。 */
    requestApplicationExit: () => { exited = true; } });
  registerLogIpcHandlers({ policy: windows.getTrustedRendererPolicy(), openLogWindow: windows.openLogWindow, sendBatch: windows.sendLogBatch });
  const pet = windows.createMainWindow();
  const chat = windows.openChatWindow();
  await waitFor(chat, "document.querySelectorAll('.message-item').length >= 2 && !document.getElementById('send-btn').disabled");
  if (process.argv.includes("--status-notices")) {
    const { InteractionEventKind } = await import("../dist-electron/shared-types.js");
    const notice = { id: "connection", plugin_name: "demo", active: true, text: "正在连接应用" };
    // 1. 事件经过正式订阅、IPC 和 preload；提示区域与历史列表分别显示。
    emit(event(InteractionEventKind.STATUS_NOTICE, notice));
    await waitFor(chat, "document.getElementById('plugin-status-notices').textContent.includes('正在连接应用')");
    assert.equal(await chat.webContents.executeJavaScript("document.getElementById('chat-history-list').textContent.includes('正在连接应用')"), false);
    await capture(chat, "status-notice-open");
    emit(event(InteractionEventKind.STATUS_NOTICE, { ...notice, text: "已连接，正在执行" }));
    await waitFor(chat, "document.getElementById('plugin-status-notices').textContent.includes('已连接，正在执行')");
    assert.equal(await chat.webContents.executeJavaScript("document.getElementById('plugin-status-notices').children.length"), 1);
    await capture(chat, "status-notice-update");
    // 2. 历史刷新与另一插件的同名提示都保留当前显示。
    emit(event(InteractionEventKind.STATUS_NOTICE, { ...notice, plugin_name: "other", text: "另一个插件的任务" }));
    emit(event(InteractionEventKind.HISTORY_RESTORED, {}));
    await waitFor(chat, "document.getElementById('plugin-status-notices').children.length === 2");
    emit(event(InteractionEventKind.STATUS_NOTICE, { id: notice.id, plugin_name: "demo", active: false }));
    emit(event(InteractionEventKind.STATUS_NOTICE, { id: notice.id, plugin_name: "other", active: false }));
    await waitFor(chat, "document.getElementById('plugin-status-notices').hidden");
    await capture(chat, "status-notice-closed");
    // 3. 关闭 SSE 验证真实断线 IPC，重连快照不恢复提示，后续新事件仍可显示。
    emit(event(InteractionEventKind.STATUS_NOTICE, notice));
    await waitFor(chat, "!document.getElementById('plugin-status-notices').hidden");
    stream.close();
    await waitFor(chat, "document.getElementById('plugin-status-notices').hidden");
    for (let attempt = 0; attempt < 40 && subscriptions < 2; attempt += 1) await pause(100);
    assert.ok(subscriptions >= 2);
    await waitFor(chat, "!document.getElementById('send-btn').disabled");
    assert.equal(await chat.webContents.executeJavaScript("document.getElementById('plugin-status-notices').hidden"), true);
    emit(event(InteractionEventKind.STATUS_NOTICE, { ...notice, text: "新连接开始的任务" }));
    await waitFor(chat, "document.getElementById('plugin-status-notices').textContent.includes('新连接开始的任务')");
    await capture(chat, "status-notice-reconnected");
    emit(event(InteractionEventKind.STATUS_NOTICE, { id: notice.id, plugin_name: "demo", active: false }));
    await waitFor(chat, "document.getElementById('plugin-status-notices').hidden");
    fs.writeFileSync(path.join(output, "status-notice-results.json"), JSON.stringify({ passed: true, subscriptions, isolated, errors, requests }, null, 2));
    console.log("持续提示验证通过：开启、更新、关闭、历史刷新及真实断线重连。");
    return;
  }
  if (process.argv.includes("--subagents")) {
    // 1. 经真实 preload 和 IPC 从聊天名单入口打开独立面板，空闲保留型仍可见。
    await waitFor(chat, "!document.getElementById('subagent-entry').hidden");
    await chat.webContents.executeJavaScript("document.getElementById('subagent-entry').click()");
    const panel = windows.getSettingsWindow();
    await waitFor(panel, "document.getElementById('subagents-list')?.textContent.includes('空闲测试助手')");
    await panel.webContents.executeJavaScript("[...document.querySelectorAll('#subagents-list button')].find(item=>item.textContent==='详情').click()");
    await waitFor(panel, "document.getElementById('subagents-list').textContent.includes('检查项目代码并提供结论')");
    await pause(2400);
    assert.ok(await panel.webContents.executeJavaScript("document.getElementById('subagents-list').textContent.includes('检查项目代码并提供结论')"));
    await capture(panel, "subagent-panel");
    await panel.webContents.executeJavaScript("[...document.querySelectorAll('#subagents-list button')].find(item=>item.textContent==='停止').click()");
    await waitFor(panel, "![...document.querySelectorAll('#subagents-list button')].some(item=>item.textContent==='停止')");
    assert.equal(commands.at(-1).subagent_id, "child-running");
    // 2. 普通后台任务仍在任务页，显示所属子身份，并复用原输出和单任务停止入口。
    await panel.webContents.executeJavaScript("document.querySelector('[data-page=tasks]').click()");
    await waitFor(panel, "document.getElementById('background-tasks-list').textContent.includes('子 Agent child-running')");
    await capture(panel, "subagent-background-tasks");
    // 3. 子审批不改变父回答阶段；重连快照保持当前审批，再经统一命令入口提交实际子 ID。
    const approval = { type: "authorization_request", request_id: "child-approval", message: "子工具请求批准" };
    const prompt = { subagent_id: "child-running", request_id: approval.request_id, value: approval };
    emit(event("run_started", {}, { run_id: "parent-run" }));
    emit(event("subagent_interrupt", prompt, { request_id: approval.request_id }));
    await waitFor(chat, "!document.getElementById('screenshot-confirm-dialog').hidden");
    assert.equal(await chat.webContents.executeJavaScript("document.getElementById('send-btn').title"), "停止当前回答");
    emit(event("session_snapshot", { phase: "running", run_id: "parent-run", queued_inputs: [], tasks: [], subagents, approval_prompt: prompt }));
    await waitFor(chat, "document.getElementById('screenshot-confirm-dialog').textContent.includes('child-running')");
    await capture(chat, "subagent-approval");
    await chat.webContents.executeJavaScript("document.getElementById('screenshot-allow-btn').click()");
    await waitFor(chat, "document.getElementById('screenshot-confirm-dialog').hidden");
    await pause(100);
    assert.equal(commands.at(-1).kind, "resolve_authorization");
    assert.equal(commands.at(-1).subagent_id, "child-running");
    fs.writeFileSync(path.join(output, "subagent-results.json"), JSON.stringify({ passed: true, isolated, requests, commands, errors }, null, 2));
    console.log("子 Agent 面板、后台任务来源、审批和重连 Electron 验证通过");
    return;
  }
  if (process.argv.includes("--plugin-capabilities")) {
    windows.openSettingsWindow();
    const settingsWindow = windows.getSettingsWindow();
    await waitFor(settingsWindow, "document.querySelector('[data-page=memory].settings-tab')");
    await settingsWindow.webContents.executeJavaScript("document.querySelector('[data-page=memory].settings-tab').click()");
    await waitFor(settingsWindow, "document.querySelector('[data-plugin-name=demo] textarea')");
    // 1. 在宽窄窗口中检查真实布局，启用开关位于右上角，布尔参数与标题同行。
    const originalSize = settingsWindow.getSize();
    settingsWindow.setResizable(true);
    for (const width of [1100, 700]) {
      settingsWindow.setSize(width, 900);
      await pause(100);
      assert.ok(Math.abs(settingsWindow.getSize()[0] - width) <= 4);
      const layout = await settingsWindow.webContents.executeJavaScript(`(() => {
        const card = document.querySelector('[data-plugin-name=memory]');
        card.scrollIntoView({ block: 'start' });
        const header = card.querySelector('.plugin-card-header').getBoundingClientRect();
        const toggle = card.querySelector('.plugin-toggle').getBoundingClientRect();
        const checkbox = card.querySelector('[data-plugin-enabled]').getBoundingClientRect();
        const fields = [...card.querySelectorAll('.plugin-config-field')];
        return {
          rightAligned: Math.abs(header.right - toggle.right) < 2,
          topAligned: Math.abs(header.top - toggle.top) < 2,
          verticallyCentered: Math.abs(toggle.y + toggle.height / 2 - checkbox.y - checkbox.height / 2) < 2,
          noOverflow: card.scrollWidth <= card.clientWidth,
          grouped: fields.every(field => parseFloat(getComputedStyle(field).borderTopWidth) > 0),
          inlineBooleans: fields.filter(field => field.querySelector('input[type=checkbox]')).every(field => {
            const label = field.querySelector('.plugin-config-field-heading label').getBoundingClientRect();
            const input = field.querySelector('input[type=checkbox]').getBoundingClientRect();
            return Math.abs(label.y + label.height / 2 - input.y - input.height / 2) < 2;
          }),
        };
      })()`);
      for (const [name, passed] of Object.entries(layout)) assert.equal(passed, true, `${width}px: ${name}`);
      await capture(settingsWindow, `plugin-memory-layout-${width}`);
    }
    settingsWindow.setSize(...originalSize);
    settingsWindow.setResizable(false);
    await settingsWindow.webContents.executeJavaScript("document.querySelector('[data-plugin-name=demo]').scrollIntoView({block:'start'})");
    const writes = () => requests.filter(route => route === "/chat_settings").length;
    const before = writes();
    await settingsWindow.webContents.executeJavaScript(`(() => {
      const card=document.querySelector('[data-plugin-name=demo]');
      card.querySelector('[data-plugin-enabled]').checked=true;
      card.querySelector('textarea').value='{"count":0}';
      document.getElementById('plugins-confirm-btn').click();
    })()`);
    await waitFor(settingsWindow, "document.querySelector('[data-plugin-name=demo]').textContent.includes('/options/count')");
    assert.equal(await settingsWindow.webContents.executeJavaScript("document.querySelector('[data-plugin-name=demo]').textContent.includes('/watch')"), true);
    assert.equal(writes(), before);
    await capture(settingsWindow, "plugin-capabilities-invalid-json-field");
    await settingsWindow.webContents.executeJavaScript(`(() => {
      const card=document.querySelector('[data-plugin-name=demo]');
      card.querySelector('textarea').value='{"count":3}';
      const watch=card.querySelector('[data-config-key=watch]'); watch.checked=true; watch.dispatchEvent(new Event('input'));
      card.querySelector('[data-config-key=endpoint]').value='backend-invalid';
      document.getElementById('plugins-confirm-btn').click();
    })()`);
    await waitFor(settingsWindow, "document.querySelector('[data-plugin-name=demo]').textContent.includes('连接地址被插件自定义规则拒绝')");
    await settingsWindow.webContents.executeJavaScript(`(() => {
      const card=document.querySelector('[data-plugin-name=demo]');
      card.querySelector('[data-config-key=endpoint]').value='updated-game';
      card.querySelectorAll('select')[0].value='deny';
      document.getElementById('plugins-confirm-btn').click();
    })()`);
    await waitFor(settingsWindow, "document.querySelector('[data-config-key=endpoint]')?.value==='updated-game' && document.querySelector('[data-plugin-name=demo] textarea')?.value.includes('3')");
    await pause(300);
    assert.equal(settings.agent_plugins.demo.tool_permissions['local:read'], 'deny');
    assert.equal(settings.agent_plugins.demo.config.options.count, 3);
    await capture(settingsWindow, "plugin-capabilities-saved");
    await settingsWindow.webContents.executeJavaScript("document.querySelector('[data-page=tools].settings-tab').click()");
    await pause(200);
    assert.equal(await settingsWindow.webContents.executeJavaScript("document.querySelector('#tools-table-body').textContent.includes('远端读取')"), false);
    fs.writeFileSync(path.join(output, "plugin-capabilities-results.json"), JSON.stringify({ passed: true, isolated, errors, requests }, null, 2));
    console.log(`插件设置验证通过：${output}`);
    return;
  }
  if (process.argv.includes("--message-paragraphs")) {
    const initialCount = await chat.webContents.executeJavaScript("document.querySelectorAll('.message-item.ai').length");
    // 1. 按真实流式事件逐次发送换行，只有第二个换行到达才出现新的段落气泡。
    emit(event("run_started", {}, { run_id: "paragraph-demo" }));
    emit(event("text_chunk", { value: "你好！\n" }, { run_id: "paragraph-demo" }));
    await pause(600);
    assert.equal(await chat.webContents.executeJavaScript("document.querySelectorAll('.message-item.ai').length"), initialCount);
    emit(event("text_chunk", { value: "\n" }, { run_id: "paragraph-demo" }));
    await waitFor(chat, `document.querySelectorAll('.message-item.ai').length === ${initialCount + 1}`);
    emit(event("text_chunk", { value: "再见！\n明天见。\n\n" }, { run_id: "paragraph-demo" }));
    await waitFor(chat, `document.querySelectorAll('.message-item.ai').length === ${initialCount + 2}`);
    const texts = await chat.webContents.executeJavaScript(`
      Array.from(document.querySelectorAll('.message-item.ai .message-bubble'), /**
       * 读取浏览器实际渲染的气泡正文，核对单换行仍在同一气泡内部。
       * Args:
       *     bubble (HTMLElement): 当前 Agent 气泡。
       * Returns:
       *     string: 当前气泡正文。
       */
      (bubble) => bubble.textContent).slice(-2)
    `);
    assert.deepEqual(texts, ["你好！", "再见！\n明天见。"]);
    // 2. 完成后重新读取历史，气泡数量与流式显示保持一致。
    history.push({ id: "paragraph-ai", role: "ai", content: "你好！\n\n再见！\n明天见。", timestamp: "2026-10-26T00:00:00Z" });
    emit(event("run_completed", { value: null }, { run_id: "paragraph-demo" }));
    await waitFor(chat, "!document.getElementById('chat-history-list').textContent.includes('对方正在输入')");
    await capture(chat, "chat-message-paragraphs");
    assert.equal(await chat.webContents.executeJavaScript("document.querySelectorAll('.message-item.ai').length"), initialCount + 2);
    console.log("消息双换行分隔 Electron 验证通过");
    return;
  }
  if (process.argv.includes("--plugin-runtime")) {
    emit(event("notice", { plugin_name: "game_helper", text: "发现两名敌人，正在观察战斗状态。" }));
    await waitFor(chat, "document.querySelector('.plugin-notice')?.textContent.includes('发现两名敌人')");
    await capture(chat, "plugin-runtime-notice");
    windows.openSettingsWindow();
    const settingsWindow = windows.getSettingsWindow();
    await waitFor(settingsWindow, "document.getElementById('exit-application-btn')?.querySelector('svg')");
    await settingsWindow.webContents.executeJavaScript("document.querySelector('[data-page=tasks]').click()");
    await waitFor(settingsWindow, "document.getElementById('background-tasks-list')?.textContent.includes('整理历史日记')");
    const taskText = await settingsWindow.webContents.executeJavaScript("document.getElementById('background-tasks-list').textContent");
    assert.match(taskText, /整理项目资料/);
    assert.match(taskText, /日记整理完成/);
    assert.doesNotMatch(taskText, /undefined/);
    // 1. 用真实页面同时展开终端输出和日记结果，检查卡片分隔及详情归属。
    assert.equal(await settingsWindow.webContents.executeJavaScript("document.querySelectorAll('#background-tasks-list > .background-task-card.ui-surface').length"), 3);
    await settingsWindow.webContents.executeJavaScript(`
      document.querySelectorAll('.background-task-row button[aria-expanded]')[0].click();
      document.querySelectorAll('.background-task-row button[aria-expanded]')[2].click();
    `);
    await waitFor(settingsWindow, "document.querySelectorAll('.background-task-output')[0].textContent.includes('完整终端输出')");
    const taskCards = await settingsWindow.webContents.executeJavaScript(`
      Array.from(document.querySelectorAll('.background-task-card'), /**
       * 读取各卡片在浏览器中实际显示的详情和位置。
       * Args:
       *     card (HTMLElement): 当前任务卡片。
       */
      (card) => ({
        top: card.getBoundingClientRect().top,
        bottom: card.getBoundingClientRect().bottom,
        border: getComputedStyle(card).borderTopWidth,
        expanded: !card.querySelector('.background-task-details').hidden,
        output: card.querySelector('.background-task-output').textContent,
      }))
    `);
    assert.deepEqual(taskCards.map(/** 返回各卡片详情是否展开。 */ (card) => card.expanded), [true, false, true]);
    assert.match(taskCards[0].output, /完整终端输出/);
    assert.equal(taskCards[2].output, "日记整理完成");
    assert.ok(Number.parseFloat(taskCards[0].border) > 0);
    assert.ok(taskCards[1].top > taskCards[0].bottom);
    assert.ok(taskCards[2].top > taskCards[1].bottom);
    await capture(settingsWindow, "plugin-runtime-tasks");
    await settingsWindow.webContents.executeJavaScript("document.getElementById('exit-application-btn').click()");
    await waitFor(settingsWindow, "!document.getElementById('delete-confirm-dialog').hidden");
    assert.equal(await settingsWindow.webContents.executeJavaScript("document.activeElement.id"), "delete-confirm-cancel");
    assert.ok(await settingsWindow.webContents.executeJavaScript("document.querySelector('#delete-confirm-dialog svg')"));
    assert.match(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-message').textContent"), /当前有正在进行的后台任务/);
    assert.equal(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-ok').textContent"), "退出");
    await capture(settingsWindow, "plugin-runtime-exit-warning");
    await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-cancel').click()");
    assert.equal(exited, false);
    // 1. 查询失败和超时都进入同一个退出确认，取消保留应用，默认焦点仍为取消。
    for (const mode of ["error", "timeout"]) {
      taskSummaryMode = mode;
      await settingsWindow.webContents.executeJavaScript("document.getElementById('exit-application-btn').click()");
      await waitFor(settingsWindow, "!document.getElementById('delete-confirm-dialog').hidden");
      assert.match(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-dialog').textContent"), /退出 Ayaya/);
      assert.equal(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-message').textContent"),
        "当前后台任务状态查询出错，退出前请人工确认后台任务情况。");
      assert.equal(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-ok').textContent"), "退出");
      assert.equal(await settingsWindow.webContents.executeJavaScript("document.activeElement.id"), "delete-confirm-cancel");
      await capture(settingsWindow, `plugin-runtime-exit-${mode}`);
      if (mode === "timeout") assert.equal(taskSummaryAborted, true);
      await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-cancel').click()");
      assert.equal(exited, false);
    }
    // 2. 查询报错后用户确认，仍将真实退出请求送达主进程。
    taskSummaryMode = "error";
    await settingsWindow.webContents.executeJavaScript("document.getElementById('exit-application-btn').click()");
    await waitFor(settingsWindow, "!document.getElementById('delete-confirm-dialog').hidden");
    await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-ok').click()");
    await pause(100);
    assert.equal(exited, true);
    fs.writeFileSync(path.join(output, "plugin-runtime-results.json"), JSON.stringify({ passed: true, isolated, errors, requests }, null, 2));
    console.log("插件运行时 Electron 专项验证通过");
    return;
  }
  if (process.argv.includes("--composer-check")) {
    const layout = await chat.webContents.executeJavaScript(`({
      hint: Boolean(document.querySelector('.composer-hint')),
      sizes: ['chat-input', 'image-btn', 'send-btn'].map(/** 读取输入框与相邻按钮的实际高度。 */ id => document.getElementById(id).getBoundingClientRect().height)
    })`);
    console.log(JSON.stringify(layout));
    assert.equal(layout.hint, false, "快捷键提示尚未移除");
    assert.deepEqual(layout.sizes, [40, 40, 40], "输入框与两侧按钮应等高");
    const expanded = await chat.webContents.executeJavaScript(`(() => {
      const input = document.getElementById('chat-input'); input.value = '第一行\\n第二行\\n第三行';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return ['chat-input', 'image-btn', 'send-btn'].map(/** 多行草稿也保持同一行控件等高。 */ id => document.getElementById(id).getBoundingClientRect().height);
    })()`);
    assert.ok(expanded[0] > 40); assert.equal(expanded[0], expanded[1]); assert.equal(expanded[0], expanded[2]);
    console.log(JSON.stringify({ expanded }));
    await capture(chat, "chat-composer-multiline");
    await chat.webContents.executeJavaScript("document.getElementById('chat-input').value = ''; document.getElementById('chat-input').dispatchEvent(new Event('input', { bubbles: true }));");
    await capture(chat, "chat-composer");
    return;
  }
  if (process.argv.includes("--native-drag")) {
    const { verifyNativeChatDrag } = await import("./verify-native-chat-drag.mjs");
    await verifyNativeChatDrag({ chat, pet, output: path.join(uiRoot, ".test-dist", "native-drag") });
    return;
  }
  if (restoreCheck) {
    // 1. 订阅可先于窗口首次绘制就绪；等待 ready-to-show 的真实显示结果，不主动调用 show。
    for (let attempt = 0; attempt < 50 && !chat.isVisible(); attempt++) await pause(100);
    assert.equal(chat.isMinimized(), false); assert.equal(chat.isVisible(), true);
    assert.equal(chat.isAlwaysOnTop(), true);
    const actual = chat.getContentBounds();
    for (const key of ["x", "y", "width", "height"]) assert.ok(Math.abs(actual[key] - originalSavedBounds[key]) <= 1);
    const savedAfterShow = JSON.parse(fs.readFileSync(path.join(isolated, "frontend_settings.json"), "utf8"));
    assert.deepEqual(savedAfterShow.chat_window_bounds, originalSavedBounds);
    fs.writeFileSync(path.join(output, "restart.json"), JSON.stringify({ isolated, originalSavedBounds, bounds: chat.getContentBounds(), pinned: true, minimized: false }, null, 2));
    console.log("重启验证通过：恢复位置尺寸与置顶，窗口正常显示。");
    return;
  }
  await pet.webContents.executeJavaScript("window.desktopPetApi.onExpressionRequest(/** 记录跨窗口收到的表情标签。 */ label => { window.receivedExpression = label; }); void 0;");
  assert.equal(await pet.webContents.executeJavaScript("document.querySelector('#chat-form') === null"), true);
  assert.equal(await chat.webContents.executeJavaScript("document.querySelector('canvas') === null"), true);
  await capture(chat, "chat");
  assert.equal(subscriptions, 1);

  // 1. 移动、缩放和置顶不会改变角色窗口，也不会重建聊天订阅。
  const originalPet = pet.getBounds();
  const area = screen.getPrimaryDisplay().workArea;
  chat.setContentBounds({ x: area.x + 50, y: area.y + 50, width: 620, height: 680 });
  await chat.webContents.executeJavaScript("document.getElementById('chat-pin-btn').click()");
  await waitFor(chat, "document.getElementById('chat-pin-btn').getAttribute('aria-pressed') === 'true'");
  assert.equal(chat.isAlwaysOnTop(), true);
  await chat.webContents.executeJavaScript("document.getElementById('chat-input').value = '最小化前的草稿'; document.getElementById('chat-input').dispatchEvent(new Event('input'))");
  await chat.webContents.executeJavaScript(`
    const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0ioAAAAASUVORK5CYII=';
    const bytes = Uint8Array.from(atob(png), /** 把固定测试图片转换为文件字节。 */ c => c.charCodeAt(0));
    const transfer = new DataTransfer(); transfer.items.add(new File([bytes], 'test.png', { type: 'image/png' }));
    document.getElementById('chat-input').dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData: transfer }));
    document.getElementById('chat-form').dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer }));
  `);
  await waitFor(chat, "document.querySelectorAll('.image-preview-item').length === 2");
  chat.close(); await pause(250);
  assert.equal(chat.isDestroyed(), false); assert.equal(chat.isMinimized(), true);
  screen.emit("display-metrics-changed", {}, screen.getPrimaryDisplay(), ["workArea"]);
  assert.equal(chat.isMinimized(), true);
  assert.deepEqual(pet.getBounds(), originalPet);
  emit(event("run_started", {}, { run_id: "run-demo" }));
  emit(event("text_chunk", { value: "<开心>这条回复是在最小化期间收到的。" }, { run_id: "run-demo" }));
  await pause(800);
  assert.equal(chat.isMinimized(), true); assert.match(chat.getTitle(), /有新消息/);
  assert.equal(await pet.webContents.executeJavaScript("window.receivedExpression"), "开心");
  emit(event("notice", { plugin_name: "game_helper", text: "发现两名敌人，正在观察战斗状态。" }));
  await waitFor(chat, "document.querySelector('.plugin-notice')?.textContent.includes('发现两名敌人')");
  await capture(chat, "plugin-runtime-notice");
  history.push({ id: "ai-2", role: "ai", content: "这条回复是在最小化期间收到的。", timestamp: "2026-10-11T04:00:02Z" });

  // 2. 审批不自动恢复，重复事件只闪烁一次，真正解决后才清除待审批。
  const flashes = [];
  const originalFlash = chat.flashFrame.bind(chat);
  chat.flashFrame = /**
   * 记录原生提醒调用，仍执行实际任务栏闪烁。
   * Args:
   *     enabled (boolean): 是否闪烁。
   */
  (enabled) => { flashes.push(enabled); originalFlash(enabled); };
  const approval = event("interrupt", { value: {
    type: "authorization_request", request_id: "approval-demo", title: "工具调用确认", message: "测试工具需要你批准后继续。", allow_steer: true,
  } }, { run_id: "run-demo", request_id: "approval-demo" });
  emit(approval); emit(approval);
  await pause(400);
  assert.equal(chat.isMinimized(), true); assert.match(chat.getTitle(), /等待审批/);
  assert.equal(flashes.filter(/**
   * 只计数启用闪烁的调用。
   * Args:
   *     value (boolean): 闪烁状态。
   * Returns:
   *     boolean: 本次启动闪烁时为 true。
   */
  (value) => value).length, 1);
  chat.restore(); chat.show(); chat.focus();
  await waitFor(chat, "!document.getElementById('screenshot-confirm-dialog').hidden");
  await capture(chat, "approval");
  await chat.webContents.executeJavaScript("document.getElementById('screenshot-deny-btn').focus()");
  chat.webContents.sendInputEvent({ type: "keyDown", keyCode: "Tab", modifiers: ["shift"] });
  chat.webContents.sendInputEvent({ type: "keyUp", keyCode: "Tab", modifiers: ["shift"] });
  await waitFor(chat, "document.activeElement.id === 'screenshot-allow-btn'");
  emit(event("authorization_resolved", {}, { request_id: "approval-demo" }));
  await waitFor(chat, "document.getElementById('screenshot-confirm-dialog').hidden");
  assert.equal(await chat.webContents.executeJavaScript("document.getElementById('chat-input').value"), "最小化前的草稿");
  assert.equal(await chat.webContents.executeJavaScript("document.querySelectorAll('.image-preview-item').length"), 2);
  await capture(chat, "chat-with-attachments");
  assert.equal(commands.length, 0);
  // 2.1. 本地点击后只提交一次决定；任务栏提醒必须由后端处理事件清除。
  emit(event("interrupt", { value: { type: "authorization_request", request_id: "approval-local", message: "验证本地批准后的提醒恢复。" } },
    { run_id: "run-demo", request_id: "approval-local" }));
  await waitFor(chat, "!document.getElementById('screenshot-confirm-dialog').hidden");
  await chat.webContents.executeJavaScript("document.getElementById('screenshot-allow-btn').click()");
  await pause(100);
  assert.equal(commands.length, 1); assert.equal(commands[0].kind, "resolve_authorization");
  assert.match(chat.getTitle(), /等待审批/);
  emit(event("authorization_resolved", {}, { request_id: "approval-local" }));
  await pause(100); assert.doesNotMatch(chat.getTitle(), /等待审批/);
  emit(event("run_completed", {}, { run_id: "run-demo" }));
  await waitFor(chat, "!document.getElementById('send-btn').disabled");
  await chat.webContents.executeJavaScript("document.getElementById('chat-input').dispatchEvent(new CompositionEvent('compositionstart')); document.getElementById('chat-form').requestSubmit()");
  await pause(100); assert.equal(commands.length, 1);
  await chat.webContents.executeJavaScript("document.getElementById('chat-input').dispatchEvent(new CompositionEvent('compositionend')); document.getElementById('chat-input').focus()");
  chat.webContents.sendInputEvent({ type: "keyDown", keyCode: "Return" });
  chat.webContents.sendInputEvent({ type: "keyUp", keyCode: "Return" });
  await waitFor(chat, "document.getElementById('chat-input').value === '' && document.querySelectorAll('.image-preview-item').length === 0");
  assert.equal(commands.length, 2); assert.equal(commands[1].kind, "human_turn");
  assert.equal(commands[1].payload.images.length, 2);
  await chat.webContents.executeJavaScript("document.getElementById('settings-btn').click()");
  const settingsWindow = windows.getSettingsWindow();
  await waitFor(settingsWindow, "document.getElementById('exit-application-btn')?.querySelector('svg') && document.querySelector('.model-item')");
  const sizes = await settingsWindow.webContents.executeJavaScript("['open-log-window-btn','exit-application-btn'].map(id=>{const r=document.getElementById(id).getBoundingClientRect();return [r.width,r.height]})");
  assert.deepEqual(sizes[0], sizes[1]);
  await capture(settingsWindow, "settings");
  for (const page of ["llm", "motion", "memory", "contextStrategy", "tools", "mcp", "channels", "tasks"]) {
    await settingsWindow.webContents.executeJavaScript(`document.querySelector('[data-page="${page}"].settings-tab').click()`);
    await pause(150);
    if (page === "tasks") {
      // 1. 原生窗口必须真实显示工具来源；旧任务保留未知说明，不能把所有记录写成终端任务。
      await waitFor(settingsWindow, "document.getElementById('background-tasks-list').textContent.includes('[exec_command]')");
      const taskText = await settingsWindow.webContents.executeJavaScript("document.getElementById('background-tasks-list').textContent");
      assert.match(taskText, /\[other_tool\]/);
      assert.match(taskText, /\[memory\]/);
      await capture(settingsWindow, "settings-tasks-running");
      // 2. 使用真实 preload 读取输出，再刷新完成状态，工具名和输出详情都必须保留。
      await settingsWindow.webContents.executeJavaScript("document.querySelector('.background-task-row button').click()");
      await waitFor(settingsWindow, "document.querySelector('.background-task-card .background-task-output').textContent.includes('完整终端输出')");
      demoTaskStatus = "completed";
      await settingsWindow.webContents.executeJavaScript("document.getElementById('refresh-background-tasks').click()");
      await waitFor(settingsWindow, "document.querySelector('.background-task-row').textContent.includes('已完成')");
    }
    await capture(settingsWindow, `settings-${page}`);
  }
  await settingsWindow.webContents.executeJavaScript("document.getElementById('exit-application-btn').click()");
  await waitFor(settingsWindow, "!document.getElementById('delete-confirm-dialog').hidden");
  assert.match(await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-dialog').textContent"), /当前有正在进行的后台任务/);
  await capture(settingsWindow, "exit-confirmation");
  await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-cancel').click()");
  assert.equal(exited, false);
  await settingsWindow.webContents.executeJavaScript("document.getElementById('exit-application-btn').click()");
  await settingsWindow.webContents.executeJavaScript("document.getElementById('delete-confirm-ok').click()");
  await pause(100); assert.equal(exited, true);
  const logs = windows.openLogWindow();
  await waitFor(logs, "document.getElementById('log-min-btn')?.querySelector('svg')");
  await capture(logs, "logs");
  const pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0ioAAAAASUVORK5CYII=";
  windows.openImagePreviewWindow(pixel);
  await pause(200);
  const preview = BrowserWindow.getAllWindows().find(/**
   * 按实际页面地址寻找图片预览窗口。
   * Args:
   *     win (BrowserWindow): 当前应用窗口。
   * Returns:
   *     boolean: 加载图片预览页面时为 true。
   */
  (win) => win.webContents.getURL().includes("image-preview.html"));
  assert.ok(preview);
  await waitFor(preview, "document.getElementById('preview-image').complete && document.getElementById('preview-image').naturalWidth > 0");
  await capture(preview, "image-preview");
  preview.close(); logs.close(); settingsWindow.close();
  assert.equal(chat.isDestroyed(), false);
  await pause(250);
  const saved = JSON.parse(fs.readFileSync(path.join(isolated, "frontend_settings.json"), "utf8"));
  assert.equal(saved.chat_window_pinned, true);
  assert.deepEqual(saved.chat_window_bounds, chat.getContentBounds());
  assert.equal(subscriptions, 1);
  // 3. 检查实际模型与页面脚本，外部字体连接失败单独报告。
  const modelStatus = await chat.webContents.executeJavaScript("document.getElementById('model-status').textContent");
  assert.equal(modelStatus, "");
  assert.equal(errors.filter(/**
   * 忽略字体网络环境错误，其他页面错误都会使测试失败。
   * Args:
   *     message (string): 页面错误日志。
   * Returns:
   *     boolean: 应计入页面错误时为 true。
   */
  (message) => !message.includes("fonts.googleapis") && !message.includes("fonts.gstatic")).length, 0, errors.join("\n"));
  fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ subscriptions, commands, requests, screenshots: fs.readdirSync(output).filter(/**
   * 收集本次截图文件名。
   * Args:
   *     name (string): 输出目录文件名。
   * Returns:
   *     boolean: PNG 截图时为 true。
   */
  (name) => name.endsWith(".png")), isolated, errors }, null, 2));
  windows.beginWindowShutdown(); chat.close();
  await pause(200);
  assert.equal(chat.isDestroyed(), true);
  console.log(`桌面验证通过：截图保存在 ${output}`);
} catch (error) {
  console.error(error);
  fs.writeFileSync(path.join(output, "failure.json"), JSON.stringify({ error: String(error), errors, requests, isolated, windowEvents,
    windows: BrowserWindow.getAllWindows().map(/**
     * 记录窗口的原生位置、可见状态和页面地址以定位失败。
     * Args:
     *     win (BrowserWindow): 当前存活窗口。
     * Returns:
     *     object: 当前窗口诊断状态。
     */
    (win) => ({ id: win.id, visible: win.isVisible(), minimized: win.isMinimized(), bounds: win.getBounds(), url: win.webContents.getURL() })),
  }, null, 2));
  process.exitCode = 1;
} finally {
  if (developmentServer) await developmentServer.close();
  stopDesktopSession(); windows.beginWindowShutdown();
  for (const win of BrowserWindow.getAllWindows()) win.destroy();
  app.exit(process.exitCode ?? 0);
}
};
void run();
