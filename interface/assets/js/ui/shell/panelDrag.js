const STORAGE_KEY = 'kira.ui.panelLayout.v1';
const DRAG_THRESHOLD = 4;
const MIN_SIZE = { width: 220, height: 140 };
const DIRECTIONS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

const PANELS = Object.freeze([
  { key: 'transcript', ids: ['kira-transcript-panel'], handle: '.left-panel-header', resizable: true },
  { key: 'apis', ids: ['kira-session-status-panel'], handle: '.runtime-status-title', resizable: true },
  { key: 'clock', ids: ['header-clock'], handle: null, resizable: false },
  { key: 'side', ids: ['drawer-whatsapp-btn-float', 'drawer-nav-btn-float', 'drawer-music-btn-float'], handle: null, resizable: false, peek: 90 },
]);

const clamp = (value, min, max) => Math.min(Math.max(value, min), Math.max(min, max));
const finite = (value) => (Number.isFinite(value) ? value : 0);

export function installPanelDrag({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const groups = [];
  const viewport = () => ({ width: windowRef.innerWidth || 1, height: windowRef.innerHeight || 1 });

  const readStore = () => {
    try {
      const value = JSON.parse(windowRef.localStorage.getItem(STORAGE_KEY) || '{}');
      return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
    } catch (_) { return {}; }
  };
  const writeStore = (key, value) => {
    try {
      const store = readStore();
      if (value) store[key] = value; else delete store[key];
      windowRef.localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
    } catch (_) { /* armazenamento indisponível: a posição vale só nesta sessão */ }
  };

  const union = (group) => {
    const rects = group.elements.map((element) => element.getBoundingClientRect());
    return {
      left: Math.min(...rects.map((r) => r.left)),
      top: Math.min(...rects.map((r) => r.top)),
      right: Math.max(...rects.map((r) => r.right)),
      bottom: Math.max(...rects.map((r) => r.bottom)),
    };
  };

  const applyOffset = (group, dx, dy) => {
    group.elements.forEach((element) => { element.style.translate = `${Math.round(dx)}px ${Math.round(dy)}px`; });
    group.dx = dx;
    group.dy = dy;
  };

  const fitIntoViewport = (group) => {
    const { width, height } = viewport();
    const rect = union(group);
    const maxLeft = width - (group.peek || rect.right - rect.left);
    const shiftX = rect.left < 0 ? -rect.left : rect.left > maxLeft ? maxLeft - rect.left : 0;
    const shiftY = rect.top < 0 ? -rect.top : rect.bottom > height ? height - rect.bottom : 0;
    if (shiftX || shiftY) applyOffset(group, group.dx + shiftX, group.dy + shiftY);
  };

  const persist = (group) => {
    const { width, height } = viewport();
    const saved = { dx: group.dx / width, dy: group.dy / height };
    if (group.sized) {
      const rect = group.elements[0].getBoundingClientRect();
      saved.w = rect.width / width;
      saved.h = rect.height / height;
    }
    writeStore(group.key, saved);
  };

  const applySize = (group, widthPx, heightPx) => {
    Object.assign(group.elements[0].style, {
      width: `${Math.round(widthPx)}px`,
      height: `${Math.round(heightPx)}px`,
      minWidth: '0',
      minHeight: '0',
      maxWidth: 'none',
      maxHeight: 'none',
      flex: 'none',
    });
    group.sized = true;
  };

  const restore = (group) => {
    const saved = readStore()[group.key];
    if (!saved) return;
    const { width, height } = viewport();
    if (group.resizable && finite(saved.w) > 0 && finite(saved.h) > 0) {
      applySize(group, clamp(saved.w * width, MIN_SIZE.width, width), clamp(saved.h * height, MIN_SIZE.height, height));
    }
    applyOffset(group, finite(saved.dx) * width, finite(saved.dy) * height);
    fitIntoViewport(group);
  };

  let suppressClick = false;
  documentRef.addEventListener('click', (event) => {
    if (!suppressClick) return;
    suppressClick = false;
    event.stopPropagation();
    event.preventDefault();
  }, true);

  const beginGesture = (group, event, onMove) => {
    if (event.button != null && event.button !== 0) return;
    const startX = event.clientX;
    const startY = event.clientY;
    const origin = { dx: group.dx, dy: group.dy };
    const target = event.currentTarget;
    let active = false;
    const move = (next) => {
      const deltaX = next.clientX - startX;
      const deltaY = next.clientY - startY;
      if (!active) {
        if (Math.hypot(deltaX, deltaY) < DRAG_THRESHOLD) return;
        active = true;
        documentRef.body.classList.add('kui-panel-dragging');
      }
      next.preventDefault?.();
      onMove(deltaX, deltaY, origin);
    };
    const finish = () => {
      target.removeEventListener('pointermove', move);
      target.removeEventListener('pointerup', finish);
      target.removeEventListener('pointercancel', finish);
      try { target.releasePointerCapture(event.pointerId); } catch (_) { /* captura já liberada */ }
      documentRef.body.classList.remove('kui-panel-dragging');
      if (!active) return;
      suppressClick = true;
      windowRef.setTimeout(() => { suppressClick = false; }, 0);
      fitIntoViewport(group);
      persist(group);
    };
    target.addEventListener('pointermove', move);
    target.addEventListener('pointerup', finish);
    target.addEventListener('pointercancel', finish);
    try { target.setPointerCapture(event.pointerId); } catch (_) { /* sem captura: eventos seguem pelo alvo */ }
  };

  const startMove = (group, event) => {
    if (event.target.closest('input, textarea, select, a')) return;
    if (group.handleOnly && event.target.closest('button')) return;
    beginGesture(group, event, (deltaX, deltaY, origin) => {
      applyOffset(group, origin.dx + deltaX, origin.dy + deltaY);
      fitIntoViewport(group);
    });
  };

  const startResize = (group, event, direction) => {
    event.stopPropagation();
    const element = group.elements[0];
    const base = element.getBoundingClientRect();
    beginGesture(group, event, (deltaX, deltaY) => {
      const { width, height } = viewport();
      let { left, top, right, bottom } = base;
      if (direction.includes('e')) right = clamp(base.right + deltaX, base.left + MIN_SIZE.width, width);
      if (direction.includes('w')) left = clamp(base.left + deltaX, 0, base.right - MIN_SIZE.width);
      if (direction.includes('s')) bottom = clamp(base.bottom + deltaY, base.top + MIN_SIZE.height, height);
      if (direction.includes('n')) top = clamp(base.top + deltaY, 0, base.bottom - MIN_SIZE.height);
      applySize(group, right - left, bottom - top);
      const actual = element.getBoundingClientRect();
      applyOffset(group, group.dx + (left - actual.left), group.dy + (top - actual.top));
    });
  };

  PANELS.forEach((spec) => {
    const elements = spec.ids.map((id) => documentRef.getElementById(id)).filter(Boolean);
    if (!elements.length) return;
    const group = { key: spec.key, elements, resizable: spec.resizable, handleOnly: Boolean(spec.handle), peek: spec.peek, dx: 0, dy: 0, sized: false };
    const handles = spec.handle ? [elements[0].querySelector(spec.handle)].filter(Boolean) : elements;
    handles.forEach((handle) => {
      handle.classList.add('kui-panel-handle');
      handle.addEventListener('pointerdown', (event) => startMove(group, event));
    });
    if (spec.resizable) {
      const host = elements[0];
      if (windowRef.getComputedStyle(host).position === 'static') host.style.position = 'relative';
      DIRECTIONS.forEach((direction) => {
        const grip = documentRef.createElement('div');
        grip.className = `kui-panel-grip kui-panel-grip-${direction}`;
        grip.setAttribute('aria-hidden', 'true');
        grip.addEventListener('pointerdown', (event) => startResize(group, event, direction));
        host.appendChild(grip);
      });
    }
    groups.push(group);
    restore(group);
  });

  const onResize = () => groups.forEach((group) => restore(group));
  windowRef.addEventListener('resize', onResize);

  // Por voz ("põe a transcrição no canto direito", "aumenta o painel de APIs"): onde e tamanho em palavras.
  const WHERE = {
    esquerda: [0, 0.5], direita: [1, 0.5], centro: [0.5, 0.5], meio: [0.5, 0.5], topo: [0.5, 0], cima: [0.5, 0], baixo: [0.5, 1],
    'canto superior esquerdo': [0, 0], 'canto superior direito': [1, 0], 'canto inferior esquerdo': [0, 1], 'canto inferior direito': [1, 1],
  };
  const place = (key, where = '', size = '') => {
    const group = groups.find((item) => item.key === key);
    if (!group) return false;
    const { width, height } = viewport();
    const element = group.elements[0];
    if (size && group.resizable) {
      const rect = element.getBoundingClientRect();
      const sized = {
        maior: [rect.width * 1.25, rect.height * 1.25], menor: [rect.width * 0.8, rect.height * 0.8],
        grande: [width * 0.42, height * 0.8], medio: [width * 0.28, height * 0.6], pequeno: [width * 0.2, height * 0.38],
      }[size];
      if (size === 'padrao') {
        ['width', 'height', 'minWidth', 'minHeight', 'maxWidth', 'maxHeight', 'flex'].forEach((property) => { element.style[property] = ''; });
        group.sized = false;
      } else if (sized) {
        applySize(group, clamp(sized[0], MIN_SIZE.width, width - 32), clamp(sized[1], MIN_SIZE.height, height - 32));
      }
    }
    const anchor = WHERE[where];
    if (anchor) {
      const rect = union(group);
      const margin = 24;
      const left = margin + anchor[0] * (width - (rect.right - rect.left) - margin * 2);
      const top = margin + anchor[1] * (height - (rect.bottom - rect.top) - margin * 2);
      applyOffset(group, group.dx + (left - rect.left), group.dy + (top - rect.top));
    } else if (where === 'padrao') {
      applyOffset(group, 0, 0);
    }
    fitIntoViewport(group);
    persist(group);
    return true;
  };

  return {
    place,
    reset() {
      groups.forEach((group) => {
        group.elements.forEach((element) => {
          ['translate', 'width', 'height', 'minWidth', 'minHeight', 'maxWidth', 'maxHeight', 'flex'].forEach((property) => { element.style[property] = ''; });
        });
        group.dx = 0;
        group.dy = 0;
        group.sized = false;
        writeStore(group.key, null);
      });
    },
    destroy() { windowRef.removeEventListener('resize', onResize); },
  };
}
