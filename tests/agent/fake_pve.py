"""A simulated Proxmox VE: nodes, quorum, HA, QEMU VMs and LXC containers.

Every node listens on its own port and answers for the whole cluster, like
pveproxy. A node that is shut down closes its port. It only covers the API
the agent uses, with the answers shaped as Proxmox VE 9 sends them.

Run alone (end-to-end tests): python fake_pve.py --tls --node pve1=8006 ...
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import ssl
import threading
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

TOKEN = "ha-hypervisor@pve!agent"
SECRET = "0b6b0bf2-7a56-4c4b-9f0e-6f5e1f1c2b10"
GIB = 1024**3


@dataclass
class Guest:
    vmid: int
    name: str
    node: str
    kind: str = "qemu"  # qemu or lxc
    running: bool = False
    agent: bool = True  # QEMU guest agent installed
    ignores_shutdown: bool = False
    template: bool = False
    ha: str | None = None  # HA requested state: started, stopped, ...
    # name -> {"vmstate": bool, "running": bool}, oldest first.
    snapshots: dict[str, dict[str, bool]] = field(default_factory=dict)

    @property
    def sid(self) -> str:
        return f"{'vm' if self.kind == 'qemu' else 'ct'}:{self.vmid}"


@dataclass
class Node:
    name: str
    port: int
    online: bool = True
    server: ThreadingHTTPServer | None = None


@dataclass
class Cluster:
    name: str | None = None  # None: a single node, no cluster
    nodes: dict[str, Node] = field(default_factory=dict)
    guests: dict[int, Guest] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    # Things a real cluster would punish: fencing, killed guests.
    violations: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    tls: ssl.SSLContext | None = None
    host: str = "127.0.0.1"

    # ------------------------------------------------------------- topology

    def add_node(self, name: str, port: int = 0) -> Node:
        node = Node(name, port)
        self.nodes[name] = node
        self._listen(node)
        return node

    def add_guest(self, vmid: int, name: str, node: str, **kwargs: Any) -> Guest:
        guest = Guest(vmid, name, node, **kwargs)
        self.guests[vmid] = guest
        return guest

    def _listen(self, node: Node) -> None:
        cluster = self

        class Handler(_Handler):
            pass

        Handler.cluster = cluster
        Handler.node_name = node.name
        server = ThreadingHTTPServer((self.host, node.port), Handler)
        server.daemon_threads = True
        if self.tls is not None:
            server.socket = self.tls.wrap_socket(server.socket, server_side=True)
        node.port = server.server_address[1]
        node.server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def power_off(self, name: str, reason: str = "shutdown") -> None:
        node = self.nodes[name]
        with self.lock:
            server, node.server = node.server, None
        if server is not None:
            # Like a powered-off machine: the port is closed before it reads as off.
            server.shutdown()
            server.server_close()
        with self.lock:
            if not node.online:
                return
            for guest in self.guests.values():
                if guest.node == name and guest.running:
                    if guest.ha == "started" and self.name:
                        self.violations.append(f"HA guest {guest.name} left on {name}")
                    else:
                        self.violations.append(f"guest {guest.name} killed by {reason} of {name}")
                    guest.running = False
            node.online = False
            self.events.append(f"node_off:{name}")

    def power_on(self, name: str) -> None:
        with self.lock:
            node = self.nodes[name]
            if node.online:
                return
            node.online = True
            self.events.append(f"node_on:{name}")
            self._listen(node)
            # HA starts what it is asked to keep running, onboot is ignored here.
            for guest in self.guests.values():
                if guest.node == name and guest.ha == "started" and self.quorate:
                    guest.running = True

    def stop(self) -> None:
        for node in self.nodes.values():
            if node.server is not None:
                node.server.shutdown()
                node.server.server_close()

    @property
    def quorate(self) -> bool:
        if self.name is None:
            return True
        online = sum(1 for node in self.nodes.values() if node.online)
        return online > len(self.nodes) // 2

    def apply_ha(self) -> None:
        """The HA manager: started resources run, stopped ones do not."""
        if not self.quorate:
            return
        for guest in self.guests.values():
            if guest.ha == "started" and not guest.running and self.nodes[guest.node].online:
                guest.running = True
                self.events.append(f"ha_start:{guest.name}")


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _Handler(BaseHTTPRequestHandler):
    cluster: Cluster
    node_name: str

    def log_message(self, *_args: Any) -> None:
        pass

    def _params(self) -> dict[str, str]:
        query = {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            body = self.rfile.read(length).decode()
            query.update({k: v[-1] for k, v in parse_qs(body).items()})
        return query

    def _handle(self, method: str) -> None:
        try:
            params = self._params()
            if self.headers.get("Authorization") != f"PVEAPIToken={TOKEN}={SECRET}":
                raise ApiError(401, "authentication failure")
            path = unquote(urlparse(self.path).path)
            if not path.startswith("/api2/json"):
                raise ApiError(404, "not found")
            with self.cluster.lock:
                data = route(self.cluster, self.node_name, method, path[len("/api2/json") :], params)
            status, body = 200, {"data": data}
        except ApiError as err:
            status, body = err.status, {"data": None, "errors": {"detail": err.message}}
        payload = json.dumps(body).encode()
        reason = body.get("errors", {}).get("detail") if status != 200 else "OK"
        self.send_response(status, reason)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")


def _guest(cluster: Cluster, node: str, kind: str, vmid: str) -> Guest:
    guest = cluster.guests.get(int(vmid))
    if guest is None or guest.kind != kind or guest.node != node:
        raise ApiError(500, f"Configuration file 'nodes/{node}/{kind}/{vmid}.conf' does not exist")
    return guest


def _reach(cluster: Cluster, node: str) -> None:
    """A call about another node is proxied, it fails when that node is off."""
    target = cluster.nodes.get(node)
    if target is None:
        raise ApiError(500, f"hostname lookup '{node}' failed - does not exist")
    if not target.online:
        raise ApiError(595, f"No route to host {node}")


def _snapshot(  # noqa: PLR0913
    cluster: Cluster,
    method: str,
    params: dict[str, str],
    node: str,
    kind: str,
    vmid: str,
    name: str | None,
    rollback: str | None,
) -> Any:
    _reach(cluster, node)
    guest = _guest(cluster, node, kind, vmid)
    upid = f"UPID:{node}:0000:0000:0000:snapshot:{vmid}:{TOKEN}:"
    if name is None:
        if method == "GET":
            return [
                {"name": snap, "snaptime": 1700000000 + index, "vmstate": int(data["vmstate"]),
                 "description": ""}
                for index, (snap, data) in enumerate(guest.snapshots.items())
            ] + [{"name": "current", "digest": "x", "running": int(guest.running)}]
        if method != "POST":
            raise ApiError(501, "not implemented")
        _need_quorum(cluster)
        snapname = params.get("snapname", "")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,39}", snapname):
            raise ApiError(400, "snapname: invalid format")
        if snapname in guest.snapshots:
            raise ApiError(500, f"snapshot name '{snapname}' already used")
        vmstate = params.get("vmstate") == "1"
        if vmstate and kind != "qemu":
            raise ApiError(400, "vmstate: property is not defined in schema")
        guest.snapshots[snapname] = {"vmstate": vmstate and guest.running, "running": guest.running}
        cluster.events.append(f"snapshot:{guest.name}:{snapname}")
        return upid
    if name not in guest.snapshots:
        raise ApiError(500, f"snapshot '{name}' does not exist")
    _need_quorum(cluster)
    if rollback:
        if method != "POST":
            raise ApiError(501, "not implemented")
        if guest.running and kind == "lxc":
            raise ApiError(500, "CT is running")
        if guest.running:
            # qm rollback stops a running VM itself.
            guest.running = False
        guest.running = guest.snapshots[name]["vmstate"]
        cluster.events.append(f"rollback:{guest.name}:{name}")
        return upid
    if method == "DELETE":
        del guest.snapshots[name]
        cluster.events.append(f"delsnapshot:{guest.name}:{name}")
        return upid
    raise ApiError(501, "not implemented")


def _need_quorum(cluster: Cluster) -> None:
    if not cluster.quorate:
        raise ApiError(500, "cluster not ready - no quorum?")


def route(cluster: Cluster, here: str, method: str, path: str, params: dict[str, str]) -> Any:  # noqa: C901, PLR0911, PLR0912
    if path == "/version":
        return {"version": "9.0.10", "release": "9.0", "repoid": "deadbeef"}

    if path == "/cluster/status":
        nodes = [
            {
                "type": "node",
                "id": f"node/{node.name}",
                "name": node.name,
                "online": int(node.online),
                "local": int(node.name == here),
                "nodeid": index + 1,
                "ip": "127.0.0.1",
            }
            for index, node in enumerate(cluster.nodes.values())
            if cluster.name or node.name == here
        ]
        if cluster.name is None:
            return nodes
        return [
            {
                "type": "cluster",
                "id": "cluster",
                "name": cluster.name,
                "nodes": len(cluster.nodes),
                "quorate": int(cluster.quorate),
                "version": 4,
            },
            *nodes,
        ]

    if path == "/cluster/resources":
        kind = params.get("type")
        result: list[dict[str, Any]] = []
        if kind in (None, "node"):
            for node in cluster.nodes.values():
                result.append(
                    {
                        "id": f"node/{node.name}",
                        "type": "node",
                        "node": node.name,
                        "status": "online" if node.online else "offline",
                        **(
                            {"cpu": 0.25, "maxcpu": 8, "mem": 8 * GIB, "maxmem": 32 * GIB, "uptime": 3600}
                            if node.online
                            else {}
                        ),
                    }
                )
        if kind in (None, "vm"):
            for guest in cluster.guests.values():
                online = cluster.nodes[guest.node].online
                item = {
                    "id": f"{guest.kind}/{guest.vmid}",
                    "type": guest.kind,
                    "vmid": guest.vmid,
                    "name": guest.name,
                    "node": guest.node,
                    "template": int(guest.template),
                    "status": ("running" if guest.running else "stopped") if online else "unknown",
                    "maxcpu": 2,
                    "maxmem": 4 * GIB,
                    "maxdisk": 32 * GIB,
                    "cpu": 0.5 if guest.running else 0,
                    "mem": 2 * GIB if guest.running else 0,
                    "uptime": 100 if guest.running else 0,
                }
                if guest.ha:
                    item["hastate"] = guest.ha
                result.append(item)
        if kind in (None, "storage"):
            for node in cluster.nodes.values():
                if not node.online:
                    continue
                result.append(
                    {
                        "id": f"storage/{node.name}/local-lvm",
                        "type": "storage",
                        "storage": "local-lvm",
                        "node": node.name,
                        "status": "available",
                        "plugintype": "lvmthin",
                        "shared": 0,
                        "disk": 100 * GIB,
                        "maxdisk": 400 * GIB,
                    }
                )
                if cluster.name:
                    result.append(
                        {
                            "id": f"storage/{node.name}/nfs",
                            "type": "storage",
                            "storage": "nfs",
                            "node": node.name,
                            "status": "available",
                            "plugintype": "nfs",
                            "shared": 1,
                            "disk": 1 * 1024 * GIB,
                            "maxdisk": 4 * 1024 * GIB,
                        }
                    )
        return result

    if path == "/cluster/ha/resources":
        return [
            {"sid": guest.sid, "state": guest.ha, "type": "vm" if guest.kind == "qemu" else "ct"}
            for guest in cluster.guests.values()
            if guest.ha
        ]

    match = re.fullmatch(r"/cluster/ha/resources/(vm|ct):(\d+)", path)
    if match and method == "PUT":
        _need_quorum(cluster)
        guest = cluster.guests.get(int(match.group(2)))
        if guest is None or guest.ha is None:
            raise ApiError(500, "resource does not exist")
        guest.ha = params["state"]
        cluster.events.append(f"ha_state:{guest.name}={guest.ha}")
        if guest.ha == "stopped":
            guest.running = False
        cluster.apply_ha()
        return None

    match = re.fullmatch(r"/nodes/([^/]+)/status", path)
    if match:
        node = match.group(1)
        _reach(cluster, node)
        if method == "POST":
            if node != here:
                # Works through a proxy too, the agent calls each node itself.
                cluster.events.append(f"proxied_shutdown:{node}")
            if params.get("command") != "shutdown":
                raise ApiError(400, "command: value must be one of reboot, shutdown")
            # pve-guests stops the guests first, a clean agent stops them before.
            threading.Timer(0.05, cluster.power_off, args=(node, "shutdown")).start()
            return None
        return {
            "cpuinfo": {"cpus": 8, "mhz": "2200.000", "model": "Intel(R) Xeon(R) D-1528", "sockets": 1, "cores": 6},
            "pveversion": "pve-manager/9.0.10/abcdef",
            "uptime": 3600,
            "memory": {"total": 32 * GIB, "used": 8 * GIB},
        }

    match = re.fullmatch(r"/nodes/([^/]+)/tasks/([^/]+)/status", path)
    if match:
        _reach(cluster, match.group(1))
        # Every task of the simulator finishes at once.
        return {"status": "stopped", "exitstatus": "OK", "upid": match.group(2)}

    match = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/snapshot(?:/([^/]+)(/rollback)?)?", path)
    if match:
        return _snapshot(cluster, method, params, *match.groups())

    match = re.fullmatch(r"/nodes/([^/]+)/lxc/(\d+)/interfaces", path)
    if match:
        _reach(cluster, match.group(1))
        guest = _guest(cluster, match.group(1), "lxc", match.group(2))
        if not guest.running:
            raise ApiError(500, "CT is not running")
        return [
            {"name": "lo", "inet": "127.0.0.1/8"},
            {"name": "eth0", "inet": f"10.0.0.{guest.vmid % 250}/24", "hwaddr": "bc:24:11:00:00:01"},
        ]

    match = re.fullmatch(r"/nodes/([^/]+)/(qemu|lxc)/(\d+)/(status/\w+|agent/[\w-]+)", path)
    if match:
        node, kind, vmid, action = match.groups()
        _reach(cluster, node)
        guest = _guest(cluster, node, kind, vmid)
        if action == "status/current":
            return {
                "vmid": guest.vmid,
                "name": guest.name,
                "status": "running" if guest.running else "stopped",
                "ha": {"managed": int(guest.ha is not None), **({"state": guest.ha} if guest.ha else {})},
                "agent": int(guest.agent),
            }
        if action.startswith("agent/"):
            if kind != "qemu" or not guest.running or not guest.agent:
                raise ApiError(500, f"QEMU guest agent is not running")
            if action == "agent/ping":
                return {}
            if action == "agent/network-get-interfaces":
                return {
                    "result": [
                        {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
                        {
                            "name": "ens18",
                            "ip-addresses": [
                                {"ip-address": "fe80::1", "ip-address-type": "ipv6"},
                                {"ip-address": f"10.0.1.{guest.vmid % 250}", "ip-address-type": "ipv4"},
                            ],
                        },
                    ]
                }
            raise ApiError(501, "not implemented")
        if method != "POST":
            raise ApiError(501, "not implemented")
        _need_quorum(cluster)
        verb = action.split("/")[1]
        upid = f"UPID:{node}:0000:0000:0000:qm{verb}:{vmid}:{TOKEN}:"
        if guest.ha is not None:
            # Proxmox hands HA guests to the HA manager.
            guest.ha = "started" if verb == "start" else "stopped"
            cluster.events.append(f"ha_request:{guest.name}={guest.ha}")
            if verb == "start":
                cluster.apply_ha()
            elif verb == "stop" or not guest.ignores_shutdown:
                guest.running = False
            return upid
        cluster.events.append(f"{verb}:{guest.name}")
        if verb == "start":
            guest.running = True
        elif verb == "stop" or not guest.ignores_shutdown:
            guest.running = False
        return upid

    raise ApiError(501, f"Method '{method} {path}' not implemented")


def self_signed_context() -> ssl.SSLContext:
    """A throwaway certificate, like the default one of a Proxmox node."""
    import datetime  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415
    from cryptography.x509.oid import NameOID  # noqa: PLC0415

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pve")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    folder = tempfile.mkdtemp()
    cert_path, key_path = f"{folder}/cert.pem", f"{folder}/key.pem"
    with open(cert_path, "wb") as stream:
        stream.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as stream:
        stream.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", action="append", required=True, help="NAME=PORT")
    parser.add_argument("--cluster", help="cluster name, omit for a single node")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--wol-port", type=int, default=9, help="UDP port for magic packets")
    args = parser.parse_args()

    cluster = Cluster(name=args.cluster, host="0.0.0.0", tls=self_signed_context() if args.tls else None)  # noqa: S104
    for item in args.node:
        name, port = item.split("=")
        cluster.add_node(name, int(port))
    first = next(iter(cluster.nodes))
    cluster.add_guest(100, "ad01", first, running=True)
    cluster.add_guest(101, "files01", first, running=True)
    cluster.add_guest(102, "media01", first, running=False, agent=False)
    cluster.add_guest(200, "pihole", first, kind="lxc", running=True)
    cluster.add_guest(9000, "debian-template", first, template=True)
    print("fake Proxmox VE ready", flush=True)  # noqa: T201
    _serve_wol(cluster, args.wol_port)


def _serve_wol(cluster: Cluster, port: int) -> None:
    """Power a node on when a magic packet for 02:00:00:00:00:NN arrives (NN = node index)."""
    import socket  # noqa: PLC0415

    names = list(cluster.nodes)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", port))  # noqa: S104
    while True:
        packet, _ = sock.recvfrom(1024)
        if len(packet) >= 102 and packet[:6] == b"\xff" * 6:
            index = packet[11] - 1
            if 0 <= index < len(names):
                cluster.power_on(names[index])


if __name__ == "__main__":
    main()
