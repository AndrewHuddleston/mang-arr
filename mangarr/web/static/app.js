/* mang-arr UI: sidebar, series views, chapter groups, log filter, status poller.
   Plain browser JS, no dependencies. Every page works without it; this only
   adds convenience. */
(function () {
  'use strict';
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const narrow = () => window.matchMedia('(max-width: 1000px)').matches;

  /* -- status poller: running job + health pill (ids used by base.html) -- */
  async function poll() {
    try {
      const r = await fetch('/api/v1/system/status', { headers: { Accept: 'application/json' } });
      if (!r.ok) return;
      const d = await r.json();
      const el = document.getElementById('status');
      if (el) el.textContent = d.job ? `running: ${d.job.kind} ${d.job.title} ${d.job.progress || ''}` : '';
      const h = document.getElementById('health');
      if (h && d.health) {
        h.className = 'health ' + (d.health.errors ? 'bad' : d.health.warnings ? 'warn' : 'ok');
        h.textContent = d.health.errors ? `${d.health.errors} problem${d.health.errors === 1 ? '' : 's'}`
          : d.health.warnings ? `${d.health.warnings} warning${d.health.warnings === 1 ? '' : 's'}` : 'healthy';
      }
    } catch (e) { /* offline; try again next tick */ }
  }
  if (document.getElementById('status')) setInterval(poll, 5000);

  /* -- sidebar ------------------------------------------------------------ */
  const body = document.body;
  const navToggle = $('#navtoggle');
  const backdrop = $('#backdrop');
  function openNav(open) {
    body.classList.toggle('nav-open', open);
    if (backdrop) backdrop.hidden = !open;
    if (navToggle) navToggle.setAttribute('aria-expanded', String(open));
  }
  if (navToggle) {
    if (store.get('nav-collapsed') === '1' && !narrow()) body.classList.add('nav-collapsed');
    navToggle.addEventListener('click', () => {
      if (narrow()) {
        openNav(!body.classList.contains('nav-open'));
      } else {
        const c = body.classList.toggle('nav-collapsed');
        store.set('nav-collapsed', c ? '1' : '0');
      }
    });
  }
  if (backdrop) backdrop.addEventListener('click', () => openNav(false));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && body.classList.contains('nav-open')) openNav(false); });
  window.addEventListener('resize', () => { if (!narrow()) openNav(false); });
  $$('.navexpand').forEach((btn) => {
    btn.addEventListener('click', () => {
      const sec = btn.closest('.navsec');
      const sub = $('.navsub', sec);
      const open = !sec.classList.contains('open');
      sec.classList.toggle('open', open);
      if (sub) sub.hidden = !open;
      btn.setAttribute('aria-expanded', String(open));
      btn.setAttribute('aria-label', (open ? 'Collapse ' : 'Expand ') + sec.dataset.section);
    });
  });
  // on a phone, following a link inside the overlay closes it
  $$('#sidebar a').forEach((a) => a.addEventListener('click', () => { if (narrow()) openNav(false); }));

  /* -- monitored toggle: post the tiny form in place, keep the page ------- */
  $$('form[data-async="monitor"]').forEach((form) => {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = $('button', form);
      const input = $('input[name="monitored"]', form);
      btn.disabled = true;
      try {
        const r = await fetch(form.action, { method: 'POST', body: new FormData(form), redirect: 'follow' });
        if (!r.ok) throw new Error(r.status);
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
      } catch (err) {
        form.submit();               // fall back to the plain form (lands on the series page)
      } finally { btn.disabled = false; }
    });
  });

  /* -- series index: view toggle, search, sort, filter -------------------- */
  const views = $('.views[data-view]');
  const seriesSearch = $('#series-search');
  const sortSel = $('#series-sort');
  const filterSel = $('#series-filter');
  const dirBtn = $('#series-dir');
  const countEl = $('#series-count');
  function setView(v) {
    if (!views) return;
    views.dataset.view = v;
    store.set('series-view', v);
    $$('[data-setview]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.setview === v)));
  }
  if (views) {
    const saved = store.get('series-view');
    if (saved === 'posters' || saved === 'table') setView(saved); else setView(views.dataset.view);
    $$('[data-setview]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.setview)));
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
    const f = matchers[filterSel ? filterSel.value : 'all'] || matchers.all;
    const sortKey = sortSel ? sortSel.value : 'title';
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
    if (countEl) countEl.textContent = shown + ' of ' + countEl.dataset.total + ' series';
    $$('th.sortable', views).forEach((th) => {
      th.setAttribute('aria-sort', th.dataset.sort === sortKey ? (dir === 1 ? 'ascending' : 'descending') : 'none');
    });
    store.set('series-sort', sortKey + ':' + (dir === 1 ? 'asc' : 'desc'));
    store.set('series-filter', filterSel ? filterSel.value : 'all');
  }
  if (views) {
    const savedSort = (store.get('series-sort') || 'title:asc').split(':');
    if (sortSel && keyOf[savedSort[0]]) sortSel.value = savedSort[0];
    if (dirBtn) dirBtn.dataset.dir = savedSort[1] === 'desc' ? 'desc' : 'asc';
    const savedFilter = store.get('series-filter');
    if (filterSel && savedFilter && matchers[savedFilter]) filterSel.value = savedFilter;
    if (seriesSearch) seriesSearch.addEventListener('input', applySeriesView);
    if (sortSel) sortSel.addEventListener('change', applySeriesView);
    if (filterSel) filterSel.addEventListener('change', applySeriesView);
    if (dirBtn) dirBtn.addEventListener('click', () => {
      dirBtn.dataset.dir = dirBtn.dataset.dir === 'desc' ? 'asc' : 'desc';
      dirBtn.textContent = dirBtn.dataset.dir === 'desc' ? '▾' : '▴';
      applySeriesView();
    });
    $$('th.sortable', views).forEach((th) => {
      th.tabIndex = 0;
      const go = () => {
        if (sortSel && sortSel.value === th.dataset.sort && dirBtn) {
          dirBtn.dataset.dir = dirBtn.dataset.dir === 'desc' ? 'asc' : 'desc';
        } else if (sortSel) { sortSel.value = th.dataset.sort; if (dirBtn) dirBtn.dataset.dir = 'asc'; }
        if (dirBtn) dirBtn.textContent = dirBtn.dataset.dir === 'desc' ? '▾' : '▴';
        applySeriesView();
      };
      th.addEventListener('click', go);
      th.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
    if (dirBtn) dirBtn.textContent = dirBtn.dataset.dir === 'desc' ? '▾' : '▴';
    applySeriesView();
  }

  /* -- collapsible groups (chapter blocks) -------------------------------- */
  $$('.grouphead').forEach((btn) => {
    btn.addEventListener('click', () => {
      const open = btn.getAttribute('aria-expanded') !== 'true';
      btn.setAttribute('aria-expanded', String(open));
      const panel = document.getElementById(btn.getAttribute('aria-controls'));
      if (panel) panel.hidden = !open;
    });
  });
  $$('[data-expand-all]').forEach((b) => b.addEventListener('click', () => {
    const open = b.dataset.expandAll === '1';
    $$('.grouphead').forEach((h) => {
      h.setAttribute('aria-expanded', String(open));
      const panel = document.getElementById(h.getAttribute('aria-controls'));
      if (panel) panel.hidden = !open;
    });
  }));

  /* -- manual search: what every source has for one chapter --------------- */
  function esc(v) { return String(v == null ? '' : v).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
  function renderReleases(list, downloadUrl) {
    if (!list.length) return '<p class="nolist">no source lists this chapter</p>';
    const rows = list.map((r) => {
      const cells = [
        `<td><b>${esc(r.source)}</b>${r.error ? `<div class="err small">${esc(r.error)}</div>` : ''}</td>`,
        `<td>${esc(r.title)}${r.note ? `<div class="note">${esc(r.note)}</div>` : ''}</td>`,
        `<td>${r.listed === false ? '<span class="nolist">not listed</span>' : esc(r.name || '')}</td>`,
        `<td class="muted">${esc(r.scanlator || '')}</td>`,
        `<td class="muted nowrap">${esc((r.uploaded || '').slice(0, 10))}</td>`,
        `<td class="act">${r.downloaded ? '<span class="on-disk">already downloaded</span> ' : ''}` +
          (r.listed === false ? ''
            : `<form method="post" action="${esc(downloadUrl)}"><input type="hidden" name="manga_id" value="${esc(r.mangaId)}">` +
              `<button class="small">Download</button></form>`) + '</td>',
      ];
      return `<tr>${cells.join('')}</tr>`;
    });
    return '<div class="tablewrap"><table class="list small"><thead><tr><th>Source</th><th>Entry title</th><th>Chapter name</th>' +
      '<th>Scanlator</th><th>Uploaded</th><th></th></tr></thead><tbody>' + rows.join('') + '</tbody></table></div>';
  }
  $$('button[data-manual]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const tr = btn.closest('tr');
      const open = btn.getAttribute('aria-expanded') === 'true';
      const existing = tr.nextElementSibling && tr.nextElementSibling.classList.contains('manualrow') ? tr.nextElementSibling : null;
      if (open) { if (existing) existing.remove(); btn.setAttribute('aria-expanded', 'false'); return; }
      btn.setAttribute('aria-expanded', 'true');
      const row = document.createElement('tr');
      row.className = 'manualrow';
      const cols = tr.children.length;
      row.innerHTML = `<td colspan="${cols}"><div class="manual"><div class="mhead">searching every source…</div></div></td>`;
      tr.after(row);
      const box = $('.manual', row);
      try {
        const r = await fetch(btn.dataset.manual, { headers: { Accept: 'application/json' } });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        const list = Array.isArray(d) ? d : (d.releases || d.items || []);
        box.innerHTML = '<div class="mhead"><span>What each source has for this chapter. A note means the automatic search would ' +
          'not use that entry; downloading it here overrides that.</span></div>' + renderReleases(list, btn.dataset.download);
      } catch (e) {
        box.innerHTML = `<div class="mhead err">could not fetch releases: ${esc(e.message || e)}</div>`;
      }
    });
  });

  /* -- description clamp ---------------------------------------------------- */
  $$('[data-clamp]').forEach((el) => {
    const btn = document.getElementById(el.dataset.clamp);
    if (!btn) return;
    el.classList.add('clamped');
    // only offer "more" when the text is actually cut
    if (el.scrollHeight <= el.clientHeight + 2) { btn.hidden = true; return; }
    btn.addEventListener('click', () => {
      const clamped = el.classList.toggle('clamped');
      btn.textContent = clamped ? 'more' : 'less';
      btn.setAttribute('aria-expanded', String(!clamped));
    });
  });

  /* -- log page: level filter + auto refresh ------------------------------ */
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
  $$('img.thumb, .poster .art img, img.bigcover').forEach((img) => {
    img.addEventListener('error', () => {
      const ph = document.createElement(img.classList.contains('bigcover') ? 'div' : 'span');
      ph.className = img.classList.contains('bigcover') ? 'bigcover none' : img.classList.contains('thumb') ? 'thumb none' : 'noart';
      ph.innerHTML = '<svg class="ic"><use href="#i-image"/></svg>' + (ph.className === 'noart' ? 'no cover' : '');
      img.replaceWith(ph);
    }, { once: true });
  });

  /* -- pages that reload themselves while something runs ------------------ */
  const main = $('main[data-reload]');
  if (main) {
    const ms = Number(main.dataset.reload) || 10000;
    setTimeout(() => {
      const a = document.activeElement;
      if (a && (a.tagName === 'INPUT' || a.tagName === 'SELECT' || a.tagName === 'TEXTAREA')) return;
      location.reload();
    }, ms);
  }

  /* -- lists page: show the fields for the chosen kind (kept from lists.html) */
  // lists.html keeps its own inline script; nothing to do here.
})();
