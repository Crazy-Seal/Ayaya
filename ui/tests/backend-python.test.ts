import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import type { ChildProcess, SpawnOptions } from "node:child_process";

import {
  buildBackendPythonEnvironment,
  createBackendPythonResolver,
} from "../electron/backend-python.js";

/**
 * 模拟 Conda 环境查询进程，用例手动提供输出、取消和退出状态。
 */
class FakeCondaProcess extends EventEmitter {
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  pid: number | undefined = 4321;
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  killed = false;

  /**
   * 记录辅助进程终止请求，供检查取消和超时清理。
   *
   * 此定义用于：提供 FakeCondaProcess 的页面、进程或定时器测试状态，供用例手动控制事件并检查执行记录。
   */
  kill(): boolean {
    this.killed = true;
    this.signalCode = "SIGTERM";
    return true;
  }
}

test("自动发现 ayaya 环境后返回直接 Python 路径", /**
 * 验证自动发现 ayaya 环境后返回直接 Python 路径。
 */
async () => {
  const child = new FakeCondaProcess();
  const calls: Array<{ command: string; args: readonly string[]; options: SpawnOptions }> = [];
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证自动发现 ayaya 环境后返回直接 Python 路径。
     *
     * Args:
     *     command (string): 要启动的可执行程序。
     *     args (readonly string[]): 传递给子进程的命令行参数。
     *     options (SpawnOptions): 调用方提供的配置或运行选项。
     */
    spawnProcess: (command, args, options) => {
      calls.push({ command, args, options });
      return child as unknown as ChildProcess;
    },
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证自动发现 ayaya 环境后返回直接 Python 路径。
     *
     * Args:
     *     path (string): 本用例检查的后端接口地址或解释器文件路径。
     */
    fileExists: async (path) => path === "D:\\Miniconda\\envs\\ayaya\\python.exe",
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stdout.end(JSON.stringify({
    envs: [
      "D:\\Miniconda",
      "D:\\Miniconda\\envs\\ayaya",
    ],
  }));
  child.exitCode = 0;
  child.emit("close", 0, null);

  assert.deepEqual(await resolving, {
    executable: "D:\\Miniconda\\envs\\ayaya\\python.exe",
    environmentRoot: "D:\\Miniconda\\envs\\ayaya",
  });
  assert.equal(calls[0]?.command, "conda");
  assert.deepEqual(calls[0]?.args, ["env", "list", "--json"]);
});

test("显式 Python 覆盖跳过 Conda 环境发现", /**
 * 验证显式 Python 覆盖跳过 Conda 环境发现。
 */
async () => {
  let spawnCalls = 0;
  const resolver = createBackendPythonResolver({
    env: { AYAYA_PYTHON_EXECUTABLE: "X:\\Python\\python.exe" },
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证显式 Python 覆盖跳过 Conda 环境发现。
     */
    spawnProcess: () => {
      spawnCalls += 1;
      throw new Error("不应启动 Conda");
    },
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证显式 Python 覆盖跳过 Conda 环境发现。
     */
    fileExists: async () => true,
  });

  assert.deepEqual(await resolver.resolve(), {
    executable: "X:\\Python\\python.exe",
  });
  assert.equal(spawnCalls, 0);
});

test("多个同名 ayaya 环境时拒绝猜测解释器", /**
 * 验证多个同名 ayaya 环境时拒绝猜测解释器。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证多个同名 ayaya 环境时拒绝猜测解释器。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证多个同名 ayaya 环境时拒绝猜测解释器。
     */
    fileExists: async () => true,
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stdout.end(JSON.stringify({
    envs: [
      "C:\\Conda\\envs\\ayaya",
      "D:\\Conda\\envs\\ayaya",
    ],
  }));
  child.exitCode = 0;
  child.emit("close", 0, null);

  await assert.rejects(resolving, /找到多个名为 ayaya 的 Conda 环境/);
});

test("取消环境发现会终止 Conda 辅助进程", /**
 * 验证取消环境发现会终止 Conda 辅助进程。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证取消环境发现会终止 Conda 辅助进程。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证取消环境发现会终止 Conda 辅助进程。
     */
    fileExists: async () => true,
    timeoutMs: 1_000,
  });
  const abortController = new AbortController();

  const resolving = resolver.resolve(abortController.signal);
  const rejected = assert.rejects(resolving, /后端启动已取消/);
  abortController.abort();

  await rejected;
  assert.equal(child.killed, true);
});

test("Conda 命令不存在时返回启动错误", /**
 * 验证Conda 命令不存在时返回启动错误。
 */
async () => {
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证Conda 命令不存在时返回启动错误。
     */
    spawnProcess: () => {
      throw Object.assign(new Error("spawn conda ENOENT"), { code: "ENOENT" });
    },
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证Conda 命令不存在时返回启动错误。
     */
    fileExists: async () => true,
  });

  await assert.rejects(resolver.resolve(), /ENOENT/);
});

