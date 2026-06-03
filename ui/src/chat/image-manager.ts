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
 * @param file 图片文件
 * @param quality JPEG 压缩质量 (0-1)，默认 0.8
 */
export const convertToJpegDataUrl = async (file: File, quality = 0.8): Promise<string> => {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
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
    img.onerror = () => {
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
    return this.images.map((img) => img.dataUrl);
  }

  /**
   * 添加图片文件
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
   */
  removeImage(id: string): void {
    this.images = this.images.filter((img) => img.id !== id);
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
   */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private notifyListeners(): void {
    this.listeners.forEach((listener) => listener());
  }

  private fileToDataUrl(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(new Error("Failed to read file"));
      reader.readAsDataURL(file);
    });
  }
}
