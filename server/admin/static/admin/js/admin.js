/* TG-Fox API — Admin Panel JS */

// ─── Theme ─────────────────────────────────────────────────────────────────

const THEME_KEY = 'admin_theme';

function initTheme() {
  const saved = localStorage.getItem(THEME_KEY) || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem(THEME_KEY, next);
  updateThemeIcon(next);
}

function updateThemeIcon(theme) {
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.textContent = theme === 'dark' ? 'Light' : 'Dark';
}

// ─── Toast ─────────────────────────────────────────────────────────────────

function toast(msg, type = 'info', duration = 3500) {
  const container = document.getElementById('toast-container') || (() => {
    const el = document.createElement('div');
    el.id = 'toast-container';
    document.body.appendChild(el);
    return el;
  })();

  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  const icon = document.createElement('span');
  const text = document.createElement('span');
  icon.textContent = ({ success: 'OK', error: '!', info: 'i', warning: '!' })[type] || '•';
  text.textContent = String(msg);
  t.append(icon, text);
  container.appendChild(t);

  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transform = 'translateX(20px)';
    t.style.transition = '.3s ease';
    setTimeout(() => t.remove(), 300);
  }, duration);
}

// Alias so templates can call either showToast() or toast()
function showToast(msg, type = 'info', duration = 3500) { toast(msg, type, duration); }

// ─── CSRF ────────────────────────────────────────────────────────────────────

let _csrfToken = '';
let _csrfLoading = null;

async function loadCsrf() {
  if (_csrfToken) return _csrfToken;
  if (_csrfLoading) return _csrfLoading;
  _csrfLoading = (async () => {
    try {
      const r = await fetch('/admin/api/csrf', { credentials: 'same-origin' });
      if (r.ok) { const d = await r.json(); _csrfToken = d.csrf || ''; }
    } catch(e) {}
    finally { _csrfLoading = null; }
    return _csrfToken;
  })();
  return _csrfLoading;
}

// ─── API helpers ────────────────────────────────────────────────────────────

async function api(method, url, body = null) {
  if (method !== 'GET' && !_csrfToken) await loadCsrf();
  if (method !== 'GET' && !_csrfToken) throw new Error('Secure admin session is not ready. Please retry.');
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
  };
  if (method !== 'GET' && _csrfToken) {
    opts.headers['X-CSRF-Token'] = _csrfToken;
  }
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (res.status === 401) { window.location = '/adminlogin'; throw new Error('Unauthorized'); }
  return res;
}

async function apiJSON(method, url, body = null) {
  const res = await api(method, url, body);
  const type = res.headers.get('content-type') || '';
  const data = type.includes('application/json') ? await res.json() : { detail: `Request failed (${res.status})` };
  return res.ok ? data : { ...data, ok: false, status: res.status };
}

async function apiGET(url) { return apiJSON('GET', url); }
async function apiPOST(url, body = {}) { return apiJSON('POST', url, body); }
async function apiDELETE(url) { return apiJSON('DELETE', url); }
async function apiPATCH(url, body = {}) { return apiJSON('PATCH', url, body); }

// ─── Modal ─────────────────────────────────────────────────────────────────

function openModal(id) {
  const m = document.getElementById(id);
  if (m) { m.classList.add('open'); m.dataset.open = '1'; }
}

function closeModal(id) {
  const m = document.getElementById(id);
  if (m) { m.classList.remove('open'); delete m.dataset.open; }
}

function closeAllModals() {
  document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
}

// Click outside to close
document.addEventListener('click', (e) => {
  if (e.target.classList.contains('modal-overlay')) closeAllModals();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeAllModals();
});

// ─── Date formatting ────────────────────────────────────────────────────────

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

function fmtDateShort(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function timeAgo(iso) {
  if (!iso) return '—';
  const sec = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return `${Math.floor(sec/86400)}d ago`;
}

// ─── Status badges ──────────────────────────────────────────────────────────

function statusBadge(status) {
  const map = {
    completed:  ['green',  '✓ Completed'],
    pending:    ['yellow', '⏳ Pending'],
    cancelled:  ['red',    '✕ Cancelled'],
    refunded:   ['blue',   '↩ Refunded'],
    disputed:   ['orange', '⚡ Disputed'],
    failed:     ['red',    '✕ Failed'],
    processing: ['cyan',   '⟳ Processing'],
    rejected:   ['red',    '✕ Rejected'],
    approved:   ['green',  '✓ Approved'],
    expired:    ['gray',   '⌛ Expired'],
    paid:       ['green',  '$ Paid'],
    clean:      ['green',  '✓ Clean'],
    limited:    ['yellow', '⚠ Limited'],
    frozen:     ['red',    '❄ Frozen'],
    unknown:    ['gray',   '? Unknown'],
    active:     ['green',  '● Active'],
    inactive:   ['gray',   '○ Inactive'],
  };
  const [cls, label] = map[status] || ['gray', escapeHtml(String(status || 'unknown'))];
  return `<span class="badge badge-${cls}">${label}</span>`;
}

function escapeHtml(value) {
  const node = document.createElement('span');
  node.textContent = String(value);
  return node.innerHTML;
}

function spamBadge(s) { return statusBadge(s || 'unknown'); }

// ─── Pagination helper ──────────────────────────────────────────────────────

function renderPagination(container, { total, page, limit }, onPage) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const info = `${(page-1)*limit + 1}–${Math.min(page*limit, total)} of ${total}`;

  container.innerHTML = `
    <span class="pagination-info">${info}</span>
    <button class="page-btn" ${page <= 1 ? 'disabled' : ''} onclick="(${onPage})(${page-1})">‹</button>
    ${paginationPages(page, pages).map(p =>
      p === '...' ? `<span style="padding:0 4px;color:var(--text3)">…</span>`
                  : `<button class="page-btn ${p === page ? 'active' : ''}" onclick="(${onPage})(${p})">${p}</button>`
    ).join('')}
    <button class="page-btn" ${page >= pages ? 'disabled' : ''} onclick="(${onPage})(${page+1})">›</button>
  `;
}

