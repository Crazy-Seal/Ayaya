import type {
  LogLevel,
  LogRecord,
  LogSide,
  LogSnapshot,
  LogSource,
} from "../../shared-types.js";

/**
 * 描述日志关键词、最低级别和来源筛选条件。
 */
export interface LogFilter {
  query: string;
  minimumLevel: LogLevel;
  sources: ReadonlySet<LogSource>;
}

/**
 * 返回本次日志更新中新增及被缓存上限淘汰的记录，供页面局部更新。
 */
export interface LogModelDelta {
  added: LogRecord[];
  removed: LogRecord[];
}

/**
 * 创建没有新增或移除记录的更新结果。
 *
 * Returns:
 *     LogModelDelta: 独立的空增删记录对象。
 */
const EMPTY_DELTA = (): LogModelDelta => ({ added: [], removed: [] });

const LOG_LEVEL_RANK: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
};

/**
 * 管理日志窗口的内存记录、暂停期间的待显示日志和过滤条件，不负责文件保存。
 */
export class LogViewerModel {
  private readonly records: LogSnapshot;
  private readonly pending: LogRecord[] = [];
  private paused = false;

  /**
   * 从主进程快照复制最近日志，前端和后端分别应用缓存上限。
   *
   * Args:
   *     snapshot (LogSnapshot): 初始化时从主进程取得的前端和后端日志集合。
   *     limit (number): 最多保留或读取的记录数量。
   */
  constructor(snapshot: LogSnapshot, private readonly limit = 5_000) {
    this.records = {
      frontend: snapshot.frontend.slice(-limit),
      backend: snapshot.backend.slice(-limit),
    };
  }

  /**
   * 收到实时日志时按编号去重；暂停期间先暂存，恢复后再更新可见记录。
   *
   * Args:
   *     incoming (readonly LogRecord[]): 刚收到的日志记录，可能与已有记录重复。
   *
   * Returns:
   *     LogModelDelta: 本次新增和淘汰的记录，暂停时为空。
   */
  append(incoming: readonly LogRecord[]): LogModelDelta {
    if (this.paused) {
      this.pending.push(...incoming);
      return EMPTY_DELTA();
    }
    return this.apply(incoming);
  }

  /**
   * 切换显示暂停状态，恢复时一次应用暂停期间积累的日志。
   *
   * Args:
   *     paused (boolean): 是否暂存新日志，等待恢复显示。
   *
   * Returns:
   *     LogModelDelta: 恢复显示产生的新增与淘汰记录，其他情况为空。
   */
  setPaused(paused: boolean): LogModelDelta {
    if (this.paused === paused) return EMPTY_DELTA();
    this.paused = paused;
    if (!paused && this.pending.length > 0) {
      const pending = this.pending.splice(0);
      return this.apply(pending);
    }
    return EMPTY_DELTA();
  }

  /**
   * 读取当前日志显示是否处于暂停状态。
   *
   * Returns:
   *     boolean: 已暂停时为 true。
   */
  isPaused(): boolean {
    return this.paused;
  }

  /**
   * 读取指定类别当前保留的记录，调用方只能以只读方式使用。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *
   * Returns:
   *     readonly LogRecord[]: 按日志顺序排列的缓存记录。
   */
  getRecords(side: LogSide): readonly LogRecord[] {
    return this.records[side];
  }

  /**
   * 清空指定类别当前记录及暂停期间同类记录，恢复时不会再显示已清空内容。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   */
  clear(side: LogSide): void {
    this.records[side] = [];
    for (let index = this.pending.length - 1; index >= 0; index -= 1) {
      if (this.pending[index]?.side === side) this.pending.splice(index, 1);
    }
  }

  /**
   * 按级别、来源及关键词筛选当前类别日志。
   *
   * Args:
   *     side (LogSide): 前端或后端日志类别。
   *     filter (LogFilter): 关键词、最低级别和来源过滤条件。
   *
   * Returns:
   *     LogRecord[]: 符合全部筛选条件的记录列表。
   */
  filter(side: LogSide, filter: LogFilter): LogRecord[] {
    return this.records[side].filter(/**
     * 逐条检查日志是否满足当前级别、来源和关键词条件。
     *
     * Args:
     *     record (LogRecord): 当前需要显示或筛选的日志记录。
     *
     * Returns:
     *     boolean: 该记录满足全部条件时为 true。
     */
    (record) => this.matches(record, filter));
  }

  /**
   * 检查一条日志是否同时满足级别、来源和模块/正文关键词条件。
   *
   * Args:
   *     record (LogRecord): 当前需要显示或筛选的日志记录。
   *     filter (LogFilter): 关键词、最低级别和来源过滤条件。
   *
   * Returns:
   *     boolean: 全部条件满足时为 true。
   */
  matches(record: LogRecord, filter: LogFilter): boolean {
    const query = filter.query.trim().toLocaleLowerCase();
    return (
      LOG_LEVEL_RANK[record.level] >= LOG_LEVEL_RANK[filter.minimumLevel]
      && (filter.sources.size === 0 || filter.sources.has(record.source))
      && (!query || `${record.scope} ${record.message}`.toLocaleLowerCase().includes(query))
    );
  }

  /**
   * 按稳定编号接收新记录，分别淘汰前后端超出上限的旧日志。
   *
   * Args:
   *     incoming (readonly LogRecord[]): 刚收到的日志记录，可能与已有记录重复。
   *
   * Returns:
   *     LogModelDelta: 本次确实新增和被淘汰的记录。
   */
  private apply(incoming: readonly LogRecord[]): LogModelDelta {
    const delta = EMPTY_DELTA();
    const knownIds = new Set([
      ...this.records.frontend.map(/**
       * 提取前端已缓存记录的编号，用于实时日志去重。
       *
       * Args:
       *     record (LogRecord): 当前需要显示或筛选的日志记录。
       *
       * Returns:
       *     number: 当前日志编号。
       */
      (record) => record.id),
      ...this.records.backend.map(/**
       * 提取后端已缓存记录的编号，与前端编号一起检查重复。
       *
       * Args:
       *     record (LogRecord): 当前需要显示或筛选的日志记录。
       *
       * Returns:
       *     number: 当前日志编号。
       */
      (record) => record.id),
    ]);
    for (const record of incoming) {
      if (knownIds.has(record.id)) continue;
      knownIds.add(record.id);
      const sideRecords = this.records[record.side];
      sideRecords.push(record);
      delta.added.push(record);
      if (sideRecords.length > this.limit) {
        delta.removed.push(...sideRecords.splice(0, sideRecords.length - this.limit));
      }
    }
    return delta;
  }
}
