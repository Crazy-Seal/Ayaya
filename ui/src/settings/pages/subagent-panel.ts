import type { DesktopPetApi, SubagentDetail, SubagentSummary } from "../../types.js";
import { CommandKind, DESKTOP_CHANNEL, SubagentStatus } from "../../../shared-types.js";
import { TASK_REFRESH_INTERVAL_MS } from "../../../runtime-constants.js";
import type { SettingsNotifier } from "../components/settings-toast.js";

type SubagentBridge = Pick<DesktopPetApi, "getSubagents" | "getSubagentDetails" | "submitInteractionCommand">;

/**
 * 在独立设置面板展示运行中及空闲可复用子 Agent，普通后台进程仍在原任务页。
 *
 * Attributes:
 *     sessionId (string): 当前父会话，跨会话迟到查询不会显示。
 *     revision (number): 查询代次，只显示最新结果。
 *     timer (ReturnType<typeof setInterval> | null): 仅页面可见时更新状态和耗时。
 */
export class SubagentPanel {
  private sessionId = "";
  private revision = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly expanded = new Map<string, string>();

  /**
   * 装配列表、手动刷新和 IPC 查询端口。
   *
   * Args:
   *     list (HTMLElement): 子 Agent 独立列表容器。
   *     refresh (HTMLButtonElement): 手动刷新入口。
   *     notifier (SettingsNotifier | undefined): 展示操作失败。
   *     bridge (SubagentBridge): 查询及目标子任务停止入口。
   */
  constructor(
    private readonly list: HTMLElement,
    refresh: HTMLButtonElement,
    private readonly notifier?: SettingsNotifier,
    private readonly bridge: SubagentBridge = window.desktopPetApi,
  ) {
    refresh.addEventListener("click", /** 手动读取当前父会话的子名单。 */
      () => { void this.render(this.sessionId); });
  }

