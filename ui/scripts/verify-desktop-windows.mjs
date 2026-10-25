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
import { spawn } from "node:child_process";

const uiRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.join(uiRoot, ".test-dist", "desktop-smoke");
const restoreCheck = process.argv.includes("--restore-check");
const focusedCheck = process.argv.includes("--native-drag") || process.argv.includes("--composer-check");
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
  if (route === "/internal/desktop-windows") return json({ accepted: true,
    window_restore_path: path.join(isolated, "desktop", "window_restore.json") });
  if (route === "/desktop_settings") return json({ code: 200, data: init.method === "PUT"
    ? JSON.parse(init.body) : { hide_on_screenshot: true } });
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
        emit(event("session_snapshot", { phase: "idle", queued_inputs: [], tasks: [], interrupt: null }));
        init.signal?.addEventListener("abort", /** 取消订阅只关闭测试流，不访问任何后台进程。 */
        () => { try { controller.close(); } catch { /* 流可能已经关闭。 */ } }, { once: true });
      },
    }), { headers: { "Content-Type": "text/event-stream" } });
  }
  if (route.startsWith("/chat_history")) return json({ code: 200, data: history });
  if (route === "/chat_settings" && init.method === "POST") { settings = JSON.parse(init.body); return json({ code: 200, data: settings }); }
  if (route.startsWith("/chat_settings")) return json({ code: 200, data: settings });
  if (route === "/agent/commands") {
    const command = JSON.parse(init.body); commands.push(command);
    return json({ accepted: true, input_sequence: commands.length });
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
      error: null, plugin_name: null, lifetime: "independent", notify_agent: true };
    return json({ tasks: [
      { ...base, task_id: "task-demo", tool_name: "exec_command", command: "整理项目资料", title: "整理项目资料", pid: 123,
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
  if (route.startsWith("/tool-permissions")) return json({ code: 200, data: { tools: [] } });
  if (route === "/plugins") return json({ code: 200, data: { plugins: [] } });
  if (route.startsWith("/mcp/")) return json({ code: 200, data: [] });
  throw new Error(`桌面验证禁止未知请求：${route}`);
};

const windows = await import("../dist-electron/electron/window-manager.js");
const { registerIpcHandlers } = await import("../dist-electron/electron/ipc-handlers.js");
const { configureBackendClient } = await import("../dist-electron/electron/backend-client.js");
const { stopDesktopSession } = await import("../dist-electron/electron/interaction-adapter.js");
const { desktopWindowRegistry } = await import("../dist-electron/electron/desktop-window-runtime.js");
const { desktopWindowHandle } = await import("../dist-electron/electron/desktop-window-registration.js");
const { createBackendPythonResolver, buildBackendPythonEnvironment } = await import("../dist-electron/electron/backend-python.js");
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

/**
 * 启动真实 Python 桌面模块，Electron 保持事件循环以接收异步显隐。
 * Args:
 *     pet (BrowserWindow): 唯一桌宠窗口。
 *     chat (BrowserWindow): 唯一聊天窗口。
 *     extras (object): 仅用于断言未隐藏窗口的角色与句柄。
 *     options (object): 是否隐藏和独立输入窗口句柄。
 * Returns:
 *     Promise<object>: 捕获时真实显隐状态及输入执行结果。
 */
const probeDesktop = async (pet, chat, extras = {}, options = {}) => {
  const resolved = await createBackendPythonResolver().resolve();
  const handles = { pet: desktopWindowHandle(pet), chat: desktopWindowHandle(chat) };
  return new Promise(/** 等真实 Python 清理并退出后才允许下一个桌面场景。 */ (resolve, reject) => {
    const child = spawn(resolved.executable, ["-B", path.join(uiRoot, "scripts", "backend-desktop-probe.py")], {
      cwd: path.dirname(uiRoot), windowsHide: true, env: buildBackendPythonEnvironment(process.env, resolved, process.platform),
    });
    let stdout = "", stderr = "";
    child.stdout.on("data", /** 收集小型验证结果，不输出屏幕图片。 */ value => { stdout += value; });
    child.stderr.on("data", /** 保存 Python 失败原因。 */ value => { stderr += value; });
    child.once("error", reject);
    child.once("close", /** 正常退出已完成清理；显式崩溃场景保留记录由 Electron 恢复。 */ code => {
      if (code !== 0 && !(options.crash && code === 23)) reject(new Error(`原生桌面验证失败：${stderr}`));
      else { try { resolve(JSON.parse(stdout)); } catch (error) { reject(new Error(`${error}: ${stdout}`)); } }
    });
    child.stdin.end(JSON.stringify({ owner_pid: process.pid, windows: handles, observed: { ...handles, ...extras }, ...options }));
  });
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
  await desktopWindowRegistry.connect();
  await waitFor(chat, "document.querySelectorAll('.message-item').length >= 2 && !document.getElementById('send-btn').disabled");
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
  const minimizedProbe = await probeDesktop(pet, chat);
  assert.equal(minimizedProbe.observed[0].pet.visible, false);
  assert.equal(minimizedProbe.observed[0].chat.minimized, true);
  assert.equal(chat.isMinimized(), true);
  // 临时显隐行为由后端桌面模块和隔离 Win32 验证覆盖，聊天最小化仍保留前端验证。
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
  for (const page of ["llm", "motion", "memory", "contextStrategy", "tools", "mcp", "multimodal", "channels", "tasks"]) {
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
      await waitFor(settingsWindow, "document.getElementById('background-task-output').textContent.includes('完整终端输出')");
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
  // 3. 实际截图只隐藏两个登记窗口；位置、置顶、草稿和共享订阅保持。
  const beforeProbe = { pet: pet.getBounds(), chat: chat.getBounds(), pinned: chat.isAlwaysOnTop() };
  const extras = { settings: desktopWindowHandle(settingsWindow), logs: desktopWindowHandle(logs), preview: desktopWindowHandle(preview) };
  const nativeProbe = await probeDesktop(pet, chat, extras);
  assert.equal(nativeProbe.observed[0].pet.visible, false); assert.equal(nativeProbe.observed[0].chat.visible, false);
  for (const role of ["settings", "logs", "preview"]) assert.equal(nativeProbe.observed[0][role].visible, true);
  assert.equal(pet.isVisible(), true); assert.equal(chat.isVisible(), true);
  assert.deepEqual(pet.getBounds(), beforeProbe.pet); assert.deepEqual(chat.getBounds(), beforeProbe.chat);
  assert.equal(chat.isAlwaysOnTop(), beforeProbe.pinned); assert.equal(subscriptions, 1);
  const shownProbe = await probeDesktop(pet, chat, extras, { hide: false });
  assert.equal(shownProbe.observed[0].pet.visible, true); assert.equal(shownProbe.observed[0].chat.visible, true);
  // 3.1. 后端在真实隐藏期间直接死亡，Electron 按已登记身份及死亡 PID 恢复两个窗口。
  const focusBeforeCrash = BrowserWindow.getFocusedWindow()?.id;
  const crashProbe = await probeDesktop(pet, chat, extras, { crash: true });
  assert.equal(pet.isVisible(), false); assert.equal(chat.isVisible(), false);
  desktopWindowRegistry.recoverIfBackendExited();
  assert.equal(pet.isVisible(), true); assert.equal(chat.isVisible(), true);
  assert.equal(BrowserWindow.getFocusedWindow()?.id, focusBeforeCrash);
  assert.deepEqual(chat.getBounds(), beforeProbe.chat); assert.equal(chat.isAlwaysOnTop(), beforeProbe.pinned);
  assert.equal(subscriptions, 1);
  await desktopWindowRegistry.connect();
  // 3.2. 鼠标输入仅使用独立置顶测试窗口中心，绝不点击聊天或用户应用。
  const target = new BrowserWindow({ x: area.x + 150, y: area.y + 150, width: 400, height: 220,
    alwaysOnTop: true, frame: false, webPreferences: { contextIsolation: true, nodeIntegration: false } });
  await target.loadURL("data:text/html;charset=utf-8," + encodeURIComponent('<input id="probe" style="position:fixed;inset:0;width:100%;height:100%;font-size:20px">'));
  target.show(); target.focus(); await pause(200);
  const inputProbe = await probeDesktop(pet, chat, {}, { target: desktopWindowHandle(target) });
  assert.equal(inputProbe.status, "completed", inputProbe.error ?? "");
  assert.equal(await target.webContents.executeJavaScript("document.activeElement.id"), "probe");
  const chineseProbe = await probeDesktop(pet, chat, {}, { target: desktopWindowHandle(target), text: "后端中文输入验证" });
  if (chineseProbe.status === "not_started" && chineseProbe.error?.includes("剪贴板包含文件或无法可靠恢复的格式")) {
    assert.equal(await target.webContents.executeJavaScript("document.getElementById('probe').value"), "");
    console.log("真实中文粘贴受到当前未知剪贴板格式保护，未改变用户剪贴板；中文路径由隔离 adapter 测试覆盖。");
  } else {
    assert.equal(chineseProbe.status, "completed", chineseProbe.error ?? "");
    assert.equal(await target.webContents.executeJavaScript("document.getElementById('probe').value"), "后端中文输入验证");
  }
  target.destroy();
  fs.writeFileSync(path.join(output, "backend-desktop.json"), JSON.stringify({ nativeProbe, minimizedProbe, shownProbe, crashProbe, inputProbe, chineseProbe }, null, 2));
  preview.close(); logs.close(); settingsWindow.close();
  assert.equal(chat.isDestroyed(), false);
  await pause(250);
  const saved = JSON.parse(fs.readFileSync(path.join(isolated, "frontend_settings.json"), "utf8"));
  assert.equal(saved.chat_window_pinned, true);
  assert.deepEqual(saved.chat_window_bounds, chat.getContentBounds());
  assert.equal("hide_on_screenshot" in saved, false); assert.equal(subscriptions, 1);
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
  desktopWindowRegistry.disconnect();
  for (const win of BrowserWindow.getAllWindows()) win.destroy();
  app.exit(process.exitCode ?? 0);
}
};
void run();
