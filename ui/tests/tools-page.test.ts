import assert from "node:assert/strict";
import { test } from "node:test";

import { ToolsPage } from "../src/settings/pages/tools-page.js";
import { MCPServerStatus, MCPTransport, ToolPolicy, ToolSource } from "../shared-types.js";
import type { MCPServerView, ToolPolicyView } from "../shared-types.js";
import type { PageRenderData } from "../src/settings/types.js";

/** 保存真实页面生成的元素、属性和点击事件，检查分组显隐及权限收集。 */
class Element extends EventTarget {
  children: Element[] = [];
  dataset: Record<string, string> = {};
  className = "";
  textContent = "";
  hidden = false;
  disabled = false;
  value = "";
  type = "";
  private attributes = new Map<string, string>();

  /**
   * 创建指定标签的测试元素。
   * Args:
   *     tagName (string): 页面创建的标签名称。
   */
  constructor(readonly tagName: string) { super(); }

  /**
   * 模拟页面重绘时清除旧子元素。
   * Args:
   *     _value (string): 页面写入的静态 HTML。
   */
  set innerHTML(_value: string) { this.children = []; }

  /**
   * 记录页面追加的分组、工具行及控件。
   * Args:
   *     elements (Element[]): 当前元素接收的子元素。
   */
  append(...elements: Element[]): void { this.children.push(...elements); }

  /**
   * 记录单个子元素并返回它。
   * Args:
   *     element (Element): 当前追加的子元素。
   * Returns:
   *     Element: 已加入当前元素的子元素。
   */
  appendChild(element: Element): Element { this.append(element); return element; }

  /**
   * 保存按钮的无障碍状态和名称。
   * Args:
   *     name (string): 属性名称。
   *     value (string): 属性值。
   */
  setAttribute(name: string, value: string): void { this.attributes.set(name, value); }

  /**
   * 读取按钮展开状态，供真实点击回调使用。
   * Args:
   *     name (string): 属性名称。
   * Returns:
   *     string | null: 已设置的属性值或空值。
   */
  getAttribute(name: string): string | null { return this.attributes.get(name) ?? null; }

  /**
   * 遍历控件和表格行，包括隐藏分组中的权限下拉框。
   * Args:
   *     selector (string): 标签选择器或权限控件选择器。
   * Returns:
   *     Element[]: 所有匹配的后代元素。
   */
  querySelectorAll(selector: string): Element[] {
    return this.children.flatMap(/** 递归收集当前子树中的匹配元素。 */ (child) => {
      const matches = selector === 'select[data-stable-key]'
        ? child.tagName === "select" && child.dataset.stableKey !== undefined
        : child.tagName === selector;
      return [...(matches ? [child] : []), ...child.querySelectorAll(selector)];
    });
  }
}

/**
 * 创建启用但连接失败的服务，区分服务开关与工具可用性。
 * Args:
 *     id (string): 稳定服务编号。
 *     enabled (boolean): 全局开关。
 * Returns:
 *     MCPServerView: 两个服务可以重名的服务目录项。
 */
const server = (id: string, enabled: boolean): MCPServerView => ({
  config: {
    id, name: "同名服务", enabled, transport: MCPTransport.STDIO,
    command: "test", args: [], env: {}, connect_timeout_seconds: 10, call_timeout_seconds: 10,
  },
  affected_model_count: 1,
  runtime: { status: MCPServerStatus.ERROR, tool_count: 0, last_error: "测试连接失败", instructions: null },
});

/**
 * 创建未连接服务中保留的工具权限。
 * Args:
 *     serverId (string): 工具所属服务编号。
 *     name (string): 服务中的工具名。
 * Returns:
 *     ToolPolicyView: 不可用但仍有可保存权限的工具目录项。
 */
const tool = (serverId: string, name: string): ToolPolicyView => ({
  stable_key: `mcp:${serverId}:${name}`, source: ToolSource.MCP, name, display_name: name,
  description: "测试工具", policy: ToolPolicy.ASK, server_id: serverId, server_name: "同名服务",
  available: false,
});

