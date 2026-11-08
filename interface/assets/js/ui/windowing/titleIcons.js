// Ícone do cabeçalho de cada janela, no mesmo traço dos ícones da barra lateral (24x24, contorno).
const SVG_NS = 'http://www.w3.org/2000/svg';

const PATHS = {
  newspaper: ['M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2', 'M18 14h-8', 'M15 18h-5', 'M10 6h8v4h-8V6Z'],
  book: ['M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z', 'M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z'],
  weather: ['M12 2v2', 'm4.93 4.93 1.41 1.41', 'M20 12h2', 'm19.07 4.93-1.41 1.41', 'M15.947 12.65a4 4 0 0 0-5.925-4.128', 'M13 22H7a5 5 0 1 1 4.9-6H13a3 3 0 0 1 0 6Z'],
  calendar: ['M8 2v4', 'M16 2v4', 'M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'M3 10h18'],
  mail: ['M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z', 'm22 6-10 7L2 6'],
  health: ['M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z', 'M3.22 12H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27'],
  chart: ['M3 3v18h18', 'M18 17V9', 'M13 17V5', 'M8 17v-3'],
  briefing: ['M12 2v8', 'm4.93 10.93 1.41 1.41', 'M2 18h2', 'M20 18h2', 'm19.07 10.93-1.41 1.41', 'M22 22H2', 'm8 6 4-4 4 4', 'M16 18a4 4 0 0 0-8 0'],
  image: ['M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z', 'M7 9a2 2 0 1 0 4 0a2 2 0 1 0-4 0', 'm21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21'],
  video: ['M6 3l14 9-14 9V3z'],
  article: ['M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z', 'M14 2v6h6', 'M16 13H8', 'M16 17H8', 'M10 9H8'],
  vote: ['m9 12 2 2 4-4', 'M5 7c0-1.1.9-2 2-2h10a2 2 0 0 1 2 2v12H5V7Z', 'M22 19H2'],
  trophy: ['M6 9H4.5a2.5 2.5 0 0 1 0-5H6', 'M18 9h1.5a2.5 2.5 0 0 0 0-5H18', 'M4 22h16', 'M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22', 'M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22', 'M18 2H6v7a6 6 0 0 0 12 0V2Z'],
  activity: ['M22 12h-4l-3 9L9 3l-3 9H2'],
  search: ['M3 11a8 8 0 1 0 16 0a8 8 0 1 0-16 0', 'm21 21-4.3-4.3'],
  sparkle: ['M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z'],
  insta: ['M7 3h10a4 4 0 0 1 4 4v10a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4V7a4 4 0 0 1 4-4Z', 'M16 11.4A4 4 0 1 1 12.6 8a4 4 0 0 1 3.4 3.4Z', 'M17.5 6.5h.01'],
  chef: ['M17 21a1 1 0 0 0 1-1v-5.35c0-.457.316-.844.727-1.041a4 4 0 0 0-2.134-7.589 5 5 0 0 0-9.186 0 4 4 0 0 0-2.134 7.588c.411.198.727.585.727 1.041V20a1 1 0 0 0 1 1Z', 'M6 17h12'],
};

const SECTION_ICONS = {
  news: 'newspaper', weather: 'weather', agenda: 'calendar', email: 'mail', health: 'health',
  chart: 'chart', video: 'video', image_gallery: 'image', text: 'book', timeline: 'book', recipe: 'chef', instagram: 'insta',
};
const PRESENTATION_ICONS = {
  'news-kira': 'newspaper', 'weather-kira': 'weather', 'briefing-kira': 'briefing', 'video-kira': 'video',
  'health-kira': 'health', 'agenda-kira': 'calendar', 'email-kira': 'mail', 'cozinha-kira': 'chef', 'instagram-kira': 'insta',
};
const KIND_ICONS = {
  gallery: 'image', article: 'article', election: 'vote', scoreboard: 'trophy', video: 'video',
  vitals: 'activity', 'search-log': 'search', info: 'search',
};

/** Escolhe o desenho pelo conteúdo: notícia -> jornal, história/pesquisa -> livro, clima -> nuvem... */
export function titleIconName(kind, props = {}, current = '') {
  if (kind === 'presentation') {
    const presentation = props.presentation || {};
    const known = PRESENTATION_ICONS[String(presentation.presentationId || '')];
    if (known) return known;
    const types = (Array.isArray(presentation.sections) ? presentation.sections : []).map((section) => section?.type);
    const first = types.find((type) => SECTION_ICONS[type]);
    // Patch parcial não traz os tipos das seções: mantém o desenho escolhido na abertura.
    return first ? SECTION_ICONS[first] : (current || 'book');
  }
  if (kind === 'viewer') return Array.isArray(props.beats) && props.beats.length > 1 ? 'book' : 'image';
  return KIND_ICONS[kind] || 'sparkle';
}

export function renderTitleIcon(holder, name, documentRef = holder?.ownerDocument) {
  if (!holder || !documentRef?.createElementNS) return;
  holder.replaceChildren();
  holder.dataset.icon = name;
  const svg = documentRef.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of PATHS[name] || PATHS.sparkle) {
    const path = documentRef.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  }
  holder.appendChild(svg);
}
