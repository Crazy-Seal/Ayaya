import { resolveAiName } from "../ai-identity.js";
import { fetchChatSettingsBySessionId } from "./chat-settings.js";
import { appendFrontendLog } from "./logging/app-logger.js";

const names = new Map<string, string>();
const versions = new Map<string, number>();
const requests = new Map<string, Promise<string>>();

/**
 * 保存成功后更新指定会话的名字缓存，使等待中的旧查询不能覆盖新名字。
 *
 * Args:
 *     sessionId (string): 已保存 LLM 配置的会话编号。
 *     name (string | null | undefined): 已保存的角色名字。
 *
 * Returns:
 *     string: 标题与卡片共同使用的名字。
 */
export const rememberAiName = (sessionId: string, name?: string | null): string => {
  const resolved = resolveAiName(name);
  versions.set(sessionId, (versions.get(sessionId) ?? 0) + 1);
  names.set(sessionId, resolved);
  return resolved;
};

/**
 * 模型会话删除后移除名字，同时使尚未返回的旧查询失效。
 *
 * Args:
 *     sessionId (string): 已删除模型对应的会话编号。
 */
export const forgetAiName = (sessionId: string): void => {
  versions.set(sessionId, (versions.get(sessionId) ?? 0) + 1);
  names.delete(sessionId);
};

/**
 * 按会话读取 AI 名字，重复请求共用正在执行的查询，成功结果由主进程缓存。
 *
 * Args:
 *     sessionId (string): 需要显示角色名字的会话编号。
 *
 * Returns:
 *     Promise<string>: 已保存名字或未填写时的默认称呼。
 *
 * Raises:
 *     Error: 后端查询失败，或等待期间该会话已被删除。
 */
const readAiName = async (sessionId: string): Promise<string> => {
  const cached = names.get(sessionId);
  if (cached !== undefined) return cached;
  const pending = requests.get(sessionId);
  if (pending) return pending;
  const version = versions.get(sessionId);
  const request = fetchChatSettingsBySessionId(sessionId).then(/**
   * 后端返回设置后只提取名字；保存通知和会话删除优先于较早的查询。
   *
   * Args:
   *     settings (ChatSettingsData): 当前会话的后端设置，其他字段不会传给页面。
   *
   * Returns:
   *     string: 当前有效的会话名字。
   */
  (settings) => {
    if (versions.get(sessionId) !== version) {
      const latest = names.get(sessionId);
      if (latest === undefined) throw new Error("会话已删除，名字查询已失效");
      return latest;
    }
    const name = resolveAiName(settings.name);
    names.set(sessionId, name);
    return name;
  });
  requests.set(sessionId, request);
  try { return await request; }
  finally { if (requests.get(sessionId) === request) requests.delete(sessionId); }
};

/**
 * 查询指定模型会话的名字，单个会话失败不丢弃其他模型的有效名字。
 *
 * Args:
 *     sessionIds (string[]): IPC 已核对属于本地模型的会话编号。
 *
 * Returns:
 *     Promise<Record<string, string>>: 查询成功的会话及其名字，失败项不伪造默认名字。
 */
export const readAiNames = async (sessionIds: string[]): Promise<Record<string, string>> => {
  const results = await Promise.allSettled(sessionIds.map(/**
   * 为每个已核对的会话查询名字，主进程内部复用缓存和正在等待的请求。
   *
   * Args:
   *     sessionId (string): 本地模型对应会话编号。
   *
   * Returns:
   *     Promise<string>: 该会话的名字查询。
   */
  (sessionId) => readAiName(sessionId)));
  const resolved: Record<string, string> = {};
  // 1. 逐项处理结果，只记录失败会话编号，不输出可能包含凭据的完整设置响应。
  for (const [index, result] of results.entries()) {
    if (result.status === "fulfilled") resolved[sessionIds[index]] = result.value;
    else appendFrontendLog("warn", "ai-name", `读取会话 AI 名字失败：${sessionIds[index]}`);
  }
  return resolved;
};
