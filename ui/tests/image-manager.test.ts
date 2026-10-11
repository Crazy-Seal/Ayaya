import assert from "node:assert/strict";
import { test } from "node:test";
import { ImageManager } from "../src/chat/image-manager.js";

test("切换会话或清空后忽略旧选图，新草稿选图仍正常加入", /**
 * 模拟原生选图尚未返回时切换会话，旧结果不得进入当前草稿。
 * Returns:
 *     Promise<void>: 两次选图结果均已处理。
 */
async () => {
  const manager = new ImageManager();
  let complete!: (images: string[]) => void;
  const selection = new Promise<string[]>(/**
   * 保存模拟原生选图的结束入口，由测试在草稿清空后交付结果。
   * Args:
   *     resolve ((images: string[]) => void): 结束选图等待的函数。
   */
  (resolve) => { complete = resolve; });
  const pending = manager.addFromSelection(/**
   * 返回尚未完成的旧会话选择，不打开系统文件选择框。
   * Returns:
   *     Promise<string[]>: 由本用例控制返回时间的图片地址。
   */
  () => selection);
  manager.clear();
  manager.addDataUrls(["new-session-existing-image"]);
  complete(["old-session-image"]);
  await pending;
  assert.deepEqual(manager.getDataUrls(), ["new-session-existing-image"]);
  await manager.addFromSelection(/**
   * 模拟新会话正常完成的选择。
   * Returns:
   *     Promise<string[]>: 当前草稿新图片。
   */
  async () => ["new-session-selected-image"]);
  assert.deepEqual(manager.getDataUrls(), ["new-session-existing-image", "new-session-selected-image"]);
});

/**
 * 为真实附件转换流程提供可控制完成时间的图片与 canvas，文件仅存在于内存。
 * Returns:
 *     { images: TestImage[]; restore: () => void }: 等待转换的图片和全局环境恢复入口。
 */
function installImageConversion() {
  const previousImage = globalThis.Image;
  const previousDocument = globalThis.document;
  const images: TestImage[] = [];
  /** 模拟浏览器图片读取，测试手动触发 onload 以安排会话切换顺序。 */
  class TestImage {
    width = 1;
    height = 1;
    src = "";
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    /** 构造转换图片时登记本次等待，供测试控制图片加载完成。 */
    constructor() { images.push(this); }
  }
  globalThis.Image = TestImage as unknown as typeof Image;
  globalThis.document = {
    /**
     * 图片加载后提供内存 canvas，不使用文件系统或真实屏幕。
     * Returns:
     *     object: 真实转换函数所需的绘制与 JPEG 输出接口。
     */
    createElement: () => ({
      /**
       * 模拟 canvas 的二维绘图上下文。
       * Returns:
       *     object: 无副作用的绘制入口。
       */
      getContext: () => ({ /** 接收图片绘制，不采集或保存任何画面。 */ drawImage() {} }),
      /**
       * 返回固定图片正文，让测试只核对附件归属与数量。
       * Returns:
       *     string: 当前测试图片的 data URL。
       */
      toDataURL: () => "data:image/jpeg;base64,dGVzdA==",
    }),
  } as unknown as Document;
  return {
    images,
    /** 用例结束后恢复浏览器全局替身，不影响其他单元测试。 */
    restore(): void { globalThis.Image = previousImage; globalThis.document = previousDocument; },
  };
}

test("图片转换完成前切换会话，旧粘贴或拖入不会恢复到新草稿", /**
 * 运行真实 addFiles 与 JPEG 转换流程，在异步读取期间清空并建立新会话附件。
 * Returns:
 *     Promise<void>: 旧转换已结束且当前附件保持正确归属。
 */
async () => {
  const conversion = installImageConversion();
  try {
    const manager = new ImageManager();
    const pending = manager.addFiles([new File(["image"], "old.png", { type: "image/png" })]);
    manager.clear();
    manager.addDataUrls(["new-session-image"]);
    conversion.images[0].onload!();
    assert.equal(await pending, 0);
    assert.deepEqual(manager.getDataUrls(), ["new-session-image"]);
  } finally { conversion.restore(); }
});

test("并发读取图片完成时重新核对附件上限", /**
 * 同时开始两次转换，第二次完成时不能突破另一批次已占用的附件数量。
 * Returns:
 *     Promise<void>: 并发读取结束且附件数量未越界。
 */
async () => {
  const conversion = installImageConversion();
  try {
    const manager = new ImageManager(1);
    const first = manager.addFiles([new File(["a"], "a.png", { type: "image/png" })]);
    const second = manager.addFiles([new File(["b"], "b.png", { type: "image/png" })]);
    conversion.images[0].onload!();
    assert.equal(await first, 1);
    conversion.images[1].onload!();
    assert.equal(await second, 0);
    assert.equal(manager.getCount(), 1);
  } finally { conversion.restore(); }
});
