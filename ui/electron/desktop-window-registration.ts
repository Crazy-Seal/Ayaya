/** 只同步两个窗口的身份，并在后端意外退出时恢复原可见窗口，不执行屏幕工具。 */
import fs from "node:fs";

/** 主进程窗口登记与恢复需要的最小原生窗口能力。 */
export interface RegisteredDesktopWindow {
  /** 判断对象是否已释放。 */ isDestroyed(): boolean;
  /** 判断用户是否最小化窗口。 */ isMinimized(): boolean;
  /** 判断当前窗口是否已经显示。 */ isVisible(): boolean;
  /** 获取原生 HWND 字节，不能用页面标题猜测身份。 */ getNativeWindowHandle(): Buffer;
  /** 只恢复可见状态，不主动取得输入焦点。 */ showInactive(): void;
}

/** 当前明确的两个角色，其他应用窗口不进入此快照。 */
type Windows = { pet: RegisteredDesktopWindow | null; chat: RegisteredDesktopWindow | null };

/** 身份登记使用受认证请求和现有退出状态，测试可替换 HTTP 与原生窗口。 */
interface Dependencies {
  ownerPid: number;
  getWindows: () => Windows;
  isShuttingDown: () => boolean;
  fetchBackend: (path: string, init?: RequestInit) => Promise<Response>;
  logError: (message: string, error: unknown) => void;
  /** 可替换的本机进程存活检查；读取 PID 身份，不终止或操控进程。 */
  isBackendAlive?: (pid: number) => boolean;
  /** 确认本机后端已退出并恢复窗口后通知现有恢复对话框。 */
  onBackendExited?: () => void;
}

/**
 * 将一个 HWND 转成无精度损失的十进制字符串。
 * Args:
 *     window (RegisteredDesktopWindow | null): 当前角色原生窗口。
 * Returns:
 *     string | null: 存活窗口句柄，未创建或销毁时为空。
 */
export const desktopWindowHandle = (window: RegisteredDesktopWindow | null): string | null => {
  if (!window || window.isDestroyed()) return null;
  const handle = window.getNativeWindowHandle();
  return handle.length === 8 ? handle.readBigUInt64LE().toString() : String(handle.readUInt32LE());
};

/** 身份快照更新串行发送，连接代次隔离旧结果，恢复仅操作当前已知的两个窗口。 */
export class DesktopWindowRegistrationController {
  private generation = 0;
  private revision = 0;
  private acknowledged = 0;
  private controller: AbortController | null = null;
  private pending: Promise<void> | null = null;
  private restorePath: string | null = null;

  /**
   * 保存主进程依赖，构造时不发送请求或读取恢复记录。
   * Args:
   *     dependencies (Dependencies): 准确窗口引用、认证请求、退出门禁和现有恢复通知。
   */
  constructor(private readonly dependencies: Dependencies) {}

  /**
   * 连接或重连后发送完整身份快照，旧请求回报不能接到本次连接。
   * Returns:
   *     Promise<void>: 当前快照已确认，退出时静默结束。
   */
  async connect(): Promise<void> {
    this.disconnect();
    if (this.dependencies.isShuttingDown()) return;
    this.controller = new AbortController();
    await this.refresh();
  }

  /** 后端退出或应用关闭后取消登记等待，不改变窗口显示状态。 */
  disconnect(): void {
    this.generation += 1;
    this.controller?.abort();
    this.controller = null;
    this.pending = null;
  }

  /**
   * 窗口创建或销毁后更新完整快照；多个更新只共用一条串行请求链。
   * Returns:
   *     Promise<void>: 当前及等待中新增的角色变化均已发送完成。
   */
  async refresh(): Promise<void> {
    if (!this.controller || this.dependencies.isShuttingDown()) return;
    this.revision += 1;
    // 1. 变化先推进 revision，再共用当前请求；publish 完成当前响应后补发最新完整快照。
    if (this.pending) return this.pending;
    const controller = this.controller;
    const generation = this.generation;
    const operation = this.publish(controller, generation);
    this.pending = operation;
    try { await operation; }
    // 2. 旧连接的迟到收尾不能清除新连接正在发送的请求。
    finally { if (this.pending === operation) this.pending = null; }
  }

