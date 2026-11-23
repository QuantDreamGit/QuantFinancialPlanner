# Deployment and backups

[Back to the README](../README.md)

## Local startup

The startup script uses Python 3.10 or later and `lsof`. The default build requires CMake 3.20 or later and a C++23 toolchain with `std::expected` and `std::format` support.

From the project directory, run `./start.sh` to start on port 8080. Use `./start.sh 8081` to choose another port or `./start.sh 0` to pick a free one automatically. The script replaces an existing Quant process running from the same project under your user account.

## Local network

To access Quant from devices on the server’s local network, run `./start.sh --lan` and open the address printed in the terminal. On macOS and Linux, LAN mode detects the active private IPv4 address and accepts clients on the same subnet. For a custom port, use `./start.sh --lan 8081`. The Python-only equivalent is `python3 -m apps.web --lan`.

## VM configuration

`./start.sh` reads the project’s [quant.ini](../quant.ini). For a VM-specific copy, set `database` and the backup `directory` to persistent paths that the service account can write to, such as `/var/lib/quant/finance.sqlite3` and `/mnt/backups/quant`. Relative paths resolve from the configuration file. If you keep the config outside the project, use an absolute `projection_bin` path for the built engine. Set `build = false` to use a prebuilt engine or run the Python dashboard alone.

After enrolling the VM, set `[server] mode` to `tailscale` or `netbird`. Quant detects the chosen provider’s IPv4 address, listens on that address, and restricts incoming connections to the detected overlay range. Open the URL printed at startup from an allowed VPN peer. Grant access to the configured TCP port using your VPN access policy. For an HTTPS reverse proxy, use `mode = loopback` with `public_url` set to the address used by browsers.

```sh
./start.sh --config /etc/quant/quant.ini
```

For automatic startup on a Linux VM, this example assumes a service account named `quant`, the checkout at `/opt/quant`, and a configured `/etc/quant/quant.ini`. Build once as needed, then set `build = false` and give the service account write access to the data and backup directories.

```ini
[Unit]
Description=Quant Financial Planner
Wants=network-online.target
After=network-online.target

[Service]
User=quant
WorkingDirectory=/opt/quant
ExecStart=/opt/quant/start.sh --config /etc/quant/quant.ini
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save the unit as `/etc/systemd/system/quant.service`, then run `sudo systemctl daemon-reload` and `sudo systemctl enable --now quant`. Use `journalctl -u quant -f` to see the dashboard address and backup log.

## HTTPS reverse proxy

For HTTPS access through a reverse proxy, forward requests to the loopback address on the machine running Quant. Preserve the public `Host` header, and forward the original `Origin` header when the browser sends one. Start the dashboard with the same HTTPS origin:

```sh
python3 -m apps.web --port 8080 --public-url https://finance.example.com
```

## Manual startup

```sh
cmake -S . -B build
cmake --build build
python3 -m apps.web
```

Open [the local dashboard](http://127.0.0.1:8080/). Pass `--port 0` to use an available port and open the address printed in the terminal. Pass `--projection-bin /path/to/planner_projection` if you build into a different directory. The ledger and dashboard also run with Python alone.

## Account recovery

Sessions expire after seven days. Anyone can change their own password from the account menu. To recover a password from the server terminal, run `python3 -m apps.web --reset-password USERNAME` with the same `--config` or `--db` option used by the running server. The reset signs out existing sessions for that username.

## Backups and restore

Set `[backups] directory`, `interval_hours`, and `keep` in `quant.ini`. The defaults write to `backups/` every 24 hours and retain the 14 latest completed snapshots of the configured database. Quant takes the first snapshot at startup and checks for a missed backup whenever it starts. `./start.sh --config /etc/quant/quant.ini --backup-now` takes a snapshot on demand while the dashboard runs. Backup completion and errors are printed to the server log.

Each timestamped snapshot has a `ledger/` folder containing the main database, workspace databases and registry, and any saved provider encryption keys. The manifest lists the files and their SHA-256 checksums. The snapshot contains personal financial data and saved account credentials, so store it in a private directory and keep a copy on a separate disk or backup service.

To restore or move to a new VM, stop Quant at the restore destination and copy the contents of one snapshot’s `ledger/` folder into a fresh data directory. Keep the main database’s filename and its sibling `.workspaces` directory together. Set `database` in the destination configuration to the restored main file and start Quant. Provision the VM’s VPN client and environment-based secrets separately.

## Run the tests

```sh
cmake -S . -B build
cmake --build build
ctest --test-dir build --output-on-failure
```

The C++ CLI is available as `./build/financial_planner` for exercising the shared car purchase example. The optional argument sets the purchase price in whole euros.
