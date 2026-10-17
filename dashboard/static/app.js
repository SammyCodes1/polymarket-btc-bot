/* Dashboard front-end: Alpine.js component + Chart.js. No build step. */
function dash() {
  return {
    tab: 'overview', busy: false, offline: false,
    proc: {}, cfg: { editable: {}, display: {}, mode: '' }, state: { available: false },
    positions: [], unrealized: 0, stats: {}, statsMode: 'auto', trades: [], tradesTotal: 0,
    decisions: [], showNoise: false, logLines: [], logFollow: true, logFilter: '',
    form: {}, modal: { open: false }, toasts: [], chart: null,
    token: localStorage.getItem('dashToken') || '',
    tzLabel: (() => { try { return new Intl.DateTimeFormat([], { timeZoneName: 'short' }).formatToParts(new Date()).find(p => p.type === 'timeZoneName').value; } catch { return 'local time'; } })(),

    async init() {
      await this.loadConfig();
      this.refreshAll();
      setInterval(() => this.refreshFast(), 4000);
      setInterval(() => this.refreshSlow(), 8000);
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
    // ---------- controls ----------
    confirm(o) { return new Promise(res => { this.modal = { open: true, input: '', ok: 'Confirm', danger: false, phrase: '', ...o, resolve: res }; if (o.phrase) this.$nextTick(() => this.$refs.phraseInput && this.$refs.phraseInput.focus()); }); },
    modalOk() { if (this.modal.phrase && this.modal.input.trim().toUpperCase() !== this.modal.phrase) return; const r = this.modal.resolve; const v = this.modal.input; this.modal.open = false; r && r(v || true); },
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
    hint(k) {
      const v = Number(this.form[k]); if (isNaN(v)) return '';
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
          body: 'The bot will trade <b>real money</b> from your wallet the next time it starts. You can lose the full amount you put at risk.' });
        if (!v) return; body.live_confirmation = 'GO LIVE'; body.confirm_live = true;
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
      const color = last >= 0 ? '16,185,129' : '244,63,94';
      if (this.chart) { const ds = this.chart.data.datasets[0]; ds.data = pts; ds.borderColor = `rgb(${color})`; ds._rgb = color; this.chart.update('none'); return; }
      const ctx = el.getContext('2d');
      this.chart = new Chart(ctx, {
        type: 'line',
        data: { datasets: [{ data: pts, _rgb: color, borderColor: `rgb(${color})`, borderWidth: 2, tension: 0.25, stepped: false,
          pointRadius: c => c.raw && c.raw.trade !== undefined ? 2.5 : 0, pointHoverRadius: 5,
          pointBackgroundColor: c => (c.raw && c.raw.trade < 0) ? '#f43f5e' : '#10b981', pointBorderWidth: 0,
          fill: { target: 'origin' },
          backgroundColor: c => { const { chartArea, ctx } = c.chart; const rgb = c.dataset._rgb; if (!chartArea) return `rgba(${rgb},.1)`; const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom); g.addColorStop(0, `rgba(${rgb},.28)`); g.addColorStop(1, `rgba(${rgb},0)`); return g; } }] },
        options: { responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: 'nearest', intersect: false },
          plugins: { legend: { display: false }, tooltip: { backgroundColor: '#0f141d', borderColor: 'rgba(255,255,255,.1)', borderWidth: 1, padding: 10, titleColor: '#e2e8f0', bodyColor: '#cbd5e1', displayColors: false,
            callbacks: { label: c => [`Cumulative: ${this.sgnUsd(c.raw.y)}`].concat(c.raw.trade !== undefined ? [`Trade: ${this.sgnUsd(c.raw.trade)} (${c.raw.reason})`, c.raw.slug] : []) } } },
          scales: { x: { type: 'time', grid: { color: 'rgba(255,255,255,.04)' }, ticks: { color: '#64748b', maxTicksLimit: 8 }, border: { display: false } },
                    y: { grid: { color: 'rgba(255,255,255,.04)' }, ticks: { color: '#64748b', callback: v => '$' + v.toFixed(2) }, border: { display: false } } } }
      });
    },
    // ---------- formatting ----------
    usd(v) { const n = Number(v); return isFinite(n) && v !== '' && v != null ? '$' + n.toFixed(2) : '—'; },
    sgnUsd(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null) return '—'; return (n > 0 ? '+' : n < 0 ? '−' : '') + '$' + Math.abs(n).toFixed(2); },
    sgnPct(v) { const n = Number(v) * 100; return (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n).toFixed(1) + '%'; },
    pct(v) { return v == null ? '—' : Math.round(v * 100) + '%'; },
    pctOf(a, b) { if (!b) return '—'; const n = 100 * a / b; return (n > 0 ? '+' : n < 0 ? '−' : '') + Math.abs(n).toFixed(1) + '%'; },
    cents(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null) return '—'; const c = Math.round(n * 1000) / 10; return (Number.isInteger(c) ? c.toFixed(0) : c.toFixed(1)) + '¢'; },
    num(v, d) { const n = Number(v); return isFinite(n) && v !== '' ? n.toFixed(d) : '—'; },
    pnlClass(v) { const n = Number(v); if (!isFinite(n) || v === '' || v == null || n === 0) return 'text-slate-200'; return n > 0 ? 'text-emerald-400' : 'text-rose-400'; },
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
    verdictDot(v) { if (v === 'ENTER') return 'bg-emerald-400 shadow-[0_0_8px_rgba(16,185,129,.8)]'; if (v === 'skip:risk') return 'bg-amber-400'; if (/edge|price|spread/.test(v || '')) return 'bg-sky-400/70'; return 'bg-slate-600'; },
    verdictLabel(v) { const m = { 'ENTER': 'Entered', 'skip:fav_price': 'Skip · favorite outside price band', 'skip:fav_time': 'Skip · outside entry time window', 'skip:insufficient_edge': 'Skip · no edge after fee',
      'skip:wide_spread': 'Skip · spread too wide', 'skip:risk': 'Skip · blocked by risk limits', 'skip:no_book': 'Skip · no order book', 'skip:research_stale': 'Skip · news research stale',
      'skip:no_fresh_price_data': 'Skip · no fresh BTC price', 'skip:not_accepting_orders': 'Skip · market not accepting orders', 'skip:news_against': 'Skip · news against', 'skip:fav_duration': 'Skip · not a 15m market' };
      return m[v] || (v || '').replace('skip:', 'Skip · ').replace(/_/g, ' '); },
    filteredLog() { if (!this.logFilter) return this.logLines; const re = new RegExp(this.logFilter); return this.logLines.filter(l => re.test(l)); },
    logClass(l) { if (/ ERROR /.test(l)) return 'text-rose-300'; if (/ WARNING /.test(l)) return 'text-amber-300'; if (/ ENTER | CLOSED /.test(l)) return 'text-emerald-300'; if (/HALT/.test(l)) return 'text-rose-300'; return 'text-slate-400'; },
  };
}
