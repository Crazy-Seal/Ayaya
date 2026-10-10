/**
 * 按 SSE 帧增量解码 JSON，保留跨网络分片的 CRLF 和 UTF-8 状态。
 *
 * Args:
 *     response (Response): 带流式响应体的后端响应。
 *     signal (AbortSignal): 应用或订阅关闭的取消信号。
 *
 * Yields:
 *     T: 按接收顺序解码的 JSON 事件。
 *
 * Raises:
 *     Error: 响应没有可读取的流式正文。
 *     SyntaxError: 已闭合的 SSE data 不是有效 JSON。
 */
export async function* readSseJson<T>(
  response: Response, signal: AbortSignal,
): AsyncGenerator<T> {
  if (!response.body) throw new Error("事件流没有响应体");
  // 1. 保留 reader、TextDecoder 和帧缓冲，网络分片不等于事件边界。
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) return;
      // 2. 增量解码并按空行定位完整 SSE 帧，未闭合的帧留到下一次读取。
      buffer += decoder.decode(value, { stream: true });
      let boundary = /\r?\n\r?\n/.exec(buffer);
      while (boundary) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        // 3. 合并同帧 data 行再解析 JSON，不能把 event 或其他字段当载荷。
        const data = frame.split(/\r?\n/)
          .filter(/**
           * 只保留 SSE data 字段。
           *
           * Args:
           *     line (string): SSE 帧中的一行。
           *
           * Returns:
           *     boolean: 当前查询、回调或视图操作的结果。
           */
          (line) => line.startsWith("data:"))
          .map(/**
           * 移除 SSE data 字段前缀，保留 JSON 正文。
           *
           * Args:
           *     line (string): SSE 帧中的一行。
           *
           * Returns:
           *     string: 当前查询、回调或视图操作的结果。
           */
          (line) => line.slice(5).trimStart());
        if (data.length) yield JSON.parse(data.join("\n")) as T;
        boundary = /\r?\n\r?\n/.exec(buffer);
      }
    }
  } finally {
    // 4. 订阅退出时释放 reader，清理失败不覆盖原运行结果。
    await reader.cancel().catch(/**
     * 忽略连接已经关闭造成的 reader 清理失败。
     */
    () => {});
  }
}
