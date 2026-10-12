import type { BackgroundTaskRecord } from "../../types.js";
import { CommandKind, DESKTOP_CHANNEL, TaskStatus } from "../../../shared-types.js";
import { TASK_REFRESH_INTERVAL_MS } from "../../../runtime-constants.js";
import { appendErrorMessage, type SettingsNotifier } from "../components/settings-toast.js";

/**
 * 在设置页查询任务记录和完整输出，并提交独立停止命令。
 *
 * Attributes:
 *     sessionId (string): 当前会话标识，用于丢弃跨会话迟到响应。
 *     selectedTask (string): 当前查看输出的任务标识。
 *     outputOffset (number): 下一页完整输出的字节游标。
 *     active (boolean): 任务页是否可见，仅可见页刷新运行时长和最近输出。
 *     revision (number): 查询代次，防止事件刷新和轮询结果互相覆盖。
 *     timer (ReturnType<typeof setInterval> | null): 可见页运行任务的刷新计时器。
 *     refreshFeedbackSession (string | null): 用户已点击刷新、但尚未得到最新查询结果的会话编号。
 */
export class BackgroundTasksPage {
  private sessionId = "";
  private selectedTask = "";
  private outputOffset = 0;
  private active = false;
  private revision = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private refreshFeedbackSession: string | null = null;

  /**
   * 装配 BackgroundTasksPage 的依赖并初始化本地视图状态。
   *
   * Args:
   *     list (HTMLElement): 任务列表容器。
   *     output (HTMLElement): 分页任务输出容器。
   *     more (HTMLButtonElement): 继续读取输出的按钮。
   *     refresh (HTMLButtonElement): 手动刷新任务记录的按钮。
   *     notifier (SettingsNotifier | undefined): 手动刷新成功或失败时显示设置页提示；自动轮询不弹提示。
   */
  constructor(
    private readonly list: HTMLElement,
    private readonly output: HTMLElement,
    private readonly more: HTMLButtonElement,
    refresh: HTMLButtonElement,
    private readonly notifier?: SettingsNotifier,
  ) {
    refresh.addEventListener("click", /**
     * 用户手动刷新任务记录后显示成功或失败提示；自动轮询继续保持安静。
     */
    () => { void this.render(this.sessionId, true); });
    more.addEventListener("click", /**
     * 已选择任务时定时读取新输出，继续读取位置由返回偏移决定。
     */
    () => { void this.loadOutput(); });
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
      this.selectedTask = "";
      this.outputOffset = 0;
      this.output.textContent = "";
      this.output.hidden = true;
      this.more.hidden = true;
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
      // 2. 只为可见页中的运行任务刷新耗时和输出摘要，终态或切页后释放计时器。
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
   * 重建任务列表，提供输出查询和仅运行任务可用的停止入口。
   *
   * Args:
   *     records (BackgroundTaskRecord[]): 需要显示的持久任务记录。
   */
  private show(records: BackgroundTaskRecord[]): void {
    this.list.replaceChildren();
    if (!records.length) {
      this.list.textContent = "暂无后台任务";
      return;
    }
    // 1. 逐项提供输出详情，只有 running 任务显示独立停止动作。
    for (const record of records) {
      const row = document.createElement("div");
      row.className = "background-task-row";
      const title = document.createElement("span");
      const labels: Record<TaskStatus, string> = {
        [TaskStatus.RUNNING]: "运行中", [TaskStatus.COMPLETED]: "已完成", [TaskStatus.FAILED]: "失败",
        [TaskStatus.STOPPED]: "已停止", [TaskStatus.ORPHANED]: "已失联",
      };
      const seconds = Math.max(0, Math.floor((Date.parse(record.ended_at || new Date().toISOString())
        - Date.parse(record.created_at)) / 1000));
      title.textContent = `${record.command} · ${labels[record.status]} · ${Math.floor(seconds / 60)}分${seconds % 60}秒`;
      const details = document.createElement("button");
      details.type = "button";
      details.className = "ui-button ui-button--secondary ui-button--small";
      details.textContent = "输出";
      details.addEventListener("click", /**
       * 用户选择任务详情时切换任务编号并清空旧输出，从零开始读取该任务输出。
       */
      () => {
        this.selectedTask = record.task_id;
        this.outputOffset = 0;
        this.output.textContent = "";
        this.output.hidden = false;
        void this.loadOutput();
      });
      row.append(title, details);
      const recent = document.createElement("div");
      recent.className = "background-task-recent";
      recent.textContent = `最近输出：${record.recent_output?.trim() || "暂无输出"}`;
      row.append(recent);
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
            session_id: this.sessionId, origin_channel: DESKTOP_CHANNEL,
            kind: CommandKind.STOP_BACKGROUND_TASK, task_id: record.task_id,
          }).then(/**
           * 停止回执到达后只在已受理时刷新任务状态，拒绝则交给错误提示。
           *
           * Args:
           *     receipt (CommandReceipt): 后端对任务操作的命令回执。
           */
          (receipt) => {
            if (!receipt.accepted) throw new Error(receipt.reason || "无法停止任务");
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
      this.list.appendChild(row);
    }
  }

  /**
   * 读取选中任务的下一页完整输出，忽略切换任务、会话或重复游标的迟到页。
   *
   * Returns:
   *     Promise<void>: 当前异步处理完成的等待结果。
   */
  private async loadOutput(): Promise<void> {
    // 1. 使用当前任务字节游标读取下一页，后端 next_offset 决定继续位置。
    if (!this.selectedTask) return;
    const sessionId = this.sessionId;
    const taskId = this.selectedTask;
    const offset = this.outputOffset;
    try {
      const page = await window.desktopPetApi.getBackgroundTaskOutput(
        sessionId, taskId, offset,
      );
      if (this.sessionId !== sessionId || this.selectedTask !== taskId || this.outputOffset !== offset) return;
      this.output.textContent += page.text;
      this.outputOffset = page.next_offset;
      this.more.hidden = !page.has_more;
    } catch (error) {
      if (this.sessionId === sessionId && this.selectedTask === taskId && this.outputOffset === offset) {
        this.output.textContent += `\n读取输出失败: ${String(error)}`;
      }
    }
  }
}
