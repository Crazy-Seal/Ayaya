import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

/**
 * 提取页面元素的初始属性，检查可访问名称和键盘焦点。
 *
 * Args:
 *     html (string): 页面 HTML 内容。
 *     id (string): 待检查元素的 DOM 编号。
 *
 * Returns:
 *     string: 包含元素初始属性的起始标签。
 */
const elementById = (html: string, id: string): string => {
  const element = html.match(new RegExp(`<[^>]+id="${id}"[^>]*>`))?.[0];
  assert.ok(element, `缺少元素 #${id}`);
  return element;
};

test("设置页日志入口提供可访问名称", /**
 * 验证日志入口可通过其可访问名称识别。
 */
() => {
  const html = readFileSync("settings.html", "utf8");
  assert.match(elementById(html, "open-log-window-btn"), /aria-label="打开日志控制台"/);
});

test("日志窗口按钮提供可访问名称并列出日志级别", /**
 * 验证窗口操作按钮的可访问名称及页面公开的日志级别选项。
 */
() => {
  const html = readFileSync("logs.html", "utf8");

  assert.match(elementById(html, "log-min-btn"), /aria-label="最小化"/);
  assert.match(elementById(html, "log-close-btn"), /aria-label="关闭"/);
  assert.match(html, /<option value="debug">DEBUG<\/option>/);
  assert.match(html, /<option value="info">INFO<\/option>/);
  assert.match(html, /<option value="warn">WARNING<\/option>/);
  assert.match(html, /<option value="error">ERROR<\/option>/);
});

test("前后端日志列表允许键盘聚焦", /**
 * 验证两个日志列表在页面初始化时都可以进入键盘焦点顺序。
 */
() => {
  const html = readFileSync("logs.html", "utf8");

  assert.match(elementById(html, "frontend-logs"), /tabindex="0"/);
  assert.match(elementById(html, "backend-logs"), /tabindex="0"/);
});
