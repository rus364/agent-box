"""Port directions and namespace policy, shared by settings and launch admission."""
from dataclasses import dataclass
import ipaddress
import re


@dataclass(frozen=True, order=True)
class Port:
    address: str
    host: int
    container: int
    protocol: str

    def publish(self):
        address = f"[{self.address}]" if ":" in self.address else self.address
        return f"{address}:{self.host}:{self.container}" + ("/udp" if self.protocol == "udp" else "")


def parse_port(value, *, forward=False):
    pattern = r"(?:(\[[^\]]+\]|[^:]+):)?([0-9]{1,5}):([0-9]{1,5})(?:/(tcp|udp))?"
    match = re.fullmatch(pattern, value) if isinstance(value, str) else None
    if not match:
        raise ValueError("expected [IP:]HOST:CONTAINER[/tcp|udp] (bracket IPv6 addresses)")
    address, host, container, protocol = match.groups()
    if forward and address is not None:
        raise ValueError("host forwarding expects HOST:WORKSPACE[/tcp|udp], without an address")
    try:
        ip = ipaddress.ip_address((address or "127.0.0.1").strip("[]"))
    except ValueError:
        raise ValueError("bind address must be an IPv4 or bracketed IPv6 address") from None
    if "%" in str(ip):
        raise ValueError("scoped IPv6 addresses are not supported")
    if address and address.startswith("[") and ip.version != 6:
        raise ValueError("brackets are only valid for IPv6 addresses")
    if not all(1 <= int(port) <= 65535 for port in (host, container)):
        raise ValueError("ports must be between 1 and 65535")
    return Port(str(ip), int(host), int(container), protocol or "tcp")


def overlaps(first, second):
    if first.host != second.host or first.protocol != second.protocol:
        return False
    a, b = ipaddress.ip_address(first.address), ipaddress.ip_address(second.address)
    # IPv6 wildcard may also occupy IPv4 sockets; refuse the ambiguous overlap.
    return a == b or ((a.is_unspecified or b.is_unspecified)
                     and (a.version == b.version or a == ipaddress.ip_address("::")
                          or b == ipaddress.ip_address("::")))


def ports(values, *, forward=False):
    if not isinstance(values, list):
        raise ValueError("ports must be a list")
    parsed = [parse_port(value, forward=forward) for value in values]
    for i, item in enumerate(parsed):
        for previous in parsed[:i]:
            conflict = (item.container == previous.container and item.protocol == previous.protocol
                        if forward else overlaps(item, previous))
            if conflict:
                raise ValueError("duplicate or overlapping port bindings")
    return parsed


def problems(network, k3s):
    errors = []
    if network.mode not in ("default", "host"):
        errors.append("box.network.mode must be default or host")
    parsed = {}
    for name, values, forward in (("box.network.ports", network.ports, False),
                                  ("box.network.host_ports", network.host_ports, True),
                                  ("box.k3s.ports", k3s.ports, False)):
        try:
            parsed[name] = ports(values, forward=forward)
        except ValueError as exc:
            errors.append(f"{name}: {exc}")
    if errors:
        return errors
    if network.mode == "host" and (network.ports or network.host_ports):
        errors.append("host network conflicts with workspace ports/host forwards; "
                      "clear them with --no-publish --no-host-ports or use --network=default")
    publications = parsed["box.network.ports"]
    if k3s.enabled:
        if any(overlaps(a, b) for a in publications for b in parsed["box.k3s.ports"]):
            errors.append("workspace and k3s publications overlap on the host")
    if any(a.container == b.container and a.protocol == b.protocol
           for a in publications for b in parsed["box.network.host_ports"]):
        errors.append("workspace publication and host forward use the same workspace port")
    return errors


def arguments(network, api_port=None):
    published = ports(network.ports)
    forwarded = ports(network.host_ports, forward=True)
    if api_port is not None and network.mode != "host":
        if any(p.container == api_port and p.protocol == "tcp" for p in published + forwarded):
            raise ValueError(f"workspace port {api_port}/tcp is reserved for the k3s API forward")
        forwarded.insert(0, Port("127.0.0.1", api_port, api_port, "tcp"))
    args = [f"--publish={p.publish()}" for p in published]
    if network.mode == "host":
        return ["--network=host", *args]
    if forwarded:
        options = []
        for protocol, flag in (("tcp", "-T"), ("udp", "-U")):
            entries = [str(p.container) if p.host == p.container else f"{p.container}:{p.host}"
                       for p in forwarded if p.protocol == protocol]
            if entries:
                for entry in entries:
                    options.extend((flag, entry))
        args.insert(0, "--network=pasta:" + ",".join(options))
    return args


def identity(network):
    return (network.mode, sorted(ports(network.ports)), sorted(ports(network.host_ports, forward=True)))


def port_summary(values, target):
    lines = []
    for p in ports(values, forward=target == "host"):
        if target == "host":
            lines.append(f"workspace localhost:{p.container} -> host localhost:{p.host}/{p.protocol}")
        else:
            listener = "[::]" if ":" in p.address else "0.0.0.0"
            bind = f"[{p.address}]" if ":" in p.address else p.address
            exposure = "Accessible from other computers: " if not ipaddress.ip_address(p.address).is_loopback else ""
            lines.append(f"{exposure}{bind}:{p.host} -> {target}:{p.container}/{p.protocol}; "
                         f"application must listen on {listener}:{p.container}")
    return lines


def summary(network, k3s):
    lines = ["Network: " + network.mode]
    lines += port_summary(network.ports, "workspace")
    lines += port_summary(k3s.ports if k3s.enabled else [], "k3s")
    lines += port_summary(network.host_ports, "host")
    if network.mode == "host":
        lines.append("Host network shares localhost and ports, including nested containers; "
                     "wildcard listeners may be accessible from other computers.")
    return lines


def inspection_problem(network, current, api_port=None):
    """Creation labels cannot attest that the actual namespace/bindings still match."""
    host = current.get("HostConfig", {})
    mode = host.get("NetworkMode", "")
    expected = arguments(network, api_port)
    if network.mode == "host":
        if mode != "host":
            return "expected host network"
    elif mode == "host" or mode == "none" or mode.startswith(("container:", "ns:")):
        return "expected isolated networking"
    if network.mode != "host" and (network.host_ports or api_port):
        if mode != "pasta":
            return "expected pasta forwarding"
        command = current.get("Config", {}).get("CreateCommand", [])
        if expected[0] not in command:
            return "pasta forwarding does not match the creation policy"
    bindings = host.get("PortBindings") or {}
    actual = []
    try:
        for target, entries in bindings.items():
            port, protocol = target.split("/")
            for entry in entries or []:
                actual.append(Port(str(ipaddress.ip_address(entry["HostIp"])), int(entry["HostPort"]),
                                   int(port), protocol))
    except (ValueError, KeyError, TypeError):
        return "unreadable published ports"
    if sorted(actual) != sorted(ports(network.ports)):
        return "published ports do not match the creation policy"
    return None
