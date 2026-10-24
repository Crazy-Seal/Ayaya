import { MCPTransport } from "../../shared-types.js";
import type { MCPServerConfig } from "../../shared-types.js";

type ServerPolicy = {
  enabled: boolean;
  identity_fingerprint?: string | null;
};

/**
 * 递归稳定化对象键顺序，便于比较配置内容。
 *
 * Args:
 *     value (unknown): 待稳定化的任意 JSON 兼容值。
 *
 * Returns:
 *     unknown: 保持数组顺序且对象键已排序的值。
 */
const stableValue = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(/**
         * 按配置字段名称排序，避免键插入顺序影响服务身份摘要。
         *
         * Args:
         *     [left] ([string, unknown]): 参与稳定排序的左侧字段名称。
         *     [right] ([string, unknown]): 参与稳定排序的右侧字段名称。
         *
         * Returns:
         *     number: 字段名称的排序比较结果。
         */
        ([left], [right]) => left.localeCompare(right))
        .map(/**
         * 递归规范化每个字段值，保持嵌套对象也使用稳定键序。
         *
         * Args:
         *     [key, item] ([string, unknown]): 当前配置对象中的字段名称及字段值。
         *
         * Returns:
         *     [string, unknown]: 当前字段名与规范化值组成的键值对。
         */
        ([key, item]) => [key, stableValue(item)])
    );
  }
  return value;
};

/**
 * 生成用于前端测试状态绑定的 MCP 配置指纹。
 *
 * Args:
 *     config (MCPServerConfig): 当前编辑的 MCP Server 配置。
 *
 * Returns:
 *     string: 可稳定比较的配置序列化字符串。
 */
export const fingerprintMcpConfig = (config: MCPServerConfig): string =>
  JSON.stringify(stableValue(config.transport === MCPTransport.STDIO ? {
    id: config.id,
    transport: config.transport,
    command: config.command,
    args: config.args,
    cwd: config.cwd ?? null,
    env: config.env,
    connect_timeout_seconds: config.connect_timeout_seconds,
  } : {
    id: config.id,
    transport: config.transport,
    url: config.url,
    headers: config.headers,
    connect_timeout_seconds: config.connect_timeout_seconds,
  }));

/**
 * 构造启用状态的模型级 MCP Server 权限。
 *
 * Args:
 *     current (ServerPolicy | undefined): 当前已保存的 Server 权限。
 *
 * Returns:
 *     ServerPolicy: 保留身份绑定的 Server 启用状态。
 */
export const buildEnabledServerPolicy = (
  current: ServerPolicy | undefined
): ServerPolicy => {
  return {
    enabled: true,
    ...(current?.identity_fingerprint
      ? { identity_fingerprint: current.identity_fingerprint }
      : {}),
  };
};