function paginationPages(cur, total) {
  if (total <= 7) return Array.from({length: total}, (_,i) => i+1);
  const pages = [];
  if (cur <= 4) {
    for (let i = 1; i <= 5; i++) pages.push(i);
    pages.push('...'); pages.push(total);
  } else if (cur >= total - 3) {
    pages.push(1); pages.push('...');
    for (let i = total - 4; i <= total; i++) pages.push(i);
  } else {
    pages.push(1); pages.push('...');
    for (let i = cur - 1; i <= cur + 1; i++) pages.push(i);
    pages.push('...'); pages.push(total);
  }
  return pages;
}

// ─── Loading state helpers ──────────────────────────────────────────────────

function showLoading(el, msg = 'Loading...') {
  el.innerHTML = `<div class="loading"><div class="spinner"></div><div style="margin-top:10px">${msg}</div></div>`;
}

function showEmpty(el, msg = 'No items found.', sub = '') {
  el.innerHTML = `<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">${msg}</div>${sub ? `<div class="empty-sub">${sub}</div>` : ''}</div>`;
}

// ─── Confirm dialog ─────────────────────────────────────────────────────────

function confirm(msg, onYes) {
  const id = '__confirm_modal';
  let m = document.getElementById(id);
  if (!m) {
    m = document.createElement('div');
    m.id = id;
    m.className = 'modal-overlay';
    m.innerHTML = `
      <div class="modal">
        <div class="modal-header"><span class="modal-title">Confirm</span></div>
        <div class="modal-body" id="${id}_msg"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('${id}')">Cancel</button>
          <button class="btn btn-danger" id="${id}_yes">Confirm</button>
        </div>
      </div>`;
    document.body.appendChild(m);
  }
  document.getElementById(`${id}_msg`).textContent = msg;
  document.getElementById(`${id}_yes`).onclick = () => { closeModal(id); onYes(); };
  openModal(id);
}

// ─── Number formatting ──────────────────────────────────────────────────────

function fmtMoney(n) {
  if (n === null || n === undefined) return '—';
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtNum(n) {
  return Number(n).toLocaleString('en-US');
}

// ─── Init ───────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => { initTheme(); loadCsrf(); initLiveUpdates(); });

// Mobile sidebar toggle
function toggleSidebar() {
  document.querySelector('.layout')?.classList.toggle('sidebar-open');
}

// ─── Live SSE updates ────────────────────────────────────────────────────────

let _liveSource = null;
let _liveReconnectTimer = null;
let _liveListeners = [];

function onLiveStats(cb) { _liveListeners.push(cb); }

function initLiveUpdates() {
  if (!window.EventSource) return;
  const connect = () => {
    if (_liveSource) return;
    try {
      _liveSource = new EventSource('/admin/api/events/stream');
      _liveSource.onopen = () => setLiveOnline(true);
      _liveSource.onerror = () => { setLiveOnline(false); disconnect(); };
      _liveSource.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          setLiveOnline(true);
          _liveListeners.forEach(cb => cb(data));
        } catch(err) {}
      };
    } catch(e) { setLiveOnline(false); }
  };
  const disconnect = () => {
    if (_liveSource) { _liveSource.close(); _liveSource = null; }
    setLiveOnline(false);
    clearTimeout(_liveReconnectTimer);
    _liveReconnectTimer = setTimeout(connect, 5000);
  };
  connect();
  window.addEventListener('beforeunload', () => { if (_liveSource) _liveSource.close(); });
}

function setLiveOnline(online) {
  const el = document.getElementById('live-indicator');
  if (!el) return;
  el.classList.toggle('active', online);
  el.title = online ? 'Live updates connected' : 'Live updates disconnected';
  const toast = document.getElementById('connection-toast');
  if (toast) toast.classList.toggle('hidden', online);
}

// ─── CSV export helper ───────────────────────────────────────────────────────

function exportCSV(filename, rows) {
  if (!rows || !rows.length) { toast('No data to export', 'warning'); return; }
  const headers = Object.keys(rows[0]);
  const escape = (v) => {
    if (v === null || v === undefined) return '';
    const s = String(v).replace(/"/g, '""');
    return s.includes(',') || s.includes('"') || s.includes('\n') ? `"${s}"` : s;
  };
  const csv = [headers.join(','), ...rows.map(r => headers.map(h => escape(r[h])).join(','))].join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
  toast('CSV downloaded', 'success');
}
