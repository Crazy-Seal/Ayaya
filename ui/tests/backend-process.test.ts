import assert from "node:assert/strict";
import { spawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";

import type { LogLevel } from "../shared-types.js";
import {
  createBackendProcessController,
  startBackend,
  type BackendUnexpectedExit,
} from "../electron/backend-process.js";

void startBackend;

/**
 * 模拟受管 Python 进程及输出事件，用例手动控制就绪前后错误和退出。
 */
class FakeChildProcess extends EventEmitter {
  pid: number | undefined = 1234;
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  stdout = new PassThrough();
  stderr = new PassThrough();
}

/**
 * 配置进程控制器测试中的就绪响应、等待期限和辅助依赖。
 */
interface ControllerHarnessOptions {
  env?: NodeJS.ProcessEnv;
  ready?: () => Promise<Response>;
  resolvePythonExecutable?: (
    signal?: AbortSignal,
  ) => Promise<{ executable: string; environmentRoot: string }>;
  resolvedPython?: {
    executable: string;
    environmentRoot: string;
  };
  startupTimeoutMs?: number;
  shutdownTimeoutMs?: number;
}

/**
 * 装配可以记录子进程启动、认证、输出及关闭的测试依赖。
 *
 * Args:
 *     options (ControllerHarnessOptions): 调用方提供的配置或运行选项。
 */
const createHarness = (options: ControllerHarnessOptions = {}) => {
  const children: FakeChildProcess[] = [];
  const spawnCalls: Array<{ command: string; args: readonly string[]; options: SpawnOptions }> = [];
  const terminated: ChildProcess[] = [];
  const loggedErrors: unknown[] = [];
  const backendLogs: Array<{ source: "stdout" | "stderr"; level: LogLevel; message: string }> = [];
  let shutdownCalls = 0;
  const controller = createBackendProcessController({
    env: options.env ?? {},
    workspaceRoot: "E:\\code\\Ayaya",
    backendBaseUrl: "http://127.0.0.1:8000",
    /**
     * 创建并记录测试子进程，供用例控制输出、错误和退出事件。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     *
     * Args:
     *     command (string): 要启动的可执行程序。
     *     args (readonly string[]): 传递给子进程的命令行参数。
     *     spawnOptions (SpawnOptions): 测试记录的子进程目录、环境和隐藏窗口配置。
     */
    spawnProcess: (command, args, spawnOptions) => {
      spawnCalls.push({ command, args, options: spawnOptions });
      const child = new FakeChildProcess();
      children.push(child);
      return child as unknown as ChildProcess;
    },
    /**
     * 记录本次后端通信使用的测试令牌。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     */
    configureClient: () => undefined,
    /**
     * 提供固定测试令牌，避免用例依赖随机值。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     */
    createToken: () => "t".repeat(43),
    resolvePythonExecutable: options.resolvePythonExecutable ?? (/**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     */
    async () =>
      options.resolvedPython ?? ({
        executable: "D:\\Miniconda\\envs\\ayaya\\python.exe",
        environmentRoot: "D:\\Miniconda\\envs\\ayaya",
      })),
    /**
     * 根据用例脚本返回后端健康或关闭响应，记录请求顺序。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     *
     * Args:
     *     path (string): 本用例检查的后端接口地址或解释器文件路径。
     */
    fetchBackend: async (path) => {
      if (path === "/internal/shutdown") {
        shutdownCalls += 1;
        return new Response(null, { status: 200 });
      }
      return (options.ready ?? (/**
       * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
       *
       * 此定义用于：根据用例脚本返回后端健康或关闭响应，记录请求顺序。
       */
      async () => new Response(null, { status: 200 })))();
    },
    /**
     * 记录受管进程终止操作，供断言不会重复终止。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     *
     * Args:
     *     process (ChildProcess): 本用例创建的模拟子进程。
     */
    terminateProcess: async (process) => {
      terminated.push(process);
      (process as unknown as FakeChildProcess).exitCode = -1;
    },
    /**
     * 收集当前用例产生的错误，避免触发真实应用日志流程。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     *
     * Args:
     *     _message (string): 当前需要显示或记录的文本。
     *     error (unknown): 本次捕获的异常。
     */
    logError: (_message, error) => loggedErrors.push(error),
    /**
     * 记录后端输出的来源、级别和正文，供外层断言。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     *
     * Args:
     *     source ("stdout" | "stderr"): 日志产生的来源。
     *     level (LogLevel): 当前日志级别。
     *     message (string): 当前需要显示或记录的文本。
     */
    appendBackendLog: (source, level, message) => {
      backendLogs.push({ source, level, message });
    },
    pollIntervalMs: 1,
    startupTimeoutMs: options.startupTimeoutMs ?? 100,
    shutdownTimeoutMs: options.shutdownTimeoutMs ?? 5,
  });
  return {
    /**
     * 读取当前测试创建的子进程，供手动发送生命周期事件。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     */
    get child() {
      const child = children.at(-1);
      assert.ok(child, "后端子进程尚未创建");
      return child;
    },
    children,
    controller,
    spawnCalls,
    terminated,
    loggedErrors,
    backendLogs,
    /**
     * 读取测试记录的关闭请求数量。
     *
     * 此定义用于：装配可以记录子进程启动、认证、输出及关闭的测试依赖。
     */
    get shutdownCalls() {
      return shutdownCalls;
    },
  };
};

/**
 * 等待测试进程创建记录出现，再继续验证启动状态。
 */
const waitForSpawn = async (): Promise<void> => {
  await new Promise<void>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：等待测试进程创建记录出现，再继续验证启动状态。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setImmediate(resolve));
};

