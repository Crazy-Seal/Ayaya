import { StringDecoder } from "node:string_decoder";

import type {
  LogBatch,
  LogLevel,
  LogRecord,
  LogSide,
  LogSnapshot,
  LogSource,
} from "../../shared-types.js";

/**
 * 约定日志文件写入和关闭操作，日志中心不依赖具体文件轮转实现。
 */
export interface LogWriter {
  /**
   * 接收已脱敏格式化的日志文本并写入目标。
   *
   * Args:
   *     content (string): 当前需要处理的正文。
   *
   * Returns:
   *     Promise<void> | void: 实现可以同步完成，或返回等待写入的 Promise。
   */
  write(content: string): Promise<void> | void;
  /**
   * 完成剩余写入并释放日志资源。
   *
   * Returns:
   *     Promise<void> | void: 同步结束或等待关闭的 Promise。
   */
  close(): Promise<void> | void;
}

/**
 * 配置日志缓存数量、批次间隔、文本限制和可替换时钟。
 */
interface LogHubOptions {
  bufferLimit?: number;
  batchIntervalMs?: number;
  maxMessageBytes?: number;
  immediateSides?: readonly LogSide[];
  now?: () => Date;
}

/**
 * 保存一条新日志的类别、来源、级别、模块和原正文。
 */
interface AppendLogInput {
  side: LogSide;
  source: LogSource;
  level: LogLevel;
  scope: string;
  message: string;
}

const DEFAULT_BUFFER_LIMIT = 5_000;
const DEFAULT_BATCH_INTERVAL_MS = 100;
const DEFAULT_MAX_MESSAGE_BYTES = 32 * 1024;
const REDACTED = "[REDACTED]";

/**
 * 按字面内容遮蔽日志中的敏感值，避免把凭据当作正则表达式。
 *
 * Args:
 *     value (string): 当前待检查或转换的值。
 *     needle (string): 需要在日志中遮蔽的敏感文本。
 *
 * Returns:
 *     string: 敏感文本被替换后的日志正文。
 */
const replaceAllLiteral = (value: string, needle: string): string =>
  needle ? value.split(needle).join(REDACTED) : value;

/**
 * 按 UTF-8 字节预算截断日志，向前寻找完整字符边界。
 *
 * Args:
 *     value (string): 当前待检查或转换的值。
 *     maxBytes (number): 最多保留的 UTF-8 字节数量。
 *
 * Returns:
 *     string: 有效 UTF-8 文本及可选截断说明。
 */
const truncateUtf8 = (value: string, maxBytes: number): string => {
  const buffer = Buffer.from(value);
  if (buffer.length <= maxBytes) return value;
  let end = Math.max(0, maxBytes);
  const decoder = new TextDecoder("utf-8", { fatal: true });
  while (end > 0) {
    try {
      return `${decoder.decode(buffer.subarray(0, end))}…[已截断，原始 ${buffer.length} 字节]`;
    } catch {
      end -= 1;
    }
  }
  return `[已截断，原始 ${buffer.length} 字节]`;
};

/**
 *  在日志进入内存、文件或 renderer 前执行统一脱敏与限长。
 *
 * Args:
 *     rawMessage (string): 尚未脱敏和限长的日志正文。
 *     sensitiveValues (readonly string[]): 本次需要遮蔽的敏感值。
 *     maxBytes (number): 最多保留的 UTF-8 字节数量。
 */
