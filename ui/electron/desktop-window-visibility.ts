/** 截图隐藏窗口时只保存可见状态，不改变原本最小化或隐藏的窗口。 */
export interface DesktopWindow {
  /** 返回窗口是否已释放。 */
  isDestroyed(): boolean;
  /** 返回窗口是否处于可见状态。 */
  isVisible(): boolean;
  /** 返回窗口是否最小化。 */
  isMinimized(): boolean;
  /** 临时隐藏窗口，页面与订阅仍继续运行。 */
  hide(): void;
  /** 恢复可见窗口但不抢焦点。 */
  showInactive(): void;
}

/**
 * 截图前隐藏可见桌面窗口，返回成功或异常结束后都必须调用的恢复入口。
 * Args:
 *     windows ((DesktopWindow | null)[]): 当前 Live2D 和聊天窗口。
 * Returns:
 *     { hidden: boolean; restore: () => void }: 是否需要等待隐藏完成及恢复操作。
 */
export const hideDesktopWindows = (windows: Array<DesktopWindow | null>) => {
  const hidden: DesktopWindow[] = [];
  // 1. 最小化窗口不参与隐藏，避免截图结束后把它意外弹出。
  for (const window of new Set(windows)) {
    if (window && !window.isDestroyed() && window.isVisible() && !window.isMinimized()) {
      window.hide(); hidden.push(window);
    }
  }
  return {
    hidden: hidden.length > 0,
    /** 仅恢复本次隐藏且仍存活的窗口，不抢走屏幕操作目标的焦点。 */
    restore(): void {
      for (const window of hidden) {
        if (!window.isDestroyed() && !window.isMinimized()) window.showInactive();
      }
      hidden.length = 0;
    },
  };
};
