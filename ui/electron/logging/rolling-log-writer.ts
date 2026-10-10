import { appendFile, mkdir, rename, rm, stat } from "node:fs/promises";
import path from "node:path";

/**
 * 配置单类日志的保存目录、文件大小、备份数量和写入失败回调。
 */
export interface RollingLogWriterOptions {
  directory: string;
  fileName: string;
  maxBytes: number;
  maxFiles: number;
  onError?: (error: unknown) => void;
}

/**
 * 识别轮转时可以忽略的文件不存在错误。
 *
 * Args:
 *     error (unknown): 本次捕获的异常。
 *
 * Returns:
 *     boolean: 系统错误码为 ENOENT 时为 true。
 */
const isMissingFile = (error: unknown): boolean =>
  error instanceof Error && "code" in error && error.code === "ENOENT";

/**
 * 串行追加并按大小轮换的轻量日志写入器。
 *
 * 写入错误会报告给回调，但不会向调用方传播，避免日志故障影响应用主流程。
 */
export class RollingLogWriter {
  private readonly filePath: string;
  private readonly onError: (error: unknown) => void;
  private queue: Promise<void> = Promise.resolve();
  private currentBytes: number | null = null;
  private closed = false;

  /**
   * 确定日志路径和轮转限制，建立串行写入链，准备目录及现有文件大小。
   *
   * Args:
   *     options (RollingLogWriterOptions): 调用方提供的配置或运行选项。
   */
  constructor(private readonly options: RollingLogWriterOptions) {
    this.filePath = path.join(options.directory, options.fileName);
    this.onError = options.onError ?? (/**
     * 未注入错误回调时消费文件错误通知，由日志中心决定后续展示。
     */
    () => undefined);
  }

  /**
   * 把本次写入追加到串行 Promise 链，写入失败报告后允许后续日志继续。
   *
   * Args:
   *     content (string): 当前需要处理的正文。
   *
   * Returns:
   *     Promise<void>: 本次排队写入完成后的结果。
   */
  write(content: string): Promise<void> {
    if (this.closed) return Promise.resolve();
    const task = this.queue
      .then(/**
       * 前一项文件操作结束后执行当前日志写入，保持轮转与追加顺序一致。
       */
      () => this.writeInternal(content))
      .catch(/**
       * 串行写入失败后通知错误接收方，并让后续写入仍可继续排队。
       *
       * Args:
       *     error (any): 本次捕获的异常。
       */
      (error) => {
        this.onError(error);
      });
    this.queue = task;
    return task;
  }

  /**
   * 等待之前已排队的写入完成。
   *
   * Returns:
   *     Promise<void>: 当前写入链完成后的结果。
   */
  flush(): Promise<void> {
    return this.queue;
  }

  /**
   * 停止接受新写入并等待已排队日志保存。
   *
   * Returns:
   *     Promise<void>: 现有写入完成后结束。
   */
  async close(): Promise<void> {
    await this.queue;
    this.closed = true;
  }

  /**
   * 在串行链中检查文件大小，必要时先轮转再追加本次日志。
   *
   * Args:
   *     content (string): 当前需要处理的正文。
   *
   * Returns:
   *     Promise<void>: 本次文件追加完成后的结果。
   */
  private async writeInternal(content: string): Promise<void> {
    await mkdir(this.options.directory, { recursive: true });
    if (this.currentBytes === null) {
      try {
        this.currentBytes = (await stat(this.filePath)).size;
      } catch (error) {
        if (!isMissingFile(error)) throw error;
        this.currentBytes = 0;
      }
    }

    const bytes = Buffer.byteLength(content);
    if (this.currentBytes > 0 && this.currentBytes + bytes > this.options.maxBytes) {
      await this.rotate();
      this.currentBytes = 0;
    }
    await appendFile(this.filePath, content, "utf8");
    this.currentBytes += bytes;
  }

  /**
   * 先移除最旧备份，再逐项移动现有备份和活动日志，保持备份数量上限。
   *
   * Returns:
   *     Promise<void>: 文件轮转完成后的结果。
   */
  private async rotate(): Promise<void> {
    const maxFiles = Math.max(1, this.options.maxFiles);
    if (maxFiles === 1) {
      await rm(this.filePath, { force: true });
      return;
    }

    for (let index = maxFiles - 1; index >= 1; index -= 1) {
      const source = index === 1 ? this.filePath : `${this.filePath}.${index - 1}`;
      const target = `${this.filePath}.${index}`;
      await rm(target, { force: true });
      try {
        await rename(source, target);
      } catch (error) {
        if (!isMissingFile(error)) throw error;
      }
    }
  }
}
