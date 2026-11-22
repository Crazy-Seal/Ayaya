import type { InteractionEvent, InteractionStreamReady } from "../shared-types.js";
import { InteractionStreamControlKind } from "../shared-types.js";
import { SUBSCRIPTION_RECONNECT_DELAY_MS } from "../runtime-constants.js";
import { readSseJson } from "./sse-json.js";

/** 提供全局连接、会话视图投递和应用日志所需端口。 */
export interface DesktopSessionDependencies {
  /** 发起带取消信号的事件流或快照查询。 */
  fetchBackend: (path: string, init: RequestInit) => Promise<Response>;
  /** 将会话快照和增量交给提醒处理及窗口转发。 */
  deliverEvent: (event: InteractionEvent) => void;
  /** 实时更新所有会话的提醒，当前页面缓冲结束后重新按快照校正。 */
  observeEvent?: (event: InteractionEvent) => void;
  /** 记录连接和快照查询失败。 */
  log: (level: "info" | "warn", message: string, error?: unknown) => void;
  reconnectDelayMs?: number;
  /** 建立连接前刷新外部后端身份。 */
  onConnected?: (signal: AbortSignal) => Promise<void>;
  /** 通知页面全局连接已经断开。 */
  onDisconnected?: () => void;
  /** 通知当前页面自动恢复快照失败。 */
  onSnapshotError?: (sessionId: string, error: unknown) => void;
}

/** 持有当前页面的一次快照查询和等待应用的增量。 */
interface SessionView {
  sessionId: string;
  controller: AbortController;
  loading: boolean;
  events: InteractionEvent[];
}

/**
 * 维持应用的一条全局流，按需初始化当前会话视图。
 *
 * Attributes:
 *     view (SessionView | null): 当前快照查询和增量缓冲，切换时替换。
 *     sequences (Map<string, number>): 当前连接中每会话已接收的序号。
 */
export class DesktopSessionController {
  private controller: AbortController | null = null;
  private connection: AbortController | null = null;
  private view: SessionView | null = null;
  private ready = false;
  private epoch = "";
  private readonly waiters = new Set<() => void>();
  private readonly sequences = new Map<string, number>();

  /** 保存网络和窗口端口，应用启动时显式建立连接。
   * Args:
   *     dependencies (DesktopSessionDependencies): 网络、提醒及页面投递入口。
   */
  constructor(private readonly dependencies: DesktopSessionDependencies) {}

  /** 建立应用拥有的全局流，重复调用复用已有连接。 */
  start(): void {
    if (this.controller) return;
    this.controller = new AbortController();
    void this.run(this.controller);
  }

  /**
   * 等待后端确认订阅已登记，用户发送消息前复用此等待。
   *
   * Returns:
   *     Promise<void>: 当前全局连接已经就绪。
   */
  async ensureReady(): Promise<void> {
    this.start();
    const controller = this.controller;
    if (!this.ready) await new Promise<void>(/** 保存就绪等待，关闭时统一释放。 */
      (resolve) => { this.waiters.add(resolve); });
    if (controller !== this.controller || controller?.signal.aborted) throw new Error("全局事件连接已停止");
  }

  /**
   * 缓冲当前会话后查询快照，将快照及更新增量依次交给页面。
   *
   * Args:
   *     sessionId (string): 页面选择展示的主会话。
   *
   * Returns:
   *     Promise<void>: 快照和缓冲已转发，或本次视图已被替换。
   */
  async initializeSession(sessionId: string): Promise<void> {
    this.view?.controller.abort();
    const view: SessionView = { sessionId, controller: new AbortController(), loading: true, events: [] };
    this.view = view;
    try {
      // 1. 先建立视图缓冲，等待后端登记全局订阅后才查询状态。
      await this.ensureReady();
      if (!this.isCurrentView(view)) return;
      const response = await this.dependencies.fetchBackend(
        `/agent/snapshot/${encodeURIComponent(sessionId)}?channel=desktop`, { signal: view.controller.signal },
      );
      if (!this.isCurrentView(view)) return;
      if (!response.ok) throw new Error(`会话快照查询失败: ${response.status}`);
      const snapshot = await response.json() as InteractionEvent;
      if (!this.isCurrentView(view)) return;
      if (snapshot.stream_epoch !== this.epoch) { this.connection?.abort(); return; }
      // 2. 先恢复快照，再按会话水位交付查询期间的后续事件。
      this.dependencies.observeEvent?.(snapshot);
      this.dependencies.deliverEvent(snapshot);
      for (const event of view.events) {
        if (event.event_sequence > snapshot.event_sequence) {
          this.dependencies.observeEvent?.(event);
          this.dependencies.deliverEvent(event);
        }
      }
      view.events = [];
      view.loading = false;
    } catch (error) {
      if (!this.isCurrentView(view)) return;
      view.loading = false;
      view.events = [];
      throw error;
    }
  }

