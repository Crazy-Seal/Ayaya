/**
 * DOM 元素引用
 */

/**
 * 主要 UI 元素
 */
export interface MainUiElements {
  stageHost: HTMLDivElement;
  bubble: HTMLDivElement;
  form: HTMLFormElement;
  input: HTMLInputElement;
  sendBtn: HTMLButtonElement;
  settingsBtn: HTMLButtonElement;
}

/**
 * 获取主要 UI 元素
 */
export const getMainUiElements = (): MainUiElements => {
  const stageHost = document.querySelector<HTMLDivElement>("#live2d-stage");
  const bubble = document.querySelector<HTMLDivElement>("#bubble");
  const form = document.querySelector<HTMLFormElement>("#chat-form");
  const input = document.querySelector<HTMLInputElement>("#chat-input");
  const sendBtn = document.querySelector<HTMLButtonElement>("#send-btn");
  const settingsBtn = document.querySelector<HTMLButtonElement>("#settings-btn");

  if (!stageHost || !bubble || !form || !input || !sendBtn || !settingsBtn) {
    throw new Error("UI 初始化失败");
  }

  return {
    stageHost,
    bubble,
    form,
    input,
    sendBtn,
    settingsBtn,
  };
};
