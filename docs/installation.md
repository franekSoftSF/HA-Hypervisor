# Installation

Polish version: [pl/installation.md](pl/installation.md).

Order matters: the broker first, then the agent, and Home Assistant last, because adding the integration checks that
the agent answers.

```
Home Assistant + integration  <->  Mosquitto  <--MQTT over TLS--  agent
                                                                    |-- vCenter API (443)
                                                                    |-- ESXi API (443)
                                                                    '-- BMC: Redfish (443), IPMI (623/udp), Wake on LAN
```

## 1. Requirements

| What | Details |
|---|---|
| Home Assistant | 2026.11 or newer, with the MQTT integration. HACS is not needed, the built-in Marketplace replaced it |
| Machine for the agent | Always on, **outside the cluster**, in the vCenter management network, with Debian or Ubuntu (or Docker). A Raspberry Pi, a mini PC or a VM on other hardware |
| Network from the agent | vCenter and every ESXi on 443, every BMC on 443 and 623/udp, the broker |
| Clock | NTP on the agent and on Home Assistant. A command expires after two minutes |
| Hosts | Host names exactly as shown in vCenter |

Do not put the agent on the cluster or on the vCenter appliance. It would stop together with the cluster, and nothing
could start the cluster again.

## 2. Broker

Home Assistant and the agent must use the same broker. These steps use the Mosquitto broker app in Home Assistant.
With your own Mosquitto, apply the same account and ACL.

### Account

In the Mosquitto app configuration, add a login for the agent:

```yaml
logins:
  - username: ha-hypervisor-agent
    password: use-a-long-random-password
```

Enable the ACL so that this account can only use its own topics:

```yaml
customize:
  active: true
  folder: mosquitto
```

`/share/mosquitto/acl.conf`:

```
acl_file /share/mosquitto/accesscontrollist
```

`/share/mosquitto/accesscontrollist`, with `lab` replaced by your `agent_id`. Home Assistant needs its full access:

```
user addons
topic readwrite #

user homeassistant
topic readwrite #

user ha-hypervisor-agent
topic readwrite ha_hypervisor/lab/#
```

Restart the Mosquitto app.

### How the agent reaches the broker

**Option A: MQTT over WebSocket through your reverse proxy (port 443).** No new port is opened. The Mosquitto app
listens for WebSocket on 1884. nginx example:

