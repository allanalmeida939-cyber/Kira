const DEFAULT_ALARM_MS = 60_000;
const VOLUME_KEY = 'kiraAlarmVolume'; // aba Áudio, "Avisos sonoros": 0-200 (%)

function volumeFactor(windowRef) {
  try {
    const raw = windowRef?.localStorage?.getItem(VOLUME_KEY);
    const value = raw == null ? 100 : Number(raw);
    return Number.isFinite(value) ? Math.max(0, Math.min(200, value)) / 100 : 1;
  } catch (_) {
    return 1;
  }
}

function getAudioContext(windowRef) {
  const AudioContextRef = windowRef?.AudioContext || windowRef?.webkitAudioContext;
  if (!AudioContextRef) return null;
  try { return new AudioContextRef(); } catch (_) { return null; }
}

export class AlarmSound {
  constructor({ windowRef = globalThis.window, logger = globalThis.console } = {}) {
    this.window = windowRef;
    this.logger = logger;
    this.context = null;
    this.timer = null;
    this.stopTimer = null;
  }

  ensureContext() {
    if (!this.context) this.context = getAudioContext(this.window);
    return this.context;
  }

  async resume() {
    const context = this.ensureContext();
    if (!context) return false;
    try {
      if (context.state === 'suspended') await context.resume();
      return context.state === 'running';
    } catch (error) {
      this.logger?.debug?.('[Kira] som de lembrete indisponível', error);
      return false;
    }
  }

  async beep({ frequency = 880, duration = 0.18, volume = 0.035 } = {}) {
    const level = volume * volumeFactor(this.window);
    if (level <= 0) return true; // avisos sonoros no zero: silêncio
    const context = this.ensureContext();
    if (!context || !(await this.resume())) return false;
    try {
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      const now = context.currentTime;
      oscillator.type = 'sine';
      oscillator.frequency.setValueAtTime(frequency, now);
      gain.gain.setValueAtTime(0.0001, now);
      gain.gain.exponentialRampToValueAtTime(Math.max(level, 0.001), now + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + duration);
      oscillator.connect(gain);
      gain.connect(context.destination);
      // Contexto parado fica renderizando silêncio sem parar (CPU): suspende quando o último bipe acaba.
      this.pending = (this.pending || 0) + 1;
      oscillator.onended = () => {
        this.pending -= 1;
        if (!this.pending && this.timer == null) context.suspend?.().catch?.(() => {});
      };
      oscillator.start(now);
      oscillator.stop(now + duration + 0.03);
      return true;
    } catch (error) {
      this.logger?.debug?.('[Kira] falha ao emitir som de lembrete', error);
      return false;
    }
  }

  playReminder() {
    return this.beep({ frequency: 880, duration: 0.2, volume: 0.04 });
  }

  startAlarm(maxMs = DEFAULT_ALARM_MS) {
    this.stop();
    const play = () => {
      this.beep({ frequency: 740, duration: 0.2, volume: 0.045 });
      const start = this.window?.setTimeout || setTimeout;
      start(() => this.beep({ frequency: 1040, duration: 0.22, volume: 0.045 }), 260);
    };
    play();
    const setIntervalRef = this.window?.setInterval || setInterval;
    this.timer = setIntervalRef(play, 1800);
    const setTimeoutRef = this.window?.setTimeout || setTimeout;
    this.stopTimer = setTimeoutRef(() => this.stop(), Math.min(Math.max(maxMs, 1000), DEFAULT_ALARM_MS));
  }

  stop() {
    if (this.timer != null) (this.window?.clearInterval || clearInterval)(this.timer);
    if (this.stopTimer != null) (this.window?.clearTimeout || clearTimeout)(this.stopTimer);
    this.timer = null;
    this.stopTimer = null;
  }
}

export default AlarmSound;
