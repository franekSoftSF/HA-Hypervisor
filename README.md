# HA VMware

Home Assistant control of a vCenter lab that Home Assistant cannot reach directly: statistics, VM power,
cluster shutdown and start, and start scenarios. Polish version: [README.pl.md](README.pl.md).

```
Home Assistant + this integration  <->  Mosquitto  <--MQTT over TLS--  agent
                                                                         |-- vCenter API
                                                                         |-- ESXi API
                                                                         '-- BMC (Redfish, IPMI), Wake on LAN
```

The agent opens the connection, so nothing needs to reach into the vCenter network. vSphere and BMC credentials
stay on the agent.

**The agent must run outside the cluster**, on an always-on machine in the management network. Otherwise it stops
with the cluster and nothing can start it again.

Requires Home Assistant 2026.11 or newer with the MQTT integration.

## What you get

| Device | Entities |
|---|---|
| Agent / cluster | cluster state, CPU and memory usage, hosts online, VMs running, operation and step, buttons: preview shutdown, shut down, start, cancel, refresh, arm switch |
| Host | CPU, memory, uptime, power state, connection |
| VM | power switch (guest shutdown), force power off, CPU, memory, storage, IP, host, VMware Tools |
| Datastore | used %, free, capacity |
| Scenario | start, stop, state |

Numeric sensors have a state class, so Home Assistant keeps long-term statistics. Values come from vSphere quick
stats: disk and network throughput are not collected.

## Installation

### 1. Broker

Use Mosquitto next to Home Assistant, with a separate account for the agent limited to its topics:

```
user ha-vmware-agent
topic readwrite ha_vmware/lab/#
```

With a public HTTPS address, the simplest path is MQTT over WebSocket through the same reverse proxy (port 443), so
no new port is opened. Direct TLS on 8883 also works. Never expose an unencrypted listener or anonymous access.

### 2. Agent

```bash
cd agent
cp config.example.yaml config.yaml
docker compose -f docker-compose.example.yml run --rm --entrypoint ha-vmware-agent ha-vmware-agent genkey
```

Fill in `config.yaml` and put the secrets in `.env`. Then test all connections without changing anything, and start:

```bash
docker compose -f docker-compose.example.yml run --rm ha-vmware-agent check --config /config/config.yaml
```

```bash
docker compose -f docker-compose.example.yml up -d
```

The clock of the agent must be correct (NTP): a command expires after two minutes.

Accounts needed:

- **vCenter**: a role with VM power operations, guest shutdown, and `Sessions.ValidateSession`; to use vCLS Retreat
  Mode also permission to change advanced settings.
- **ESXi**: a local account on every host that can power VMs and shut down the host. Lockdown mode must allow it.
- **BMC**: an account with power control. On Supermicro enable IPMI over LAN or Redfish in the BMC settings.

### 3. Home Assistant

1. HACS, Custom repositories, add this repository as an Integration, and install **HA VMware**.
2. Restart Home Assistant.
3. Settings, Devices & services, Add integration, **HA VMware**: enter `agent_id` and `shared_key`.

## Shutting the cluster down

1. **Preview shutdown** builds the plan. It appears in the attributes of the Operation sensor and changes nothing.
2. **Arm cluster shutdown** unlocks the button for 60 seconds.
3. **Shut down cluster** stops VMs, enters vCLS Retreat Mode, stops vCenter, then the hosts.

Before anything stops the agent checks vCenter, ESXi logins, and that each host can be started again. The shutdown
is refused if it fails, or if a VM from `protected_vms` is running. The vCenter VM is never forced off.

**Start cluster** powers on the hosts, starts vCenter through ESXi and leaves Retreat Mode.

## Scenarios

Devices & services, HA VMware, **Add scenario**. A scenario is an ordered list of groups: VMs in a group start
together and the next group waits for them (VMware Tools, power state or nothing, plus an extra delay).

Starting a scenario also powers on the hosts it needs, and vCenter, when they are off. Stopping works in reverse
order and leaves VMs used by another active scenario running. From automations:

```yaml
action: ha_vmware.start_scenario
data:
  scenario: Test VDI
```

## Security

- Commands are signed with HMAC-SHA256 over the content and topic, expire after two minutes, can only be used once,
  and are never retained. A broker account alone cannot issue commands.
- Agent data is signed too, a forged state is ignored.
- The shared key is stored in the Home Assistant config entry and in the agent configuration.

## Development

```powershell
.\tests\run.ps1 -E2E
```

Everything runs in Docker: agent tests, integration tests on the current Home Assistant image, hassfest, and the
full stack against `vcsim` (a vSphere API simulator). What the simulator cannot prove: real shutdown of ESXi and BMC
power control need a test on real hardware.
