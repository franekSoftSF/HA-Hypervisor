# Status

Version 0.3.0 (renamed from HA VMware), built for Home Assistant 2026.11.

## Done
- Agent: statistics, VM power, cluster shutdown and start, scenarios, signed MQTT protocol, Redfish, IPMI, WOL.
- Integration: config flow, entities, scenario wizard, actions, EN and PL translations, diagnostics.
- Agent packaged as .deb (amd64, arm64) with a systemd service and a CLI for MQTT, vCenter and ESXi connections.
- Tests in Docker on the real Home Assistant 2026.11 image, hassfest, end-to-end stack with `vcsim`.

## Not verified on real hardware
- ESXi host shutdown (`vcsim` does not implement it), full shutdown and start sequence on a real vCenter.
- Redfish and IPMI on the Supermicro BMC, Wake on LAN.
- vCLS Retreat Mode through the advanced setting on the installed vSphere version.
- MQTT over WebSocket through the real reverse proxy.

## Next
1. Deploy the agent and run `check`, then a shutdown preview, then a real cycle.
2. Proxmox VE and mixed scenarios, only after step 1 passes. See the plan below.
3. Possible additions: disk and network statistics through PerformanceManager, BMC sensors (power draw, temperature).

## Plan: Proxmox and mixed mode (agreed, starts after the test of 0.3.0 on the Supermicro server)

- One agent is one DC, one platform per DC. DCs are in separate networks, each agent must see its own BMCs. Each DC is
  a separate config entry in Home Assistant, which already works.
- Agent: add `platform: vmware | proxmox` to the configuration. Proxmox is a driver next to the VMware code. BMC, MQTT,
  signing and per-DC scenarios stay shared. Each DC can have several servers.
- CLI: `init --platform proxmox`, `proxmox set --host ... --token-id ...`, `proxmox node add NAME --bmc-address ...`.
- Proxmox specifics: API on every node, VMs and LXC, QEMU Guest Agent or ACPI for clean shutdown, stop Proxmox HA
  services before shutdown so guests are not moved, watch quorum while nodes go down. Verify against current API docs.
- Mixed scenario (new, in Home Assistant): an ordered list of existing per-DC scenarios. HA starts one, waits for its
  operation to finish, then starts the next; stops in reverse. Own state, buttons and action. If HA or the broker fails
  midway, the running DC scenario is finished by its agent, the next does not start, and HA reports an error.
- All-DC shutdown: an action sending the command to every agent, with the same arming as for one cluster.
- Ask the user first: Proxmox version, number of nodes, whether it is a cluster, whether Proxmox HA is used.
