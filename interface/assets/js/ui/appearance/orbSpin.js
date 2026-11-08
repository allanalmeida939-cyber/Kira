/* Orbe girável com o mouse.
   - Orbes 3D (pontos calculados em 3D): o arraste gira a câmera de verdade — esquerda/direita dá a volta inteira
     (frente, lado, trás), cima/baixo mostra o topo e a base. Escrito em globalThis.kiraOrbView, que os desenhos leem.
   - Orbes planos (Galáxia, Círculo, Buraco negro, Plasma, Anéis): inclinação 3D por CSS + giro no próprio eixo.
   Solto, segue com inércia e assenta devagar. O laço de quadros só roda enquanto há movimento. */
// 'globo' é uma esfera WebGL de verdade (orb3d.js gira a malha com kiraOrbView); os demais 3D giram os pontos no canvas.
// Em todos eles o arraste NÃO aplica transform de CSS (isso é que fazia "arrastar uma foto").
const ORBS_3D = new Set(['globo', 'particles', 'constellation', 'hive', 'torus', 'ribbons', 'fusion']);
const BASE = 'translateY(-6px)';

const view = (globalThis.kiraOrbView = { yaw: 0, pitch: 0, moving: false });

function attach(wrapper) {
  if (!wrapper || wrapper.__kiraSpin) return;
  wrapper.__kiraSpin = true;
  wrapper.style.touchAction = 'none';
  let vYaw = 0; let vPitch = 0; let drag = null; let raf = 0; let last = 0;
  let cssSpin = 0; let cssTiltX = 0; let cssTiltY = 0;
  const is3d = () => ORBS_3D.has(document.documentElement.dataset.kiraOrb || 'galaxy');
  const reduce = () => globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

  const paintCss = () => {
    if (is3d() || (!cssSpin && !cssTiltX && !cssTiltY)) { wrapper.style.transform = ''; return; }
    wrapper.style.transform = `${BASE} perspective(800px) rotateX(${cssTiltX.toFixed(2)}deg) rotateY(${cssTiltY.toFixed(2)}deg) rotateZ(${cssSpin.toFixed(2)}deg)`;
  };

  const frame = (now) => {
    raf = 0;
    const dt = Math.min(0.05, (now - (last || now)) / 1000); last = now;
    if (!drag) {
      view.yaw += vYaw * dt; view.pitch += vPitch * dt;
      const k = Math.pow(0.08, dt); vYaw *= k; vPitch *= k; // atrito
      if (Math.abs(vYaw) < 0.4) { const home = Math.round(view.yaw / (Math.PI * 2)) * Math.PI * 2; view.yaw += (home - view.yaw) * Math.min(1, dt * 2.5); }
      if (Math.abs(vPitch) < 0.4) view.pitch += -view.pitch * Math.min(1, dt * 2.5);
      view.pitch = Math.max(-1.5, Math.min(1.5, view.pitch));
      cssSpin = (view.yaw * 180) / Math.PI; cssTiltX = (-view.pitch * 180) / Math.PI * 0.4; cssTiltY = 0;
    }
    paintCss();
    const settled = !drag && Math.abs(vYaw) < 0.02 && Math.abs(vPitch) < 0.02
      && Math.abs(view.yaw - Math.round(view.yaw / (Math.PI * 2)) * Math.PI * 2) < 0.002 && Math.abs(view.pitch) < 0.002;
    if (settled) { view.yaw = 0; view.pitch = 0; view.moving = false; cssSpin = cssTiltX = cssTiltY = 0; paintCss(); return; }
    raf = requestAnimationFrame(frame);
  };
  const kick = () => { view.moving = true; if (!raf) { last = 0; raf = requestAnimationFrame(frame); } };

  wrapper.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    drag = { x: event.clientX, y: event.clientY, lx: event.clientX, ly: event.clientY, t: performance.now(), moved: false, id: event.pointerId };
  });
  wrapper.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    if (!drag.moved) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
      drag.moved = true;
      try { wrapper.setPointerCapture(event.pointerId); } catch (_) { /* sem captura */ }
    }
    const now = performance.now(); const dt = Math.max(0.008, (now - drag.t) / 1000);
    const dx = event.clientX - drag.lx; const dy = event.clientY - drag.ly;
    const dYaw = dx * 0.011; const dPitch = dy * 0.011;
    view.yaw += dYaw; view.pitch = Math.max(-1.5, Math.min(1.5, view.pitch + dPitch));
    vYaw = dYaw / dt; vPitch = dPitch / dt;
    cssSpin = (view.yaw * 180) / Math.PI;
    cssTiltY = Math.max(-35, Math.min(35, (event.clientX - drag.x) * 0.15));
    cssTiltX = Math.max(-35, Math.min(35, -(event.clientY - drag.y) * 0.15));
    drag.lx = event.clientX; drag.ly = event.clientY; drag.t = now;
    kick();
  });
  const end = (event) => {
    if (!drag || (event && event.pointerId !== drag.id)) return;
    const moved = drag.moved; drag = null;
    if (!moved) return;
    if (reduce()) { vYaw = 0; vPitch = 0; }
    vYaw = Math.max(-25, Math.min(25, vYaw)); vPitch = Math.max(-10, Math.min(10, vPitch));
    kick();
    wrapper.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); }, { capture: true, once: true });
  };
  wrapper.addEventListener('pointerup', end);
  wrapper.addEventListener('pointercancel', end);
}

const boot = () => attach(document.querySelector('.orb-wrapper'));
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true }); else boot();
new MutationObserver(() => { const w = document.querySelector('.orb-wrapper'); if (w && !w.__kiraSpin) attach(w); })
  .observe(document.body || document.documentElement, { childList: true });
