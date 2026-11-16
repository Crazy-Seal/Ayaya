import { BackendStartupCancelledError } from "./backend-errors.js";
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
  isProcessAlive: (pid: number) => boolean;
  terminateProcess: (child: ChildProcess) => Promise<void>;
  pollIntervalMs: number;
  startupTimeoutMs: number;
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
   * 当前 SSE 连接建立前刷新外部后端身份。
   * Args:
   *     signal (AbortSignal): 当前订阅的取消信号，旧请求随订阅失效。
   */
  refreshExternalBackendIdentity(signal: AbortSignal): Promise<void>;
  /** SSE 断线时确认已连接的外部进程是否退出，存活进程继续使用普通重连。 */
  checkExternalBackendExit(): void;
  /**
   *  启动受管后端，或连接已由外部启动的后端。
   */
  startBackend(): Promise<void>;

  /**
   * 请求后端正常退出，等待已经开始的保存和资源关闭完成。
   */
  stopBackend(): Promise<void>;

  /** 用户在退出等待期间主动强制终止受管后端。 */
  forceStopBackend(): Promise<void>;

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
      reject(new BackendStartupCancelledError());
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
      reject(new BackendStartupCancelledError());
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
    if (!child.kill("SIGKILL")) throw new Error("无法向后端发送终止信号");
    return;
  }

  await new Promise<void>(/**
   * Promise 创建时启动隐藏的 taskkill 辅助进程，等待退出或错误事件后结束终止操作。
   *
   * Args:
   *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
   */
  (resolve, reject) => {
    const taskkill = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], {
      windowsHide: true,
      stdio: "ignore",
    });
    taskkill.once("error", reject);
    taskkill.once("exit", /** 终止命令失败且后端仍在运行时，将真实错误交给退出界面。 */
    (code) => {
      if (code === 0 || !isRunning(child)) resolve();
      else reject(new Error(`强制终止后端失败，taskkill 退出码：${code}`));
    });
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
  /**
   * 查询本机进程是否存在，权限不足时保留其存活判断。
   * Args:
   *     pid (number): 成功就绪时由本机后端提供的进程编号。
   * Returns:
   *     boolean: 仅进程明确不存在时返回 false。
   */
  isProcessAlive: (pid) => {
    try { process.kill(pid, 0); return true; }
    catch (error) { return (error as NodeJS.ErrnoException).code !== "ESRCH"; }
  },
  terminateProcess: terminateProcessTree,
  pollIntervalMs: DEFAULT_POLL_INTERVAL_MS,
  startupTimeoutMs: DEFAULT_STARTUP_TIMEOUT_MS,
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
 * 等待受管进程实际退出，允许后端完成已经开始的事务确认。
 * Args:
 *     child (ChildProcess): 本次管理的后端进程。
 * Returns:
 *     Promise<void>: 收到进程退出事件后完成。
 */
