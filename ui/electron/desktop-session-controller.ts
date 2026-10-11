import type { DesktopSessionSubscriptionOptions, InteractionEvent } from "../shared-types.js";
import { InteractionEventKind } from "../shared-types.js";
import { SUBSCRIPTION_RECONNECT_DELAY_MS } from "../runtime-constants.js";
import { readSseJson } from "./sse-json.js";

/**
 * 提供会话订阅所需的后端请求、事件转发和日志端口，不依赖 Electron 窗口。
 *
 * Attributes:
 *     fetchBackend ((path: string, init: RequestInit) => Promise<Response>): 带认证的后端请求函数。
 *     deliverEvent ((event: InteractionEvent) => void): 将当前连接事件交给聊天页和设置页的同步函数。
 *     log ((level: "info" | "warn", message: string, error?: unknown) => void): 记录断线和恢复的函数。
 *     reconnectDelayMs (number | undefined): 重试间隔，测试可缩短；生产使用统一常量。
 */
export interface DesktopSessionDependencies {
  /** 发起带取消信号的会话事件请求。 */
  fetchBackend: (path: string, init: RequestInit) => Promise<Response>;
  /** 将已经核对连接归属的事件同步转发给界面。 */
  deliverEvent: (event: InteractionEvent) => void;
  /** 记录当前订阅的断线或恢复，不记录心跳。 */
  log: (level: "info" | "warn", message: string, error?: unknown) => void;
  reconnectDelayMs?: number;
  /** 首次连接与重连后同步窗口身份，完成前不交付新的快照。 */
  onConnected?: () => Promise<void>;
  /** 断线后检查本机恢复记录，网络波动不能直接显示仍在执行的窗口。 */
  onDisconnected?: () => void;
}

/**
 * 管理一个桌面会话的展示连接，新页面可以强制重新取得快照。
 *
 * Attributes:
 *     dependencies (DesktopSessionDependencies): 后端请求、界面投递与日志端口。
 *     activeSession (string): 当前订阅会话。
 *     controller (AbortController | null): 当前请求和重试等待的取消控制器。
 *     generation (number): 每次重建或停止递增，用于拒绝旧连接迟到的结果。
 *     firstSnapshot (Promise<void> | null): 当前连接首次快照已转发的等待结果。
 *     completeSnapshot (() => void | null): 结束当前首次快照等待的函数。
 */
export class DesktopSessionController {
  private activeSession = "";
  private controller: AbortController | null = null;
  private generation = 0;
  private firstSnapshot: Promise<void> | null = null;
  private completeSnapshot: (() => void) | null = null;

  /**
   * 保存订阅依赖，此时尚未连接后端。
   *
   * Args:
   *     dependencies (DesktopSessionDependencies): 生产或测试提供的请求、投递和日志函数。
   */
  constructor(private readonly dependencies: DesktopSessionDependencies) {}

