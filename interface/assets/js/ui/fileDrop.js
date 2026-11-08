// Soltar arquivo em qualquer lugar da tela: PDF, imagem, texto ou ZIP vira conhecimento da Kira; SKILL.md (ou ZIP com
// SKILL.md, o servidor responde skill=true) vai para o instalador de skills. Véu só com opacity/transform (GPU fraca).
const CSS = `.kira-drop-veil{position:fixed;inset:0;z-index:100000;display:grid;place-items:center;pointer-events:none;opacity:0;
background:rgba(8,4,20,.72);transition:opacity .15s ease}.kira-drop-veil.on{opacity:1}
.kira-drop-veil>div{padding:26px 40px;border:2px dashed rgba(192,132,252,.85);border-radius:22px;text-align:center;color:#f5f3ff;
transform:scale(.94);transition:transform .15s ease;font:600 18px system-ui}.kira-drop-veil.on>div{transform:none}
.kira-drop-veil span{display:block;margin-top:6px;font-weight:400;font-size:13px;color:#c4b5fd}
.kira-drop-note{position:fixed;left:50%;bottom:110px;z-index:100000;transform:translateX(-50%);padding:10px 18px;border-radius:14px;
background:rgba(20,12,40,.95);border:1px solid rgba(192,132,252,.5);color:#f5f3ff;font:500 13px system-ui;transition:opacity .3s}`;

export function installFileDrop(doc = document, win = window) {
  const style = doc.createElement('style');
  style.textContent = CSS;
  const veil = doc.createElement('div');
  veil.className = 'kira-drop-veil';
  veil.innerHTML = '<div>Solte aqui<span>PDF, imagem, ZIP ou skill — a Kira lê, identifica e guarda</span></div>';
  doc.head.append(style);
  doc.body.append(veil);
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const note = (text) => {
    const box = doc.createElement('div');
    box.className = 'kira-drop-note';
    box.textContent = text;
    doc.body.append(box);
    win.setTimeout(() => { box.style.opacity = '0'; win.setTimeout(() => box.remove(), 400); }, 3500);
  };
  const post = async (url, file) => {
    const form = new FormData();
    form.append('file', file);
    const r = await win.fetch(url, { method: 'POST', body: form });
    return { status: r.status, ...(await r.json().catch(() => ({}))) };
  };
  const send = async (file) => {
    try {
      let r = /^skill\.md$/i.test(file.name) ? { skill: true } : await post('/api/knowledge/drop', file);
      if (r.skill) {
        r = await post('/api/skills/install', file);
        note(r.status === 404 ? 'Instalador de skills indisponível.' : r.ok ? `Skill instalada: ${r.name || file.name}` : `Skill não instalada: ${r.detail || r.error || 'erro'}`);
        return;
      }
      note(r.ok ? `Lendo ${file.name}… a Kira avisa quando guardar.` : `Não deu para enviar ${file.name}: ${r.detail || 'erro'}`);
    } catch (_) { note(`Não deu para enviar ${file.name}.`); }
  };
  win.addEventListener('dragenter', (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth += 1; veil.classList.add('on'); });
  win.addEventListener('dragover', (e) => { if (!hasFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
  win.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) veil.classList.remove('on'); });
  win.addEventListener('drop', (e) => {
    depth = 0;
    veil.classList.remove('on');
    if (!hasFiles(e)) return;
    const handled = e.defaultPrevented; // área de soltar do painel Estudos já cuidou
    e.preventDefault(); // sem isso o navegador abre o arquivo no lugar da Kira
    if (!handled) [...e.dataTransfer.files].slice(0, 10).forEach(send);
  });
  return veil;
}

if (globalThis.document?.body && !globalThis.window?.kiraFileDrop) globalThis.window.kiraFileDrop = installFileDrop();
else if (globalThis.document && !globalThis.window?.kiraFileDrop) {
  globalThis.document.addEventListener('DOMContentLoaded', () => { globalThis.window.kiraFileDrop ||= installFileDrop(); }, { once: true });
}
