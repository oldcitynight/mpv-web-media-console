export function getCanvasClientRect(canvas) {
  return canvas.getBoundingClientRect();
}

export function pointerEventToNdc(event, canvasRect, target) {
  const nextTarget = target ?? { x: 0, y: 0 };
  nextTarget.x = ((event.clientX - canvasRect.left) / canvasRect.width) * 2 - 1;
  nextTarget.y = -((event.clientY - canvasRect.top) / canvasRect.height) * 2 + 1;
  return nextTarget;
}
