"""HTTP session cookies and account management for the dashboard."""

import re
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from .auth import SESSION_SECONDS
from .starter_categories import starter_requested
from .values import ApiError


def public_origin(url):
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        hostname, port = parsed.hostname, parsed.port
        if (parsed.scheme != "https" or not hostname or parsed.username is not None
                or not re.fullmatch(r"[a-z0-9.:-]+", hostname)
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or any(character.isspace() for character in url)):
            raise ValueError
    except ValueError as exc:
        raise ValueError("Use an HTTPS origin such as https://finance.example.com for --public-url.") from exc
    host = f"[{hostname}]" if ":" in hostname else hostname
    return "https://" + host + (f":{port}" if port not in (None, 443) else "")


def session_token(handler):
    try:
        cookies = SimpleCookie(handler.headers.get("Cookie", ""))
    except CookieError:
        return None
    name = "__Host-qfp_session" if handler.server.public_url else "qfp_session"
    return cookies[name].value if name in cookies else None


def send_session(handler, token=None):
    secure = "; Secure" if handler.server.public_url else ""
    name = "__Host-qfp_session" if secure else "qfp_session"
    seconds = SESSION_SECONDS if token else 0
    handler.session_cookie = f"{name}={token or ''}; Path=/; HttpOnly; SameSite=Strict; Max-Age={seconds}{secure}"


def session_state(store, user):
    return {
        "required": True, "configured": bool(user) or not store.needs_setup(),
        "username": user["username"] if user else None,
        "profile_id": user["profile_id"] if user else None,
        "is_admin": bool(user and user["is_admin"]),
    }


def signed_in(user):
    if not user:
        raise ApiError("Sign in to continue.", status=401)


def admin_only(user):
    signed_in(user)
    if not user["is_admin"]:
        raise ApiError("Only the service owner can manage family access and workspaces.", status=403)


def auth_api(handler, store, path, method, token, user):
    remote = handler.client_address[0]
    action = path.removeprefix("/api/auth/")
    if method == "GET" and action == "state":
        return session_state(store, user), 200
    if method == "POST" and action in {"setup", "login", "register", "accept", "password", "logout"}:
        data = handler.read_data(16384)
        username, password = data.get("username"), data.get("password")
        if action == "logout":
            store.logout(token)
            send_session(handler)
            return session_state(store, None), 200
        if action == "setup":
            result = store.bootstrap(username, password, data.get("setup_code"),
                                     starter_categories=starter_requested(data))
        elif action == "login":
            result = store.login(username, password, remote)
        elif action == "register":
            result = store.register(username, password, remote, starter_categories=starter_requested(data))
        elif action == "accept":
            result = store.accept_invite(data.get("code"), username, password, remote,
                                         starter_categories=starter_requested(data))
        else:
            signed_in(user)
            result = store.change_password(user["id"], data.get("current_password"), password, remote)
        if token:
            store.logout(token)
        send_session(handler, result["token"])
        return session_state(store, result["user"]), 201 if action == "register" else 200

    admin_only(user)
    if method == "GET" and action == "members":
        return store.members(user["id"]), 200
    if method == "POST" and action == "invitations":
        profile_id = handler.read_data(8192).get("profile_id")
        return store.issue_invite(user["id"], profile_id), 201
    if method == "DELETE" and (invite := re.fullmatch(r"invitations/([1-9][0-9]{0,17})", action)):
        key = int(invite[1])
        store.revoke_invite(user["id"], key)
        return {"id": key}, 200
    if method == "PUT" and (member := re.fullmatch(r"members/([1-9][0-9]{0,17})", action)):
        key = int(member[1])
        store.set_enabled(user["id"], key, handler.read_data(8192).get("enabled"))
        return {"id": key}, 200
    if method == "DELETE" and (member := re.fullmatch(r"members/([1-9][0-9]{0,17})/sessions", action)):
        key = int(member[1])
        store.revoke_sessions(user["id"], key)
        return {"id": key}, 200
    raise ApiError("This account action was not found.", status=404)
