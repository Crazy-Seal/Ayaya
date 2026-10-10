import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { RollingLogWriter } from "../electron/logging/rolling-log-writer.js";

test("滚动日志串行写入并保留限定数量的文件", /**
 * 验证滚动日志串行写入并保留限定数量的文件。
 */
async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "ayaya-log-writer-"));
  try {
    const writer = new RollingLogWriter({
      directory,
      fileName: "frontend.log",
      maxBytes: 12,
      maxFiles: 3,
    });

    await Promise.all([
      writer.write("first\n"),
      writer.write("second\n"),
      writer.write("third\n"),
      writer.write("fourth\n"),
    ]);
    await writer.close();

    const files = (await readdir(directory)).sort();
    assert.deepEqual(files, ["frontend.log", "frontend.log.1", "frontend.log.2"]);
    const contents = await Promise.all(
      files.map(/**
       * 由集合的 map 遍历每项数据时调用，读取当前测试或资源文件。
       *
       * 此定义用于：验证滚动日志串行写入并保留限定数量的文件。
       *
       * Args:
       *     file (string): 用户拖入或选取的图片文件。
       */
      (file) => readFile(path.join(directory, file), "utf8")),
    );
    assert.equal(contents.join("").includes("fourth"), true);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("日志写入失败只报告错误而不拒绝调用方", /**
 * 验证日志写入失败只报告错误而不拒绝调用方。
 */
async () => {
  const errors: unknown[] = [];
  const writer = new RollingLogWriter({
    directory: "\0invalid",
    fileName: "backend.log",
    maxBytes: 10,
    maxFiles: 2,
    /**
     * 收集日志文件写入错误，供断言失败被报告而不会拒绝业务调用方。
     *
     * 此定义用于：验证日志写入失败只报告错误而不拒绝调用方。
     *
     * Args:
     *     error (unknown): 本次捕获的异常。
     */
    onError: (error) => errors.push(error),
  });

  await writer.write("line\n");
  await writer.close();

  assert.equal(errors.length > 0, true);
});