  /**
   * 建立或复用订阅；新页面请求刷新时取消旧连接，等待新快照转发。
   *
   * Args:
   *     sessionId (string): 共享 Ayaya 会话标识。
   *     options (DesktopSessionSubscriptionOptions): 是否为新页面重新取得快照。
   *
   * Returns:
   *     Promise<void>: 当前首次快照转发完成；会话切换或停止会释放旧等待。
   */
  subscribe(sessionId: string, options: DesktopSessionSubscriptionOptions = {}): Promise<void> {
    // 1. 已初始化页面的普通发送复用连接；新页面必须取得新的状态，不能复用旧完成标记。
    if (!options.refreshSnapshot && this.activeSession === sessionId
        && this.controller && !this.controller.signal.aborted) {
      return this.firstSnapshot ?? Promise.resolve();
    }
    this.stop();
    this.activeSession = sessionId;
    const controller = new AbortController();
    this.controller = controller;
    this.firstSnapshot = new Promise<void>(/**
     * Promise 创建时保存当前订阅首次快照的完成函数，快照转发或订阅作废后结束等待。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => { this.completeSnapshot = resolve; });
    // 2. 捕获代次；即使旧 fetch 忽略取消信号，其结果也不能进入当前页面。
    void this.run(sessionId, controller, this.generation);
    return this.firstSnapshot;
  }

  /**
   *  取消展示连接、结束重试等待并释放旧快照等待，不发送 Agent 停止命令。
   */
  stop(): void {
    this.generation += 1;
    this.controller?.abort();
    this.completeSnapshot?.();
    this.completeSnapshot = null;
    this.controller = null;
    this.activeSession = "";
    this.firstSnapshot = null;
  }

  /**
   * 检查异步结果是否仍属于当前连接。
   *
   * Args:
   *     controller (AbortController): 发起请求时捕获的控制器。
   *     generation (number): 发起请求时捕获的订阅代次。
   *
   * Returns:
   *     boolean: 连接未取消且仍为当前订阅时为真。
   */
  private isCurrent(controller: AbortController, generation: number): boolean {
    return !controller.signal.aborted && this.controller === controller && this.generation === generation;
  }

  /**
   * 持续消费当前会话事件，断线重连后仍先转发新的快照。
   *
   * Args:
   *     sessionId (string): 当前请求所属会话。
   *     controller (AbortController): 当前订阅取消控制器。
   *     generation (number): 当前订阅代次。
   *
   * Returns:
   *     Promise<void>: 订阅被取消或替换后结束。
   */
  private async run(sessionId: string, controller: AbortController, generation: number): Promise<void> {
    let reconnecting = false;
    while (this.isCurrent(controller, generation)) {
      try {
        // 1.1. 登记先于建立 SSE；登记失败重试不能留下无人消费的后端订阅。
        if (this.dependencies.onConnected) await this.dependencies.onConnected();
        if (!this.isCurrent(controller, generation)) return;
        // 1. 请求完成和每次读取后都核对归属，旧连接迟到的快照与文字一律不转发。
        const response = await this.dependencies.fetchBackend(
          `/agent/events/${encodeURIComponent(sessionId)}?channel=desktop`,
          { signal: controller.signal },
        );
        if (!this.isCurrent(controller, generation)) return;
        if (!response.ok) throw new Error(`事件订阅失败: ${response.status}`);
        if (reconnecting) this.dependencies.log("info", "交互事件流已重新连接");
        reconnecting = false;
        for await (const event of readSseJson<InteractionEvent>(response, controller.signal)) {
          if (!this.isCurrent(controller, generation)) break;
          this.dependencies.deliverEvent(event);
          // 2. 先转发快照再结束主进程等待；Renderer 另行等待历史和状态应用完成。
          if (this.isCurrent(controller, generation) && event.kind === InteractionEventKind.SESSION_SNAPSHOT) {
            this.completeSnapshot?.();
            this.completeSnapshot = null;
          }
        }
        if (this.isCurrent(controller, generation)) throw new Error("交互事件订阅意外结束");
      } catch (error) {
        if (this.isCurrent(controller, generation)) this.dependencies.onDisconnected?.();
        // 3. 同一次断线只记录首次失败；正常取消不写日志，也不取消 Agent。
        if (this.isCurrent(controller, generation) && !reconnecting) {
          this.dependencies.log("warn", "交互事件流断开，将重新订阅", error);
        }
        reconnecting = true;
      }
      if (this.isCurrent(controller, generation)) await this.waitToReconnect(controller.signal);
    }
  }

  /**
   * 等待重试间隔；连接停止时立即结束等待并清理计时器和监听器。
   *
   * Args:
   *     signal (AbortSignal): 当前订阅取消信号。
   *
   * Returns:
   *     Promise<void>: 计时结束或订阅取消后完成。
   */
  private waitToReconnect(signal: AbortSignal): Promise<void> {
    return new Promise<void>(/**
     * Promise 创建时登记重连间隔与取消监听，任一到达都清理定时器后结束等待。
     *
     * Args:
     *     resolve ((value: void | PromiseLike<void>) => void): 准备成功或等待结束时完成 Promise 的函数。
     */
    (resolve) => {
      /**
       *  清理重连计时器及取消监听，然后结束等待。
       */
      const finish = (): void => {
        clearTimeout(timer);
        signal.removeEventListener("abort", finish);
        resolve();
      };
      const timer = setTimeout(finish, this.dependencies.reconnectDelayMs ?? SUBSCRIPTION_RECONNECT_DELAY_MS);
      signal.addEventListener("abort", finish, { once: true });
      if (signal.aborted) finish();
    });
  }
}
