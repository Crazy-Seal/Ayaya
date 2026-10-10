import assert from "node:assert/strict";
import { test } from "node:test";

import { selectPrimaryDisplaySource } from "../electron/screen-capture-source.js";

test("按主显示器 display_id 选择截屏来源，而不是固定使用第一个来源", /**
 * 验证按主显示器 display_id 选择截屏来源，而不是固定使用第一个来源。
 */
() => {
  const sources = [
    { display_id: "2", name: "secondary" },
    { display_id: "1", name: "primary" },
  ];

  assert.equal(selectPrimaryDisplaySource(sources, 1).name, "primary");
});

test("找不到主显示器来源时明确报错", /**
 * 验证找不到主显示器来源时明确报错。
 */
() => {
  assert.throws(
    /**
     * 由外层操作在对应处理阶段调用，取得本用例指定的条目或响应，供外层断言处理结果。
     *
     * 此定义用于：验证找不到主显示器来源时明确报错。
     */
    () => selectPrimaryDisplaySource([{ display_id: "2" }], 1),
    /display_id=1/,
  );
});