/**
 * 提供包含全局禁用、会话禁用及已启用服务的实际页面输入。
 * Returns:
 *     PageRenderData: 服务工具故意交错排列，以验证按编号归组。
 */
const renderData = (): PageRenderData => ({
  saved: {
    session_id: "tools", model_name: "test", openai_api_key: "test",
    openai_base_url: "http://127.0.0.1:1/v1", temperature: 0.1, system_prompt: "测试",
    tool_permissions: {}, tool_permissions_schema_version: 1,
    mcp: { servers: { enabled: { enabled: true }, globalOff: { enabled: true }, sessionOff: { enabled: false } } },
    context_strategy: { compression_trigger_tokens_k: 100, max_screenshots_in_context: 2, screenshot_ttl_human_messages: 2 },
  },
  dependencies: {
    mcpServers: [server("enabled", true), server("globalOff", false), server("sessionOff", true)],
    toolPolicies: [
      { stable_key: "local:test", source: ToolSource.LOCAL, name: "test", display_name: "本地工具",
        description: "本地说明", policy: ToolPolicy.ALLOW, available: true },
      tool("enabled", "first"), tool("globalOff", "off"), tool("enabled", "second"), tool("sessionOff", "off"),
    ],
  },
});

test("MCP 按启用开关默认折叠，重名服务独立切换且折叠不丢权限", /**
 * 通过真实页面渲染和点击回调检查默认状态、分组身份及保存结果。
 */
(t) => {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    /** 创建页面实际需要的标签元素。 */
    createElement: (name: string) => new Element(name),
  } });
  t.after(/** 恢复其他用例使用的 document。 */ () => {
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else Reflect.deleteProperty(globalThis, "document");
  });
  const body = new Element("tbody");
  const page = new ToolsPage(body as unknown as HTMLTableSectionElement,
    new Element("div") as unknown as HTMLDivElement, new Element("button") as unknown as HTMLButtonElement);
  const data = renderData();
  page.render(data);
  const buttons = body.querySelectorAll("button");
  assert.equal(buttons.length, 3);
  assert.deepEqual(buttons.map(/** 读取各组初始展开状态。 */ (button) => button.getAttribute("aria-expanded")),
    ["true", "false", "false"]);
  // 1. 已启用但失联的服务默认展开；全局或当前会话禁用的服务默认折叠。
  assert.deepEqual(body.children.map(/** 读取按编号归组后的各行可见性。 */ (row) => row.hidden),
    [false, false, false, false, false, false, true, false, true]);
  const initialPolicies = page.getEditingData();
  assert.equal(Object.keys(initialPolicies.tools!.tool_permissions).length, 5);
  // 2. 同名服务独立切换，折叠保留权限，展开也保持工具原有的不可用状态。
  buttons[0].dispatchEvent(new Event("click"));
  buttons[1].dispatchEvent(new Event("click"));
  assert.deepEqual(buttons.map(/** 读取用户点击后的各组展开状态。 */ (button) => button.getAttribute("aria-expanded")),
    ["false", "true", "false"]);
  assert.equal(body.children[3].hidden, true);
  assert.equal(body.children[4].hidden, true);
  assert.equal(body.children[6].hidden, false);
  assert.equal(body.children[6].querySelectorAll("select")[0].disabled, true);
  assert.deepEqual(page.getEditingData(), initialPolicies);
  // 3. 同一会话重绘保留手动选择；切换会话后重新采用默认值。
  page.render(data);
  assert.deepEqual(body.querySelectorAll("button").map(/** 读取重绘后的展开状态。 */
    (button) => button.getAttribute("aria-expanded")), ["false", "true", "false"]);
  page.render({ ...data, saved: { ...data.saved, session_id: "other" } });
  assert.deepEqual(body.querySelectorAll("button").map(/** 读取新会话的默认状态。 */
    (button) => button.getAttribute("aria-expanded")), ["true", "false", "false"]);
});
