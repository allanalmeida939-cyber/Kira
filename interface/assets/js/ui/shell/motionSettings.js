// Animações escolhidas na aba Aparência (guardadas em configs/kira_settings.json): de onde os painéis surgem, a
// velocidade e se há animação. Vale para a janela principal e para as janelas extras (satellite.js).

export const ANIM_STYLES = Object.freeze(['orb', 'rise', 'slide', 'fade']);
export const ANIM_SPEEDS = Object.freeze({ instant: 0, fast: 0.7, normal: 1, smooth: 1.45, slow: 2.2, slower: 3.3, slowest: 4.4 }); // x ~450 ms: 0, 0,3 / 0,45 / 0,65 / 1 / 1,5 / 2 s
const MOTION_MODES = ['full', 'system', 'off'];

/** Põe no <html>: data-kira-motion, data-kira-anim, data-kira-anim-speed e a variável --kira-anim-speed (multiplica as durações). */
export function applyMotionSettings(settings = {}, root = globalThis.document?.documentElement) {
  if (!root) return null;
  const applied = {
    animations: MOTION_MODES.includes(settings.animations) ? settings.animations : 'full',
    anim_style: ANIM_STYLES.includes(settings.anim_style) ? settings.anim_style : 'orb',
    anim_speed: Object.hasOwn(ANIM_SPEEDS, settings.anim_speed) ? settings.anim_speed : 'normal',
  };
  root.dataset.kiraMotion = applied.animations;
  root.dataset.kiraAnim = applied.anim_style;
  root.dataset.kiraAnimSpeed = applied.anim_speed;
  root.style?.setProperty?.('--kira-anim-speed', String(ANIM_SPEEDS[applied.anim_speed]));
  return applied;
}

/** Lê as configurações do backend e aplica; sem resposta fica o padrão (orbe, normal, animações sempre). */
export async function loadMotionSettings({ fetchRef = globalThis.fetch, root = globalThis.document?.documentElement } = {}) {
  try {
    const response = await fetchRef('/api/ui/settings', { cache: 'no-store' });
    const payload = await response.json();
    let phone = null; // celular: as animações escolhidas nele valem só nele
    try { phone = globalThis.KIRA_REMOTE ? JSON.parse(globalThis.localStorage?.getItem('kira.phone.motion') || 'null') : null; } catch (_) { phone = null; }
    return applyMotionSettings({ ...(payload?.settings || {}), ...(phone || {}) }, root);
  } catch (_) {
    return applyMotionSettings({}, root);
  }
}
