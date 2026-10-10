import type { ControlScreenCoordinates, ScreenActionPoint } from "../shared-types.js";

export type ScreenBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/**
 * 排除 NaN 和无穷值，避免无效坐标进入鼠标操作。
 *
 * Args:
 *     value (unknown): 当前待检查或转换的值。
 *
 * Returns:
 *     value is number: 值为有限数字时为 true。
 */
const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/**
 * 把模型归一化坐标转换为主显示器的实际桌面位置，并检查坐标范围。
 *
 * Args:
 *     coordinates (ControlScreenCoordinates): 模型或定位工具给出的归一化坐标。
 *     bounds (ScreenBounds): 主显示器在桌面坐标系中的边界。
 *     scaleFactor (number): 显示器的缩放比例。
 *
 * Returns:
 *     ScreenActionPoint: 桌面动作使用的 x、y 坐标。
 */
export const resolveScreenActionPoint = (
  coordinates: ControlScreenCoordinates,
  bounds: ScreenBounds,
  scaleFactor: number,
): ScreenActionPoint => {
  if (
    !isFiniteNumber(coordinates.x_ratio)
    || !isFiniteNumber(coordinates.y_ratio)
    || coordinates.x_ratio < 0
    || coordinates.x_ratio > 1
    || coordinates.y_ratio < 0
    || coordinates.y_ratio > 1
    || !isFiniteNumber(scaleFactor)
    || scaleFactor <= 0
  ) {
    throw new Error("Invalid screen coordinates or scale factor");
  }

  return {
    x: Math.round((bounds.x + coordinates.x_ratio * bounds.width) * scaleFactor),
    y: Math.round((bounds.y + coordinates.y_ratio * bounds.height) * scaleFactor),
  };
};
