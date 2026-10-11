/** 聊天与日志标题栏、八个缩放边缘共用指针捕获，窗口坐标由主进程计算。 */
import type { WindowGesture } from "../../shared-types.js";

/**
 * 安装标题栏与边缘拖动，按钮保持自己的点击交互。
 * Args:
 *     root (HTMLElement): 具有共享圆角样式的聊天或日志窗口容器。
 * Returns:
 *     (() => void): 页面卸载时取消当前操作并释放全部监听。
 */
export const setupWindowGestures = (root: HTMLElement): (() => void) => {
  const titlebar = root.querySelector<HTMLElement>(".ui-titlebar, .settings-titlebar")!;
  const removers: Array<() => void> = [];
  let captured: { element: HTMLElement; pointerId: number } | null = null;
  /** 结束当前操作并释放页面指针捕获，允许 pointerup、cancel 和 lostcapture 重复通知。 */
  const finish = (): void => {
    if (!captured) return;
    const previous = captured; captured = null;
    window.desktopPetApi.endWindowGesture();
    if (previous.element.hasPointerCapture(previous.pointerId)) previous.element.releasePointerCapture(previous.pointerId);
  };
  /**
   * 为标题栏或一个边缘安装捕获；主进程持续读取鼠标，不依赖变化中的页面坐标。
   * Args:
   *     element (HTMLElement): 标题栏或边缘区域。
   *     kind (WindowGesture): 当前区域对应的操作。
   */
  const bind = (element: HTMLElement, kind: WindowGesture): void => {
    /**
     * 用户按住左键时捕获该指针，拖出窗口后仍能收到松开；标题栏按钮不进入此操作。
     * Args:
     *     event (PointerEvent): 当前指针按下事件。
     */
    const begin = (event: PointerEvent): void => {
      if (event.button !== 0 || !event.isPrimary || (event.target as HTMLElement).closest("button, input, textarea, a")) return;
      finish(); event.preventDefault();
      // 1. 先捕获再通知主进程；捕获失败时不留下后台拖动定时器。
      element.setPointerCapture(event.pointerId);
      captured = { element, pointerId: event.pointerId };
      window.desktopPetApi.beginWindowGesture(kind);
    };
    /**
     * 只处理当前捕获区域和指针的结束事件，旧区域迟到的 lostcapture 不结束新操作。
     * Args:
     *     event (PointerEvent): 当前区域收到的松开、取消或捕获结束事件。
     */
    const release = (event: PointerEvent): void => {
      if (captured?.element === element && captured.pointerId === event.pointerId) finish();
    };
    element.addEventListener("pointerdown", begin);
    for (const name of ["pointerup", "pointercancel", "lostpointercapture"] as const) element.addEventListener(name, release);
    removers.push(/** 页面卸载时移除当前区域的所有监听。 */
    () => {
      element.removeEventListener("pointerdown", begin);
      for (const name of ["pointerup", "pointercancel", "lostpointercapture"] as const) element.removeEventListener(name, release);
    });
  };
  bind(titlebar, "move");
  // 1. 边缘区域独立于聊天或日志滚动内容，保持八个方向的命中宽度，不挤压布局。
  for (const kind of ["n", "s", "e", "w", "ne", "nw", "se", "sw"] as const) {
    const handle = document.createElement("div");
    handle.className = `ui-resize-handle ui-resize-${kind}`; handle.setAttribute("aria-hidden", "true");
    root.append(handle); bind(handle, kind);
    removers.push(/** 页面卸载时移除只供鼠标命中的缩放区域。 */ () => handle.remove());
  }
  window.addEventListener("blur", finish);
  /** 页面卸载时结束当前拖动，移除边缘区域和全部事件监听。 */
  return () => { finish(); window.removeEventListener("blur", finish); for (const remove of removers) remove(); };
};
