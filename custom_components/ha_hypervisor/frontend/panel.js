// Sidebar panel of HA Hypervisor. Built from the standard cards, its layout
// comes from the ha_hypervisor/dashboard websocket command, so new hosts, VMs
// and scenarios show up without any configuration.

const TEXT = {
  en: {
    title: "Hypervisor",
    cluster: "Cluster",
    control: "Control",
    hosts: "Hosts",
    datastores: "Datastores",
    running: "Running VMs",
    stopped: "Stopped VMs",
    scenarios: "Scenarios",
    offline: "agent offline",
    empty: "No agent is set up yet.",
    confirm: "Shut the whole cluster down?",
    none: "None",
  },
  pl: {
    title: "Hypervisor",
    cluster: "Klaster",
    control: "Sterowanie",
    hosts: "Hosty",
    datastores: "Magazyny danych",
    running: "Włączone maszyny",
    stopped: "Wyłączone maszyny",
    scenarios: "Scenariusze",
    offline: "agent offline",
    empty: "Nie dodano jeszcze żadnego agenta.",
    confirm: "Wyłączyć cały klaster?",
    none: "Brak",
  },
};

const CLUSTER_KEYS = [
  "cluster_state",
  "vcenter_reachable",
  "cluster_cpu_percent",
  "cluster_memory_percent",
  "hosts_online",
  "vms_running",
  "operation",
  "operation_step",
];

const CONTROL_KEYS = [
  "cluster_start",
  "shutdown_preview",
  "shutdown_armed",
  "cluster_shutdown",
  "cancel_operation",
  "refresh",
];

const press = (entity, confirmation) => ({
  type: "tile",
  entity,
  name: { type: "entity" },
  hide_state: true,
  vertical: false,
  tap_action: {
    action: "perform-action",
    perform_action: "button.press",
    target: { entity_id: entity },
    ...(confirmation ? { confirmation: { text: confirmation } } : {}),
  },
  icon_tap_action: { action: "none" },
});

const tile = (entity, nameType = "entity", extra = {}) => ({
  type: "tile",
  entity,
  name: { type: nameType },
  ...extra,
});

// The card helpers exist only once the dashboard code is loaded, which does
// not happen when the browser opens this panel first.
async function cardHelpers() {
  if (!window.loadCardHelpers) {
    await customElements.whenDefined("partial-panel-resolver");
    const resolver = document.createElement("partial-panel-resolver");
    resolver.hass = { panels: [{ url_path: "tmp", component_name: "lovelace" }] };
    resolver._updateRoutes();
    await resolver.routerOptions.routes.tmp.load();
  }
  return window.loadCardHelpers();
}

class HaHypervisorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards = [];
    this._layout = null;
    this._signature = null;
    this._grouping = "";
    this._loading = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._renderFrame();
    if (this._menu) this._menu.hass = hass;
    const signature = this._entitySignature();
    if (signature !== this._signature) {
      this._signature = signature;
      this._load();
    } else if (this._layout && this._vmGrouping() !== this._grouping) {
      this._build();
    }
    for (const card of this._cards) card.hass = hass;
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

  _entitySignature() {
    const entities = this._hass?.entities || {};
    return Object.values(entities)
      .filter((entry) => entry.platform === "ha_hypervisor")
      .map((entry) => entry.entity_id)
      .sort()
      .join(",");
  }

  _isOn(entityId) {
    return this._hass?.states[entityId]?.state === "on";
  }

  _vmGrouping() {
    if (!this._layout) return "";
    return this._layout.agents
      .flatMap((agent) => agent.vms)
      .map((vm) => (this._isOn(vm.entities.vm_power) ? "1" : "0"))
      .join("");
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
    this.shadowRoot.innerHTML = `
      <style>
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
        .content {
          padding: 16px;
          max-width: 1600px;
          margin: 0 auto;
        }
        .agent + .agent { margin-top: 32px; }
        .agent-title {
          font-size: 18px;
          font-weight: 500;
          margin: 4px 4px 12px;
          display: flex;
          gap: 8px;
          align-items: baseline;
        }
        .badge {
          font-size: 13px;
          color: var(--error-color);
        }
        .grid {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 16px;
          align-items: start;
        }
        .message {
          padding: 32px 16px;
          text-align: center;
          color: var(--secondary-text-color);
        }
      </style>
      <div class="toolbar"><span class="menu"></span><span class="title"></span></div>
      <div class="content"></div>
    `;
    this.shadowRoot.querySelector(".title").textContent = this._text.title;
    if (customElements.get("ha-menu-button")) {
      this._menu = document.createElement("ha-menu-button");
      this._menu.hass = this._hass;
      this._menu.narrow = this._narrow;
      this.shadowRoot.querySelector(".menu").appendChild(this._menu);
    }
  }

  _showMessage(text) {
    const content = this.shadowRoot.querySelector(".content");
    content.innerHTML = "";
    const message = document.createElement("div");
    message.className = "message";
    message.textContent = text;
    content.appendChild(message);
    this._cards = [];
  }

  _sections(agent) {
    const t = this._text;
    const sections = [];
    const add = (heading, cards, emptyText) => {
      if (!cards.length && !emptyText) return;
      sections.push({ heading, cards, emptyText });
    };

    const cluster = CLUSTER_KEYS.filter((key) => agent.cluster[key]).map((key) =>
      tile(agent.cluster[key]),
    );
    add(t.cluster, cluster);

    const control = CONTROL_KEYS.filter((key) => agent.cluster[key]).map((key) => {
      const entity = agent.cluster[key];
      if (key === "shutdown_armed") return tile(entity);
      return press(entity, key === "cluster_shutdown" ? t.confirm : null);
    });
    add(t.control, control);

    const hosts = agent.hosts.flatMap((host) =>
      [
        host.entities.host_power && tile(host.entities.host_power, "device"),
        // Full names, with several hosts "CPU usage" alone is ambiguous.
        host.entities.host_cpu_percent && { type: "tile", entity: host.entities.host_cpu_percent },
        host.entities.host_memory_percent && { type: "tile", entity: host.entities.host_memory_percent },
      ].filter(Boolean),
    );
    add(t.hosts, hosts);

    const vms = agent.vms.filter((vm) => vm.entities.vm_power);
    const vmTile = (vm) => tile(vm.entities.vm_power, "device", { features_position: "inline" });
    add(t.running, vms.filter((vm) => this._isOn(vm.entities.vm_power)).map(vmTile), t.none);
    add(t.stopped, vms.filter((vm) => !this._isOn(vm.entities.vm_power)).map(vmTile), t.none);

    const scenarios = agent.scenarios.flatMap((scenario) =>
      [
        scenario.entities.scenario_state && tile(scenario.entities.scenario_state, "device"),
        scenario.entities.scenario_start && press(scenario.entities.scenario_start),
        scenario.entities.scenario_stop && press(scenario.entities.scenario_stop),
      ].filter(Boolean),
    );
    add(t.scenarios, scenarios);

    const datastores = agent.datastores
      .filter((store) => store.entities.datastore_used_percent)
      .map((store) => tile(store.entities.datastore_used_percent, "device"));
    add(t.datastores, datastores);
    return sections;
  }

  async _build() {
    const helpers = await cardHelpers();
    const layout = this._layout;
    if (!layout) return;
    this._grouping = this._vmGrouping();
    if (!layout.agents.length) {
      this._showMessage(this._text.empty);
      return;
    }
    const content = this.shadowRoot.querySelector(".content");
    const fragment = document.createDocumentFragment();
    const cards = [];
    for (const agent of layout.agents) {
      const block = document.createElement("div");
      block.className = "agent";
      if (layout.agents.length > 1 || !agent.online) {
        const title = document.createElement("div");
        title.className = "agent-title";
        title.textContent = agent.title;
        if (!agent.online) {
          const badge = document.createElement("span");
          badge.className = "badge";
          badge.textContent = this._text.offline;
          title.appendChild(badge);
        }
        block.appendChild(title);
      }
      const grid = document.createElement("div");
      grid.className = "grid";
      for (const section of this._sections(agent)) {
        const stack = {
          type: "vertical-stack",
          cards: [
            { type: "heading", heading: section.heading },
            section.cards.length
              ? { type: "grid", columns: 1, square: false, cards: section.cards }
              : { type: "markdown", content: section.emptyText },
          ],
        };
        const card = this._createCard(helpers, stack);
        grid.appendChild(card);
        cards.push(card);
      }
      block.appendChild(grid);
      fragment.appendChild(block);
    }
    content.innerHTML = "";
    content.appendChild(fragment);
    this._cards = cards;
  }

  _createCard(helpers, config) {
    const card = helpers.createCardElement(config);
    card.hass = this._hass;
    // A lazily loaded card type replaces itself once its code has arrived.
    card.addEventListener(
      "ll-rebuild",
      (ev) => {
        ev.stopPropagation();
        const fresh = this._createCard(helpers, config);
        card.replaceWith(fresh);
        this._cards = this._cards.map((item) => (item === card ? fresh : item));
      },
      { once: true },
    );
    return card;
  }
}

if (!customElements.get("ha-hypervisor-panel")) {
  customElements.define("ha-hypervisor-panel", HaHypervisorPanel);
}
