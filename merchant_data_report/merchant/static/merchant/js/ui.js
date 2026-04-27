/* Shared UI helpers: showToast() + showConfirm() */

(function () {
  // ── Toast ──────────────────────────────────────────────────
  function ensureContainer() {
    let c = document.getElementById('toast-container');
    if (!c) {
      c = document.createElement('div');
      c.id = 'toast-container';
      document.body.appendChild(c);
    }
    return c;
  }

  const ICONS = {
    success: '<svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/></svg>',
    error:   '<svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/></svg>',
    warning: '<svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>',
    info:    '<svg width="18" height="18" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd"/></svg>',
  };

  window.showToast = function (message, type) {
    type = type || 'info';
    const container = ensureContainer();
    const t = document.createElement('div');
    t.className = 'toast toast-' + type;
    t.innerHTML =
      '<span class="toast-icon">' + (ICONS[type] || ICONS.info) + '</span>' +
      '<span class="toast-body">' + message + '</span>' +
      '<button class="toast-close" aria-label="Close">&times;</button>';

    t.querySelector('.toast-close').addEventListener('click', () => dismiss(t));
    container.appendChild(t);

    const timer = setTimeout(() => dismiss(t), 4500);
    t._timer = timer;
  };

  function dismiss(t) {
    clearTimeout(t._timer);
    t.classList.add('toast-hide');
    setTimeout(() => t.remove(), 280);
  }

  // ── Confirm modal ──────────────────────────────────────────
  function ensureConfirmDOM() {
    let bd = document.getElementById('confirm-backdrop');
    if (!bd) {
      bd = document.createElement('div');
      bd.id = 'confirm-backdrop';
      bd.innerHTML =
        '<div id="confirm-box">' +
          '<div class="confirm-icon">' +
            '<svg width="20" height="20" viewBox="0 0 20 20" fill="#d97706"><path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd"/></svg>' +
          '</div>' +
          '<h4 id="confirm-title">Are you sure?</h4>' +
          '<p id="confirm-msg"></p>' +
          '<div class="confirm-btns">' +
            '<button class="confirm-btn-cancel" id="confirm-cancel">Cancel</button>' +
            '<button class="confirm-btn-ok" id="confirm-ok">Confirm</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(bd);

      document.getElementById('confirm-cancel').addEventListener('click', closeConfirm);
      bd.addEventListener('click', (e) => { if (e.target === bd) closeConfirm(); });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeConfirm(); });
    }
    return bd;
  }

  let _confirmCallback = null;

  function closeConfirm() {
    const bd = document.getElementById('confirm-backdrop');
    if (bd) bd.classList.remove('show');
    _confirmCallback = null;
  }

  /**
   * showConfirm(message, onConfirm, options)
   * options: { title, danger } — danger=true makes the OK button red
   */
  window.showConfirm = function (message, onConfirm, options) {
    options = options || {};
    const bd = ensureConfirmDOM();
    document.getElementById('confirm-title').textContent = options.title || 'Are you sure?';
    document.getElementById('confirm-msg').textContent   = message;

    const okBtn = document.getElementById('confirm-ok');
    okBtn.textContent = options.okLabel || 'Confirm';
    okBtn.className   = 'confirm-btn-ok' + (options.danger ? ' danger' : '');

    _confirmCallback = onConfirm;
    okBtn.onclick = function () {
      const cb = _confirmCallback; // capture before closeConfirm nulls it
      closeConfirm();
      if (cb) cb();
    };

    bd.classList.add('show');
  };
})();