test("Conda 返回非零退出码时包含有界错误信息", /**
 * 验证Conda 返回非零退出码时包含有界错误信息。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证Conda 返回非零退出码时包含有界错误信息。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证Conda 返回非零退出码时包含有界错误信息。
     */
    fileExists: async () => true,
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stderr.end("environment query failed");
  child.exitCode = 1;
  child.emit("close", 1, null);

  await assert.rejects(resolving, /退出码 1.*environment query failed/);
});

test("Conda 返回非法 JSON 时拒绝启动", /**
 * 验证Conda 返回非法 JSON 时拒绝启动。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证Conda 返回非法 JSON 时拒绝启动。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证Conda 返回非法 JSON 时拒绝启动。
     */
    fileExists: async () => true,
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stdout.end("not-json");
  child.exitCode = 0;
  child.emit("close", 0, null);

  await assert.rejects(resolving, /无法解析的环境列表/);
});

test("没有 ayaya 环境时返回可操作的错误", /**
 * 验证没有 ayaya 环境时返回可操作的错误。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证没有 ayaya 环境时返回可操作的错误。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证没有 ayaya 环境时返回可操作的错误。
     */
    fileExists: async () => true,
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stdout.end(JSON.stringify({ envs: ["C:\\Conda"] }));
  child.exitCode = 0;
  child.emit("close", 0, null);

  await assert.rejects(resolving, /未找到名为 ayaya/);
});

test("ayaya 环境缺少解释器时拒绝启动", /**
 * 验证ayaya 环境缺少解释器时拒绝启动。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证ayaya 环境缺少解释器时拒绝启动。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证ayaya 环境缺少解释器时拒绝启动。
     */
    fileExists: async () => false,
    timeoutMs: 100,
  });

  const resolving = resolver.resolve();
  child.stdout.end(JSON.stringify({ envs: ["C:\\Conda\\envs\\ayaya"] }));
  child.exitCode = 0;
  child.emit("close", 0, null);

  await assert.rejects(resolving, /不存在 Python 解释器/);
});

test("Conda 环境发现超时会终止辅助进程", /**
 * 验证Conda 环境发现超时会终止辅助进程。
 */
async () => {
  const child = new FakeCondaProcess();
  const resolver = createBackendPythonResolver({
    env: {},
    platform: "win32",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：验证Conda 环境发现超时会终止辅助进程。
     */
    spawnProcess: () => child as unknown as ChildProcess,
    /**
     * 按用例指定的路径判断解释器文件是否存在。
     *
     * 此定义用于：验证Conda 环境发现超时会终止辅助进程。
     */
    fileExists: async () => true,
    timeoutMs: 1,
  });

  await assert.rejects(resolver.resolve(), /查找 ayaya Conda 环境超时/);
  assert.equal(child.killed, true);
});

test("直接启动环境 Python 时补充环境可执行目录且不修改父环境", /**
 * 验证直接启动环境 Python 时补充环境可执行目录且不修改父环境。
 */
() => {
  const parentEnvironment = { PATH: "C:\\Windows\\System32" };

  const childEnvironment = buildBackendPythonEnvironment(
    parentEnvironment,
    {
      executable: "C:\\Conda\\envs\\ayaya\\python.exe",
      environmentRoot: "C:\\Conda\\envs\\ayaya",
    },
    "win32",
  );

  assert.equal(parentEnvironment.PATH, "C:\\Windows\\System32");
  assert.equal(
    childEnvironment.PATH,
    [
      "C:\\Conda\\envs\\ayaya",
      "C:\\Conda\\envs\\ayaya\\Scripts",
      "C:\\Conda\\envs\\ayaya\\Library\\bin",
      "C:\\Windows\\System32",
    ].join(";"),
  );
  assert.equal(childEnvironment.PYTHONIOENCODING, "utf-8");
  assert.equal(childEnvironment.PYTHONUTF8, "1");
  assert.deepEqual(parentEnvironment, { PATH: "C:\\Windows\\System32" });
});

test("显式 Python 没有环境根目录时仍强制标准流使用 UTF-8", /**
 * 验证显式 Python 没有环境根目录时仍强制标准流使用 UTF-8。
 */
() => {
  const parentEnvironment = {
    PATH: "C:\\Windows\\System32",
    PYTHONIOENCODING: "gbk",
    PYTHONUTF8: "0",
  };

  const childEnvironment = buildBackendPythonEnvironment(
    parentEnvironment,
    { executable: "C:\\Python\\python.exe" },
    "win32",
  );

  assert.equal(childEnvironment.PYTHONIOENCODING, "utf-8");
  assert.equal(childEnvironment.PYTHONUTF8, "1");
  assert.equal(parentEnvironment.PYTHONIOENCODING, "gbk");
  assert.equal(parentEnvironment.PYTHONUTF8, "0");
});
