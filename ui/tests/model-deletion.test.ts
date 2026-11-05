import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { test } from "node:test";
import ts from "typescript";
import { IpcChannel } from "../ipc-channels.js";

test("删除角色先保存并通知窗口，目录清理阻塞或失败均不恢复角色", /** 验证真实 IPC 回调的持久化、窗口通知与异步目录释放顺序。 */
async () => {
  const steps: string[] = [];
  const builtin = { id: "builtin-hiyori", sessionId: "builtin", source: "builtin", name: "默认角色" };
  const custom = { id: "custom", sessionId: "parent", source: "imported", rootDir: "custom", name: "待删除角色" };
  const config = { models: [builtin, custom], activeModelId: custom.id };
  const callbacks = new Map<string, (event: unknown, id: string) => Promise<unknown>>();
  let rejectRemoval: (error: Error) => void = /** 清理实际开始时再绑定失败回调。 */ () => {};
  const removal = new Promise<void>(/** 保持目录删除未完成。 */ (_resolve, reject) => { rejectRemoval = reject; });
  const window = {
    isDestroyed: /** 测试窗口保持打开。 */ () => false,
    webContents: { send: /** 记录窗口接收到切换后的活动角色。 */ (_channel: string, payload: { id: string }) => {
      assert.equal(payload.id, builtin.id);
      steps.push("notify");
    } },
  };
  const mocks: Record<string, unknown> = {
    "../ipc-channels.js": { IpcChannel },
    "node:fs": { promises: { rm: /** 记录异步删除开始并等待外部放行。 */ () => { steps.push("remove"); return removal; } } },
    "./ipc-security.js": { createTrustedIpcRegistrar: /** 记录可信 IPC 注册结果。 */ () => ({
      handle: /** 保存真实回调，供模拟页面调用。 */ (name: string, callback: (event: unknown, id: string) => Promise<unknown>) => callbacks.set(name, callback),
      on: /** 本用例只调用有返回值的删除请求。 */ () => {},
    }) },
    "./model-manager.js": {
      loadModelConfig: /** 读取真实回调会修改的角色配置。 */ () => config,
      saveModelConfig: /** 保存后用户列表必须已无目标角色。 */ () => { assert.deepEqual(config.models, [builtin]); steps.push("save"); },
      getActiveModelRecord: /** 活动角色应已切换。 */ () => builtin,
      resolveModelUrl: /** 提供窗口展示所需地址。 */ () => "model-url",
      resolveRootDirAbsolute: /** 测试文件系统替身使用此路径。 */ (directory: string) => directory,
    },
    "./chat-settings.js": {
      deleteChatSettingsBySessionId: /** 模拟已完成的后端配置删除。 */ async () => { steps.push("backend"); },
      clearChatSettingsCache: /** 记录角色切换后的缓存失效。 */ () => { steps.push("cache"); },
    },
    "./window-manager.js": {
      getTrustedRendererPolicy: /** 返回 IPC 测试权限。 */ () => ({}),
      getMainWindow: /** 提供桌面窗口。 */ () => window,
      getChatWindow: /** 提供聊天窗口。 */ () => window,
      getSettingsWindow: /** 提供设置窗口。 */ () => window,
      deliverModelError: /** 本用例无角色装配错误。 */ () => {},
    },
    "./ai-names.js": { forgetAiName: /** 记录被删除角色的名称缓存清理。 */ () => { steps.push("forget"); } },
  };
  const exports: { registerIpcHandlers?: (options: { requestApplicationExit: () => void }) => void } = {};
  const source = readFileSync(new URL("../../electron/ipc-handlers.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true } }).outputText;
  runInNewContext(javascript, {
    exports,
    require: /** 只替换外部 Electron、网络与文件系统，执行完整生产注册函数。 */ (name: string) => mocks[name] ?? {},
    console: { error: /** 清理失败只有日志，不撤销角色删除。 */ () => { steps.push("error"); } },
  });
  exports.registerIpcHandlers!({ requestApplicationExit: /** 本用例不退出应用。 */ () => {} });
  const result = await callbacks.get(IpcChannel.DELETE_MODEL)!(null, custom.id);
  assert.equal((result as { activeModelId: string }).activeModelId, builtin.id);
  assert.equal(config.activeModelId, builtin.id);
  assert.deepEqual(steps, ["backend", "forget", "save", "cache", "notify", "notify", "notify", "remove"]);
  rejectRemoval(new Error("测试目录被占用"));
  await new Promise<void>(/** 等待目录失败处理完成。 */ (resolve) => setImmediate(resolve));
  assert.equal(steps.at(-1), "error");
  assert.deepEqual(config.models, [builtin]);
});
