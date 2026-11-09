# HA-Vmware - project instructions

Home Assistant integration (HACS) plus an agent outside the cluster that controls a vCenter lab. All HA to agent
communication is MQTT. Current state: `status.md`. Talk to the user in Polish; code, logs and docs in English, with
Polish translations next to English ones.

## Layout
- `custom_components/ha_vmware/` - integration. `link.py` is the MQTT side, scenarios are config subentries.
- `agent/ha_vmware_agent/` - agent: `vim.py` (vSphere), `power.py` (Redfish, IPMI, WOL), `inventory.py` (state),
  `operations.py` (sequences), `service.py` (MQTT), `store.py` (what survives the cluster being off).
- `tests/agent`, `tests/integration`, `tests/e2e`.

## Rules
- Always target the newest Home Assistant release. Before a change check the contributors blog for new deprecations
  and bump `hacs.json`. `tests/docker/integration.Dockerfile` runs on the real current image.
- `protocol.py` exists twice and must stay byte-identical (tested). Edit one, copy to the other.
- Commands are never retained, always signed, always expiring. Do not add a queued or retained command path.
- New error code or step: add to `protocol.py` and to both translation files (tested).
- Everything that can fail must fail in preflight, before anything is stopped. Never force off the vCenter VM.
- Flows must use `async_update_and_abort`: the entry has an update listener, reloading methods are an error from 2026.12.
- Never run `python` on the Windows dev PC (Store stub hangs). Everything runs in Docker.
- In `vim.py` read leaf properties through the property collector, not whole objects such as `vm.runtime`.

## Verification
`.\tests\run.ps1 -E2E`. Not testable locally: real ESXi shutdown, BMC power on, vCLS Retreat on a real cluster, WOL.
