import type { ControlScreenCoordinates } from "../shared-types.js";

export type ScreenBounds = {
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ScreenPoint = {
  x: number;
  y: number;
};

const isFiniteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

export const resolveScreenActionPoint = (
  coordinates: ControlScreenCoordinates,
  bounds: ScreenBounds,
  scaleFactor: number,
): ScreenPoint => {
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
