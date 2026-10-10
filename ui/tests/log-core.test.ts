import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BackendLineDecoder,
  LogHub,
  inferBackendLevel,
  sanitizeLogMessage,
} from "../electron/logging/log-core.js";
import type { LogBatch } from "../shared-types.js";

test("日志脱敏会移除令牌、授权头、API Key 和图片 Base64", /**
 * 验证日志脱敏会移除令牌、授权头、API Key 和图片 Base64。
 */
() => {
  const token = "secret-token-value";
  const input = [
    `AYAYA_API_TOKEN=${token}`,
    `Authorization: Bearer ${token}`,
    'openai_api_key="sk-sensitive"',
    "data:image/png;base64,AAAABBBBCCCC",
  ].join(" ");

  const result = sanitizeLogMessage(input, [token], 32 * 1024);

  assert.doesNotMatch(result, /secret-token-value|sk-sensitive|AAAABBBBCCCC/);
  assert.match(result, /\[REDACTED\]/);
});

test("日志消息超过限制时按 UTF-8 字节安全截断", /**
 * 验证日志消息超过限制时按 UTF-8 字节安全截断。
 */
() => {
  const result = sanitizeLogMessage("你".repeat(20), [], 24);

  assert.match(result, /已截断/);
  assert.doesNotMatch(result, /\uFFFD/);
});

test("LogHub 保留分侧环形缓存、批量发布并支持清空视图", /**
 * 验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
 */
async () => {
  const batches: LogBatch[] = [];
  const hub = new LogHub({ bufferLimit: 2, batchIntervalMs: 1 });
  hub.subscribe(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
   *
   * Args:
   *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
   */
  (batch) => batches.push(batch));

  hub.append({
    side: "frontend",
    source: "electron-main",
    level: "info",
    scope: "test",
    message: "one",
  });
  hub.append({
    side: "frontend",
    source: "renderer",
    level: "warn",
    scope: "test",
    message: "two",
  });
  hub.append({
    side: "frontend",
    source: "renderer",
    level: "error",
    scope: "test",
    message: "three",
  });

  await new Promise(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
   *
   * Args:
   *     resolve ((value: unknown) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setTimeout(resolve, 10));

  assert.deepEqual(
    hub.getSnapshot().frontend.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 record.message 供当前步骤继续处理。
     *
     * 此定义用于：验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
     *
     * Args:
     *     record (LogRecord): 当前需要显示或筛选的日志记录。
     */
    (record) => record.message),
    ["two", "three"],
  );
  assert.deepEqual(
    batches.flatMap(/**
     * 由外层操作在对应处理阶段调用，读取 batch.records 供当前步骤继续处理。
     *
     * 此定义用于：验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
     *
     * Args:
     *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
     */
    (batch) => batch.records).map(/**
     * 由集合的 map 遍历每项数据时调用，读取 record.message 供当前步骤继续处理。
     *
     * 此定义用于：验证LogHub 保留分侧环形缓存、批量发布并支持清空视图。
     *
     * Args:
     *     record (LogRecord): 当前需要显示或筛选的日志记录。
     */
    (record) => record.message),
    ["one", "two", "three"],
  );

  hub.clear("frontend");
  assert.deepEqual(hub.getSnapshot().frontend, []);
  await hub.close();
});