export const sanitizeLogMessage = (
  rawMessage: string,
  sensitiveValues: readonly string[],
  maxBytes = DEFAULT_MAX_MESSAGE_BYTES,
): string => {
  let message = rawMessage;
  for (const value of sensitiveValues) {
    if (value.length >= 4) message = replaceAllLiteral(message, value);
  }
  message = message
    .replace(/(authorization\s*:\s*bearer\s+)[^\s"',;]+/gi, `$1${REDACTED}`)
    .replace(
      /((?:api[_-]?key|token|secret|password)\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)/gi,
      `$1${REDACTED}`,
    )
    .replace(/data:image\/[a-z0-9.+-]+;base64,[a-z0-9+/=_-]+/gi, `data:image/[REDACTED]`);
  return truncateUtf8(message, Math.max(1, maxBytes));
};

/**
 * 识别 Python 输出行开头的结构化级别，异常追踪按错误处理。
 *
 * Args:
 *     line (string): 已经拼接完整的一行后端输出。
 *
 * Returns:
 *     LogLevel: 识别出的级别，普通输出默认 info。
 */
export const inferBackendLevel = (line: string): LogLevel => {
  const normalized = line.trimStart();
  const structuredLevel = normalized.match(
    /^(?:[^|\r\n]{1,80}\|\s*)?(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)(?=\s|:|\|)/i,
  )?.[1]?.toUpperCase();
  if (structuredLevel === "DEBUG") return "debug";
  if (structuredLevel === "WARN" || structuredLevel === "WARNING") return "warn";
  if (structuredLevel === "ERROR" || structuredLevel === "CRITICAL") {
    return "error";
  }
  if (/^Traceback\b/i.test(normalized)) return "error";
  return "info";
};

/**
 *  将任意分块的 UTF-8 子进程输出还原为完整日志行。
 */
export class BackendLineDecoder {
  private readonly decoder = new StringDecoder("utf8");
  private pending = "";

  /**
   * 保存完整行回调，每条 stdout 或 stderr 使用独立解码器。
   *
   * Args:
   *     onLine ((line: string) => void): 完整一行准备好后接收它的回调。
   */
  constructor(private readonly onLine: (line: string) => void) {}

  /**
   * 收到子进程片段时拼接 UTF-8 字节，并把完整行交给日志回调。
   *
   * Args:
   *     chunk (Buffer | string): 子进程刚到达的文本或字节片段。
   */
  write(chunk: Buffer | string): void {
    this.pending += typeof chunk === "string" ? chunk : this.decoder.write(chunk);
    this.drainLines();
  }

  /**
   * 输出流结束时处理剩余字节和最后一条没有换行的日志。
   *
   * Args:
   *     chunk (Buffer | string): 子进程刚到达的文本或字节片段。
   */
  end(chunk?: Buffer | string): void {
    if (chunk !== undefined) this.write(chunk);
    this.pending += this.decoder.end();
    this.drainLines();
    if (this.pending) {
      this.onLine(this.pending.replace(/\r$/, ""));
      this.pending = "";
    }
  }

  /**
   * 逐条取出已经收到换行的日志，去除 CRLF 中的回车后交给回调。
   */
  private drainLines(): void {
    let newline = this.pending.indexOf("\n");
    while (newline >= 0) {
      const line = this.pending.slice(0, newline).replace(/\r$/, "");
      this.pending = this.pending.slice(newline + 1);
      this.onLine(line);
      newline = this.pending.indexOf("\n");
    }
  }
}

/**
 *  Electron 主进程内的日志汇聚、缓存与批量分发中心。
 */
export class LogHub {
  private readonly buffers: LogSnapshot = { frontend: [], backend: [] };
  private readonly subscribers = new Set<(batch: LogBatch) => void>();
  private readonly sensitiveValues = new Set<string>();
  private readonly bufferLimit: number;
  private readonly batchIntervalMs: number;
  private readonly maxMessageBytes: number;
  private readonly immediateSides: ReadonlySet<LogSide>;
  private readonly now: () => Date;
  private readonly writers: Partial<Record<LogSide, LogWriter>> = {};
  private pendingBatch: LogRecord[] = [];
  private batchTimer: ReturnType<typeof setTimeout> | null = null;
  private nextId = 1;
  private closed = false;

  /**
   * 初始化前后端缓存、脱敏集合和分发配置，此时尚未写入日志文件。
   *
   * Args:
   *     options (LogHubOptions): 调用方提供的配置或运行选项。
   */
  constructor(options: LogHubOptions = {}) {
    this.bufferLimit = options.bufferLimit ?? DEFAULT_BUFFER_LIMIT;
    this.batchIntervalMs = options.batchIntervalMs ?? DEFAULT_BATCH_INTERVAL_MS;
    this.maxMessageBytes = options.maxMessageBytes ?? DEFAULT_MAX_MESSAGE_BYTES;
    this.immediateSides = new Set(options.immediateSides ?? []);
    this.now = options.now ?? (/**
     * 日志中心未注入时钟时读取当前日期时间。
     *
     * Returns:
     *     Date: 本机当前时间对象。
     */
    () => new Date());
  }

  /**
   * 给指定日志类别接入文件写入器。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *     writer (LogWriter): 负责把该类日志保存到文件的写入对象。
   */
  setWriter(side: LogSide, writer: LogWriter): void {
    this.writers[side] = writer;
  }

  /**
   * 登记足够长的敏感值，之后所有新日志入缓存或文件前统一遮蔽。
   *
   * Args:
   *     value (string): 当前待检查或转换的值。
   */
  addSensitiveValue(value: string): void {
    if (value.length >= 4) this.sensitiveValues.add(value);
  }

  /**
   * 先脱敏并生成稳定编号，再更新内存和文件，按类别立即或定时通知日志窗口。
   *
   * Args:
   *     input (AppendLogInput): 当前日志输入或配置输入框。
   *
   * Returns:
   *     LogRecord | null: 已接收的日志记录，中心关闭后返回 null。
   */
  append(input: AppendLogInput): LogRecord | null {
    if (this.closed) return null;
    // 1. 在进入内存、文件及页面前遮蔽敏感值，并分配本进程内递增的去重编号。
    const record: LogRecord = {
      id: this.nextId,
      timestamp: this.now().toISOString(),
      side: input.side,
      source: input.source,
      level: input.level,
      scope: input.scope,
      message: sanitizeLogMessage(
        String(input.message),
        [...this.sensitiveValues],
        this.maxMessageBytes,
      ),
    };
    this.nextId += 1;

    // 2. 前后端分别限制缓存条数，超过上限只淘汰该类最旧记录。
    const buffer = this.buffers[input.side];
    buffer.push(record);
    if (buffer.length > this.bufferLimit) {
      buffer.splice(0, buffer.length - this.bufferLimit);
    }
    // 3. 文件写入器内部按顺序保存和轮转；这里不等待磁盘完成，以免拖慢业务产生日志。
    const writer = this.writers[input.side];
    if (writer) {
      const prefix = `${record.timestamp} ${record.level.toUpperCase().padEnd(5)} `
        + `[${record.source}/${record.scope}] `;
      const content = `${record.message
        .split(/\r?\n/)
        .map(/**
         * 保存多行日志时给每行加相同时间、级别及来源前缀。
         *
         * Args:
         *     line (string): 已经拼接完整的一行后端输出。
         *
         * Returns:
         *     string: 可写日志文件的一行文本。
         */
        (line) => `${prefix}${line}`)
        .join("\n")}\n`;
      void Promise.resolve(writer.write(content)).catch(/**
       * 文件写入失败时消费拒绝结果，错误由写入器报告，避免日志故障影响业务。
       */
      () => undefined);
    }

    // 4. 后端等指定类别立即交付，其他类别短暂合并，窗口与文件使用相同脱敏正文。
    if (this.immediateSides.has(record.side)) {
      this.publishRecords([record]);
    } else {
      this.pendingBatch.push(record);
      this.scheduleBatch();
    }
    return record;
  }

  /**
   * 复制前后端缓存数组，供新日志窗口初始化。
   *
   * Returns:
   *     LogSnapshot: 当前日志快照，不共享可修改的数组。
   */
  getSnapshot(): LogSnapshot {
    return {
      frontend: [...this.buffers.frontend],
      backend: [...this.buffers.backend],
    };
  }

  /**
   * 读取指定类别最近的日志记录。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *     limit (number): 最多保留或读取的记录数量。
   *
   * Returns:
   *     LogRecord[]: 按时间顺序排列的最近记录。
   */
  getRecent(side: LogSide, limit: number): LogRecord[] {
    return this.buffers[side].slice(-Math.max(0, limit));
  }

  /**
   * 清空指定类别缓存，并从待分发批次中移除该类别，避免清空后又收到旧记录。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   */
  clear(side: LogSide): void {
    this.buffers[side] = [];
    this.pendingBatch = this.pendingBatch.filter(/**
     * 清空一类日志时从待发布批次排除同类记录。
     *
     * Args:
     *     record (LogRecord): 当前需要显示或筛选的日志记录。
     *
     * Returns:
     *     boolean: 记录属于其他日志类别时为 true。
     */
    (record) => record.side !== side);
  }

  /**
   * 登记批次接收回调，发布时一个订阅者异常不会阻止其他订阅者。
   *
   * Args:
   *     listener ((batch: LogBatch) => void): 接收后续事件的回调，取消订阅后不再调用。
   *
   * Returns:
   *     () => void: 取消当前订阅的函数。
   */
  subscribe(listener: (batch: LogBatch) => void): () => void {
    this.subscribers.add(listener);
    /**
     * 调用方取消日志订阅时移除当前监听者。
     */
    return () => this.subscribers.delete(listener);
  }

  /**
   * 停止接收新日志，取消定时器、发布剩余批次并等待所有文件写入器关闭。
   *
   * Returns:
   *     Promise<void>: 日志资源全部关闭后完成。
   */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    if (this.batchTimer) {
      clearTimeout(this.batchTimer);
      this.batchTimer = null;
    }
    this.publishBatch();
    await Promise.all(
      Object.values(this.writers).map(/**
       * 日志中心关闭时等待每个已接入写入器刷新并释放资源。
       *
       * Args:
       *     writer (LogWriter): 负责把该类日志保存到文件的写入对象。
       */
      (writer) => Promise.resolve(writer?.close())),
    );
  }

  /**
   * 尚无定时器时安排一次批次发送，多条新日志共用同一定时器。
   */
  private scheduleBatch(): void {
    if (this.batchTimer) return;
    this.batchTimer = setTimeout(/**
     * 批次定时器到期时先清空定时器标记，再交付累积日志，之后新日志可安排下一批。
     */
    () => {
      this.batchTimer = null;
      this.publishBatch();
    }, this.batchIntervalMs);
  }

  /**
   * 取走待发送记录并清空内存批次，再通知各订阅者。
   */
  private publishBatch(): void {
    if (this.pendingBatch.length === 0) return;
    const records = this.pendingBatch;
    this.pendingBatch = [];
    this.publishRecords(records);
  }

  /**
   * 把一组日志交给订阅者，捕获观察者异常以保护日志产生流程。
   *
   * Args:
   *     records (LogRecord[]): 按产生顺序排列的日志记录。
   */
  private publishRecords(records: LogRecord[]): void {
    const batch = { records };
    for (const subscriber of this.subscribers) {
      try {
        subscriber(batch);
      } catch {
        // 日志观察者不能反向破坏日志生产者。
      }
    }
  }
}
