# Status

Version 0.10.0, built for Home Assistant 2026.11. Agent 0.3.1 runs on the real Supermicro lab (172.16.2.20, vCenter HomeLAB).

## Done
- Agent: statistics, VM power, cluster shutdown and start, scenarios, signed MQTT protocol, Redfish, IPMI, WOL.
- Integration: config flow, entities, scenario wizard, actions, EN and PL translations, diagnostics.
- Agent packaged as .deb (amd64, arm64) with a systemd service and a CLI for MQTT, vCenter and ESXi connections.
- Automatic Hypervisor sidebar panel (frontend/panel.js plus the ha_hypervisor/dashboard websocket command).
- Proxmox VE 9 agent (pve.py, pve_operations.py): one node, cluster, cluster with HA. HA guests stop through the HA
  manager and HA resources are started again after quorum. CLI proxmox set and node add. Tested against a simulated
  Proxmox API (tests/agent/fake_pve.py), also end to end over HTTPS with Wake on LAN.
- Several agents on one machine: systemd template ha-hypervisor-agent@NAME, CLI --instance NAME, own state file.
- Snapshots on both platforms: create, revert, delete actions in HA, Snapshots sensor per VM, scenario option
  "revert to snapshot before start". Preflight checks every VM first; vCenter and protected VMs are never reverted.
- Agent on the Proxmox node itself (the user's single node setup): own node last, completed reported first, no remote
  start possible. An agent in a guest of the same node is not supported.
- Hosts without BMC or Wake on LAN: manual_power_on (CLI --manual-power-on) allows the shutdown, the start waits
  for the power button. Compact panel layout: overview row, control grid, one VM list running first.
- Scenario switch (on starts, off stops), scenario CPU/memory/storage sums, VM usage on the panel tiles.
- Panel of cards in two tabs: Overview (agent with controls, one card per server with its VMs, datastores) and
  Scenarios (one card per scenario with a per VM usage table). Reason of the last failed
  operation on the Overview tab. Agent log in HA: the agent keeps its last 2000 log lines in memory (logbuffer.py),
  the logs command returns them, the panel downloads them and the diagnostics include them.
- Repairs after every start: agent not answering within a minute, agent older than the integration.
- Agent self update: `update` downloads the release package for the machine, checks its .sha256 and installs it;
  `update --file` installs a downloaded package; `autoupdate enable` turns on a daily systemd timer. Updates wait
  while an operation runs. Not yet tried against the real GitHub releases from a lab machine.
- Tests in Docker on the real Home Assistant 2026.11 image, hassfest, end-to-end stack with `vcsim`.

## Not verified on real hardware
- ESXi host shutdown (`vcsim` does not implement it), full shutdown and start sequence on a real vCenter.
- Redfish and IPMI on the Supermicro BMC, Wake on LAN.
- vCLS Retreat Mode through the advanced setting on the installed vSphere version.
- MQTT over WebSocket through the real reverse proxy.

## Next
1. On the Supermicro (no BMC configured): esxi add 172.16.2.20 --update --manual-power-on, then a preview, then a
   real cycle with the power button.
2. Proxmox on the real node: proxmox set, node add, node test, then preview, shutdown, start.
3. Mixed scenarios across DCs, see the plan below.
4. Possible additions: disk and network statistics through PerformanceManager, BMC sensors (power draw, temperature).

## Plan: mixed mode (Proxmox itself is done in 0.5.0)

- One agent is one DC, one platform per DC. DCs are in separate networks, each agent must see its own BMCs. Each DC is
  a separate config entry in Home Assistant, which already works.
- Mixed scenario (new, in Home Assistant): an ordered list of existing per-DC scenarios. HA starts one, waits for its
  operation to finish, then starts the next; stops in reverse. Own state, buttons and action. If HA or the broker fails
  midway, the running DC scenario is finished by its agent, the next does not start, and HA reports an error.
- All-DC shutdown: an action sending the command to every agent, with the same arming as for one cluster.
- The user's Proxmox: VE 9, a single node homelab; the code must keep working for clusters with and without HA.

## Requested next (2026-11-10, after 0.5.1)

The user runs two agents: one for vCenter, one for Proxmox (now possible with `--instance`).
1. Snapshots: done in 0.6.0.
2. Mixed scenarios in Home Assistant: one scenario with VMs from both agents (vCenter and Proxmox), run by HA in
   order, each step sent to the agent that owns the VM.
3. Schedules and automations: the existing switches, buttons and actions already work in HA automations; add
   actions that report completion (wait for the operation) and examples or blueprints, e.g. shut VMs down after a task.
4. Discussed, not started (2026-11-11): Intel AMT power control for the HP EliteDesk 800 G2 running Proxmox, and
   hardware sensors from BMCs over Redfish (iDRAC, iLO, Supermicro): temperatures, power draw, fans, health.
