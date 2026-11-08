function make(documentRef, tag, className = '') {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  return node;
}

function appendText(parent, value, documentRef, query = '') {
  const source = String(value || '');
  const needle = String(query || '').trim();
  if (!needle) {
    parent.appendChild(documentRef.createTextNode(source));
    return;
  }
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'ig');
  let cursor = 0;
  let match;
  while ((match = pattern.exec(source))) {
    if (match.index > cursor) parent.appendChild(documentRef.createTextNode(source.slice(cursor, match.index)));
    const mark = make(documentRef, 'mark');
    mark.textContent = match[0];
    parent.appendChild(mark);
    cursor = match.index + match[0].length;
  }
  if (cursor < source.length) parent.appendChild(documentRef.createTextNode(source.slice(cursor)));
}

function appendInline(parent, value, documentRef, query = '') {
  const source = String(value || '');
  const tokenPattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\(https:\/\/[^\s)]+\))/g;
  let cursor = 0;
  let match;
  while ((match = tokenPattern.exec(source))) {
    if (match.index > cursor) appendText(parent, source.slice(cursor, match.index), documentRef, query);
    const token = match[0];
    if (token.startsWith('**')) {
      const strong = make(documentRef, 'strong');
      appendText(strong, token.slice(2, -2), documentRef, query);
      parent.appendChild(strong);
    } else if (token.startsWith('*')) {
      const italic = make(documentRef, 'em');
      appendText(italic, token.slice(1, -1), documentRef, query);
      parent.appendChild(italic);
    } else if (token.startsWith('`')) {
      const code = make(documentRef, 'code');
      appendText(code, token.slice(1, -1), documentRef, query);
      parent.appendChild(code);
    } else {
      const separator = token.indexOf('](');
      const label = token.slice(1, separator);
      const href = token.slice(separator + 2, -1);
      const link = make(documentRef, 'a');
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      appendText(link, label, documentRef, query);
      parent.appendChild(link);
    }
    cursor = match.index + token.length;
  }
  if (cursor < source.length) appendText(parent, source.slice(cursor), documentRef, query);
}

function appendParagraph(parent, lines, documentRef, query) {
  if (!lines.length) return;
  const paragraph = make(documentRef, 'p', 'kui-chat-paragraph');
  appendInline(paragraph, lines.join('\n'), documentRef, query);
  parent.appendChild(paragraph);
}

function appendCode(parent, lines, language, documentRef, onCopy) {
  const wrapper = make(documentRef, 'div', 'kui-chat-code');
  const header = make(documentRef, 'div', 'kui-chat-code-header');
  const label = make(documentRef, 'span');
  label.textContent = language || 'código';
  const copy = make(documentRef, 'button');
  copy.type = 'button';
  copy.className = 'kui-chat-copy';
  copy.textContent = 'Copiar';
  copy.setAttribute('aria-label', 'Copiar código');
  copy.addEventListener?.('click', () => onCopy?.(lines.join('\n'), copy));
  header.appendChild(label);
  header.appendChild(copy);
  const pre = make(documentRef, 'pre');
  const code = make(documentRef, 'code');
  code.textContent = lines.join('\n');
  pre.appendChild(code);
  wrapper.appendChild(header);
  wrapper.appendChild(pre);
  parent.appendChild(wrapper);
}

export function renderMarkdown(container, source, { documentRef = container?.ownerDocument || globalThis.document, query = '', onCopy = null } = {}) {
  if (!container || !documentRef) return container;
  while (container.firstChild) container.removeChild(container.firstChild);
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  let list = null;
  let listType = '';
  let code = null;

  const flushParagraph = () => { appendParagraph(container, paragraph, documentRef, query); paragraph = []; };
  const flushList = () => { if (list) container.appendChild(list); list = null; listType = ''; };

  for (const line of lines) {
    const fence = line.match(/^\s*```\s*([\w-]*)\s*$/);
    if (fence) {
      if (code) { appendCode(container, code.lines, code.language, documentRef, onCopy); code = null; }
      else { flushParagraph(); flushList(); code = { language: fence[1] || '', lines: [] }; }
      continue;
    }
    if (code) { code.lines.push(line); continue; }
    const listMatch = line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)$/);
    if (listMatch) {
      flushParagraph();
      const nextType = /^\s*\d+\./.test(line) ? 'ol' : 'ul';
      if (!list || listType !== nextType) { flushList(); listType = nextType; list = make(documentRef, nextType, 'kui-chat-list'); }
      const item = make(documentRef, 'li');
      appendInline(item, listMatch[1], documentRef, query);
      list.appendChild(item);
      continue;
    }
    flushList();
    if (!line.trim()) flushParagraph();
    else paragraph.push(line);
  }
  if (code) appendCode(container, code.lines, code.language, documentRef, onCopy);
  flushParagraph();
  flushList();
  return container;
}

export const __testing = Object.freeze({ appendInline, appendText });
