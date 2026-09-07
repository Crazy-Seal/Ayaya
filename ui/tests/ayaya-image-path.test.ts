import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildImageBackendPath,
  normalizeImageRelativePath,
} from "../electron/ayaya-image-path.js";

test("历史图片路径允许旧根文件和三个受限子目录", () => {
  assert.equal(normalizeImageRelativePath("legacy.png"), "legacy.png");
  assert.equal(normalizeImageRelativePath("tool/result.png"), "tool/result.png");
  assert.equal(normalizeImageRelativePath("screenshot/shot.png"), "screenshot/shot.png");
  assert.equal(normalizeImageRelativePath("mcp/chart.webp"), "mcp/chart.webp");
  assert.equal(buildImageBackendPath("screenshot/shot 1.png"), "/images/screenshot/shot%201.png");
});

test("历史图片路径拒绝越权目录和路径穿越", () => {
  for (const value of [
    "",
    "../secret.png",
    "screenshot/../secret.png",
    "unknown/image.png",
    "tool/nested/image.png",
    "C:\\secret.png",
    "/absolute.png",
  ]) {
    assert.equal(normalizeImageRelativePath(value), null);
  }
});
