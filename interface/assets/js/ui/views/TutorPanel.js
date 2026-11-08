// Painel Estudos (Fase 3): download com progresso real, simulado (respostas salvas, cronômetro, Finalizar),
// correção com justificativa por alternativa e painel de progresso por tema. Fala só com /api/tutor/*.
import { clear, create, safeText } from './panelDom.js';

const post = (win, action, body) => win.fetch(`/api/tutor/${action}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  .then((r) => r.json()).catch(() => ({ ok: false }));

export function createTutorPanel({ windowRef = globalThis.window } = {}) {
  return {
    mount(container, context) {
      const doc = container.ownerDocument;
      const el = (tag, cls, text) => create(doc, tag, cls, text);
      const btn = (label, fn) => { const b = el('button', 'kui-panel-button', label); b.type = 'button'; b.addEventListener('click', fn); return b; };
      let timer = 0;
      const render = (props = {}) => {
        clearInterval(timer);
        clear(container);
        container.classList.add('kui-tutor');
        const mode = props.mode;
        if (mode === 'job') {
          const j = props.job || {};
          const pct = j.total ? Math.min(100, (100 * j.value) / j.total) : j.state === 'done' ? 100 : 0;
          const fill = el('span', 'kui-bar-fill');
          fill.style.width = `${pct}%`;
          const track = el('span', 'kui-bar-track');
          track.append(fill);
          const size = j.stage === 'baixando' ? `${(j.value / 1e6).toFixed(1)}${j.total ? ` de ${(j.total / 1e6).toFixed(1)}` : ''} MB` : j.total ? `${j.value} de ${j.total}` : '';
          container.append(el('h3', '', `Biblioteca · ${safeText(j.course)}`), el('p', 'kui-tutor-muted', safeText(j.source).slice(0, 90)), track,
            el('p', '', j.state === 'failed' ? `Falhou: ${safeText(j.text)}` : j.state === 'cancelled' ? 'Cancelado' : `${safeText(j.stage)} ${size} ${safeText(j.text)}`));
          if (j.state === 'running') container.append(btn('Cancelar', () => post(windowRef, 'cancelar', { job: j.id })));
          return;
        }
        if (mode === 'card') {
          if (/^(https:\/\/upload\.wikimedia\.org\/|\/api\/design\/file\/[\w.-]+$)/.test(props.imagem?.url || '')) {
            const img = el('img', 'kui-tutor-img');
            img.src = props.imagem.url;
            img.alt = safeText(props.titulo);
            container.append(img, el('p', 'kui-tutor-muted', safeText(props.imagem.credito)));
          }
          for (const line of safeText(props.texto).split('\n')) {
            const check = line.match(/^\s*-\s*\[( |x)\]\s*(.*)/i);
            if (check) {
              const row = el('label', 'kui-tutor-opt');
              const box = el('input');
              Object.assign(box, { type: 'checkbox', checked: check[1].toLowerCase() === 'x' });
              row.append(box, el('span', '', check[2]));
              container.append(row);
            } else if (line.trim()) container.append(el('p', line.startsWith('#') ? 'kui-tutor-stem' : '', line.replace(/^#+\s*/, '')));
          }
          return;
        }
        if (mode === 'dashboard') {
          if (props.load) { windowRef.fetch('/api/tutor/dados').then((r) => r.json()).then((d) => render({ mode: 'dashboard', ...d })).catch(() => {}); }
          container.append(el('h3', '', 'Estudos'));
          if (props.error) container.append(el('p', 'kui-tutor-warn', safeText(props.error)));
          // soltar PDF: vai para a biblioteca do curso escrito aqui
          const course = el('input', 'kui-tutor-text');
          Object.assign(course, { placeholder: 'Curso (ex.: PMCE, inglês, ENEM)', value: (props.cursos || [])[0]?.curso || '' });
          course.style.minHeight = '0';
          const drop = el('label', 'kui-tutor-drop', 'Solte PDFs aqui ou clique para escolher');
          const file = el('input');
          Object.assign(file, { type: 'file', accept: '.pdf,.docx,.txt,.md', multiple: true, hidden: true });
          const send = (files) => [...files].forEach((f) => {
            const reader = new FileReader();
            reader.onload = () => post(windowRef, 'enviar', { curso: course.value || 'geral', nome: f.name, dados: String(reader.result).split(',')[1] });
            reader.readAsDataURL(f);
          });
          file.addEventListener('change', () => send(file.files));
          drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('is-over'); });
          drop.addEventListener('dragleave', () => drop.classList.remove('is-over'));
          drop.addEventListener('drop', (e) => { e.preventDefault(); drop.classList.remove('is-over'); send(e.dataTransfer.files); });
          drop.append(file);
          container.append(course, drop);
          const list = props.cursos || [];
          if (!list.length) container.append(el('p', 'kui-tutor-muted', 'Nenhum curso ainda. Peça: "Kira, baixa o edital de ..." ou "faz um simulado de ...".'));
          for (const c of list) {
            container.append(el('h4', '', c.curso));
            if (c.proxima) container.append(el('p', 'kui-tutor-next', `Próxima aula sugerida: ${c.proxima} (onde você mais erra)`));
            if (c.media != null) {
              container.append(el('p', 'kui-tutor-score', `Média ${String(c.media).replace('.', ',')}`));
              const bars = el('div', 'kui-tutor-bars'); // evolução das notas
              c.evolucao.slice(-12).forEach((n) => { const b = el('i'); b.style.height = `${Math.max(4, n * 10)}%`; b.title = String(n); bars.append(b); });
              container.append(bars);
            }
            for (const t of c.temas) container.append(el('p', t.pct < 60 ? 'kui-tutor-warn' : '', `${t.tema}: ${t.acertos}/${t.total} (${t.pct}%)`));
            for (const q of c.simulados || []) {
              container.append(btn(`${q.data} · ${q.tema} · ${q.nota == null ? 'em andamento' : `nota ${String(q.nota).replace('.', ',')}`}`, async () => {
                try {
                  const response = await windowRef.fetch(`/api/tutor/quiz/${q.id}`);
                  const r = await response.json().catch(() => ({}));
                  if (!response.ok || !r.quiz) throw new Error(r?.detail?.message || r?.error || 'Simulado não encontrado.');
                  render({ mode: r.quiz.resultado ? 'result' : 'quiz', quiz: r.quiz });
                } catch (error) {
                  render({ mode: 'dashboard', ...props, error: error?.message || 'Não consegui abrir esse simulado.' });
                }
              }));
            }
            container.append(el('p', 'kui-tutor-muted', `${c.documentos.length} documento(s) na biblioteca${c.aulas.length ? ` · últimas aulas: ${c.aulas.join(', ')}` : ''}`));
          }
          return;
        }
        const quiz = props.quiz || {};
        const result = mode === 'result' ? quiz.resultado : null;
        const answers = { ...(quiz.respostas || {}) };
        let saveWait = 0;
        const save = () => { clearTimeout(saveWait); saveWait = setTimeout(() => post(windowRef, 'responder', { id: quiz.id, respostas: answers }), 600); };
        const head = el('div', 'kui-tutor-head');
        head.append(el('h3', '', `${result ? 'Correção' : 'Simulado'} · ${safeText(quiz.tema)}`));
        if (result) head.append(el('strong', 'kui-tutor-score', `Nota ${String(result.nota).replace('.', ',')} · ${result.acertos}/${result.total}`));
        else {
          const clock = el('span', 'kui-tutor-muted', '');
          const start = Date.now();
          head.append(btn('Cronômetro', () => { clearInterval(timer); timer = setInterval(() => { const s = Math.floor((Date.now() - start) / 1000); clock.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }, 1000); }), clock);
        }
        container.append(head);
        if (quiz.sem_fonte) container.append(el('p', 'kui-tutor-warn', 'Sem fonte na biblioteca: questões de conhecimento geral, confira.'));
        (quiz.questoes || []).forEach((q, i) => {
          const box = el('div', 'kui-tutor-q');
          const item = result?.items?.[i];
          if (item) box.classList.add(item.certa ? 'is-right' : 'is-wrong');
          box.append(el('p', 'kui-tutor-stem', `${i + 1}. ${safeText(q.enunciado)}`));
          const opts = q.tipo === 'vf' ? ['V) Verdadeiro', 'F) Falso'] : q.alternativas || [];
          if (q.tipo === 'discursiva') {
            const area = el('textarea', 'kui-tutor-text');
            area.value = answers[i] || '';
            area.disabled = Boolean(result);
            area.addEventListener('input', () => { answers[i] = area.value; save(); });
            box.append(area);
          } else {
            opts.forEach((opt) => {
              const letter = safeText(opt).trim()[0]?.toUpperCase();
              const row = el('label', 'kui-tutor-opt');
              const radio = el('input');
              Object.assign(radio, { type: 'radio', name: `q${quiz.id}-${i}`, checked: answers[i] === letter, disabled: Boolean(result) });
              radio.addEventListener('change', () => { answers[i] = letter; save(); });
              row.append(radio, el('span', '', safeText(opt)));
              if (result && letter === safeText(q.gabarito).trim()[0]?.toUpperCase()) row.classList.add('is-key');
              box.append(row);
            });
          }
          if (item) {
            box.append(el('p', 'kui-tutor-muted', `Gabarito: ${safeText(item.gabarito)} · sua: ${safeText(item.resposta) || 'em branco'}`));
            for (const [k, v] of Object.entries(item.justificativas || {})) box.append(el('p', 'kui-tutor-why', `${k}: ${safeText(v)}`));
            if (item.comentario) box.append(el('p', 'kui-tutor-why', `Rubrica ${Math.round((item.nota || 0) * 100)}%: ${safeText(item.comentario)}`));
            if (item.revisao_humana) box.append(el('p', 'kui-tutor-warn', 'Discursiva corrigida pela IA: vale revisão de um professor.'));
            if (item.fonte) box.append(el('p', 'kui-tutor-muted', `Fonte: ${safeText(item.fonte)}`));
          }
          container.append(box);
        });
        const actions = el('div', 'kui-tutor-actions');
        const status = el('span', 'kui-tutor-muted', '');
        if (!result) actions.append(btn('Finalizar', async () => { status.textContent = 'Corrigindo…'; await post(windowRef, 'finalizar', { id: quiz.id, respostas: answers }); }));
        actions.append(btn('Exportar PDF + gabarito', async () => {
          status.textContent = 'Gerando…';
          const r = await post(windowRef, 'exportar', { id: quiz.id });
          status.textContent = r.ok ? `Salvo: ${r.arquivos.map((f) => f.split(/[\\/]/).pop()).join(' e ')}` : 'Não consegui exportar';
        }), status);
        container.append(actions);
      };
      render(context.props);
      container.__kuiTutor = render;
      return () => { clearInterval(timer); delete container.__kuiTutor; };
    },
    update(container, props) { container.__kuiTutor?.(props); },
  };
}
