"""Per-user provider credentials encrypted with private Fernet master keys."""
import os
import re
import stat
import tempfile

from .import_ai import validate_api_key as validate_groq_key
from .values import ApiError


SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    user_id INTEGER PRIMARY KEY REFERENCES auth_users(id) ON DELETE CASCADE,
    ciphertext BLOB NOT NULL
);
"""
MASTER_SHAPE = re.compile(rb"[A-Za-z0-9_-]{43}=")


def _load_fernet():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        return None
    return Fernet, InvalidToken


class _ProviderKeyStore:
    def __init__(self, registry):
        self.registry = registry
        self._table = f"auth_{self._slug}_keys"
        self.key_path = registry.folder / f"{self._slug}-fernet.key"
        self._crypto = _load_fernet()
        with registry.connection(write=True) as conn:
            conn.execute(SCHEMA.format(table=self._table))

    @property
    def available(self):
        return self._crypto is not None

    def _sign_in_required(self):
        return ApiError(f"Sign in to manage a saved {self._name} key.", status=401)

    def _account(self, user_id):
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise self._sign_in_required()
        return user_id

    def _unavailable(self):
        return ApiError(f"Saved {self._name} keys are unavailable. Check the server’s encryption key and permissions.",
                        status=503)

    def _prefix(self, user_id):
        return f"{self._slug}-v1:{user_id}:".encode("ascii")

    def has(self, user_id):
        with self.registry.connection() as conn:
            return conn.execute(
                f"SELECT 1 FROM {self._table} WHERE user_id = ?", (self._account(user_id),),
            ).fetchone() is not None

    def _require_crypto(self):
        if not self.available:
            raise ApiError(f"Install the server’s optional cryptography dependency to use saved {self._name} keys.",
                           status=503)
        return self._crypto

    def _check_directory(self):
        folder = self.key_path.parent.lstat()
        if (not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.geteuid()
                or stat.S_IMODE(folder.st_mode) & 0o077):
            raise self._unavailable()

    def _read_master(self):
        def secure_open(path, flags):
            return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)

        with open(self.key_path, "rb", opener=secure_open) as master:
            info = os.fstat(master.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
                raise self._unavailable()
            value = master.read(45)
            if not MASTER_SHAPE.fullmatch(value):
                raise self._unavailable()
            return value

    def _create_master(self, fernet):
        # Publish the complete private file without replacing a concurrently created key.
        temporary = tempfile.NamedTemporaryFile(dir=self.key_path.parent, prefix=f".{self._slug}-", delete=False)
        try:
            with temporary:
                os.fchmod(temporary.fileno(), 0o600)
                temporary.write(fernet.generate_key())
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(temporary.name, self.key_path, follow_symlinks=False)
            except FileExistsError:
                return
            fd = os.open(self.key_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.unlink(temporary.name)

    def _cipher(self, conn, *, create=False):
        fernet, _ = self._require_crypto()
        try:
            self._check_directory()
            try:
                master = self._read_master()
            except FileNotFoundError:
                if not create or conn.execute(f"SELECT 1 FROM {self._table} LIMIT 1").fetchone():
                    raise self._unavailable() from None
                self._create_master(fernet)
                master = self._read_master()
            return fernet(master)
        except (OSError, ValueError):
            raise self._unavailable() from None

    def get(self, user_id):
        user_id = self._account(user_id)
        with self.registry.connection() as conn:
            row = conn.execute(f"""
                SELECT k.ciphertext FROM {self._table} AS k JOIN auth_users AS u ON u.id = k.user_id
                WHERE k.user_id = ? AND u.enabled = 1
            """, (user_id,)).fetchone()
            if row is None:
                return None
            cipher = self._cipher(conn)
            _, invalid_token = self._require_crypto()
            try:
                plaintext = cipher.decrypt(row["ciphertext"])
                prefix = self._prefix(user_id)
                if not plaintext.startswith(prefix):
                    raise self._unavailable()
                return self._validate_key(plaintext[len(prefix):].decode("ascii"))
            except (invalid_token, UnicodeError, TypeError, ApiError):
                raise self._unavailable() from None

    def save(self, user_id, key, *, only_if_missing=False):
        user_id = self._account(user_id)
        key = self._validate_key(key)
        with self.registry.connection(write=True) as conn:
            if not conn.execute("SELECT 1 FROM auth_users WHERE id = ? AND enabled = 1", (user_id,)).fetchone():
                raise self._sign_in_required()
            if only_if_missing and conn.execute(f"SELECT 1 FROM {self._table} WHERE user_id = ?", (user_id,)).fetchone():
                return False
            cipher = self._cipher(conn, create=True)
            encrypted = cipher.encrypt(self._prefix(user_id) + key.encode("ascii"))
            conn.execute(f"""
                INSERT INTO {self._table}(user_id, ciphertext) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET ciphertext = excluded.ciphertext
            """, (user_id, encrypted))
            return True

    def remove(self, user_id):
        with self.registry.connection(write=True) as conn:
            conn.execute(f"DELETE FROM {self._table} WHERE user_id = ?", (self._account(user_id),))


class GroqKeyStore(_ProviderKeyStore):
    _name = "Groq"
    _slug = "groq"
    _validate_key = staticmethod(validate_groq_key)


class OpenAIKeyStore(_ProviderKeyStore):
    _name = "OpenAI"
    _slug = "openai"

    @staticmethod
    def _validate_key(value):
        from .import_openai import validate_api_key

        return validate_api_key(value)
