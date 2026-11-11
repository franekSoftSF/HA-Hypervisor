// Sidebar panel of HA Hypervisor. Built from the standard cards, its layout
// comes from the ha_hypervisor/dashboard websocket command, so new hosts, VMs
// and scenarios show up without any configuration.

const TEXT = {
  en: {
    title: "Hypervisor",
    overview: "Overview",
    control: "Control",
    hosts: "Hosts",
    datastores: "Datastores",
    vms: "Virtual machines",
    vmsCount: (on, total) => `${on} of ${total} running`,
    cpu: "CPU",
    memory: "Memory",
    storage: "Storage",
    scenarios: "Scenarios",
    offline: "agent offline",
    empty: "No agent is set up yet.",
    confirm: "Shut the whole cluster down?",
    none: "None",
  },
  pl: {
    title: "Hypervisor",
    overview: "Przegląd",
    control: "Sterowanie",
    hosts: "Hosty",
    datastores: "Magazyny danych",
    vms: "Maszyny wirtualne",
    vmsCount: (on, total) => `włączone ${on} z ${total}`,
    cpu: "CPU",
    memory: "Pamięć",
    storage: "Dysk",
    scenarios: "Scenariusze",
    offline: "agent offline",
    empty: "Nie dodano jeszcze żadnego agenta.",
    confirm: "Wyłączyć cały klaster?",
    none: "Brak",
  },
};

