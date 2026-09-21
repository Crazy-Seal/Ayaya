import assert from "node:assert/strict";
import test from "node:test";

import { buildEnabledServerPolicy, fingerprintMcpConfig } from "../src/settings/mcp-config.js";
import type { MCPServerConfig } from "../shared-types.js";

test("首次启用服务器只写入服务开关", () => {
  assert.deepEqual(buildEnabledServerPolicy(undefined), {
    enabled: true,
  });
});

test("重新启用服务器保留身份绑定", () => {
  const policy = buildEnabledServerPolicy(
    { enabled: true, identity_fingerprint: "a".repeat(64) }
  );
  assert.deepEqual(policy, {
    enabled: true,
    identity_fingerprint: "a".repeat(64),
  });
});

test("启用服务时不附加额外工具策略", () => {
  const policy = buildEnabledServerPolicy(
    { enabled: false }
  );
  assert.deepEqual(policy, { enabled: true });
});

test("MCP 配置指纹不受 env 键顺序影响", () => {
  const base: MCPServerConfig = {
    id: "fs",
    name: "Filesystem",
    enabled: true,
    transport: "stdio",
    command: "node",
    args: ["server.js"],
    cwd: null,
    env: { TOKEN: "secret", MODE: "local" },
    connect_timeout_seconds: 15,
    call_timeout_seconds: 60,
  };
  assert.equal(
    fingerprintMcpConfig(base),
    fingerprintMcpConfig({ ...base, env: { MODE: "local", TOKEN: "secret" } })
  );
  assert.equal(
    fingerprintMcpConfig(base),
    fingerprintMcpConfig({ ...base, name: "Renamed", enabled: false, call_timeout_seconds: 30 })
  );
});
