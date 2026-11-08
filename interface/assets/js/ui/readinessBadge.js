function text(value) {
  return String(value ?? '').trim().toLowerCase();
}

export function getReadinessBadgeState(snapshot = {}) {
  const phase = text(snapshot.phase || snapshot.boot_state || snapshot.status);
  const uiStatus = text(snapshot.ui_status);
  const failure = text(snapshot.failure_reason || snapshot.error);
  const ready = snapshot.functional_ready === true
    || snapshot.ui_ready === true
    || uiStatus === 'session_ready'
    || phase === 'functional_ready'
    || phase === 'ui_ready';

  if (snapshot.recovery_active === true) return { visible: true, state: 'recovering', label: 'Reconectando a Kira…' };
  if (snapshot.agent_process_alive === false) {
    return { visible: true, state: 'error', label: 'Kira indisponível. Clique para reiniciar a sessão', action: 'restart' };
  }
  if (ready) return { visible: true, state: 'ready', label: 'Kira pronta' };
  if (failure || phase === 'failed' || phase === 'error' || phase === 'critical') {
    return { visible: true, state: 'error', label: 'Kira indisponível' };
  }

  const waiting = phase && !['minimal_ready', 'standby', 'idle'].includes(phase);
  return {
    visible: Boolean(waiting),
    state: 'starting',
    label: 'Kira iniciando…',
  };
}

export function createReadinessBadgeController(badge, { onRestart, holdMs = 3000, fadeMs = 400, setTimer = globalThis.setTimeout, clearTimer = globalThis.clearTimeout } = {}) {
  let wasReady = false;
  let timers = [];
  const cancel = () => { timers.forEach((timer) => clearTimer(timer)); timers = []; };
  let restartable = false;
  badge.addEventListener?.('click', () => { if (restartable) onRestart?.(); });
  return {
    sync(snapshot) {
      const next = getReadinessBadgeState(snapshot);
      if (next.state === 'ready') {
        if (wasReady) return;
        wasReady = true;
        restartable = false;
        cancel();
        badge.classList.remove('is-fading');
        badge.dataset.state = 'ready';
        badge.textContent = next.label;
        badge.hidden = false;
        timers.push(setTimer(() => badge.classList.add('is-fading'), holdMs));
        timers.push(setTimer(() => { badge.hidden = true; badge.classList.remove('is-fading'); }, holdMs + fadeMs));
        return;
      }
      wasReady = false;
      restartable = next.action === 'restart';
      if (badge.style) badge.style.cursor = restartable ? 'pointer' : '';
      cancel();
      badge.classList.remove('is-fading');
      badge.hidden = !next.visible;
      badge.dataset.state = next.state;
      badge.textContent = next.label;
    },
    destroy: cancel,
  };
}
