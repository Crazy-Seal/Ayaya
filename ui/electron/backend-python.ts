import { spawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import { access } from "node:fs/promises";
import path from "node:path";

const DEFAULT_DISCOVERY_TIMEOUT_MS = 10_000;
const MAX_STDOUT_BYTES = 1024 * 1024;
const MAX_STDERR_BYTES = 64 * 1024;

type SpawnProcess = (
  command: string,
  args: readonly string[],
  options: SpawnOptions,
) => ChildProcess;

/**
 * 约定环境变量、文件存在性检查及 Conda 查询子进程，供生产与测试使用。
 */
interface BackendPythonResolverDependencies {
  env: NodeJS.ProcessEnv;
  platform: NodeJS.Platform;
  spawnProcess: SpawnProcess;
  fileExists: (filePath: string) => Promise<boolean>;
  timeoutMs: number;
}

/**
 *  可直接启动后端的 Python 解释器信息。
 */
export interface ResolvedBackendPython {
  executable: string;
  environmentRoot?: string;
}

/**
 *  后端 Python 解释器解析器。
 */
export interface BackendPythonResolver {
  /**
   *  解析显式解释器或名为 ayaya 的 Conda 环境。
   *
   * Args:
   *     signal (AbortSignal): 调用方提供的取消信号。
   */
  resolve(signal?: AbortSignal): Promise<ResolvedBackendPython>;
}

/**
 * 检查解析出的 Python 文件是否确实存在。
 *
 * Args:
 *     filePath (string): 待检查存在性的本地文件路径。
 *
 * Returns:
 *     Promise<boolean>: 本地路径存在时为 true。
 */
const defaultFileExists = async (filePath: string): Promise<boolean> => {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
};

const defaultDependencies: BackendPythonResolverDependencies = {
  env: process.env,
  platform: process.platform,
  /**
   * 启动查询 Conda 环境的辅助进程。
   *
   * Args:
   *     command (string): 要启动的可执行程序。
   *     args (readonly string[]): 传递给子进程的命令行参数。
   *     options (SpawnOptions): 调用方提供的配置或运行选项。
   *
   * Returns:
   *     ChildProcess: 可读取输出并取消的子进程对象。
   */
  spawnProcess: (command, args, options) => spawn(command, args, options),
  fileExists: defaultFileExists,
  timeoutMs: DEFAULT_DISCOVERY_TIMEOUT_MS,
};

/**
 * 构造 Python 环境解析被关闭流程取消时使用的错误。
 *
 * Returns:
 *     Error: 表示环境查询已取消的 Error。
 */
const createCancelledError = (): Error => new Error("后端启动已取消");

/**
 * 取消环境查询时停止辅助子进程，避免退出后仍继续查询。
 *
 * Args:
 *     child (ChildProcess): 本次管理的子进程。
 */
const terminateHelperProcess = (child: ChildProcess): void => {
  try {
    child.kill();
  } catch {
    // 进程可能尚未获得 PID 或已经退出，无需覆盖原始失败原因。
  }
};

/**
 * 创建后端 Python 解释器解析器。
 *
 * Args:
 *     overrides (Partial<BackendPythonResolverDependencies>): 供 Electron 组装或测试替换的环境、进程和文件系统依赖。
 *
 * Returns:
 *     BackendPythonResolver: 仅在内存中缓存本次解析结果的解释器解析器。
 */
export const createBackendPythonResolver = (
  overrides: Partial<BackendPythonResolverDependencies> = {},
): BackendPythonResolver => {
  const dependencies = { ...defaultDependencies, ...overrides };
  let cached: ResolvedBackendPython | undefined;

  /**
   * 读取 Conda 环境 JSON，等待退出后解析；取消、超时或错误时清理辅助进程。
   *
   * Args:
   *     signal (AbortSignal): 调用方提供的取消信号。
   *
   * Returns:
   *     Promise<string>: Conda 返回的环境目录列表。
   */
  const runCondaEnvironmentList = (signal?: AbortSignal): Promise<string> =>
    new Promise<string>(/**
     * Promise 创建时启动 Conda 查询，登记输出、退出、取消和超时处理，所有路径经同一收尾函数结束。
     *
     * Args:
     *     resolve ((value: string | PromiseLike<string>) => void): 准备成功或等待结束时完成 Promise 的函数。
     *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
     */
    (resolve, reject) => {
      if (signal?.aborted) {
        reject(createCancelledError());
        return;
      }

      let child: ChildProcess;
      try {
        child = dependencies.spawnProcess("conda", ["env", "list", "--json"], {
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
        });
      } catch (error) {
        reject(error);
        return;
      }

      let settled = false;
      let stdout = "";
      let stderr = "";
      /**
       * Conda 查询成功、失败或取消时统一清理监听和定时器，再完成对应等待。
       *
       * Args:
       *     error (unknown): 本次捕获的异常。
       */
      const finish = (error?: unknown): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        if (error !== undefined) {
          reject(error);
        } else {
          resolve(stdout);
        }
      };
      /**
       * 关闭流程取消环境查询时终止辅助进程，并交给统一收尾函数拒绝等待。
       */
      const onAbort = (): void => {
        terminateHelperProcess(child);
        finish(createCancelledError());
      };
      const timer = setTimeout(/**
       * 环境查询超时后终止辅助进程，并以可读错误结束 Python 解析等待。
       */
      () => {
        terminateHelperProcess(child);
        finish(new Error("查找 ayaya Conda 环境超时"));
      }, dependencies.timeoutMs);

      signal?.addEventListener("abort", onAbort, { once: true });
      child.stdout?.setEncoding("utf8");
      child.stderr?.setEncoding("utf8");
      child.stdout?.on("data", /**
       * Conda stdout 到达后限制累积字节数，输出过大时终止查询，防止继续占用内存。
       *
       * Args:
       *     chunk (string): 子进程刚到达的文本或字节片段。
       */
      (chunk: string) => {
        if (settled) return;
        if (Buffer.byteLength(stdout) + Buffer.byteLength(chunk) > MAX_STDOUT_BYTES) {
          terminateHelperProcess(child);
          finish(new Error("Conda 环境列表输出超过大小限制"));
          return;
        }
        stdout += chunk;
      });
      child.stderr?.on("data", /**
       * Conda stderr 到达后只保留限定长度，供查询失败提示使用。
       *
       * Args:
       *     chunk (string): 子进程刚到达的文本或字节片段。
       */
      (chunk: string) => {
        if (settled || Buffer.byteLength(stderr) >= MAX_STDERR_BYTES) return;
        const remaining = MAX_STDERR_BYTES - Buffer.byteLength(stderr);
        stderr += Buffer.from(chunk).subarray(0, remaining).toString("utf8");
      });
      child.on("error", /**
       * 辅助进程报告启动错误时交给统一收尾函数拒绝等待。
       *
       * Args:
       *     error (Error): 本次捕获的异常。
       */
      (error) => finish(error));
      child.once("close", /**
       * Conda 进程退出后检查退出码，成功则解析环境 JSON，失败则报告限定长度的诊断。
       *
       * Args:
       *     code (number | null): 子进程退出码，信号退出时可能为空。
       *     exitSignal (Signals | null): 子进程退出时收到的系统信号。
       */
      (code, exitSignal) => {
        if (settled) return;
        if (code !== 0) {
          const detail = exitSignal
            ? `信号 ${exitSignal}`
            : `退出码 ${String(code)}`;
          const message = stderr.trim();
          finish(new Error(`无法查询 Conda 环境（${detail}）${message ? `：${message}` : ""}`));
          return;
        }
        finish();
      });
    });

  /**
   * 按配置优先级解析 Python 可执行文件，必要时查询 Conda 环境并确认文件存在。
   *
   * Args:
   *     signal (AbortSignal): 调用方提供的取消信号。
   *
   * Returns:
   *     Promise<ResolvedBackendPython>: 可执行文件路径及启动环境所需信息。
   */
  const resolve = async (signal?: AbortSignal): Promise<ResolvedBackendPython> => {
    if (signal?.aborted) throw createCancelledError();
    if (cached) return cached;

    const explicit = dependencies.env.AYAYA_PYTHON_EXECUTABLE?.trim();
    if (explicit) {
      cached = {
        executable: explicit,
      };
      return cached;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(await runCondaEnvironmentList(signal));
    } catch (error) {
      if (error instanceof Error && error.message === "后端启动已取消") throw error;
      if (error instanceof SyntaxError) {
        throw new Error("Conda 返回了无法解析的环境列表", { cause: error });
      }
      throw error;
    }
    if (
      typeof parsed !== "object"
      || parsed === null
      || !Array.isArray((parsed as { envs?: unknown }).envs)
    ) {
      throw new Error("Conda 返回的环境列表缺少 envs 字段");
    }

    const pathApi = dependencies.platform === "win32" ? path.win32 : path.posix;
    const candidates = (parsed as { envs: unknown[] }).envs.filter(
      /**
       * 筛选路径字符串中目录名为 ayaya 的环境，多个匹配由调用方明确拒绝猜测。
       *
       * Args:
       *     item (unknown): 当前遍历的配置或目录条目。
       *
       * Returns:
       *     item is string: 当前条目确实是 ayaya 环境路径时为 true。
       */
      (item): item is string =>
        typeof item === "string"
        && pathApi.basename(pathApi.normalize(item)).toLowerCase() === "ayaya",
    );
    if (candidates.length === 0) {
      throw new Error(
        "未找到名为 ayaya 的 Conda 环境，请创建该环境或设置 AYAYA_PYTHON_EXECUTABLE",
      );
    }
    if (candidates.length > 1) {
      throw new Error(
        "找到多个名为 ayaya 的 Conda 环境，请使用 AYAYA_PYTHON_EXECUTABLE 明确指定解释器",
      );
    }

    const environmentRoot = candidates[0];
    const executable = dependencies.platform === "win32"
      ? pathApi.join(environmentRoot, "python.exe")
      : pathApi.join(environmentRoot, "bin", "python");
    if (!await dependencies.fileExists(executable)) {
      throw new Error(
        "ayaya Conda 环境中不存在 Python 解释器，请修复环境或设置 AYAYA_PYTHON_EXECUTABLE",
      );
    }
    if (signal?.aborted) throw createCancelledError();

    cached = { executable, environmentRoot };
    return cached;
  };

  return { resolve };
};

