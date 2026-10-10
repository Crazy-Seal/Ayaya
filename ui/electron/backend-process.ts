import {
  spawn,
  type ChildProcess,
  type SpawnOptions,
} from "node:child_process";
import { randomBytes } from "node:crypto";

import type { LogLevel } from "../shared-types.js";
import { AYAYA_BACKEND_BASE_URL, WORKSPACE_ROOT } from "./config.js";
import { backendFetch, configureBackendClient } from "./backend-client.js";
import {
  buildBackendPythonEnvironment,
  createBackendPythonResolver,
  type ResolvedBackendPython,
} from "./backend-python.js";
import {
  appendBackendLog,
  appendFrontendLog,
  registerSensitiveLogValue,
} from "./logging/app-logger.js";
import { BackendLineDecoder, inferBackendLevel } from "./logging/log-core.js";

const DEFAULT_STARTUP_TIMEOUT_MS = 30_000;
const DEFAULT_POLL_INTERVAL_MS = 200;
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 5_000;

type SpawnProcess = (
  command: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcess;

/**
 * 约定后端进程启动、健康检查、认证配置、日志和退出处理所需的可替换依赖。
 */
interface BackendProcessDependencies {
  env: NodeJS.ProcessEnv;
  workspaceRoot: string;
  backendBaseUrl: string;
  spawnProcess: SpawnProcess;
  configureClient: (token: string) => void;
  createToken: () => string;
  resolvePythonExecutable: (signal?: AbortSignal) => Promise<ResolvedBackendPython>;
  fetchBackend: (path: string, init?: RequestInit) => Promise<Response>;
  terminateProcess: (child: ChildProcess) => Promise<void>;
  pollIntervalMs: number;
  startupTimeoutMs: number;
  shutdownTimeoutMs: number;
  platform: NodeJS.Platform;
  ownerPid: number;
  logError: (message: string, error: unknown) => void;
  appendBackendLog: (
    source: "stdout" | "stderr",
    level: LogLevel,
    message: string,
  ) => void;
  registerSensitiveValue: (value: string) => void;
}

/**
 *  后端进程在成功就绪后的非预期退出信息。
 */
export interface BackendUnexpectedExit {
  exitCode: number | null;
  signal: NodeJS.Signals | null;
  message: string;
}

type BackendUnexpectedExitListener = (event: BackendUnexpectedExit) => void;

/**
 *  Electron 后端进程控制器。
 */
export interface BackendProcessController {
  /**
   *  启动受管后端，或连接已由外部启动的后端。
   */
  startBackend(): Promise<void>;

  /**
   *  请求后端优雅退出，并在超时后终止受管子进程。
   */
  stopBackend(): Promise<void>;

  /**
   *  订阅成功就绪后的非预期退出事件。
   *
   * Args:
   *     listener (BackendUnexpectedExitListener): 接收后续事件的回调，取消订阅后不再调用。
   */
  onUnexpectedExit(listener: BackendUnexpectedExitListener): () => void;
}

/**
 * 保存本次启动的子进程及就绪、预期退出和异常通知状态，用于排除旧进程事件。
 */
interface ManagedBackendGeneration {
  child: ChildProcess;
  startupComplete: boolean;
  expectedExit: boolean;
  unexpectedExitReported: boolean;
}

/**
 * 根据退出码和退出信号判断子进程是否仍需管理。
 *
 * Args:
 *     child (ChildProcess): 本次管理的子进程。
 *
 * Returns:
 *     boolean: 尚未收到退出码或信号时为 true。
 */
const isRunning = (child: ChildProcess): boolean =>
  child.exitCode === null && child.signalCode === null;

/**
 * 构造后端启动被关闭流程取消时使用的错误。
 *
 * Returns:
 *     Error: 表示启动已经取消的 Error。
 */
const createCancelledError = (): Error => new Error("后端启动已取消");

/**
 * 等待重试间隔，取消信号到达时提前退出。
 *
 * Args:
 *     milliseconds (number): 本次等待的毫秒数。
 *     signal (AbortSignal): 调用方提供的取消信号。
 *
 * Returns:
 *     Promise<void>: 定时器到期时完成，取消时拒绝。
 */
const delay = (milliseconds: number, signal: AbortSignal): Promise<void> =>
  new Promise(/**
   * Promise 创建时登记重试定时器和取消监听，已经取消则直接拒绝等待。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
   */
  (resolve, reject) => {
    if (signal.aborted) {
      reject(createCancelledError());
      return;
    }
    const timer = setTimeout(/**
     * 重试间隔到期后移除取消监听，再允许下一次就绪检查。
     */
    () => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    /**
     * 取消信号到达时清除重试定时器，并使等待以启动取消错误结束。
     */
    const onAbort = (): void => {
      clearTimeout(timer);
      reject(createCancelledError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });

/**
 *  终止后端进程树，并完整消费辅助进程的生命周期事件。
 *
 * Args:
 *     child (ChildProcess): 本次管理的子进程。
 */
const terminateProcessTree = async (child: ChildProcess): Promise<void> => {
  if (!isRunning(child) || !child.pid) return;
  if (process.platform !== "win32") {
    try {
      child.kill("SIGKILL");
    } catch (error) {
      appendFrontendLog("error", "backend-process", "强制终止后端进程失败", error);
    }
    return;
  }

  await new Promise<void>(/**
   * Promise 创建时启动隐藏的 taskkill 辅助进程，等待退出或错误事件后结束终止操作。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => {
    const taskkill = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
      windowsHide: true,
      stdio: "ignore",
    });
    let settled = false;
    /**
     * 辅助终止进程退出或报错后只完成一次等待，避免重复回调影响清理。
     */
    const finish = (): void => {
      if (settled) return;
      settled = true;
      resolve();
    };
    taskkill.once("error", finish);
    taskkill.once("exit", finish);
  });
};

const defaultDependencies: BackendProcessDependencies = {
  env: process.env,
  workspaceRoot: WORKSPACE_ROOT,
  backendBaseUrl: AYAYA_BACKEND_BASE_URL,
  /**
   * 创建隐藏的后端或辅助子进程，使用调用方提供的命令及环境。
   *
   * Args:
   *     command (string): 要启动的可执行程序。
   *     args (readonly string[]): 传递给子进程的命令行参数。
   *     options (SpawnOptions): 调用方提供的配置或运行选项。
   *
   * Returns:
   *     ChildProcess: Node 子进程对象。
   */
  spawnProcess: (command, args, options) => spawn(command, args, options),
  configureClient: configureBackendClient,
  /**
   * 为本次受管后端生成随机通信凭据。
   *
   * Returns:
   *     string: 足够随机性的认证令牌。
   */
  createToken: () => randomBytes(32).toString("base64url"),
  resolvePythonExecutable: createBackendPythonResolver().resolve,
  fetchBackend: backendFetch,
  terminateProcess: terminateProcessTree,
  pollIntervalMs: DEFAULT_POLL_INTERVAL_MS,
  startupTimeoutMs: DEFAULT_STARTUP_TIMEOUT_MS,
  shutdownTimeoutMs: DEFAULT_SHUTDOWN_TIMEOUT_MS,
  platform: process.platform,
  ownerPid: process.pid,
  /**
   * 将后端进程管理中的错误写入 Electron 日志。
   *
   * Args:
   *     message (string): 当前需要显示或记录的文本。
   *     error (unknown): 本次捕获的异常。
   */
  logError: (message, error) => appendFrontendLog("error", "backend-process", message, error),
  appendBackendLog,
  registerSensitiveValue: registerSensitiveLogValue,
};

/**
 * 等待子进程退出或宽限时间到期，超时后的强制终止由调用方决定。
 *
 * Args:
 *     child (ChildProcess): 本次管理的子进程。
 *     timeoutMs (number): 本次等待的最长毫秒数。
 *
 * Returns:
 *     Promise<void>: 收到退出事件或达到等待期限后完成。
 */
const waitForExit = async (child: ChildProcess, timeoutMs: number): Promise<void> => {
  if (!isRunning(child)) return;
  await new Promise<void>(/**
   * Promise 创建时登记子进程退出监听及宽限定时器，两者任一个到达都结束等待。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve) => {
    let settled = false;
    /**
     * 子进程退出或宽限到期后结束等待，并清理退出监听与定时器。
     */
    const finish = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener("exit", finish);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    child.once("exit", finish);
  });
};

/**
 * 创建隔离的后端进程控制器。
 *
 * Args:
 *     overrides (Partial<BackendProcessDependencies>): 仅供 Electron 组装和单元测试替换的进程、网络与时钟依赖。
 *
 * Returns:
 *     BackendProcessController: 拥有独立子进程状态的后端进程控制器。
 */
export const createBackendProcessController = (
  overrides: Partial<BackendProcessDependencies> = {},
): BackendProcessController => {
  const dependencies = { ...defaultDependencies, ...overrides };
  if (!overrides.resolvePythonExecutable && overrides.env) {
    dependencies.resolvePythonExecutable = createBackendPythonResolver({
      env: dependencies.env,
      platform: dependencies.platform,
      spawnProcess: dependencies.spawnProcess,
    }).resolve;
  }
  const unexpectedExitListeners = new Set<BackendUnexpectedExitListener>();
  let activeGeneration: ManagedBackendGeneration | null = null;
  let activeStartup: {
    abortController: AbortController;
    completion: Promise<void>;
  } | null = null;
  let managed = false;

  /**
   * 后端就绪后意外退出时通知所有监听者，某个监听者失败不影响其他监听者。
   *
   * Args:
   *     event (BackendUnexpectedExit): 调用方或浏览器触发的当前事件。
   */
  const publishUnexpectedExit = (event: BackendUnexpectedExit): void => {
    dependencies.logError(event.message, event);
    for (const listener of unexpectedExitListeners) {
      try {
        listener(event);
      } catch (error) {
        dependencies.logError("处理后端异常退出事件失败", error);
      }
    }
  };

  /**
   * 反复检查带认证的就绪接口，服务令牌不匹配则立即失败。
   *
   * Args:
   *     signal (AbortSignal): 调用方提供的取消信号。
   *
   * Returns:
   *     Promise<void>: 就绪接口成功后完成，取消或令牌错误时拒绝。
   */
  const waitForReady = async (signal: AbortSignal): Promise<void> => {
    while (!signal.aborted) {
      try {
        const response = await dependencies.fetchBackend("/internal/ready");
        if (response.ok) return;
        if (response.status === 401) {
          throw new Error("端口上的服务不接受当前 API Token");
        }
      } catch (error) {
        if (error instanceof Error && error.message.includes("API Token")) {
          throw error;
        }
      }
      await delay(dependencies.pollIntervalMs, signal);
    }
    throw createCancelledError();
  };

  /**
   * 配置本次认证，按模式启动或连接后端，同时等待就绪、进程失败和超时；失败时回收受管进程。
   *
   * Args:
   *     abortController (AbortController): 本次启动或订阅的取消控制器。
   *
   * Returns:
   *     Promise<void>: 后端就绪后完成；启动失败或取消时拒绝。
   */
  const performStart = async (abortController: AbortController): Promise<void> => {
    if (activeGeneration && isRunning(activeGeneration.child)) {
      throw new Error("后端进程已经启动");
    }
    activeGeneration = null;

    // 1. 每次受管启动使用新令牌；外部后端模式要求调用方已提供令牌，再配置统一请求客户端。
    managed = dependencies.env.AYAYA_MANAGE_BACKEND !== "false";
    const token = managed
      ? dependencies.createToken()
      : (dependencies.env.AYAYA_API_TOKEN ?? "");
    if (!managed && token.length < 43) {
      throw new Error("外部后端模式必须显式提供至少 256 bit 的 AYAYA_API_TOKEN");
    }
    dependencies.configureClient(token);
    dependencies.registerSensitiveValue(token);

    let generation: ManagedBackendGeneration | null = null;
    let timeout: NodeJS.Timeout | undefined;
    try {
      const contenders: Array<Promise<void>> = [];
      if (managed) {
        // 2. 等待解释器解析完成后再创建子进程，解析期间取消就不启动 Python。
        const resolvedPython = await dependencies.resolvePythonExecutable(
          abortController.signal,
        );
        if (abortController.signal.aborted) throw createCancelledError();
        const childEnvironment = buildBackendPythonEnvironment(
          dependencies.env,
          resolvedPython,
          dependencies.platform,
        );
        childEnvironment.AYAYA_API_TOKEN = token;
        if (dependencies.platform === "win32") {
          childEnvironment.AYAYA_PARENT_PID = String(dependencies.ownerPid);
        }
        // 3. 后端继承经过整理的环境，Windows 额外绑定桌面所有者编号，窗口关闭后可自行退出。
        const current = dependencies.spawnProcess(
          resolvedPython.executable,
          ["-B", "-m", "app.server"],
          {
            cwd: dependencies.env.AYAYA_BACKEND_CWD || dependencies.workspaceRoot,
            env: childEnvironment,
            windowsHide: true,
            stdio: ["ignore", "pipe", "pipe"],
          },
        );
        const stdoutDecoder = new BackendLineDecoder(/**
         * stdout 解码器交付完整行后识别日志级别，记录为后端标准输出。
         *
         * Args:
         *     line (string): 已经拼接完整的一行后端输出。
         */
        (line) => {
          dependencies.appendBackendLog("stdout", inferBackendLevel(line), line);
        });
        const stderrDecoder = new BackendLineDecoder(/**
         * stderr 解码器交付完整行后按正文识别级别，不把全部标准错误输出都当作异常。
         *
         * Args:
         *     line (string): 已经拼接完整的一行后端输出。
         */
        (line) => {
          dependencies.appendBackendLog("stderr", inferBackendLevel(line), line);
        });
        current.stdout?.on("data", /**
         * 子进程 stdout 字节到达后交给独立解码器，等待拼接完整日志行。
         *
         * Args:
         *     chunk (Buffer | string): 子进程刚到达的文本或字节片段。
         */
        (chunk: Buffer | string) => stdoutDecoder.write(chunk));
        current.stderr?.on("data", /**
         * 子进程 stderr 字节到达后交给独立解码器，防止与 stdout 半行混合。
         *
         * Args:
         *     chunk (Buffer | string): 子进程刚到达的文本或字节片段。
         */
        (chunk: Buffer | string) => stderrDecoder.write(chunk));
        current.stdout?.once("end", /**
         * stdout 结束后释放末尾没有换行的日志行。
         */
        () => stdoutDecoder.end());
        current.stderr?.once("end", /**
         * stderr 结束后释放末尾没有换行的日志行。
         */
        () => stderrDecoder.end());
        generation = {
          child: current,
          startupComplete: false,
          expectedExit: false,
          unexpectedExitReported: false,
        };
        activeGeneration = generation;
        const currentGeneration = generation;

        contenders.push(
          new Promise<void>(/**
           * Promise 创建时登记当前子进程错误和退出监听，让就绪前失败能够结束启动等待。
           *
           * Args:
           *     _resolve ((value: void | PromiseLike<void>) => void): 当前分支不使用的 Promise 完成函数。
           *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
           */
          (_resolve, reject) => {
            current.on("error", /**
             * 子进程 error 事件到达时，就绪前拒绝启动；就绪后只报告错误，退出仍由 exit 事件处理。
             *
             * Args:
             *     error (Error): 本次捕获的异常。
             */
            (error) => {
              if (!currentGeneration.startupComplete) {
                reject(error);
                return;
              }
              dependencies.logError("后端进程在就绪后报告错误", error);
            });
            current.once("exit", /**
             * 子进程退出时，就绪前拒绝启动；就绪后排除正常关闭和旧进程事件，只发布一次意外退出通知。
             *
             * Args:
             *     code (number | null): 子进程退出码，信号退出时可能为空。
             *     signal (Signals | null): 进程退出时的系统信号，无信号退出时为空。
             */
            (code, signal) => {
              if (!currentGeneration.startupComplete) {
                const detail = signal ? `信号 ${signal}` : `退出码 ${String(code)}`;
                reject(new Error(`后端进程在就绪前退出（${detail}）`));
                return;
              }
              if (
                currentGeneration.expectedExit
                || currentGeneration.unexpectedExitReported
                || activeGeneration !== currentGeneration
              ) return;

              currentGeneration.unexpectedExitReported = true;
              activeGeneration = null;
              const detail = signal ? `信号 ${signal}` : `退出码 ${String(code)}`;
              publishUnexpectedExit({
                exitCode: code,
                signal,
                message: `后端进程意外退出（${detail}）`,
              });
            });
          }),
        );
      }
      if (!managed) {
        dependencies.appendBackendLog(
          "stderr",
          "info",
          "当前为外部后端模式，Electron 无法捕获外部 Python 进程的实时日志",
        );
      }

      contenders.unshift(waitForReady(abortController.signal));
      contenders.push(
        new Promise<void>(/**
         * Promise 创建时设置启动总超时，与就绪和进程失败共同决定启动结果。
         *
         * Args:
         *     _resolve ((value: void | PromiseLike<void>) => void): 当前分支不使用的 Promise 完成函数。
         *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
         */
        (_resolve, reject) => {
          timeout = setTimeout(
            /**
             * 启动总等待期限到达时拒绝就绪等待，由启动收尾流程负责终止受管进程。
             */
            () => reject(new Error(`等待后端就绪超时: ${dependencies.backendBaseUrl}`)),
            dependencies.startupTimeoutMs,
          );
        }),
      );

      // 4. 就绪、进程失败和超时共同结束启动等待；成功就绪后才把后续退出视为运行异常。
      await Promise.race(contenders);
      if (generation) generation.startupComplete = true;
    } catch (error) {
      // 5. 启动失败先标记预期退出，再终止本次进程，清理过程不会重复触发用户恢复提示。
      if (generation) {
        generation.expectedExit = true;
        if (activeGeneration === generation) activeGeneration = null;
        await dependencies.terminateProcess(generation.child);
      }
      throw error;
    } finally {
      abortController.abort();
      if (timeout) clearTimeout(timeout);
    }
  };

  /**
   * 为一次后端启动创建取消控制器，拒绝与尚未结束的启动流程重叠。
   *
   * Returns:
   *     Promise<void>: 本次启动和就绪检查的完成结果。
   */
  const startBackend = (): Promise<void> => {
    if (activeStartup) {
      throw new Error("后端正在启动");
    }
    const abortController = new AbortController();
    const startup = {
      abortController,
      completion: Promise.resolve(),
    };
    const completion = performStart(abortController).finally(/**
     * 本次启动成功或失败后，仅在仍指向同一启动流程时清除启动记录。
     */
    () => {
      if (activeStartup === startup) activeStartup = null;
    });
    startup.completion = completion;
    activeStartup = startup;
    return completion;
  };

  /**
   * 先取消未完成启动，再请求后端正常退出，宽限结束仍存活时终止受管进程。
   *
   * Returns:
   *     Promise<void>: 本次停止过程结束后完成。
   */
  const stopBackend = async (): Promise<void> => {
    // 1. 启动尚未结束时先取消并等其清理完成，再检查是否还有受管进程需要停止。
    const startup = activeStartup;
    if (startup) {
      startup.abortController.abort();
      try {
        await startup.completion;
      } catch {
        // 启动取消或失败已由启动调用方处理，继续检查是否仍有受管进程。
      }
    }
    if (!managed || !activeGeneration) return;
    const generation = activeGeneration;
    generation.expectedExit = true;
    activeGeneration = null;
    const current = generation.child;
    try {
      await dependencies.fetchBackend("/internal/shutdown", { method: "POST" });
    } catch {
      // 后端可能已经退出，继续等待进程状态即可。
    }
    // 2. 已请求后端关闭；宽限期结束仍存活才强制终止进程。
    await waitForExit(current, dependencies.shutdownTimeoutMs);
    if (isRunning(current)) {
      await dependencies.terminateProcess(current);
    }
  };

  /**
   * 登记成功就绪后的异常退出监听者，正常停止不触发此通知。
   *
   * Args:
   *     listener (BackendUnexpectedExitListener): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     (() => void): 移除该监听者的取消订阅函数。
   */
  const onUnexpectedExit = (listener: BackendUnexpectedExitListener): (() => void) => {
    unexpectedExitListeners.add(listener);
    /**
     * 由调用方取消异常退出订阅时移除当前监听者。
     */
    return () => unexpectedExitListeners.delete(listener);
  };

  return { startBackend, stopBackend, onUnexpectedExit };
};

const defaultController = createBackendProcessController();

/**
 *  启动或连接受认证保护的本地后端。
 */
export const startBackend = (): Promise<void> => defaultController.startBackend();

/**
 *  请求后端优雅退出，并在超时后终止受管子进程。
 */
export const stopBackend = (): Promise<void> => defaultController.stopBackend();

/**
 *  订阅默认受管后端成功就绪后的非预期退出事件。
 *
 * Args:
 *     listener (BackendUnexpectedExitListener): 接收后续事件的回调，取消订阅后不再调用。
 */
export const onBackendUnexpectedExit = (
  listener: BackendUnexpectedExitListener,
): (() => void) => defaultController.onUnexpectedExit(listener);
