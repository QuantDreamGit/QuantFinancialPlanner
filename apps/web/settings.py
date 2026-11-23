"""Load the settings shared by the dashboard, launcher, and backup command."""
import argparse
import configparser
import re
import subprocess
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path

from .auth_http import public_origin
from .lan import PRIVATE_NETWORKS, command, discover_lan


ROOT = Path(__file__).resolve().parents[2]
MODES = ("loopback", "lan", "tailscale", "netbird")
OVERLAY = IPv4Network("100.64.0.0/10")
KEYS = {
    "server": {"mode", "port", "database", "build", "projection_bin", "public_url"},
    "backups": {"enabled", "directory", "interval_hours", "keep"},
}


@dataclass(frozen=True)
class BackupSettings:
    enabled: bool
    directory: Path
    interval_hours: int
    keep: int


@dataclass(frozen=True)
class Settings:
    port: int
    mode: str
    database: Path
    projection_bin: Path
    public_url: str | None
    build: bool
    backups: BackupSettings


@dataclass(frozen=True)
class OverlayBinding:
    interface: IPv4Interface

    @property
    def host(self):
        return str(self.interface.ip)

    def allows(self, client):
        return IPv4Address(client) in self.interface.network


def add_settings_arguments(parser):
    parser.add_argument("--config", type=Path, help="read server and backup settings from an INI file")
    parser.add_argument("--port", type=int, help="listen port, or 0 for an available port")
    parser.add_argument("--db", type=Path, help="path to the SQLite ledger")
    parser.add_argument("--projection-bin", type=Path, help="path to the compiled C++ projection engine")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--public-url", help="external HTTPS origin when running behind a reverse proxy")
    access.add_argument("--lan", action="store_true", help="listen on the active private IPv4 address for local devices")


def configured_path(section, key, base, default):
    value = section.get(key, default).strip()
    if not value:
        raise ValueError(f"Set [{section.name}] {key} to a path.")
    return (base / Path(value).expanduser()).resolve()


def load_settings(options):
    config = configparser.ConfigParser(interpolation=None)
    for section in KEYS:
        config.add_section(section)
    base = ROOT
    if options.config is not None:
        file = options.config.expanduser().resolve()
        base = file.parent
        with file.open(encoding="utf-8") as source:
            config.read_file(source)
        if config.defaults():
            raise ValueError("Put settings in [server] or [backups].")
        for section in config.sections():
            if section not in KEYS:
                raise ValueError(f"Unknown configuration section [{section}].")
            unknown = set(config[section]) - KEYS[section]
            if unknown:
                raise ValueError(f"Unknown [{section}] setting {min(unknown)}.")
    server, backups = config["server"], config["backups"]
    port = options.port if options.port is not None else server.getint("port", 8080)
    if not 0 <= port <= 65535:
        raise ValueError("Enter a port between 0 and 65535.")
    mode = "lan" if options.lan else "loopback" if options.public_url else server.get("mode", "loopback").lower()
    if mode not in MODES:
        raise ValueError("Choose loopback, lan, tailscale, or netbird for [server] mode.")
    external = public_origin(options.public_url or (None if options.lan else server.get("public_url")))
    if external and mode != "loopback":
        raise ValueError("Use loopback mode with [server] public_url.")
    database = (options.db.expanduser().resolve() if options.db else
                configured_path(server, "database", base, "data/finance.sqlite3"))
    projection = (options.projection_bin.expanduser().resolve() if options.projection_bin else
                  configured_path(server, "projection_bin", base, str(ROOT / "build" / "planner_projection")))
    directory = configured_path(backups, "directory", base, "backups")
    interval, keep = backups.getint("interval_hours", 24), backups.getint("keep", 14)
    if interval < 1:
        raise ValueError("Set [backups] interval_hours to at least 1.")
    if keep < 1:
        raise ValueError("Set [backups] keep to at least 1.")
    if directory == database or directory.is_relative_to(database.with_suffix(".workspaces")):
        raise ValueError("Choose a backup directory outside the workspace data folder.")
    enabled = backups.getboolean("enabled", options.config is not None)
    return Settings(port, mode, database, projection, external, server.getboolean("build", True),
                    BackupSettings(enabled, directory, interval, keep))


def network_binding(mode):
    if mode == "loopback":
        return None
    if mode == "lan":
        return discover_lan()
    try:
        if mode == "tailscale":
            address = command("tailscale", "ip", "-4").strip()
            interface = IPv4Interface(f"{address}/{OVERLAY.prefixlen}")
            networks = (OVERLAY,)
        else:
            status = command("netbird", "status")
            address = re.search(r"^NetBird IP:\s+(\S+)", status, re.MULTILINE)
            if address is None or "/" not in address[1]:
                raise ValueError("NetBird IP is missing.")
            interface = IPv4Interface(address[1])
            networks = (OVERLAY, *PRIVATE_NETWORKS)
        if not any(interface.network.subnet_of(network) for network in networks):
            raise ValueError("The overlay address is outside the supported private ranges.")
        return OverlayBinding(interface)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        label = "Tailscale" if mode == "tailscale" else "NetBird"
        raise ValueError(f"{label} mode needs a connected VPN client and its private IPv4 address.") from error


def settings_from_cli(parser, args):
    try:
        return load_settings(args)
    except (OSError, configparser.Error, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validate the Quant startup settings.")
    add_settings_arguments(parser)
    settings = settings_from_cli(parser, parser.parse_args())
    try:
        network_binding(settings.mode)
    except ValueError as error:
        parser.error(str(error))
    print(f"{settings.port} {int(settings.build)}")
