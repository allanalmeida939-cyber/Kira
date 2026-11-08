import { el, clear, applyAppearance } from './kit.js';

const MAX_SHORTCUTS = 20;
const MAX_UPLOAD = 3_000_000;

/** Só https público: o mesmo critério do backend, repetido aqui para não renderizar links inválidos. */
export function safeHttps(value) {
  try {
    const url = new URL(String(value || '').trim());
    const host = url.hostname;
    const privateHost = host === 'localhost' || !host.includes('.') || /^(10|127|169\.254|192\.168)\./.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host);
    return url.protocol === 'https:' && !privateHost && !url.username && !url.password ? url.href : null;
  } catch (_) {
    return null;
  }
}

const notes = {
  kind: 'notes', title: 'Notas rápidas', category: 'Produtividade', size: { w: 320, h: 260 }, bare: false,
  styles: [['plain', 'Simples'], ['paper', 'Papel']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    box.classList.add('w-notes', `w-notes-${ctx.style || 'plain'}`);
    const area = el('textarea', 'w-textarea');
    area.placeholder = 'Escreva aqui…';
    area.maxLength = 1800;
    area.value = String(ctx.options.text || '');
    area.addEventListener('input', () => ctx.setOptions({ text: area.value }));
    box.appendChild(area);
  },
};

const todo = {
  kind: 'todo', title: 'Lista de tarefas', category: 'Produtividade', size: { w: 320, h: 320 }, bare: false,
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    const items = Array.isArray(ctx.options.items) ? ctx.options.items.slice(0, 40) : [];
    const form = el('form', 'w-form');
    const input = el('input', 'w-input');
    input.placeholder = 'Nova tarefa';
    input.maxLength = 120;
    const add = el('button', 'w-button', '+');
    add.type = 'submit';
    form.append(input, add);
    const list = el('div', 'w-list');
    box.append(form, list);
    const save = () => ctx.setOptions({ items });
    const draw = () => {
      clear(list);
      items.forEach((item, index) => {
        const row = el('label', 'w-todo');
        const check = el('input');
        check.type = 'checkbox';
        check.checked = Boolean(item.done);
        const text = el('span', item.done ? 'is-done' : '', item.text);
        const remove = el('button', 'w-link', '×');
        remove.type = 'button';
        check.addEventListener('change', () => { item.done = check.checked; save(); draw(); });
        remove.addEventListener('click', (event) => { event.preventDefault(); items.splice(index, 1); save(); draw(); });
        row.append(check, text, remove);
        list.appendChild(row);
      });
    };
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (text && items.length < 40) { items.push({ text, done: false }); input.value = ''; save(); draw(); }
    });
    draw();
  },
};

const shortcuts = {
  kind: 'shortcuts', title: 'Atalhos', category: 'Produtividade', size: { w: 320, h: 260 }, bare: false,
  styles: [['list', 'Lista'], ['chips', 'Botões']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    let links = (Array.isArray(ctx.options.items) ? ctx.options.items : []).filter((l) => l && l.name && safeHttps(l.url)).slice(0, MAX_SHORTCUTS);
    const form = el('form', 'w-form w-form-wrap');
    const name = el('input', 'w-input');
    name.placeholder = 'Nome';
    name.maxLength = 30;
    const url = el('input', 'w-input');
    url.placeholder = 'https://…';
    url.maxLength = 300;
    const add = el('button', 'w-button', '+');
    add.type = 'submit';
    form.append(name, url, add);
    const list = el('div', ctx.style === 'chips' ? 'w-chips' : 'w-list');
    box.append(form, list);
    const hint = el('p', 'w-muted', '');
    const draw = () => {
      clear(list);
      links.forEach((link, index) => {
        const row = el('div', ctx.style === 'chips' ? 'w-chip-row' : 'w-row');
        const open = el('button', ctx.style === 'chips' ? 'w-chip' : 'w-link', link.name);
        open.type = 'button';
        open.title = link.url;
        open.addEventListener('click', () => ctx.openExternal(link.url));
        const remove = el('button', 'w-link w-x', '×');
        remove.type = 'button';
        remove.addEventListener('click', () => { links.splice(index, 1); ctx.setOptions({ items: links }); draw(); });
        row.append(open, remove);
        list.appendChild(row);
      });
      if (!links.length) list.appendChild(el('p', 'w-muted w-center', 'Adicione um nome e um endereço https acima.'));
    };
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const safe = safeHttps(url.value);
      if (!name.value.trim() || !safe || links.length >= MAX_SHORTCUTS) { hint.textContent = 'Use um endereço https público.'; if (!hint.isConnected) box.appendChild(hint); return; }
      links.push({ name: name.value.trim(), url: safe });
      name.value = '';
      url.value = '';
      hint.remove();
      ctx.setOptions({ items: links });
      draw();
    });
    draw();
  },
};

