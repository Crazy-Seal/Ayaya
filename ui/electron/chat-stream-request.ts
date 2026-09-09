import { backendFetch } from "./backend-client.js";
import { consumeSseStream, type SseStreamHandlers } from "./sse-stream.js";
import type { ChatResult } from "../shared-types.js";
import { Agent } from "undici";

/** 聊天及恢复请求持续等待明确终态，执行超时由后端负责。 */
export const requestChatStream = async (
  path: string,
  body: object,
  handlers: SseStreamHandlers,
): Promise<ChatResult> => {
  // 只关闭聊天流的响应等待时限，不修改其他请求或连接失败策略。
  const dispatcher = new Agent({ headersTimeout: 0, bodyTimeout: 0 });
  const init: RequestInit & { dispatcher: Agent } = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    dispatcher,
  };
  try {
    const response = await backendFetch(path, init);
    if (!response.ok) {
      throw new Error((await response.text()) || `请求失败: ${response.status}`);
    }
    if (!response.body) throw new Error("聊天响应缺少数据流");
    return await consumeSseStream(response.body, handlers, "后端流式请求失败");
  } finally {
    // 请求结束后释放专属连接；销毁失败不能替换业务结果。
    void dispatcher.destroy().catch(() => {});
  }
};
