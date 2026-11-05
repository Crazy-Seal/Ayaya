import assert from "node:assert/strict";
import { test } from "node:test";
import { SubagentPanel } from "../src/settings/pages/subagent-panel.js";
import { SubagentStatus, type ChannelCommand, type SubagentDetail, type SubagentSummary } from "../shared-types.js";

/** 为页面提供可观察的文本及按钮事件，不模拟后台状态机。 */
class Element {
  children: Element[] = [];
  private text = "";
  className = "";
  type = "";
  hidden = false;
  disabled = false;
  readonly handlers = new Map<string, () => void>();
  ownerDocument = { createElement: /** 创建可观察的展示节点。 */ (tag: string): Element => new Element(tag) };
  /** 保存元素种类，供模拟用户点击按钮。 */
  constructor(readonly tag = "div") {}
  /** 返回包含子节点的用户可见文本。 */
  get textContent(): string { return this.text + this.children.map(/** 读取子节点文本。 */ (item) => item.textContent).join(""); }
  /** 设置正文时替换旧子节点，与浏览器一致。 */
  set textContent(text: string) { this.text = text; this.children = []; }
  /** 添加渲染生成的子节点。 */
  append(...nodes: Element[]): void { this.children.push(...nodes); }
  /** 替换旧页面内容。 */
  replaceChildren(...nodes: Element[]): void { this.text = ""; this.children = nodes; }
  /** 保存用户可触发的事件。 */
  addEventListener(name: string, listener: () => void): void { this.handlers.set(name, listener); }
  /** 更新辅助显示属性，测试不依赖这些局部样式。 */
  setAttribute(): void {}
}

test("独立子面板显示空闲保留型和运行一次性，停止只发送目标子命令", /** 验证实际渲染与按钮的命令参数。 */
async () => {
  const list = new Element();
  const refresh = new Element("button");
  const commands: ChannelCommand[] = [];
  const records: SubagentSummary[] = [
    { subagent_id: "a", name: "代码探索助手", if_retain_context: true, status: SubagentStatus.IDLE, created_at: "2026-10-31", started_at: null },
    { subagent_id: "b", name: "测试助手", if_retain_context: false, status: SubagentStatus.RUNNING, created_at: "2026-10-31", started_at: "2026-10-31" },
  ];
  const panel = new SubagentPanel(list as unknown as HTMLElement, refresh as unknown as HTMLButtonElement, undefined, {
    getSubagents: /** 返回本例实际可见的子身份。 */ async () => records,
    getSubagentDetails: /** 本例不打开详情。 */ async () => { throw new Error("不应读取详情"); },
    submitInteractionCommand: /** 记录停止请求，返回已受理。 */ async (command) => { commands.push(command); return { accepted: true }; },
  });
  await panel.render("parent");
  assert.match(list.textContent, /代码探索助手/);
  assert.match(list.textContent, /空闲/);
  assert.match(list.textContent, /一次性/);
  const buttons: Element[] = [];
  /** 收集真实渲染结果中的按钮，模拟用户操作。 */
  function visit(node: Element): void { if (node.tag === "button") buttons.push(node); node.children.forEach(visit); }
  visit(list);
  buttons.find(/** 找到执行中子任务的停止操作。 */ (item) => item.textContent === "停止")?.handlers.get("click")?.();
  assert.equal(commands.length, 1);
  assert.equal(commands[0].subagent_id, "b");
  assert.equal(commands[0].session_id, "parent");
  assert.equal(commands[0].kind, "interact_subagent");
  assert.deepEqual(commands[0].payload, { action: "stop" });
  panel.dispose();
});

/**
 * 从当前卡片取得详情按钮，轮询后重新查找，模拟用户操作实际页面。
 *
 * Args:
 *     list (Element): 已渲染子 Agent 卡片的列表。
 *
 * Returns:
 *     Element: 第一张卡片的详情按钮。
 */
function detailsButton(list: Element): Element {
  return list.children[0].children[0].children[1];
}

/**
 * 等待详情请求及其触发的列表刷新完成。
 *
 * Returns:
 *     Promise<void>: 当前异步请求的回调执行完成后结束。
 */
async function settleDetails(): Promise<void> {
  await new Promise<void>(/** 等待当前请求的 Promise 回调执行完毕。 */
    (resolve) => { setImmediate(resolve); });
}

for (const pollBeforeFailure of [false, true]) {
  test(`详情失败在轮询后保留错误且重新展开可重试（查询中轮询=${pollBeforeFailure}）`, /** 验证失败状态及重试结果经过真实列表重建后仍可见。 */
  async () => {
    const list = new Element();
    let rejectDetails!: (reason: Error) => void;
    const failedDetails = new Promise<SubagentDetail>(/** 由测试控制错误到达时机。 */
      (_resolve, reject) => { rejectDetails = reject; });
    let attempts = 0;
    const detail: SubagentDetail = {
      subagent_id: "a", name: "测试助手", system_prompt: "检查任务", tool_whitelist: [],
      if_retain_context: true, context_available: true, status: SubagentStatus.IDLE,
      created_at: "2026-10-31", recent_interactions: [], results: [],
    };
    const panel = new SubagentPanel(list as unknown as HTMLElement, new Element("button") as unknown as HTMLButtonElement,
      undefined, {
        getSubagents: /** 返回当前可复用子身份。 */ async () => [
          { subagent_id: "a", name: "测试助手", if_retain_context: true, status: SubagentStatus.IDLE,
            started_at: null, created_at: "2026-10-31" },
        ],
        getSubagentDetails: /** 首次查询失败，用户重新展开后返回成功详情。 */ async () => {
          attempts += 1;
          return attempts === 1 ? failedDetails : detail;
        },
        submitInteractionCommand: /** 本例只读取详情。 */ async () => ({ accepted: true }),
      });
    try {
      await panel.render("parent");
      detailsButton(list).handlers.get("click")?.();
      if (pollBeforeFailure) await panel.render("parent");
      rejectDetails(new Error("网络暂时不可用"));
      await settleDetails();
      assert.match(list.textContent, /读取详情失败/);
      await panel.render("parent");
      assert.match(list.textContent, /网络暂时不可用/);
      assert.doesNotMatch(list.textContent, /正在读取详情/);
      // 1. 用户收起后重新展开，发起新的详情请求并替换失败提示。
      detailsButton(list).handlers.get("click")?.();
      detailsButton(list).handlers.get("click")?.();
      await settleDetails();
      assert.equal(attempts, 2);
      assert.match(list.textContent, /检查任务/);
      assert.doesNotMatch(list.textContent, /读取详情失败/);
    } finally {
      panel.dispose();
    }
  });
}
