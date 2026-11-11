# HA-Hypervisor - project instructions

Home Assistant integration (installed from the HA Marketplace, which replaced HACS in 2026.11) plus an agent outside the cluster that controls a hypervisor lab (VMware vSphere today). All HA to agent
communication is MQTT. Current state: `status.md`. Talk to the user in Polish; code, logs and docs in English, with
Polish translations next to English ones.

## Layout
- `custom_components/ha_hypervisor/` - integration. `link.py` is the MQTT side, scenarios are config subentries.
- `agent/ha_hypervisor_agent/` - agent: `vim.py` (vSphere), `power.py` (Redfish, IPMI, WOL), `inventory.py` (state),
  `operations.py` (sequences), `service.py` (MQTT), `store.py` (what survives the cluster being off), `cli.py` (commands).
- `packaging/deb/` - the .deb with its own Python, built by `.github/workflows/packages.yml` for every release.
- `tests/agent`, `tests/integration`, `tests/e2e`.

## Rules
- Always target the newest Home Assistant release. Before a change check the contributors blog for new deprecations
  and bump `hacs.json` (the Marketplace still reads it). Release tags equal the manifest version. `tests/docker/integration.Dockerfile` runs on the real current image.
- Versioning from 0.10.0: a release normally bumps the patch (0.10.1, 0.10.2, ...) up to at most x.y.20; the release
  after x.y.20 is the next minor x.(y+1).0. A significant change bumps the minor earlier (patch back to 0), the biggest
  changes bump the major (x+1.0.0). Propose minor or major to the user and let them decide.
- `protocol.py` exists twice and must stay byte-identical (tested). Edit one, copy to the other.
- Commands are never retained, always signed, always expiring. Do not add a queued or retained command path.
- New error code or step: add to `protocol.py` and to both translation files (tested).
- Everything that can fail must fail in preflight, before anything is stopped. Never force off the vCenter VM.
- Flows must use `async_update_and_abort`: the entry has an update listener, reloading methods are an error from 2026.12.
- Passwords are never command line arguments: prompt, stdin or an environment reference.
- Never run `python` on the Windows dev PC (Store stub hangs). Everything runs in Docker.
- In `vim.py` read leaf properties through the property collector, not whole objects such as `vm.runtime`.

## Verification
`.\tests\run.ps1 -E2E` and, for packaging or CLI changes, `.\tests\run-deb.ps1`. Not testable locally: real ESXi shutdown, BMC power on, vCLS Retreat on a real cluster, WOL.
