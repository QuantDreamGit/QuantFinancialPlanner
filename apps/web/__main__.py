import argparse
import errno
import json
import os
import re
import secrets
import sqlite3
import sys
from contextlib import nullcontext
from datetime import date
from getpass import getpass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .attachments import Download, handle_attachments
from .attention import attention
from .auth import AuthStore
from .auth_http import admin_only, auth_api, public_origin, session_token, signed_in
from .backups import BackupWorker, create_backup, ledger_access
from .budgets import handle_budgets, limit_report
from .cars import handle_cars
from .demo import sample_ledger
from .decision import handle_decision
from .forecast import handle_forecast, plan_metadata
from .funding import handle_funding
from .goals import handle_goals
from .imports import handle_imports
from .insights import handle_insights, month_window
from .planning import Projection
from .provider_keys import GroqKeyStore, OpenAIKeyStore
from .reports import PERIODS, dashboard, planner_inputs, selected_window, transaction_list
from .returns import handle_returns
from .settings import add_settings_arguments, network_binding, settings_from_cli
from .shared import handle_shared
from .starter_categories import handle_starter_categories
from .statement_documents import handle_statement_documents
from .storage import Database, ENTRY_KINDS
from .values import ApiError, choice, identifier, line
from .workspaces import WorkspaceRegistry, group_id, local_id, owner_id


