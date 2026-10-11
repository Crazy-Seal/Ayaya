/**
 * 拖拽处理器 - 处理图片拖拽到独立聊天窗口
 */

import type { ImageManager } from "./image-manager.js";

/**
 * 拖拽处理器选项
 */
export interface DropHandlerOptions {
  root: HTMLElement;
  imageManager: ImageManager;
  dragOverlay: HTMLElement;
}

/**
 * 设置拖拽处理器
 *
 * Args:
 *     options (DropHandlerOptions): 调用方提供的配置或运行选项。
 */
export const setupDropHandler = (options: DropHandlerOptions): (() => void) => {
  const { root, imageManager, dragOverlay } = options;

  let dragCounter = 0;
  let isDragging = false;

  /**
   * 图片拖入聊天区域时阻止默认操作并显示可投放提示。
   *
   * Args:
   *     event (DragEvent): 调用方或浏览器触发的当前事件。
   */
  const handleDragEnter = (event: DragEvent) => {
    event.preventDefault();
    if (!event.dataTransfer?.types.includes("Files")) return;

    dragCounter++;
    if (!isDragging) {
      isDragging = true;
      dragOverlay.hidden = false;
    }
  };

  /**
   * 拖拽离开聊天区域时更新进入计数，完全离开后清除投放提示。
   *
   * Args:
   *     event (DragEvent): 调用方或浏览器触发的当前事件。
   */
  const handleDragLeave = (event: DragEvent) => {
    event.preventDefault();
    dragCounter--;
    if (dragCounter === 0) {
      isDragging = false;
      dragOverlay.hidden = true;
    }
  };

  /**
   * 拖拽经过聊天区域时阻止默认导航，并允许在这里放下图片。
   *
   * Args:
   *     event (DragEvent): 调用方或浏览器触发的当前事件。
   */
  const handleDragOver = (event: DragEvent) => {
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "copy";
    }
  };

  /**
   * 用户放下文件时清除拖拽提示，筛选图片并交给附件管理器读取。
   *
   * Args:
   *     event (DragEvent): 调用方或浏览器触发的当前事件。
   *
   * Returns:
   *     Promise<void>: 本批附件处理完成后结束。
   */
  const handleDrop = async (event: DragEvent) => {
    event.preventDefault();
    dragCounter = 0;
    isDragging = false;
    dragOverlay.hidden = true;

    const files = event.dataTransfer?.files;
    if (files && files.length > 0) {
      const imageFiles = Array.from(files).filter(/**
       * 只接收 MIME 声明为图片的拖入文件，其他文件不加入附件。
       *
       * Args:
       *     file (File): 用户拖入或选取的图片文件。
       *
       * Returns:
       *     boolean: 文件类型以 image/ 开头时为 true。
       */
      (file) =>
        file.type.startsWith("image/")
      );
      if (imageFiles.length > 0) {
        await imageManager.addFiles(imageFiles);
      }
    }
  };

  // 只在窗口根容器监听，避免表单冒泡导致同一张图片被重复添加
  const targets = [root];

  targets.forEach(/**
   * 为每个拖拽目标登记同一组进入、离开、经过及放下处理器。
   *
   * Args:
   *     target (HTMLElement): 本次操作目标。
   */
  (target) => {
    target.addEventListener("dragenter", handleDragEnter);
    target.addEventListener("dragleave", handleDragLeave);
    target.addEventListener("dragover", handleDragOver);
    target.addEventListener("drop", handleDrop);
  });

  // 返回清理函数
  /**
   * 组件释放时逐个目标移除本次登记的拖拽监听，避免重复响应。
   */
  return () => {
    targets.forEach(/**
     * 清理当前目标的全部拖拽监听，使用与登记时相同的函数引用。
     *
     * Args:
     *     target (HTMLElement): 本次操作目标。
     */
    (target) => {
      target.removeEventListener("dragenter", handleDragEnter);
      target.removeEventListener("dragleave", handleDragLeave);
      target.removeEventListener("dragover", handleDragOver);
      target.removeEventListener("drop", handleDrop);
    });
  };
};
