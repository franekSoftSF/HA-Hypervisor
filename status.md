# Status

Version 0.2.0 (renamed from HA VMware), built for Home Assistant 2026.11.

## Done
- Agent: statistics, VM power, cluster shutdown and start, scenarios, signed MQTT protocol, Redfish, IPMI, WOL.
- Integration: config flow, entities, scenario wizard, actions, EN and PL translations, diagnostics.
- Tests in Docker on the real Home Assistant 2026.11 image, hassfest, end-to-end stack with `vcsim`.

## Not verified on real hardware
- ESXi host shutdown (`vcsim` does not implement it), full shutdown and start sequence on a real vCenter.
- Redfish and IPMI on the Supermicro BMC, Wake on LAN.
- vCLS Retreat Mode through the advanced setting on the installed vSphere version.
- MQTT over WebSocket through the real reverse proxy.

## Next
1. Deploy the agent and run `check`, then a shutdown preview, then a real cycle.
2. Proxmox VE driver next to VMware, after the test on real hardware passes. Open: one platform per environment or mixed.
3. Possible additions: disk and network statistics through PerformanceManager, BMC sensors (power draw, temperature).