```nginx
server {
    listen 443 ssl;
    server_name mqtt.example.com;
    # ssl_certificate and ssl_certificate_key as for your other sites

    location /mqtt {
        proxy_pass http://HOME_ASSISTANT_IP:1884;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

In Nginx Proxy Manager add a proxy host to port 1884 and turn on **Websockets Support**.

**Option B: MQTT over TLS on port 8883.** Give the Mosquitto app a certificate (`certfile`, `keyfile`) and forward
port 8883 to it.

Never expose 1883 or 1884 directly to the internet.

## 3. vSphere accounts

### vCenter

Create a role and assign it to the agent account on the vCenter root, with propagation:

| Privilege | Needed for |
|---|---|
| Virtual machine, Interaction, Power on | Starting VMs |
| Virtual machine, Interaction, Power off | Stopping VMs, including guest shutdown |
| Global, Settings | vCLS Retreat Mode |

### ESXi

On every host create a local account (for example `svc-ha`) and give it the Administrator role on that host. The agent
needs it to stop and start the vCenter VM and to shut down the host, at moments when vCenter is not running.

With lockdown mode enabled, add the account to **Exception Users**.

## 4. Server power control

Starting the cluster needs a way to power on a host that is completely off.

**Supermicro BMC:**

1. In the BMC web interface enable **IPMI over LAN** and Redfish (Configuration, Port).
2. Create an account allowed to control power (Operator or higher).
3. Give the BMC a static address reachable from the agent, and keep it off public networks.

The agent tries Redfish first, then IPMI. On some boards and firmware versions parts of Redfish need a license. The
agent then falls back to IPMI.

**Wake on LAN** is an optional last resort. The network card and BIOS must support waking from a full shutdown, the
agent must be in the same network segment, and it cannot be verified in advance. Test it once by hand.

## 5. Agent

### Install the package

Packages for Debian 12 or newer and Ubuntu 22.04 or newer, on amd64 and arm64 (Raspberry Pi included). The package
brings its own Python and does not touch the system one.

```bash
VERSION=0.3.2
ARCH=$(dpkg --print-architecture)
wget "https://github.com/franekSoftSF/HA-Hypervisor/releases/download/$VERSION/ha-hypervisor-agent_${VERSION}_$ARCH.deb"
sudo apt install "./ha-hypervisor-agent_${VERSION}_$ARCH.deb"
```

The service is enabled but waits for a configuration.

### Configure the connections

Create the configuration. The command prints the **agent ID and shared key** needed later in Home Assistant:

```bash
sudo ha-hypervisor-agent init --agent-id lab
```

MQTT, with option A from section 2. For option B use `--transport tcp --port 8883`. In a trusted local network a plain connection works too: `--transport tcp --port 1883` (TLS is then off):

```bash
sudo ha-hypervisor-agent mqtt set --host mqtt.example.com --transport websockets --username ha-hypervisor-agent
```

```bash
sudo ha-hypervisor-agent mqtt test
```

vCenter. `--vm-name` is the name of the vCenter VM when it runs on the managed hosts:

```bash
sudo ha-hypervisor-agent vcenter set --host vcenter.lab.local --username svc-ha@vsphere.local --vm-name "VMware vCenter Server"
```

```bash
sudo ha-hypervisor-agent vcenter test
```

The test also lists the hosts vCenter knows, so you can see the exact names to use below. Add each ESXi host with its
BMC:

```bash
sudo ha-hypervisor-agent esxi add esxi01.lab.local --username svc-ha --bmc-address 10.0.0.21 --bmc-username ADMIN
```

```bash
sudo ha-hypervisor-agent esxi test
```

Passwords are asked for interactively and never passed as arguments. Then test everything together and start:

```bash
sudo ha-hypervisor-agent check
```

```bash
sudo systemctl start ha-hypervisor-agent
```

```bash
journalctl -u ha-hypervisor-agent -f
```

Every line of `check` should start with `OK`. `WARN` means the BMC did not answer and only an unverifiable Wake on LAN
is left. The log should say `Connected to the broker`.

### Command reference

| Command | Purpose |
|---|---|
| `init --agent-id NAME` | Create the configuration and a shared key |
| `show` | Print the configuration without secrets, and what is missing |
| `check` | Test all connections, nothing is changed |
| `key show`, `key rotate` | Print or replace the shared key |
| `mqtt set`, `mqtt test` | Broker connection |
| `vcenter set`, `vcenter test` | vCenter connection |
| `esxi add NAME`, `esxi add NAME --update` | Add or change a host |
| `esxi list`, `esxi remove NAME`, `esxi test [NAME]` | Manage and test hosts |

Each command has `--help`. Options of `esxi add` include `--address`, `--bmc-type`, `--wol-mac`, `--wol-broadcast`,
`--no-bmc` and `--no-wol`.

Restart the service after changing the configuration: `sudo systemctl restart ha-hypervisor-agent`. Each command reminds you. The agent connects to Home Assistant as soon as MQTT is set: it shows the cluster as **Not configured** until vCenter and a host are added, so the integration can be added before that.

For scripts, `--password-stdin` reads the password from standard input (`esxi add` reads the ESXi password first, then
the BMC one with `--bmc-password-stdin`). `--password-env NAME` stores only a reference, with the value kept in
`/etc/ha-hypervisor-agent/agent.env`, which the service loads.

The configuration is in `/etc/ha-hypervisor-agent/config.yaml`, readable only by root and the agent.
`apt purge ha-hypervisor-agent` removes it together with the passwords.

### Alternative: Docker

```bash
git clone https://github.com/franekSoftSF/HA-Hypervisor.git
cd HA-Hypervisor/agent
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env config.yaml
docker compose -f docker-compose.example.yml run --rm ha-hypervisor-agent genkey
```

Put the key and the passwords in `.env`, fill in `config.yaml`, then:

```bash
docker compose -f docker-compose.example.yml run --rm ha-hypervisor-agent check --config /config/config.yaml
```

```bash
docker compose -f docker-compose.example.yml up -d
```

## 6. Home Assistant

1. Set up the **MQTT** integration with the same broker, if it is not there yet.
2. Go to **Settings, Marketplace** and accept the warning. Select **Add from link** and connect your GitHub account when
   asked: the Marketplace requires it for repositories added by link.
3. Enter `https://github.com/franekSoftSF/HA-Hypervisor`, choose **Integration** if asked for the type, and select
   **Add**. Open **HA Hypervisor** and install it. Restart only if a repair asks you to.
4. **Settings, Devices & services, Add integration, HA Hypervisor.** Enter `agent_id` and the shared key.

Hosts, VMs and datastores appear as devices within one polling interval.

![Integration page](images/en/integration.png)

### Scenarios

On the integration page choose **Add scenario**, enter a name, and add groups of VMs one by one.

![Scenario wizard](images/en/scenario-wizard.png)

### Dashboard

Create a dashboard, open the raw configuration editor and paste [dashboard.example.yaml](dashboard.example.yaml),
replacing `lab`, the VM names and the scenario names with yours.

![Example dashboard](images/en/dashboard.png)

## 7. First test

Go step by step, ideally when you can reach the server physically:

1. Turn one unimportant VM off and on with its switch.
2. Press **Preview shutdown** and read the `plan` attribute of the **Operation** sensor. Check the VM list, the vCenter
   host and any `wake_unverified` warning.
3. Power on a host from the BMC web interface once, to confirm the BMC works when the host is off.
4. Run the full cycle: **Arm cluster shutdown**, **Shut down cluster**, wait, then **Start cluster**.

## Troubleshooting

| Symptom | Check |
|---|---|
| `Agent not found` when adding the integration | The agent runs and logs `Connected to the broker`, and HA uses the same broker |
| `Different key` when adding the integration | The same `shared_key` in `.env` and in Home Assistant |
| `is not a valid app repository` | This is the Apps store. Integrations are added in **Settings, Marketplace, Add from link** |
| Actions end with "The agent did not answer" | Clocks on both sides (NTP), agent log shows `Rejected command` |
| `Not authorised` in the agent log | The account and ACL of the broker, `agent_id` in the ACL |
| `no_wake_method` during the preview | The BMC does not answer: run `check` |
| `esxi_unavailable` during the preview | ESXi account, lockdown mode, port 443 from the agent |
| `host_not_configured` | `hosts[].name` differs from the name in vCenter |
| Cluster stays `degraded` after a start | vCenter services need several minutes, watch the Operation step |

## Update

Integration: through the Marketplace. Agent: install the new package with `apt install`, the configuration stays. In
Docker: `git pull` and `docker compose -f docker-compose.example.yml up -d --build`.
