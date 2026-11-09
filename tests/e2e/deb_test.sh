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
$NAME mqtt set --host mosquitto --no-tls
$NAME mqtt test
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
$PY - "$KEY" <<'EOF'
import sys, threading
import paho.mqtt.client as mqtt
from ha_hypervisor_agent.protocol import unseal

key = sys.argv[1]
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
assert state["cluster"] in ("on", "degraded"), state["cluster"]
print("agent published signed state:", state["cluster"], len(state["vms"]), "VMs")
EOF
kill -TERM $AGENT
wait $AGENT
test -s /var/lib/$NAME/state.json
test "$(stat -c '%U' /var/lib/$NAME/state.json)" = "$NAME"

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
