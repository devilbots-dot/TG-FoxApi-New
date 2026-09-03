(function () {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { try { tg.ready(); tg.expand(); } catch (_) {} }
  const initData = (tg && tg.initData) || '';
  const HEADERS = { 'X-Telegram-Init-Data': initData, 'Content-Type': 'application/json' };

  async function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, HEADERS, opts.headers || {});
    const res = await fetch(path, opts);
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      let msg = 'Request failed';
      try { msg = JSON.parse(body).detail || msg; } catch (_) { if (body) msg = body; }
      throw new Error(msg);
    }
    return res.json();
  }

  const toastEl = document.getElementById('toast');
  let toastT;
  function toast(msg, err) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.className = 'toast' + (err ? ' err' : '');
    toastEl.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { toastEl.hidden = true; }, 2600);
  }

  const drawer = document.getElementById('drawer');
  const scrim = document.getElementById('scrim');
  const openDrawer = () => { drawer.classList.add('open'); scrim.classList.add('open'); };
  const closeDrawer = () => { drawer.classList.remove('open'); scrim.classList.remove('open'); };
  document.getElementById('drawerToggle')?.addEventListener('click', openDrawer);
  document.getElementById('drawerClose')?.addEventListener('click', closeDrawer);
  scrim?.addEventListener('click', closeDrawer);

  const openModal = (id) => document.getElementById(id).hidden = false;
  const closeModal = (id) => document.getElementById(id).hidden = true;
  document.querySelectorAll('[data-close-modal]').forEach(b => {
    b.addEventListener('click', () => b.closest('.modal-root').hidden = true);
  });

  // ── Theme + accent
  const ACCENTS = [
    { id: 'gold',    c1: '#f4b53f', c2: '#ffd97a' },
    { id: 'blue',    c1: '#5aa9ff', c2: '#8ec5ff' },
    { id: 'purple',  c1: '#c084fc', c2: '#e0b8ff' },
    { id: 'pink',    c1: '#ff6ab0', c2: '#ffb0d4' },
    { id: 'green',   c1: '#4ade80', c2: '#a7f3d0' },
    { id: 'orange',  c1: '#ff9d6c', c2: '#ffd0aa' },
    { id: 'red',     c1: '#ff5a6a', c2: '#ff9aa5' },
    { id: 'cyan',    c1: '#22d3ee', c2: '#a5f3fc' },
  ];
  function hexToRgb(h) { const n = parseInt(h.slice(1), 16); return [n>>16&255, n>>8&255, n&255]; }
  function applyAccent(id) {
    const a = ACCENTS.find(x => x.id === id) || ACCENTS[0];
    const [r,g,b] = hexToRgb(a.c1);
    const root = document.documentElement.style;
    root.setProperty('--accent', a.c1);
    root.setProperty('--accent-2', a.c2);
    root.setProperty('--accent-soft', `rgba(${r},${g},${b},.16)`);
    try { localStorage.setItem('tgfox_accent', id); } catch(_) {}
  }
  function loadAccent() {
    try { const s = localStorage.getItem('tgfox_accent'); if (s) applyAccent(s); } catch(_) {}
  }
  function applyTheme(t) { document.documentElement.setAttribute('data-theme', t); }

  async function openThemes() {
    closeDrawer();
    openModal('themesModal');
    // Accent picker
    const acc = document.getElementById('accentRow');
    if (acc && !acc.dataset.ready) {
      acc.innerHTML = '';
      ACCENTS.forEach(a => {
        const b = document.createElement('button');
        b.className = 'accent-dot';
        b.style.background = `linear-gradient(135deg, ${a.c1}, ${a.c2})`;
        b.title = a.id;
        b.dataset.id = a.id;
        b.addEventListener('click', () => {
          applyAccent(a.id);
          document.querySelectorAll('.accent-dot').forEach(d => d.classList.toggle('active', d.dataset.id === a.id));
          toast('Accent applied');
        });
        acc.appendChild(b);
      });
      acc.dataset.ready = '1';
      try {
        const s = localStorage.getItem('tgfox_accent') || 'gold';
        document.querySelectorAll('.accent-dot').forEach(d => d.classList.toggle('active', d.dataset.id === s));
      } catch(_) {}
    }
    const grid = document.getElementById('themeGrid');
    grid.innerHTML = '<div class="muted" style="padding:16px">Loading…</div>';
    try {
      const data = await api('/webapp/api/themes');
      grid.innerHTML = '';
      data.themes.forEach(t => {
        const el = document.createElement('button');
        el.className = 'theme-tile' + (t.active ? ' active' : '') + (t.premium && !t.owned ? ' locked' : '');
        el.dataset.t = t.id;
        el.innerHTML = (t.premium && !t.owned ? `<span class="price-tag">$${t.price.toFixed(2)}</span>` : '') +
          `<span class="t-name">${t.name}</span>`;
        el.addEventListener('click', () => onThemeClick(t));
        grid.appendChild(el);
      });
    } catch (e) { grid.innerHTML = `<div class="muted" style="padding:16px">${e.message}</div>`; }
  }
  document.getElementById('openThemes')?.addEventListener('click', openThemes);
  document.getElementById('openThemesBtn')?.addEventListener('click', openThemes);

  async function onThemeClick(t) {
    if (t.premium && !t.owned) {
      openModal('confirmModal');
      document.getElementById('confirmTitle').textContent = `Buy "${t.name}" theme?`;
      document.getElementById('confirmBody').innerHTML = `Deduct <b>$${t.price.toFixed(2)}</b> and unlock <b>${t.name}</b> permanently.`;
      const yes = document.getElementById('confirmYes');
      yes.textContent = `Yes, buy for $${t.price.toFixed(2)}`;
      const handler = async () => {
        yes.disabled = true;
        try {
          const r = await api('/webapp/api/theme/purchase', { method: 'POST', body: JSON.stringify({ theme: t.id }) });
          toast(`Unlocked ${r.name}! New balance $${r.new_balance.toFixed(2)}`);
          applyTheme(r.theme); closeModal('confirmModal'); closeModal('themesModal'); hydrate();
        } catch (e) { toast(e.message, true); }
        finally { yes.disabled = false; yes.removeEventListener('click', handler); }
      };
      yes.replaceWith(yes.cloneNode(true));
      document.getElementById('confirmYes').addEventListener('click', handler);
      return;
    }
    try {
      const r = await api('/webapp/api/theme/select', { method: 'POST', body: JSON.stringify({ theme: t.id }) });
      applyTheme(r.theme); closeModal('themesModal'); toast('Theme applied'); hydrate();
    } catch (e) { toast(e.message, true); }
  }

  // ── Deposit / Withdraw actions
  function openBotDeepLink(action) {
    const uname = window.__BOT_USERNAME__;
    if (uname) {
      const url = `https://t.me/${uname}?start=${action}`;
      try { tg.openTelegramLink(url); tg.close(); return; } catch (_) {}
    }
    try { tg.close(); } catch(_) {}
    toast('Open the bot chat and use the ' + action + ' menu.', true);
  }
  document.getElementById('depositBtn')?.addEventListener('click', () => openBotDeepLink('deposit'));
  document.getElementById('withdrawBtn')?.addEventListener('click', () => openBotDeepLink('withdraw'));

  const fmt$ = n => '$' + (Number(n || 0)).toFixed(2);
  const fmtInt$ = n => '$' + Math.round(Number(n || 0));

  // Prefill user id / name from Telegram client (before server round-trip)
  function prefillFromTg() {
    try {
      const u = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
      if (!u) return;
      const el = document.getElementById('accountId');
      if (el && el.textContent === '—') el.textContent = String(u.id);
      window.__TG_USER__ = u;
    } catch(_) {}
  }
  prefillFromTg();

  async function hydrate() {
    loadAccent();
    try {
      const d = await api('/webapp/api/me');
      if (d.bot_username) window.__BOT_USERNAME__ = d.bot_username;
      applyTheme(d.prefs.theme);
      const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
      set('topBalance', fmt$(d.balance));
      set('topRank', d.rank);
      set('balHuge', fmt$(d.balance));
      set('buySpent', fmt$(d.buy_spent));
      set('sellEarned', fmt$(d.sell_earned));
      set('purchasesN', d.counts.purchases);
      set('salesN', d.counts.sales);
      set('rankName', d.rank);
      set('accountId', d.user.id);

      const nxt = d.next_reward || {};
      const goal = Number(nxt.goal || 0);
      const cur = Math.min(Number(nxt.progress || 0), goal || Number(nxt.progress || 0));
      const pct = goal > 0 ? Math.min(100, Math.round((cur / goal) * 100)) : 100;
      const pf = document.getElementById('progressFill'); if (pf) pf.style.width = pct + '%';
      set('progressCur', fmtInt$(cur));
      set('progressGoal', goal > 0 ? fmtInt$(goal) : '—');
      if (nxt.rank) {
        set('rewardPrize', nxt.rank);
        set('progressRem', Number(nxt.remaining || 0) > 0
          ? `${fmtInt$(nxt.remaining)} more to reach ${nxt.rank}`
          : `Ready — reach ${nxt.rank} soon`);
      } else { set('rewardPrize', 'MAX'); set('progressRem', 'Max rank reached'); }

      const ctn = document.getElementById('currentThemeName');
      if (ctn) {
        const names = { dark_gold:'Dark Gold', midnight_blue:'Midnight Blue', neon_purple:'Neon Purple', greenfield:'Green Field', village:'Village', mountains:'Mountains' };
        ctn.textContent = names[d.prefs.theme] || d.prefs.theme;
      }
    } catch (e) {
      toast((e && e.message) ? ('Load failed: '+e.message) : 'Open from the bot to load data.', true);
    }
  }
  hydrate();
})();