const image = {
  kind: 'image', title: 'Imagem / GIF', category: 'Produtividade', size: { w: 360, h: 280 }, bare: true,
  styles: [['contain', 'Inteira'], ['cover', 'Preencher']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    box.classList.add('w-image-box', `w-image-${ctx.style || 'contain'}`);
    const source = () => {
      if (ctx.options.file && /^[a-f0-9]{16}\.(jpg|png|gif|webp)$/.test(ctx.options.file)) return `/api/ui/widgets/file/${ctx.options.file}`;
      return safeHttps(ctx.options.url);
    };
    const draw = () => {
      clear(box);
      const src = source();
      if (src) {
        const picture = el('img', 'w-image');
        picture.alt = '';
        picture.referrerPolicy = 'no-referrer';
        picture.addEventListener('error', () => { ctx.setOptions({ file: '', url: '' }); draw(); });
        picture.src = src;
        const change = el('button', 'w-button w-image-change', 'Trocar');
        change.type = 'button';
        change.addEventListener('click', () => { ctx.setOptions({ file: '', url: '' }); draw(); });
        box.append(picture, change);
        return;
      }
      const picker = el('input');
      picker.type = 'file';
      picker.accept = 'image/jpeg,image/png,image/gif,image/webp';
      picker.hidden = true;
      const choose = el('button', 'w-button', 'Escolher arquivo');
      choose.type = 'button';
      choose.addEventListener('click', () => picker.click());
      const status = el('p', 'w-muted w-center', 'Escolha uma imagem do computador ou cole um endereço https.');
      picker.addEventListener('change', async () => {
        const file = picker.files?.[0];
        if (!file) return;
        if (file.size > MAX_UPLOAD) { status.textContent = 'A imagem passa de 3 MB.'; return; }
        try {
          const response = await fetch('/api/ui/widgets/upload', { method: 'POST', body: await file.arrayBuffer() });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          ctx.setOptions({ file: (await response.json()).file, url: '' });
          draw();
        } catch (_) { status.textContent = 'Não consegui usar essa imagem.'; }
      });
      const form = el('form', 'w-form');
      const url = el('input', 'w-input');
      url.placeholder = 'https://…';
      const use = el('button', 'w-button', 'Usar');
      use.type = 'submit';
      form.append(url, use);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        const safe = safeHttps(url.value);
        if (!safe) { status.textContent = 'Use um endereço https público.'; return; }
        ctx.setOptions({ url: safe, file: '' });
        draw();
      });
      const wrap = el('div', 'w-image-picker');
      wrap.append(choose, picker, form, status);
      box.appendChild(wrap);
    };
    draw();
  },
};

const text = {
  kind: 'text', title: 'Texto livre', category: 'Produtividade', size: { w: 300, h: 160 }, bare: true,
  styles: [['plain', 'Normal'], ['title', 'Título grande'], ['note', 'Destaque']],
  mount(box, ctx) {
    applyAppearance(box, ctx.options);
    box.classList.add('w-freetext', `w-text-${ctx.style || 'plain'}`);
    const area = el('textarea', 'w-textarea w-textarea-bare');
    area.placeholder = 'Clique e escreva…';
    area.maxLength = 1800;
    area.value = String(ctx.options.content || '');
    area.addEventListener('input', () => ctx.setOptions({ content: area.value }));
    box.appendChild(area);
  },
};

export const PRODUCTIVITY_WIDGETS = [notes, todo, shortcuts, image, text];
