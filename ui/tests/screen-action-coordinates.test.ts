import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveScreenActionPoint } from "../electron/screen-action-coordinates.js";
import type { ControlScreenCoordinates } from "../shared-types.js";

const coordinates: ControlScreenCoordinates = {
  bbox: [450, 480, 580, 550],
  x_ratio: 0.5,
  y_ratio: 0.5,
};

test("屏幕操作使用 scaleFactor 将逻辑坐标转换成物理坐标", () => {
  const point = resolveScreenActionPoint(coordinates, {
    x: 0,
    y: 0,
    width: 1536,
    height: 864,
  }, 1.25);

  assert.deepEqual(point, { x: 960, y: 540 });
});

test("显示器逻辑坐标原点和尺寸会一起应用 scaleFactor", () => {
  const point = resolveScreenActionPoint(
    coordinates,
    {
      x: 100,
      y: 50,
      width: 1536,
      height: 864,
    },
    1.25,
  );

  assert.deepEqual(point, { x: 1085, y: 603 });
});

test("无效的比例坐标或 scaleFactor 会被拒绝", () => {
  assert.throws(
    () => resolveScreenActionPoint({ ...coordinates, x_ratio: Number.NaN }, { x: 0, y: 0, width: 1, height: 1 }, 1),
    /Invalid screen coordinates/,
  );
  assert.throws(
    () => resolveScreenActionPoint(coordinates, { x: 0, y: 0, width: 1, height: 1 }, 0),
    /Invalid screen coordinates/,
  );
  assert.throws(
    () => resolveScreenActionPoint({ ...coordinates, y_ratio: 1.1 }, { x: 0, y: 0, width: 1, height: 1 }, 1),
    /Invalid screen coordinates/,
  );
});
