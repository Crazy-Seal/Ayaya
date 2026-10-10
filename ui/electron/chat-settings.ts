/**
 * 聊天设置 API 调用
 */

import {
  CHAT_HISTORY_PAGE_SIZE,
  CHAT_HISTORY_MAX_PAGES,
} from "./config.js";
import { backendFetch } from "./backend-client.js";
import type { ChatSettingsData, ChatHistoryItem, ApiResponse, ToolItem, PluginItem } from "./types.js";
import type { ToolPolicy, ToolPolicyView } from "../shared-types.js";
import { getActiveModelRecord } from "./model-manager.js";

/**
 * 缓存的聊天设置
 */
let chatSettingsCache: ChatSettingsData | null = null;

const DEFAULT_AGENT_PLUGINS: NonNullable<ChatSettingsData["agent_plugins"]> = {
  memory: {
    enabled: true,
    config: {
      enable_diary: true,
      enable_episodic: true,
      enable_semantic: true,
      long_term_memory_every_human_messages: 10,
    },
  },
};

const DEFAULT_CONTEXT_STRATEGY: ChatSettingsData["context_strategy"] = {
  compression_trigger_tokens_k: 100,
  max_screenshots_in_context: 2,
  screenshot_ttl_human_messages: 2,
};

/**
 * 向测试提供默认记忆插件设置副本。
 *
 * Returns:
 *     NonNullable<ChatSettingsData["agent_plugins"]>: 默认插件配置，不与调用方共享可修改对象。
 */
export const getDefaultAgentPluginsForTest = (): NonNullable<ChatSettingsData["agent_plugins"]> =>
  JSON.parse(JSON.stringify(DEFAULT_AGENT_PLUGINS)) as NonNullable<ChatSettingsData["agent_plugins"]>;

/**
 * 合并已保存插件配置与默认值，保持固有记忆插件配置完整。
 *
 * Args:
 *     value (Partial<ChatSettingsData>["agent_plugins"]): 当前待检查或转换的值。
 *
 * Returns:
 *     NonNullable<ChatSettingsData["agent_plugins"]>: 供界面编辑和保存使用的插件设置。
 */
const normalizeAgentPlugins = (
  value: Partial<ChatSettingsData>["agent_plugins"]
): NonNullable<ChatSettingsData["agent_plugins"]> => {
  const source = value && typeof value === "object" ? value : {};
  const result: NonNullable<ChatSettingsData["agent_plugins"]> = {};
  for (const [name, defaults] of Object.entries(DEFAULT_AGENT_PLUGINS)) {
    const current = source[name];
    const currentConfig = current?.config && typeof current.config === "object"
      ? { ...current.config }
      : {};
    result[name] = {
      enabled: typeof current?.enabled === "boolean" ? current.enabled : defaults.enabled,
      config: {
        ...defaults.config,
        ...currentConfig,
      },
    };
  }
  for (const [name, current] of Object.entries(source)) {
    if (result[name]) continue;
    result[name] = {
      enabled: Boolean(current?.enabled),
      config: current?.config && typeof current.config === "object" ? current.config : {},
    };
  }
  return result;
};

/**
 * 安全解析 JSON
 *
 * Args:
 *     res (Response): 后端 HTTP 响应，正文解析失败时返回空值。
 */
const parseJsonSafe = async <T>(res: Response): Promise<T | null> => {
  try {
    return (await res.json()) as T;
  } catch {
    return null;
  }
};