test("Python 不存在时 startBackend 应拒绝而不是触发未处理 error", /**
 * 验证Python 不存在时 startBackend 应拒绝而不是触发未处理 error。
 */
async () => {
  const moduleUrl = new URL("../electron/backend-process.js", import.meta.url).href;
  const script = `
    import { startBackend } from ${JSON.stringify(moduleUrl)};
    try {
      await startBackend();
      console.log("UNEXPECTED_SUCCESS");
      process.exitCode = 2;
    } catch (error) {
      console.log("REJECTED:" + (error instanceof Error ? error.message : String(error)));
    }
  `;
  const result = await new Promise<{ code: number | null; stdout: string; stderr: string }>(
    /**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证Python 不存在时 startBackend 应拒绝而不是触发未处理 error。
     *
     * Args:
     *     resolve ((value: { code: number | null; stdout: string; stderr: string; } | PromiseLike<{ code: number | null; stdout: string; stderr: string; }>) => void): 准备成功或等待结束时完成 Promise 的函数。
     *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
     */
    (resolve, reject) => {
      const child = spawn(process.execPath, ["--input-type=module", "--eval", script], {
        cwd: process.cwd(),
        env: {
          ...process.env,
          AYAYA_MANAGE_BACKEND: "true",
          AYAYA_PYTHON_EXECUTABLE: "Z:\\definitely-missing\\python.exe",
          AYAYA_BACKEND_CWD: process.cwd(),
        },
        stdio: ["ignore", "pipe", "pipe"],
      });
      let stdout = "";
      let stderr = "";
      child.stdout.setEncoding("utf8");
      child.stderr.setEncoding("utf8");
      child.stdout.on("data", /**
       * 由 Electron 或子进程在 data 到达时调用，取得本用例指定的条目或响应，供外层断言处理结果。
       *
       * 此定义用于：由 Promise 构造器立即调用，按本次条目的条件完成当前转换或状态检查。
       *
       * Args:
       *     chunk (string): 子进程刚到达的文本或字节片段。
       */
      (chunk: string) => {
        stdout += chunk;
      });
      child.stderr.on("data", /**
       * 由 Electron 或子进程在 data 到达时调用，取得本用例指定的条目或响应，供外层断言处理结果。
       *
       * 此定义用于：由 Promise 构造器立即调用，按本次条目的条件完成当前转换或状态检查。
       *
       * Args:
       *     chunk (string): 子进程刚到达的文本或字节片段。
       */
      (chunk: string) => {
        stderr += chunk;
      });
      child.once("error", reject);
      child.once("close", /**
       * 由 Electron 或子进程在 close 到达时调用，结束本次 Promise 等待。
       *
       * 此定义用于：由 Promise 构造器立即调用，按本次条目的条件完成当前转换或状态检查。
       *
       * Args:
       *     code (number | null): 子进程退出码，信号退出时可能为空。
       */
      (code) => resolve({ code, stdout, stderr }));
    },
  );

  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /REJECTED:.*ENOENT/);
  assert.doesNotMatch(result.stderr, /Unhandled 'error' event/);
});

