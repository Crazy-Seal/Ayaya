import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { MotionPage } from "../src/settings/pages/motion-page.js";
import type { ChatSettingsState } from "../src/settings/types.js";

/**
 * 读取页面文件，检查初始属性及运行时使用的配置。
 *
 * Args:
 *     relativePath (string): 相对于 UI 根目录的文件路径。
 *
 * Returns:
 *     string: 待检查的文件内容。
 */
const readUiFile = (relativePath: string): string =>
  readFileSync(relativePath, "utf8");

/**
 * 提取指定元素的初始属性，检查隐藏状态和可访问名称。
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

test("MCP 权限保存按钮在页面初始化时隐藏", /**
 * 验证服务器配置加载前不会显示权限保存按钮。
 */
() => {
  const html = readUiFile("settings.html");
  assert.match(elementById(html, "mcp-policy-save-btn"), /\shidden(?:\s|>)/);
});

test("动态表情设置下拉框提供包含动作名称的可访问名称", /**
 * 渲染两个不同动作，检查实际加入表格的下拉框能通过动作名称区分。
 */
() => {
  /**
   * 保存动作页面渲染的控件及父子关系，不模拟浏览器布局。
   *
   * Attributes:
   *     tagName (string): 创建元素时指定的标签名。
   *     children (Element[]): 页面实际追加的子元素。
   *     dataset (Record<string, string>): 页面写入的动作编号等数据。
   *     ariaLabel (string): 页面设置的可访问名称。
   */
  class Element extends EventTarget {
    children: Element[] = [];
    dataset: Record<string, string> = {};
    ariaLabel = "";

    /**
     * 创建供动作页面追加内容和登记事件的测试元素。
     *
     * Args:
     *     tagName (string): 当前元素的标签名。
     */
    constructor(public readonly tagName: string) {
      super();
    }

    /**
     * 页面渲染调用时保存子元素，供断言检查真正加入表格的控件。
     *
     * Args:
     *     child (Element): 页面准备追加的元素。
     *
     * Returns:
     *     Element: 已追加的子元素。
     */
    appendChild(child: Element): Element {
      this.children.push(child);
      return child;
    }
  }

  const originalDocument = globalThis.document;
  const body = new Element("tbody");
  const motions = ["挥手", "眨眼"];
  // 1. 仅替换元素创建入口，页面仍执行真实渲染并追加各行动作控件。
  globalThis.document = {
    /**
     * 动作页面创建表格和表单控件时返回可记录父子关系的测试元素。
     *
     * Args:
     *     tagName (string): 页面请求创建的 HTML 标签名。
     *
     * Returns:
     *     Element: 供页面填写属性并追加的元素。
     */
    createElement: (tagName: string) => new Element(tagName),
  } as unknown as Document;
  try {
    const page = new MotionPage(
      body as unknown as HTMLTableSectionElement,
      new Element("div") as unknown as HTMLDivElement,
      new Element("button") as unknown as HTMLButtonElement,
      new Element("textarea") as unknown as HTMLTextAreaElement,
    );
    page.render({
      saved: { name: "测试助手" } as ChatSettingsState,
      dependencies: { availableMotions: motions },
    });
    // 2. 检查已追加的两行动作控件，不要求属性通过某种源码语句赋值。
    const selects: Element[] = [];
    for (const row of body.children) {
      for (const cell of row.children) {
        for (const control of cell.children) {
          if (control.tagName === "select") selects.push(control);
        }
      }
    }
    assert.equal(selects.length, motions.length);
    assert.ok(selects[0].ariaLabel.includes(motions[0]));
    assert.ok(selects[1].ariaLabel.includes(motions[1]));
    assert.notEqual(selects[0].ariaLabel, selects[1].ariaLabel);
  } finally {
    // 3. 无论渲染或断言是否失败，都恢复其他用例使用的页面环境。
    globalThis.document = originalDocument;
  }
});

test("工具审批引导输入框提供可访问名称", /**
 * 验证审批输入框在页面初始化时声明可访问名称。
 */
() => {
  const html = readUiFile("chat.html");

  assert.match(
    elementById(html, "tool-steer-input"),
    /aria-label="引导 Agent 下一步操作"/,
  );
});

test("Vite 生产构建使用相对资源路径供 Electron loadFile 加载", /**
 * 验证构建配置声明供本地文件加载使用的相对资源路径。
 */
() => {
  const viteConfig = readUiFile("vite.config.ts");

  assert.match(viteConfig, /base:\s*["']\.\/["']/);
});
