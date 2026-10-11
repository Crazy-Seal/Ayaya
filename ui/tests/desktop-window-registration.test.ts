import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DesktopWindowRegistrationController } from "../electron/desktop-window-registration.js";

test("只登记桌宠聊天的精确句柄，窗口变化按完整快照串行发送", /** 验证窗口身份登记不传动作或结果。 */
async () => {
  const requests: unknown[] = [];
  let chat = "12";
  /** 提供最小原生窗口身份，不创建真实 Electron 窗口。 */
  const window = (value: string) => ({
    /** 返回测试窗口仍然存活。 */ isDestroyed: () => false,
    /** 返回测试窗口没有最小化。 */ isMinimized: () => false,
    /** 返回测试窗口当前可见。 */ isVisible: () => true,
    /** 原生句柄使用完整指针宽度。 */ getNativeWindowHandle: () => { const buffer = Buffer.alloc(8); buffer.writeBigUInt64LE(BigInt(value)); return buffer; },
    /** 恢复兜底在本用例不应执行。 */ showInactive: () => { throw new Error("不应恢复"); },
  });
  const registry = new DesktopWindowRegistrationController({
    ownerPid: 100,
    /** 返回当前存活的两个指定角色。 */ getWindows: () => ({ pet: window("11"), chat: window(chat) }),
    /** 测试应用仍然接受操作。 */ isShuttingDown: () => false,
    /**
     * 通过公共请求观察完整快照。
     * Args:
     *     _path (string): 仅使用 /internal/desktop-windows 的登记路径。
     *     init (RequestInit): 当前完整窗口身份和取消信号。
     * Returns:
     *     Promise<Response>: 后端确认的身份及隔离恢复路径。
     */ fetchBackend: async (_path, init) => {
      requests.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ accepted: true, window_restore_path: "unused" }));
    },
    /**
     * 任何失败都会使测试失败。
     * Args:
     *     _message (string): 当前中文错误说明。
     *     error (unknown): 捕获的登记或恢复异常。
     */ logError: (_message, error) => { throw error; },
  });
  await registry.connect();
  chat = "14";
  await registry.refresh();
  assert.deepEqual(requests, [
    { owner_pid: 100, revision: 1, windows: { pet: "11", chat: "12" } },
    { owner_pid: 100, revision: 2, windows: { pet: "11", chat: "14" } },
  ]);
  registry.disconnect();
});

test("重连的迟到响应不覆盖新恢复路径，退出后不恢复任何窗口", /** 验证连接代次与单向退出门禁。 */
async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "ayaya-registry-"));
  const restorePath = path.join(directory, "restore.json");
  let shuttingDown = false, restored = 0, release: (value: Response) => void = () => {};
  let calls = 0;
  const pet = {
    /** 返回当前对象存活。 */ isDestroyed: () => false,
    /** 返回当前对象未最小化。 */ isMinimized: () => false,
    /** 返回当前窗口被后端隐藏。 */ isVisible: () => false,
    /** 返回指针宽度的测试句柄。 */ getNativeWindowHandle: () => { const value = Buffer.alloc(8); value.writeBigUInt64LE(11n); return value; },
    /** 记录恢复次数，不模拟取得焦点。 */ showInactive: () => { restored++; },
  };
  const registry = new DesktopWindowRegistrationController({
    ownerPid: 100,
    /** 完整快照只包含两个角色。 */ getWindows: () => ({ pet, chat: null }),
    /** 读取同一退出状态。 */ isShuttingDown: () => shuttingDown,
    /** 首次请求故意迟到，新连接先返回真实隔离恢复文件。 */ fetchBackend: async () => {
      calls++;
      if (calls === 1) return new Promise<Response>(/** 测试控制旧响应到达时机。 */ resolve => { release = resolve; });
      return new Response(JSON.stringify({ accepted: true, window_restore_path: restorePath }));
    },
    /** 不允许隐藏恢复错误。 */ logError: (_message, error) => { throw error; },
  });
  try {
    const oldConnection = registry.connect();
    await registry.connect();
    release(new Response(JSON.stringify({ accepted: true, window_restore_path: "wrong-old-path" })));
    await oldConnection;
    fs.writeFileSync(restorePath, JSON.stringify({ owner_pid: 100, windows: [{ role: "pet", hwnd: "11" }] }));
    registry.recoverWindows(); assert.equal(restored, 1); assert.equal(fs.existsSync(restorePath), false);
    fs.writeFileSync(restorePath, JSON.stringify({ owner_pid: 100, windows: [{ role: "pet", hwnd: "11" }] }));
    shuttingDown = true; registry.recoverWindows(); await registry.refresh();
    assert.equal(restored, 1); assert.equal(calls, 2); assert.equal(fs.existsSync(restorePath), true);
    await assert.rejects(registry.ensureReady());
  } finally { registry.disconnect(); fs.rmSync(directory, { recursive: true }); }
});

