import type { BackgroundTaskRecord } from "../../types.js";
import { CommandKind, TaskStatus, TaskExecutionPhase } from "../../../shared-types.js";
import { TASK_REFRESH_INTERVAL_MS } from "../../../runtime-constants.js";
import { appendErrorMessage, type SettingsNotifier } from "../components/settings-toast.js";

/**
 * 保存单个任务卡片的详情元素和分页进度，供列表刷新后继续查看。
 *
 * Attributes:
 *     element (HTMLElement): 当前任务独立的详情区域，hidden 表示是否收起。
 *     output (HTMLPreElement): 已读取的完整输出或任务结果正文。
 *     more (HTMLButtonElement): 当前任务继续读取输出的按钮。
 *     offset (number): 下一页输出的字节游标。
 *     loading (boolean): 当前任务是否已有输出请求正在等待响应。
 *     loaded (boolean): 是否已成功读取过真实日志，空日志也算已读取，占位提示不计入。
 *     atEnd (boolean): 最近一次读取是否到达当时的文件末尾，刷新时从原游标检查新增日志。
 *     refreshPending (boolean): 当前日志请求等待期间是否又收到刷新，响应后需要检查新内容。
 */
interface TaskDetails {
  element: HTMLElement;
  output: HTMLPreElement;
  more: HTMLButtonElement;
  offset: number;
  loading: boolean;
  loaded: boolean;
  atEnd: boolean;
  refreshPending: boolean;
}

/**
 * 在设置页查询任务记录和完整输出，并提交独立停止命令。
 *
 * Attributes:
 *     sessionId (string): 当前会话标识，用于丢弃跨会话迟到响应。
 *     taskDetails (Map<string, TaskDetails>): 按任务编号保留各卡片的详情和分页进度。
 *     active (boolean): 任务页是否可见，仅可见页刷新运行时长和最近输出。
 *     revision (number): 查询代次，防止事件刷新和轮询结果互相覆盖。
 *     timer (ReturnType<typeof setInterval> | null): 可见页运行任务的刷新计时器。
 *     refreshFeedbackSession (string | null): 用户已点击刷新、但尚未得到最新查询结果的会话编号。
 */
export class BackgroundTasksPage {
  private sessionId = "";
  private readonly taskDetails = new Map<string, TaskDetails>();
  private active = false;
  private revision = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private refreshFeedbackSession: string | null = null;

  /**
   * 装配 BackgroundTasksPage 的依赖并初始化本地视图状态。
   *
   * Args:
   *     list (HTMLElement): 任务列表容器。
   *     refresh (HTMLButtonElement): 手动刷新任务记录的按钮。
   *     notifier (SettingsNotifier | undefined): 手动刷新成功或失败时显示设置页提示；自动轮询不弹提示。
   */
  constructor(
    private readonly list: HTMLElement,
    refresh: HTMLButtonElement,
    private readonly notifier?: SettingsNotifier,
  ) {
    refresh.addEventListener("click", /**
     * 用户手动刷新任务记录后显示成功或失败提示；自动轮询继续保持安静。
     */
    () => { void this.render(this.sessionId, true); });
  }

