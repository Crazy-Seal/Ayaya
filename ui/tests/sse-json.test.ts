import assert from "node:assert/strict";
import { test } from "node:test";

import { readSseJson } from "../electron/sse-json.js";

test("SSE 解码跨分片 UTF-8、CRLF 和多个事件", /**
 * 验证SSE 解码跨分片 UTF-8、CRLF 和多个事件。
 */
async () => {
  // 1. 建立隔离 DOM、bridge 替身与事件监听，按场景安排输入和到达顺序。
  // 1.1. 分片心跳只能被忽略，不能产生假业务事件或影响后续中文解码。
  const bytes = new TextEncoder().encode(': ping\n\ndata: {"message":"你好"}\r\n\r\n: ping\n\ndata: {"message":"完成"}\n\n');
  const stream = new ReadableStream<Uint8Array>({
    /**
     * 按指定字节边界入队测试 SSE 数据。
     *
     * Args:
     *     controller (ReadableStreamDefaultController<Uint8Array<ArrayBufferLike>>): 测试流的入队控制器。
     */
    start(controller) {
      const cuts = [3, 17, 22, 24, 30, bytes.length];
      let start = 0;
      for (const end of cuts) {
        controller.enqueue(bytes.slice(start, end));
        start = end;
      }
      controller.close();
    },
  });
  const received: Array<{ message: string }> = [];
  for await (const event of readSseJson<{ message: string }>(
    new Response(stream), new AbortController().signal,
  )) received.push(event);
  assert.deepEqual(received, [{ message: "你好" }, { message: "完成" }]);
});
