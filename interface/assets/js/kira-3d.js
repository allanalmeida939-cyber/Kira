/**
 * KIRA 3D - Galaxy Renderer v13
 * A galáxia é desenhada por kira-orb-render.js (WebGL; sem WebGL, canvas 2D). Esta classe liga o desenho à página:
 * tamanho, estado, nível da voz, ponteiro, configuração da aba Aparência e as pausas (cada uma com seu motivo).
 */

const KIRA_ORB_VOICE_STATES = new Set(["listening", "thinking", "speaking"]);
const KIRA_ORB_STATES = new Set(["idle", "listening", "thinking", "speaking", "error"]);

class Kira3D {
  constructor(containerId) {
    this.container = document.getElementById(containerId);
    if (!this.container) {
      console.error("[KIRA ORB] Container nao encontrado:", containerId);
      return;
    }
    this.canvas = null;
    this.loop = null;
    this.mode = "";
    this.isActive = false;
    this.isDestroyed = false;
    this.currentState = "idle";
    this.audioLevel = 0;
    this.orbConfig = this._loadOrbConfig();
    // motivos de pausa: "user" (botão da aba Aparência), "drawer" (aba cobrindo), "appearance" (outro orbe na tela),
    // "godseye" (globo do Olho de Deus), "hidden" (janela escondida). Roda só sem nenhum.
    this.holds = new Set(document.hidden ? ["hidden"] : []);
    try {
      if (localStorage.getItem("KIRA_ORB_PAUSED") === "true") this.holds.add("user");
    } catch (_) {}
    this.reduceMotionQuery = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    this.resizeObserver = null;
    this.resizeTimers = [];
    this.handlers = {};
    this.init();
  }

  _loadOrbConfig() {
    const defaults = { ...(window.KIRA_ORB_DEFAULT_CONFIG || {}) };
    try {
      const stored = JSON.parse(localStorage.getItem("KIRA_ORB_CONFIG") || "{}");
      return { ...defaults, ...stored };
    } catch (_) {
      return defaults;
    }
  }

  saveOrbConfig() {
    try {
      localStorage.setItem("KIRA_ORB_CONFIG", JSON.stringify(this.orbConfig));
    } catch (_) {}
  }

  init() {
    try {
      this._createCanvas();
      this.loop = new window.KiraOrbLoop(this.canvas, {
        raf: (callback) => window.requestAnimationFrame(callback),
        caf: (id) => window.cancelAnimationFrame(id),
        config: this.orbConfig,
        // nível da voz da Kira medido pela página 30 vezes por segundo (index.html)
        readLevel: () => Number(window.kiraVoiceLevel ?? window.agentVoiceLevel ?? 0),
      });
      this.mode = this.loop.kind;
      this.loop.setReduced(Boolean(this.reduceMotionQuery?.matches));
      this.forceResize("init");
      this._bindEvents();
      this.loop.run(this.holds.size === 0);
      this.isActive = true;
      this.container.dataset.renderMode = "3d";
      this.container.dataset.orbRenderer = this.mode;
      this._scheduleResize();
    } catch (error) {
      console.error("[KIRA ORB] Falha:", error);
      this.destroy();
      this.container.dataset.renderMode = "fallback";
    }
  }

  _createCanvas() {
    this.container.innerHTML = "";
    this.canvas = document.createElement("canvas");
    this.canvas.className = "kira-3d-surface";
    this.canvas.setAttribute("aria-label", "Kira Galaxy");
    Object.assign(this.canvas.style, {
      display: "block",
      width: "100%",
      height: "100%",
      borderRadius: "50%",
      pointerEvents: "none",
      transform: "translateZ(0)",
      willChange: "transform",
      opacity: "1",
      visibility: "visible",
    });
    this.container.appendChild(this.canvas);
  }