/**
 * 为直接启动的环境 Python 补充必要的可执行文件搜索目录。
 *
 * Args:
 *     baseEnvironment (NodeJS.ProcessEnv): Electron 当前环境。
 *     resolvedPython (ResolvedBackendPython): 已解析的解释器及环境根目录。
 *     platform (NodeJS.Platform): 当前 Node 平台。
 *
 * Returns:
 *     NodeJS.ProcessEnv: 不修改输入对象的后端子进程环境。
 */
export const buildBackendPythonEnvironment = (
  baseEnvironment: NodeJS.ProcessEnv,
  resolvedPython: ResolvedBackendPython,
  platform: NodeJS.Platform,
): NodeJS.ProcessEnv => {
  const childEnvironment: NodeJS.ProcessEnv = {
    ...baseEnvironment,
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  };
  if (!resolvedPython.environmentRoot) return childEnvironment;

  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const additions = platform === "win32"
    ? [
        resolvedPython.environmentRoot,
        pathApi.join(resolvedPython.environmentRoot, "Scripts"),
        pathApi.join(resolvedPython.environmentRoot, "Library", "bin"),
      ]
    : [pathApi.join(resolvedPython.environmentRoot, "bin")];
  const currentPathKey = Object.keys(childEnvironment).find(
    /**
     * 查找父进程环境中实际使用的 PATH 键名，兼容 Windows 大小写。
     *
     * Args:
     *     key (string): 当前配置字段或索引名称。
     *
     * Returns:
     *     boolean: 当前键名忽略大小写后为 path 时为 true。
     */
    (key) => key.toLocaleLowerCase() === "path",
  ) ?? "PATH";
  const currentPath = childEnvironment[currentPathKey] ?? "";
  const delimiter = platform === "win32" ? ";" : ":";
  childEnvironment[currentPathKey] = [...additions, currentPath]
    .filter(Boolean)
    .join(delimiter);
  return childEnvironment;
};
