import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  clearChatSettingsCache,
  createEmptyChatSettings,
  getDefaultAgentPluginsForTest,
  getChatSettingsCache,
  runMcpMutationWithCacheInvalidation,
  updateChatSettings,
  updateChatSettingsCache,
} from "../electron/chat-settings.js";
import type { ChatSettingsData } from "../shared-types.js";
import { configureBackendClient } from "../electron/backend-client.js";

const originalFetch = globalThis.fetch;

const savedSettings: ChatSettingsData = {
  session_id: "saved-session",
  model_name: "saved-model",
  openai_api_key: "saved-key",
  openai_base_url: "http://saved.example/v1",
  temperature: 0.7,
  system_prompt: "saved prompt",
  tools_list: [],
  mcp: { servers: {} },
  context_strategy: {
    compression_trigger_tokens_k: 100,
  },
};

configureBackendClient("t".repeat(43));

afterEach(() => {
  globalThis.fetch = originalFetch;
  clearChatSettingsCache();
});

test("PUT 失败时保留最后一次成功的 ChatSettings 缓存", async () => {
  updateChatSettingsCache(savedSettings);
  globalThis.fetch = async () => new Response("backend unavailable", { status: 503 });

  const attemptedSettings: ChatSettingsData = {
    ...savedSettings,
    model_name: "unsaved-model",
  };

  await assert.rejects(updateChatSettings(attemptedSettings));
  assert.deepEqual(getChatSettingsCache(), savedSettings);
});

test("PUT 成功后更新 ChatSettings 缓存", async () => {
  updateChatSettingsCache(savedSettings);
  globalThis.fetch = async () =>
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

test("前端默认 memory 插件为固有启用", () => {
  const defaults = getDefaultAgentPluginsForTest();

  assert.equal(defaults.memory.enabled, true);
});

test("新建模型默认使用 100K token 压缩触发阈值", async () => {
  const requestBodies: ChatSettingsData[] = [];
  globalThis.fetch = async (_input, init) => {
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

test("更新 ChatSettings 时不再发送旧记忆插件字段", async () => {
  let requestBody: Record<string, unknown> | null = null;
  globalThis.fetch = async (_input, init) => {
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

test("更新 ChatSettings 时完整往返 MCP 模型权限", async () => {
  let requestBody: Record<string, unknown> | null = null;
  globalThis.fetch = async (_input, init) => {
    requestBody = JSON.parse(String(init?.body));
    const normalized = {
      ...settings,
      mcp: { servers: { filesystem: { enabled: true, tools: { read_file: "ask", write_file: "ask" } } } },
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
        filesystem: { enabled: true, tools: { read_file: "allow", write_file: "ask" } },
      },
    },
  };

  await updateChatSettings(settings);

  assert.deepEqual((requestBody as Record<string, unknown> | null)?.mcp, settings.mcp);
  assert.equal(getChatSettingsCache()?.mcp.servers.filesystem?.tools.read_file, "ask");
});

test("MCP 更新或删除成功后清除 ChatSettings 缓存", async () => {
  updateChatSettingsCache(savedSettings);

  const result = await runMcpMutationWithCacheInvalidation(async () => "done");

  assert.equal(result, "done");
  assert.equal(getChatSettingsCache(), null);
});

test("MCP 更新或删除失败时保留 ChatSettings 缓存", async () => {
  updateChatSettingsCache(savedSettings);

  await assert.rejects(
    runMcpMutationWithCacheInvalidation(async () => {
      throw new Error("failed");
    })
  );

  assert.deepEqual(getChatSettingsCache(), savedSettings);
});
