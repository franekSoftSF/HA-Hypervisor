// Sidebar panel of HA Hypervisor. Its layout comes from the
// ha_hypervisor/dashboard websocket command, so new servers, VMs and scenarios
// show up without any configuration. Two tabs made of the same kind of cards:
// the overview (each agent with its controls, each server with its VMs, the
// datastores) and the scenarios (each scenario with the usage of its VMs).

const TEXT = {
  en: {
    title: "Hypervisor",
    tabOverview: "Overview",
    tabScenarios: "Scenarios",
    servers: "Servers",
    datastores: "Datastores",
    otherVms: "Other virtual machines",
    cpu: "CPU",
    memory: "Memory",
    storage: "Storage",
    clusterCpu: "Cluster CPU",
    clusterMemory: "Cluster memory",
    hostsOnline: "Servers online",
    vmsRunning: "VMs running",
    operation: "Operation",
    offline: "agent offline",
    empty: "No agent is set up yet.",
    vm: "Virtual machine",
    start: "Start",
    stop: "Stop",
    clusterStart: "Start cluster",
    preview: "Preview shutdown",
    arm: "Unlock shutdown",
    clusterShutdown: "Shut down cluster",
    cancel: "Cancel",
    refresh: "Refresh",
    confirmShutdown: (name) => `Shut down ${name}: all VMs, vCenter and the servers?`,
    armHint: "Unlock first, the shutdown button stays locked otherwise.",
    confirmStop: (name) => `Stop the scenario ${name}? Its VMs are shut down.`,
    noScenarios: "No scenarios. Add one under Settings → Devices & services → HA Hypervisor → Add scenario.",
    noVms: "No virtual machines.",
    vmsOn: (on, total) => `${on} of ${total} VMs running`,
    free: (free, total) => `${free} free of ${total}`,
    uptime: "up",
    downloadLog: "Download agent log",
    downloading: "Downloading…",
    logFailed: "The log could not be downloaded",
    failed: (what) => `${what} failed`,
    kinds: {
      cluster_shutdown: "Cluster shutdown",
      cluster_start: "Cluster start",
      scenario_start: "Scenario start",
      scenario_stop: "Scenario stop",
      snapshot_create: "Snapshot",
      snapshot_revert: "Snapshot revert",
      snapshot_delete: "Snapshot delete",
    },
    lastOperation: "The last operation",
    logHint: "The agent log tells more.",
  },
  pl: {
    title: "Hypervisor",
    tabOverview: "Przegląd",
    tabScenarios: "Scenariusze",
    servers: "Serwery",
    datastores: "Magazyny danych",
    otherVms: "Pozostałe maszyny wirtualne",
    cpu: "CPU",
    memory: "Pamięć",
    storage: "Dysk",
    clusterCpu: "CPU klastra",
    clusterMemory: "Pamięć klastra",
    hostsOnline: "Serwery online",
    vmsRunning: "Działające maszyny",
    operation: "Operacja",
    offline: "agent offline",
    empty: "Nie dodano jeszcze żadnego agenta.",
    vm: "Maszyna wirtualna",
    start: "Uruchom",
    stop: "Zatrzymaj",
    clusterStart: "Uruchom klaster",
    preview: "Podgląd wyłączenia",
    arm: "Odbezpiecz wyłączenie",
    clusterShutdown: "Wyłącz klaster",
    cancel: "Anuluj",
    refresh: "Odśwież",
    confirmShutdown: (name) => `Wyłączyć ${name}: wszystkie maszyny, vCenter i serwery?`,
    armHint: "Najpierw odbezpiecz, inaczej przycisk wyłączenia jest zablokowany.",
    confirmStop: (name) => `Zatrzymać scenariusz ${name}? Jego maszyny zostaną wyłączone.`,
    noScenarios: "Brak scenariuszy. Dodaj je w Ustawienia → Urządzenia oraz usługi → HA Hypervisor → Dodaj scenariusz.",
    noVms: "Brak maszyn wirtualnych.",
    vmsOn: (on, total) => `włączone ${on} z ${total} maszyn`,
    free: (free, total) => `wolne ${free} z ${total}`,
    uptime: "działa",
    downloadLog: "Pobierz log agenta",
    downloading: "Pobieranie…",
    logFailed: "Nie udało się pobrać logu",
    failed: (what) => `${what}: błąd`,
    kinds: {
      cluster_shutdown: "Wyłączanie klastra",
      cluster_start: "Uruchamianie klastra",
      scenario_start: "Uruchamianie scenariusza",
      scenario_stop: "Zatrzymywanie scenariusza",
      snapshot_create: "Tworzenie migawki",
      snapshot_revert: "Przywracanie migawki",
      snapshot_delete: "Usuwanie migawki",
    },
    lastOperation: "Ostatnia operacja",
    logHint: "Więcej powie log agenta.",
  },
};

