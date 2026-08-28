import assert from "node:assert/strict";
import { test } from "node:test";

import { selectPrimaryDisplaySource } from "../electron/screen-capture-source.js";

test("按主显示器 display_id 选择截屏来源，而不是固定使用第一个来源", () => {
  const sources = [
    { display_id: "2", name: "secondary" },
    { display_id: "1", name: "primary" },
  ];

  assert.equal(selectPrimaryDisplaySource(sources, 1).name, "primary");
});

test("找不到主显示器来源时明确报错", () => {
  assert.throws(
    () => selectPrimaryDisplaySource([{ display_id: "2" }], 1),
    /display_id=1/,
  );
});
