let resizeListenerInstalled = false;
const subscribers = new Set();

export function getWindowViewport() {
  return {
    width: Math.max(1, window.innerWidth || 1),
    height: Math.max(1, window.innerHeight || 1),
    dpr: Math.max(1, window.devicePixelRatio || 1)
  };
}

function notifySubscribers() {
  const viewport = getWindowViewport();
  subscribers.forEach((listener) => {
    listener(viewport);
  });
}

function ensureResizeListener() {
  if (resizeListenerInstalled) {
    return;
  }
  window.addEventListener('resize', notifySubscribers);
  resizeListenerInstalled = true;
}

export function subscribeWindowViewport(listener) {
  ensureResizeListener();
  subscribers.add(listener);
  listener(getWindowViewport());
  return () => {
    subscribers.delete(listener);
  };
}
