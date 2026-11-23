import unittest
from ipaddress import IPv4Address, IPv4Interface
from unittest.mock import patch

from apps.web.__main__ import DashboardServer
from apps.web.lan import LanBinding, discover_lan


class LanTests(unittest.TestCase):
    def test_macos_selects_the_interface_address_matching_the_gateway(self):
        with patch("apps.web.lan.sys.platform", "darwin"), patch("apps.web.lan.command", side_effect=[
            "   route to: default\n    gateway: 192.168.1.204\n  interface: en0\n",
            "en0: flags=8863\n\tinet 192.168.2.50 netmask 0xffffff00\n"
            "\tinet 192.168.1.160 netmask 0xffffff00 broadcast 192.168.1.255\n",
        ]) as command:
            lan = discover_lan()
        self.assertEqual(lan.host, "192.168.1.160")
        self.assertEqual(command.call_args.args, ("ifconfig", "en0"))

    def test_linux_uses_the_route_source_and_interface_prefix(self):
        with patch("apps.web.lan.sys.platform", "linux"), patch("apps.web.lan.command", side_effect=[
            '[{"gateway":"10.4.0.1","dev":"eth0","prefsrc":"10.4.1.20"}]',
            '[{"addr_info":[{"local":"10.4.0.11","prefixlen":23},{"local":"10.4.1.20","prefixlen":23}]}]',
        ]):
            lan = discover_lan()
        self.assertEqual(lan.host, "10.4.1.20")
        self.assertEqual(str(lan.interface.network), "10.4.0.0/23")

    def test_lan_requests_are_restricted_by_tcp_peer_address(self):
        server = DashboardServer.__new__(DashboardServer)
        server.binding = LanBinding(IPv4Interface("192.168.1.160/24"), IPv4Address("192.168.1.204"))
        for address, allowed in {
            "192.168.1.160": True, "192.168.1.42": True, "192.168.1.204": False,
            "192.168.2.42": False, "127.0.0.1": False, "203.0.113.42": False,
        }.items():
            with self.subTest(address=address):
                self.assertEqual(server.verify_request(None, (address, 48000)), allowed)

    def test_refuses_a_public_interface_or_a_gateway_outside_the_subnet(self):
        for interface, gateway in [
            ("203.0.113.10/24", "203.0.113.1"),
            ("192.168.1.160/24", "192.168.2.1"),
            ("192.168.1.160/8", "192.168.1.204"),
        ]:
            with self.subTest(interface=interface):
                with self.assertRaises(ValueError):
                    LanBinding(IPv4Interface(interface), IPv4Address(gateway))
