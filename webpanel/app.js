/* WARDOGS панель управления — Vue 3, без сборки (CDN). */
const { createApp } = Vue;

const BOOT = window.PANEL_BOOT || { token: "", needLogin: false, refresh: 5 };
const TOKEN_KEY = "wardogs_panel_token";

const MAP_RU = { Kavkazi: "Bakurani", Europe: "Ozeti", NorthAmerica: "Zestafona" };
const LIGHT_RU = {
  DayStartClear: "Рассвет", DayEarlyClear: "Раннее утро", DayEarlyFog: "Раннее утро, туман",
  DayClear: "День", DayLateClear: "Поздний день", DayLateGray: "Серый день",
  DayLateGrayFog: "Серый день, туман", DayEndClear: "Закат",
};

function pad(n) { return String(n).padStart(2, "0"); }

const app = createApp({
  data() {
    return {
      state: {
        authed: false,
        needLogin: BOOT.needLogin,
        password: "",
        servers: [],
        sid: "",
        tab: "status",
        live: true,
        refresh: BOOT.refresh || 5,
        refreshing: false,
        ovById: {},
      },
      token: "",
      busy: false,
      loginError: "",
      lastUpdate: "",
      toasts: [],
      catalog: { maps: {}, lightings: [] },
      form: { map: "", experiences: [], lighting: "", zoneAlternator: "" },
      audit: [],
      auditLimit: 50,
      cfg: { text: "", revision: "", writable: true, warnings: [], loaded: false },
      cfgResult: null,
      cfgResultStatus: "",
      _timer: null,
      _logoutTimer: null,
      _denied: false,
    };
  },

  computed: {
    tabs: () => [
      { id: "status", label: "Статус" },
      { id: "players", label: "Игроки" },
      { id: "controls", label: "Карта / Матч" },
      { id: "bans", label: "Баны" },
      { id: "audit", label: "Аудит" },
      { id: "config", label: "Конфиг" },
    ],
    overview() { return this.state.ovById[this.state.sid] || null; },
    ov() { return this.state.ovById[this.state.sid] || null; },
    sortedFactions() {
      const o = this.overview;
      const list = (o && o.status && o.status.factionScores) || [];
      return [...list].sort((a, b) => (b.score || 0) - (a.score || 0));
    },
    sortedPlayers() {
      const o = this.overview;
      const list = (o && o.players && o.players.players) || [];
      return [...list].sort((a, b) => (b.kills || 0) - (a.kills || 0));
    },
    nextEntry() {
      const o = this.overview;
      if (!o || !o.rotation) return null;
      return (o.rotation.entries || []).find((e) => e.status === "next") || null;
    },
  },

  watch: {
    "state.tab"(t) {
      if (t === "controls") this.ensureCatalog();
      if (t === "audit") this.loadAudit();
      if (t === "config") this.ensureConfig();
    },
    "state.sid"() {
      this.ensureCatalog();
      if (!this.state.ovById[this.state.sid]) this.loadOverview(this.state.sid, true);
      if (this.state.tab === "audit") this.loadAudit();
      if (this.state.tab === "config") this.loadConfig();
    },
  },

  mounted() {
    const self = this;
    this.api = {
      async request(method, url, body) {
        const headers = { "X-Panel-Token": self.token };
        const opts = { method, headers };
        if (body !== undefined) {
          opts.headers["Content-Type"] = "application/json";
          opts.body = JSON.stringify(body);
        }
        const r = await fetch(url, opts);
        let data = null;
        try { data = await r.json(); } catch (e) { /* не JSON */ }
        if (r.status === 401 && !self._denied) {
          self._denied = true;
          self.toast("Сессия истекла — войдите снова", "warn");
          setTimeout(() => { self._denied = false; }, 3000);
          self.logout();
        }
        return { status: r.status, data };
      },
    };
    this.token = localStorage.getItem(TOKEN_KEY) || (BOOT.needLogin ? "" : BOOT.token || "");
    this.init();
  },

  methods: {
    toast(text, kind = "") {
      this.toasts.push({ text, kind });
      setTimeout(() => this.toasts.shift(), 3500);
    },

    async init() {
      const { data } = await this.api.request("GET", "/api/session");
      if (!data || !data.ok) return;
      this.state.authed = !!data.authed;
      this.state.needLogin = !!data.needs_password;
      this.state.servers = data.servers || [];
      this.state.refresh = data.refresh || 5;
      if (!this.state.needLogin && !this.token) this.token = BOOT.token || "";
      if (this.state.servers.length) this.state.sid = this.state.sid || this.state.servers[0].id;
      if (this.state.authed) {
        this.startTimer();
        this.refreshAll(true);
      }
    },

    async doLogin() {
      this.busy = true;
      this.loginError = "";
      const { status, data } = await this.api.request("POST", "/api/login", { password: this.state.password });
      this.busy = false;
      if (status === 200 && data && data.ok) {
        this.token = data.token;
        localStorage.setItem(TOKEN_KEY, this.token);
        await this.init();
        // на случай если бэкенд на другом порту — авторизация уже есть
      } else {
        this.loginError = (data && data.error && data.error.message) || "Ошибка входа";
      }
    },

    logout() {
      localStorage.removeItem(TOKEN_KEY);
      this.token = "";
      this.state.authed = false;
      this.state.password = "";
      this.stopTimer();
    },

    selectServer(id) {
      this.state.sid = id;
    },

    isOffline(sid) {
      const o = this.state.ovById[sid];
      return !o || !!o.error;
    },
    plCount(sid) {
      const o = this.state.ovById[sid];
      if (!o || o.error || !o.status) return null;
      const p = o.status.players || {};
      return `${p.current}/${p.max}`;
    },

    toggleLive() {
      if (this.state.live) this.startTimer();
      else this.stopTimer();
    },
    startTimer() {
      this.stopTimer();
      this._timer = setInterval(() => { if (this.state.live) this.refreshAll(false); }, this.state.refresh * 1000);
    },
    stopTimer() {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },

    async refreshAll(force) {
      if (this.state.refreshing) return;
      this.state.refreshing = true;
      await Promise.all(this.state.servers.map((s) => this.loadOverview(s.id, force)));
      this.state.refreshing = false;
      this.lastUpdate = new Date().toLocaleTimeString("ru-RU");
    },

    async loadOverview(id, force) {
      const cached = this.state.ovById[id];
      if (cached && !force && Date.now() - (cached._ts || 0) < 1500) return;
      const { data } = await this.api.request("GET", `/api/server/${id}/overview`);
      if (!data) return;
      data._ts = Date.now();
      this.state.ovById[id] = data;
    },

    async ensureCatalog() {
      if (Object.keys(this.catalog.maps).length || !this.state.sid) return;
      const { data } = await this.api.request("GET", `/api/server/${this.state.sid}/catalog`);
      if (data && data.ok) {
        this.catalog.maps = data.maps || {};
        this.catalog.lightings = data.lightings || [];
        if (!this.form.map) this.form.map = Object.keys(this.catalog.maps)[0] || "";
      } else if (data && data.error) {
        this.toast(`Каталог: ${data.error.message}`, "err");
      }
    },

    // ---- форматирование ----
    mapLabel(m) { return m || "—"; },
    ruName(id) { return MAP_RU[id] || ""; },
    lightLabel(l) {
      return l ? (LIGHT_RU[l] ? `${LIGHT_RU[l]} (${l})` : l) : "—";
    },
    modeLabel(list) { return (list && list.length) ? list.join(" · ") : ""; },
    rotMode(r) { return r.mode === "random" ? "Случайно" : "По порядку"; },
    fmtClock(s) {
      s = Math.max(0, Math.floor(s || 0));
      return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    },
    fmtDur(s) {
      s = Math.floor(s || 0);
      if (!s) return "—";
      const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
      return d ? `${d}д ${h}ч` : `${h}ч`;
    },
    pct(p) { return p.max ? Math.min(100, Math.round((p.current / p.max) * 100)) : 0; },
    pctScore(f) {
      const total = this.sortedFactions.reduce((a, x) => a + (x.score || 0), 0);
      return total ? Math.max(2, Math.round((f.score / total) * 100)) : 0;
    },
    kd(p) {
      const k = p.kills || 0, d = p.deaths || 0;
      if (!d) return k ? "∞" : "0.0";
      return (k / d).toFixed(1);
    },
    shortSid(s) { return s ? `${String(s).slice(0, 5)}…${String(s).slice(-3)}` : "—"; },
    fmtDate(t) {
      if (!t) return "—";
      const d = new Date(t);
      if (isNaN(d)) return t;
      return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" });
    },

    // ---- действия ----
    async act(url, method, body) {
      this.busy = true;
      const { status, data } = await this.api.request(method, url, body);
      this.busy = false;
      const msg = (data && data.error && data.error.message) || (status >= 400 ? `HTTP ${status}` : null);
      if (status >= 400) {
        this.toast(msg || "Ошибка", "err");
        return false;
      }
      this.toast("Выполнено", "ok");
      this.refreshAll(true);
      return true;
    },

    kickPlayer(p) {
      const reason = prompt(`Кикнуть ${p.name}? Причина (можно пусто):`);
      if (reason === null) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/kick`, "POST",
        reason.trim() ? { reason: reason.trim() } : {});
    },
    killPlayer(p) {
      if (!confirm(`Убить ${p.name}?`)) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/kill`, "POST", {});
    },
    sendMsg(p) {
      const m = prompt(`Личное сообщение для ${p.name}:`);
      if (!m || !m.trim()) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}/message`, "POST", { message: m.trim() });
    },
    moveFaction(p, faction) {
      if (faction === p.faction || !faction) return;
      if (!confirm(`Перевести ${p.name} в команду «${faction}»?`)) return;
      this.act(`/api/server/${this.state.sid}/players/${p.steamId}`, "PATCH", { faction });
    },
    banPlayer(p) {
      const reason = prompt(`Бан ${p.name} (${p.steamId})? Причина (можно пусто):`);
      if (reason === null) return;
      if (!confirm(`Подтверди бан: ${p.name} (${p.steamId})`)) return;
      const body = { steamId: p.steamId };
      if (reason.trim()) body.reason = reason.trim();
      this.act(`/api/server/${this.state.sid}/bans`, "POST", body);
    },
    unban(b) {
      if (!confirm(`Снять бан с ${b.steamId}?`)) return;
      this.act(`/api/server/${this.state.sid}/bans/${b.steamId}`, "DELETE", {});
    },

    openBroadcast() {
      const m = prompt("Текст объявления всем игрокам:");
      if (!m || !m.trim()) return;
      if (!confirm(`Отправить всем: ${m.trim()}`)) return;
      this.act(`/api/server/${this.state.sid}/broadcast`, "POST", { message: m.trim() });
    },
    openAddBan() {
      const steam = prompt("Steam64 пользователя:", "")?.trim();
      if (!steam) return;
      const reason = prompt("Причина (можно пусто):")?.trim() || "";
      if (!confirm(`Забанить ${steam}?${reason ? " Причина: " + reason : ""}`)) return;
      this.act(`/api/server/${this.state.sid}/bans`, "POST",
        reason ? { steamId: steam, reason } : { steamId: steam });
    },

    changeMap() {
      const m = this.form.map;
      if (!m) { this.toast("Выберите карту", "warn"); return; }
      const label = `${this.catalog.maps[m].display}${MAP_RU[m] ? ` (${MAP_RU[m]})` : ""}`;
      const msg = `Сменить карту на ${label}?\n` +
        `Режимы: ${this.form.experiences.length ? this.form.experiences.join(", ") : "— по умолчанию —"}\n` +
        `Свет: ${this.form.lighting ? this.lightLabel(this.form.lighting) : "— текущий —"}`;
      if (!confirm(msg)) return;
      const body = { map: m };
      if (this.form.experiences.length) body.experiences = this.form.experiences;
      if (this.form.lighting) body.lighting = this.form.lighting;
      if (this.form.zoneAlternator) body.zoneAlternator = this.form.zoneAlternator;
      this.act(`/api/server/${this.state.sid}/match/map`, "POST", body);
    },
    changeLighting(l) {
      if (!confirm(`Сменить свет на ${this.lightLabel(l)}?`)) return;
      this.act(`/api/server/${this.state.sid}/world/lighting`, "PUT", { lighting: l });
    },
    endMatch() {
      if (!confirm("Завершить текущий матч?")) return;
      this.act(`/api/server/${this.state.sid}/match/end`, "POST", {});
    },
    restartMatch() {
      if (!confirm("Перезапустить матч? Игроков выкинет из матча.")) return;
      this.act(`/api/server/${this.state.sid}/match/restart`, "POST", {});
    },

    // ---- аудит ----
    async loadAudit() {
      const { data } = await this.api.request("GET", `/api/server/${this.state.sid}/audit?limit=${this.auditLimit}`);
      if (data && data.ok) this.audit = data.entries || [];
      else if (data && data.error) this.toast(data.error.message, "err");
    },

    // ---- конфиг ----
    async ensureConfig() {
      if (!this.cfg.loaded) await this.loadConfig();
    },
    async loadConfig() {
      this.busy = true;
      const { status, data } = await this.api.request("GET", `/api/server/${this.state.sid}/config`);
      this.busy = false;
      if (status === 200 && data.ok) {
        this.cfg = { text: data.text || "", revision: data.revision, writable: !!data.writable, warnings: data.warnings || [], loaded: true };
        this.cfgResult = null;
      } else if (data && data.error) {
        this.toast(data.error.message, "err");
        this.cfg = { ...this.cfg, loaded: true };
      }
    },
    async validateConfig() {
      if (!this.cfg.loaded) return;
      this.busy = true;
      const r = await fetch(`/api/server/${this.state.sid}/config/validate`, {
        method: "POST",
        headers: { "X-Panel-Token": this.token, "Content-Type": "text/plain" },
        body: this.cfg.text,
      });
      this.busy = false;
      const data = (await r.json().catch(() => null)) || { status: r.status };
      this.cfgResult = data;
      this.cfgResultStatus = r.status >= 400 ? "err" : "ok";
      if (data.error) this.toast(data.error.message, r.status >= 400 ? "err" : "warn");
    },
    async applyConfig() {
      if (!this.cfg.loaded) return;
      if (!confirm("Применить конфиг целиком на сервер? Если ревизия устарела — придётся обновить и применить заново.")) return;
      this.busy = true;
      const r = await fetch(`/api/server/${this.state.sid}/config?force=true`, {
        method: "PUT",
        headers: { "X-Panel-Token": this.token, "Content-Type": "text/plain", "If-Match": this.cfg.revision },
        body: this.cfg.text,
      });
      this.busy = false;
      const data = (await r.json().catch(() => null)) || { status: r.status };
      this.cfgResult = data;
      this.cfgResultStatus = r.status >= 400 ? "err" : "ok";
      if (r.status === 200) {
        this.toast("Конфиг применён", "ok");
        if (data.revision) this.cfg.revision = data.revision;
        this.refreshAll(true);
      } else {
        this.toast("Конфиг не применён — см. результат", "err");
        if (data.revision) this.cfg.revision = data.revision;
      }
    },
  },
});

const vm = app.mount("#app");
vm.init();