/**
 * 根据会话 ID 获取聊天设置
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 */
export const fetchChatSettingsBySessionId = async (
  sessionId: string
): Promise<ChatSettingsData> => {
  const url = `/chat_settings/${encodeURIComponent(sessionId)}`;
  const res = await backendFetch(url, {
    method: "GET",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `读取 chat_settings 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<Partial<ChatSettingsData>>>(res);

  if (!result || result.code !== 200 || !result.data) {
    throw new Error(result?.msg || "读取 chat_settings 失败：返回格式错误");
  }

  return {
    session_id: String(result.data.session_id ?? sessionId),
    model_name: String(result.data.model_name ?? ""),
    openai_api_key: String(result.data.openai_api_key ?? ""),
    openai_base_url: String(result.data.openai_base_url ?? ""),
    temperature: typeof result.data.temperature === "number" ? result.data.temperature : 0.7,
    system_prompt: String(result.data.system_prompt ?? ""),
    tool_permissions: result.data.tool_permissions && typeof result.data.tool_permissions === "object"
      ? result.data.tool_permissions
      : {},
    tool_permissions_schema_version: Number(result.data.tool_permissions_schema_version ?? 1),
    mcp: result.data.mcp && typeof result.data.mcp === "object"
      ? result.data.mcp
      : { servers: {} },
    context_strategy: {
      ...DEFAULT_CONTEXT_STRATEGY,
      ...(result.data.context_strategy || {}),
    },
    agent_plugins: normalizeAgentPlugins(result.data.agent_plugins),
    name: result.data.name ?? null,
    feature: result.data.feature ?? null,
    character: result.data.character ?? null,
    address: result.data.address ?? null,
    characteristic: result.data.characteristic ?? null,
    constraint: result.data.constraint ?? null,
  };
};

/**
 * 获取聊天历史分页
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 *     start (number): 可见聊天历史的起始位置，从零开始。
 *     limit (number): 最多保留或读取的记录数量。
 */
export const fetchChatHistoryPageBySessionId = async (
  sessionId: string,
  start: number,
  limit: number
): Promise<ChatHistoryItem[]> => {
  const url = `/chat_history/${encodeURIComponent(sessionId)}?start=${start}&limit=${limit}`;
  const res = await backendFetch(url, {
    method: "GET",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `读取 chat_history 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<Array<Partial<ChatHistoryItem>>>>(res);

  if (!result || result.code !== 200 || !Array.isArray(result.data)) {
    throw new Error(result?.msg || "读取 chat_history 失败：返回格式错误");
  }

  return result.data.map(/**
   * 分页历史响应到达后统一正文、角色、时间及图片字段，交给聊天页展示。
   *
   * Args:
   *     item (Partial<ChatHistoryItem>): 当前遍历的配置或目录条目。
   *
   * Returns:
   *     { role: string; content: string; timestamp: string; images: string[] | undefined; }: 规范化的单条可见历史消息。
   */
  (item) => ({
    role: String(item.role ?? ""),
    content: String(item.content ?? ""),
    timestamp: String(item.timestamp ?? ""),
    images: Array.isArray(item.images) ? item.images : undefined,
  }));
};

/**
 * 获取最后 N 条聊天历史
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 *     n (number): 最多读取的历史消息数量。
 */
export const fetchChatHistoryLastN = async (
  sessionId: string,
  n: number
): Promise<ChatHistoryItem[]> => {
  const url = `/chat_history_last_n/${encodeURIComponent(sessionId)}?n=${n}`;
  const res = await backendFetch(url, {
    method: "GET",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `读取 chat_history_last_n 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<Array<Partial<ChatHistoryItem>>>>(res);

  if (!result || result.code !== 200 || !Array.isArray(result.data)) {
    throw new Error(result?.msg || "读取 chat_history_last_n 失败：返回格式错误");
  }

  return result.data.map(/**
   * 最近历史响应到达后统一字段类型，缺少图片列表时保留为空。
   *
   * Args:
   *     item (Partial<ChatHistoryItem>): 当前遍历的配置或目录条目。
   *
   * Returns:
   *     { role: string; content: string; timestamp: string; images: string[] | undefined; }: 规范化的单条最近历史消息。
   */
  (item) => ({
    role: String(item.role ?? ""),
    content: String(item.content ?? ""),
    timestamp: String(item.timestamp ?? ""),
    images: Array.isArray(item.images) ? item.images : undefined,
  }));
};

/**
 * 从历史记录中获取最新的 AI 消息
 *
 * Args:
 *     history (ChatHistoryItem[]): 按原对话顺序排列的可见聊天历史。
 */
const getLatestAiMessageFromHistory = (history: ChatHistoryItem[]): string | null => {
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const item = history[index];
    const role = item.role.trim().toLowerCase();
    const isAi = role === "ai" || role === "assistant";
    const content = item.content.trim();
    if (isAi && content.length > 0) {
      return content;
    }
  }

  return null;
};

/**
 * 获取最新 AI 消息
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 */
export const fetchLatestAiMessageBySessionId = async (
  sessionId: string
): Promise<string | null> => {
  // 直接获取最后 20 条记录查找最新 AI 消息
  const history = await fetchChatHistoryLastN(sessionId, 20);
  return getLatestAiMessageFromHistory(history);
};

/**
 * 创建空的聊天设置
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 */
export const createEmptyChatSettings = async (sessionId: string): Promise<ChatSettingsData> => {
  const payload: ChatSettingsData = {
    session_id: sessionId,
    model_name: "",
    openai_api_key: "",
    openai_base_url: "",
    temperature: 0,
    system_prompt: "",
    tool_permissions: {},
    tool_permissions_schema_version: 1,
    mcp: { servers: {} },
    context_strategy: { ...DEFAULT_CONTEXT_STRATEGY },
    agent_plugins: DEFAULT_AGENT_PLUGINS,
    name: null,
    feature: null,
    character: null,
    address: null,
    characteristic: null,
    constraint: null,
  };

  const res = await backendFetch("/chat_settings", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `创建 chat_settings 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<ChatSettingsData>>(res);
  if (!result || result.code !== 200 || !result.data) {
    throw new Error(result?.msg || "创建 chat_settings 失败：返回格式错误");
  }
  chatSettingsCache = result.data;
  return result.data;
};

/**
 * 删除聊天设置
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 */
export const deleteChatSettingsBySessionId = async (sessionId: string): Promise<void> => {
  const url = `/chat_settings/${encodeURIComponent(sessionId)}`;
  const res = await backendFetch(url, {
    method: "DELETE",
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `删除 chat_settings 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<never>>(res);
  if (!result || result.code !== 200) {
    throw new Error(result?.msg || "删除 chat_settings 失败：返回格式错误");
  }
};

/**
 * 确保聊天设置已加载
 */
export const ensureChatSettingsLoaded = async (): Promise<ChatSettingsData> => {
  const active = getActiveModelRecord();
  if (chatSettingsCache && chatSettingsCache.session_id === active.sessionId) {
    return chatSettingsCache;
  }

  const fetched = await fetchChatSettingsBySessionId(active.sessionId);
  chatSettingsCache = fetched;
  return fetched;
};

/**
 * 更新聊天设置缓存
 *
 * Args:
 *     settings (ChatSettingsData): 当前会话或桌面界面的设置。
 */
export const updateChatSettingsCache = (settings: ChatSettingsData): void => {
  chatSettingsCache = settings;
};

/**
 * 获取缓存的聊天设置
 */
export const getChatSettingsCache = (): ChatSettingsData | null => {
  return chatSettingsCache;
};

/**
 * 清除聊天设置缓存
 */
export const clearChatSettingsCache = (): void => {
  chatSettingsCache = null;
};

/**
 * 执行 MCP 配置变更，并仅在成功后清理模型配置缓存。
 *
 * @throws {Error} 配置变更失败时原样向上抛出，且保留当前缓存。
 *
 * Args:
 *     mutation (() => Promise<T>): 创建、更新、删除或重连 MCP Server 的异步操作。
 *
 * Returns:
 *     Promise<T>: 配置变更操作的原始结果。
 */
export const runMcpMutationWithCacheInvalidation = async <T>(
  mutation: () => Promise<T>
): Promise<T> => {
  const result = await mutation();
  clearChatSettingsCache();
  return result;
};

/**
 * 更新聊天设置到后端
 *
 * Args:
 *     payload (ChatSettingsData): 已经过入口校验的桌面动作参数。
 */
export const updateChatSettings = async (
  payload: ChatSettingsData
): Promise<ChatSettingsData> => {
  const res = await backendFetch("/chat_settings", {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `更新 chat_settings 失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<ChatSettingsData>>(res);
  if (!result || result.code !== 200 || !result.data) {
    throw new Error(result?.msg || "更新 chat_settings 失败：返回格式错误");
  }

  updateChatSettingsCache(result.data);
  return result.data;
};

/**
 * 获取可用工具列表
 */
export const fetchAvailableTools = async (): Promise<ToolItem[]> => {
  const url = "/tools";
  const res = await backendFetch(url, { method: "GET" });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `获取工具列表失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<{ tools: ToolItem[] }>>(res);
  if (!result || result.code !== 200 || !result.data?.tools) {
    throw new Error(result?.msg || "获取工具列表失败：返回格式错误");
  }

  return result.data.tools.map(/**
   * 把后端本地工具目录项转换为页面使用的名称和说明。
   *
   * Args:
   *     item (ToolItem): 当前遍历的配置或目录条目。
   *
   * Returns:
   *     { name: string; description: string; }: 单个本地工具的信息。
   */
  (item) => ({
    name: String(item.name ?? ""),
    description: String(item.description ?? ""),
  }));
};

/**
 * 按会话请求最新工具权限目录，包含本地和 MCP 工具状态。
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 *
 * Returns:
 *     Promise<ToolPolicyView[]>: 后端返回的工具权限列表。
 */
export const fetchToolPermissions = async (sessionId: string): Promise<ToolPolicyView[]> => {
  const res = await backendFetch(`/tool-permissions/${encodeURIComponent(sessionId)}`);
  const result = await parseJsonSafe<ApiResponse<{ tools: ToolPolicyView[] }>>(res);
  if (!res.ok || !result?.data?.tools) throw new Error(result?.msg || "读取工具权限失败");
  return result.data.tools;
};

/**
 * 把用户编辑的会话工具权限保存到后端，再读取返回的规范化结果。
 *
 * Args:
 *     sessionId (string): 当前操作所属的会话编号。
 *     toolPermissions (Record<string, ToolPolicy>): 按稳定工具标识保存的新权限。
 *
 * Returns:
 *     Promise<ToolPolicyView[]>: 保存后的工具权限目录。
 */
export const saveToolPermissions = async (
  sessionId: string,
  toolPermissions: Record<string, ToolPolicy>
): Promise<ToolPolicyView[]> => {
  const res = await backendFetch(`/tool-permissions/${encodeURIComponent(sessionId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool_permissions: toolPermissions }),
  });
  const result = await parseJsonSafe<ApiResponse<{ tools: ToolPolicyView[] }>>(res);
  if (!res.ok || !result?.data?.tools) throw new Error(result?.msg || "保存工具权限失败");
  clearChatSettingsCache();
  return result.data.tools;
};

/**
 * 获取可用插件列表
 */
export const fetchAvailablePlugins = async (): Promise<PluginItem[]> => {
  const url = "/plugins";
  const res = await backendFetch(url, { method: "GET" });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `获取插件列表失败: ${res.status}`);
  }

  const result = await parseJsonSafe<ApiResponse<{ plugins: PluginItem[] }>>(res);
  if (!result || result.code !== 200 || !result.data?.plugins) {
    throw new Error(result?.msg || "获取插件列表失败：返回格式错误");
  }

  return result.data.plugins.map(/**
   * 把插件目录项转换为页面模型，补齐固有标记、默认值和配置结构。
   *
   * Args:
   *     item (PluginItem): 当前遍历的配置或目录条目。
   *
   * Returns:
   *     { name: string; description: string; inherent: boolean; default_config: Record<string, unknown>; config_schema: Record<string, unknown>; }: 单个插件的页面编辑信息。
   */
  (item) => ({
    name: String(item.name ?? ""),
    description: String(item.description ?? ""),
    inherent: Boolean(item.inherent),
    default_config: item.default_config ?? {},
    config_schema: item.config_schema ?? {},
  }));
};