const OVERVIEW_KEYS = [
  "cluster_state",
  "vcenter_reachable",
  "proxmox_reachable",
  "cluster_cpu_percent",
  "cluster_memory_percent",
  "hosts_online",
  "vms_running",
  "operation",
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

// A VM: running ones show CPU, memory and disk (the icon colour tells it is on),
// stopped ones their state and disk.
const vmTile = (entity, on) =>
  tile(entity, "device", { state_content: on ? ["cpu", "memory", "disk"] : ["state", "disk"] });

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

  // What changes the layout: VMs moving between on and off, an operation
  // starting or ending (its step is shown only while it runs).
  _vmGrouping() {
    if (!this._layout) return "";
    return this._layout.agents
      .map(
        (agent) =>
          (this._running(agent) ? "r" : "-") +
          agent.vms.map((vm) => (this._isOn(vm.entities.vm_power) ? "1" : "0")).join(""),
      )
      .join("|");
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
        .sections {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
          gap: 20px 16px;
          align-items: start;
        }
        .sections > .full { grid-column: 1 / -1; }
        .tiles {
          display: grid;
          grid-template-columns: repeat(auto-fill, minmax(var(--min, 160px), 1fr));
          gap: 8px;
          margin-top: 4px;
        }
        .tiles.fit { grid-template-columns: repeat(auto-fit, minmax(var(--min, 160px), 1fr)); }
        .tiles + .tiles { margin-top: 12px; }
        .tiles + .tiles.tight { margin-top: 8px; }
        .tiles > .span { grid-column: 1 / -1; }
        @media (max-width: 600px) {
          .content { padding: 8px; }
          .sections { grid-template-columns: 1fr; }
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

  _running(agent) {
    const operation = agent.cluster.operation;
    return operation && this._hass?.states[operation]?.state === "running";
  }

  // A section is a heading and groups of tiles; each group is a grid whose
  // columns are at least `min` pixels wide, so it adapts to the screen.
  _sections(agent) {
    const t = this._text;
    const sections = [];
    const add = (heading, groups, options = {}) => {
      const filled = groups.filter((group) => group.tiles.length);
      if (filled.length) sections.push({ heading, groups: filled, ...options });
    };
    const small = (entity, extra = {}) => tile(entity, "entity", { vertical: true, ...extra });

    const overview = OVERVIEW_KEYS.filter((key) => agent.cluster[key]).map((key) => small(agent.cluster[key]));
    if (this._running(agent) && agent.cluster.operation_step) {
      overview.push(small(agent.cluster.operation_step));
    }
    add(t.overview, [{ min: 150, tiles: overview }], { full: true });

    const control = CONTROL_KEYS.filter((key) => agent.cluster[key]).map((key) => {
      const entity = agent.cluster[key];
      if (key === "shutdown_armed") return small(entity);
      return { ...press(entity, key === "cluster_shutdown" ? t.confirm : null), vertical: true };
    });
    add(t.control, [{ min: 170, tiles: control }]);

    // Each host: its state on one row, CPU and memory sharing the next one.
    const hosts = agent.hosts.flatMap((host) => [
      { min: 200, tiles: [host.entities.host_power && tile(host.entities.host_power, "device")].filter(Boolean) },
      {
        min: 110,
        fit: true,
        tight: true,
        tiles: [
        host.entities.host_cpu_percent && tile(host.entities.host_cpu_percent, "entity", { name: t.cpu }),
        host.entities.host_memory_percent &&
          tile(host.entities.host_memory_percent, "entity", { name: t.memory }),
        ].filter(Boolean),
      },
    ]);
    add(t.hosts, hosts);

    // Each scenario: its state, which also switches it on and off, what its VMs
    // use together, and the VMs themselves with their power state.
    const scenarios = agent.scenarios.flatMap((scenario) => {
      const { scenario_state: state, scenario_power: power } = scenario.entities;
      const toggle = power && {
        tap_action: {
          action: "perform-action",
          perform_action: "switch.toggle",
          target: { entity_id: power },
        },
        icon_tap_action: {
          action: "perform-action",
          perform_action: "switch.toggle",
          target: { entity_id: power },
        },
      };
      const head = state ? tile(state, "device", toggle || {}) : power && tile(power, "device");
      const usage = [
        ["scenario_cpu", t.cpu],
        ["scenario_memory", t.memory],
        ["scenario_storage", t.storage],
      ]
        .filter(([key]) => scenario.entities[key])
        .map(([key, name]) => tile(scenario.entities[key], "entity", { name }));
      return [
        { min: 200, tiles: [head].filter(Boolean) },
        { min: 90, fit: true, tight: true, tiles: usage },
        {
          min: 260,
          tight: true,
          tiles: (scenario.vms || []).map((entity) => vmTile(entity, this._isOn(entity))),
        },
      ];
    });
    add(t.scenarios, scenarios);

    const datastores = agent.datastores
      .filter((store) => store.entities.datastore_used_percent)
      .map((store) => tile(store.entities.datastore_used_percent, "device"));
    add(t.datastores, [{ min: 150, tiles: datastores }]);

    // One list, running first, so a long list stays easy to scan.
    const vms = agent.vms.filter((vm) => vm.entities.vm_power);
    const on = vms.filter((vm) => this._isOn(vm.entities.vm_power));
    const off = vms.filter((vm) => !this._isOn(vm.entities.vm_power));
    add(
      `${t.vms} · ${t.vmsCount(on.length, vms.length)}`,
      [{ min: 260, tiles: [...on, ...off].map((vm) => vmTile(vm.entities.vm_power, this._isOn(vm.entities.vm_power))) }],
      { full: true },
    );
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
      grid.className = "sections";
      for (const section of this._sections(agent)) {
        const box = document.createElement("section");
        if (section.full) box.className = "full";
        const heading = this._createCard(helpers, { type: "heading", heading: section.heading });
        box.appendChild(heading);
        cards.push(heading);
        for (const group of section.groups) {
          const tiles = document.createElement("div");
          tiles.className = "tiles";
          tiles.style.setProperty("--min", `${group.min}px`);
          // fit: few tiles share the whole width instead of leaving gaps.
          if (group.fit) tiles.classList.add("fit");
          if (group.tight) tiles.classList.add("tight");
          for (const config of group.tiles) {
            const { grid: span, ...cardConfig } = config;
            const card = this._createCard(helpers, cardConfig);
            if (span === "full") card.classList.add("span");
            tiles.appendChild(card);
            cards.push(card);
          }
          box.appendChild(tiles);
        }
        grid.appendChild(box);
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
