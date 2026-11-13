import type { BackgroundTaskRecord, DesktopPetApi } from "../types.js";
import { TaskStatus, TaskExecutionPhase } from "../../shared-types.js";
import { TASK_NOTICE_DURATION_MS } from "../../runtime-constants.js";

/**
 * 在聊天记录标题栏显示运行任务入口和短暂终态通知，详情交给设置页。
 *
 * Attributes:
 *     sessionId (string): 当前共享会话，切换后忽略旧查询。
 *     records (BackgroundTaskRecord[]): 仅用于计算正在运行数量的当前任务快照。
 *     revision (number): 状态代次，事件和快照使迟到的查询失效。
 *     noticeTimer (ReturnType<typeof setTimeout> | null): 四秒后清除终态通知。
 */
export class BackgroundTaskEntry {
  private sessionId = "";
  private records: BackgroundTaskRecord[] = [];
  private revision = 0;
  private noticeTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * 装配标题栏控件和查询端口，点击时直接进入已有任务详情页。
   *
   * Args:
   *     root (HTMLElement): 参与正常布局的聊天标题栏。
   *     button (HTMLButtonElement): 运行任务快捷入口。
   *     notice (HTMLElement): 终态短提示。
   *     bridge (Pick<DesktopPetApi, "getBackgroundTasks">): 当前会话任务查询端口。
   *     openDetails (() => void): 打开已有设置窗口任务页的回调。
   */
  constructor(
    private readonly root: HTMLElement,
    private readonly button: HTMLButtonElement,
    private readonly notice: HTMLElement,
    private readonly bridge: Pick<DesktopPetApi, "getBackgroundTasks">,
    openDetails: () => void,
  ) {
    this.button.addEventListener("click", openDetails);
  }

  /**
   * 切换会话并清除旧快照与短提示，随后查询当前记录。
   *
   * Args:
   *     sessionId (string): 当前共享会话标识。
   */
  setSessionId(sessionId: string): void {
    if (this.sessionId === sessionId) return;
    this.sessionId = sessionId;
    this.revision += 1;
    this.records = [];
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    this.notice.textContent = "";
    this.render();
    void this.refresh();
  }

  /**
   * 应用订阅快照，历史终态记录不产生运行徽标或重复完成通知。
   *
   * Args:
   *     tasks (unknown): 后端任务列表，必须是数组才应用。
   */
  setSnapshot(tasks: unknown): void {
    if (!Array.isArray(tasks)) return;
    this.revision += 1;
    this.records = tasks as BackgroundTaskRecord[];
    this.render();
  }

  /**
   * 即时更新已知任务的状态，并用独立查询补齐新任务记录。
   *
   * Args:
   *     taskId (string): 状态变更的任务身份。
   *     status (string): 后端运行或终态标记。
   *     toolName (string | null | undefined): 创建任务的工具名，缺失时保留原值。
   */
  onStatus(taskId: string, status: string, toolName?: string | null): void {
    // 1. 先更新已知任务，完成事件到达后不能继续显示旧运行数量。
    this.revision += 1;
    this.records = this.records.map(/**
     * 状态事件到达后只替换对应任务的状态，保留其他任务记录。
     *
     * Args:
     *     record (BackgroundTaskRecord): 当前后台任务记录，包含编号、执行状态及结果信息。
     *
     * Returns:
     *     BackgroundTaskRecord: 更新后的任务记录。
     */
    (record) => record.task_id === taskId
      ? { ...record, status: status as BackgroundTaskRecord["status"], tool_name: toolName === undefined ? record.tool_name : toolName } : record);
    if (status !== TaskStatus.RUNNING) {
      const labels: Record<string, string> = {
        [TaskStatus.COMPLETED]: "已完成", [TaskStatus.FAILED]: "失败",
        [TaskStatus.STOPPED]: "已停止", [TaskStatus.ORPHANED]: "已失联",
        [TaskExecutionPhase.SAVE_FAILED]: "结果保存失败",
      };
      const label = labels[status] || status;
      this.showNotice(`任务 ${taskId.slice(0, 8)} ${label}`);
    }
    this.render();
    // 2. 状态事件不附完整任务列表，新启动的任务通过查询补齐；迟到结果有代次保护。
    void this.refresh();
  }

  /**
   * 读取当前任务数量，事件或会话切换后丢弃旧查询。
   *
   * Returns:
   *     Promise<void>: 当前查询完成或失败短提示已经展示。
   */
  private async refresh(): Promise<void> {
    const sessionId = this.sessionId;
    const revision = ++this.revision;
    if (!sessionId) return;
    try {
      const records = await this.bridge.getBackgroundTasks(sessionId);
      if (sessionId !== this.sessionId || revision !== this.revision) return;
      this.records = records;
      this.render();
    } catch (error) {
      if (sessionId === this.sessionId && revision === this.revision) {
        this.showNotice(`任务状态读取失败: ${String(error)}`);
      }
    }
  }

  /**
   * 显示四秒通知，结束后没有运行任务就隐藏整行。
   *
   * Args:
   *     text (string): 需要短暂显示的状态或错误说明。
   */
  private showNotice(text: string): void {
    if (this.noticeTimer) clearTimeout(this.noticeTimer);
    this.notice.textContent = text;
    this.render();
    this.noticeTimer = setTimeout(/**
     * 任务结束提示到期后清空通知和计时标记，再刷新任务入口。
     */
    () => { this.notice.textContent = ""; this.noticeTimer = null; this.render(); }, TASK_NOTICE_DURATION_MS);
  }

  /**
   *  根据正在运行的数量和短提示控制标题栏，不展示历史任务总数。
   */
  private render(): void {
    const running = this.records.filter(/**
     * 筛选仍在运行的任务，入口计数不包含完成或未知任务。
     *
     * Args:
     *     record (BackgroundTaskRecord): 当前后台任务记录，包含编号、执行状态及结果信息。
     *
     * Returns:
     *     boolean: 状态为 running 时为 true。
     */
    (record) => record.status === TaskStatus.RUNNING).length;
    this.button.textContent = `任务 · ${running}`;
    this.button.hidden = running === 0;
    this.notice.hidden = !this.notice.textContent;
    this.root.hidden = running === 0 && this.notice.hidden;
  }
}
