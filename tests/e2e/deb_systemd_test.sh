#!/bin/sh
# Runs inside a container booted with systemd: the real unit with its sandbox.
set -eu

NAME=ha-hypervisor-agent

export DEBIAN_FRONTEND=noninteractive
apt-get install -y -qq --no-install-recommends "/debs/${NAME}_"*"_$(dpkg --print-architecture).deb" >/dev/null
test "$(systemctl is-enabled $NAME)" = "enabled"

echo "--- without a configuration it stops and does not loop"
systemctl start $NAME || true
sleep 3
test "$(systemctl is-active $NAME || true)" != "active"
systemctl show -p NRestarts --value $NAME | grep -qx 0

echo "--- configured"
$NAME init --agent-id systemdtest >/dev/null
$NAME mqtt set --host mosquitto --no-tls | grep -q "start it: sudo systemctl restart"
printf 'pass\n' | $NAME vcenter set --host vcsim --username user --password-stdin >/dev/null
printf 'pass\n' | $NAME esxi add DC0_C0_H0 --address esxi --username user --password-stdin --wol-mac 00:25:90:00:00:01 >/dev/null
systemctl reset-failed $NAME
systemctl start $NAME
sleep 8
test "$(systemctl is-active $NAME)" = "active"
journalctl -u $NAME --no-pager -n 20 | grep -q "Connected to the broker"
test -s /var/lib/$NAME/state.json
systemd-analyze security --no-pager $NAME | tail -1

echo "--- second agent next to it: Proxmox, ha-hypervisor-agent@pve"
$NAME init --agent-id systemdpve --instance pve | grep -q "ha-hypervisor-agent@pve"
$NAME mqtt set --host mosquitto --no-tls --instance pve | grep -q "enable --now ha-hypervisor-agent@pve"
printf '0b6b0bf2-7a56-4c4b-9f0e-6f5e1f1c2b10\n' | $NAME proxmox set --token-id 'ha-hypervisor@pve!agent' --secret-stdin --instance pve >/dev/null
$NAME node add pve --address pve --wol-mac 02:00:00:00:00:01 --instance pve >/dev/null
systemctl enable --now $NAME@pve
sleep 8
test "$(systemctl is-active $NAME@pve)" = "active"
test "$(systemctl is-active $NAME)" = "active"
journalctl -u $NAME@pve --no-pager -n 30 | grep -q "Connected to the broker"
test -s /var/lib/$NAME/pve.state.json
test -s /var/lib/$NAME/state.json

echo "--- stop reports offline, removal stops the service"
systemctl stop $NAME
apt-get remove -y -qq $NAME >/dev/null
test "$(systemctl is-enabled $NAME 2>&1 || true)" != "enabled"
test "$(systemctl is-active $NAME@pve || true)" != "active"
test "$(systemctl is-enabled $NAME@pve 2>&1 || true)" != "enabled"
test -e /etc/$NAME/config.yaml

echo "SYSTEMD TEST PASSED"