  /**
   * 在接受真人命令前等待当前窗口登记，退出时不开放新的执行入口。
   * Returns:
   *     Promise<void>: 当前快照已登记且仍接受命令。
   * Raises:
   *     Error: 应用已退出、后端断开或最新快照没有登记成功。
   */
  async ensureReady(): Promise<void> {
    if (!this.controller || this.dependencies.isShuttingDown()) throw new Error("桌面窗口尚未登记或应用正在退出");
    if (this.pending) await this.pending;
    if (this.acknowledged < this.revision) await this.refresh();
    if (!this.controller || this.dependencies.isShuttingDown()) throw new Error("应用正在退出或后端已断开");
  }

  /**
   * 每次读取最新角色快照，旧连接回报直接作废，仍有变化则继续发送。
   * Args:
   *     controller (AbortController): 本次连接的取消控制器。
   *     generation (number): 本次连接代次。
   */
  private async publish(controller: AbortController, generation: number): Promise<void> {
    while (!controller.signal.aborted && generation === this.generation && !this.dependencies.isShuttingDown()
      && this.acknowledged < this.revision) {
      const revision = this.revision;
      const windows = this.dependencies.getWindows();
      const response = await this.dependencies.fetchBackend("/internal/desktop-windows", {
        method: "POST", signal: controller.signal, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner_pid: this.dependencies.ownerPid, revision,
          windows: { pet: desktopWindowHandle(windows.pet), chat: desktopWindowHandle(windows.chat) } }),
      });
      if (!response.ok) throw new Error(`登记桌面窗口失败: ${response.status}`);
      const receipt = await response.json() as { accepted: boolean; window_restore_path: string };
      // 1. 响应晚于断线、重连或退出时完全作废，不能推进确认 revision 或替换恢复路径。
      if (controller.signal.aborted || generation !== this.generation || this.dependencies.isShuttingDown()) return;
      if (!receipt.accepted) throw new Error("后端拒绝当前窗口身份快照");
      this.acknowledged = revision;
      this.restorePath = receipt.window_restore_path;
    }
  }

  /** SSE 断线时仅在恢复记录对应的本机后端 PID 确认已退出后恢复窗口。 */
  recoverIfBackendExited(): void {
    if (this.dependencies.isShuttingDown() || !this.restorePath || !fs.existsSync(this.restorePath)) return;
    try {
      const record = JSON.parse(fs.readFileSync(this.restorePath, "utf8")) as { owner_pid: number; backend_pid: number };
      if (record.owner_pid !== this.dependencies.ownerPid || !Number.isSafeInteger(record.backend_pid) || record.backend_pid <= 0) return;
      // 1. 信号 0 仅查询进程是否存在；权限错误按仍存活处理，不能误恢复正在操作的窗口。
      let alive = true;
      if (this.dependencies.isBackendAlive) alive = this.dependencies.isBackendAlive(record.backend_pid);
      else {
        try { process.kill(record.backend_pid, 0); }
        catch (error) { alive = (error as NodeJS.ErrnoException).code !== "ESRCH"; }
      }
      if (alive) return;
      // 2. 先封住旧登记入口并恢复匹配窗口，再交给应用原有重连/退出对话框。
      this.disconnect();
      this.recoverWindows();
      this.dependencies.onBackendExited?.();
    } catch (error) { this.dependencies.logError("检查后端退出恢复记录失败", error); }
  }

  /** 后端意外退出时按记录恢复匹配窗口，应用已经退出则完全跳过，不创建或聚焦窗口。 */
  recoverWindows(): void {
    if (this.dependencies.isShuttingDown() || !this.restorePath || !fs.existsSync(this.restorePath)) return;
    try {
      const record = JSON.parse(fs.readFileSync(this.restorePath, "utf8")) as {
        owner_pid: number; windows: Array<{ role: "pet" | "chat"; hwnd: string }>;
      };
      if (record.owner_pid !== this.dependencies.ownerPid || !Array.isArray(record.windows)) return;
      const current = this.dependencies.getWindows();
      let failed = false;
      for (const saved of record.windows) {
        if (saved.role !== "pet" && saved.role !== "chat") continue;
        const window = current[saved.role];
        // 1. 单个恢复失败仍继续其他角色，并保留记录供下一次恢复；不创建丢失窗口。
        try {
          if (window && !window.isDestroyed() && !window.isMinimized() && desktopWindowHandle(window) === saved.hwnd
            && !window.isVisible()) window.showInactive();
        } catch (error) { failed = true; this.dependencies.logError("恢复桌面窗口失败", error); }
      }
      if (!failed) fs.unlinkSync(this.restorePath);
    } catch (error) { this.dependencies.logError("恢复桌面窗口失败", error); }
  }
}
