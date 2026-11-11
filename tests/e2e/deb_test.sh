#!/bin/sh
# Installs the package on a clean system and drives it only through its CLI.
# Runs on the network of the e2e stack: broker, vcsim (vCenter) and esxi (ESXi).
set -eu

NAME=ha-hypervisor-agent
CONFIG=/etc/$NAME/config.yaml
PY=/opt/$NAME/python/bin/python3

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends "/debs/${NAME}_"*"_$(dpkg --print-architecture).deb" >/dev/null
echo "installed: $($NAME --version)"

test "$(stat -c '%U:%G %a' /etc/$NAME)" = "root:$NAME 750"
test "$(stat -c '%U %a' /var/lib/$NAME)" = "$NAME 750"

$NAME init --agent-id debtest
test "$(stat -c '%U:%G %a' $CONFIG)" = "root:$NAME 640"
# As a user would type it: the plain port alone must turn TLS off.
$NAME mqtt set --host mosquitto --transport tcp --port 1883
$NAME mqtt test

echo "--- MQTT alone is enough to reach Home Assistant"
cat > /tmp/probe.py <<'EOF'
import sys, threading
import paho.mqtt.client as mqtt
from ha_hypervisor_agent.protocol import unseal

key, expected = sys.argv[1], sys.argv[2].split(",")
root = "ha_hypervisor/debtest/"
seen = {}
done = threading.Event()

def connected(client, *_):
    client.subscribe(root + "#", qos=1)

def message(_client, _data, msg):
    seen[msg.topic[len(root):]] = msg
    if {"status", "info", "state"} <= set(seen):
        done.set()

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = connected
client.on_message = message
client.connect("mosquitto", 1883)
client.loop_start()
assert done.wait(15), f"only got {sorted(seen)}"
client.loop_stop()
assert seen["status"].payload == b"online"
state = unseal(key, root + "state", seen["state"].payload).body
assert state["cluster"] in expected, state["cluster"]
print("agent published signed state:", state["cluster"], len(state["vms"]), "VMs")
EOF
setpriv --reuid="$NAME" --regid="$NAME" --clear-groups $NAME run --config $CONFIG &
AGENT=$!
sleep 6
$PY /tmp/probe.py "$($NAME key show)" not_configured
kill -TERM $AGENT
wait $AGENT

printf 'pass\n' | $NAME vcenter set --host vcsim --username user --password-stdin
$NAME vcenter test
printf 'pass\n' | $NAME esxi add DC0_C0_H0 --address esxi --username user --password-stdin --wol-mac 00:25:90:00:00:01
$NAME esxi test
$NAME esxi list
$NAME show
$NAME check

echo "--- wrong broker address is reported"
cp $CONFIG /tmp/good.yaml
$NAME mqtt set --host nothing-here.invalid --no-tls
if $NAME mqtt test; then echo "expected a failure"; exit 1; fi
cp /tmp/good.yaml $CONFIG
chown root:$NAME $CONFIG

echo "--- running as the service user"
KEY=$($NAME key show)
setpriv --reuid="$NAME" --regid="$NAME" --clear-groups $NAME run --config $CONFIG &
AGENT=$!
sleep 8
$PY /tmp/probe.py "$KEY" on,degraded
kill -TERM $AGENT
wait $AGENT
test -s /var/lib/$NAME/state.json
test "$(stat -c '%U' /var/lib/$NAME/state.json)" = "$NAME"

echo "--- Proxmox VE agent next to it, own configuration (HTTPS with a self-signed certificate)"
PVE=/etc/$NAME/pve.yaml
$NAME init --agent-id pve --config $PVE >/dev/null
$NAME mqtt set --host mosquitto --no-tls --config $PVE >/dev/null
printf '0b6b0bf2-7a56-4c4b-9f0e-6f5e1f1c2b10\n' | $NAME proxmox set --token-id 'ha-hypervisor@pve!agent' --secret-stdin --config $PVE
if printf 'pass\n' | $NAME vcenter set --host x --username u --password-stdin --config $PVE 2>/dev/null; then
    echo "a Proxmox agent accepted vCenter"; exit 1
fi
$NAME node add pve --address pve --wol-mac 02:00:00:00:00:01 --wol-broadcast pve --config $PVE
$NAME node test --config $PVE | tee /tmp/node.txt
grep -q "OK    Proxmox pve: 9.0.10, single node" /tmp/node.txt
$NAME check --config $PVE

echo "--- update from a downloaded package, checked against its .sha256"
DEB="/debs/${NAME}_$($NAME --version)_$(dpkg --print-architecture).deb"
$NAME update --file "$DEB" | grep -q "already installed"
$NAME update --file "$DEB" --force | tee /tmp/update.txt
grep -q "Checksum OK" /tmp/update.txt
test "$($NAME key show)" = "$KEY"
cp "$DEB" /tmp/broken.deb
printf 'x' >> /tmp/broken.deb
sha256sum "$DEB" | sed 's|  .*|  broken.deb|' > /tmp/broken.deb.sha256
if $NAME update --file /tmp/broken.deb --force 2>/tmp/err.txt; then echo "installed a damaged package"; exit 1; fi
grep -q "damaged or altered" /tmp/err.txt

echo "--- upgrade keeps the configuration"
dpkg -i "/debs/${NAME}_"*"_$(dpkg --print-architecture).deb" >/dev/null
test "$($NAME key show)" = "$KEY"

echo "--- purge removes secrets and the user"
apt-get purge -y -qq $NAME >/dev/null
test ! -e /etc/$NAME
test ! -e /var/lib/$NAME
test ! -e /opt/$NAME
if getent passwd $NAME >/dev/null; then echo "user left behind"; exit 1; fi

echo "DEB TEST PASSED"