test("意外退出仅恢复匹配存活窗口，最小化聊天与其他角色保持原状", /** 验证恢复日志不会扩大隐藏窗口范围。 */
async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "ayaya-registry-"));
  const restorePath = path.join(directory, "restore.json");
  const restored: string[] = [];
  /**
   * 提供只读窗口身份与最小化状态。
   * Args:
   *     handle (bigint): 精确 HWND。
   *     minimized (boolean): 用户最小化状态。
   */
  const window = (handle: bigint, minimized = false) => ({
    /** 模拟存活窗口。 */ isDestroyed: () => false,
    /** 读取用户最小化状态。 */ isMinimized: () => minimized,
    /** 模拟临时隐藏窗口。 */ isVisible: () => false,
    /** 返回精确原生句柄。 */ getNativeWindowHandle: () => { const buffer = Buffer.alloc(8); buffer.writeBigUInt64LE(handle); return buffer; },
    /** 恢复只记录指定角色。 */ showInactive: () => { restored.push(String(handle)); },
  });
  const registry = new DesktopWindowRegistrationController({ ownerPid: 100,
    /** 返回当前明确角色。 */ getWindows: () => ({ pet: window(11n), chat: window(12n, true) }),
    /** 恢复期间应用没有退出。 */ isShuttingDown: () => false,
    /** 返回后端实际恢复日志路径。 */ fetchBackend: async () => new Response(JSON.stringify({ accepted: true, window_restore_path: restorePath })),
    /** 恢复失败使当前用例失败。 */ logError: (_message, error) => { throw error; },
  });
  try {
    await registry.connect();
    fs.writeFileSync(restorePath, JSON.stringify({ owner_pid: 100, windows: [
      { role: "pet", hwnd: "999" }, { role: "chat", hwnd: "12" }, { role: "settings", hwnd: "11" }, { role: "pet", hwnd: "11" },
    ] }));
    registry.recoverWindows(); assert.deepEqual(restored, ["11"]);
  } finally { registry.disconnect(); fs.rmSync(directory, { recursive: true }); }
});

test("SSE 断线只在本机后端死亡后恢复，临时断线不弹出窗口", /** 验证外部后端恢复使用本机死亡证据。 */
async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "ayaya-registry-"));
  const restorePath = path.join(directory, "restore.json");
  let alive = true, restored = 0, notified = 0;
  const pet = {
    /** 当前窗口仍存活。 */ isDestroyed: () => false,
    /** 用户没有最小化。 */ isMinimized: () => false,
    /** 后端当前隐藏窗口。 */ isVisible: () => false,
    /** 提供精确身份。 */ getNativeWindowHandle: () => { const value = Buffer.alloc(8); value.writeBigUInt64LE(11n); return value; },
    /** 记录恢复，不激活窗口。 */ showInactive: () => { restored++; },
  };
  const registry = new DesktopWindowRegistrationController({ ownerPid: 100,
    /** 只返回明确角色。 */ getWindows: () => ({ pet, chat: null }),
    /** 此用例应用保持运行。 */ isShuttingDown: () => false,
    /** 模拟同一个本机后端存活状态。 */ isBackendAlive: pid => { assert.equal(pid, 200); return alive; },
    /** 通知发生在恢复之后。 */ onBackendExited: () => { assert.equal(restored, 1); notified++; },
    /** 返回隔离恢复记录路径。 */ fetchBackend: async () => new Response(JSON.stringify({ accepted: true, window_restore_path: restorePath })),
    /** 禁止吞掉恢复错误。 */ logError: (_message, error) => { throw error; },
  });
  try {
    await registry.connect();
    fs.writeFileSync(restorePath, JSON.stringify({ owner_pid: 100, backend_pid: 200, windows: [{ role: "pet", hwnd: "11" }] }));
    registry.recoverIfBackendExited(); assert.equal(restored, 0); assert.equal(notified, 0);
    alive = false; registry.recoverIfBackendExited(); assert.equal(restored, 1); assert.equal(notified, 1);
    await assert.rejects(registry.ensureReady());
  } finally { registry.disconnect(); fs.rmSync(directory, { recursive: true }); }
});
