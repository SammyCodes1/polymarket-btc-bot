/* Dashboard front-end: Alpine.js component + Chart.js. No build step. */
function dash() {
  return {
    tab: 'overview', busy: false, offline: false,
    proc: {}, cfg: { editable: {}, display: {}, mode: '' }, state: { available: false },
    positions: [], unrealized: 0, stats: {}, statsMode: 'auto', trades: [], tradesTotal: 0,
    decisions: [], showNoise: false, logLines: [], logFollow: true, logFilter: '', logOpen: window.innerWidth >= 1024,
    form: {}, modal: { open: false }, share: { open: false, url: '', blob: null, name: '', canShare: false, canCopy: false }, closing: {}, toasts: [], chart: null, now: Date.now() / 1000,
    setup: { needs_setup: false, starter: {} }, needsSetup: false, setupSkipped: localStorage.getItem('setupSkipped') === '1',
    setupForm: { private_key: '', wallet_address: '', showKey: false, mode: 'paper', live_confirmation: '', live_ack: false, settings: {} },
    wallet: { replacing: false, key: '', addr: '', show: false },
    token: localStorage.getItem('dashToken') || '',
    tzLabel: (() => { try { return new Intl.DateTimeFormat([], { timeZoneName: 'short' }).formatToParts(new Date()).find(p => p.type === 'timeZoneName').value; } catch { return 'local time'; } })(),

    async init() {
      await this.loadSetup();
      await this.loadConfig();
      this.refreshAll();
      setInterval(() => this.refreshFast(), 4000);
      setInterval(() => this.refreshSlow(), 8000);
      setInterval(() => { this.now = Date.now() / 1000; }, 1000);
    },
    // ---------- data ----------
    async api(path, opts = {}) {
      const headers = { 'Content-Type': 'application/json' };
      if (this.token) headers['X-Dashboard-Token'] = this.token;
      const r = await fetch(path, { ...opts, headers });
      if (r.status === 401) {
        const t = prompt('This dashboard needs its access token (DASHBOARD_TOKEN):');
        if (t) { this.token = t; localStorage.setItem('dashToken', t); return this.api(path, opts); }
      }
      let j = {}; try { j = await r.json(); } catch {}
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      return j;
    },
    post(path, body = {}) { return this.api(path, { method: 'POST', body: JSON.stringify(body) }); },
    async refreshAll() { await Promise.all([this.refreshFast(), this.refreshSlow()]); },
    async refreshFast() {
      try {
        const [s, st, d, lg] = await Promise.all([this.api('/api/status'), this.api('/api/state'),
          this.api(`/api/decisions?limit=80&interesting=${!this.showNoise}`), this.api('/api/log?lines=200')]);
        this.offline = false;
        this.proc = s.process; this.cfg.mode = s.mode; this.cfg.live_armed = s.live_armed;
        if (s.needs_setup !== this.needsSetup) { this.needsSetup = s.needs_setup; if (s.needs_setup) this.loadSetup(); }
        this.state = st; this.decisions = d.decisions; this.logLines = lg.lines;
        if (this.logFollow) this.$nextTick(() => { const b = document.getElementById('logbox'); if (b) b.scrollTop = b.scrollHeight; });
      } catch (e) { this.offline = true; }
    },
    async refreshSlow() {
      try {
        const [p, t] = await Promise.all([this.api('/api/positions'), this.api('/api/trades?limit=300')]);
        this.positions = p.positions; this.unrealized = p.unrealized_total;
        this.trades = t.trades; this.tradesTotal = t.total;
        await this.refreshStats();
      } catch (e) { /* offline banner handled by fast loop */ }
    },
    async refreshStats() { try { this.stats = await this.api(`/api/stats?mode=${this.statsMode}`); this.drawChart(); } catch {} },
    async refreshDecisions() { try { this.decisions = (await this.api(`/api/decisions?limit=80&interesting=${!this.showNoise}`)).decisions; } catch {} },
    async loadConfig() {
      try {
        const c = await this.api('/api/config');
        this.cfg = { ...this.cfg, ...c };
        this.resetForm();
      } catch {}
    },
    // ---------- first-run setup & wallet ----------
    async loadSetup() {
      try {
        const st = await this.api('/api/setup'); this.setup = st; this.needsSetup = st.needs_setup;
        if (!Object.keys(this.setupForm.settings).length) { const f = {}; for (const [k, v] of Object.entries(st.starter || {})) f[k] = v.value; this.setupForm.settings = f; }
      } catch {}
    },
    showSetup() { return this.needsSetup && !(this.setupSkipped && this.setup.env_file_exists); },
    isKey(v) { return /^(0x)?[0-9a-fA-F]{64}$/.test((v || '').trim()); },
    isAddr(v) { return /^0x[0-9a-fA-F]{40}$/.test((v || '').trim()); },
    keyOk() { return this.isKey(this.setupForm.private_key); },
    addrOk() { return this.isAddr(this.setupForm.wallet_address); },
    setupReady() { const f = this.setupForm; if (!this.keyOk() || !this.addrOk()) return false;
      return f.mode === 'paper' || (f.live_confirmation.trim() === 'GO LIVE' && f.live_ack); },
    async saveSetup() {
      const f = this.setupForm;
      const body = { private_key: f.private_key.trim(), wallet_address: f.wallet_address.trim(), mode: f.mode, settings: f.settings, start: true };
      if (f.mode === 'live') { body.live_confirmation = f.live_confirmation.trim(); body.live_ack = f.live_ack === true; }
      this.busy = true;
      try {
        const r = await this.post('/api/setup', body);
        this.setupForm.private_key = ''; this.setupForm.showKey = false; this.setupForm.live_confirmation = '';
        this.needsSetup = r.needs_setup; this.setupSkipped = false; localStorage.removeItem('setupSkipped');
        await this.loadSetup(); await this.loadConfig(); this.tab = 'overview'; window.scrollTo(0, 0);
        if (r.started) this.toast(`Saved. Bot started in ${r.mode === 'LIVE' ? 'LIVE' : 'paper'} mode.`);
        else this.toast('Saved, but the bot did not start: ' + (r.start_error || 'unknown error'), 'err');
        await this.refreshAll();
      } catch (e) { this.toast(e.message, 'err'); } finally { this.busy = false; }
    },
    async skipSetup() {
      this.busy = true;
      try {
        if (!this.setup.env_file_exists) await this.post('/api/setup', { skip_wallet: true, mode: 'paper', settings: this.setupForm.settings, start: false });
        this.setupSkipped = true; localStorage.setItem('setupSkipped', '1');
        await this.loadSetup(); await this.loadConfig(); this.toast('Paper mode. Add your wallet any time in Settings → Wallet.');
      } catch (e) { this.toast(e.message, 'err'); } finally { this.busy = false; }
    },
    async replaceKey() {
      const body = { private_key: this.wallet.key.trim() }; if (this.wallet.addr.trim()) body.wallet_address = this.wallet.addr.trim();
      const r = await this.act(() => this.post('/api/wallet/replace', body));
      this.wallet = { replacing: false, key: '', addr: '', show: false };
      if (r) { await this.loadConfig(); this.toast('Key saved' + (r.restart_needed ? '. Restart the bot to use it.' : '.')); }
    },
    async removeKey() {
      const v = await this.confirm({ title: 'Remove private key', danger: true, ok: 'Remove key', phrase: 'REMOVE',
        body: 'Deletes the key from .env and switches to paper mode. The bot must be stopped. Your funds stay in your Polymarket wallet.' });
      if (!v) return;
      const r = await this.act(() => this.post('/api/wallet/remove', { confirmation: 'REMOVE' }), 'Key removed');
      if (r) { await this.loadConfig(); await this.loadSetup(); }
    },
    pendingCount() { return Object.keys((this.state && this.state.pending_redeem) || {}).length; },
    // ---------- controls ----------
    confirm(o) { return new Promise(res => { this.modal = { open: true, input: '', ok: 'Confirm', danger: false, phrase: '', ack: '', acked: false, ...o, resolve: res }; if (o.phrase) this.$nextTick(() => this.$refs.phraseInput && this.$refs.phraseInput.focus()); }); },
    modalOk() { if (this.modal.phrase && this.modal.input.trim().toUpperCase() !== this.modal.phrase) return; if (this.modal.ack && !this.modal.acked) return; const r = this.modal.resolve; const v = this.modal.input; this.modal.open = false; r && r(v || true); },
    toast(msg, kind = 'ok') { const id = Math.random(); this.toasts.push({ id, msg, kind }); setTimeout(() => this.toasts = this.toasts.filter(t => t.id !== id), 5000); },
    async act(fn, okMsg) { this.busy = true; try { const r = await fn(); if (okMsg) this.toast(okMsg); await this.refreshFast(); return r; } catch (e) { this.toast(e.message, 'err'); } finally { this.busy = false; } },
    async liveConfirm(verb) {
      if (this.cfg.mode !== 'LIVE') return true;
      return !!(await this.confirm({ title: `${verb} in LIVE mode?`, danger: true, ok: `${verb} live`,
        body: 'The bot is configured for <b>real-money trading</b>. It will place real orders from your wallet.' }));
    },
    async startBot() { if (!(await this.liveConfirm('Start'))) return; this.act(() => this.post('/api/bot/start', { confirm_live: true }), 'Bot started'); },
    async stopBot() {
      if (!(await this.confirm({ title: 'Stop the bot?', ok: 'Stop', body: 'Graceful stop: the bot finishes its current loop and exits. Open positions stay open (not sold). If it hasn\'t exited after 45 s it is force-killed.' }))) return;
      this.act(() => this.post('/api/bot/stop'), 'Stopping…');
    },
    async restartBot() { if (!(await this.liveConfirm('Restart'))) return; this.act(() => this.post('/api/bot/restart', { confirm_live: true }), 'Restarting…'); },
    async forceKill() {
      if (!(await this.confirm({ title: 'Force kill the bot?', danger: true, ok: 'Force kill', body: 'Ends the process immediately, even mid-order. Open positions stay open. Use Stop unless the bot is stuck.' }))) return;
      this.act(() => this.post('/api/bot/kill'), 'Bot process killed');
    },
    async haltBot() {
      const v = await this.confirm({ title: 'Emergency halt & flatten', danger: true, ok: 'Halt', phrase: 'HALT',
        body: 'Creates the <span class="font-mono">KILL</span> file. On its next loop the running bot <b>sells all open positions</b> at market and stops taking new trades. The halt is saved in state.json until you clear it.' });
      if (v) this.act(() => this.post('/api/bot/halt', { confirmation: 'HALT' }), 'KILL file created');
    },
    async clearHalt() {
      const v = await this.confirm({ title: 'Clear halt', ok: 'Clear halt', phrase: 'RESUME',
        body: 'Removes any KILL file and resets the halt flag in state.json (a drawdown halt also resets the peak to the current bankroll). The bot must be stopped. Only do this once you know why it halted.' });
      if (v) this.act(() => this.post('/api/bot/clear-halt', { confirmation: 'RESUME' }), 'Halt cleared');
    },
    // ---------- settings ----------
    keysIn(g) { return Object.keys(this.cfg.editable || {}).filter(k => this.cfg.editable[k].group === g); },
    resetForm() { const f = {}; for (const [k, s] of Object.entries(this.cfg.editable || {})) f[k] = s.value; this.form = f; },
    isPaperForm() { return !(this.form.DRY_RUN === false || this.form.DRY_RUN === 'false'); },
    isDirty(k) { const s = this.cfg.editable[k]; if (!s) return false; if (s.type === 'bool') return (this.form[k] === true || this.form[k] === 'true') !== s.value; return String(this.form[k]) !== '' && Number(this.form[k]) !== Number(s.value); },
    dirtyCount() { return Object.keys(this.cfg.editable || {}).filter(k => this.isDirty(k)).length; },
    hint(k) { return this.hintFor(k, this.form[k]); },
    hintFor(k, raw) {
      const v = Number(raw); if (raw === '' || raw == null || isNaN(v)) return '';
      if (/PRICE|BID/.test(k)) return Math.round(v * 100) + '¢';
      if (/PCT/.test(k)) return Math.round(v * 100) + '%';
      if (/SECS/.test(k)) return (v / 60).toFixed(1) + ' min';
      if (k === 'MAX_POSITION_USD') return '$' + v.toFixed(2);
      if (k === 'FAV_EDGE_EST') return (v * 100).toFixed(1) + '¢';
      return '';
    },
    async saveConfig(restart) {
      const values = {}; for (const k of Object.keys(this.cfg.editable)) if (this.isDirty(k)) values[k] = this.form[k];
      const body = { values, restart };
      if ('DRY_RUN' in values && !this.isPaperForm()) {
        const v = await this.confirm({ title: 'Switch to LIVE trading?', danger: true, ok: 'Switch to live', phrase: 'GO LIVE',
          ack: 'I understand this uses real money and I can lose all of it.',
          body: 'The bot will trade <b>real money</b> from your wallet the next time it starts. You can lose the full amount you put at risk.' });
        if (!v) return; body.live_confirmation = 'GO LIVE'; body.live_ack = true; body.confirm_live = true;
      } else if (restart && this.cfg.mode === 'LIVE' && this.proc.running) {
        if (!(await this.liveConfirm('Restart'))) return; body.confirm_live = true;
      }
      const r = await this.act(() => this.post('/api/config', body));
      if (r) { this.cfg = { ...this.cfg, ...r.config }; this.resetForm();
        this.toast(`Saved ${r.changed.length} setting(s)` + (r.restarting ? ' · restarting bot' : (restart && !this.proc.running ? ' · bot is stopped, start it to apply' : ''))); }
    },
    cfgVal(k) { const s = (this.cfg.editable || {})[k]; return s ? s.value : null; },
    // ---------- chart ----------
    drawChart() {
      const el = document.getElementById('pnlChart'); if (!el || !window.Chart) return;
      const pts = (this.stats.series || []).map(p => ({ x: p.t * 1000, y: p.pnl, trade: p.trade, slug: p.slug, reason: p.reason }));
      const last = pts.length ? pts[pts.length - 1].y : 0;
      const color = last >= 0 ? '52,211,153' : '248,113,113';
      if (this.chart) { const ds = this.chart.data.datasets[0]; ds.data = pts; ds.borderColor = `rgb(${color})`; ds._rgb = color; this.chart.update('none'); return; }
      const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const ctx = el.getContext('2d');
      this.chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [{ data: pts, _rgb: color, borderColor: `rgb(${color})`, borderWidth: 2, tension: 0.25, cubicInterpolationMode: 'monotone',
          pointRadius: c => c.raw && c.raw.trade !== undefined && pts.length < 40 ? 2.5 : 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: '#11151c',
          pointBackgroundColor: c => (c.raw && c.raw.trade < 0) ? '#f87171' : '#34d399', pointBorderWidth: 0,
          fill: { target: 'origin' },
          backgroundColor: c => { const { chartArea, ctx } = c.chart; const rgb = c.dataset._rgb; if (!chartArea) return `rgba(${rgb},.08)`; const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom); g.addColorStop(0, `rgba(${rgb},.20)`); g.addColorStop(1, `rgba(${rgb},0)`); return g; } }] },
        options: { responsive: true, maintainAspectRatio: false, animation: reduce ? false : { duration: 600, easing: 'easeOutQuart' }, interaction: { mode: 'nearest', intersect: false },
          plugins: { legend: { display: false }, tooltip: { backgroundColor: '#1c2230', borderColor: '#2a3343', borderWidth: 1, padding: 10, cornerRadius: 8,
            titleColor: '#e7eaf0', bodyColor: '#b5bdca', displayColors: false, titleFont: { family: 'Inter', weight: '600' }, bodyFont: { family: 'Inter' },
            callbacks: { label: c => [`Cumulative: ${this.sgnUsd(c.raw.y)}`].concat(c.raw.trade !== undefined ? [`Trade: ${this.sgnUsd(c.raw.trade)} (${c.raw.reason})`, c.raw.slug] : []) } } },
          scales: { x: { type: 'time', grid: { display: false }, ticks: { color: '#6f7a8c', maxTicksLimit: 5, maxRotation: 0, font: { family: 'Inter', size: 11 } }, border: { display: false } },
                    y: { grid: { color: 'rgba(148,163,184,.08)' }, ticks: { color: '#6f7a8c', maxTicksLimit: 5, font: { family: 'JetBrains Mono', size: 11 }, callback: v => '$' + v.toFixed(2) }, border: { display: false } } } }
      });
    },
    // ---------- manual close ("Close" button on a position) ----------
    closeState(p) { return this.closing[p.key] || {}; },
    closeDisabled(p) { const st = this.closeState(p).state; return st === 'closing' || st === 'closed' || !this.proc.running || this.secsLeft(p) <= 0; },
    closeTitle(p) { if (!this.proc.running) return 'Start the bot first: it does the selling'; if (this.secsLeft(p) <= 0) return 'Window ended: it settles at resolution';
      return 'Sell this position now at the best bid'; },
    closeEstimate(p) {
      const bid = p.bid == null ? null : Number(p.bid), sh = Number(p.shares) || 0, cost = Number(p.cost_usd) || 0;
      const fr = p.fee_rate == null ? 0.07 : Number(p.fee_rate), slip = Number(this.cfgVal('SLIPPAGE') ?? 0.02);
      if (bid == null || !isFinite(bid)) return { bid: null };
      const fee = fr * sh * bid * (1 - bid), proceeds = sh * bid - fee;
      return { bid, fee, proceeds, pnl: proceeds - cost, floor: Math.max(0.01, bid - slip) };
    },
    esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); },
    async closePosition(p) {
      if (this.closeDisabled(p)) return;
      const e = this.closeEstimate(p), live = this.cfg.mode === 'LIVE';
      const row = (k, v, cls = '') => `<dt>${k}</dt><dd class="${cls}">${v}</dd>`;
      const body = `The bot sells all of this position at market on its next loop (a few seconds), using its normal exit path. It won't re-enter this window afterwards.`
        + (live ? ' <b class="down">This is a real-money sell.</b>' : ' <span class="warn">Paper mode: simulated sell.</span>')
        + `<dl class="close-sum">${row('Market', this.esc(p.slug))}${row('Side', this.esc(p.side), p.side === 'UP' ? 't-up' : 't-down')}`
        + row('Shares', this.num(p.shares, 2)) + row('Best bid now', e.bid == null ? '—' : this.cents(e.bid))
        + row('Est. proceeds', e.bid == null ? '—' : this.usd(e.proceeds)) + row('Est. P&amp;L', e.bid == null ? '—' : this.sgnUsd(e.pnl), e.bid == null ? '' : this.pnlClass(e.pnl)) + '</dl>'
        + `<div class="mt-2 text-[11.5px] dim">Estimate after the taker fee. The real fill can be lower if the bid moves: it sells down to ${e.bid == null ? 'bid − slippage' : this.cents(e.floor)} at worst.</div>`;
      if (!(await this.confirm({ title: 'Close this position?', danger: true, ok: 'Close position', body }))) return;
      const key = p.key;
      this.closing = { ...this.closing, [key]: { state: 'closing' } };
      try {
        const r = await this.post('/api/positions/close', { key, token_id: String(p.token_id || ''), slug: p.slug });
        this.closing = { ...this.closing, [key]: { state: 'closing', id: r.request.id } };
        this.toast('Close requested. The bot sells on its next loop.');
        this.pollClose(key, r.request.id, p.slug);
      } catch (err) { this.closing = { ...this.closing, [key]: { state: 'error', msg: err.message } }; this.toast(err.message, 'err'); }
    },
    async pollClose(key, id, slug) {
      const t0 = Date.now(), done = (st, msg) => { this.closing = { ...this.closing, [key]: { state: st, id, msg } }; };
      const tick = async () => {
        if ((this.closing[key] || {}).id !== id) return;
        try {
          const [c, ps] = await Promise.all([this.api('/api/positions/close'), this.api('/api/positions')]);
          const res = (c.results || []).find(x => x.id === id);
          const stillOpen = (ps.positions || []).some(x => x.key === key);
          if (res && (res.status === 'closed' || res.status === 'partial')) {
            done('closed'); this.toast(`Closed ${this.shortSlug(slug)} · P&L ${this.sgnUsd(res.pnl)}`);
            setTimeout(() => this.refreshAll(), 1500); return;
          }
          if (res) { const msg = ({ not_open: 'Already closed', expired: 'Request expired: the bot did not pick it up in time', window_ended: 'Window ended: it settles at resolution' }[res.status] || 'Sell failed') + (res.note ? ` (${res.note})` : '');
            if (res.status === 'not_open') { done('closed'); setTimeout(() => this.refreshAll(), 1500); return; }
            done('error', msg); this.toast(msg, 'err'); return; }
          if (!stillOpen) { done('closed'); setTimeout(() => this.refreshAll(), 1500); return; }
        } catch (err) { /* transient; keep polling */ }
        if (Date.now() - t0 > 150000) { const m = 'No answer from the bot. Is it running?'; done('error', m); this.toast(m, 'err'); return; }
        setTimeout(tick, 1500);
      };
      setTimeout(tick, 1200);
    },
    // ---------- visual helpers ----------
    countUp(el, v, kind = 'usd') {
      const fmt = x => kind === 'hero' ? this.heroUsd(x) : kind === 'sgn' ? this.sgnUsd(x) : kind === 'sgnHero' ? this.sgnUsd(x) : this.usd(x);
      const n = Number(v);
      if (v === '' || v == null || !isFinite(n)) { el.textContent = '—'; el._v = undefined; return; }
      const from = el._v; el._v = n;
      const set = x => { if (kind === 'hero') el.innerHTML = fmt(x); else el.textContent = fmt(x); };
      const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (from === n) { set(n); return; }
      if (reduce) { set(n); return; }
      const start = from === undefined ? 0 : from, t0 = performance.now(), dur = from === undefined ? 1100 : 650;
      if (from !== undefined) { el.classList.remove('flash-up', 'flash-down'); void el.offsetWidth; el.classList.add(n > from ? 'flash-up' : 'flash-down'); }
      cancelAnimationFrame(el._raf);
      const step = t => { const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 4); set(start + (n - start) * e); if (k < 1) el._raf = requestAnimationFrame(step); else set(n); };
      el._raf = requestAnimationFrame(step);
    },
    heroUsd(v) { const n = Number(v); if (!isFinite(n)) return '—'; const [d, c] = Math.abs(n).toFixed(2).split('.');
      return (n < 0 ? '−' : '') + '$' + Number(d).toLocaleString('en-US') + '<span class="cents">.' + c + '</span>'; },
    statusPill() {
      if (this.state.available && this.state.halted) return { label: 'HALTED', cls: 'pill-red', pulse: true };
      return { LIVE: { label: 'LIVE', cls: 'pill-red', pulse: true }, PAPER: { label: 'PAPER', cls: 'pill-amber', pulse: true },
        LIVE_NOT_ARMED: { label: 'LIVE · not armed', cls: 'pill-orange', pulse: false } }[this.cfg.mode] || { label: '—', cls: 'pill-muted', pulse: false };
    },
    windowInfo() {
      const W = 900, now = this.now, start = Math.floor(now / W) * W, left = Math.max(0, start + W - now);
      const mx = Number(this.cfgVal('FAV_MAX_SECS_LEFT') ?? 900), mn = Number(this.cfgVal('FAV_MIN_SECS_LEFT') ?? 600);
      const open = left <= mx && left > mn;
      const nextIn = open ? left - mn : (left > mx ? left - mx : left + (W - mx));
      const t = s => new Date(s * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
      return { left, pct: left / W, open, nextIn, range: `${t(start)}–${t(start + W)}`, mn, mx };
    },
    secsLeft(p) { return p.end_ts ? Math.round(Number(p.end_ts) - this.now) : p.secs_left; },
    posTrack(p) {
      const basis = (p.cost_usd && p.shares) ? Number(p.cost_usd) / Number(p.shares) : Number(p.entry_price);
      const slp = Number(this.cfgVal('STOP_LOSS_PCT') ?? 0.6), sl = basis * (1 - slp), tp = Number(this.cfgVal('TAKE_PROFIT_MIN_BID') ?? 0.97);
      const e = Number(p.entry_price), m = p.mark == null ? null : Number(p.mark), up = m != null && m >= e;
      const hasSL = slp > 0;
      // zoom the bar to the range that matters (lowest of entry/now/SL minus a margin, up to $1)
      const lows = [e, tp]; if (m != null) lows.push(m); if (hasSL) lows.push(sl);
      const lo = Math.max(0, Math.min(...lows) - 0.08), hi = 1;
      const cl = x => Math.max(0, Math.min(100, (x - lo) / (hi - lo) * 100));
      const mX = m == null ? null : cl(m);
      return { sl, tp, e, m, up, hasSL, slX: cl(sl), tpX: cl(tp), eX: cl(e), mX, bX: mX == null ? null : Math.max(12, Math.min(88, mX)),
        fillL: m == null ? cl(e) : Math.min(cl(e), cl(m)), fillW: m == null ? 0 : Math.abs(cl(m) - cl(e)) };
    },
    tradeRowClass(t) { if (this.isFalse(t.ok) || t.action === 'BUY') return ''; const p = Number(t.pnl); return t.pnl === '' || t.pnl == null ? '' : (p > 0 ? 'win' : 'loss'); },
    // ---------- share a trade as an image card ----------
    tradeInfo(t) {
      const same = r => r.slug === t.slug && r.side === t.side && r.mode === t.mode && !this.isFalse(r.ok);
      const ts = r => new Date(r.time).getTime() || 0;
      let buy = null, close = null;
      if (t.action === 'BUY') {
        buy = t;
        close = this.trades.filter(r => same(r) && r.action !== 'BUY' && ts(r) >= ts(t)).sort((a, b) => ts(a) - ts(b))[0] || null;
      } else {
        close = t;
        buy = this.trades.filter(r => same(r) && r.action === 'BUY' && ts(r) <= ts(t)).sort((a, b) => ts(b) - ts(a))[0] || null;
      }
      const shares = Number((buy || close).shares), side = t.side, slug = t.slug;
      let stake = buy ? Number(buy.usd) : NaN, entry = buy ? Number(buy.price) : NaN;
      if (!buy && close && isFinite(Number(close.pnl))) { stake = Number(close.usd) - Number(close.pnl); entry = stake / shares; }
      const info = { slug, side, shares, stake, entry, mode: t.mode, opened: buy ? buy.time : null };
      const sm = /btc-updown-(\d+)m-(\d+)/.exec(slug || ''); const winEnd = sm ? (+sm[2] + (+sm[1]) * 60) * 1000 : null;
      info.openedT = buy ? new Date(buy.time).getTime() : null; info.winStartT = sm ? +sm[2] * 1000 : null; info.winEndT = winEnd;
      if (close) {
        const pnl = Number(close.pnl); info.status = pnl > 0 ? 'WIN' : pnl < 0 ? 'LOSS' : 'EVEN';
        info.pnl = pnl; info.pct = stake > 0 ? pnl / stake : null; info.exit = Number(close.price);
        info.exitLabel = close.action === 'RESOLVE' ? 'Settled at' : 'Sold at'; info.closed = close.time;
        info.isResolve = close.action === 'RESOLVE';
        const ct = new Date(close.time).getTime(); info.exitT = (info.isResolve && winEnd) ? Math.min(ct, winEnd) : ct;
        info.how = close.action === 'RESOLVE' ? 'Held to settlement' : ({ spot_exit: 'Early exit · BTC moved against it', stop_loss: 'Stop loss', take_profit: 'Take profit' }[close.reason] || 'Sold early');
      } else {
        const pos = this.positions.find(x => x.slug === slug);
        info.status = 'OPEN'; info.pnl = pos && pos.unrealized_pnl != null ? Number(pos.unrealized_pnl) : null;
        info.pct = pos && pos.unrealized_pct != null ? Number(pos.unrealized_pct) : null;
        info.exit = pos && pos.mark != null ? Number(pos.mark) : null; info.exitLabel = 'Current bid';
        info.closed = winEnd ? new Date(winEnd).toISOString() : (pos && pos.end_ts ? new Date(pos.end_ts * 1000).toISOString() : null); info.how = 'Open · waiting for settlement';
        info.exitT = winEnd ? Math.min(Date.now(), winEnd) : Date.now();
      }
      return info;
    },
    async openShare(t) {
      const tok = (this._shareTok = (this._shareTok || 0) + 1);
      this.revokeShare();
      this.share = { open: true, url: '', blob: null, name: '', canShare: false, canCopy: false };
      try {
        const info = this.tradeInfo(t);
        try { info.series = await Promise.race([this.api(`/api/trade-series?slug=${encodeURIComponent(info.slug)}&side=${encodeURIComponent(info.side)}`), new Promise((_, rj) => setTimeout(() => rj(new Error('timeout')), 4000))]); } catch { info.series = null; }
        const blob = await this.renderTradeCard(info);
        if (tok !== this._shareTok || !this.share.open) return;
        let canShare = false;
        try { canShare = !!(window.isSecureContext && navigator.share && navigator.canShare && navigator.canShare({ files: [new File([blob], 'x.png', { type: 'image/png' })] })); } catch {}
        const canCopy = !!(window.isSecureContext && navigator.clipboard && window.ClipboardItem);
        const d = new Date(info.opened || info.closed || Date.now()), z = n => String(n).padStart(2, '0');
        const name = `btc-bot-${info.status.toLowerCase()}-${info.side.toLowerCase()}-${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}-${z(d.getHours())}${z(d.getMinutes())}.png`;
        this.share = { open: true, url: URL.createObjectURL(blob), blob, name, canShare, canCopy };
      } catch (e) { this.share.open = false; this.toast('Could not build the trade image: ' + (e.message || e), 'err'); }
    },
    revokeShare() { if (this.share && this.share.url) URL.revokeObjectURL(this.share.url); },
    closeShare() { this._shareTok = (this._shareTok || 0) + 1; this.revokeShare(); this.share = { open: false, url: '', blob: null, name: '', canShare: false, canCopy: false }; },
    downloadShare() {
      if (!this.share.blob) return;
      const a = document.createElement('a'); a.href = this.share.url; a.download = this.share.name; document.body.appendChild(a); a.click(); a.remove();
      this.toast('Image saved.');
    },
    async nativeShare() {
      if (!this.share.blob) return;
      try { await navigator.share({ files: [new File([this.share.blob], this.share.name, { type: 'image/png' })], title: 'BTC Up/Down Bot trade' }); }
      catch (e) { if (e && e.name !== 'AbortError') this.toast('Sharing failed: ' + (e.message || e), 'err'); }
    },
    async copyShare() {
      if (!this.share.blob) return;
      try { await navigator.clipboard.write([new ClipboardItem({ 'image/png': this.share.blob })]); this.toast('Image copied.'); }
      catch (e) { this.toast('Copy failed: ' + (e.message || e), 'err'); }
    },
    async renderTradeCard(i) {
      try { await Promise.all([document.fonts.load('700 40px Inter'), document.fonts.load('500 24px Inter'), document.fonts.load('600 24px "JetBrains Mono"')]); } catch {}
      const W = 1080, CHH = 440, H = 1350 + CHH + 24 - 36 - 16, cv = document.createElement('canvas'); cv.width = W; cv.height = H;
      const g = cv.getContext('2d'), F = 'Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
      const C = { bg: '#0b0e13', surf: '#11151c', surf2: '#161b24', line: '#1f2632', line2: '#2a3343', text: '#e7eaf0', muted: '#9aa3b2', dim: '#6f7a8c',
        up: '#34d399', down: '#f87171', warn: '#e3b341', accent: '#6c8cff' };
      const tone = i.status === 'WIN' ? C.up : i.status === 'LOSS' ? C.down : i.status === 'OPEN' ? C.warn : C.muted;
      const rgba = (hex, a) => { const n = parseInt(hex.slice(1), 16); return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`; };
      const rr = (x, y, w, h, r) => { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); };
      const T = (s, x, y, size, color, weight = 500, align = 'left', fam = F, maxW) => { g.font = `${weight} ${size}px ${fam}`; g.fillStyle = color; g.textAlign = align; g.textBaseline = 'alphabetic';
        if (maxW) { while (g.measureText(s).width > maxW && size > 14) { size -= 1; g.font = `${weight} ${size}px ${fam}`; } } g.fillText(s, x, y); };
      const spaced = (s, x, y, size, color, weight, ls, align = 'left') => { g.font = `${weight} ${size}px ${F}`; g.fillStyle = color; g.textBaseline = 'alphabetic'; g.textAlign = 'left';
        const w = [...s].reduce((a, ch) => a + g.measureText(ch).width + ls, -ls); let cx = align === 'right' ? x - w : align === 'center' ? x - w / 2 : x;
        for (const ch of s) { g.fillText(ch, cx, y); cx += g.measureText(ch).width + ls; } return w; };
      const money = n => (n == null || !isFinite(n)) ? '—' : '$' + Math.abs(n).toFixed(2);
      const sgn = n => n > 0 ? '+' : n < 0 ? '−' : '';
      const cents = n => { if (n == null || !isFinite(n)) return '—'; const c = Math.round(n * 1000) / 10; return (Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)) + '¢'; };
      const when = iso => { if (!iso) return '—'; const d = new Date(iso); if (isNaN(d)) return '—';
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' · ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }); };

      // background + glow
      g.fillStyle = C.bg; g.fillRect(0, 0, W, H);
      const glow = g.createRadialGradient(W / 2, 470, 20, W / 2, 470, 760); glow.addColorStop(0, rgba(tone, 0.22)); glow.addColorStop(1, rgba(tone, 0));
      g.fillStyle = glow; g.fillRect(0, 0, W, H);
      rr(40, 40, W - 80, H - 80, 44); g.fillStyle = rgba('#11151c', 0.92); g.fill(); g.lineWidth = 2; g.strokeStyle = C.line2; g.stroke();
      const X0 = 90, X1 = W - 90, IW = X1 - X0;

      // header
      g.beginPath(); g.arc(X0 + 36, 140, 36, 0, Math.PI * 2); g.fillStyle = C.accent; g.fill();
      T('₿', X0 + 36, 158, 46, '#0a1030', 700, 'center');
      T('BTC Up/Down Bot', X0 + 94, 138, 40, '#ffffff', 700, 'left', F, 520);
      T('Polymarket · 15m favorite strategy', X0 + 94, 174, 24, C.dim, 500, 'left', F, 520);
      const live = i.mode !== 'PAPER', mlabel = live ? 'LIVE' : 'PAPER', mcol = live ? C.down : C.warn;
      g.font = `700 22px ${F}`; const mw = spaced(mlabel, 0, -999, 22, mcol, 700, 2) + 44;
      rr(X1 - mw, 112, mw, 52, 26); g.fillStyle = rgba(mcol, 0.14); g.fill(); g.lineWidth = 2; g.strokeStyle = rgba(mcol, 0.4); g.stroke();
      spaced(mlabel, X1 - mw + 22, 147, 22, mcol, 700, 2);
      g.fillStyle = C.line; g.fillRect(X0, 218, IW, 2);

      // market + side
      const mkt = this.shortSlug(i.slug);
      T(mkt, X0, 300, 58, '#ffffff', 700, 'left', F, IW - 230);
      const sd = i.slug && /btc-updown-\d+m-(\d+)/.exec(i.slug); const sdate = sd ? new Date(+sd[1] * 1000).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }) : '';
      T(sdate, X0, 344, 28, C.muted, 500);
      g.font = `800 38px ${F}`; const up = i.side === 'UP', sc = up ? C.up : C.down, bw = Math.ceil(g.measureText(i.side).width) + 108, bh = 74, bx = X1 - bw, by = 238;
      rr(bx, by, bw, bh, 20); g.fillStyle = rgba(sc, 0.14); g.fill(); g.lineWidth = 2; g.strokeStyle = rgba(sc, 0.45); g.stroke();
      g.beginPath(); if (up) { g.moveTo(bx + 34, by + 48); g.lineTo(bx + 54, by + 48); g.lineTo(bx + 44, by + 27); } else { g.moveTo(bx + 34, by + 27); g.lineTo(bx + 54, by + 27); g.lineTo(bx + 44, by + 48); }
      g.closePath(); g.fillStyle = sc; g.fill();
      T(i.side, bx + 74, by + 50, 38, sc, 800);

      // result panel
      const py = 392, ph = 340;
      rr(X0, py, IW, ph, 32); g.fillStyle = rgba(tone, 0.09); g.fill(); g.lineWidth = 2; g.strokeStyle = rgba(tone, 0.38); g.stroke();
      g.font = `800 30px ${F}`; const sw = spaced(i.status, 0, -999, 30, tone, 800, 3) + 48;
      rr(X0 + 40, py + 40, sw, 60, 30); g.fillStyle = rgba(tone, 0.18); g.fill(); spaced(i.status, X0 + 64, py + 80, 30, tone, 800, 3);
      T(i.how || '', X1 - 40, py + 80, 25, C.muted, 500, 'right', F, IW - sw - 140);
      const has = i.pnl != null && isFinite(i.pnl);
      const big = has ? sgn(i.pnl) + money(i.pnl) : '—';
      T(big, X0 + 40, py + 242, 140, has ? tone : C.muted, 800, 'left', F, IW - 80);
      const pctTxt = i.pct != null && isFinite(i.pct) ? `${sgn(i.pct)}${Math.abs(i.pct * 100).toFixed(1)}% ${i.status === 'OPEN' ? 'unrealized' : 'return'}` : (i.status === 'OPEN' ? 'Awaiting settlement' : '');
      T(pctTxt, X0 + 40, py + 304, 40, has ? tone : C.muted, 600);
      if (i.status !== 'OPEN') T('Net P&L after fees', X1 - 40, py + 304, 25, C.dim, 500, 'right');

      // price wave chart
      this.drawWave(g, { C, F, rr, T, spaced, rgba, cents, tone, X0, X1, IW, y: py + ph + 24, h: CHH }, i);

      // stats grid
      const cw = (IW - 20) / 2, ch = 118, gy = py + ph + 24 + CHH + 24;
      const cells = [
        ['Entry price', cents(i.entry)], ['Shares', isFinite(i.shares) ? i.shares.toFixed(2) : '—'],
        ['Stake', money(i.stake)], [i.exitLabel, cents(i.exit)],
        ['Opened', when(i.opened)], [i.status === 'OPEN' ? 'Settles' : 'Closed', when(i.closed)],
      ];
      cells.forEach((c, k) => { const x = X0 + (k % 2) * (cw + 20), y = gy + Math.floor(k / 2) * (ch + 18);
        rr(x, y, cw, ch, 24); g.fillStyle = C.surf2; g.fill(); g.lineWidth = 1.5; g.strokeStyle = C.line; g.stroke();
        spaced(String(c[0]).toUpperCase(), x + 30, y + 44, 21, C.dim, 600, 2);
        T(c[1], x + 30, y + 98, k >= 4 ? 40 : 50, C.text, 700, 'left', F, cw - 60); });

      // footer
      const fy = gy + 3 * ch + 2 * 18 + 28;
      g.fillStyle = C.line; g.fillRect(X0, fy, IW, 2);
      T('Polymarket BTC bot', X0, fy + 52, 28, C.muted, 600);
      T(new Date().toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' }), X1, fy + 52, 24, C.dim, 500, 'right');
      return new Promise((res, rej) => cv.toBlob(b => b ? res(b) : rej(new Error('canvas export failed')), 'image/png'));
    },
    // ---------- price wave on the share card ----------
    drawWave(g, u, i) {
      const { C, F, rr, T, spaced, rgba, cents, tone, X0, X1, IW } = u, cy = u.y, CH = u.h;
      rr(X0, cy, IW, CH, 32); g.fillStyle = C.surf2; g.fill(); g.lineWidth = 1.5; g.strokeStyle = C.line; g.stroke();
      spaced(`PRICE · ${i.side} OUTCOME`, X0 + 34, cy + 50, 21, C.dim, 600, 2);
      const hm = ms => { const d = new Date(ms); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }); };
      // ---- assemble the series: recorded points only (+ the real entry and exit fills)
      const xT0 = i.exitT, eT = i.openedT || null, ent = Number(i.entry), ex = i.exit != null && isFinite(i.exit) ? Number(i.exit) : null;
      if (!isFinite(ent) || !xT0) { T('Price history not available', X0 + IW / 2, cy + CH / 2 + 8, 28, C.dim, 500, 'center'); return; }
      const xT = Math.max(xT0, (eT || 0) + 1000);
      const rec = ((i.series && i.series.points) || []).map(p => [p[0] * 1000, Number(p[1])]).filter(p => isFinite(p[0]) && isFinite(p[1]) && p[0] <= xT - 3000 && (!eT || Math.abs(p[0] - eT) > 5000));
      const pts = rec.slice();
      if (eT) pts.push([eT, ent]);
      if (ex != null) pts.push([xT, ex]);
      pts.sort((a, b) => a[0] - b[0]);
      if (pts.length < 2) { T('Price history not available', X0 + IW / 2, cy + CH / 2 + 8, 28, C.dim, 500, 'center'); return; }
      let tA = pts[0][0], tB = pts[pts.length - 1][0];
      if (tB - tA < 30000) tA = tB - 30000;
      if (eT && eT - tA < (tB - tA) * 0.05) tA = eT - (tB - tA) * 0.06;
      const PL = X0 + 36, PR = X1 - 36 - 84, PT = cy + 96, PB = cy + CH - 64;
      let lo = Math.min(...pts.map(p => p[1]), ent), hi = Math.max(...pts.map(p => p[1]), ent);
      const rg = Math.max(hi - lo, 0.12); lo = Math.max(0, lo - rg * 0.42); hi = Math.min(1.06, hi + rg * 0.38);
      const X = t => PL + (t - tA) / (tB - tA) * (PR - PL), Y = v => PB - (v - lo) / (hi - lo) * (PB - PT);
      // ---- grid + y labels
      const rng = hi - lo, step = rng > 0.55 ? 0.2 : rng > 0.28 ? 0.1 : rng > 0.12 ? 0.05 : 0.02;
      g.save(); g.lineWidth = 1.5; g.font = `500 20px ${F}`; g.textAlign = 'left'; g.textBaseline = 'middle';
      for (let v = Math.ceil(lo / step - 1e-9) * step; v <= Math.min(hi, 1.0001); v += step) {
        const y = Y(v); if (y < PT - 8 || y > PB + 2) continue;
        const xy = ex != null ? Y(ex) : null;
        g.strokeStyle = 'rgba(255,255,255,0.06)'; g.beginPath(); g.moveTo(PL, y); g.lineTo(PR, y); g.stroke();
        if (xy != null && Math.abs(y - xy) < 16) continue; g.fillStyle = C.dim; g.fillText(cents(Math.round(v * 100) / 100), PR + 16, y);
      }
      g.restore();
      g.fillStyle = 'rgba(255,255,255,0.10)'; g.fillRect(PL, PB, PR - PL, 2);
      // ---- split into runs of recorded points; long gaps are drawn dotted (not recorded)
      const runs = []; let cur = [];
      pts.forEach(p => { if (cur.length && p[0] - cur[cur.length - 1][0] > 90000) { runs.push(cur); cur = []; } cur.push(p); }); runs.push(cur);
      const sp = runs.map(r => r.map(p => [X(p[0]), Y(p[1])]));
      const curve = (a, move) => {
        if (move) g.moveTo(a[0][0], a[0][1]); else g.lineTo(a[0][0], a[0][1]);
        if (a.length < 2) return;
        const n = a.length, d = [], m = [];
        for (let k = 0; k < n - 1; k++) { const dx = a[k + 1][0] - a[k][0] || 1e-6; d.push((a[k + 1][1] - a[k][1]) / dx); }
        m[0] = d[0]; m[n - 1] = d[n - 2];
        for (let k = 1; k < n - 1; k++) m[k] = d[k - 1] * d[k] <= 0 ? 0 : (d[k - 1] + d[k]) / 2;
        for (let k = 0; k < n - 1; k++) { if (d[k] === 0) { m[k] = 0; m[k + 1] = 0; continue; }
          const a1 = m[k] / d[k], b1 = m[k + 1] / d[k], s = a1 * a1 + b1 * b1; if (s > 9) { const tt = 3 / Math.sqrt(s); m[k] = tt * a1 * d[k]; m[k + 1] = tt * b1 * d[k]; } }
        for (let k = 0; k < n - 1; k++) { const dx = (a[k + 1][0] - a[k][0]) / 3;
          g.bezierCurveTo(a[k][0] + dx, a[k][1] + m[k] * dx, a[k + 1][0] - dx, a[k + 1][1] - m[k + 1] * dx, a[k + 1][0], a[k + 1][1]); }
      };
      g.save(); g.beginPath(); g.rect(PL - 6, PT - 30, PR - PL + 12, PB - PT + 30); g.clip();
      // area fill
      g.beginPath(); sp.forEach((a, k) => curve(a, k === 0));
      const last = sp[sp.length - 1][sp[sp.length - 1].length - 1], first = sp[0][0];
      g.lineTo(last[0], PB); g.lineTo(first[0], PB); g.closePath();
      const gr = g.createLinearGradient(0, PT - 20, 0, PB); gr.addColorStop(0, rgba(tone, 0.34)); gr.addColorStop(1, rgba(tone, 0.02)); g.fillStyle = gr; g.fill();
      // entry price dashed reference
      g.strokeStyle = 'rgba(231,234,240,0.38)'; g.lineWidth = 2; g.setLineDash([12, 10]); g.beginPath(); g.moveTo(PL, Y(ent)); g.lineTo(PR, Y(ent)); g.stroke(); g.setLineDash([]);
      // line: solid for recorded runs, dotted across gaps
      g.lineJoin = 'round'; g.lineCap = 'round'; g.strokeStyle = tone; g.lineWidth = 5.5;
      sp.forEach(a => { if (a.length < 2) return; g.beginPath(); curve(a, true); g.stroke(); });
      g.lineWidth = 5; g.setLineDash([0.1, 13]); g.strokeStyle = rgba(tone, 0.95);
      for (let k = 0; k < sp.length - 1; k++) { const a = sp[k][sp[k].length - 1], b = sp[k + 1][0]; g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke(); }
      g.setLineDash([]); g.restore();
      const gaps = runs.length > 1;
      if (gaps) T('dotted = price not recorded', X1 - 34, cy + 50, 20, C.dim, 500, 'right');
      // ---- markers + pills
      const pill = (label, cx, y, col, textCol, right, rx) => {
        g.font = `700 25px ${F}`; const w = Math.ceil(spacedW(label)) + 36, h = 46;
        const x = rx != null ? rx - w : right ? Math.max(PL - 10, Math.min(cx + 12 - w, PR + 12 - w)) : Math.max(PL - 10, Math.min(cx - w / 2, PR + 12 - w));
        rr(x, y, w, h, 23); g.fillStyle = '#0b0e13'; g.fill(); g.fillStyle = rgba(col, 0.2); g.fill(); g.lineWidth = 2; g.strokeStyle = rgba(col, 0.75); g.stroke();
        spaced(label, x + 18, y + 31, 25, textCol, 700, 0.5);
      };
      const spacedW = s => { g.font = `700 25px ${F}`; return [...s].reduce((a, ch) => a + g.measureText(ch).width + 0.5, 0); };
      const dot = (x, y, col, r, halo) => {
        if (halo) { g.beginPath(); g.arc(x, y, r + 12, 0, Math.PI * 2); g.fillStyle = rgba(col, 0.22); g.fill(); }
        g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fillStyle = col; g.fill(); g.lineWidth = 4; g.strokeStyle = '#0b0e13'; g.stroke();
        g.beginPath(); g.arc(x, y, r + 2, 0, Math.PI * 2); g.lineWidth = 2.5; g.strokeStyle = 'rgba(255,255,255,0.9)'; g.stroke();
      };
      const ey = Y(ent), ph2 = 46, gap = 20;
      let exY = null, exX = null;
      if (ex != null) { exX = X(xT); exY = Y(ex); }
      const prevY = (() => { const a = sp[sp.length - 1]; return a.length > 1 ? a[a.length - 2][1] : (sp.length > 1 ? sp[sp.length - 2][sp[sp.length - 2].length - 1][1] : exY); })();
      if (eT) {
        const ex0 = X(eT);
        // entry pill goes above unless it would leave the card; line rises into entry from below, so above is clear
        let py2 = ey - gap - ph2; if (py2 < cy + 62) py2 = ey + gap;
        dot(ex0, ey, C.accent, 11, false);
        pill(`BUY ${cents(ent)}`, ex0, py2, C.accent, '#c4d0ff');
      }
      if (ex != null) {
        const open = i.status === 'OPEN', lab = (open ? 'NOW ' : (i.isResolve ? 'SETTLED ' : 'SOLD ')) + cents(ex);
        const descending = prevY != null && prevY < exY - 2 ? false : (prevY != null && prevY > exY + 2);
        let py2 = descending ? exY + gap : exY - gap - ph2;
        let rx = null;
        if (py2 < cy + 62) py2 = exY + gap;
        if (py2 + ph2 > PB + 40) { if (descending) { py2 = exY - ph2 / 2; rx = exX - 26; } else py2 = exY - gap - ph2; }
        dot(exX, exY, tone, 12, open);
        pill(lab, exX, py2, tone, tone === C.muted ? C.text : tone, true, rx);
      }
      // ---- time axis
      T(hm(tA), PL, PB + 42, 22, C.dim, 500, 'left');
      T(hm(tB) + (i.status === 'OPEN' ? ' · now' : ''), PR, PB + 42, 22, C.dim, 500, 'right');
      if (eT && X(eT) - PL > 150 && PR - X(eT) > 150) T(hm(eT), X(eT), PB + 42, 22, C.accent, 600, 'center');
    },
    dayChange() { const s = this.state; if (!s.available || !s.day_start_bankroll_usd) return null; return Number(s.bankroll_usd) - Number(s.day_start_bankroll_usd); },
    // ---------- formatting ----------
    usd(v) { const n = Number(v); return isFinite(n) && v !== '' && v != null ? '$' + n.toFixed(2) : '—'; },
    sgnUsd(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null) return '—'; return (n > 0 ? '+' : n < 0 ? '−' : '') + '$' + Math.abs(n).toFixed(2); },
    sgnPct(v) { const n = Number(v) * 100; return (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n).toFixed(1) + '%'; },
    pct(v) { return v == null ? '—' : Math.round(v * 100) + '%'; },
    pctOf(a, b) { if (!b) return '—'; const n = 100 * a / b; return (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n).toFixed(1) + '%'; },
    cents(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null) return '—'; const c = Math.round(n * 1000) / 10; return (Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)) + '¢'; },
    num(v, d) { const n = Number(v); return isFinite(n) && v !== '' ? n.toFixed(d) : '—'; },
    pnlClass(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null || n === 0) return 't-flat'; return n > 0 ? 't-up' : 't-down'; },
    isFalse(v) { return String(v).toLowerCase() === 'false'; },
    actClass(t) { if (this.isFalse(t.ok)) return 'act-fail'; if (t.action === 'BUY') return 'act-buy'; const p = Number(t.pnl); return p > 0 ? 'act-win' : 'act-loss'; },
    fmtTime(iso) { if (!iso) return ''; const d = new Date(iso); if (isNaN(d)) return iso; const today = new Date().toDateString() === d.toDateString();
      return (today ? '' : d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ') + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }); },
    shortSlug(s) { if (!s) return ''; const m = /btc-updown-(\d+m)-(\d+)/.exec(s); if (!m) return s; const d = new Date(+m[2] * 1000);
      return `BTC ${m[1]} · ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}`; },
    clock(s) { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); },
    dur(s) { if (s == null) return '—'; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`; },
    ago(ts) { if (!ts) return '—'; const s = Date.now() / 1000 - ts; return s < 60 ? Math.round(s) + 's ago' : s < 3600 ? Math.round(s / 60) + 'm ago' : Math.round(s / 3600) + 'h ago'; },
    heartbeatStale() { return this.proc.last_log_at && (Date.now() / 1000 - this.proc.last_log_at) > 90; },
    modeLabel() { return { LIVE: 'LIVE', PAPER: 'PAPER', LIVE_NOT_ARMED: 'LIVE · not armed' }[this.cfg.mode] || '—'; },
    modeClass() { return { LIVE: 'pill-red', PAPER: 'pill-amber', LIVE_NOT_ARMED: 'pill-orange' }[this.cfg.mode] || 'pill-muted'; },
    verdictDot(v) { if (v === 'ENTER') return 'bg-[#34d399]'; if (v === 'skip:risk') return 'bg-[#e3b341]'; if (/edge|price|spread/.test(v || '')) return 'bg-[#6c8cff]'; return 'bg-slate-600'; },
    verdictLabel(v) { const m = { 'ENTER': 'Entered', 'skip:fav_price': 'Skip · favorite outside price band', 'skip:fav_time': 'Skip · outside entry time window', 'skip:insufficient_edge': 'Skip · no edge after fee',
      'skip:wide_spread': 'Skip · spread too wide', 'skip:risk': 'Skip · blocked by risk limits', 'skip:no_book': 'Skip · no order book', 'skip:research_stale': 'Skip · news research stale',
      'skip:no_fresh_price_data': 'Skip · no fresh BTC price', 'skip:not_accepting_orders': 'Skip · market not accepting orders', 'skip:news_against': 'Skip · news against', 'skip:fav_duration': 'Skip · not a 15m market' };
      return m[v] || (v || '').replace('skip:', 'Skip · ').replace(/_/g, ' '); },
    filteredLog() { if (!this.logFilter) return this.logLines; const re = new RegExp(this.logFilter); return this.logLines.filter(l => re.test(l)); },
    logClass(l) { if (/ ERROR /.test(l)) return 'text-red-300'; if (/ WARNING /.test(l)) return 'text-amber-300'; if (/ ENTER | CLOSED /.test(l)) return 'text-emerald-300'; if (/HALT/.test(l)) return 'text-red-300'; return 'text-slate-400'; },
  };
}