  /** 结束应用观察连接，取消当前快照并释放连接等待。 */
  stop(): void {
    this.controller?.abort(); this.connection?.abort(); this.view?.controller.abort();
    this.controller = null; this.connection = null; this.view = null; this.ready = false;
    this.releaseWaiters();
  }

  /** 判断查询结果是否仍属于当前页面和有效连接。
   * Args:
   *     view (SessionView): 发起查询时捕获的页面初始化记录。
   * Returns:
   *     boolean: 当前页面仍在等待这次查询时为真。
   */
  private isCurrentView(view: SessionView): boolean {
    return this.view === view && !view.controller.signal.aborted;
  }

  /** 连接就绪或关闭后释放正在等待的调用方。 */
  private releaseWaiters(): void {
    for (const resolve of this.waiters) resolve();
    this.waiters.clear();
  }

  /** 按会话去重，将初始化中的当前会话事件保留到快照之后。
   * Args:
   *     event (InteractionEvent): 已由后端按渠道筛选的实时事件。
   */
  private receiveEvent(event: InteractionEvent): void {
    if (event.stream_epoch !== this.epoch || event.event_sequence <= (this.sequences.get(event.session_id) ?? -1)) return;
    this.sequences.set(event.session_id, event.event_sequence);
    this.dependencies.observeEvent?.(event);
    if (this.view?.loading && this.view.sessionId === event.session_id) this.view.events.push(event);
    else this.dependencies.deliverEvent(event);
  }

  /** 持续接收全局事件，重连就绪时只恢复当前页面的会话。
   * Args:
   *     controller (AbortController): 应用持有的全局观察生命周期。
   * Returns:
   *     Promise<void>: 应用停止观察后结束。
   */
  private async run(controller: AbortController): Promise<void> {
    let reconnecting = false;
    while (!controller.signal.aborted) {
      const connection = new AbortController();
      this.connection = connection;
      try {
        if (this.dependencies.onConnected) await this.dependencies.onConnected(connection.signal);
        if (controller.signal.aborted) return;
        const response = await this.dependencies.fetchBackend("/agent/events?channel=desktop", { signal: connection.signal });
        if (controller.signal.aborted) return;
        if (!response.ok) throw new Error(`事件订阅失败: ${response.status}`);
        for await (const frame of readSseJson<InteractionEvent | InteractionStreamReady>(response, connection.signal)) {
          if (controller.signal.aborted) return;
          if (frame.kind === InteractionStreamControlKind.READY) {
            this.epoch = frame.stream_epoch;
            this.sequences.clear(); this.ready = true; this.releaseWaiters();
            if (reconnecting) this.dependencies.log("info", "交互事件流已重新连接");
            reconnecting = false;
            if (this.view && !this.view.loading) {
              const sessionId = this.view.sessionId;
              void this.initializeSession(sessionId).catch(/** 自动恢复失败时通知页面并记录原因。 */
                (error: unknown) => {
                  this.dependencies.log("warn", "重连后查询会话快照失败", error);
                  this.dependencies.onSnapshotError?.(sessionId, error);
                });
            }
          } else if (this.ready) this.receiveEvent(frame);
        }
        if (!controller.signal.aborted) throw new Error("交互事件订阅意外结束");
      } catch (error) {
        if (controller.signal.aborted) return;
        // 1. 连接失效立即取消快照，旧响应由视图身份拒绝。
        connection.abort(); this.ready = false;
        if (this.view) { this.view.controller.abort(); this.view.loading = false; this.view.events = []; }
        this.dependencies.onDisconnected?.();
        if (!reconnecting) this.dependencies.log("warn", "交互事件流断开，将重新订阅", error);
        reconnecting = true;
      }
      if (!controller.signal.aborted) await this.waitToReconnect(controller.signal);
    }
  }

  /** 等待重试间隔，应用停止时立即结束等待。
   * Args:
   *     signal (AbortSignal): 应用退出时结束重试等待的信号。
   * Returns:
   *     Promise<void>: 计时完成或应用已经停止观察。
   */
  private waitToReconnect(signal: AbortSignal): Promise<void> {
    return new Promise<void>(/** 保存计时器和取消入口，完成后移除监听。 */
      (resolve) => {
        /** 释放当前重试等待。 */
        const finish = (): void => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
        const timer = setTimeout(finish, this.dependencies.reconnectDelayMs ?? SUBSCRIPTION_RECONNECT_DELAY_MS);
        signal.addEventListener("abort", finish, { once: true });
        if (signal.aborted) finish();
      });
  }
}