test("默认直接使用解析出的 ayaya Python 启动后端", /**
 * 验证默认直接使用解析出的 ayaya Python 启动后端。
 */
async () => {
  const harness = createHarness();

  await harness.controller.startBackend();

  assert.equal(harness.spawnCalls[0]?.command, "D:\\Miniconda\\envs\\ayaya\\python.exe");
  assert.deepEqual(harness.spawnCalls[0]?.args, [
    "-B",
    "-m",
    "app.server",
  ]);
  assert.equal(
    (harness.spawnCalls[0]?.options.env as NodeJS.ProcessEnv).AYAYA_PARENT_PID,
    String(process.pid),
  );
  assert.deepEqual(harness.spawnCalls[0]?.options.stdio, ["ignore", "pipe", "pipe"]);
});

test("托管后端持续解析 stdout 和 stderr，退出时刷新残留行", /**
 * 验证托管后端持续解析 stdout 和 stderr，退出时刷新残留行。
 */
async () => {
  const harness = createHarness();
  await harness.controller.startBackend();

  harness.child.stdout.write("application ");
  harness.child.stdout.write("ready\n");
  harness.child.stderr.write("INFO: Uvicorn running\r\nERROR: broken");
  harness.child.stdout.end();
  harness.child.stderr.end();
  await new Promise<void>(/**
   * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
   *
   * 此定义用于：验证托管后端持续解析 stdout 和 stderr，退出时刷新残留行。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => setImmediate(resolve));

  assert.deepEqual(harness.backendLogs, [
    { source: "stdout", level: "info", message: "application ready" },
    { source: "stderr", level: "info", message: "INFO: Uvicorn running" },
    { source: "stderr", level: "error", message: "ERROR: broken" },
  ]);
});

test("外部后端模式不创建 Python，并写入无法捕获外部日志的提示", /**
 * 验证外部后端模式不创建 Python，并写入无法捕获外部日志的提示。
 */
async () => {
  const harness = createHarness({
    env: {
      AYAYA_MANAGE_BACKEND: "false",
      AYAYA_API_TOKEN: "e".repeat(43),
    },
  });

  await harness.controller.startBackend();

  assert.equal(harness.children.length, 0);
  assert.match(harness.backendLogs[0]?.message ?? "", /无法捕获外部 Python/);
});

test("AYAYA_PYTHON_EXECUTABLE 覆盖默认 Conda 命令", /**
 * 验证AYAYA_PYTHON_EXECUTABLE 覆盖默认 Conda 命令。
 */
async () => {
  const harness = createHarness({
    env: { AYAYA_PYTHON_EXECUTABLE: "D:\\Python\\python.exe" },
    resolvedPython: {
      executable: "D:\\Python\\python.exe",
      environmentRoot: "D:\\Python",
    },
  });

  await harness.controller.startBackend();

  assert.equal(harness.spawnCalls[0]?.command, "D:\\Python\\python.exe");
  assert.deepEqual(harness.spawnCalls[0]?.args, ["-B", "-m", "app.server"]);
});

test("子进程 error 会拒绝启动并清理进程引用", /**
 * 验证子进程 error 会拒绝启动并清理进程引用。
 */
async () => {
  const harness = createHarness({ /**
   * 手动让当前测试后端进入就绪状态。
   *
   * 此定义用于：验证子进程 error 会拒绝启动并清理进程引用。
   */
  ready: () => new Promise(/**
   * 由 Promise 构造器立即调用，交付当前 undefined。
   *
   * 此定义用于：手动让当前测试后端进入就绪状态。
   */
  () => undefined) });
  const startup = harness.controller.startBackend();
  await waitForSpawn();

  harness.child.pid = undefined;
  harness.child.emit("error", Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" }));

  await assert.rejects(startup, /ENOENT/);
  assert.equal(harness.terminated.length, 1);
  await harness.controller.stopBackend();
  assert.equal(harness.terminated.length, 1);
});

test("子进程在就绪前退出会立即拒绝启动", /**
 * 验证子进程在就绪前退出会立即拒绝启动。
 */
async () => {
  const harness = createHarness({ /**
   * 手动让当前测试后端进入就绪状态。
   *
   * 此定义用于：验证子进程在就绪前退出会立即拒绝启动。
   */
  ready: () => new Promise(/**
   * 由 Promise 构造器立即调用，交付当前 undefined。
   *
   * 此定义用于：手动让当前测试后端进入就绪状态。
   */
  () => undefined) });
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证子进程在就绪前退出会立即拒绝启动。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  const startup = harness.controller.startBackend();
  await waitForSpawn();

  harness.child.exitCode = 1;
  harness.child.emit("exit", 1, null);

  await assert.rejects(startup, /就绪前退出/);
  assert.equal(harness.terminated.length, 1);
  assert.deepEqual(exits, []);
});

test("等待 ready 超时后会终止并清空受管进程", /**
 * 验证等待 ready 超时后会终止并清空受管进程。
 */
async () => {
  const harness = createHarness({
    /**
     * 手动让当前测试后端进入就绪状态。
     *
     * 此定义用于：验证等待 ready 超时后会终止并清空受管进程。
     */
    ready: async () => {
      throw new Error("connection refused");
    },
    startupTimeoutMs: 10,
  });

  await assert.rejects(harness.controller.startBackend(), /等待后端就绪超时/);

  assert.equal(harness.terminated.length, 1);
  await harness.controller.stopBackend();
  assert.equal(harness.terminated.length, 1);
});

test("ready 成功后保留后端进程", /**
 * 验证ready 成功后保留后端进程。
 */
async () => {
  const harness = createHarness();

  await harness.controller.startBackend();

  assert.equal(harness.terminated.length, 0);
});

test("ready 成功后的进程 error 会被安全记录", /**
 * 验证ready 成功后的进程 error 会被安全记录。
 */
async () => {
  const harness = createHarness();
  await harness.controller.startBackend();
  const error = new Error("late process error");

  harness.child.emit("error", error);

  assert.deepEqual(harness.loggedErrors, [error]);
});

test("ready 后异常退出会清空当前进程并发布一次事件", /**
 * 验证ready 后异常退出会清空当前进程并发布一次事件。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证ready 后异常退出会清空当前进程并发布一次事件。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();

  harness.child.exitCode = 1;
  harness.child.emit("exit", 1, null);
  harness.child.emit("exit", 1, null);

  assert.deepEqual(exits, [{ exitCode: 1, signal: null, message: "后端进程意外退出（退出码 1）" }]);
  await harness.controller.startBackend();
  assert.equal(harness.spawnCalls.length, 2);
});

test("ready 后被信号终止会发布信号详情", /**
 * 验证ready 后被信号终止会发布信号详情。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证ready 后被信号终止会发布信号详情。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();

  harness.child.signalCode = "SIGTERM";
  harness.child.emit("exit", null, "SIGTERM");

  assert.deepEqual(exits, [{
    exitCode: null,
    signal: "SIGTERM",
    message: "后端进程意外退出（信号 SIGTERM）",
  }]);
});

test("ready 后未经 stop 的退出码 0 仍视为异常", /**
 * 验证ready 后未经 stop 的退出码 0 仍视为异常。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证ready 后未经 stop 的退出码 0 仍视为异常。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();

  harness.child.exitCode = 0;
  harness.child.emit("exit", 0, null);

  assert.equal(exits.length, 1);
  assert.equal(exits[0]?.exitCode, 0);
});

test("正常 stopBackend 导致的退出不会发布异常事件", /**
 * 验证正常 stopBackend 导致的退出不会发布异常事件。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证正常 stopBackend 导致的退出不会发布异常事件。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();

  const current = harness.child;
  const stopping = harness.controller.stopBackend();
  current.exitCode = 0;
  current.emit("exit", 0, null);
  await stopping;

  assert.deepEqual(exits, []);
});

test("取消异常退出订阅后不再接收事件", /**
 * 验证取消异常退出订阅后不再接收事件。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  const unsubscribe = harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证取消异常退出订阅后不再接收事件。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();
  unsubscribe();

  harness.child.exitCode = 1;
  harness.child.emit("exit", 1, null);

  assert.deepEqual(exits, []);
});

test("旧 generation 的延迟事件不会清空新进程", /**
 * 验证旧 generation 的延迟事件不会清空新进程。
 */
async () => {
  const harness = createHarness();
  const exits: BackendUnexpectedExit[] = [];
  harness.controller.onUnexpectedExit(/**
   * 由外层操作在对应处理阶段调用，追加当前记录。
   *
   * 此定义用于：验证旧 generation 的延迟事件不会清空新进程。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  (event) => exits.push(event));
  await harness.controller.startBackend();
  const oldChild = harness.child;
  oldChild.exitCode = 1;
  oldChild.emit("exit", 1, null);

  await harness.controller.startBackend();
  const current = harness.child;
  oldChild.emit("exit", 2, null);
  const stopping = harness.controller.stopBackend();
  current.exitCode = 0;
  current.emit("exit", 0, null);
  await stopping;

  assert.equal(exits.length, 1);
  assert.equal(harness.shutdownCalls, 1);
});

test("优雅退出超时后只强制终止一次", /**
 * 验证优雅退出超时后只强制终止一次。
 */
async () => {
  const harness = createHarness({ shutdownTimeoutMs: 1 });
  await harness.controller.startBackend();

  await harness.controller.stopBackend();
  await harness.controller.stopBackend();

  assert.equal(harness.shutdownCalls, 1);
  assert.equal(harness.terminated.length, 1);
});

test("环境解析期间停止后端会取消启动且不创建 Python", /**
 * 验证环境解析期间停止后端会取消启动且不创建 Python。
 */
async () => {
  let aborted = false;
  const harness = createHarness({
    /**
     * 按测试脚本解析 Python 路径，可延迟或取消以验证启动边界。
     *
     * 此定义用于：验证环境解析期间停止后端会取消启动且不创建 Python。
     *
     * Args:
     *     signal (AbortSignal | undefined): 调用方提供的取消信号。
     */
    resolvePythonExecutable: (signal) => new Promise(/**
     * 由 Promise 构造器立即调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：按测试脚本解析 Python 路径，可延迟或取消以验证启动边界。
     *
     * Args:
     *     _resolve ((value: { executable: string; environmentRoot: string; } | PromiseLike<{ executable: string; environmentRoot: string; }>) => void): 当前分支不使用的 Promise 完成函数。
     *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
     */
    (_resolve, reject) => {
      signal?.addEventListener("abort", /**
       * 由浏览器在请求取消时调用，以失败结果结束本次 Promise 等待。
       *
       * 此定义用于：由 Promise 构造器立即调用，按本次条目的条件完成当前转换或状态检查。
       */
      () => {
        aborted = true;
        reject(new Error("后端启动已取消"));
      }, { once: true });
    }),
  });
  const startup = harness.controller.startBackend();
  const rejected = assert.rejects(startup, /后端启动已取消/);

  await harness.controller.stopBackend();
  await rejected;

  assert.equal(aborted, true);
  assert.equal(harness.spawnCalls.length, 0);
});
