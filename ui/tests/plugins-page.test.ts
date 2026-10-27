import assert from "node:assert/strict";
import { test } from "node:test";

import { PluginsPage } from "../src/settings/pages/plugins-page.js";
import type { PluginItem } from "../shared-types.js";

const memoryPlugin: PluginItem = {
  name: "memory",
  description: "记忆",
  default_config: {
    long_term_memory_every_human_messages: 10,
  },
  config_schema: {
    properties: {
      long_term_memory_every_human_messages: { type: "integer", minimum: 1 },
    },
  },
};

/** 为插件页面提供元素树，观察实际渲染的开关和保存结果。 */
class FakeElement {
  children: FakeElement[] = [];
  dataset: Record<string, string> = {};
  checked = false;
  disabled = false;
  hidden = false;
  value = "on";
  type = "";
  innerHTML = "";

  /**
   * 接收页面创建的子元素。
   *
   * Args:
   *     elements (FakeElement[]): 追加到当前元素的子元素。
   */
  append(...elements: FakeElement[]): void {
    this.children.push(...elements);
  }

  /**
   * 接收一张插件卡片或一个配置控件。
   *
   * Args:
   *     element (FakeElement): 页面追加的子元素。
   */
  appendChild(element: FakeElement): void {
    this.children.push(element);
  }

  /**
   * 接收保存按钮的事件注册；本用例直接读取页面编辑结果。
   *
   * Args:
   *     _name (string): 页面登记的事件名称。
   *     _listener (() => void): 点击保存时的回调。
   */
  addEventListener(_name: string, _listener: () => void): void {}

  /** 接收无障碍字段关联属性。 */
  setAttribute(_name: string, _value: string): void {}

  /**
   * 查找页面收集配置和用例操作开关时使用的元素。
   *
   * Args:
   *     selector (string): 插件卡片、启用开关或配置输入的选择器。
   *
   * Returns:
   *     FakeElement[]: 匹配的后代元素。
   */
  querySelectorAll(selector: string): FakeElement[] {
    return this.children.flatMap(/**
     * 先检查当前子元素，再查找它的后代元素。
     *
     * Args:
     *     child (FakeElement): 当前遍历的子元素。
     *
     * Returns:
     *     FakeElement[]: 当前子树中匹配的元素。
     */
    (child) => {
      const matches = selector === "[data-plugin-name]"
        ? child.dataset.pluginName !== undefined
        : selector === 'input[data-plugin-enabled="true"]'
          ? child.dataset.pluginEnabled === "true"
          : selector === "input[data-config-key]" && child.dataset.configKey !== undefined;
      return [...(matches ? [child] : []), ...child.querySelectorAll(selector)];
    });
  }

  /**
   * 返回插件卡片中的第一个启用开关。
   *
   * Args:
   *     selector (string): 页面或用例传入的元素选择器。
   *
   * Returns:
   *     FakeElement | null: 第一个匹配元素，缺失时为空。
   */
  querySelector(selector: string): FakeElement | null {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

test("所有插件均可开关，保存时保留未显示的配置字段", /**
 * 渲染真实插件卡片，模拟关闭记忆和开启普通插件，再检查页面提交结果。
 */
() => {
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      createElement: /** 创建插件页面使用的元素替身。 */
      () => new FakeElement(),
    },
  });
  try {
    const container = new FakeElement();
    const page = new PluginsPage(
      container as unknown as HTMLDivElement,
      new FakeElement() as unknown as HTMLDivElement,
      new FakeElement() as unknown as HTMLButtonElement,
    );
    page.render({
      saved: {
        session_id: "plugins", model_name: "test", openai_api_key: "test",
        openai_base_url: "http://127.0.0.1:1/v1", temperature: 0.1, system_prompt: "测试",
        tool_permissions: {}, tool_permissions_schema_version: 1, mcp: { servers: {} },
        context_strategy: { compression_trigger_tokens_k: 100, max_screenshots_in_context: 2, screenshot_ttl_human_messages: 2 },
        agent_plugins: {
          memory: { enabled: true, config: { long_term_memory_every_human_messages: 12 } },
          sample: { enabled: false, config: { endpoint: "用户地址" } },
        },
      },
      dependencies: { availablePlugins: [memoryPlugin, {
        name: "sample", description: "普通插件", default_config: { endpoint: "默认地址", timeout: 5 }, config_schema: {},
      }] },
    });
    const cards = container.querySelectorAll("[data-plugin-name]");
    const memoryToggle = cards[0].querySelector('input[data-plugin-enabled="true"]');
    const sampleToggle = cards[1].querySelector('input[data-plugin-enabled="true"]');
    assert.ok(memoryToggle && sampleToggle);
    assert.equal(memoryToggle.disabled, false);
    assert.equal(sampleToggle.disabled, false);
    assert.equal(memoryToggle.checked, true);
    assert.equal(sampleToggle.checked, false);
    memoryToggle.checked = false;
    sampleToggle.checked = true;
    cards[0].querySelectorAll("input[data-config-key]")[0].value = "14";
    assert.deepEqual(page.getEditingData().plugins?.agent_plugins, {
      memory: { enabled: false, config: { long_term_memory_every_human_messages: 14 }, tool_permissions: {} },
      sample: { enabled: true, config: { endpoint: "用户地址", timeout: 5 }, tool_permissions: {} },
    });
  } finally {
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument);
    else Reflect.deleteProperty(globalThis, "document");
  }
});
