import type {
  ChatSettingsState,
  ISettingsPage,
  PageEditingData,
  PageEventCallback,
  PageRenderData,
} from "../types.js";

type ContextStrategyKey = keyof ChatSettingsState["context_strategy"];

export const CONTEXT_STRATEGY_FIELDS: ContextStrategyKey[] = [
  "compression_trigger_tokens_k",
];

export const getContextStrategyMinimum = (_key: ContextStrategyKey): number => 10;

export class ContextStrategyPage implements ISettingsPage {
  private inputs: Record<ContextStrategyKey, HTMLInputElement>;
  private eventCallback?: PageEventCallback;

  constructor(inputs: Record<ContextStrategyKey, HTMLInputElement>, confirmBtn: HTMLButtonElement) {
    this.inputs = inputs;
    confirmBtn.addEventListener("click", () => {
      this.eventCallback?.({ type: "submit", page: "contextStrategy" });
    });
  }

  onEvent(callback: PageEventCallback): void {
    this.eventCallback = callback;
  }

  render(data: PageRenderData): void {
    for (const key of CONTEXT_STRATEGY_FIELDS) {
      this.inputs[key].value = data.saved.context_strategy[key] == null
        ? ""
        : String(data.saved.context_strategy[key]);
    }
  }

  getEditingData(): PageEditingData {
    return {
      contextStrategy: {
        context_strategy: {
          compression_trigger_tokens_k: Number(this.inputs.compression_trigger_tokens_k.value),
        },
      },
    };
  }
}
