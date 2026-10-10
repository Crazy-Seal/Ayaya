import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { backendFetch, configureBackendClient } from "../electron/backend-client.js";

const originalFetch = globalThis.fetch;

afterEach(/**
 * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
 */
() => {
  globalThis.fetch = originalFetch;
});

test("后端客户端自动添加主进程私有 Bearer Token", /**
 * 验证后端客户端自动添加主进程私有 Bearer Token。
 */
async () => {
  configureBackendClient("s".repeat(43));
  let authorization = "";
  globalThis.fetch = /**
   * 由浏览器在fetch时调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证后端客户端自动添加主进程私有 Bearer Token。
   *
   * Args:
   *     _input (URL | RequestInfo): 当前日志输入或配置输入框。
   *     init (RequestInit | undefined): 本次后端 HTTP 请求选项。
   */
  async (_input, init) => {
    authorization = new Headers(init?.headers).get("Authorization") ?? "";
    return new Response("ok");
  };

  await backendFetch("/internal/ready");

  assert.equal(authorization, `Bearer ${"s".repeat(43)}`);
});

test("调用方不能覆盖后端认证头", /**
 * 验证调用方不能覆盖后端认证头。
 */
async () => {
  configureBackendClient("s".repeat(43));

  await assert.rejects(
    /**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证调用方不能覆盖后端认证头。
     */
    async () => backendFetch("/internal/ready", { headers: { Authorization: "Bearer evil" } }),
    /不允许覆盖/,
  );
});
