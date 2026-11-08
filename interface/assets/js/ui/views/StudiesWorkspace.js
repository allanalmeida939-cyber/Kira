/* Área principal de Estudos: dados reais da API educacional, sem valores de demonstração. */

// Enxuto: cada seção faz uma coisa (Plano junta matérias + cronograma; Simulados junta TDs; Desempenho junta histórico).
const NAV = [
  ['overview', 'Painel', '🏠', '#c084fc'],
  ['courses', 'Cursos', '🎯', '#f472b6'],
  ['plan', 'Plano', '🗺️', '#34d399'],
  ['lessons', 'Aulas', '👩‍🏫', '#22d3ee'],
  ['materials', 'Fontes', '📚', '#60a5fa'],
  ['quizzes', 'Simulados', '📝', '#fbbf24'],
  ['reviews', 'Revisões', '🔁', '#a78bfa'],
  ['performance', 'Desempenho', '📈', '#fb7185'],
];

const esc = (value, fallback = '—') => {
  const text = String(value == null ? '' : value).trim();
  return text || fallback;
};

const number = (value, digits = 0) => {
  if (value == null || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
};

const percent = (value) => value == null ? 'Sem dados' : `${number(value, 1)}%`;

const dateLabel = (value) => {
  if (!value) return 'Sem data';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short', year: 'numeric' });
};

function node(documentRef, tag, className = '', content = '') {
  const element = documentRef.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined && content !== null) element.textContent = String(content);
  return element;
}

function button(documentRef, label, attrs = {}) {
  const element = node(documentRef, 'button', attrs.className || 'kira-studies-button', label);
  element.type = 'button';
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'className' || value == null) return;
    element.setAttribute(key, String(value));
  });
  return element;
}

function field(documentRef, label, type, name, value = '', attrs = {}) {
  const wrap = node(documentRef, 'label', 'kira-studies-field');
  wrap.append(node(documentRef, 'span', 'kira-studies-label', label));
  const input = node(documentRef, type === 'textarea' ? 'textarea' : 'input', 'kira-studies-input');
  if (type !== 'textarea') input.type = type;
  input.name = name;
  if (value !== undefined && value !== null) input.value = value;
  Object.entries(attrs).forEach(([key, attrValue]) => {
    if (key === 'className' || attrValue == null) return;
    input.setAttribute(key, String(attrValue));
  });
  wrap.append(input);
  return wrap;
}

function card(documentRef, label, value, detail = '') {
  const element = node(documentRef, 'article', 'kira-studies-metric');
  element.append(node(documentRef, 'span', 'kira-studies-metric-label', label), node(documentRef, 'strong', 'kira-studies-metric-value', value));
  if (detail) element.append(node(documentRef, 'span', 'kira-studies-metric-detail', detail));
  return element;
}

function empty(documentRef, title, text, actionLabel, action) {
  const element = node(documentRef, 'div', 'kira-studies-empty');
  element.append(node(documentRef, 'h3', '', title), node(documentRef, 'p', '', text));
  if (actionLabel && action) element.append(button(documentRef, actionLabel, { 'data-tab': action, className: 'kira-studies-button is-primary' }));
  return element;
}

