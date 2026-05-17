/**
 * 拖拽处理器 - 处理图片拖拽到 Live2D 舞台和聊天表单
 */

import type { ImageManager } from "./image-manager.js";

/**
 * 拖拽处理器选项
 */
export interface DropHandlerOptions {
  stageHost: HTMLElement;
  form: HTMLElement;
  imageManager: ImageManager;
  dragOverlay: HTMLElement;
}

/**
 * 设置拖拽处理器
 */
export const setupDropHandler = (options: DropHandlerOptions): (() => void) => {
  const { stageHost, form, imageManager, dragOverlay } = options;

  let dragCounter = 0;
  let isDragging = false;

  const handleDragEnter = (event: DragEvent) => {
    event.preventDefault();
    if (!event.dataTransfer?.types.includes("Files")) return;

    dragCounter++;
    if (!isDragging) {
      isDragging = true;
      dragOverlay.hidden = false;
    }
  };

  const handleDragLeave = (event: DragEvent) => {
    event.preventDefault();
    dragCounter--;
    if (dragCounter === 0) {
      isDragging = false;
      dragOverlay.hidden = true;
    }
  };

  const handleDragOver = (event: DragEvent) => {
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "copy";
    }
  };

  const handleDrop = async (event: DragEvent) => {
    event.preventDefault();
    dragCounter = 0;
    isDragging = false;
    dragOverlay.hidden = true;

    const files = event.dataTransfer?.files;
    if (files && files.length > 0) {
      const imageFiles = Array.from(files).filter((file) =>
        file.type.startsWith("image/")
      );
      if (imageFiles.length > 0) {
        await imageManager.addFiles(imageFiles);
      }
    }
  };

  // 添加监听器到舞台和表单
  const targets = [stageHost, form];

  targets.forEach((target) => {
    target.addEventListener("dragenter", handleDragEnter);
    target.addEventListener("dragleave", handleDragLeave);
    target.addEventListener("dragover", handleDragOver);
    target.addEventListener("drop", handleDrop);
  });

  // 返回清理函数
  return () => {
    targets.forEach((target) => {
      target.removeEventListener("dragenter", handleDragEnter);
      target.removeEventListener("dragleave", handleDragLeave);
      target.removeEventListener("dragover", handleDragOver);
      target.removeEventListener("drop", handleDrop);
    });
  };
};