  _bindEvents() {
    const onResize = () => this.forceResize("resize");
    this.handlers.resize = onResize;
    window.addEventListener("resize", onResize);
    if (typeof ResizeObserver === "function") {
      this.resizeObserver = new ResizeObserver(onResize);
      this.resizeObserver.observe(this.container);
    }
    this.handlers.pointerMove = (event) => {
      const rect = this.container.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      this.loop?.setPointer(
        (((event.clientX - rect.left) / rect.width) * 2 - 1) * 0.18,
        (((event.clientY - rect.top) / rect.height) * 2 - 1) * 0.14,
      );
    };
    this.handlers.pointerLeave = () => this.loop?.setPointer(0, 0);
    this.container.addEventListener("pointermove", this.handlers.pointerMove);
    this.container.addEventListener("pointerleave", this.handlers.pointerLeave);
    this.handlers.visibility = () => this.hold("hidden", document.hidden);
    document.addEventListener("visibilitychange", this.handlers.visibility);
    this.handlers.reduced = () => this.loop?.setReduced(Boolean(this.reduceMotionQuery?.matches));
    this.reduceMotionQuery?.addEventListener?.("change", this.handlers.reduced);
  }

  _scheduleResize() {
    this.resizeTimers.forEach((timer) => clearTimeout(timer));
    this.resizeTimers = [120, 280, 460].map((delay) => setTimeout(() => this.forceResize("startup"), delay));
    requestAnimationFrame(() => this.forceResize("startup"));
  }

  /** Mede o lugar do orbe e repassa; false enquanto ele ainda não tem tamanho. */
  forceResize(reason = "manual") {
    void reason;
    if (!this.container || !this.loop || this.isDestroyed) return false;
    const rect = this.container.getBoundingClientRect?.() || { width: 0, height: 0 };
    const width = Math.round(rect.width || this.container.clientWidth || 0);
    const height = Math.round(rect.height || this.container.clientHeight || 0);
    return this.loop.resize(width, height, Math.min(window.devicePixelRatio || 1, 2));
  }

  /** Pausa por um motivo; volta a rodar quando nenhum motivo sobra. */
  hold(reason, on) {
    if (on) this.holds.add(reason);
    else this.holds.delete(reason);
    this.loop?.run(this.holds.size === 0);
  }

  setState(newState) {
    let state = KIRA_ORB_STATES.has(newState) ? newState : "idle";
    if (window._kiraOrbVoiceAnimEnabled === false && KIRA_ORB_VOICE_STATES.has(state)) state = "idle";
    this.currentState = state;
    this.loop?.setState(state);
  }

  setAudioLevel(level) {
    const value = Math.max(0, Math.min(1, Number(level || 0)));
    this.audioLevel = value;
    this.loop?.boost(value);
  }

  syncFromSphereState(sphereState) {
    if (window._kiraOrbVoiceAnimEnabled === false) {
      this.setState("idle");
      this.setAudioLevel(0);
      return;
    }
    const mode = String(sphereState?.mode || "").toLowerCase();
    if (mode === "speaking") this.setState("speaking");
    else if (mode === "thinking" || mode === "processing" || mode === "connecting") this.setState("thinking");
    else if (mode === "listening") this.setState("listening");
    else if (mode === "error") this.setState("error");
    else this.setState("idle");
    this.setAudioLevel(sphereState?.voiceLevel || window.kiraVoiceLevel || 0);
  }

  /** Aba Aparência: muda a configuração ao vivo (patch: {chave: valor}). */
  updateConfig(patch) {
    Object.assign(this.orbConfig, patch || {});
    this.loop?.configure({ ...this.orbConfig });
  }

  destroy() {
    this.isDestroyed = true;
    this.isActive = false;
    this.loop?.run(false);
    this.loop = null;
    this.resizeTimers.forEach((timer) => clearTimeout(timer));
    this.resizeTimers = [];
    this.resizeObserver?.disconnect();
    if (this.handlers.resize) window.removeEventListener("resize", this.handlers.resize);
    this.container?.removeEventListener("pointermove", this.handlers.pointerMove);
    this.container?.removeEventListener("pointerleave", this.handlers.pointerLeave);
    if (this.handlers.visibility) document.removeEventListener("visibilitychange", this.handlers.visibility);
    this.reduceMotionQuery?.removeEventListener?.("change", this.handlers.reduced);
    if (this.canvas?.parentNode === this.container) this.canvas.parentNode.removeChild(this.canvas);
    this.canvas = null;
    if (this.container) this.container.dataset.renderMode = "fallback";
  }
}

window.Kira3D = Kira3D;