PUBLIC = Path(__file__).with_name("public")
ASSETS = {
    "/": ("index.html", "text/html"),
    "/auth": ("auth.html", "text/html"),
    "/auth.css": ("auth.css", "text/css"),
    "/auth.js": ("auth.js", "text/javascript"),
    "/auth-shell.js": ("auth-shell.js", "text/javascript"),
    "/access.js": ("access.js", "text/javascript"),
    "/access.css": ("access.css", "text/css"),
    "/dashboard.css": ("dashboard.css", "text/css"),
    "/dashboard.js": ("dashboard.js", "text/javascript"),
    "/charts.js": ("charts.js", "text/javascript"),
    "/flow.js": ("flow.js", "text/javascript"),
    "/editor.js": ("editor.js", "text/javascript"),
    "/attachments.js": ("attachments.js", "text/javascript"),
    "/attachments.css": ("attachments.css", "text/css"),
    "/ui.js": ("ui.js", "text/javascript"),
    "/planning.js": ("planning.js", "text/javascript"),
    "/insights.js": ("insights.js", "text/javascript"),
    "/insights.css": ("insights.css", "text/css"),
    "/forecast.js": ("forecast.js", "text/javascript"),
    "/forecast.css": ("forecast.css", "text/css"),
    "/funding.js": ("funding.js", "text/javascript"),
    "/funding.css": ("funding.css", "text/css"),
    "/documentation.js": ("documentation.js", "text/javascript"),
    "/documentation-topics.js": ("documentation-topics.js", "text/javascript"),
    "/documentation.css": ("documentation.css", "text/css"),
    "/feature-help.js": ("feature-help.js", "text/javascript"),
    "/feature-help.css": ("feature-help.css", "text/css"),
    "/shared.js": ("shared.js", "text/javascript"),
    "/shared.css": ("shared.css", "text/css"),
    "/decision.js": ("decision.js", "text/javascript"),
    "/decision.css": ("decision.css", "text/css"),
    "/imports.js": ("imports.js", "text/javascript"),
    "/imports.css": ("imports.css", "text/css"),
    "/budgets.js": ("budgets.js", "text/javascript"),
    "/budgets.css": ("budgets.css", "text/css"),
    "/cars.js": ("cars.js", "text/javascript"),
    "/cars-diagram.js": ("cars-diagram.js", "text/javascript"),
    "/cars.css": ("cars.css", "text/css"),
    "/workspaces.js": ("workspaces.js", "text/javascript"),
    "/returns.js": ("returns.js", "text/javascript"),
    "/returns.css": ("returns.css", "text/css"),
    "/goals.js": ("goals.js", "text/javascript"),
    "/goals.css": ("goals.css", "text/css"),
    "/icons.svg": ("icons.svg", "image/svg+xml"),
}
RECORDS = re.compile(r"/api/(accounts|categories|transactions|scenarios)(?:/([1-9][0-9]{0,17}))?")


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, port, database, projection, *, public_url=None, setup_code=None, lan=None, binding=None):
        self.database = database
        self.workspaces = WorkspaceRegistry(database)
        self.projection = projection
        self.public_url = public_origin(public_url)
        self.binding = binding or lan
        if self.binding and self.public_url:
            raise ValueError("Choose a private network mode or an external HTTPS origin.")
        self.setup_code = setup_code or secrets.token_urlsafe(32)
        self.auth = AuthStore(self.workspaces, bootstrap_token=self.setup_code)
        self.groq_keys = GroqKeyStore(self.workspaces)
        self.openai_keys = OpenAIKeyStore(self.workspaces)
        listen_host = self.binding.host if self.binding else "127.0.0.1"
        super().__init__((listen_host, port), DashboardHandler)
        if self.public_url:
            public_host = urlsplit(self.public_url).netloc
            self.hosts = {public_host}
            if urlsplit(self.public_url).port is None:
                self.hosts.add(public_host + ":443")
        else:
            names = (listen_host,) if self.binding else (listen_host, "localhost")
            self.hosts = {f"{name}:{self.server_port}" for name in names}
            if self.server_port == 80:
                self.hosts.update(names)

    def verify_request(self, request, client_address):
        return self.binding is None or self.binding.allows(client_address[0])

    def ledger_access(self):
        return ledger_access(self.database.path)


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.dispatch()

    do_HEAD = do_POST = do_PUT = do_DELETE = do_GET

    def log_request(self, code="-", size="-"):
        pass

    def reply(self, body, status, mime, *, download_name=None):
        self.send_response(status)
        self.send_header("Content-Type", mime if download_name else f"{mime}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self.server, "public_url", None):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        if cookie := getattr(self, "session_cookie", None):
            self.send_header("Set-Cookie", cookie)
        if download_name:
            fallback = re.sub(r"[^a-zA-Z0-9_.-]", "_", download_name)
            encoded = quote(download_name, safe="")
            self.send_header("Content-Disposition", f"""attachment; filename="{fallback}"; filename*=UTF-8''{encoded}""")
        self.send_header(
            "Content-Security-Policy",
            "sandbox; default-src 'none'" if download_name else
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_reply(self, data, status=200):
        self.reply(json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode(), status, "application/json")

    def problem(self, error):
        detail = {"message": str(error)}
        if error.field:
            detail["field"] = error.field
        self.json_reply({"error": detail}, error.status)

    def read_data(self, max_size):
        if self.headers.get_content_type() != "application/json":
            raise ApiError("Send the data as application/json.", status=415)
        try:
            length = int(self.headers.get("Content-Length", ""))
            if length < 1:
                raise ValueError
            if length > max_size:
                raise ApiError("The request is too large.", status=413)
            data = json.loads(self.rfile.read(length))
            if isinstance(data, dict):
                return data
        except (ValueError, UnicodeDecodeError):
            pass
        raise ApiError("Send a JSON object.", status=400)

    def dispatch(self):
        try:
            host = self.headers.get("Host")
            if host not in self.server.hosts:
                raise ApiError("Use the dashboard address shown in the server terminal.", status=403)
            origin = self.headers.get("Origin")
            expected = getattr(self.server, "public_url", None) or f"http://{host}"
            if self.command in ("POST", "PUT", "DELETE"):
                if ((origin is not None and origin != expected)
                        or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
                    raise ApiError("Open the dashboard to make changes.", status=403)
            url = urlsplit(self.path)
            if url.path in ASSETS and self.command in ("GET", "HEAD"):
                name, mime = ASSETS[url.path]
                self.reply((PUBLIC / name).read_bytes(), 200, mime)
                return
            query = {key: values[-1] for key, values in parse_qs(url.query, max_num_fields=20).items()}
            with getattr(self.server, "ledger_access", nullcontext)():
                data, status = self.api(url.path, query)
            if isinstance(data, Download):
                self.reply(data.body, status, "application/octet-stream", download_name=data.name)
            else:
                self.json_reply(data, status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except ApiError as error:
            self.problem(error)
        except ValueError:
            self.problem(ApiError("Check the request parameters.", status=400))
        except Exception as error:
            self.log_error("Dashboard request failed (%s)", type(error).__name__)
            self.problem(ApiError("The dashboard could not complete this request.", status=500))

    def api(self, path, query):
        method = "GET" if self.command == "HEAD" else self.command
        registry = getattr(self.server, "workspaces", None)
        store = getattr(self.server, "auth", None)
        user = None
        if store:
            token = session_token(self)
            user = store.session(token)
            profile_header = self.headers.get("X-Profile")
            if user and profile_header is not None and profile_header != str(user["profile_id"]):
                raise ApiError("Your signed-in account changed. Reopen the dashboard.", status=401)
            if path.startswith("/api/auth/"):
                return auth_api(self, store, path, method, token, user)
            signed_in(user)
        self.user = user
        selected = identifier(getattr(self, "headers", {}).get("X-Workspace"), "workspace")
        if selected is None:
            selected = user["profile_id"] if user else 1
        if registry and path == "/api/workspaces/demo" and method == "POST":
            signed_in(user)
            return registry.provision_demo(user["profile_id"], date.today(), self), 200
        if registry and (path == "/api/workspaces" or re.fullmatch(r"/api/workspaces/[1-9][0-9]{0,17}", path)):
            if method == "GET" and path == "/api/workspaces":
                visible = registry.list()
                managed = [item for item in visible if not item.get("demo_role")] if user is None or user["is_admin"] else []
                if user:
                    visible = [item for item in visible if self.can_open_workspace(item)]
                active = next((item for item in visible if item["id"] == selected), None)
                if active is None:
                    raise ApiError("You don’t have access to this workspace.", status=403)
                return {"active": active, "workspaces": visible, "managed": managed}, 200
            if store:
                admin_only(user)
            if method == "POST" and path == "/api/workspaces":
                return registry.save(self.read_data(16_384)), 201
            if method == "PUT" and path != "/api/workspaces":
                key = int(path.rsplit("/", 1)[1])
                data = self.read_data(16_384)
                with registry.group_access(key) if registry.workspace(key)["kind"] == "group" else nullcontext():
                    return registry.save(data, key), 200
            raise ApiError("This workspace action is unavailable.", status=405)
        self.workspace = registry.workspace(selected) if registry else {"id": 1, "kind": "profile", "members": []}
        if not self.can_open_workspace(self.workspace):
            raise ApiError("You don’t have access to this workspace.", status=403)
        if registry and self.workspace["kind"] == "group":
            with registry.group_access(selected):
                self.workspace = registry.workspace(selected)
                if not self.can_open_workspace(self.workspace):
                    raise ApiError("You don’t have access to this workspace.", status=403)
                return self.workspace_api(registry, path, query, method)
        return self.workspace_api(registry, path, query, method)

    def can_open_workspace(self, workspace):
        if workspace.get("demo_role"):
            return bool(self.user and workspace["demo_ready"] and workspace["demo_owner"] == self.user["profile_id"])
        if not self.user:
            return True
        own = self.user["profile_id"]
        return workspace["id"] == own or any(member["id"] == own for member in workspace["members"])

    def can_use_personal_profile(self, profile_id):
        if not self.user:
            return True
        own = self.user["profile_id"]
        return profile_id == own or (
            self.workspace.get("demo_owner") == own
            and any(member["id"] == profile_id for member in self.workspace["members"])
        )

    def workspace_api(self, registry, path, query, method):
        today = date.today()
        cars = handle_cars(self, registry, path, method, today)
        if cars is not None:
            return cars
        self.database = registry.database(self.workspace) if registry else self.server.database
        database, projection = self.database, self.server.projection
        for handler in (handle_forecast, handle_goals, handle_shared, handle_decision, handle_statement_documents, handle_imports,
                        handle_budgets, handle_returns, handle_funding, handle_attachments, handle_starter_categories):
            result = handler(self, path, query, method, today)
            if result is not None:
                return result
        if method == "GET":
            if path == "/api/attention":
                report = attention(database, today)
                groups = [(database, self.workspace)] if self.workspace["kind"] == "group" else self.group_budget_contexts()
                limits = []
                for group, workspace in groups:
                    budget = limit_report(group, workspace, today.isoformat()[:7], today,
                                          only_member=None if self.workspace["kind"] == "group" else self.workspace["id"])
                    limits.extend({"name": row["name"], "group": workspace["name"], "exceeded": row["exceeded"],
                                   "gap": str(int(row["projected"] if row["projected_exceeded"] else row["spent"])
                                              - int(row["monthly_limit"]))}
                                  for row in budget["limits"] if row["exceeded"] or row["projected_exceeded"])
                return dict(report, limits=limits), 200
            if path in ("/api/insights/replay", "/api/insights/waterfall", "/api/insights/detective"):
                ledger = sample_ledger(today) if query.get("sample") == "1" else database.ledger()
                return handle_insights(path, query, ledger, today), 200
            if path == "/api/example":
                return projection.example(), 200
            if path == "/api/planner-inputs":
                return planner_inputs(database.ledger(), today), 200
            if path == "/api/dashboard":
                period = choice(query.get("period", "6M"), PERIODS, "period")
                sample = query.get("sample") == "1"
                ledger = sample_ledger(today) if sample else database.ledger()
                return dict(dashboard(ledger, period, today), sample=sample), 200
            if path == "/api/transactions":
                return self.transactions(query, today), 200
        if method == "POST" and path == "/api/plans":
            _, result = projection.compare(self.read_data(8192))
            return result, 200

        match = RECORDS.fullmatch(path)
        if not match:
            raise ApiError("This page or record was not found.", status=404)
        resource, key = match.groups()
        item_id = int(key) if key else None
        table = "entries" if resource == "transactions" else resource
        if resource == "scenarios" and method == "GET":
            return (database.scenario(item_id) if item_id else database.scenario_list()), 200
        if method == "DELETE" and item_id:
            if registry and resource == "accounts" and self.workspace["kind"] == "profile":
                inherited = group_id(self.workspace["id"], item_id)
                groups = self.workspace["groups"]
                if any(inherited in registry.group_goal_accounts(group["id"]) for group in groups):
                    raise ApiError("Move the group goal’s funding before deleting this account.", status=409)
            database.delete(table, item_id)
            return {"id": item_id}, 200
        if (method == "POST" and item_id is None) or (method == "PUT" and item_id is not None):
            data = self.read_data(16_384)
            if resource == "accounts":
                saved = database.save_account(data, today, item_id)
            elif resource == "categories":
                saved = database.save_category(data, item_id)
            elif resource == "transactions":
                if self.user and self.workspace["kind"] == "group":
                    profile = owner_id(identifier(data.get("account_id"), "account_id"))
                    if profile and not self.can_use_personal_profile(profile):
                        raise ApiError("Only the account owner can record activity in this personal account.",
                                       "account_id", 403)
                saved = database.save_entry(data, today, item_id)
            else:
                name = line(data.get("name"), "name", 80)
                scenario = data.get("input")
                if not isinstance(scenario, dict):
                    raise ApiError("Enter the scenario inputs.", "input")
                inputs, result = projection.compare(scenario)
                inputs.update(plan_metadata(database, data))
                saved = database.save_scenario(name, inputs, result, item_id)
            result = {"id": saved}
            if resource == "transactions" and (owner := owner_id(saved)):
                result["attachment_target"] = {"id": local_id(saved), "workspace": owner}
            return result, 201 if method == "POST" else 200
        raise ApiError("This request method is unavailable.", status=405)

    def transactions(self, query, today):
        ledger = sample_ledger(today) if query.get("sample") == "1" else self.database.ledger()
        period = choice(query["period"], PERIODS, "period") if "period" in query else None
        kind = choice(query["kind"], (*ENTRY_KINDS, "settlement"), "kind") if "kind" in query else None
        category = query.get("category")
        if category != "uncategorized":
            category = identifier(category, "category")
        return transaction_list(
            ledger, page=max(1, int(query.get("page", 1))), size=min(100, max(1, int(query.get("size", 25)))),
            kind=kind, category=category, account=identifier(query.get("account"), "account"),
            descendants=query.get("descendants") == "1",
            search=line(query.get("search", ""), "search", 160, required=False),
            window=month_window(query["month"], today) if "month" in query else
                selected_window(ledger, period, today) if period else None,
        )

    def group_budget_contexts(self):
        registry = getattr(self.server, "workspaces", None)
        if registry and self.workspace["kind"] == "profile":
            for group in self.workspace["groups"]:
                with registry.group_access(group["id"]):
                    workspace = registry.workspace(group["id"])
                    if self.can_open_workspace(workspace):
                        yield registry.database(workspace), workspace


def main():
    parser = argparse.ArgumentParser(description="Run the Quant Financial Planner dashboard.")
    add_settings_arguments(parser)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--backup-now", action="store_true", help="take a backup and exit")
    operation.add_argument("--reset-password", metavar="USERNAME", help="reset a sign-in password from the server terminal")
    options = parser.parse_args()
    settings = settings_from_cli(parser, options)
    database = Database(settings.database)
    if options.backup_now:
        try:
            print(f"Backup saved at {create_backup(settings.database, settings.backups)}")
        except (OSError, sqlite3.Error, ValueError) as error:
            parser.exit(1, f"Quant backup failed: {error}\n")
        return
    if options.reset_password:
        password = getpass("New password: ")
        if password != getpass("Confirm password: "):
            parser.error("The passwords did not match.")
        try:
            with ledger_access(settings.database):
                store = AuthStore(WorkspaceRegistry(database))
                user = store.reset_password(options.reset_password, password)
        except ApiError as error:
            parser.error(str(error))
        print(f"Password updated for {user['username']}. Existing sessions have been signed out.")
        return
    try:
        binding = network_binding(settings.mode)
    except ValueError as error:
        parser.error(str(error))
    setup_code = os.environ.get("QFP_SETUP_CODE")
    if setup_code is not None and not (20 <= len(setup_code) <= 256 and setup_code.strip() == setup_code):
        parser.error("QFP_SETUP_CODE must contain 20 to 256 characters without surrounding whitespace.")
    host = binding.host if binding else "127.0.0.1"
    try:
        with ledger_access(settings.database):
            database.initialize()
            server = DashboardServer(settings.port, database, Projection(settings.projection_bin),
                                     public_url=settings.public_url, setup_code=setup_code, binding=binding)
    except OSError as error:
        if error.errno == errno.EADDRNOTAVAIL:
            parser.exit(1, f"Cannot start Quant. The listen address {host} is unavailable.\n")
        if error.errno != errno.EADDRINUSE:
            raise
        print(f"Cannot start Quant Financial Planner. Port {settings.port} is already in use on {host}.", file=sys.stderr)
        if settings.public_url:
            print("Free the port or choose another --port and update the reverse proxy upstream.", file=sys.stderr)
        else:
            print("Free the port or rerun with --port 0 to select an available port.", file=sys.stderr)
        raise SystemExit(1) from None
    with server:
        address = server.public_url or f"http://{server.server_address[0]}:{server.server_port}"
        print(f"Dashboard available at {address}/", flush=True)
        if server.auth.needs_setup():
            print(f"Create the service owner at {address}/auth", flush=True)
            if setup_code is None:
                print(f"One-time setup code: {server.setup_code}", flush=True)
        print(f"SQLite ledger: {database.path}", flush=True)
        backups = BackupWorker(settings.database, settings.backups) if settings.backups.enabled else None
        if backups:
            backups.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if backups:
                backups.finish()


if __name__ == "__main__":
    main()
