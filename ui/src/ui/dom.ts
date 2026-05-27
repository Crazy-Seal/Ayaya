/**
 * DOM 元素引用
 */

/**
 * 主要 UI 元素
 */
export interface MainUiElements {
  stageHost: HTMLDivElement;
  bubble: HTMLDivElement;
  chatHistoryList: HTMLDivElement;
  form: HTMLFormElement;
  input: HTMLTextAreaElement;
  sendBtn: HTMLButtonElement;
  settingsBtn: HTMLButtonElement;
  imagePreviewContainer: HTMLDivElement;
  imagePreviewList: HTMLDivElement;
  clearImagesBtn: HTMLButtonElement;
  dragOverlay: HTMLDivElement;
  imageInput: HTMLInputElement;
  imageBtn: HTMLButtonElement;
}

/**
 * 获取主要 UI 元素
 */
export const getMainUiElements = (): MainUiElements => {
  const stageHost = document.querySelector<HTMLDivElement>("#live2d-stage");
  const bubble = document.querySelector<HTMLDivElement>("#bubble");
  const chatHistoryList = document.querySelector<HTMLDivElement>("#chat-history-list");
  const form = document.querySelector<HTMLFormElement>("#chat-form");
  const input = document.querySelector<HTMLTextAreaElement>("#chat-input");
  const sendBtn = document.querySelector<HTMLButtonElement>("#send-btn");
  const settingsBtn = document.querySelector<HTMLButtonElement>("#settings-btn");
  const imagePreviewContainer = document.querySelector<HTMLDivElement>("#image-preview-container");
  const imagePreviewList = document.querySelector<HTMLDivElement>("#image-preview-list");
  const clearImagesBtn = document.querySelector<HTMLButtonElement>("#clear-images-btn");
  const dragOverlay = document.querySelector<HTMLDivElement>("#drag-overlay");
  const imageInput = document.querySelector<HTMLInputElement>("#image-input");
  const imageBtn = document.querySelector<HTMLButtonElement>("#image-btn");

  if (
    !stageHost ||
    !bubble ||
    !chatHistoryList ||
    !form ||
    !input ||
    !sendBtn ||
    !settingsBtn ||
    !imagePreviewContainer ||
    !imagePreviewList ||
    !clearImagesBtn ||
    !dragOverlay ||
    !imageInput ||
    !imageBtn
  ) {
    throw new Error("UI 初始化失败");
  }

  return {
    stageHost,
    bubble,
    chatHistoryList,
    form,
    input,
    sendBtn,
    settingsBtn,
    imagePreviewContainer,
    imagePreviewList,
    clearImagesBtn,
    dragOverlay,
    imageInput,
    imageBtn,
  };
};
