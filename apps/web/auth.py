"""Credential, invitation, and session persistence for the workspace registry."""
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time

from .starter_categories import apply_starter_categories
from .values import ApiError, identifier


SESSION_SECONDS = 7 * 24 * 60 * 60
INVITE_SECONDS = 48 * 60 * 60
RATE_WINDOW = 15 * 60
NAME_RATE_LIMIT = 8
ADDRESS_RATE_LIMIT = 40
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.@-]{2,63}")
# OWASP scrypt configuration with a 32 MiB working set per password operation.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 3
_PASSWORD_SLOTS = threading.BoundedSemaphore(4)
_DUMMY = "scrypt-v1$" + "00" * 16 + "$" + "00" * 32
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL UNIQUE REFERENCES workspaces(id),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES workspaces(id),
    token_hash BLOB NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES auth_users(id),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked_at INTEGER
);
CREATE INDEX IF NOT EXISTS auth_invitation_profile ON auth_invitations(profile_id, expires_at);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash BLOB PRIMARY KEY NOT NULL,
    user_id INTEGER NOT NULL REFERENCES auth_users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_session_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS auth_session_expiry ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    login_key BLOB,
    address_key BLOB NOT NULL,
    attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_attempts_name ON auth_attempts(scope, login_key, attempted_at);
