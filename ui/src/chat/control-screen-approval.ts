import type { ControlScreenActionData, ScreenActionPoint } from "../types.js";

export const formatControlScreenApprovalMessage = (
  action: ControlScreenActionData,
  point: ScreenActionPoint,
): string => {
  const operationText: Record<ControlScreenActionData["operation"], string> = {
    click: "单击",
    double: "双击",
    right: "右键单击",
    scroll: action.scroll_direction === "up" ? "向上滚动" : "向下滚动",
  };
  const inputText = action.text ? `，并输入“${action.text}”` : "";
  const enterText = action.text && action.press_enter ? "后按回车" : "";

  return `Agent 请求在屏幕坐标 (${point.x}, ${point.y}) 对“${action.target}”执行${operationText[action.operation]}${inputText}${enterText}。是否允许？`;
};