export function createStudiesWorkspace({ documentRef = globalThis.document, windowRef = globalThis.window } = {}) {
  const state = { tab: 'overview', data: null, courseId: null, quiz: null, lesson: null, session: null, busy: false, error: '', notice: '' };
  const root = node(documentRef, 'section', 'kira-studies-workspace');
  root.id = 'kira-studies-workspace';
  root.hidden = true;
  root.setAttribute('aria-label', 'Área de Estudos da Kira');

  const api = async (path, options = {}) => {
    const config = { cache: 'no-store', ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } };
    const response = await windowRef.fetch(path, config);
    let body = {};
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok || body.ok === false) throw new Error(body.detail || body.error || `HTTP ${response.status}`);
    return body;
  };

  const selectedCourse = () => {
    const data = state.data;
    return data?.detail?.course || data?.courses?.find((course) => course.id === data.selected_course_id) || null;
  };

  const setNotice = (text, isError = false) => {
    state.notice = text || '';
    state.error = isError ? text : '';
  };

  const refresh = async (courseId = state.courseId) => {
    state.busy = true;
    render();
    try {
      const suffix = courseId ? `?course_id=${encodeURIComponent(courseId)}` : '';
      state.data = await api(`/api/education/workspace${suffix}`);
      state.courseId = state.data.selected_course_id || null;
      state.error = '';
    } catch (error) {
      state.error = error?.message || 'Não consegui carregar Estudos.';
    } finally {
      state.busy = false;
      render();
    }
  };

  const header = () => {
    const bar = node(documentRef, 'header', 'kira-studies-header');
    const identity = node(documentRef, 'div', 'kira-studies-identity');
    identity.append(node(documentRef, 'span', 'kira-studies-kicker', 'KIRA // NÚCLEO PEDAGÓGICO'), node(documentRef, 'h1', '', 'Estudos'));
    const courseSelect = node(documentRef, 'select', 'kira-studies-course-select');
    courseSelect.setAttribute('aria-label', 'Curso selecionado');
    courseSelect.append(node(documentRef, 'option', '', state.data?.courses?.length ? 'Selecionar curso' : 'Nenhum curso criado'));
    (state.data?.courses || []).forEach((course) => {
      const option = node(documentRef, 'option', '', course.name);
      option.value = course.id;
      option.selected = Number(course.id) === Number(state.courseId);
      courseSelect.append(option);
    });
    courseSelect.addEventListener('change', () => { state.courseId = Number(courseSelect.value) || null; state.tab = 'overview'; state.quiz = null; refresh(state.courseId); });
    const actions = node(documentRef, 'div', 'kira-studies-header-actions');
    actions.append(courseSelect, button(documentRef, '⟳', { 'data-action': 'refresh', className: 'kira-studies-button is-icon', title: 'Atualizar', 'aria-label': 'Atualizar' }));
    bar.append(identity, actions);
    return bar;
  };

  const navigation = () => {
    const nav = node(documentRef, 'nav', 'kira-studies-nav');
    nav.setAttribute('aria-label', 'Seções de Estudos');
    NAV.forEach(([id, label, emoji, color]) => {
      const item = button(documentRef, '', { 'data-tab': id, className: `kira-studies-nav-button${state.tab === id ? ' is-active' : ''}` });
      item.style.setProperty('--tab', color);
      item.append(node(documentRef, 'span', 'kira-studies-nav-emoji', emoji), node(documentRef, 'span', 'kira-studies-nav-label', label));
      item.setAttribute('aria-current', state.tab === id ? 'page' : 'false');
      nav.append(item);
    });
    return nav;
  };

  const sectionTitle = (title, subtitle = '') => {
    const heading = node(documentRef, 'div', 'kira-studies-section-heading');
    heading.append(node(documentRef, 'h2', '', title));
    if (subtitle) heading.append(node(documentRef, 'p', '', subtitle));
    return heading;
  };

  const renderOverview = (content) => {
    const data = state.data || {};
    const summary = data.summary || {};
    content.append(sectionTitle('Visão geral', selectedCourse() ? `Dados persistidos de ${selectedCourse().name}.` : 'Comece criando um curso ou objetivo.'));
    const metrics = node(documentRef, 'div', 'kira-studies-metrics');
    metrics.append(
      card(documentRef, 'Cursos ativos', number(summary.courses)),
      card(documentRef, 'Horas estudadas', number(summary.study_hours, 1), 'sessões encerradas'),
      card(documentRef, 'Aulas concluídas', number(summary.lessons_completed)),
      card(documentRef, 'Simulados concluídos', number(summary.quizzes_completed)),
      card(documentRef, 'Média', summary.average_score == null ? 'Sem dados' : number(summary.average_score, 1), 'de 10'),
      card(documentRef, 'Acurácia', percent(summary.accuracy)),
      card(documentRef, 'Sequência', `${number(summary.streak_days)} dia(s)`),
      card(documentRef, 'Revisões para hoje', number(summary.reviews_due)),
    );
    content.append(metrics);
    if (!data.courses?.length) {
      content.append(empty(documentRef, 'Seu histórico começa aqui', 'Nenhum curso, disciplina ou nota foi criado ainda. Os indicadores aparecerão depois de registros reais.', 'Criar curso', 'courses'));
      return;
    }
    const grid = node(documentRef, 'div', 'kira-studies-dashboard-grid');
    const subjectPanel = node(documentRef, 'article', 'kira-studies-panel');
    subjectPanel.append(node(documentRef, 'h3', '', 'Domínio por disciplina'));
    const subjects = data.detail?.subjects || [];
    if (!subjects.length) subjectPanel.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhuma disciplina cadastrada. Crie as disciplinas do edital ou do idioma para acompanhar domínio.'));
    subjects.forEach((subject) => {
      const row = node(documentRef, 'div', 'kira-studies-progress-row');
      const label = node(documentRef, 'div', 'kira-studies-progress-label');
      label.append(node(documentRef, 'span', '', subject.name), node(documentRef, 'strong', '', percent(subject.mastery)));
      const track = node(documentRef, 'div', 'kira-studies-progress-track');
      const fill = node(documentRef, 'span', 'kira-studies-progress-fill');
      fill.style.width = `${Math.max(0, Math.min(100, Number(subject.mastery) || 0))}%`;
      track.append(fill); row.append(label, track); subjectPanel.append(row);
    });
    const activityPanel = node(documentRef, 'article', 'kira-studies-panel');
    activityPanel.append(node(documentRef, 'h3', '', 'Próximas atividades'));
    const activities = (data.detail?.tasks || []).filter((task) => task.status === 'pending').slice(0, 6);
    const reviews = (data.detail?.reviews || []).filter((review) => !review.completed_at).slice(0, 4);
    if (!activities.length && !reviews.length) activityPanel.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhuma atividade ou revisão agendada. Gere um plano para começar.'));
    activities.forEach((task) => activityPanel.append(node(documentRef, 'div', 'kira-studies-list-row', `${task.title} · ${dateLabel(task.due_at)}`)));
    reviews.forEach((review) => activityPanel.append(node(documentRef, 'div', 'kira-studies-list-row is-review', `Revisar ${review.topic} · ${dateLabel(review.scheduled_at)}`)));
    grid.append(subjectPanel, activityPanel);
    const notes = node(documentRef, 'article', 'kira-studies-panel kira-studies-data-note');
    const course = selectedCourse();
    notes.append(node(documentRef, 'h3', '', 'O que falta registrar'), node(documentRef, 'p', '', course?.exam_date ? `Data da prova: ${dateLabel(course.exam_date)} (${summary.exam_days ?? '—'} dia(s)).` : 'Data da prova ainda não informada.'), node(documentRef, 'p', '', course?.daily_goal_minutes ? `Meta diária: ${number(course.daily_goal_minutes)} minutos.` : 'Meta diária ainda não configurada.'));
    grid.append(notes); content.append(grid);
  };

  const renderCourses = (content) => {
    content.append(sectionTitle('Meus cursos e concursos', 'Cadastre o objetivo real; disciplinas e materiais serão associados a ele.'));
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'course';
    form.append(field(documentRef, 'Nome', 'text', 'name', '', { required: 'required', placeholder: 'PMCE, Inglês, Cálculo 1...' }));
    form.append(field(documentRef, 'Objetivo', 'text', 'target', '', { placeholder: 'Cargo, nível ou resultado desejado' }));
    form.append(field(documentRef, 'Tipo', 'select', 'kind', 'curso')); // trocado abaixo por select real
    const kind = form.querySelector('input[name="kind"]');
    if (kind) {
      const select = node(documentRef, 'select', 'kira-studies-input'); select.name = 'kind';
      [['curso', 'Curso'], ['concurso', 'Concurso'], ['idioma', 'Idioma'], ['faculdade', 'Faculdade'], ['outro', 'Outro']].forEach(([value, label]) => { const option = node(documentRef, 'option', '', label); option.value = value; select.append(option); });
      kind.parentElement.replaceChild(select, kind);
    }
    form.append(field(documentRef, 'Banca / instituição', 'text', 'organization', '', { placeholder: 'Opcional' }), field(documentRef, 'Nível / CEFR', 'text', 'level', '', { placeholder: 'A1, A2, B1... ou nível atual' }), field(documentRef, 'Data da prova', 'date', 'exam_date'), field(documentRef, 'Meta diária (minutos)', 'number', 'daily_goal_minutes', '', { min: '0', max: '1440' }));
    form.append(button(documentRef, 'Salvar curso', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form);
    const list = node(documentRef, 'div', 'kira-studies-course-grid');
    (state.data?.courses || []).forEach((course) => {
      const item = node(documentRef, 'article', `kira-studies-course-card${Number(course.id) === Number(state.courseId) ? ' is-selected' : ''}`);
       item.append(node(documentRef, 'span', 'kira-studies-chip', course.kind), node(documentRef, 'h3', '', course.name), node(documentRef, 'p', '', course.target || 'Objetivo ainda não descrito'), node(documentRef, 'p', 'kira-studies-muted', `${course.level ? `${course.level} · ` : ''}${number(course.subjects)} disciplina(s) · ${number(course.study_minutes, 1)} min estudados`));
      const row = node(documentRef, 'div', 'kira-studies-actions');
      row.append(button(documentRef, Number(course.id) === Number(state.courseId) ? '✓ Selecionado' : 'Abrir', { 'data-action': 'select-course', 'data-course-id': course.id, className: 'kira-studies-button' }),
        button(documentRef, '🗑 Apagar', { 'data-action': 'delete-course', 'data-course-id': course.id, 'data-course-name': course.name, className: 'kira-studies-button is-danger' }));
      item.append(row);
      list.append(item);
    });
    if (!list.children.length) list.append(empty(documentRef, 'Nenhum curso ainda', 'O sistema não cria cursos automaticamente sem sua confirmação.', null, null));
    content.append(list);
  };

  const renderSubjects = (content) => {
    const course = selectedCourse();
    content.append(sectionTitle('Disciplinas', course ? `Competências vinculadas a ${course.name}.` : 'Selecione um curso primeiro.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie ou selecione um curso para registrar disciplinas.', 'Criar curso', 'courses')); return; }
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'subject';
    form.append(field(documentRef, 'Disciplina', 'text', 'name', '', { required: 'required', placeholder: 'Direito Constitucional, Gramática...' }), field(documentRef, 'Peso', 'number', 'weight', '1', { min: '0.1', max: '20', step: '0.1' }), field(documentRef, 'Domínio-alvo (%)', 'number', 'target_mastery', '80', { min: '1', max: '100' }), button(documentRef, 'Adicionar disciplina', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form);
    const list = node(documentRef, 'div', 'kira-studies-subject-list');
    (state.data?.detail?.subjects || []).forEach((subject) => {
      const row = node(documentRef, 'article', 'kira-studies-subject-card');
      row.append(node(documentRef, 'div', 'kira-studies-subject-title', subject.name), node(documentRef, 'span', 'kira-studies-chip', `${percent(subject.mastery)} domínio`), node(documentRef, 'p', 'kira-studies-muted', `${number(subject.attempts)} tentativa(s) · alvo ${percent(subject.target_mastery)}`));
      const track = node(documentRef, 'div', 'kira-studies-progress-track'); const fill = node(documentRef, 'span', 'kira-studies-progress-fill'); fill.style.width = `${Math.max(0, Math.min(100, Number(subject.mastery) || 0))}%`; track.append(fill); row.append(track); list.append(row);
    });
    if (!list.children.length) list.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhuma disciplina registrada ainda.'));
    content.append(list);
  };

  const taskRows = (tasks) => {
    const list = node(documentRef, 'div', 'kira-studies-task-list');
    (tasks || []).forEach((task) => {
      const row = node(documentRef, 'article', `kira-studies-task${task.status === 'done' ? ' is-done' : ''}`);
      const info = node(documentRef, 'div', 'kira-studies-task-info'); info.append(node(documentRef, 'strong', '', task.title), node(documentRef, 'span', 'kira-studies-muted', `${esc(task.subject_name, 'Sem disciplina')} · ${dateLabel(task.due_at)}${task.minutes ? ` · ${number(task.minutes)} min` : ''}`));
      row.append(info); if (task.status === 'pending') row.append(button(documentRef, '✓', { title: 'Marcar como feito (a Kira também conclui sozinha quando você atinge a meta nos simulados)', 'data-action': 'complete-task', 'data-task-id': task.id, className: 'kira-studies-button is-small' })); list.append(row);
    });
    return list;
  };

  const renderPlan = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Plano de estudos', course ? 'O plano nasce das disciplinas e do tempo que você informou.' : 'Selecione um curso para montar o plano.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso antes de planejar.', 'Criar curso', 'courses')); return; }
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'plan'; form.append(field(documentRef, 'Dias por semana', 'number', 'days_per_week', '5', { min: '1', max: '7' }), field(documentRef, 'Minutos por dia', 'number', 'minutes_per_day', String(course.daily_goal_minutes || 60), { min: '10', max: '720' }), field(documentRef, 'Início', 'date', 'start_date'), button(documentRef, 'Gerar plano', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form, taskRows(state.data?.detail?.tasks));
  };

  const renderLessons = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Aulas', course ? `Aulas baseadas na biblioteca de ${course.name}.` : 'Selecione um curso para iniciar uma aula.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para começar.', 'Criar curso', 'courses')); return; }
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'lesson'; form.append(field(documentRef, 'Tema da aula', 'text', 'topic', '', { required: 'required', placeholder: 'Ex.: princípios fundamentais' }), button(documentRef, '▶ Iniciar aula', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form);
    // A Kira sugere: matérias com menor domínio primeiro (o que mais rende estudar agora)
    const weak = [...(state.data?.detail?.subjects || [])].sort((a, b) => (Number(a.mastery) || 0) - (Number(b.mastery) || 0)).slice(0, 6);
    if (weak.length) {
      const chips = node(documentRef, 'div', 'kira-studies-suggest'); chips.append(node(documentRef, 'span', 'kira-studies-muted', '💡 Sugestões da Kira:'));
      weak.forEach((subject) => chips.append(button(documentRef, `${subject.name} · ${percent(subject.mastery)}`, { 'data-action': 'lesson-topic', 'data-topic': subject.name, className: 'kira-studies-chip is-button' })));
      content.append(chips);
    }
    if (state.session) content.append(node(documentRef, 'p', 'kira-studies-live', `Sessão em andamento · ${state.session.id}`), button(documentRef, 'Encerrar sessão', { 'data-action': 'finish-session', className: 'kira-studies-button is-small' }));
    if (state.lesson) {
      const lesson = node(documentRef, 'article', 'kira-studies-panel'); lesson.append(node(documentRef, 'h3', '', state.lesson.topic));
      if (state.lesson.sem_fonte) lesson.append(node(documentRef, 'p', 'kira-studies-warning', 'Não encontrei trechos na biblioteca; qualquer explicação geral deve ser identificada como tal.'));
      (state.lesson.trechos || []).forEach((text) => lesson.append(node(documentRef, 'p', '', text)));
      if (!state.lesson.trechos?.length) lesson.append(node(documentRef, 'p', 'kira-studies-muted', 'A biblioteca ainda não tem trechos para este tema. Pesquise ou adicione um material oficial.'));
      content.append(lesson);
    }
    const previous = node(documentRef, 'article', 'kira-studies-panel'); previous.append(node(documentRef, 'h3', '', 'Aulas anteriores')); const lessons = state.data?.detail?.lessons || []; if (!lessons.length) previous.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhuma aula anterior persistida ainda.')); lessons.forEach((item) => previous.append(node(documentRef, 'div', 'kira-studies-list-row', `${item.topic} · ${dateLabel(item.created_at)}`))); content.append(previous);
  };

  const renderMaterials = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Biblioteca de materiais', course ? `Fontes descobertas e adicionadas para ${course.name}.` : 'Selecione um curso para pesquisar materiais.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para pesquisar fontes.', 'Criar curso', 'courses')); return; }
    const researchForm = node(documentRef, 'form', 'kira-studies-form'); researchForm.dataset.form = 'research'; researchForm.append(node(documentRef, 'p', 'kira-studies-muted', 'A pesquisa usa o serviço existente e registra os links retornados; a oficialidade é marcada pelo domínio, não presumida.'), button(documentRef, 'Pesquisar fontes oficiais e provas', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(researchForm);
    const addForm = node(documentRef, 'form', 'kira-studies-form is-compact'); addForm.dataset.form = 'material'; addForm.append(field(documentRef, 'URL ou arquivo local', 'url', 'url', '', { required: 'required', placeholder: 'https://...' }), button(documentRef, 'Adicionar material', { type: 'submit', className: 'kira-studies-button' })); content.append(addForm);
    const list = node(documentRef, 'div', 'kira-studies-material-list');
    (state.data?.detail?.materials || []).forEach((material) => { const row = node(documentRef, 'article', 'kira-studies-material'); row.append(node(documentRef, 'span', 'kira-studies-chip', material.official ? 'domínio institucional' : material.status), node(documentRef, 'strong', '', material.title), node(documentRef, 'a', 'kira-studies-link', material.url)); row.querySelector('a').href = material.url; row.querySelector('a').target = '_blank'; row.querySelector('a').rel = 'noreferrer'; list.append(row); });
    if (!list.children.length) list.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhum material registrado.'));
    content.append(list);
  };

  const renderActivities = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('TDs e tarefas', course ? 'Listas de exercícios e tarefas avulsas (entram no Plano).' : 'Selecione um curso.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para registrar atividades.', 'Criar curso', 'courses')); return; }
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'task'; form.append(field(documentRef, 'Atividade', 'text', 'title', '', { required: 'required', placeholder: 'Resolver questões de Direito Constitucional' }), field(documentRef, 'Prazo', 'date', 'due_at'), field(documentRef, 'Minutos', 'number', 'minutes', '30', { min: '0', max: '1440' }), button(documentRef, 'Adicionar atividade', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form);
    const tdForm = node(documentRef, 'form', 'kira-studies-form'); tdForm.dataset.form = 'quiz'; tdForm.dataset.kind = 'td';
    tdForm.append(node(documentRef, 'p', 'kira-studies-muted', 'Gere uma lista de exercícios baseada nas fontes indexadas. Ela fica salva no banco de simulados e pode ser corrigida abaixo.'), field(documentRef, 'Tema do TD', 'text', 'topic', '', { required: 'required', placeholder: 'Ex.: interpretação de texto' }), field(documentRef, 'Questões', 'number', 'count', '5', { min: '1', max: '50' }), button(documentRef, 'Gerar TD', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(tdForm);
  };

  const renderQuiz = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Simulados', course ? 'Questões geradas a partir da biblioteca quando houver fontes indexadas.' : 'Selecione um curso.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para gerar um simulado.', 'Criar curso', 'courses')); return; }
    if (!state.quiz) {
      const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'quiz'; form.dataset.kind = 'simulado'; form.append(field(documentRef, 'Tema', 'text', 'topic', '', { required: 'required', placeholder: 'Ex.: Direito Constitucional' }), field(documentRef, 'Banca', 'text', 'banca', '', { placeholder: 'Opcional' }), field(documentRef, 'Quantidade', 'number', 'count', '5', { min: '1', max: '50' }), button(documentRef, 'Gerar simulado', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form, node(documentRef, 'p', 'kira-studies-muted', 'O sistema só marca a fonte quando ela estiver nos dados recebidos; questões sem fonte aparecem explicitamente como conhecimento geral.')); return;
    }
    const quiz = state.quiz; const result = quiz.result; const quizLabel = quiz.kind === 'td' ? 'TD' : 'Simulado'; const form = node(documentRef, 'form', 'kira-studies-quiz'); form.dataset.form = 'quiz-answer'; form.append(node(documentRef, 'h3', '', `${result ? 'Correção' : quizLabel} · ${esc(quiz.tema)}`));
    if (quiz.sem_fonte) form.append(node(documentRef, 'p', 'kira-studies-warning', 'Este simulado não encontrou fontes indexadas. Não o trate como prova oficial.'));
    (quiz.questoes || []).forEach((question, index) => {
      const box = node(documentRef, 'article', 'kira-studies-question'); box.append(node(documentRef, 'p', 'kira-studies-question-stem', `${index + 1}. ${question.enunciado}`)); const item = result?.items?.[index];
      if (question.tipo === 'discursiva') { const area = node(documentRef, 'textarea', 'kira-studies-input'); area.name = String(index); area.value = quiz.answers?.[index] || ''; area.disabled = Boolean(result); box.append(area); } else (question.alternativas || ['V) Verdadeiro', 'F) Falso']).forEach((alternative) => { const letter = String(alternative).trim().charAt(0).toUpperCase(); const label = node(documentRef, 'label', 'kira-studies-option'); const radio = node(documentRef, 'input'); radio.type = 'radio'; radio.name = String(index); radio.value = letter; radio.checked = quiz.answers?.[index] === letter; radio.disabled = Boolean(result); label.append(radio, node(documentRef, 'span', '', alternative)); box.append(label); });
      if (item) { box.classList.add(item.certa ? 'is-right' : 'is-wrong'); box.append(node(documentRef, 'p', 'kira-studies-muted', `Gabarito: ${esc(item.gabarito)} · sua resposta: ${esc(item.resposta, 'em branco')}`)); Object.entries(item.justificativas || {}).forEach(([key, value]) => box.append(node(documentRef, 'p', 'kira-studies-explanation', `${key}: ${value}`))); if (item.fonte) box.append(node(documentRef, 'p', 'kira-studies-muted', `Fonte: ${item.fonte}`)); }
      form.append(box);
    });
    const actions = node(documentRef, 'div', 'kira-studies-actions'); if (!result) actions.append(button(documentRef, 'Finalizar e corrigir', { type: 'submit', className: 'kira-studies-button is-primary' })); else actions.append(button(documentRef, 'Novo simulado', { 'data-action': 'new-quiz', className: 'kira-studies-button' })); actions.append(button(documentRef, 'Exportar prova + gabarito', { 'data-action': 'export-quiz', className: 'kira-studies-button' })); form.append(actions); content.append(form);
  };

  const renderReviews = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Revisões', 'Use recuperação ativa e registre a conclusão para ampliar o intervalo gradualmente.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para agendar revisões.', 'Criar curso', 'courses')); return; }
    const form = node(documentRef, 'form', 'kira-studies-form'); form.dataset.form = 'review'; form.append(field(documentRef, 'Assunto', 'text', 'topic', '', { required: 'required', placeholder: 'Tema a revisar' }), field(documentRef, 'Data', 'date', 'scheduled_at'), button(documentRef, 'Agendar revisão', { type: 'submit', className: 'kira-studies-button is-primary' })); content.append(form);
    const list = node(documentRef, 'div', 'kira-studies-review-list'); (state.data?.detail?.reviews || []).forEach((review) => { const row = node(documentRef, 'article', `kira-studies-review${review.completed_at ? ' is-complete' : ''}`); row.append(node(documentRef, 'strong', '', review.topic), node(documentRef, 'span', 'kira-studies-muted', `${dateLabel(review.scheduled_at)} · intervalo ${number(review.interval_days)} dia(s)`)); if (!review.completed_at) row.append(button(documentRef, 'Concluir', { 'data-action': 'complete-review', 'data-review-id': review.id, className: 'kira-studies-button is-small' })); list.append(row); }); if (!list.children.length) list.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhuma revisão registrada.')); content.append(list);
  };

  const renderCalendar = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Linha do tempo', 'Atividades e revisões em ordem de data.'));
    const rows = [...(state.data?.detail?.tasks || []).map((task) => ({ date: task.due_at, title: task.title, kind: 'atividade', status: task.status })), ...(state.data?.detail?.reviews || []).map((review) => ({ date: review.scheduled_at, title: `Revisar ${review.topic}`, kind: 'revisão', status: review.completed_at ? 'done' : 'pending' }))].filter((row) => row.date).sort((a, b) => String(a.date).localeCompare(String(b.date)));
    const list = node(documentRef, 'div', 'kira-studies-calendar'); rows.forEach((row) => { const item = node(documentRef, 'article', `kira-studies-calendar-row${row.status === 'done' ? ' is-done' : ''}`); item.append(node(documentRef, 'time', '', dateLabel(row.date)), node(documentRef, 'span', 'kira-studies-chip', row.kind), node(documentRef, 'strong', '', row.title)); list.append(item); }); if (!list.children.length) list.append(empty(documentRef, 'Calendário vazio', course ? 'Gere um plano ou agende uma revisão para aparecer aqui.' : 'Selecione um curso para planejar.', null, null)); content.append(list);
  };

  const renderPerformance = (content) => {
    const course = selectedCourse(); content.append(sectionTitle('Desempenho', 'Apenas tentativas e simulados concluídos entram nos indicadores.'));
    if (!course) { content.append(empty(documentRef, 'Nenhum curso selecionado', 'Crie um curso para acumular desempenho.', 'Criar curso', 'courses')); return; }
    const detail = state.data?.detail || {}; const metrics = node(documentRef, 'div', 'kira-studies-metrics'); metrics.append(card(documentRef, 'Média', detail.average_score == null ? 'Sem dados' : number(detail.average_score, 1), 'de 10'), card(documentRef, 'Acurácia', percent(detail.accuracy)), card(documentRef, 'Erros abertos', number(detail.errors?.length || 0))); content.append(metrics);
    const evolution = node(documentRef, 'article', 'kira-studies-panel kira-studies-evolution'); evolution.append(node(documentRef, 'h3', '', 'Evolução dos simulados')); const history = [...(detail.quiz_history || [])].reverse(); if (!history.length) evolution.append(node(documentRef, 'p', 'kira-studies-muted', 'Ainda não há simulados concluídos para comparar.')); history.forEach((item, index) => { const row = node(documentRef, 'div', 'kira-studies-evolution-row'); const label = node(documentRef, 'span', '', `${index + 1}. ${esc(item.topic, 'simulado')}`); const bar = node(documentRef, 'div', 'kira-studies-evolution-track'); const fill = node(documentRef, 'span', 'kira-studies-evolution-fill'); fill.style.width = `${Math.max(0, Math.min(100, Number(item.score) * 10 || 0))}%`; bar.append(fill); row.append(label, bar, node(documentRef, 'strong', '', item.score == null ? 'Sem nota' : `${number(item.score, 1)}/10`)); evolution.append(row); }); content.append(evolution);
    const panel = node(documentRef, 'article', 'kira-studies-panel'); panel.append(node(documentRef, 'h3', '', 'Caderno de erros')); if (!detail.errors?.length) panel.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhum erro persistido ainda.')); detail.errors?.forEach((error) => panel.append(node(documentRef, 'div', 'kira-studies-error', `${error.topic} · revisão ${dateLabel(error.next_review_at)}`), node(documentRef, 'p', 'kira-studies-muted', error.explanation || 'Sem justificativa registrada.'))); content.append(panel);
  };

  const renderHistory = (content) => {
    content.append(sectionTitle('Histórico', 'Registro cronológico do que foi criado, estudado, avaliado e revisado.'));
    const history = state.data?.detail?.history || []; const list = node(documentRef, 'div', 'kira-studies-history'); history.forEach((item) => { const row = node(documentRef, 'article', 'kira-studies-history-row'); row.append(node(documentRef, 'time', '', dateLabel(item.created_at)), node(documentRef, 'strong', '', item.event), node(documentRef, 'p', 'kira-studies-muted', item.detail || '')); list.append(row); }); if (!list.children.length) list.append(node(documentRef, 'p', 'kira-studies-muted', 'Nenhum evento educacional registrado ainda.')); content.append(list);
  };

  const renderContent = () => {
    const content = node(documentRef, 'main', 'kira-studies-content');
    if (state.busy && !state.data) content.append(node(documentRef, 'div', 'kira-studies-loading', 'Carregando dados persistidos…'));
    else if (state.error) content.append(empty(documentRef, 'Não consegui carregar Estudos', state.error, 'Tentar novamente', 'overview'));
    else if (state.tab === 'overview') renderOverview(content);
    else if (state.tab === 'courses') renderCourses(content);
    else if (state.tab === 'plan' || state.tab === 'subjects' || state.tab === 'calendar') { renderSubjects(content); renderPlan(content); renderCalendar(content); }
    else if (state.tab === 'lessons') renderLessons(content);
    else if (state.tab === 'materials') renderMaterials(content);
    else if (state.tab === 'quizzes' || state.tab === 'activities') { renderQuiz(content); if (!state.quiz) renderActivities(content); }
    else if (state.tab === 'reviews') renderReviews(content);
    else if (state.tab === 'performance' || state.tab === 'history') { renderPerformance(content); renderHistory(content); }
    if (state.notice) content.prepend(node(documentRef, 'div', `kira-studies-notice${state.error ? ' is-error' : ''}`, state.notice));
    return content;
  };

  // Render parcial e agrupado por quadro: trocar de aba só refaz o conteúdo (antes cabeçalho, menu e conteúdo
  // eram recriados a cada clique e duas vezes por atualização — FPS baixo e rolagem voltando ao topo).
  let frameRef = 0;
  let shell = null;
  let headerKey = '';
  const paint = () => {
    frameRef = 0;
    if (root.hidden) return;
    const key = `${state.courseId}|${state.busy}|${(state.data?.courses || []).length}|${state.data ? 1 : 0}`;
    if (!shell || !root.contains(shell.body)) {
      shell = { head: header(), body: node(documentRef, 'div', 'kira-studies-body') };
      root.replaceChildren(shell.head, shell.body);
      headerKey = key;
    } else if (key !== headerKey) {
      const head = header(); shell.head.replaceWith(head); shell.head = head; headerKey = key;
    }
    const oldNav = shell.body.querySelector(':scope > .kira-studies-nav');
    const oldContent = shell.body.querySelector(':scope > .kira-studies-content');
    const sameTab = shell.tab === state.tab;
    const navScroll = oldNav?.scrollTop || 0;
    const contentScroll = sameTab ? (oldContent?.scrollTop || 0) : 0;
    const nav = navigation(); const content = renderContent();
    shell.body.replaceChildren(nav, content);
    nav.scrollTop = navScroll; content.scrollTop = contentScroll; // mesma aba mantém a posição
    shell.tab = state.tab;
  };
  const render = () => {
    if (root.hidden || frameRef) return;
    frameRef = (windowRef.requestAnimationFrame || ((fn) => windowRef.setTimeout(fn, 16)))(paint);
  };

  const formData = (form) => Object.fromEntries(new FormData(form).entries());

  const handleSubmit = async (event) => {
    const form = event.target.closest?.('form[data-form]');
    if (!form) return;
    event.preventDefault();
    const type = form.dataset.form;
    const course = selectedCourse();
    try {
      state.busy = true; state.notice = ''; render();
      if (type === 'course') { const result = await api('/api/education/courses', { method: 'POST', body: JSON.stringify(formData(form)) }); state.courseId = result.course.id; state.tab = 'overview'; }
      else if (type === 'subject') await api(`/api/education/courses/${course.id}/subjects`, { method: 'POST', body: JSON.stringify(formData(form)) });
      else if (type === 'plan') await api(`/api/education/courses/${course.id}/plan`, { method: 'POST', body: JSON.stringify(formData(form)) });
      else if (type === 'task') await api(`/api/education/courses/${course.id}/tasks`, { method: 'POST', body: JSON.stringify(formData(form)) });
      else if (type === 'review') await api(`/api/education/courses/${course.id}/reviews`, { method: 'POST', body: JSON.stringify(formData(form)) });
      else if (type === 'research') { const result = await api(`/api/education/courses/${course.id}/research`, { method: 'POST', body: JSON.stringify({}) }); setNotice(`${result.materials?.length || 0} fonte(s) registradas; downloads/indexação continuam pelo serviço da biblioteca.`); }
      else if (type === 'material') { const result = await api(`/api/education/courses/${course.id}/materials`, { method: 'POST', body: JSON.stringify(formData(form)) }); setNotice(result.job ? 'Material registrado e enviado para indexação.' : 'Material registrado.'); }
      else if (type === 'lesson') { state.lesson = await api('/api/tutor/aula', { method: 'POST', body: JSON.stringify({ curso: course.name, tema: formData(form).topic }) }); state.lesson.topic = formData(form).topic; state.session = await api(`/api/education/courses/${course.id}/sessions`, { method: 'POST', body: JSON.stringify({ mode: 'aula', notes: formData(form).topic }) }).then((response) => response.session); state.tab = 'lessons'; }
      else if (type === 'quiz') { const result = await api(`/api/education/courses/${course.id}/quiz`, { method: 'POST', body: JSON.stringify(formData(form)) }); state.quiz = { ...result.quiz, kind: form.dataset.kind || 'simulado', answers: {} }; state.tab = 'quizzes'; }
      else if (type === 'quiz-answer') { const answers = {}; new FormData(form).forEach((value, key) => { if (key !== 'topic' && key !== 'banca' && key !== 'count') answers[key] = value; }); const result = await api(`/api/education/quiz/${state.quiz.id}/grade`, { method: 'POST', body: JSON.stringify({ answers }) }); state.quiz.result = result.result; state.quiz.answers = answers; setNotice(`Simulado corrigido: ${result.result.acertos}/${result.result.total}.`); }
      state.busy = false; await refresh(state.courseId);
    } catch (error) { state.busy = false; setNotice(error?.message || 'A ação educacional falhou.', true); render(); }
  };

  const handleClick = async (event) => {
    const target = event.target.closest?.('[data-tab], [data-action]'); if (!target || !root.contains(target)) return;
    if (target.dataset.tab) { state.tab = target.dataset.tab; state.notice = ''; render(); return; }
    const action = target.dataset.action;
    try {
      if (action === 'close') { apiObject.close(); return; }
      if (action === 'refresh') { await refresh(state.courseId); return; }
      if (action === 'lesson-topic') { const input = root.querySelector('form[data-form="lesson"] input[name="topic"]'); if (input) { input.value = target.dataset.topic || ''; input.focus(); } return; }
      if (action === 'delete-course') {
        const name = target.dataset.courseName || 'este curso';
        const sure = await windowRef.kiraConfirm('Plano, simulados, revisões, histórico e biblioteca indexada desse curso também saem. Não dá para desfazer.', { title: `Apagar "${name}"?`, okLabel: 'Apagar curso', danger: true });
        if (!sure) return;
        await api(`/api/education/courses/${target.dataset.courseId}`, { method: 'DELETE' });
        if (Number(target.dataset.courseId) === Number(state.courseId)) state.courseId = null;
        setNotice(`Curso "${name}" apagado.`); await refresh(state.courseId); return;
      }
      if (action === 'select-course') { state.courseId = Number(target.dataset.courseId); state.tab = 'overview'; await refresh(state.courseId); return; }
      if (action === 'complete-task') { await api(`/api/education/tasks/${target.dataset.taskId}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) }); await refresh(state.courseId); return; }
      if (action === 'complete-review') { await api(`/api/education/reviews/${target.dataset.reviewId}/complete`, { method: 'POST', body: '{}' }); await refresh(state.courseId); return; }
      if (action === 'finish-session') { if (state.session) await api(`/api/education/sessions/${state.session.id}/finish`, { method: 'POST', body: '{}' }); state.session = null; await refresh(state.courseId); return; }
      if (action === 'new-quiz') { state.quiz = null; render(); return; }
      if (action === 'export-quiz') { if (!state.quiz) return; const result = await api(`/api/education/quiz/${state.quiz.id}/export`, { method: 'POST', body: JSON.stringify({ format: 'pdf' }) }); setNotice(`Arquivos salvos: ${(result.files || []).map((file) => file.arquivo || file.path || file).join(', ')}`); render(); }
    } catch (error) { setNotice(error?.message || 'Ação não concluída.', true); render(); }
  };

  const mount = (parent = documentRef.body) => { parent.append(root); root.addEventListener('click', handleClick); root.addEventListener('submit', handleSubmit); return apiObject; };
  const open = async (courseId = null) => { windowRef.KiraUI?.windowManager?.close?.('tutor'); state.courseId = courseId || state.courseId; state.tab = 'overview'; state.quiz = null; root.hidden = false; documentRef.body.classList.add('kira-studies-open'); render(); await refresh(state.courseId); return { ok: true }; };
  const close = () => { root.hidden = true; documentRef.body.classList.remove('kira-studies-open'); return { ok: true }; };
  const destroy = () => { root.remove(); documentRef.body.classList.remove('kira-studies-open'); };
  const apiObject = { mount, open, close, refresh, destroy, get isOpen() { return !root.hidden; } };
  return apiObject;
}