  /**
   * 控制任务页的实时刷新，切换到其他页或关闭窗口时停止轮询。
   *
   * Args:
   *     active (boolean): 当前页是否可见。
   */
  setActive(active: boolean): void {
    this.active = active;
    if (!active) this.refreshFeedbackSession = null;
    if (!active && this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  /**
   * 查询当前会话数据并呈现页面，失败时显示错误说明。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     notify (boolean): 是否为本次手动刷新显示结果，默认不为页面加载和自动轮询弹提示。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  async render(sessionId: string, notify = false): Promise<void> {
    // 1. 会话切换清除旧任务输出，事件与轮询请求用代次防止迟到覆盖。
    if (sessionId !== this.sessionId) {
      this.refreshFeedbackSession = null;
      this.taskDetails.clear();
      this.list.replaceChildren();
    }
    this.sessionId = sessionId;
    // 1.1. 手动刷新提示跟随本会话的最新查询，避免后来的轮询让用户点击后没有反馈。
    if (notify) this.refreshFeedbackSession = sessionId;
    const revision = ++this.revision;
    try {
      const records = await window.desktopPetApi.getBackgroundTasks(sessionId);
      if (this.sessionId !== sessionId || revision !== this.revision) return;
      this.show(records);
      // 1.2. 只有最新查询已显示到页面后才完成手动刷新提示，普通轮询不会新建提示请求。
      if (this.refreshFeedbackSession === sessionId) {
        this.refreshFeedbackSession = null;
        this.notifier?.success("刷新成功");
      }
      // 2. 为可见页中的运行任务刷新状态、摘要和已展开的日志，最后一次刷新补读终态输出。
      const running = records.some(/**
       * 检查是否仍有运行任务，全部结束后停止任务状态轮询。
       *
       * Args:
       *     record (BackgroundTaskRecord): 当前后台任务记录，包含编号、执行状态及结果信息。
       *
       * Returns:
       *     boolean: 任务状态为 running 时为 true。
       */
      (record) => record.status === TaskStatus.RUNNING);
      if (this.active && running && !this.timer) {
        this.timer = setInterval(/**
         * 任务状态变化后刷新可见任务页面。
         */
        () => { void this.render(this.sessionId); }, TASK_REFRESH_INTERVAL_MS);
      } else if ((!this.active || !running) && this.timer) {
        clearInterval(this.timer);
        this.timer = null;
      }
    } catch (error) {
      if (this.sessionId === sessionId && revision === this.revision) {
        this.list.textContent = `读取任务失败: ${String(error)}`;
        if (this.refreshFeedbackSession === sessionId) {
          this.refreshFeedbackSession = null;
          this.notifier?.error(appendErrorMessage("刷新失败", error));
        }
      }
    }
  }

  /**
   * 为每条任务创建独立卡片，刷新时保留对应详情元素和已读取的输出。
   *
   * Args:
   *     records (BackgroundTaskRecord[]): 需要显示的持久任务记录。
   */
  private show(records: BackgroundTaskRecord[]): void {
    // 1. 删除已经不在列表中的详情，迟到的输出通过对象身份检查丢弃。
    const taskIds = new Set(records.map(/** 从最新记录中取得仍需显示的任务编号。 */
      (record) => record.task_id));
    for (const taskId of this.taskDetails.keys()) {
      if (!taskIds.has(taskId)) this.taskDetails.delete(taskId);
    }
    this.list.replaceChildren();
    if (!records.length) {
      this.list.textContent = "暂无后台任务";
      return;
    }
    // 2. 每张卡片各自包含任务标题、最近输出和可展开的完整详情。
    for (const record of records) {
      const card = document.createElement("div");
      card.className = "background-task-card ui-surface";
      const row = document.createElement("div");
      row.className = "background-task-row";
      const title = document.createElement("span");
      const labels: Record<BackgroundTaskRecord["status"], string> = {
        [TaskStatus.RUNNING]: "运行中", [TaskStatus.COMPLETED]: "已完成", [TaskStatus.FAILED]: "失败",
        [TaskStatus.STOPPED]: "已停止", [TaskStatus.ORPHANED]: "已失联",
        [TaskExecutionPhase.SAVE_FAILED]: "结果保存失败",
      };
      const seconds = Math.max(0, Math.floor((Date.parse(record.ended_at || new Date().toISOString())
        - Date.parse(record.created_at)) / 1000));
      title.textContent = `[${record.plugin_name || record.tool_name}] ${record.title}`
        + (record.subagent_id ? ` · 子 Agent ${record.subagent_id}` : "")
        + ` · ${labels[record.status]} · ${Math.floor(seconds / 60)}分${seconds % 60}秒`;
      const details = document.createElement("button");
      details.type = "button";
      details.className = "ui-button ui-button--secondary ui-button--small";
      let taskDetails = this.taskDetails.get(record.task_id);
      if (!taskDetails) {
        taskDetails = this.createDetails(record.task_id);
        this.taskDetails.set(record.task_id, taskDetails);
      }
      const currentDetails = taskDetails;
      if (!record.output_ref) {
        currentDetails.output.textContent = [record.result, record.error].filter(Boolean).join("\n") || "任务没有结果正文";
      }
      details.textContent = currentDetails.element.hidden ? (record.output_ref ? "输出" : "结果") : "收起";
      details.setAttribute("aria-expanded", String(!currentDetails.element.hidden));
      details.addEventListener("click", /**
       * 用户展开当前卡片时读取初始日志，或从此前文件末尾继续读取新增内容。
       */
      () => {
        currentDetails.element.hidden = !currentDetails.element.hidden;
        details.textContent = currentDetails.element.hidden ? (record.output_ref ? "输出" : "结果") : "收起";
        details.setAttribute("aria-expanded", String(!currentDetails.element.hidden));
        if (record.output_ref) this.refreshOutput(record.task_id, currentDetails);
      });
      row.append(title, details);
      const recent = document.createElement("div");
      recent.className = "background-task-recent";
      recent.textContent = record.output_ref ? `最近输出：${record.recent_output?.trim() || "暂无输出"}` : record.error || record.result || "暂无结果正文";
      if (record.status === TaskStatus.RUNNING) {
        const stop = document.createElement("button");
        stop.type = "button";
        stop.className = "ui-button ui-button--danger ui-button--small";
        stop.textContent = "停止";
        stop.addEventListener("click", /**
         * 用户点击停止任务时提交指定任务的停止命令，完成后检查后端是否受理。
         */
        () => {
          void window.desktopPetApi.submitInteractionCommand({
            session_id: this.sessionId, kind: CommandKind.STOP_BACKGROUND_TASK, task_id: record.task_id,
          }).then(/**
           * 停止回执到达后只在已受理时刷新任务状态，拒绝则交给错误提示。
           *
           * Args:
           *     receipt (CommandReceipt): 后端对任务操作的命令回执。
           */
          (receipt) => {
            if (!receipt.accepted) throw new Error(receipt.detail || "无法停止任务");
            void this.render(this.sessionId);
          }).catch(/**
           * 停止任务失败或被拒绝后在该任务标题显示原因。
           *
           * Args:
           *     error (unknown): 本次捕获的异常。
           */
          (error: unknown) => { title.textContent = `停止失败: ${String(error)}`; });
        });
        row.appendChild(stop);
      }
      card.append(row, recent, currentDetails.element);
      this.list.appendChild(card);
      // 2.1. 日志引用准备完成或任务状态更新时，统一检查已展开详情的首次读取和新增日志。
      if (record.output_ref) this.refreshOutput(record.task_id, currentDetails);
    }
  }

  /**
   * 为单个任务创建独立的详情正文和加载更多按钮。
   *
   * Args:
   *     taskId (string): 当前卡片对应的任务编号。
   *
   * Returns:
   *     TaskDetails: 初始收起、从零开始读取输出的任务详情。
   */
  private createDetails(taskId: string): TaskDetails {
    const element = document.createElement("div");
    element.className = "background-task-details";
    element.hidden = true;
    const output = document.createElement("pre");
    output.className = "background-task-output";
    const more = document.createElement("button");
    more.type = "button";
    more.className = "background-task-more ui-button ui-button--secondary ui-button--small";
    more.textContent = "加载更多输出";
    more.hidden = true;
    const details: TaskDetails = {
      element, output, more, offset: 0, loading: false, loaded: false,
      atEnd: false, refreshPending: false,
    };
    more.addEventListener("click", /** 当前卡片从自己的分页位置继续读取输出。 */
      () => { void this.loadOutput(taskId, details); });
    element.append(output, more);
    return details;
  }

  /**
   * 为已展开的详情安排首次读取或新增日志检查，保留请求期间收到的刷新需求。
   *
   * Args:
   *     taskId (string): 日志已经准备完成的任务编号。
   *     details (TaskDetails): 当前任务的日志读取进度和详情元素。
   */
  private refreshOutput(taskId: string, details: TaskDetails): void {
    // 1. 切换会话、移除任务或收起详情后，不再为原详情安排读取。
    if (this.taskDetails.get(taskId) !== details || details.element.hidden) return;
    // 2. 请求期间的新状态可能包含任务最后输出，合并为当前请求完成后的一次检查。
    if (details.loading) {
      details.refreshPending = true;
      return;
    }
    // 3. 根据真实日志的读取进度作决定，仍有历史分页时由用户点击加载更多。
    if (!details.loaded || details.atEnd) void this.loadOutput(taskId, details);
  }

  /**
   * 读取当前卡片的下一页输出，刷新后继续使用该卡片的详情和分页位置。
   *
   * Args:
   *     taskId (string): 需要读取输出的任务编号。
   *     details (TaskDetails): 当前任务独立的输出元素和分页进度。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async loadOutput(taskId: string, details: TaskDetails): Promise<void> {
    // 1. 每个任务同时只读取一页；不同卡片可以各自等待输出。
    if (details.loading || this.taskDetails.get(taskId) !== details) return;
    const sessionId = this.sessionId;
    details.refreshPending = false;
    details.loading = true;
    details.more.disabled = true;
    try {
      const page = await window.desktopPetApi.getBackgroundTaskOutput(
        sessionId, taskId, details.offset,
      );
      // 2. 切换会话或移除任务后丢弃旧响应，同会话刷新保留的详情继续接收。
      if (this.sessionId !== sessionId || this.taskDetails.get(taskId) !== details) return;
      // 2.1. 首次真实日志替换任务启动时的结果占位；后续分页从已有正文继续追加。
      details.output.textContent = details.loaded ? details.output.textContent + page.text : page.text;
      details.loaded = true;
      details.offset = page.next_offset;
      // 2.2. 当前文件末尾仍可能继续追加；保留游标，供下一次刷新或重新展开时检查。
      details.atEnd = !page.has_more;
      details.more.hidden = !page.has_more;
    } catch (error) {
      // 3. 读取失败仅在对应卡片显示原因，保留游标并提供重试入口。
      if (this.sessionId === sessionId && this.taskDetails.get(taskId) === details) {
        details.refreshPending = false;
        details.atEnd = false;
        details.output.textContent += `\n读取输出失败: ${String(error)}`;
        details.more.hidden = false;
      }
    } finally {
      details.loading = false;
      details.more.disabled = false;
      // 4. 旧请求结束后再检查期间到达的新状态，任务轮询停止后仍能补上最后日志。
      if (details.refreshPending) {
        details.refreshPending = false;
        this.refreshOutput(taskId, details);
      }
    }
  }
}
