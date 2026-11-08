/* Caixas de confirmação e de texto no padrão da Kira (substituem confirm()/prompt() do navegador, que abriam a janela
   cinza do Windows). Reusam o visual do modal "Encerrar ligação" (.kira-call-dialog). Esc cancela, Enter confirma,
   foco preso dentro da caixa. Devolvem Promise: kiraConfirm -> true/false, kiraPrompt -> texto ou null. */
(function () {
  function open({ title, message, okLabel = 'Confirmar', cancelLabel = 'Cancelar', danger = false, input = null }) {
    return new Promise((resolve) => {
      const back = document.createElement('div');
      back.className = 'kira-call-dialog kira-dialog';
      back.setAttribute('role', 'dialog');
      back.setAttribute('aria-modal', 'true');
      const card = document.createElement('div');
      card.className = 'kira-call-dialog-card';
      const icon = document.createElement('div');
      icon.className = `kira-call-dialog-icon${danger ? '' : ' is-info'}`;
      icon.textContent = danger ? '!' : '?';
      const h = document.createElement('h2');
      h.textContent = title || (danger ? 'Tem certeza?' : 'Confirmar');
      const p = document.createElement('p');
      p.textContent = message || '';
      card.append(icon, h, p);
      let field = null;
      if (input !== null) {
        field = document.createElement('input');
        field.className = 'kira-dialog-input';
        field.value = String(input || '');
        card.append(field);
      }
      const row = document.createElement('div');
      row.className = 'kira-call-dialog-actions';
      const cancel = document.createElement('button');
      cancel.type = 'button'; cancel.className = 'kira-call-dialog-btn'; cancel.textContent = cancelLabel;
      const ok = document.createElement('button');
      ok.type = 'button'; ok.className = `kira-call-dialog-btn${danger ? ' kira-call-dialog-danger' : ' is-primary'}`; ok.textContent = okLabel;
      row.append(cancel, ok);
      card.append(row);
      back.append(card);
      const done = (value) => { document.removeEventListener('keydown', onKey, true); back.remove(); resolve(value); };
      const accept = () => done(field ? field.value : true);
      const reject = () => done(field ? null : false);
      const onKey = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); reject(); }
        else if (e.key === 'Enter' && (document.activeElement === field || document.activeElement === ok)) { e.preventDefault(); accept(); }
        else if (e.key === 'Tab') { // foco não sai da caixa
          const items = [field, cancel, ok].filter(Boolean);
          const i = items.indexOf(document.activeElement);
          e.preventDefault(); items[(i + (e.shiftKey ? -1 : 1) + items.length) % items.length].focus();
        }
      };
      ok.addEventListener('click', accept);
      cancel.addEventListener('click', reject);
      back.addEventListener('click', (e) => { if (e.target === back) reject(); });
      document.addEventListener('keydown', onKey, true);
      document.body.append(back);
      (field || (danger ? cancel : ok)).focus();
      if (field) field.select();
    });
  }
  window.kiraConfirm = (message, opts = {}) => open({ message, ...opts });
  window.kiraPrompt = (message, value = '', opts = {}) => open({ message, input: value, okLabel: 'Salvar', ...opts });
})();
