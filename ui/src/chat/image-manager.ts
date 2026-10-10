/**
 * 图片管理器 - 管理待发送的图片状态
 */

/**
 * 待发送图片
 */
export interface PendingImage {
  id: string;
  dataUrl: string;
  file?: File;
}

/**
 * 将图片文件转换为 JPEG data URL
 *
 * Args:
 *     file (File): 图片文件
 *     quality (number): JPEG 压缩质量 (0-1)，默认 0.8
 */
export const convertToJpegDataUrl = async (file: File, quality = 0.8): Promise<string> => {
  return new Promise(/**
   * Promise 创建时准备图片加载器，加载成功后转换 JPEG，失败时清理资源并拒绝等待。
   *
   * Args:
   *     resolve ((value: string | PromiseLike<string>) => void): 准备成功或等待结束时完成 Promise 的函数。
   *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
   */
  (resolve, reject) => {
    const img = new Image();
    img.onload = /**
     * 图片加载成功后按原尺寸绘制到 canvas 并生成 JPEG，完成后释放临时对象地址。
     */
    () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        reject(new Error("Canvas context not available"));
        return;
      }
      ctx.drawImage(img, 0, 0);
      const dataUrl = canvas.toDataURL("image/jpeg", quality);
      URL.revokeObjectURL(img.src);
      resolve(dataUrl);
    };
    img.onerror = /**
     * 图片加载失败后释放临时对象地址，并使附件转换等待明确失败。
     */
    () => {
      URL.revokeObjectURL(img.src);
      reject(new Error("Failed to load image"));
    };
    img.src = URL.createObjectURL(file);
  });
};

/**
 * 图片管理器类
 */
export class ImageManager {
  private images: PendingImage[] = [];
  private maxImages = 5;
  private listeners: Set<() => void> = new Set();

  /**
   * 保存附件数量上限，创建当前用户输入的空图片集合。
   *
   * Args:
   *     maxImages (number): 一次输入最多接收的图片数量。
   */
  constructor(maxImages = 5) {
    this.maxImages = maxImages;
  }

  /**
   * 获取所有待发送图片
   */
  getImages(): PendingImage[] {
    return [...this.images];
  }

  /**
   * 获取所有图片的 data URL 数组
   */
  getDataUrls(): string[] {
    return this.images.map(/**
     * 提取当前草稿图片的 data URL，作为发送命令的附件载荷。
     *
     * Args:
     *     img (PendingImage): 当前草稿附件，包含稳定编号和模型可读图片地址。
     *
     * Returns:
     *     string: 当前图片的模型可读地址。
     */
    (img) => img.dataUrl);
  }

  /**
   * 添加图片文件
   *
   * Args:
   *     files (FileList | File[]): 用户拖入或选中的文件集合。
   */
  async addFiles(files: FileList | File[]): Promise<number> {
    const fileArray = Array.from(files);
    let addedCount = 0;

    for (const file of fileArray) {
      if (this.images.length >= this.maxImages) break;
      if (!file.type.startsWith("image/")) continue;

      try {
        // 所有图片统一转换为 JPEG 格式以减小体积
        const dataUrl = await convertToJpegDataUrl(file);

        this.images.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          dataUrl,
          file,
        });
        addedCount++;
      } catch (error) {
        console.warn("Failed to process image:", error);
      }
    }

    if (addedCount > 0) {
      this.notifyListeners();
    }

    return addedCount;
  }

  /**
   * 添加 data URL
   *
   * Args:
   *     dataUrls (string[]): 已读取的用户图片地址列表。
   */
  addDataUrls(dataUrls: string[]): void {
    for (const dataUrl of dataUrls) {
      if (this.images.length >= this.maxImages) break;

      this.images.push({
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        dataUrl,
      });
    }

    this.notifyListeners();
  }

  /**
   * 移除单张图片
   *
   * Args:
   *     id (string): 当前模型参数或日志记录的编号。
   */
  removeImage(id: string): void {
    this.images = this.images.filter(/**
     * 移除指定编号图片时保留其他草稿附件。
     *
     * Args:
     *     img (PendingImage): 当前草稿附件，包含稳定编号和模型可读图片地址。
     *
     * Returns:
     *     boolean: 当前图片编号不是待删除编号时为 true。
     */
    (img) => img.id !== id);
    this.notifyListeners();
  }

  /**
   * 清空所有图片
   */
  clear(): void {
    this.images = [];
    this.notifyListeners();
  }

  /**
   * 是否为空
   */
  isEmpty(): boolean {
    return this.images.length === 0;
  }

  /**
   * 获取图片数量
   */
  getCount(): number {
    return this.images.length;
  }

  /**
   * 订阅状态变化
   *
   * Args:
   *     listener (() => void): 接收后续事件的回调，取消订阅后不再调用。
   */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    /**
     * 调用方取消附件变化订阅时移除当前监听者。
     */
    return () => this.listeners.delete(listener);
  }

  /**
   * 附件新增、移除或清空后通知监听者刷新预览和发送按钮。
   */
  private notifyListeners(): void {
    this.listeners.forEach(/**
     * 附件集合已经更新后调用各监听者，让预览和发送按钮读取新状态。
     *
     * Args:
     *     listener (() => void): 接收后续事件的回调，取消订阅后不再调用。
     */
    (listener) => listener());
  }

  /**
   * 用 FileReader 读取用户附件，完成后交给图片格式转换流程。
   *
   * Args:
   *     file (File): 用户拖入或选取的图片文件。
   *
   * Returns:
   *     Promise<string>: 图片文件对应的 data URL，读取失败时拒绝。
   */
  private fileToDataUrl(file: File): Promise<string> {
    return new Promise(/**
     * Promise 创建时登记 FileReader 成功与失败处理，再开始读取用户图片文件。
     *
     * Args:
     *     resolve ((value: string | PromiseLike<string>) => void): 准备成功或等待结束时完成 Promise 的函数。
     *     reject ((reason?: any) => void): 准备失败或等待被取消时拒绝 Promise 的函数。
     */
    (resolve, reject) => {
      const reader = new FileReader();
      reader.onload = /**
       * 文件读取成功后以完整 data URL 结束附件读取等待。
       */
      () => resolve(reader.result as string);
      reader.onerror = /**
       * 文件读取失败后以明确错误拒绝附件读取等待。
       */
      () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(file);
    });
  }
}
