import type {
  ChatInterruptData,
  ControlScreenCaptureInterruptData,
  ControlScreenExecuteInterruptData,
} from "../../shared-types.js";

export const isControlScreenCaptureInterrupt = (
  interrupt: ChatInterruptData | { type?: unknown },
): interrupt is ControlScreenCaptureInterruptData =>
  interrupt.type === "control_screen_capture_request";

export const isControlScreenExecuteInterrupt = (
  interrupt: ChatInterruptData | { type?: unknown },
): interrupt is ControlScreenExecuteInterruptData =>
  interrupt.type === "control_screen_execute_request";
