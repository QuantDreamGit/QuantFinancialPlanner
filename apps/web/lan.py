"""Select the IPv4 LAN interface and limit incoming connections to its subnet."""
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network


PRIVATE_NETWORKS = tuple(IPv4Network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
LAN_ERROR = "LAN mode needs an active private IPv4 interface with a gateway on the same subnet."


@dataclass(frozen=True)
class LanBinding:
    interface: IPv4Interface
    gateway: IPv4Address

    def __post_init__(self):
        network = self.interface.network
        if (not any(network.subnet_of(private) for private in PRIVATE_NETWORKS)
                or self.gateway not in network or self.gateway == self.interface.ip):
            raise ValueError(LAN_ERROR)

    @property
    def host(self):
        return str(self.interface.ip)

    def allows(self, client):
        address = IPv4Address(client)
        # A router may present forwarded connections under its own LAN address.
        return address in self.interface.network and address != self.gateway


def command(*arguments):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, timeout=5).stdout


def macos_lan():
    route = dict(re.findall(r"^\s*(interface|gateway):\s+(\S+)", command("route", "-n", "get", "-inet", "default"),
                            re.MULTILINE))
    gateway = IPv4Address(route["gateway"])
    addresses = re.findall(r"^\s*inet (\S+) netmask (0x[0-9a-f]+)", command("ifconfig", route["interface"]), re.MULTILINE)
    for address, mask in addresses:
        interface = IPv4Interface(f"{address}/{IPv4Address(int(mask, 16))}")
        if gateway in interface.network:
            return LanBinding(interface, gateway)
    raise ValueError(LAN_ERROR)


def linux_lan():
    # Query the kernel's chosen IPv4 route without making a network request.
    route = json.loads(command("ip", "-j", "-4", "route", "get", "1.1.1.1"))[0]
    device = json.loads(command("ip", "-j", "-4", "address", "show", "dev", route["dev"]))[0]
    address = next(item for item in device["addr_info"] if item["local"] == route["prefsrc"])
    return LanBinding(IPv4Interface(f"{address['local']}/{address['prefixlen']}"), IPv4Address(route["gateway"]))


def discover_lan():
    try:
        if sys.platform == "darwin":
            return macos_lan()
        if sys.platform.startswith("linux"):
            return linux_lan()
    except FileNotFoundError as error:
        raise ValueError(f"LAN mode requires the {error.filename} network command.") from error
    except (OSError, subprocess.SubprocessError, IndexError, KeyError, StopIteration, ValueError) as error:
        raise ValueError(LAN_ERROR) from error
    raise ValueError("Automatic LAN detection is available on macOS and Linux.")


if __name__ == "__main__":
    try:
        discover_lan()
    except ValueError as error:
        raise SystemExit(str(error)) from None