const TABS = ["overview", "scenarios"];
const TAB_STORAGE = "ha-hypervisor-panel-tab";
const LOG_LINES = 2000;

const GREEN = "var(--success-color, #43a047)";
const ORANGE = "var(--warning-color, #ffa600)";
const BLUE = "var(--info-color, #039be5)";
const RED = "var(--error-color, #db4437)";

// Colour of the dot in front of a cluster, server or scenario, by its state.
const STATE_COLOR = {
  on: GREEN,
  active: GREEN,
  degraded: ORANGE,
  partial: ORANGE,
  suspended: ORANGE,
  starting: BLUE,
  stopping: BLUE,
  running: BLUE,
  failed: RED,
};

// A small DOM builder: dynamic text always goes through text nodes.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key.includes("-")) node.setAttribute(key, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function readTab() {
  try {
    const saved = window.localStorage.getItem(TAB_STORAGE);
    return TABS.includes(saved) ? saved : "overview";
  } catch (_err) {
    return "overview";
  }
}

function saveTab(tab) {
  try {
    window.localStorage.setItem(TAB_STORAGE, tab);
  } catch (_err) {
    // Private window: the tab is simply not remembered.
  }
}

const STYLE = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
  }
  .toolbar {
    display: flex;
    align-items: center;
    height: var(--header-height, 56px);
    padding: 0 12px;
    box-sizing: border-box;
    background: var(--app-header-background-color, var(--primary-background-color));
    color: var(--app-header-text-color, var(--primary-text-color));
    border-bottom: var(--app-header-border-bottom, none);
    font-size: 20px;
    gap: 12px;
  }
  .tabs {
    display: flex;
    gap: 4px;
    padding: 0 16px;
    border-bottom: 1px solid var(--divider-color);
    overflow-x: auto;
  }
  .tab {
    appearance: none;
    border: none;
    background: none;
    color: var(--secondary-text-color);
    font: inherit;
    font-size: 14px;
    font-weight: 500;
    padding: 12px 16px;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    white-space: nowrap;
  }
  .tab[aria-selected="true"] { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .tab .count {
    margin-left: 6px;
    font-size: 12px;
    padding: 1px 7px;
    border-radius: 10px;
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.15));
  }
  .content { padding: 16px; max-width: 1040px; margin: 0 auto; }
  .agent + .agent { margin-top: 32px; }
  .heading {
    font-size: 16px;
    font-weight: 500;
    color: var(--secondary-text-color);
    margin: 24px 4px 10px;
  }
  .badge { font-size: 13px; color: var(--error-color); font-weight: 400; }
  .card {
    background: var(--card-background-color, var(--ha-card-background));
    border-radius: var(--ha-card-border-radius, 12px);
    border: var(--ha-card-border-width, 1px) solid var(--ha-card-border-color, var(--divider-color));
    padding: 16px;
    margin-bottom: 16px;
  }
  .head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .dot {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    flex: none;
    background: var(--disabled-color, #9e9e9e);
  }
  .title { flex: 1; min-width: 180px; cursor: pointer; }
  .title .name { font-size: 18px; font-weight: 500; }
  .title .sub { font-size: 13px; color: var(--secondary-text-color); }
  .metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 8px;
    margin: 14px 0 6px;
  }
  .metric {
    border-radius: 8px;
    padding: 8px 12px;
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.08));
    cursor: pointer;
    min-width: 0;
  }
  .metric .label { font-size: 12px; color: var(--secondary-text-color); }
  .metric .value { font-size: 18px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .metric .extra { font-size: 12px; color: var(--secondary-text-color); }
  .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 12px; }
  .actions .spacer { flex: 1; }
  .arm { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; padding: 0 4px; }
  .hint { font-size: 12px; color: var(--secondary-text-color); width: 100%; }
  .alert {
    border-left: 4px solid var(--error-color);
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.08));
    border-radius: 8px;
    padding: 10px 14px;
    margin-top: 12px;
    font-size: 14px;
    line-height: 1.4;
  }
  .alert strong { color: var(--error-color); font-weight: 500; }
  .alert .detail { color: var(--secondary-text-color); word-break: break-word; }
  .alert:empty { display: none; }
  .button {
    appearance: none;
    font: inherit;
    font-size: 14px;
    font-weight: 500;
    padding: 7px 14px;
    border-radius: 18px;
    cursor: pointer;
    border: 1px solid var(--divider-color);
    background: var(--card-background-color, var(--ha-card-background));
    color: var(--primary-color);
    white-space: nowrap;
  }
  .button.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .button.danger { background: var(--error-color); border-color: var(--error-color); color: #fff; }
  .button[disabled] { opacity: 0.45; cursor: default; }
  .log-error { color: var(--error-color); font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 8px; }
  th {
    text-align: left;
    font-weight: 500;
    font-size: 12px;
    color: var(--secondary-text-color);
    padding: 6px 8px;
    border-bottom: 1px solid var(--divider-color);
  }
  td { padding: 8px; border-bottom: 1px solid var(--divider-color); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  th.num, td.num { text-align: right; white-space: nowrap; }
  td.link { cursor: pointer; }
  td .line { display: flex; align-items: center; gap: 8px; }
  td .small { font-size: 12px; color: var(--secondary-text-color); }
  .bar {
    height: 4px;
    border-radius: 2px;
    margin-top: 4px;
    background: var(--divider-color);
    overflow: hidden;
  }
  .bar > div { height: 100%; background: var(--primary-color); }
  .bar.high > div { background: var(--warning-color, #ffa600); }
  .bar.full > div { background: var(--error-color, #db4437); }
  td.switch { width: 1%; }
  .note { color: var(--secondary-text-color); font-size: 14px; padding: 8px 0 0; }
  .message { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); }
  @media (max-width: 600px) {
    .content { padding: 8px; }
    .card { padding: 12px; }
    th.cpu, td.cpu { display: none; }
    .metric .value { font-size: 16px; }
  }
`;

class HaHypervisorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._layout = null;
    this._signature = null;
    this._viewSignature = "";
    this._loading = false;
    this._tab = readTab();
    this._logBusy = {};
    this._logError = {};
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._renderFrame();
      // The error texts of the integration, for the reason of a failed operation.
      hass.loadBackendTranslation?.("exceptions", "ha_hypervisor");
    }
    if (this._menu) this._menu.hass = hass;
    const signature = this._entitySignature();
    if (signature !== this._signature) {
      this._signature = signature;
      this._load();
    } else if (this._layout && this._viewState() !== this._viewSignature) {
      this._build();
    }
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    if (this._menu) this._menu.narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  get _text() {
    const language = (this._hass?.language || "en").split("-")[0];
    return TEXT[language] || TEXT.en;
  }

  get _numbers() {
    const language = this._hass?.locale?.language || this._hass?.language || "en";
    return new Intl.NumberFormat(language, { maximumFractionDigits: 1 });
  }

  _entitySignature() {
    const entities = this._hass?.entities || {};
    return Object.values(entities)
      .filter((entry) => entry.platform === "ha_hypervisor")
      .map((entry) => entry.entity_id)
      .sort()
      .join(",");
  }

  _state(entityId) {
    return entityId ? this._hass?.states[entityId] : undefined;
  }

  _isOn(entityId) {
    return this._state(entityId)?.state === "on";
  }

  _format(stateObj) {
    if (!stateObj) return "—";
    return this._hass.formatEntityState ? this._hass.formatEntityState(stateObj) : stateObj.state;
  }

  _value(entityId) {
    return this._format(this._state(entityId));
  }

  // Everything the cards show; they are drawn again only when this changes.
  _viewState() {
    if (!this._layout) return "";
    const parts = [this._tab, this._hass?.language];
    const add = (entityId) => {
      const stateObj = this._state(entityId);
      if (!stateObj) return parts.push("-");
      const a = stateObj.attributes;
      parts.push(stateObj.state, a.cpu_mhz, a.mem_mb, a.mem_total_mb, a.storage_gb, a.host, a.error_code, a.finished);
    };
    for (const agent of this._layout.agents) {
      parts.push(agent.online, this._logBusy[agent.entry_id], this._logError[agent.entry_id]);
      Object.values(agent.cluster).forEach(add);
      for (const kind of ["hosts", "vms", "datastores", "scenarios"]) {
        for (const item of agent[kind]) Object.values(item.entities).forEach(add);
      }
    }
    return parts.join(",");
  }

  async _load() {
    if (this._loading) {
      this._reload = true;
      return;
    }
    this._loading = true;
    try {
      this._layout = await this._hass.callWS({ type: "ha_hypervisor/dashboard" });
      this._build();
    } catch (err) {
      this._showMessage(String(err?.message || err));
    } finally {
      this._loading = false;
      if (this._reload) {
        this._reload = false;
        this._load();
      }
    }
  }

  _renderFrame() {
    const style = el("style");
    style.textContent = STYLE;
    const menu = el("span");
    this.shadowRoot.replaceChildren(
      style,
      el("div", { class: "toolbar" }, menu, el("span", {}, this._text.title)),
      el("div", { class: "tabs", role: "tablist" }),
      el("div", { class: "content" }),
    );
    if (customElements.get("ha-menu-button")) {
      this._menu = document.createElement("ha-menu-button");
      this._menu.hass = this._hass;
      this._menu.narrow = this._narrow;
      menu.append(this._menu);
    }
  }

  _renderTabs() {
    const t = this._text;
    const count = (this._layout?.agents || []).reduce((sum, agent) => sum + agent.scenarios.length, 0);
    this.shadowRoot.querySelector(".tabs").replaceChildren(
      ...TABS.map((tab) =>
        el(
          "button",
          { class: "tab", role: "tab", "aria-selected": String(tab === this._tab), onclick: () => this._selectTab(tab) },
          tab === "overview" ? t.tabOverview : t.tabScenarios,
          tab === "scenarios" && count ? el("span", { class: "count" }, count) : null,
        ),
      ),
    );
  }

  _selectTab(tab) {
    if (tab === this._tab) return;
    this._tab = tab;
    saveTab(tab);
    this._build();
  }

  _showMessage(text) {
    this.shadowRoot.querySelector(".content").replaceChildren(el("div", { class: "message" }, text));
  }

  _running(agent) {
    return this._state(agent.cluster.operation)?.state === "running";
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  _call(domain, service, entityId) {
    return this._hass.callService(domain, service, { entity_id: entityId });
  }

  // ------------------------------------------------------------ building blocks

  _dot(state) {
    return el("span", { class: "dot", style: STATE_COLOR[state] ? `background:${STATE_COLOR[state]}` : null });
  }

  _head(state, name, sub, onclick, ...actions) {
    return el(
      "div",
      { class: "head" },
      this._dot(state),
      el("div", { class: "title", onclick }, el("div", { class: "name" }, name), el("div", { class: "sub" }, sub)),
      ...actions,
    );
  }

  // A tile with a label, a value, an optional line below and an optional bar.
  _metric(label, entity, { value, extra, percent } = {}) {
    return el(
      "div",
      { class: "metric", onclick: entity ? () => this._moreInfo(entity) : null },
      el("div", { class: "label" }, label),
      el("div", { class: "value" }, value ?? this._value(entity)),
      extra ? el("div", { class: "extra" }, extra) : null,
      percent === undefined || percent === null ? null : this._bar(percent / 100),
    );
  }

  _bar(share) {
    const width = Math.max(0, Math.min(1, Number(share) || 0));
    const level = width >= 0.9 ? " full" : width >= 0.75 ? " high" : "";
    return el("div", { class: `bar${level}` }, el("div", { style: `width:${Math.round(width * 100)}%` }));
  }

  _button(label, onclick, { kind = "", disabled = false } = {}) {
    return el("button", { class: `button ${kind}`.trim(), disabled, onclick }, label);
  }

  _switch(entity, on, disabled, onchange) {
    const change = onchange || ((checked) => this._call("switch", checked ? "turn_on" : "turn_off", entity));
    if (customElements.get("ha-switch")) {
      const toggle = document.createElement("ha-switch");
      toggle.checked = on;
      toggle.disabled = disabled;
      toggle.addEventListener("change", (ev) => change(ev.target.checked));
      return toggle;
    }
    return el("input", { type: "checkbox", checked: on, disabled, onchange: (ev) => change(ev.target.checked) });
  }

  _memory(mib) {
    const n = this._numbers;
    return mib >= 1024 ? `${n.format(mib / 1024)} GiB` : `${n.format(mib)} MiB`;
  }

  // A number from a sensor in its own short form, or null.
  _amount(entityId, format) {
    const value = Number(this._state(entityId)?.state);
    return entityId && Number.isFinite(value) ? format(value) : null;
  }

  // 77229 seconds as "21 h", 300000 as "3 d 11 h".
  _duration(seconds) {
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const h = (this._hass?.language || "").startsWith("pl") ? "godz." : "h";
    if (days) return hours ? `${days} d ${hours} ${h}` : `${days} d`;
    if (hours) return `${hours} ${h}`;
    return `${minutes} min`;
  }

  _percentOf(entityId) {
    const value = Number(this._state(entityId)?.state);
    return Number.isFinite(value) ? value : null;
  }

  // The VMs behind their power switches, with the numbers the switch carries.
  _vmRows(powers) {
    return powers
      .map(({ power, name }) => {
        const vm = this._state(power);
        const a = vm?.attributes || {};
        const number = (value) => (value === undefined || value === null ? null : Number(value));
        return {
          power,
          name: name || vm?.attributes?.friendly_name || power,
          host: a.host,
          on: vm?.state === "on",
          stateText: this._format(vm),
          cpu: number(a.cpu_mhz) || 0,
          mem: number(a.mem_mb) || 0,
          memTotal: number(a.mem_total_mb) || 0,
          disk: number(a.storage_gb),
        };
      });
  }

  // A table of VMs: each value with a bar showing its share of the whole card.
  _vmTable(rows, locked) {
    const t = this._text;
    const n = this._numbers;
    const sum = (key, runningOnly) =>
      rows.reduce((total, row) => total + (runningOnly && !row.on ? 0 : row[key] || 0), 0);
    const totals = { cpu: sum("cpu", true), mem: sum("mem", true), disk: sum("disk", false) };
    const share = (value, key) => (totals[key] > 0 ? value / totals[key] : 0);
    const cell = (text, part, extraClass = "") =>
      el("td", { class: `num ${extraClass}`.trim() }, text, part === null ? null : this._bar(part));
    const body = rows.map((row) =>
      el(
        "tr",
        {},
        el(
          "td",
          { class: "link", onclick: () => this._moreInfo(row.power) },
          el(
            "div",
            { class: "line" },
            this._dot(row.on ? "on" : "off"),
            el("div", {}, el("div", {}, row.name), el("div", { class: "small" }, row.stateText)),
          ),
        ),
        cell(row.on ? `${n.format(row.cpu)} MHz` : "—", row.on ? share(row.cpu, "cpu") : null, "cpu"),
        cell(
          row.on ? (row.memTotal ? `${this._memory(row.mem)} / ${this._memory(row.memTotal)}` : this._memory(row.mem)) : "—",
          row.on ? share(row.mem, "mem") : null,
        ),
        cell(row.disk === null ? "—" : `${n.format(row.disk)} GiB`, row.disk === null ? null : share(row.disk, "disk")),
        el("td", { class: "switch" }, this._switch(row.power, row.on, locked)),
      ),
    );
    return el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", {}, t.vm),
          el("th", { class: "num cpu" }, t.cpu),
          el("th", { class: "num" }, t.memory),
          el("th", { class: "num" }, t.storage),
          el("th", {}),
        ),
      ),
      el("tbody", {}, body),
    );
  }

  _sortRows(rows) {
    return rows.sort((a, b) => Number(b.on) - Number(a.on) || a.name.localeCompare(b.name));
  }

  // ------------------------------------------------------------- overview tab

  // The agent: cluster state in numbers, the controls, the reason of a failure.
  _agentCard(agent) {
    const t = this._text;
    const c = agent.cluster;
    const running = this._running(agent);
    const online = agent.online;
    const clusterState = this._state(c.cluster_state)?.state;
    const reach = c.vcenter_reachable || c.proxmox_reachable;
    const sub = [
      c.cluster_state ? this._value(c.cluster_state) : null,
      reach ? `${c.vcenter_reachable ? "vCenter" : "Proxmox API"}: ${this._value(reach)}` : null,
      !online ? t.offline : null,
    ].filter(Boolean);
    const head = this._head(clusterState, agent.title, sub.join(" · "), () => this._moreInfo(c.cluster_state));

    const vmsRunning = this._state(c.vms_running)?.state;
    const vmsTotal = this._state(c.vms_total)?.state;
    const operation = this._state(c.operation);
    const step = running && c.operation_step ? this._value(c.operation_step) : null;
    const metrics = el(
      "div",
      { class: "metrics" },
      c.cluster_cpu_percent && this._metric(t.clusterCpu, c.cluster_cpu_percent, { percent: this._percentOf(c.cluster_cpu_percent) }),
      c.cluster_memory_percent &&
        this._metric(t.clusterMemory, c.cluster_memory_percent, { percent: this._percentOf(c.cluster_memory_percent) }),
      c.hosts_online && this._metric(t.hostsOnline, c.hosts_online, { value: `${this._value(c.hosts_online)} / ${agent.hosts.length}` }),
      c.vms_running &&
        this._metric(t.vmsRunning, c.vms_running, { value: vmsTotal !== undefined ? `${vmsRunning} / ${vmsTotal}` : vmsRunning }),
      c.operation && this._metric(t.operation, c.operation, { value: this._format(operation), extra: step }),
    );

    const card = el("div", { class: "card" }, head, metrics);
    const alert = this._alert(agent);
    if (alert) card.append(alert);
    card.append(this._controls(agent));
    return card;
  }

  _controls(agent) {
    const t = this._text;
    const c = agent.cluster;
    const running = this._running(agent);
    const online = agent.online;
    const armed = this._isOn(c.shutdown_armed);
    const press = (entity) => this._call("button", "press", entity);
    const actions = el("div", { class: "actions" });
    if (c.cluster_start) {
      actions.append(this._button(t.clusterStart, () => press(c.cluster_start), { kind: "primary", disabled: !online || running }));
    }
    if (c.shutdown_preview) {
      actions.append(this._button(t.preview, () => press(c.shutdown_preview), { disabled: !online || running }));
    }
    if (c.shutdown_armed) {
      actions.append(
        el(
          "label",
          { class: "arm" },
          this._switch(c.shutdown_armed, armed, !online || running),
          t.arm,
        ),
      );
    }
    if (c.cluster_shutdown) {
      actions.append(
        this._button(
          t.clusterShutdown,
          () => {
            if (window.confirm(t.confirmShutdown(agent.title))) press(c.cluster_shutdown);
          },
          { kind: "danger", disabled: !online || running || (c.shutdown_armed && !armed) },
        ),
      );
    }
    if (c.cancel_operation) {
      actions.append(this._button(t.cancel, () => press(c.cancel_operation), { disabled: !online || !running }));
    }
    actions.append(el("span", { class: "spacer" }));
    if (c.refresh) actions.append(this._button(t.refresh, () => press(c.refresh), { disabled: !online }));
    if (this._hass?.user?.is_admin) {
      const busy = Boolean(this._logBusy[agent.entry_id]);
      actions.append(
        this._button(busy ? t.downloading : t.downloadLog, () => this._downloadLog(agent), { disabled: busy || !online }),
      );
    }
    if (c.cluster_shutdown && c.shutdown_armed && !armed && online && !running) {
      actions.append(el("div", { class: "hint" }, t.armHint));
    }
    if (this._logError[agent.entry_id]) {
      actions.append(el("div", { class: "log-error" }, this._logError[agent.entry_id]));
    }
    return actions;
  }

  // The reason of the last failed operation, in words.
  _alert(agent) {
    const t = this._text;
    const operation = this._state(agent.cluster.operation);
    if (operation?.state !== "failed") return null;
    const attributes = operation.attributes || {};
    const code = attributes.error_code;
    const key = `component.ha_hypervisor.exceptions.agent_${code}.message`;
    const translated = code ? this._hass.localize?.(key) : "";
    const what = t.kinds[attributes.kind] || t.lastOperation;
    return el(
      "div",
      { class: "alert" },
      el("strong", {}, t.failed(what)),
      el("div", {}, translated && translated !== key ? translated : code || ""),
      attributes.error ? el("div", { class: "detail" }, attributes.error) : null,
      el("div", { class: "detail" }, t.logHint),
    );
  }

  // One server in full: power, CPU and memory, and the VMs it runs.
  _serverCard(agent, host, rows) {
    const t = this._text;
    const e = host.entities;
    const power = this._state(e.host_power)?.state;
    const onCount = rows.filter((row) => row.on).length;
    const sub = [
      e.host_power ? this._value(e.host_power) : null,
      e.host_uptime && power === "on" && Number(this._state(e.host_uptime)?.state) > 0
        ? `${t.uptime} ${this._duration(Number(this._state(e.host_uptime).state))}`
        : null,
      t.vmsOn(onCount, rows.length),
    ].filter(Boolean);
    const head = this._head(power, host.name, sub.join(" · "), () => this._moreInfo(e.host_power || e.host_cpu_percent));
    const metrics = el(
      "div",
      { class: "metrics" },
      e.host_cpu_percent &&
        this._metric(t.cpu, e.host_cpu_percent, {
          extra: this._amount(e.host_cpu_used, (value) => `${this._numbers.format(value)} MHz`),
          percent: this._percentOf(e.host_cpu_percent),
        }),
      e.host_memory_percent &&
        this._metric(t.memory, e.host_memory_percent, {
          extra: this._amount(e.host_memory_used, (value) => this._memory(value)),
          percent: this._percentOf(e.host_memory_percent),
        }),
    );
    const card = el("div", { class: "card" }, head, metrics);
    const locked = this._running(agent) || !agent.online;
    card.append(rows.length ? this._vmTable(this._sortRows(rows), locked) : el("div", { class: "note" }, t.noVms));
    return card;
  }

  _datastoreCard(agent) {
    const t = this._text;
    const n = this._numbers;
    const rows = agent.datastores
      .filter((store) => store.entities.datastore_used_percent)
      .map((store) => {
        const e = store.entities;
        const percent = this._percentOf(e.datastore_used_percent);
        const free = Number(this._state(e.datastore_free)?.state);
        const capacity = Number(this._state(e.datastore_capacity)?.state);
        const space = Number.isFinite(free) && Number.isFinite(capacity) ? t.free(`${n.format(free)} GiB`, `${n.format(capacity)} GiB`) : "";
        return el(
          "tr",
          {},
          el("td", { class: "link", onclick: () => this._moreInfo(e.datastore_used_percent) }, el("div", {}, store.name), el("div", { class: "small" }, space)),
          el("td", { class: "num", style: "width:40%" }, this._value(e.datastore_used_percent), this._bar((percent || 0) / 100)),
        );
      });
    return el("div", { class: "card" }, el("table", {}, el("tbody", {}, rows)));
  }

  _renderOverview(content) {
    const t = this._text;
    const agents = this._layout.agents;
    const blocks = agents.map((agent) => {
      const block = el("div", { class: "agent" }, this._agentCard(agent));
      const rows = this._vmRows(
        agent.vms.filter((vm) => vm.entities.vm_power).map((vm) => ({ power: vm.entities.vm_power, name: vm.name })),
      );
      const placed = new Set();
      if (agent.hosts.length) {
        block.append(el("div", { class: "heading" }, `${t.servers} · ${agent.hosts.length}`));
        for (const host of agent.hosts) {
          const mine = rows.filter((row) => row.host && (row.host === host.id || row.host === host.name));
          mine.forEach((row) => placed.add(row));
          block.append(this._serverCard(agent, host, mine));
        }
      }
      const others = rows.filter((row) => !placed.has(row));
      if (others.length) {
        block.append(el("div", { class: "heading" }, t.otherVms));
        block.append(el("div", { class: "card" }, this._vmTable(this._sortRows(others), this._running(agent) || !agent.online)));
      }
      if (agent.datastores.some((store) => store.entities.datastore_used_percent)) {
        block.append(el("div", { class: "heading" }, t.datastores), this._datastoreCard(agent));
      }
      return block;
    });
    content.replaceChildren(...blocks);
  }

  async _downloadLog(agent) {
    this._logBusy[agent.entry_id] = true;
    this._logError[agent.entry_id] = "";
    this._build();
    try {
      const result = await this._hass.callWS({ type: "ha_hypervisor/agent_log", entry_id: agent.entry_id, lines: LOG_LINES });
      const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
      const url = URL.createObjectURL(new Blob([`${result.lines.join("\n")}\n`], { type: "text/plain" }));
      const link = el("a", { href: url, download: `ha-hypervisor-${result.agent_id}-${stamp}.log` });
      this.shadowRoot.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (err) {
      this._logError[agent.entry_id] = `${this._text.logFailed}: ${err?.message || err}`;
    } finally {
      this._logBusy[agent.entry_id] = false;
      this._build();
    }
  }

  // ------------------------------------------------------------ scenarios tab

  _scenarioCard(agent, scenario) {
    const t = this._text;
    const { scenario_state: stateEntity, scenario_power: power } = scenario.entities;
    const stateObj = this._state(stateEntity);
    const status = stateObj?.state;
    const locked = this._running(agent) || !agent.online;
    const rows = this._vmRows((scenario.members || []).map((member) => ({ power: member.power, name: member.name })));
    const onCount = rows.filter((row) => row.on).length;
    const isOn = power ? this._isOn(power) : ["active", "partial", "starting"].includes(status);
    const action =
      power &&
      this._button(
        isOn ? t.stop : t.start,
        () => {
          if (isOn && !window.confirm(t.confirmStop(scenario.name))) return;
          this._call("switch", isOn ? "turn_off" : "turn_on", power);
        },
        { kind: isOn ? "" : "primary", disabled: locked },
      );
    const sub = [stateObj ? this._format(stateObj) : null, t.vmsOn(onCount, rows.length)].filter(Boolean).join(" · ");
    const head = this._head(status, scenario.name, sub, () => this._moreInfo(stateEntity || power), action);
    const metrics = el(
      "div",
      { class: "metrics" },
      this._metric(t.cpu, scenario.entities.scenario_cpu),
      this._metric(t.memory, scenario.entities.scenario_memory),
      this._metric(t.storage, scenario.entities.scenario_storage),
    );
    // Start order, as the scenario starts them.
    const table = rows.length ? this._vmTable(rows, locked) : el("div", { class: "note" }, t.noVms);
    return el("div", { class: "card" }, head, metrics, table);
  }

  _renderScenarios(content) {
    const t = this._text;
    const agents = this._layout.agents;
    const blocks = agents.map((agent) => {
      const block = el("div", { class: "agent" });
      if (agents.length > 1 || !agent.online) {
        block.append(el("div", { class: "heading" }, agent.title, " ", !agent.online && el("span", { class: "badge" }, t.offline)));
      }
      if (!agent.scenarios.length) block.append(el("div", { class: "message" }, t.noScenarios));
      for (const scenario of agent.scenarios) block.append(this._scenarioCard(agent, scenario));
      return block;
    });
    content.replaceChildren(...blocks);
  }

  // ------------------------------------------------------------------ build

  _build() {
    const layout = this._layout;
    if (!layout) return;
    this._viewSignature = this._viewState();
    this._renderTabs();
    if (!layout.agents.length) {
      this._showMessage(this._text.empty);
      return;
    }
    const content = this.shadowRoot.querySelector(".content");
    if (this._tab === "scenarios") this._renderScenarios(content);
    else this._renderOverview(content);
  }
}

if (!customElements.get("ha-hypervisor-panel")) {
  customElements.define("ha-hypervisor-panel", HaHypervisorPanel);
}