const waitForExit = async (child: ChildProcess): Promise<void> => {
  if (!isRunning(child)) return;
  await new Promise<void>(/** 登记正常或强制结束后的进程退出通知。 */
  (resolve) => { child.once("exit", /** 收到进程退出通知后完成等待。 */ () => resolve()); });
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
  let externalPid: number | null = null;
  let stopping: Promise<void> | null = null;
  let forcing: Promise<void> | null = null;

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
        const response = await dependencies.fetchBackend("/internal/ready", { signal });
        if (response.ok) {
          if (!managed) await rememberExternalIdentity(response, signal);
          return;
        }
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
    throw new BackendStartupCancelledError();
  };

  /**
   * 接受当前订阅仍有效的就绪响应，记录准确外部进程身份。
   * Args:
   *     response (Response): 当前本机后端的就绪响应。
   *     signal (AbortSignal): 对应启动或订阅的取消信号。
   */
  const rememberExternalIdentity = async (response: Response, signal: AbortSignal): Promise<void> => {
    const body = await response.json() as { ready: boolean; pid: number };
    if (signal.aborted) return;
    if (body.ready !== true || !Number.isSafeInteger(body.pid) || body.pid <= 0) {
      throw new Error("后端就绪响应缺少有效进程编号");
    }
    externalPid = body.pid;
  };

  /**
   * SSE 首次连接或重连前刷新外部进程身份，受管进程沿已有子进程事件处理。
   * Args:
   *     signal (AbortSignal): 当前连接取消信号。
   */
  const refreshExternalBackendIdentity = async (signal: AbortSignal): Promise<void> => {
    if (managed || signal.aborted) return;
    const response = await dependencies.fetchBackend("/internal/ready", { signal });
    if (!response.ok) throw new Error(`读取外部后端身份失败: ${response.status}`);
    await rememberExternalIdentity(response, signal);
  };

  /** 断线时仅确认当前外部进程死亡后发布一次现有恢复通知。 */
  const checkExternalBackendExit = (): void => {
    if (managed || externalPid === null || dependencies.isProcessAlive(externalPid)) return;
    externalPid = null;
    publishUnexpectedExit({ exitCode: null, signal: null, message: "外部后端进程意外退出" });
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
    externalPid = null;
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
        if (abortController.signal.aborted) throw new BackendStartupCancelledError();
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
   * 先取消未完成启动，再请求关闭并等待受管进程实际退出。
   *
   * Returns:
   *     Promise<void>: 本次停止过程结束后完成。
   */
  const performStop = async (): Promise<void> => {
    externalPid = null;
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
    const current = generation.child;
    const shutdownAbort = new AbortController();
    // 2. 请求通知后端开始关闭，完成与否以实际进程退出为准。
    const exiting = waitForExit(current);
    /** 请求后端开始关闭，接口报错或迟迟不返回时，进程退出观察仍独立继续。 */
    const requestBackendShutdown = async (): Promise<void> => {
      try {
        await dependencies.fetchBackend("/internal/shutdown", { method: "POST", signal: shutdownAbort.signal });
      } catch {
        // 关闭请求失败后继续等待实际进程退出，用户可在等待界面主动强退。
      }
    };
    try {
      void requestBackendShutdown();
      await exiting;
    } finally {
      // 3. 实际进程退出后，中止尚未完成的关闭请求并释放登记。
      shutdownAbort.abort();
      if (activeGeneration === generation) activeGeneration = null;
    }
  };

  /** 合并同时到达的正常关闭请求，进程退出后释放等待句柄。 */
  const stopBackend = (): Promise<void> => {
    stopping ??= performStop().finally(/** 本次停止完成后释放等待句柄。 */
    () => { stopping = null; });
    return stopping;
  };

  /** 执行用户明确要求的强制结束，重复点击共用同一次进程终止。 */
  const forceStopBackend = (): Promise<void> => {
    if (forcing) return forcing;
    const generation = activeGeneration;
    if (!managed || !generation || !isRunning(generation.child)) return Promise.resolve();
    generation.expectedExit = true;
    forcing = dependencies.terminateProcess(generation.child).finally(/** 结束本次强制操作后释放句柄。 */
    () => { forcing = null; });
    return forcing;
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

  return { startBackend, stopBackend, forceStopBackend, onUnexpectedExit,
           refreshExternalBackendIdentity, checkExternalBackendExit };
};

const defaultController = createBackendProcessController();

/**
 * 为当前展示连接刷新外部后端身份。
 * Args:
 *     signal (AbortSignal): 订阅控制器持有的连接取消信号。
 */
export const refreshExternalBackendIdentity = (signal: AbortSignal): Promise<void> =>
  defaultController.refreshExternalBackendIdentity(signal);

/** 将外部后端的真实退出交给已有恢复通路。 */
export const checkExternalBackendExit = (): void => defaultController.checkExternalBackendExit();

/**
 *  启动或连接受认证保护的本地后端。
 */
export const startBackend = (): Promise<void> => defaultController.startBackend();

/**
 * 请求后端正常退出并等待保存和资源关闭完成。
 */
export const stopBackend = (): Promise<void> => defaultController.stopBackend();

/** 用户明确选择后强制结束受管后端。 */
export const forceStopBackend = (): Promise<void> => defaultController.forceStopBackend();

/**
 *  订阅默认受管后端成功就绪后的非预期退出事件。
 *
 * Args:
 *     listener (BackendUnexpectedExitListener): 接收后续事件的回调，取消订阅后不再调用。
 */
export const onBackendUnexpectedExit = (
  listener: BackendUnexpectedExitListener,
): (() => void) => defaultController.onUnexpectedExit(listener);
