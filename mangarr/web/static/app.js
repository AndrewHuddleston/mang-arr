/* mang-arr UI behaviour, modelled on Sonarr's frontend. Plain browser JS, no
   dependencies. Every form still works without it; this adds the modals,
   menus, in-place actions and client-side sorting/filtering. */
(function () {
  'use strict';
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const body = document.body;
  const narrow = () => window.matchMedia('(max-width: 768px)').matches;
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function fmtBytes(n) {
    n = Number(n || 0);
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }
  function ago(ts) {
    if (!ts) return '-';
    let t = ts;
    if (typeof ts === 'string') { const d = new Date(ts.replace(' ', 'T') + (ts.length === 19 ? 'Z' : '')); if (isNaN(d)) return ts; t = d.getTime() / 1000; }
    const d = Math.round(Date.now() / 1000 - t);
    const a = Math.abs(d);
    const f = (n, u) => (d < 0 ? `in ${n}${u}` : `${n}${u} ago`);
    if (a >= 86400) return f(Math.floor(a / 86400), 'd');
    if (a >= 3600) return f(Math.floor(a / 3600), 'h');
    if (a >= 60) return f(Math.floor(a / 60), 'm');
    return f(a, 's');
  }
  let toastTimer = null;
  function toast(msg, kind) {
    let el = $('#toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; body.appendChild(el); }
    el.className = 'toast ' + (kind || '');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
  }
  const ICON = (name, cls) => `<svg class="ic ${cls || ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

  /* -- every link that leaves mang-arr opens in a new tab ----------------- */
  function externalLinks(root) {
    $$('a[href^="http"]', root || document).forEach((a) => {
      let host = '';
      try { host = new URL(a.href, location.href).host; } catch (e) { return; }
      if (!host || host === location.host) return;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      if (!a.classList.contains('external')) {
        a.classList.add('external');
        if (!$('.ext-icon', a) && !a.classList.contains('result-link')) a.insertAdjacentHTML('beforeend', ICON('external', 'sm ext-icon'));
      }
    });
  }
  externalLinks(document);
  document.addEventListener('DOMContentLoaded', () => externalLinks(document));

  /* -- status poller: running job message + health label (ids used by base.html) */
  async function poll() {
    try {
      const r = await fetch('/api/v1/system/status', { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const d = await r.json();
      const el = document.getElementById('status');
      if (el) el.textContent = d.job ? `running: ${d.job.kind} ${d.job.title} ${d.job.progress || ''}`.trim() : '';
      const h = document.getElementById('health');
      if (h && d.health) {
        h.className = 'health label ' + (d.health.errors ? 'danger' : d.health.warnings ? 'warning' : 'success');
        h.textContent = d.health.errors ? `${d.health.errors} problem${d.health.errors === 1 ? '' : 's'}`
          : d.health.warnings ? `${d.health.warnings} warning${d.health.warnings === 1 ? '' : 's'}` : 'healthy';
      }
    } catch (e) { /* offline; try again next tick */ }
  }
  if (document.getElementById('status')) setInterval(poll, 5000);

  /* -- sidebar: off-canvas on phones (Sonarr's PageSidebar) ---------------- */
  const navToggle = $('#navtoggle');
  const backdrop = $('#backdrop');
  function openNav(open) {
    body.classList.toggle('nav-open', open);
    if (backdrop) backdrop.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
  }
  if (navToggle) navToggle.addEventListener('click', () => openNav(!body.classList.contains('nav-open')));
  if (backdrop) backdrop.addEventListener('click', () => openNav(false));
  window.addEventListener('resize', () => { if (!narrow()) openNav(false); });
  $$('#sidebar a').forEach((a) => a.addEventListener('click', () => { if (narrow()) openNav(false); }));

  /* -- toolbar menus (Sonarr's Menu / MenuContent) -------------------------- */
  const menus = $$('details.menu');
  function closeMenus(except) { menus.forEach((m) => { if (m !== except) m.open = false; }); }
  menus.forEach((m) => {
    m.addEventListener('toggle', () => { if (m.open) closeMenus(m); });
    m.addEventListener('click', (e) => {
      const item = e.target.closest('.menu-item');
      if (!item) return;
      $$('.menu-item', m).forEach((it) => it.setAttribute('aria-checked', String(it === item)));
      m.open = false;
      m.dispatchEvent(new CustomEvent('menuselect', { detail: { value: item.dataset.value, item } }));
    });
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('details.menu')) closeMenus(); });
  function menuValue(id) { const it = $(`#${id} .menu-item[aria-checked="true"]`); return it ? it.dataset.value : null; }
  function setMenuValue(id, value) {
    $$(`#${id} .menu-item`).forEach((it) => it.setAttribute('aria-checked', String(it.dataset.value === value)));
  }

  /* -- modals (Sonarr's Modal / ModalContent) ------------------------------ */
  let openModalEl = null, lastFocus = null;
  function openModal(id) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (!el) return null;
    if (openModalEl && openModalEl !== el) openModalEl.hidden = true;
    lastFocus = document.activeElement;
    el.hidden = false;
    openModalEl = el;
    body.classList.add('modal-open');
    const first = $('input:not([type=hidden]):not([readonly]), select, textarea, button:not(.modal-close)', el);
    if (first) first.focus();
    return el;
  }
  function closeModal() {
    if (!openModalEl) return;
    openModalEl.hidden = true;
    openModalEl = null;
    body.classList.remove('modal-open');
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  document.addEventListener('click', (e) => {
    const opener = e.target.closest('[data-open-modal]');
    if (opener) { e.preventDefault(); openModal(opener.dataset.openModal); return; }
    if (e.target.closest('[data-close-modal]')) { e.preventDefault(); closeModal(); return; }
    if (e.target.classList && e.target.classList.contains('modal-backdrop')) closeModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (openModalEl) { closeModal(); return; }
    if (menus.some((m) => m.open)) { closeMenus(); return; }
    if (body.classList.contains('nav-open')) openNav(false);
  });
  // a drawn checkbox that feeds a hidden field (Edit modal's "Monitored")
  $$('input[data-sync]').forEach((cb) => {
    const sync = () => { const h = $(`input[type=hidden][name="${cb.dataset.sync}"]`, cb.closest('form')); if (h) h.value = cb.checked ? '1' : '0'; };
    cb.addEventListener('change', sync);
  });
  // Delete modal: the red message appears when files are going to be deleted
  $$('#delete-modal input[name="files"]').forEach((cb) => {
    const msg = $('#delete-modal .delete-files-message');
    cb.addEventListener('change', () => { if (msg) msg.hidden = !cb.checked; });
  });

  /* -- in-place actions: monitor toggle, refresh / search (Sonarr's icon buttons) */
  async function postForm(form) {
    const r = await fetch(form.action, { method: 'POST', body: new FormData(form), redirect: 'follow', headers: { Accept: 'text/html' } });
    let msg = '';
    try { msg = new URL(r.url).searchParams.get('m') || ''; } catch (e) { /* ignore */ }
    return { ok: r.ok, msg };
  }
  $$('form[data-async="monitor"]').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button', form);
      const input = $('input[name="monitored"]', form);
      btn.disabled = true;
      try {
        const res = await postForm(form);
        if (!res.ok) throw new Error('failed');
        const nowMonitored = input.value === '1';
        input.value = nowMonitored ? '0' : '1';
        btn.classList.toggle('monitored', nowMonitored);
        btn.title = nowMonitored ? 'Monitored: click to unmonitor' : 'Unmonitored: click to monitor';
        btn.setAttribute('aria-label', btn.title);
        const use = $('use', btn);
        if (use) use.setAttribute('href', nowMonitored ? '#i-bookmark' : '#i-bookmark-off');
        $$('[data-id="' + form.dataset.id + '"]').forEach((it) => {
          it.dataset.monitored = nowMonitored ? '1' : '0';
          it.classList.toggle('dim', !nowMonitored);
        });
        applySeriesView();
        toast(nowMonitored ? 'Monitored' : 'Unmonitored', 'success');
      } catch (err) {
        form.submit();               // fall back to the plain form (lands on the series page)
      } finally { btn.disabled = false; }
    });
  });
  $$('form[data-async="refresh"]').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button', form);
      const svg = $('svg', form);
      btn.disabled = true;
      if (svg) svg.classList.add('spin');
      try {
        const res = await postForm(form);
        if (!res.ok) throw new Error('failed');
        toast(res.msg || 'queued', /already|cannot|not / .test(res.msg) ? 'danger' : 'success');
        setTimeout(() => { if (svg) svg.classList.remove('spin'); btn.disabled = false; }, 1500);
      } catch (err) {
        form.submit();
      }
    });
  });

  /* -- series index: view / sort / filter (Sonarr's SeriesIndex) ---------- */
  const views = $('.views[data-view]');
  const seriesSearch = $('#series-search');
  const dirBtn = $('#series-dir');
  const countEl = $('#series-count');
  function setView(v) {
    if (!views) return;
    views.dataset.view = v;
    store.set('series-view', v);
    setMenuValue('view-menu', v);
    const icon = $('#view-menu summary use');
    if (icon) icon.setAttribute('href', '#i-' + v);
  }
  const matchers = {
    all: () => true,
    monitored: (d) => d.monitored === '1',
    unmonitored: (d) => d.monitored !== '1',
    wanted: (d) => Number(d.wanted) > 0,
    continuing: (d) => d.status === 'continuing',
    ended: (d) => d.status === 'ended',
  };
  const keyOf = {
    title: (d) => (d.title || '').toLowerCase(),
    status: (d) => d.status || '',
    progress: (d) => Number(d.progress || 0),
    checked: (d) => d.checked || '',
    added: (d) => d.added || '',
    wanted: (d) => Number(d.wanted || 0),
  };
  function applySeriesView() {
    if (!views) return;
    const q = (seriesSearch ? seriesSearch.value : '').trim().toLowerCase();
    const filterKey = menuValue('filter-menu') || 'all';
    const sortKey = menuValue('sort-menu') || 'title';
    const f = matchers[filterKey] || matchers.all;
    const dir = dirBtn && dirBtn.dataset.dir === 'desc' ? -1 : 1;
    const key = keyOf[sortKey] || keyOf.title;
    let shown = 0;
    $$('[data-items]', views).forEach((container) => {
      const items = $$(':scope > [data-id]', container);
      items.sort((a, b) => {
        const ka = key(a.dataset), kb = key(b.dataset);
        if (ka < kb) return -1 * dir;
        if (ka > kb) return 1 * dir;
        return (a.dataset.title || '').localeCompare(b.dataset.title || '');
      });
      let n = 0;
      items.forEach((it) => {
        const ok = f(it.dataset) && (!q || (it.dataset.title || '').toLowerCase().includes(q)
          || (it.dataset.alt || '').toLowerCase().includes(q));
        it.hidden = !ok;
        if (ok) n++;
        container.appendChild(it);
      });
      shown = n;
    });
    if (countEl) countEl.textContent = (shown === Number(countEl.dataset.total) ? shown : shown + ' of ' + countEl.dataset.total) + ' series';
    $$('th.sortable', views).forEach((th) => {
      th.setAttribute('aria-sort', th.dataset.sort === sortKey ? (dir === 1 ? 'ascending' : 'descending') : 'none');
    });
    $$('#sort-menu .menu-item').forEach((it) => { if (it.dataset.value === sortKey) it.dataset.dir = dir === 1 ? 'asc' : 'desc'; else delete it.dataset.dir; });
    store.set('series-sort', sortKey + ':' + (dir === 1 ? 'asc' : 'desc'));
    store.set('series-filter', filterKey);
  }
  if (views) {
    const saved = store.get('series-view');
    setView(['posters', 'overview', 'table'].includes(saved) ? saved : views.dataset.view);
    const savedSort = (store.get('series-sort') || 'title:asc').split(':');
    if (keyOf[savedSort[0]]) setMenuValue('sort-menu', savedSort[0]);
    if (dirBtn) dirBtn.dataset.dir = savedSort[1] === 'desc' ? 'desc' : 'asc';
    const savedFilter = store.get('series-filter');
    if (savedFilter && matchers[savedFilter]) setMenuValue('filter-menu', savedFilter);
    const vm = $('#view-menu'), sm = $('#sort-menu'), fm = $('#filter-menu');
    if (vm) vm.addEventListener('menuselect', (e) => setView(e.detail.value));
    if (sm) sm.addEventListener('menuselect', (e) => {
      const prev = (store.get('series-sort') || 'title:asc').split(':')[0];
      if (dirBtn) dirBtn.dataset.dir = prev === e.detail.value && dirBtn.dataset.dir === 'asc' ? 'desc' : 'asc';
      applySeriesView();
    });
    if (fm) fm.addEventListener('menuselect', applySeriesView);
    if (seriesSearch) {
      seriesSearch.addEventListener('input', applySeriesView);
      const form = seriesSearch.closest('form');
      if (form) form.addEventListener('submit', (e) => { e.preventDefault(); applySeriesView(); });
    }
    $$('th.sortable', views).forEach((th) => {
      th.tabIndex = 0;
      const go = () => {
        const cur = menuValue('sort-menu');
        if (cur === th.dataset.sort && dirBtn) dirBtn.dataset.dir = dirBtn.dataset.dir === 'desc' ? 'asc' : 'desc';
        else { setMenuValue('sort-menu', th.dataset.sort); if (dirBtn) dirBtn.dataset.dir = 'asc'; }
        applySeriesView();
      };
      th.addEventListener('click', go);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
    applySeriesView();

    // Edit / Delete modals shared by every poster, overview row and table row
    const editForm = $('#edit-form'), deleteForm = $('#delete-form');
    function fillModal(form, d) {
      $$('[data-field="title"]', form.closest('.modal-backdrop')).forEach((el) => { el.textContent = d.title; });
      $$('[data-field="path"]', form.closest('.modal-backdrop')).forEach((el) => { el.textContent = d.path; if (el.tagName === 'INPUT') el.value = d.path; });
      $$('[data-field="have"]', form.closest('.modal-backdrop')).forEach((el) => { el.textContent = d.have || 0; });
    }
    function openEdit(id) {
      const it = $(`[data-items] > [data-id="${id}"]`);
      if (!it || !editForm) return;
      const d = it.dataset;
      editForm.action = `/series/${id}/monitor`;
      fillModal(editForm, d);
      const hidden = $('input[type=hidden][name="monitored"]', editForm);
      const cb = $('input[data-sync="monitored"]', editForm);
      if (hidden) hidden.value = d.monitored === '1' ? '1' : '0';
      if (cb) cb.checked = d.monitored === '1';
      const notes = $('[data-field="notes"]', editForm);
      if (notes) notes.value = `${d.have || 0} of ${d.progress ? Math.round(d.have / (d.progress / 100)) : d.have} chapters on disk, ${d.wanted} wanted. Status: ${d.status}. Last checked: ${ago(d.checked)}.`;
      if (deleteForm) { deleteForm.action = `/series/${id}/delete`; deleteForm.dataset.title = d.title; fillModal(deleteForm, d); }
      openModal('edit-modal');
    }
    $$('[data-edit]').forEach((b) => b.addEventListener('click', () => openEdit(b.dataset.edit)));
    $$('[data-open-delete]').forEach((b) => b.addEventListener('click', () => {
      const files = $('#delete-modal input[name="files"]');
      if (files) { files.checked = false; const msg = $('#delete-modal .delete-files-message'); if (msg) msg.hidden = true; }
      openModal('delete-modal');
    }));
  }

  /* -- series details: seasons (Sonarr's SeriesDetailsSeason) -------------- */
  function setGroup(head, open) {
    head.setAttribute('aria-expanded', String(open));
    head.title = open ? 'Hide chapters' : 'Show chapters';
    const use = $('use', head);
    if (use) use.setAttribute('href', open ? '#i-collapse' : '#i-expand');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    if (panel) panel.hidden = !open;
  }
  const heads = $$('.grouphead');
  heads.forEach((btn) => btn.addEventListener('click', () => setGroup(btn, btn.getAttribute('aria-expanded') !== 'true')));
  $$('[data-collapse]').forEach((b) => b.addEventListener('click', () => {
    const head = heads.find((h) => h.getAttribute('aria-controls') === b.dataset.collapse);
    if (head) { setGroup(head, false); head.scrollIntoView({ block: 'nearest' }); }
  }));
  const expandAll = $('#expand-all');
  function syncExpandAll() {
    if (!expandAll || !heads.length) return;
    const allOpen = heads.every((h) => h.getAttribute('aria-expanded') === 'true');
    expandAll.dataset.expandAll = allOpen ? '0' : '1';
    $('.toolbar-label', expandAll).textContent = allOpen ? 'Collapse All' : 'Expand All';
    $('use', expandAll).setAttribute('href', allOpen ? '#i-collapse' : '#i-expand');
  }
  if (expandAll) {
    expandAll.addEventListener('click', () => { const open = expandAll.dataset.expandAll === '1'; heads.forEach((h) => setGroup(h, open)); syncExpandAll(); });
    heads.forEach((h) => h.addEventListener('click', syncExpandAll));
    syncExpandAll();
  }
  // group search: queue an automatic search for every wanted chapter in the group
  $$('[data-group-search]').forEach((b) => b.addEventListener('click', async () => {
    const group = b.closest('.season');
    // the action may carry ?page=N (a paged series page), so not action$="/search"
    const forms = $$('tr.episode-row.wanted form[action*="/search"], tr.episode-row.failed form[action*="/search"]', group);
    if (!forms.length) { toast('nothing wanted in this group'); return; }
    b.disabled = true; $('svg', b).classList.add('spin');
    let n = 0;
    for (const f of forms) { try { const r = await fetch(f.action, { method: 'POST', redirect: 'follow' }); if (r.ok) n++; } catch (e) { /* keep going */ } }
    toast(`${n} chapter search${n === 1 ? '' : 'es'} queued`, 'success');
    setTimeout(() => location.reload(), 1200);
  }));

  /* -- chapter details modal (Sonarr's EpisodeDetailsModalContent) --------- */
  const chapterModal = $('#chapter-modal');
  if (chapterModal) {
    const field = (name) => $$(`[data-field="${name}"]`, chapterModal);
    const setField = (name, value) => field(name).forEach((el) => { el.textContent = value == null || value === '' ? '-' : String(value); });
    const labelKind = { have: 'success', wanted: 'danger', failed: 'danger', unavailable: 'purple', ignored: 'disabled', junk: 'default' };
    const labelText = { have: 'Downloaded', wanted: 'Missing', failed: 'Failed', unavailable: 'Unavailable', ignored: 'Ignored', junk: 'Junk' };
    let current = null;
    function showTab(name) {
      $$('.tab', chapterModal).forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
      $$('.tab-panel', chapterModal).forEach((p) => { p.hidden = p.dataset.panel !== name; });
    }
    $$('.tab', chapterModal).forEach((t) => t.addEventListener('click', () => showTab(t.dataset.tab)));
    function renderReleases(list, downloadUrl) {
      if (!list.length) return '<div class="alert info">No source lists this chapter.</div>';
      const rows = list.map((r) => {
        const rejected = r.note ? `<span class="rejected" title="${esc(r.note)}">${ICON('warning', 'sm')}</span>` : '';
        const action = r.listed === false ? '' :
          `<form method="post" action="${esc(downloadUrl)}" class="iconform"><input type="hidden" name="manga_id" value="${esc(r.mangaId)}">` +
          `<button class="iconbtn" title="${r.note ? 'Override and download from this entry' : 'Download from this entry'}">${ICON('download')}</button></form>`;
        return `<tr>
          <td>${esc(r.source)}${r.error ? `<div class="note">${esc(r.error)}</div>` : ''}</td>
          <td class="nowrap">${esc((r.uploaded || '').slice(0, 10))}</td>
          <td>${r.listed === false ? '<span class="muted">not listed</span>' : esc(r.name || '')}${r.note ? `<div class="note">${esc(r.note)}</div>` : ''}</td>
          <td>${esc(r.title)}</td>
          <td class="muted">${esc(r.scanlator || '')}</td>
          <td class="col-icon">${r.downloaded ? `<span class="grabbed" title="already downloaded in Suwayomi">${ICON('downloaded', 'sm')}</span>` : rejected}</td>
          <td class="col-actions">${action}</td></tr>`;
      });
      return '<div class="table-container"><table class="table"><thead><tr><th>Source</th><th>Age</th><th>Title</th><th>Indexer entry</th>' +
        '<th>Scanlator</th><th class="col-icon">' + ICON('warning', 'sm') + '</th><th class="col-actions">' + ICON('download', 'sm') + '</th></tr></thead><tbody>' +
        rows.join('') + '</tbody></table></div>';
    }
    async function loadReleases() {
      const box = $('[data-field="releases"]', chapterModal);
      const buttons = $('[data-field="search-buttons"]', chapterModal);
      buttons.hidden = true;
      box.hidden = false;
      box.innerHTML = '<div class="loading">Searching every source…</div>';
      try {
        const r = await fetch(current.manual, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        const list = Array.isArray(d) ? d : (d.releases || d.items || []);
        box.innerHTML = '<div class="help-text">What each source has for this chapter. A warning means the automatic search would not use that entry; downloading from it here overrides that.</div>' +
          renderReleases(list, current.download);
        externalLinks(box);
      } catch (e) {
        box.innerHTML = `<div class="alert danger">Could not fetch releases: ${esc(e.message || e)}</div>`;
      }
    }
    $('[data-start-interactive]', chapterModal).addEventListener('click', loadReleases);
    async function openChapter(btn, tab) {
      current = { url: btn.dataset.chapterUrl, manual: btn.dataset.manual, download: btn.dataset.download, search: btn.dataset.search };
      setField('number', btn.dataset.number);
      setField('name', btn.dataset.name);
      ['uploaded', 'status', 'reason', 'source', 'updated'].forEach((n) => setField(n, '…'));
      $('[data-field="file"]', chapterModal).innerHTML = '';
      $('[data-field="history"]', chapterModal).innerHTML = '<div class="loading">Loading…</div>';
      const qs = $('.quick-search-form', chapterModal);
      qs.action = current.search;
      $('[data-field="search-buttons"]', chapterModal).hidden = false;
      const rel = $('[data-field="releases"]', chapterModal); rel.hidden = true; rel.innerHTML = '';
      showTab(tab || 'summary');
      openModal(chapterModal);
      try {
        const r = await fetch(current.url, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const c = await r.json();
        setField('name', c.name || btn.dataset.name);
        setField('uploaded', (c.uploaded || '').slice(0, 10) || 'unknown');
        field('status').forEach((el) => { el.innerHTML = `<span class="label ${labelKind[c.status] || 'default'} medium">${esc(labelText[c.status] || c.status)}</span>`; });
        setField('reason', c.reason || '');
        setField('source', c.source_name || '');
        setField('updated', c.updated_at ? `${c.updated_at} (${ago(c.updated_at)})` : '');
        const fileBox = $('[data-field="file"]', chapterModal);
        if (c.library_path) {
          fileBox.innerHTML = '<div class="table-container"><table class="table"><thead><tr><th>Path</th><th>Size</th></tr></thead><tbody>' +
            `<tr><td><code>${esc(c.library_path)}</code></td><td class="nowrap">${c.size != null ? fmtBytes(c.size) : '-'}</td></tr></tbody></table></div>`;
        } else fileBox.innerHTML = '<div class="help-text">No file on disk for this chapter.</div>';
        const hist = $('[data-field="history"]', chapterModal);
        const events = c.events || [];
        hist.innerHTML = events.length
          ? '<div class="table-container"><table class="table"><thead><tr><th class="col-icon"></th><th>Event</th><th>Message</th><th class="col-date">Date</th></tr></thead><tbody>' +
            events.map((e) => `<tr><td class="col-icon event-icon ${e.kind === 'failed' ? 'danger' : e.kind === 'downloaded' || e.kind === 'imported' ? 'success' : ''}">${ICON({ downloaded: 'download', imported: 'drive', failed: 'warning', ignore: 'ignore' }[e.kind] || 'info', 'sm')}</td><td>${esc(e.kind)}</td><td>${esc(e.message)}</td><td class="nowrap">${esc(e.at)}</td></tr>`).join('') +
            '</tbody></table></div>'
          : '<div class="alert info">No history for this chapter.</div>';
        if (c.status === 'have') { $('[data-field="search-buttons"]', chapterModal).hidden = false; }
        externalLinks(chapterModal);
      } catch (e) {
        setField('status', 'could not load: ' + (e.message || e));
      }
      if (tab === 'search' && btn.hasAttribute('data-interactive')) loadReleases();
    }
    $$('.episode-title-link').forEach((b) => b.addEventListener('click', () => openChapter(b, 'summary')));
    $$('[data-interactive]').forEach((b) => b.addEventListener('click', () => openChapter(b, 'search')));
  }

  /* -- add new: result cards open the add modal (Sonarr's AddNewSeriesModal) */
  const addModal = $('#add-modal');
  if (addModal) {
    const form = $('#add-form');
    $$('.search-result [data-add]').forEach((b) => b.addEventListener('click', () => {
      const d = b.closest('.search-result').dataset;
      $('input[name="ref"]', form).value = d.ref;
      $$('[data-field="title"]', addModal).forEach((el) => { el.textContent = d.title; });
      $$('[data-field="year"]', addModal).forEach((el) => { el.textContent = d.year ? `(${d.year})` : ''; });
      $('[data-field="overview"]', addModal).textContent = d.overview || 'No description.';
      $('[data-field="network"]', addModal).value = d.network || d.kind || '';
      const root = $('[data-field="rootfolder"]', addModal);
      root.value = root.dataset.root.replace(/\/$/, '') + '/' + d.title.replace(/[\\/:*?"<>|]+/g, ' ').trim();
      $('[data-field="poster"]', addModal).innerHTML = d.cover ? `<img class="poster-image" src="${esc(d.cover)}" alt="">` : `<div class="poster-image poster-none">${ICON('image')}<span>no cover</span></div>`;
      externalLinks(addModal);
      openModal(addModal);
    }));
  }

  /* -- wanted: row selection, Search All / Search Selected (Sonarr's Missing) */
  const missingTable = $('#missing-table');
  if (missingTable) {
    const all = $('#select-all');
    const boxes = () => $$('input[data-select]', missingTable);
    const searchBtn = $('#search-missing');
    const selected = () => boxes().filter((b) => b.checked && !b.closest('tr').hidden).map((b) => b.dataset.select);
    function syncToolbar() {
      const n = selected().length;
      $('.toolbar-label', searchBtn).textContent = n ? `${searchBtn.dataset.labelSelected} (${n})` : searchBtn.dataset.labelAll;
      if (all) { const vis = boxes().filter((b) => !b.closest('tr').hidden); all.checked = vis.length > 0 && vis.every((b) => b.checked); }
    }
    if (all) all.addEventListener('change', () => { boxes().forEach((b) => { if (!b.closest('tr').hidden) b.checked = all.checked; }); syncToolbar(); });
    boxes().forEach((b) => b.addEventListener('change', syncToolbar));
    const selectAllBtn = $('#select-all-btn');
    if (selectAllBtn) selectAllBtn.addEventListener('click', () => { const vis = boxes().filter((b) => !b.closest('tr').hidden); const on = !vis.every((b) => b.checked); vis.forEach((b) => { b.checked = on; }); syncToolbar(); });
    $('#search-all-form').addEventListener('submit', async (e) => {
      const ids = selected();
      if (!ids.length) return;                       // plain "search all" post
      e.preventDefault();
      searchBtn.disabled = true;
      let n = 0;
      for (const id of ids) {
        try { const r = await fetch(`/series/${id}/refresh`, { method: 'POST', body: new URLSearchParams({ download: '1' }), redirect: 'follow' }); if (r.ok) n++; } catch (err) { /* keep going */ }
      }
      toast(`${n} search${n === 1 ? '' : 'es'} queued`, 'success');
      setTimeout(() => { location.href = '/activity'; }, 800);
    });
    const wf = $('#wanted-filter');
    if (wf) wf.addEventListener('menuselect', (e) => {
      $$('tbody tr', missingTable).forEach((tr) => {
        const failed = Number(tr.dataset.failed) > 0;
        tr.hidden = e.detail.value === 'failed' ? !failed : e.detail.value === 'clean' ? failed : false;
      });
      syncToolbar();
    });
  }

  /* -- activity queue + history filters ------------------------------------- */
  const qf = $('#queue-filter');
  if (qf) qf.addEventListener('menuselect', (e) => {
    $$('#jobs tbody tr').forEach((tr) => { tr.hidden = e.detail.value !== 'all' && tr.dataset.status !== e.detail.value; });
  });
  const hf = $('#history-filter');
  if (hf) hf.addEventListener('menuselect', (e) => {
    let n = 0;
    $$('#history-table tbody tr').forEach((tr) => { tr.hidden = e.detail.value !== 'all' && tr.dataset.kind !== e.detail.value; if (!tr.hidden) n++; });
    const c = $('#history-count');
    if (c) c.textContent = (e.detail.value === 'all' ? c.dataset.total : n + ' of ' + c.dataset.total) + ' events';
  });

  /* -- description clamp ---------------------------------------------------- */
  $$('[data-clamp]').forEach((el) => {
    const btn = document.getElementById(el.dataset.clamp);
    if (!btn) return;
    el.classList.add('clamped');
    if (el.scrollHeight <= el.clientHeight + 2) { btn.hidden = true; return; }
    btn.addEventListener('click', () => {
      const clamped = el.classList.toggle('clamped');
      btn.textContent = clamped ? 'more' : 'less';
      btn.setAttribute('aria-expanded', String(!clamped));
    });
  });

  /* -- log page: level filter + auto refresh ------------------------------- */
  const logPre = $('#log');
  if (logPre) {
    const levelSel = $('#log-level');
    const auto = $('#log-auto');
    const follow = $('#log-follow');
    const countL = $('#log-count');
    const LEVEL = /^\S+ \S+ (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/;
    const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
    function render(lines) {
      logPre.textContent = '';
      lines.forEach((l) => {
        const m = LEVEL.exec(l);
        const span = document.createElement('span');
        span.className = 'ln ' + (m ? m[1] : '');
        span.textContent = l.replace(/\n$/, '');
        logPre.appendChild(span);
      });
      filter();
    }
    function filter() {
      const min = rank[levelSel ? levelSel.value : 'DEBUG'] || 0;
      let n = 0;
      $$('.ln', logPre).forEach((s) => {
        const lvl = (s.className.match(/\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/) || [])[1];
        const ok = !lvl ? min === 0 : (rank[lvl] >= min);
        s.hidden = !ok;
        if (ok) n++;
      });
      if (countL) countL.textContent = n + ' lines';
      if (follow && follow.checked) logPre.scrollTop = logPre.scrollHeight;
    }
    async function refresh() {
      try {
        const n = Number(logPre.dataset.lines || 500);
        const r = await fetch('/api/v1/log?lines=' + n, { headers: { Accept: 'application/json' } });
        if (!r.ok) return;
        const d = await r.json();
        render(d.lines || []);
      } catch (e) { /* keep what we have */ }
    }
    if (levelSel) levelSel.addEventListener('change', filter);
    let timer = null;
    function setAuto(on) {
      if (timer) clearInterval(timer);
      timer = on ? setInterval(refresh, 5000) : null;
      store.set('log-auto', on ? '1' : '0');
    }
    if (auto) {
      auto.checked = store.get('log-auto') !== '0';
      auto.addEventListener('change', () => setAuto(auto.checked));
      setAuto(auto.checked);
    }
    if (follow) follow.addEventListener('change', filter);
    $$('[data-log-refresh]').forEach((b) => b.addEventListener('click', refresh));
    filter();
  }

  /* -- covers that fail to load fall back to the placeholder --------------- */
  $$('img.poster-image').forEach((img) => {
    img.addEventListener('error', () => {
      const ph = document.createElement('div');
      ph.className = img.className + ' poster-none';
      ph.innerHTML = ICON('image') + '<span>no cover</span>';
      img.replaceWith(ph);
    }, { once: true });
  });

  /* -- behaviour that used to be inline on*= attributes / <script> blocks.
        The Content-Security-Policy allows scripts from /static only. ------- */
  // forms that ask first: <form data-confirm="Delete X?">
  document.addEventListener('submit', (e) => {
    const f = e.target.closest && e.target.closest('form[data-confirm]');
    if (f && !window.confirm(f.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
  // a checkbox that hides an element while ticked: <input type=checkbox data-hides="element-id">
  $$('input[data-hides]').forEach((cb) => {
    const sync = () => { const el = document.getElementById(cb.dataset.hides); if (el) el.hidden = cb.checked; };
    cb.addEventListener('change', sync);
  });
  // Lists: show only the fields of the chosen list kind
  const kindSel = $('#addlist #kind');
  if (kindSel) {
    const showKind = () => $$('#addlist [data-kind]').forEach((d) => { d.style.display = d.dataset.kind === kindSel.value ? '' : 'none'; });
    kindSel.addEventListener('change', showKind);
    showKind();
  }
  // Settings -> Security: reveal the API key (fetched from the API, never in the page source)
  const keyBtn = $('[data-reveal-api-key]');
  if (keyBtn) keyBtn.addEventListener('click', async () => {
    const input = document.getElementById(keyBtn.dataset.revealApiKey);
    try {
      const r = await fetch('/api/v1/settings', { headers: { Accept: 'application/json' } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (input) { input.type = 'text'; input.value = d.api_key || ''; input.select(); }
      keyBtn.hidden = true;
    } catch (err) { toast('could not read the API key: ' + (err.message || err), 'danger'); }
  });

  /* -- pages that reload themselves while something runs ------------------ */
  const main = $('main[data-reload]');
  if (main) {
    const ms = Number(main.dataset.reload) || 10000;
    setTimeout(() => {
      const a = document.activeElement;
      if (a && (a.tagName === 'INPUT' || a.tagName === 'SELECT' || a.tagName === 'TEXTAREA')) return;
      if (openModalEl || menus.some((m) => m.open)) return;
      location.reload();
    }, ms);
  }
})();
