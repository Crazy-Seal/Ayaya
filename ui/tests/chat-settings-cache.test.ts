import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  clearChatSettingsCache,
  createEmptyChatSettings,
  fetchChatSettingsBySessionId,
  fetchChatHistoryLastN,
  getDefaultAgentPluginsForTest,
  getChatSettingsCache,
  runMcpMutationWithCacheInvalidation,
  updateChatSettings,
  updateChatSettingsCache,
} from "../electron/chat-settings.js";
import type { ChatSettingsData } from "../shared-types.js";
import { configureBackendClient } from "../electron/backend-client.js";

const originalFetch = globalThis.fetch;

test("工具完成历史查询传递终点并保留消息身份和图片", /**
 * 检查实际 HTTP 请求参数以及返回到 Renderer 的消息编号，避免刷新越过工具结果。
 */
async () => {
  const item = { id: "result:images", role: "AI", content: "", timestamp: "2026-10-29",
    images: ["screenshot/one.png"], };
  let requested = "";
  globalThis.fetch = /**
   * 记录历史请求地址，返回工具图片的完整展示项。
   * Args:
   *     input (RequestInfo | URL): 后端客户端生成的历史查询地址。
   */
    async (input) => { requested = String(input); return Response.json({ code: 200, data: [item] }); };
  const result = await fetchChatHistoryLastN("session", 100, "result/with space");
  const query = new URL(requested).searchParams;
  assert.equal(query.get("through_message_id"), "result/with space");
  assert.equal(query.get("n"), "100");
  assert.deepEqual(result, [item]);
});

const savedSettings: ChatSettingsData = {
  session_id: "saved-session",
  model_name: "saved-model",
  openai_api_key: "saved-key",
  openai_base_url: "http://saved.example/v1",
  temperature: 0.7,
  system_prompt: "saved prompt",
  tool_permissions: {},
  tool_permissions_schema_version: 1,
  mcp: { servers: {} },
  context_strategy: {
    compression_trigger_tokens_k: 100,
  },
};

configureBackendClient("t".repeat(43));

afterEach(/**
 * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
 */
() => {
  globalThis.fetch = originalFetch;
  clearChatSettingsCache();
});

test("PUT 失败时保留最后一次成功的 ChatSettings 缓存", /**
 * 验证PUT 失败时保留最后一次成功的 ChatSettings 缓存。
 */
async () => {
  updateChatSettingsCache(savedSettings);
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证PUT 失败时保留最后一次成功的 ChatSettings 缓存。
   */
  async () => new Response("backend unavailable", { status: 503 });

  const attemptedSettings: ChatSettingsData = {
    ...savedSettings,
    model_name: "unsaved-model",
  };

  await assert.rejects(updateChatSettings(attemptedSettings));
  assert.deepEqual(getChatSettingsCache(), savedSettings);
});

