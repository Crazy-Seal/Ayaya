/** 聊天窗口位置与提醒的纯逻辑，供 Electron 与隔离测试共用。 */
export type WindowBounds = { x: number; y: number; width: number; height: number };

/**
 * 保留上次保存的尺寸，忽略 Windows 缩放换算产生的一像素误差。
 * Args:
 *     observed (WindowBounds): 系统报告的可见页面区域。
 *     saved (WindowBounds): 上次保存或本次明确恢复的位置尺寸。
 * Returns:
 *     WindowBounds: 有实际移动或缩放时使用新值，仅有取整误差时保留原值。
 */
export const stabilizeChatBounds = (observed: WindowBounds, saved: WindowBounds): WindowBounds => {
  const unchanged = Math.abs(observed.x - saved.x) <= 1 && Math.abs(observed.y - saved.y) <= 1
    && Math.abs(observed.width - saved.width) <= 1 && Math.abs(observed.height - saved.height) <= 1;
  return unchanged ? saved : observed;
};

/**
 * 把保存的位置尺寸限制在当前显示器工作区内。
 * Args:
 *     saved (Partial<WindowBounds> | undefined): 上次保存的普通窗口位置尺寸。
 *     workArea (WindowBounds): Electron 当前显示器的可用工作区。
 * Returns:
 *     WindowBounds: 居中默认值或修正后的完整窗口尺寸位置。
 */
export const fitChatBounds = (saved: Partial<WindowBounds> | undefined, workArea: WindowBounds): WindowBounds => {
  /**
   * 接受有限的数字，旧配置缺失或损坏时使用默认值。
   * Args:
   *     value (number | undefined): 保存的坐标或尺寸。
   *     fallback (number): 默认值。
   * Returns:
   *     number: 已取整的有限数字。
   */
  const finite = (value: number | undefined, fallback: number): number =>
    typeof value === "number" && Number.isFinite(value) ? Math.round(value) : fallback;
  const width = Math.min(workArea.width, Math.max(420, finite(saved?.width, 560)));
  const height = Math.min(workArea.height, Math.max(480, finite(saved?.height, 720)));
  return {
    width, height,
    x: Math.max(workArea.x, Math.min(workArea.x + workArea.width - width,
      finite(saved?.x, workArea.x + (workArea.width - width) / 2))),
    y: Math.max(workArea.y, Math.min(workArea.y + workArea.height - height,
      finite(saved?.y, workArea.y + (workArea.height - height) / 2))),
  };
};