  /** 页面切换时启动或停止轮询，后台页面不持续请求。 */
  setActive(active: boolean): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = active ? setInterval(/** 刷新运行状态及耗时。 */
      () => { void this.render(this.sessionId); }, TASK_REFRESH_INTERVAL_MS) : null;
  }

  /**
   * 读取并显示指定父会话的当前名单，不向后端发送执行命令。
   *
   * Args:
   *     sessionId (string): 所属父会话。
   */
  async render(sessionId: string): Promise<void> {
    if (sessionId !== this.sessionId) {
      this.expanded.clear();
      this.list.replaceChildren();
    }
    this.sessionId = sessionId;
    const revision = ++this.revision;
    if (!sessionId) return;
    try {
      const records = await this.bridge.getSubagents(sessionId);
      if (sessionId !== this.sessionId || revision !== this.revision) return;
      this.list.replaceChildren();
      if (!records.length) this.list.textContent = "暂无子 Agent";
      for (const record of records) this.list.append(this.card(record, sessionId));
    } catch (error) {
      if (sessionId === this.sessionId && revision === this.revision) this.notifier?.error(`读取子 Agent 失败：${String(error)}`);
    }
  }

  /** 从真实摘要创建卡片，空闲子 Agent 不提供当前任务停止按钮。 */
  private card(record: SubagentSummary, sessionId: string): HTMLElement {
    const doc = this.list.ownerDocument;
    const card = doc.createElement("article");
    card.className = "background-task-card ui-surface";
    const row = doc.createElement("div");
    row.className = "background-task-row";
    const title = doc.createElement("span");
    const labels = {
      [SubagentStatus.IDLE]: "空闲", [SubagentStatus.RUNNING]: "正在运行", [SubagentStatus.WAITING_PARENT]: "等待父 Agent 回复",
    };
    const elapsed = record.started_at ? Math.max(0, Math.floor((Date.now() - Date.parse(record.started_at)) / 1000)) : null;
    title.textContent = `${record.name} · ${record.if_retain_context ? "可复用" : "一次性"} · ${record.waiting_for_approval ? "等待用户审批" : labels[record.status]}`
      + (elapsed === null ? "" : ` · ${Math.floor(elapsed / 60)}分${elapsed % 60}秒`);
    const identifier = doc.createElement("p");
    identifier.textContent = record.subagent_id;
    const detail = doc.createElement("button");
    detail.type = "button";
    detail.className = "ui-button ui-button--secondary ui-button--small";
    detail.textContent = "详情";
    const output = doc.createElement("pre");
    output.className = "background-task-output";
    output.hidden = !this.expanded.has(record.subagent_id);
    output.textContent = this.expanded.get(record.subagent_id) ?? "";
    detail.addEventListener("click", /** 展开定义、显式交互和最终报告，不请求子正文流。 */
      () => { void this.showDetails(sessionId, record.subagent_id, output); });
    row.append(title, detail);
    if (record.status !== SubagentStatus.IDLE) {
      const stop = doc.createElement("button");
      stop.type = "button";
      stop.className = "ui-button ui-button--danger ui-button--small";
      stop.textContent = "停止";
      stop.addEventListener("click", /** 只停止目标子任务，主 Agent 及其他子 Agent 继续。 */
        () => { void this.stop(sessionId, record.subagent_id, stop); });
      row.append(stop);
    }
    card.append(row, identifier, output);
    if (record.error) {
      const error = doc.createElement("p");
      error.textContent = `收尾失败：${record.error}`;
      card.append(error);
    }
    return card;
  }

  /** 展示按 ID 保存的事实，查询迟到时不覆盖另一个会话的页面。 */
  private async showDetails(sessionId: string, subagentId: string, output: HTMLElement): Promise<void> {
    output.hidden = !output.hidden;
    if (output.hidden) {
      this.expanded.delete(subagentId);
      return;
    }
    this.expanded.set(subagentId, "正在读取详情…");
    output.textContent = "正在读取详情…";
    try {
      const detail = await this.bridge.getSubagentDetails(sessionId, subagentId);
      if (sessionId !== this.sessionId || !this.expanded.has(subagentId)) return;
      const text = this.detailsText(detail);
      this.expanded.set(subagentId, text);
      // 查询返回期间轮询可能重建卡片，刷新列表以显示已展开的最新详情。
      await this.render(sessionId);
    } catch (error) {
      if (sessionId !== this.sessionId || !this.expanded.has(subagentId)) return;
      // 1. 保存失败提示，后续轮询重建卡片时继续显示；收起后重新展开会再次查询。
      const message = `读取详情失败：${String(error)}`;
      this.expanded.set(subagentId, message);
      output.textContent = message;
      // 2. 查询期间轮询可能替换原节点，刷新当前列表让错误显示在实际卡片上。
      await this.render(sessionId);
    }
  }

  /** 使用纯文本渲染父子提供的提示词和内容，避免把内容当作 HTML。 */
  private detailsText(detail: SubagentDetail): string {
    const reports = detail.results.map(/** 按实际保存状态显示最终报告。 */
      (result) => `${result.created_at} → ${result.ended_at ?? ""} [${result.status}]\n${result.result ?? ""}\n${result.error ?? ""}`);
    const interactions = detail.recent_interactions.map(/** 保留交互来源和发生时间。 */
      (item) => `${item.created_at} [${item.kind}] ${item.text}`);
    return `系统提示词：\n${detail.system_prompt}\n\n工具白名单：\n${detail.tool_whitelist.join("\n") || "无普通工具"}`
      + `\n\n最近父子交互：\n${interactions.join("\n")}\n\n最终报告：\n${reports.join("\n\n") || "暂无最终报告"}`;
  }

  /** 停止按钮等待受理回执，不能把“已受理”展示成资源已经清理完成。 */
  private async stop(sessionId: string, subagentId: string, button: HTMLButtonElement): Promise<void> {
    button.disabled = true;
    try {
      const receipt = await this.bridge.submitInteractionCommand({ session_id: sessionId, origin_channel: DESKTOP_CHANNEL,
        kind: CommandKind.INTERACT_SUBAGENT, subagent_id: subagentId, payload: { action: "stop" } });
      if (!receipt.accepted) throw new Error(receipt.reason || "停止未受理");
      if (sessionId === this.sessionId) await this.render(sessionId);
    } catch (error) {
      button.disabled = false;
      this.notifier?.error(`停止子 Agent 失败：${String(error)}`);
    }
  }

  /** 页面卸载时停止轮询并让未完成查询失效。 */
  dispose(): void {
    this.setActive(false);
    this.revision += 1;
  }
}
