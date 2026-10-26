// 库 → 面板 的显式带入通道：只有点了「带入」才会广播一次，面板自己决定怎么应用。
export type BringInDetail = { kind: 'timbre' | 'music'; params: Record<string, unknown> };
const EVENT = 'director:library-bring-in';

// 带入的目标面板可能还没挂载（例如在「库」页点带入、再去后期），所以留一份待领取。
let pending: BringInDetail | null = null;

export function emitBringIn(detail: BringInDetail) {
  pending = detail;
  window.dispatchEvent(new CustomEvent<BringInDetail>(EVENT, { detail }));
}

export function consumeBringIn(kind: BringInDetail['kind']): Record<string, unknown> | null {
  if (!pending || pending.kind !== kind) return null;
  const params = pending.params;
  pending = null;
  return params;
}

export function onBringIn(handler: (detail: BringInDetail) => void): () => void {
  const listener = (event: Event) => handler((event as CustomEvent<BringInDetail>).detail);
  window.addEventListener(EVENT, listener);
  return () => window.removeEventListener(EVENT, listener);
}
