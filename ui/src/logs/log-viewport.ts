/**
 * 区分用户滚动与程序滚动，用户主动离开底部才关闭自动跟随。
 */
export class LogViewportState {
  private userScrolling = false;

  /**
   * 保存初始跟随选项及离开底部的距离阈值。
   *
   * Args:
   *     following (boolean): 是否保持日志列表跟随底部。
   *     leaveBottomThreshold (number): 用户滚动离开底部多少像素后取消跟随。
   */
  constructor(
    private following: boolean,
    private readonly leaveBottomThreshold = 24,
  ) {}

  /**
   * 读取列表当前是否需要跟随最新日志。
   *
   * Returns:
   *     boolean: 自动跟随启用时为 true。
   */
  isFollowing(): boolean {
    return this.following;
  }

  /**
   * 切换自动跟随并清除旧用户滚动标记。
   *
   * Args:
   *     following (boolean): 是否保持日志列表跟随底部。
   */
  setFollowing(following: boolean): void {
    this.following = following;
    this.userScrolling = false;
  }

  /**
   * 用户开始滚轮、按键或拖动时标记真实滚动意图。
   */
  beginUserScroll(): void {
    this.userScrolling = true;
  }

  /**
   * 用户操作结束或短暂意图等待到期时清除滚动标记。
   */
  endUserScroll(): void {
    this.userScrolling = false;
  }

  /**
   * 只在用户主动离开底部超过阈值时关闭跟随，布局引起的滚动保持原选项。
   *
   * Args:
   *     distanceFromBottom (number): 当前滚动位置距列表底部的像素数。
   *
   * Returns:
   *     boolean: 本次确实从跟随切换为自由阅读时为 true。
   */
  handleScroll(distanceFromBottom: number): boolean {
    if (
      !this.userScrolling
      || !this.following
      || distanceFromBottom <= this.leaveBottomThreshold
    ) {
      return false;
    }
    this.following = false;
    this.userScrolling = false;
    return true;
  }
}

/**
 * 原可见日志已被淘汰时选择不早于原编号的邻近记录，末尾无匹配时使用最后记录。
 *
 * Args:
 *     orderedIds (readonly number[]): 当前可见日志按展示顺序排列的编号。
 *     previousId (number): 更新列表前保存的首个可见日志编号。
 *
 * Returns:
 *     number | undefined: 可用于恢复阅读位置的编号，列表为空时为 undefined。
 */
export const selectRestorationAnchorId = (
  orderedIds: readonly number[],
  previousId: number,
): number | undefined => {
  if (orderedIds.length === 0) return undefined;
  const nextId = orderedIds.find(/**
   * 寻找编号不早于原可见记录的第一条日志，供原记录淘汰后恢复阅读位置。
   *
   * Args:
   *     id (number): 当前模型参数或日志记录的编号。
   *
   * Returns:
   *     boolean: 当前编号不小于原编号时为 true。
   */
  (id) => id >= previousId);
  return nextId ?? orderedIds.at(-1);
};

/**
 * 按参考记录位置的变化修正滚动值，避免重建列表后阅读位置跳动。
 *
 * Args:
 *     currentScrollTop (number): 列表更新前的滚动位置。
 *     currentAnchorOffset (number): 列表更新后参考记录相对容器顶部的位置。
 *     previousAnchorOffset (number): 列表更新前同一参考记录的位置。
 *
 * Returns:
 *     number: 不小于零的新滚动位置。
 */
export const resolveAnchoredScrollTop = (
  currentScrollTop: number,
  currentAnchorOffset: number,
  previousAnchorOffset: number,
): number => Math.max(
  0,
  currentScrollTop + currentAnchorOffset - previousAnchorOffset,
);