CREATE INDEX IF NOT EXISTS auth_attempts_address ON auth_attempts(scope, address_key, attempted_at);
CREATE INDEX IF NOT EXISTS auth_attempts_at ON auth_attempts(attempted_at);
COMMIT;
"""


def _username(value, *, required=False):
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if len(normalized) <= 64 and _USERNAME.fullmatch(normalized):
        return normalized
    if required:
        raise ApiError("Use 3–64 letters, numbers, dots, underscores, @ signs or hyphens.", "username")
    return None


def _password(value, *, new=False):
    if isinstance(value, str) and value:
        try:
            return value.encode("utf-8")
        except UnicodeEncodeError:
            pass
    if new:
        raise ApiError("Enter a password.", "password")
    return None


def _derive(secret, salt):
    if not _PASSWORD_SLOTS.acquire(blocking=False):
        raise ApiError("Sign-in is busy. Try again shortly.", status=429)
    try:
        return hashlib.scrypt(secret, salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                              dklen=32, maxmem=64 * 1024 * 1024)
    finally:
        _PASSWORD_SLOTS.release()


def _password_hash(secret):
    salt = secrets.token_bytes(16)
    return "scrypt-v1$" + salt.hex() + "$" + _derive(secret, salt).hex()


def _verify(secret, encoded):
    try:
        version, salt_hex, hash_hex = encoded.split("$")
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False
    if version != "scrypt-v1" or len(salt) != 16 or len(expected) != 32:
        return False
    return hmac.compare_digest(_derive(secret, salt), expected)


def _key(value):
    return hashlib.sha256(value.encode("ascii")).digest()


def _token_hash(value):
    return _key(value) if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _remote_key(value):
    try:
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError()
        ip = ipaddress.ip_address(value.split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        prefix = str(ip) if ip.version == 4 else str(ipaddress.ip_network((ip, 64), strict=False))
    except ValueError:
        prefix = "unknown"
    return _key(prefix)


def _user(row):
    return {"id": row["id"], "profile_id": row["profile_id"], "username": row["username"],
            "is_admin": bool(row["is_admin"]), "enabled": bool(row["enabled"])}


def _admin(conn, actor):
    if type(actor) is not int or not conn.execute(
        "SELECT 1 FROM auth_users WHERE id = ? AND enabled = 1 AND is_admin = 1", (actor,),
    ).fetchone():
        raise ApiError("Administrator access is required.", status=403)


def _user_row(conn, user_id):
    row = conn.execute("SELECT * FROM auth_users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise ApiError("This user was not found.", status=404)
    return row


def _profile(conn, profile_id):
    if not conn.execute("""
        SELECT 1 FROM workspaces WHERE id = ? AND kind = 'profile'
        AND NOT EXISTS (SELECT 1 FROM demo_workspaces WHERE workspace_id = workspaces.id)
    """, (profile_id,)).fetchone():
        raise ApiError("Choose an existing personal profile.", "profile_id")


def _unclaimed(conn, profile_id):
    if conn.execute("SELECT 1 FROM auth_users WHERE profile_id = ?", (profile_id,)).fetchone():
        raise ApiError("A sign-in already exists for this profile.", "profile_id", 409)


def _create_personal_workspace(conn, username):
    name = username
    number = 2
    while conn.execute("SELECT 1 FROM workspaces WHERE name = ?", (name,)).fetchone():
        suffix = f" ({number})"
        name = username[:65 - len(suffix)] + suffix
        number += 1
    return conn.execute("INSERT INTO workspaces (name, kind) VALUES (?, 'profile')", (name,)).lastrowid


def _create_user(conn, profile_id, username, password_hash, now, is_admin=False):
    _profile(conn, profile_id)
    _unclaimed(conn, profile_id)
    if conn.execute("SELECT 1 FROM auth_users WHERE username = ?", (username,)).fetchone():
        raise ApiError("This username is unavailable.", "username", 409)
    row_id = conn.execute("""
        INSERT INTO auth_users(profile_id, username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)
    """, (profile_id, username, password_hash, is_admin, now)).lastrowid
    return _user_row(conn, row_id)


def _new_session(conn, row, now):
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
    conn.execute("INSERT INTO auth_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                 (_key(token), row["id"], now + SESSION_SECONDS))
    return {"token": token, "user": _user(row)}


def _failed_auth():
    return ApiError("The username or password is incorrect.", status=401)


def _invalid_invite():
    return ApiError("This invitation is unavailable. Ask an administrator for a new one.", status=403)


class AuthStore:
    def __init__(self, registry, bootstrap_token=None, *, clock=None):
        self.registry = registry
        self.clock = clock or time.time
        self._setup_digest = self._digest_setup(bootstrap_token)
        with registry.connection(write=True) as conn:
            conn.executescript(SCHEMA)

    @staticmethod
    def _digest_setup(value):
        # Setup may be operator-configured as a passphrase or generated as a random token.
        if not isinstance(value, str) or not 20 <= len(value) <= 256:
            return None
        try:
            return hashlib.sha256(value.encode("utf-8")).digest()
        except UnicodeEncodeError:
            return None

    def _now(self):
        return int(self.clock())

    def needs_setup(self):
        with self.registry.connection() as conn:
            return not conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone()

    def _attempt(self, scope, username, remote):
        now = self._now()
        threshold = now - RATE_WINDOW
        login_key = _key(username) if username is not None else None
        address_key = _remote_key(remote)
        with self.registry.connection(write=True) as conn:
            conn.execute("DELETE FROM auth_attempts WHERE attempted_at <= ?", (threshold,))
            by_address = conn.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE scope = ? AND address_key = ?", (scope, address_key),
            ).fetchone()[0]
            by_name = conn.execute(
                "SELECT COUNT(*) FROM auth_attempts WHERE scope = ? AND login_key = ?", (scope, login_key),
            ).fetchone()[0] if login_key is not None else 0
            if by_name >= NAME_RATE_LIMIT or by_address >= ADDRESS_RATE_LIMIT:
                raise ApiError("Too many attempts. Try again in a few minutes.", status=429)
            conn.execute("""
                INSERT INTO auth_attempts(scope, login_key, address_key, attempted_at) VALUES (?, ?, ?, ?)
            """, (scope, login_key, address_key, now))

    def bootstrap(self, username, password, setup_token, profile_id=1, *, starter_categories=False):
        candidate = self._digest_setup(setup_token)
        if self._setup_digest is None or candidate is None or not hmac.compare_digest(self._setup_digest, candidate):
            raise ApiError("The setup code is incorrect or no longer active.", status=403)
        if not self.needs_setup():
            raise ApiError("The dashboard already has an administrator.", status=409)
        login = _username(username, required=True)
        secret = _password_hash(_password(password, new=True))
        now = self._now()
        with self.registry.connection(write=True) as conn:
            if conn.execute("SELECT 1 FROM auth_users LIMIT 1").fetchone():
                raise ApiError("The dashboard already has an administrator.", status=409)
            created = _create_user(conn, profile_id, login, secret, now, is_admin=True)
            session = _new_session(conn, created, now)
            if starter_categories:
                apply_starter_categories(self.registry.database({"id": profile_id, "kind": "profile"}))
            return session

    def login(self, username, password, remote):
        login = _username(username)
        self._attempt("password", login, remote)
        secret = _password(password)
        if login is None or secret is None:
            raise _failed_auth()
        with self.registry.connection() as conn:
            observed = conn.execute("SELECT * FROM auth_users WHERE username = ?", (login,)).fetchone()
        check = observed["password_hash"] if observed and observed["enabled"] else _DUMMY
        verified = _verify(secret, check)
        if not observed or not observed["enabled"] or not verified:
            raise _failed_auth()
        with self.registry.connection(write=True) as conn:
            current = _user_row(conn, observed["id"])
            if not current["enabled"] or current["password_hash"] != check:
                raise _failed_auth()
            conn.execute("UPDATE auth_attempts SET login_key = NULL WHERE scope = 'password' AND login_key = ?", (_key(login),))
            return _new_session(conn, current, self._now())

    def register(self, username, password, remote, *, starter_categories=False):
        if self.needs_setup():
            raise ApiError("Complete Quant’s administrator setup first.", status=403)
        login = _username(username, required=True)
        self._attempt("register", login, remote)
        secret = _password_hash(_password(password, new=True))
        with self.registry.connection(write=True) as conn:
            profile_id = _create_personal_workspace(conn, login)
            now = self._now()
            created = _create_user(conn, profile_id, login, secret, now)
            if starter_categories:
                apply_starter_categories(self.registry.database({"id": profile_id, "kind": "profile"}))
            return _new_session(conn, created, now)

    def session(self, token):
        digest = _token_hash(token)
        if digest is None:
            return None
        with self.registry.connection() as conn:
            row = conn.execute("""
                SELECT u.* FROM auth_sessions AS s JOIN auth_users AS u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.enabled = 1
            """, (digest, self._now())).fetchone()
        return _user(row) if row else None

    def logout(self, token):
        digest = _token_hash(token)
        if digest is not None:
            with self.registry.connection(write=True) as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (digest,))

    def issue_invite(self, actor_id, profile_id):
        profile_id = identifier(profile_id, "profile_id")
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires = now + INVITE_SECONDS
        with self.registry.connection(write=True) as conn:
            _admin(conn, actor_id)
            _profile(conn, profile_id)
            _unclaimed(conn, profile_id)
            conn.execute("""
                UPDATE auth_invitations SET revoked_at = ?
                WHERE profile_id = ? AND expires_at > ? AND used_at IS NULL AND revoked_at IS NULL
            """, (now, profile_id, now))
            invite_id = conn.execute("""
                INSERT INTO auth_invitations(profile_id, token_hash, created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
            """, (profile_id, _key(token), actor_id, now, expires)).lastrowid
        return {"id": invite_id, "profile_id": profile_id, "token": token, "expires_at": expires}

    def _find_invite(self, conn, digest):
        row = conn.execute("""
            SELECT id, profile_id FROM auth_invitations
            WHERE token_hash = ? AND expires_at > ? AND used_at IS NULL AND revoked_at IS NULL
        """, (digest, self._now())).fetchone()
        if row is None:
            raise _invalid_invite()
        return row

    def accept_invite(self, token, username, password, remote, *, starter_categories=False):
        login = _username(username, required=True)
        self._attempt("invite", login, remote)
        digest = _token_hash(token)
        if digest is None:
            raise _invalid_invite()
        with self.registry.connection() as conn:
            self._find_invite(conn, digest)
        secret = _password_hash(_password(password, new=True))
        with self.registry.connection(write=True) as conn:
            invitation = self._find_invite(conn, digest)
            now = self._now()
            created = _create_user(conn, invitation["profile_id"], login, secret, now)
            conn.execute("UPDATE auth_invitations SET used_at = ? WHERE id = ?", (now, invitation["id"]))
            session = _new_session(conn, created, now)
            if starter_categories:
                apply_starter_categories(self.registry.database({"id": invitation["profile_id"], "kind": "profile"}))
            return session

    def members(self, actor_id):
        now = self._now()
        with self.registry.connection() as conn:
            _admin(conn, actor_id)
            profiles = [dict(row) for row in conn.execute(
                """SELECT id, name, kind FROM workspaces WHERE kind = 'profile'
                   AND NOT EXISTS (SELECT 1 FROM demo_workspaces WHERE workspace_id = workspaces.id)
                   ORDER BY name, id""",
            )]
            users = [_user(row) for row in conn.execute("SELECT * FROM auth_users ORDER BY username")]
            invitations = [dict(row) for row in conn.execute("""
                SELECT id, profile_id, created_at, expires_at, used_at, revoked_at FROM auth_invitations
                ORDER BY id DESC
            """)]
        for invite in invitations:
            if invite["used_at"] is not None:
                status = "used"
            elif invite["revoked_at"] is not None:
                status = "revoked"
            elif invite["expires_at"] <= now:
                status = "expired"
            else:
                status = "pending"
            invite["status"] = status
        return {"profiles": profiles, "users": users, "invitations": invitations}

    def revoke_invite(self, actor_id, invite_id):
        invite_id = identifier(invite_id, "invitation")
        with self.registry.connection(write=True) as conn:
            _admin(conn, actor_id)
            row = conn.execute("SELECT used_at FROM auth_invitations WHERE id = ?", (invite_id,)).fetchone()
            if row is None:
                raise ApiError("This invitation was not found.", status=404)
            if row["used_at"] is not None:
                raise ApiError("This invitation was already used.", status=409)
            conn.execute("UPDATE auth_invitations SET revoked_at = coalesce(revoked_at, ?) WHERE id = ?",
                         (self._now(), invite_id))

    def set_enabled(self, actor_id, user_id, enabled):
        if type(enabled) is not bool:
            raise ApiError("Choose whether this sign-in is enabled.", "enabled")
        user_id = identifier(user_id, "user")
        with self.registry.connection(write=True) as conn:
            _admin(conn, actor_id)
            target = _user_row(conn, user_id)
            if not enabled and target["is_admin"] and target["enabled"] and conn.execute(
                "SELECT COUNT(*) FROM auth_users WHERE is_admin = 1 AND enabled = 1",
            ).fetchone()[0] <= 1:
                raise ApiError("Keep at least one administrator enabled.", status=409)
            conn.execute("UPDATE auth_users SET enabled = ? WHERE id = ?", (enabled, user_id))
            if not enabled:
                conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            return _user(_user_row(conn, user_id))

    def revoke_sessions(self, actor_id, user_id):
        user_id = identifier(user_id, "user")
        with self.registry.connection(write=True) as conn:
            _admin(conn, actor_id)
            _user_row(conn, user_id)
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    def change_password(self, user_id, old, new, remote):
        with self.registry.connection() as conn:
            before = _user_row(conn, user_id)
        self._attempt("password", before["username"], remote)
        supplied = _password(old)
        if not before["enabled"] or supplied is None or not _verify(supplied, before["password_hash"]):
            raise _failed_auth()
        replacement = _password_hash(_password(new, new=True))
        with self.registry.connection(write=True) as conn:
            current = _user_row(conn, user_id)
            if not current["enabled"] or current["password_hash"] != before["password_hash"]:
                raise _failed_auth()
            self._rotate_password(conn, current, replacement)
            return _new_session(conn, current, self._now())

    def reset_password(self, username, password):
        """Rotate a password from the local operator console without issuing a session."""
        login = _username(username, required=True)
        replacement = _password_hash(_password(password, new=True))
        with self.registry.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM auth_users WHERE username = ?", (login,)).fetchone()
            if row is None:
                raise ApiError("This username was not found.", "username", 404)
            self._rotate_password(conn, row, replacement)
            return _user(row)

    @staticmethod
    def _rotate_password(conn, row, replacement):
        conn.execute("UPDATE auth_users SET password_hash = ? WHERE id = ?", (replacement, row["id"]))
        conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (row["id"],))
        conn.execute("UPDATE auth_attempts SET login_key = NULL WHERE scope = 'password' AND login_key = ?",
                     (_key(row["username"]),))