test("LogHub 立即逐条发布后端日志并继续批量发布前端日志", /**
 * 验证LogHub 立即逐条发布后端日志并继续批量发布前端日志。
 */
async () => {
  const batches: LogBatch[] = [];
  const hub = new LogHub({
    batchIntervalMs: 20,
    immediateSides: ["backend"],
  });
  hub.subscribe(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证LogHub 立即逐条发布后端日志并继续批量发布前端日志。
   *
   * Args:
   *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
   */
  (batch) => batches.push(batch));

  hub.append({
    side: "frontend",
    source: "electron-main",
    level: "info",
    scope: "test",
    message: "frontend-one",
  });
  for (const message of ["backend-one", "backend-two", "backend-three"]) {
    hub.append({
      side: "backend",
      source: "stderr",
      level: "info",
      scope: "test",
      message,
    });
  }

  assert.deepEqual(
    batches.map(/**
     * 由集合的 map 遍历每项数据时调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证LogHub 立即逐条发布后端日志并继续批量发布前端日志。
     *
     * Args:
     *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
     */
    (batch) => batch.records.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 item.message 供当前步骤继续处理。
     *
     * 此定义用于：由集合的 map 遍历每项数据时调用，按本次条目的条件完成当前转换或状态检查。
     *
     * Args:
     *     item (LogRecord): 当前遍历的配置或目录条目。
     */
    (item) => item.message)),
    [["backend-one"], ["backend-two"], ["backend-three"]],
  );

  await new Promise(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证LogHub 立即逐条发布后端日志并继续批量发布前端日志。
   *
   * Args:
   *     resolve ((value: unknown) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setTimeout(resolve, 40));

  assert.deepEqual(
    batches.map(/**
     * 由集合的 map 遍历每项数据时调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证LogHub 立即逐条发布后端日志并继续批量发布前端日志。
     *
     * Args:
     *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
     */
    (batch) => batch.records.map(/**
     * 由集合的 map 遍历每项数据时调用，读取 item.message 供当前步骤继续处理。
     *
     * 此定义用于：由集合的 map 遍历每项数据时调用，按本次条目的条件完成当前转换或状态检查。
     *
     * Args:
     *     item (LogRecord): 当前遍历的配置或目录条目。
     */
    (item) => item.message)),
    [["backend-one"], ["backend-two"], ["backend-three"], ["frontend-one"]],
  );
  await hub.close();
});

test("清空视图会移除尚未发布的同侧批次", /**
 * 验证清空视图会移除尚未发布的同侧批次。
 */
async () => {
  const batches: LogBatch[] = [];
  const hub = new LogHub({ batchIntervalMs: 1 });
  hub.subscribe(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证清空视图会移除尚未发布的同侧批次。
   *
   * Args:
   *     batch (LogBatch): 包含已脱敏日志记录的实时批次。
   */
  (batch) => batches.push(batch));
  hub.append({
    side: "backend",
    source: "stderr",
    level: "info",
    scope: "test",
    message: "即将清空",
  });

  hub.clear("backend");
  await new Promise(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证清空视图会移除尚未发布的同侧批次。
   *
   * Args:
   *     resolve ((value: unknown) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setTimeout(resolve, 10));

  assert.deepEqual(batches, []);
  await hub.close();
});

test("后端行解析器正确处理跨 chunk、CRLF 和退出残留", /**
 * 验证后端行解析器正确处理跨 chunk、CRLF 和退出残留。
 */
() => {
  const lines: string[] = [];
  const decoder = new BackendLineDecoder(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证后端行解析器正确处理跨 chunk、CRLF 和退出残留。
   *
   * Args:
   *     line (string): 已经拼接完整的一行后端输出。
   */
  (line) => lines.push(line));

  decoder.write(Buffer.from("INFO: boot\r\nTrace"));
  decoder.write(Buffer.from("back line\nlast"));
  decoder.end();

  assert.deepEqual(lines, ["INFO: boot", "Traceback line", "last"]);
});

test("后端行解析器正确还原跨 chunk 的中文 UTF-8 字符", /**
 * 验证后端行解析器正确还原跨 chunk 的中文 UTF-8 字符。
 */
() => {
  const lines: string[] = [];
  const decoder = new BackendLineDecoder(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证后端行解析器正确还原跨 chunk 的中文 UTF-8 字符。
   *
   * Args:
   *     line (string): 已经拼接完整的一行后端输出。
   */
  (line) => lines.push(line));
  const encoded = Buffer.from("INFO: 中文日志\r\n尾行", "utf8");
  const splitInsideFirstChineseCharacter = Buffer.byteLength("INFO: ", "utf8") + 1;

  decoder.write(encoded.subarray(0, splitInsideFirstChineseCharacter));
  decoder.write(encoded.subarray(splitInsideFirstChineseCharacter));
  decoder.end();

  assert.deepEqual(lines, ["INFO: 中文日志", "尾行"]);
});

test("Uvicorn stderr INFO 不会被误判为错误", /**
 * 验证Uvicorn stderr INFO 不会被误判为错误。
 */
() => {
  assert.equal(inferBackendLevel("INFO: Uvicorn running"), "info");
  assert.equal(inferBackendLevel("WARNING: slow request"), "warn");
  assert.equal(inferBackendLevel("ERROR: startup failed"), "error");
  assert.equal(inferBackendLevel("Traceback (most recent call last):"), "error");
  assert.equal(inferBackendLevel("2026-07-24 01:02:03 | WARNING | app.agent | slow"), "warn");
  assert.equal(inferBackendLevel("2026-07-24 01:02:03 | ERROR | app.agent | failed"), "error");
});
