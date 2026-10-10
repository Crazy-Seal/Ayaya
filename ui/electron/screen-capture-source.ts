export type ScreenCaptureSource = {
  display_id: string;
};

/**
 * 按主显示器编号选择截图来源，不随来源枚举顺序猜测目标显示器。
 *
 * Args:
 *     sources (readonly T[]): Electron 捕获到的屏幕来源列表。
 *     primaryDisplayId (number): 主显示器编号，用于选择对应的截图来源。
 *
 * Returns:
 *     T: 对应主显示器的捕获来源。
 */
export const selectPrimaryDisplaySource = <T extends ScreenCaptureSource>(
  sources: readonly T[],
  primaryDisplayId: number,
): T => {
  const displayId = String(primaryDisplayId);
  const source = sources.find(/**
   * 按 Electron 显示器编号匹配主显示器捕获来源，避免依赖来源排列顺序。
   *
   * Args:
   *     candidate (T): 当前待筛选的屏幕来源、记录或配置项。
   *
   * Returns:
   *     boolean: 当前来源对应主显示器时为 true。
   */
  (candidate) => candidate.display_id === displayId);
  if (!source) {
    throw new Error(`未找到主显示器对应的截屏来源（display_id=${displayId}）`);
  }
  return source;
};
