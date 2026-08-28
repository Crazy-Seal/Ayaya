export type ScreenCaptureSource = {
  display_id: string;
};

export const selectPrimaryDisplaySource = <T extends ScreenCaptureSource>(
  sources: readonly T[],
  primaryDisplayId: number,
): T => {
  const displayId = String(primaryDisplayId);
  const source = sources.find((candidate) => candidate.display_id === displayId);
  if (!source) {
    throw new Error(`未找到主显示器对应的截屏来源（display_id=${displayId}）`);
  }
  return source;
};
