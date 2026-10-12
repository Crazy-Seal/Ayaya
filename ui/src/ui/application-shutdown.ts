/** 全部页面共用退出等待界面，拦截鼠标、键盘、表单和拖入等功能入口。 */
import type { DesktopPetApi } from "../../shared-types.js";

/** 退出等待只需要状态查询和主进程通知，不接触聊天或设置业务。 */
type ShutdownApi = Pick<DesktopPetApi, "getApplicationShutdownState" | "onApplicationShutdown">;

/**
 * 为一个页面建立不可撤销的退出锁定，通知与首次查询都能触发同一入口。
 * Args:
 *     target (Window): 当前渲染页面及其 DOM。
 *     api (ShutdownApi): preload 提供的状态查询与通知接口。
 * Returns:
 *     { install: () => void; lock: () => void; dispose: () => void }: 安装、确认后立即锁定和卸载清理入口。
 */
export const createApplicationShutdownUi = (target: Window, api: ShutdownApi) => {
  let locked = false;
  let installed = false;
  let disposed = false;
  let unsubscribe: (() => void) | null = null;
  let observer: MutationObserver | null = null;
  let shutdownOverlay: HTMLElement | null = null;
  const events = ["click", "dblclick", "pointerdown", "pointerup", "keydown", "keyup",
    "pointermove", "mousedown", "mouseup", "mousemove", "touchstart", "touchmove", "touchend",
    "beforeinput", "input", "change", "submit", "wheel", "dragstart", "dragover", "drop", "paste", "contextmenu"];
  /**
   * 在现有页面回调之前拦截退出期间的事件，包括不经过 IPC 的搜索与导航。
   * Args:
   *     event (Event): 鼠标、键盘或表单输入事件。
   */
  const blockInteraction = (event: Event): void => {
    if (!locked) return;
    event.preventDefault(); event.stopImmediatePropagation();
  };
  /** 确认退出或收到主进程通知后锁定功能控件并展示等待状态，重复调用不重复插入。 */
  const lock = (): void => {
    if (locked || disposed) return;
    locked = true;
    const doc = target.document;
    doc.body.dataset.applicationShutdown = "true";
    const root = doc.querySelector<HTMLElement>(".ui-window-frame") ?? doc.body;
    // 1. 先锁定现有控件与焦点；异步回调即使重新启用按钮，事件门禁仍不允许操作。
    /** 退出控制器首次锁定或 DOM 更新后禁用全部功能控件，退出提示自身保持可显示。 */
    const disableControls = (): void => {
      // 1. 异步返回或重绘后出现的新控件也必须禁用，不能仅锁定退出瞬间已有的按钮。
      for (const control of doc.querySelectorAll<HTMLButtonElement | HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>("button, input, textarea, select")) {
        if (!control.disabled) control.disabled = true;
      }
      // 2. 禁止页面内容获得焦点；退出等待提示保持可显示和可聚焦。
      for (const child of root.children) {
        if (child !== shutdownOverlay && !(child as HTMLElement).inert) (child as HTMLElement).inert = true;
      }
    };
    disableControls();
    (doc.activeElement as HTMLElement | null)?.blur();
    // 2. 继续约束迟到的 UI 更新；事件捕获负责立即阻止操作，观察器负责维持禁用外观和焦点权限。
    const Observer = (target as Window & typeof globalThis).MutationObserver;
    if (typeof Observer === "function") {
      observer = new Observer(/** DOM 新增控件或异步回调重新启用控件时，重新应用退出禁用状态。 */
      disableControls);
      observer.observe(root, { subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "inert"] });
    }
    // 3. 角色透明页面只停止交互，带标题栏的功能窗口显示共用的退出提示。
    if (!doc.querySelector(".ui-titlebar, .settings-titlebar")) return;
    const overlay = doc.createElement("div"); overlay.className = "ui-shutdown-overlay";
    shutdownOverlay = overlay;
    overlay.setAttribute("role", "status"); overlay.setAttribute("aria-live", "polite"); overlay.tabIndex = -1;
    const card = doc.createElement("div"); card.className = "ui-surface ui-shutdown-message";
    const title = doc.createElement("div"); title.className = "ui-window-title"; title.textContent = "正在退出…";
    const message = doc.createElement("p"); message.textContent = "正在停止对话和后台任务，请稍候。";
    card.append(title, message); overlay.append(card); root.append(overlay); overlay.focus();
  };
  /** 仅在页面真正卸载时取消退出通知和事件监听，不恢复已经确认退出的界面。 */
  const dispose = (): void => {
    if (disposed) return;
    disposed = true; unsubscribe?.(); observer?.disconnect();
    for (const name of events) target.removeEventListener(name, blockInteraction, true);
    target.removeEventListener("beforeunload", dispose);
  };
  /** 注册交互捕获后订阅通知，再补查退出状态，覆盖通知早于页面加载的情况。 */
  const install = (): void => {
    if (installed || disposed) return;
    installed = true;
    for (const name of events) target.addEventListener(name, blockInteraction, { capture: true, passive: false });
    // 1. 开发模式热更新可能仍使用旧 preload；缺少新接口时保持页面可加载，重启后接入门禁。
    if (typeof api?.onApplicationShutdown === "function") unsubscribe = api.onApplicationShutdown(lock);
    target.addEventListener("beforeunload", dispose, { once: true });
    // 2. 查询返回 false 不能解除已收到的退出通知；卸载后的迟到结果由 lock 忽略。
    if (typeof api?.getApplicationShutdownState !== "function") return;
    void api.getApplicationShutdownState().then(/**
     * 首次状态读取完成后只补充退出锁定，不把普通启动状态写回已锁定界面。
     * Args:
     *     shuttingDown (boolean): 主进程是否已开始退出。
     */
    (shuttingDown) => { if (shuttingDown) lock(); }).catch(/** 状态查询失败时保留通知监听，不产生未处理拒绝。 */
    () => {});
  };
  return { install, lock, dispose };
};

let current: ReturnType<typeof createApplicationShutdownUi> | null = null;

/** 页面入口在其他功能监听前安装共用退出保护，重复安装复用同一个控制器。 */
export const setupApplicationShutdownUi = (): void => {
  current ??= createApplicationShutdownUi(window, window.desktopPetApi);
  current.install();
};

/** 主进程受理退出后补充锁定本窗口，与收到退出通知时使用同一入口。 */
export const lockApplicationShutdownUi = (): void => {
  setupApplicationShutdownUi(); current!.lock();
};