test("PUT 成功后更新 ChatSettings 缓存", /**
 * 验证PUT 成功后更新 ChatSettings 缓存。
 */
async () => {
  updateChatSettingsCache(savedSettings);
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证PUT 成功后更新 ChatSettings 缓存。
   */
  async () =>
    new Response(JSON.stringify({ code: 200, msg: "success", data: updatedSettings }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  const updatedSettings: ChatSettingsData = {
    ...savedSettings,
    model_name: "updated-model",
  };

  await updateChatSettings(updatedSettings);
  assert.deepEqual(getChatSettingsCache(), updatedSettings);
});

test("前端默认开启 memory 插件", /**
 * 验证新建配置保留记忆默认开启的行为。
 */
() => {
  const defaults = getDefaultAgentPluginsForTest();

  assert.equal(defaults.memory.enabled, true);
});

test("读取配置时保留记忆插件的关闭状态", /**
 * 后端返回用户禁用的记忆配置时，前端补齐默认参数但保留关闭状态。
 */
async () => {
  globalThis.fetch = /** 返回已经关闭记忆插件的会话设置。 */
  async () => new Response(JSON.stringify({ code: 200, msg: "success", data: {
    ...savedSettings,
    agent_plugins: { memory: { enabled: false, config: { enable_diary: false } } },
  } }), { status: 200, headers: { "Content-Type": "application/json" } });
  const settings = await fetchChatSettingsBySessionId("saved-session");
  assert.equal(settings.agent_plugins?.memory.enabled, false);
  assert.equal(settings.agent_plugins?.memory.config.enable_diary, false);
  assert.equal(settings.agent_plugins?.memory.config.long_term_memory_every_human_messages, 10);
});

test("新建模型默认使用 100K token 阈值与 2/2 截图策略", /**
 * 验证新建模型默认使用 100K token 阈值与 2/2 截图策略。
 */
async () => {
  const requestBodies: ChatSettingsData[] = [];
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，追加当前记录。
   *
   * 此定义用于：验证新建模型默认使用 100K token 阈值与 2/2 截图策略。
   *
   * Args:
   *     _input (URL | RequestInfo): 当前日志输入或配置输入框。
   *     init (RequestInit | undefined): 本次后端 HTTP 请求选项。
   */
  async (_input, init) => {
    const requestBody = JSON.parse(String(init?.body)) as ChatSettingsData;
    requestBodies.push(requestBody);
    return new Response(JSON.stringify({ code: 200, msg: "success", data: requestBody }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await createEmptyChatSettings("new-session");

  assert.equal(requestBodies[0]?.context_strategy.compression_trigger_tokens_k, 100);
});

test("读取旧配置时补齐 2/2 截图策略", /**
 * 验证读取旧配置时补齐 2/2 截图策略。
 */
async () => {
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证读取旧配置时补齐 2/2 截图策略。
   */
  async () =>
    new Response(JSON.stringify({
      code: 200,
      msg: "success",
      data: {
        ...savedSettings,
        context_strategy: { compression_trigger_tokens_k: 100 },
      },
    }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  const settings = await fetchChatSettingsBySessionId("legacy-session");

});

test("更新 ChatSettings 时不再发送旧记忆插件字段", /**
 * 验证更新 ChatSettings 时不再发送旧记忆插件字段。
 */
async () => {
  let requestBody: Record<string, unknown> | null = null;
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证更新 ChatSettings 时不再发送旧记忆插件字段。
   *
   * Args:
   *     _input (URL | RequestInfo): 当前日志输入或配置输入框。
   *     init (RequestInit | undefined): 本次后端 HTTP 请求选项。
   */
  async (_input, init) => {
    requestBody = JSON.parse(String(init?.body));
    return new Response(JSON.stringify({ code: 200, msg: "success", data: savedSettings }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  await updateChatSettings(savedSettings);

  assert.ok(requestBody);
  assert.equal(Object.hasOwn(requestBody, "memory" + "_plugins"), false);
});

test("更新 ChatSettings 时完整往返统一工具权限", /**
 * 验证更新 ChatSettings 时完整往返统一工具权限。
 */
async () => {
  let requestBody: Record<string, unknown> | null = null;
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证更新 ChatSettings 时完整往返统一工具权限。
   *
   * Args:
   *     _input (URL | RequestInfo): 当前日志输入或配置输入框。
   *     init (RequestInit | undefined): 本次后端 HTTP 请求选项。
   */
  async (_input, init) => {
    requestBody = JSON.parse(String(init?.body));
    const normalized = {
      ...settings,
      tool_permissions: {
        "mcp:filesystem:read_file": "ask",
        "mcp:filesystem:write_file": "ask",
      },
    } satisfies ChatSettingsData;
    return new Response(JSON.stringify({ code: 200, msg: "success", data: normalized }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  const settings: ChatSettingsData = {
    ...savedSettings,
    mcp: {
      servers: {
        filesystem: { enabled: true },
      },
    },
    tool_permissions: {
      "mcp:filesystem:read_file": "allow",
      "mcp:filesystem:write_file": "ask",
    },
  };

  await updateChatSettings(settings);

  assert.deepEqual((requestBody as Record<string, unknown> | null)?.mcp, settings.mcp);
  assert.equal(getChatSettingsCache()?.tool_permissions["mcp:filesystem:read_file"], "ask");
});

test("MCP 更新或删除成功后清除 ChatSettings 缓存", /**
 * 验证MCP 更新或删除成功后清除 ChatSettings 缓存。
 */
async () => {
  updateChatSettingsCache(savedSettings);

  const result = await runMcpMutationWithCacheInvalidation(/**
   * 由外层操作在对应处理阶段调用，返回本步骤预先指定的值。
   *
   * 此定义用于：验证MCP 更新或删除成功后清除 ChatSettings 缓存。
   */
  async () => "done");

  assert.equal(result, "done");
  assert.equal(getChatSettingsCache(), null);
});

test("MCP 更新或删除失败时保留 ChatSettings 缓存", /**
 * 验证MCP 更新或删除失败时保留 ChatSettings 缓存。
 */
async () => {
  updateChatSettingsCache(savedSettings);

  await assert.rejects(
    runMcpMutationWithCacheInvalidation(/**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证MCP 更新或删除失败时保留 ChatSettings 缓存。
     */
    async () => {
      throw new Error("failed");
    })
  );

  assert.deepEqual(getChatSettingsCache(), savedSettings);
});
