/* Dashboard front-end: Alpine.js component + Chart.js. No build step. */
function dash() {
  return {
    tab: 'overview', busy: false, offline: false,
    proc: {}, cfg: { editable: {}, display: {}, mode: '' }, state: { available: false },
    positions: [], unrealized: 0, stats: {}, statsMode: 'auto', trades: [], tradesTotal: 0,
    decisions: [], showNoise: false, logLines: [], logFollow: true, logFilter: '',
    form: {}, modal: { open: false }, closing: {}, toasts: [], chart: null, now: Date.now() / 1000,
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
      const color = last >= 0 ? '46,242,164' : '255,77,125';
      if (this.chart) { const ds = this.chart.data.datasets[0]; ds.data = pts; ds.borderColor = `rgb(${color})`; ds._rgb = color; this.chart.update('none'); return; }
      const glow = { id: 'glow', beforeDatasetDraw(c) { const x = c.ctx; x.save(); x.shadowColor = `rgba(${c.data.datasets[0]._rgb},.55)`; x.shadowBlur = 14; x.shadowOffsetY = 4; },
        afterDatasetDraw(c) { c.ctx.restore(); } };
      const reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const ctx = el.getContext('2d');
      this.chart = new Chart(ctx, {
        type: 'line', plugins: [glow],
        data: { datasets: [{ data: pts, _rgb: color, borderColor: `rgb(${color})`, borderWidth: 2.5, tension: 0.4, cubicInterpolationMode: 'monotone',
          pointRadius: c => c.raw && c.raw.trade !== undefined ? 3 : 0, pointHoverRadius: 6, pointHoverBorderWidth: 3, pointHoverBorderColor: '#0b0f25',
          pointBackgroundColor: c => (c.raw && c.raw.trade < 0) ? '#ff4d7d' : '#2ef2a4', pointBorderWidth: 0,
          fill: { target: 'origin' },
          backgroundColor: c => { const { chartArea, ctx } = c.chart; const rgb = c.dataset._rgb; if (!chartArea) return `rgba(${rgb},.1)`; const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom); g.addColorStop(0, `rgba(${rgb},.38)`); g.addColorStop(.55, `rgba(${rgb},.08)`); g.addColorStop(1, `rgba(${rgb},0)`); return g; } }] },
        options: { responsive: true, maintainAspectRatio: false, animation: reduce ? false : { duration: 900, easing: 'easeOutQuart' }, interaction: { mode: 'nearest', intersect: false },
          plugins: { legend: { display: false }, tooltip: { backgroundColor: 'rgba(13,17,38,.96)', borderColor: 'rgba(139,92,246,.45)', borderWidth: 1, padding: 12, cornerRadius: 10,
            titleColor: '#e8ecff', bodyColor: '#c7cdea', displayColors: false, titleFont: { family: 'Inter', weight: '700' }, bodyFont: { family: 'Inter' },
            callbacks: { label: c => [`Cumulative: ${this.sgnUsd(c.raw.y)}`].concat(c.raw.trade !== undefined ? [`Trade: ${this.sgnUsd(c.raw.trade)} (${c.raw.reason})`, c.raw.slug] : []) } } },
          scales: { x: { type: 'time', grid: { color: 'rgba(148,163,255,.05)' }, ticks: { color: '#8390b0', maxTicksLimit: 7, font: { family: 'Inter', size: 11 } }, border: { display: false } },
                    y: { grid: { color: 'rgba(148,163,255,.05)' }, ticks: { color: '#8390b0', font: { family: 'JetBrains Mono', size: 11 }, callback: v => '$' + v.toFixed(2) }, border: { display: false } } } }
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
        + (live ? ' <b class="text-[#ff8aa8]">This is a real-money sell.</b>' : ' <span class="text-amber-300">Paper mode: simulated sell.</span>')
        + `<dl class="close-sum">${row('Market', this.esc(p.slug))}${row('Side', this.esc(p.side), p.side === 'UP' ? 't-up' : 't-down')}`
        + row('Shares', this.num(p.shares, 2)) + row('Best bid now', e.bid == null ? '—' : this.cents(e.bid))
        + row('Est. proceeds', e.bid == null ? '—' : this.usd(e.proceeds)) + row('Est. P&amp;L', e.bid == null ? '—' : this.sgnUsd(e.pnl), e.bid == null ? '' : this.pnlClass(e.pnl)) + '</dl>'
        + `<div class="mt-2 text-[11.5px] text-[#8390b0]">Estimate after the taker fee. The real fill can be lower if the bid moves: it sells down to ${e.bid == null ? 'bid − slippage' : this.cents(e.floor)} at worst.</div>`;
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
      const cl = x => Math.max(0, Math.min(100, x * 100));
      const basis = (p.cost_usd && p.shares) ? Number(p.cost_usd) / Number(p.shares) : Number(p.entry_price);
      const slp = Number(this.cfgVal('STOP_LOSS_PCT') ?? 0.6), sl = basis * (1 - slp), tp = Number(this.cfgVal('TAKE_PROFIT_MIN_BID') ?? 0.97);
      const e = Number(p.entry_price), m = p.mark == null ? null : Number(p.mark), up = m != null && m >= e;
      const hasSL = slp > 0, tpLX = Math.min(cl(tp), 94), near = (a, b) => Math.abs(a - b) < 16;
      const eLow = near(cl(e), tpLX) || (hasSL && near(cl(e), cl(sl)));
      return { sl, tp, e, m, up, hasSL, eLow, slX: cl(sl), tpX: cl(tp), eX: cl(e), mX: m == null ? null : cl(m),
        fillL: m == null ? cl(e) : Math.min(cl(e), cl(m)), fillW: m == null ? 0 : Math.abs(cl(m) - cl(e)) };
    },
    tradeRowClass(t) { if (this.isFalse(t.ok) || t.action === 'BUY') return ''; const p = Number(t.pnl); return t.pnl === '' || t.pnl == null ? '' : (p > 0 ? 'win' : 'loss'); },
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
    verdictDot(v) { if (v === 'ENTER') return 'bg-[#2ef2a4] shadow-[0_0_10px_rgba(46,242,164,.9)]'; if (v === 'skip:risk') return 'bg-amber-400 shadow-[0_0_8px_rgba(251,191,36,.7)]'; if (/edge|price|spread/.test(v || '')) return 'bg-violet-400 shadow-[0_0_8px_rgba(167,139,250,.7)]'; return 'bg-slate-500'; },
    verdictLabel(v) { const m = { 'ENTER': 'Entered', 'skip:fav_price': 'Skip · favorite outside price band', 'skip:fav_time': 'Skip · outside entry time window', 'skip:insufficient_edge': 'Skip · no edge after fee',
      'skip:wide_spread': 'Skip · spread too wide', 'skip:risk': 'Skip · blocked by risk limits', 'skip:no_book': 'Skip · no order book', 'skip:research_stale': 'Skip · news research stale',
      'skip:no_fresh_price_data': 'Skip · no fresh BTC price', 'skip:not_accepting_orders': 'Skip · market not accepting orders', 'skip:news_against': 'Skip · news against', 'skip:fav_duration': 'Skip · not a 15m market' };
      return m[v] || (v || '').replace('skip:', 'Skip · ').replace(/_/g, ' '); },
    filteredLog() { if (!this.logFilter) return this.logLines; const re = new RegExp(this.logFilter); return this.logLines.filter(l => re.test(l)); },
    logClass(l) { if (/ ERROR /.test(l)) return 'text-rose-300'; if (/ WARNING /.test(l)) return 'text-amber-300'; if (/ ENTER | CLOSED /.test(l)) return 'text-[#7cf9c9]'; if (/HALT/.test(l)) return 'text-rose-300'; return 'text-slate-300/80'; },
  };
}
