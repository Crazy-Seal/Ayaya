const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopPetApi", {
  chat: async (message, sessionId, requestId, images) => {
    return ipcRenderer.invoke("desktop-pet:chat", { message, sessionId, requestId, images });
  },
  selectImages: () => {
    return ipcRenderer.invoke("desktop-pet:select-images");
  },
  setMousePassthrough: (enabled) => {
    ipcRenderer.send("desktop-pet:set-mouse-passthrough", Boolean(enabled));
  },
  setPointerInteractive: (enabled) => {
    ipcRenderer.send("desktop-pet:set-pointer-interactive", Boolean(enabled));
  },
  openSettingsWindow: () => {
    ipcRenderer.send("desktop-pet:open-settings-window");
  },
  minimizeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:minimize-current-window");
  },
  closeCurrentWindow: () => {
    ipcRenderer.send("desktop-pet:close-current-window");
  },
  openImagePreview: (imageSrc) => {
    ipcRenderer.send("desktop-pet:open-image-preview", imageSrc);
  },
  getActiveModel: () => {
    return ipcRenderer.invoke("desktop-pet:get-active-model");
  },
  getModelConfig: () => {
    return ipcRenderer.invoke("desktop-pet:get-model-config");
  },
  getChatSettings: () => {
    return ipcRenderer.invoke("desktop-pet:get-chat-settings");
  },
  getLatestAiMessage: (sessionId) => {
    return ipcRenderer.invoke("desktop-pet:get-latest-ai-message", sessionId);
  },
  getChatHistory: (sessionId, start, limit) => {
    return ipcRenderer.invoke("desktop-pet:get-chat-history", sessionId, start, limit);
  },
  getChatHistoryLastN: (sessionId, n) => {
    return ipcRenderer.invoke("desktop-pet:get-chat-history-last-n", sessionId, n);
  },
  updateChatSettings: (payload) => {
    return ipcRenderer.invoke("desktop-pet:update-chat-settings", payload);
  },
  getAvailableTools: () => {
    return ipcRenderer.invoke("desktop-pet:get-available-tools");
  },
  previewLive2DImport: () => {
    return ipcRenderer.invoke("desktop-pet:preview-live2d-import");
  },
  importLive2DModel: (payload) => {
    return ipcRenderer.invoke("desktop-pet:import-live2d-model", payload);
  },
  updateModelTransform: (payload) => {
    return ipcRenderer.invoke("desktop-pet:update-model-transform", payload);
  },
  setActiveModel: (modelId) => {
    return ipcRenderer.invoke("desktop-pet:set-active-model", modelId);
  },
  deleteModel: (modelId) => {
    return ipcRenderer.invoke("desktop-pet:delete-model", modelId);
  },
  onCursor: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("desktop-pet:cursor", handler);
    return () => ipcRenderer.removeListener("desktop-pet:cursor", handler);
  },
  onModelChanged: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("desktop-pet:model-changed", handler);
    return () => ipcRenderer.removeListener("desktop-pet:model-changed", handler);
  },
  onModelTransformChanged: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("desktop-pet:model-transform-changed", handler);
    return () => ipcRenderer.removeListener("desktop-pet:model-transform-changed", handler);
  },
  onChatChunk: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("desktop-pet:chat-chunk", handler);
    return () => ipcRenderer.removeListener("desktop-pet:chat-chunk", handler);
  },
  // 截屏审批响应（返回 SSE 流）
  screenshotRespond: (sessionId, approved, requestId) => {
    return ipcRenderer.invoke("desktop-pet:screenshot-respond", { sessionId, approved, requestId });
  },
  // 监听截屏中断事件
  onChatInterrupt: (listener) => {
    const handler = (_event, payload) => listener(payload);
    ipcRenderer.on("desktop-pet:chat-interrupt", handler);
    return () => ipcRenderer.removeListener("desktop-pet:chat-interrupt", handler);
  }
});
