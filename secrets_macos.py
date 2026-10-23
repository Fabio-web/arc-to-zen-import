from __future__ import annotations

from collections import Counter
import ctypes
import hashlib
import hmac
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit


CHROME_EPOCH_SECONDS = 11644473600
SESSION_EXPIRY_SECONDS = 253402300799


class SecretReadError(RuntimeError):
    pass


def _rows_with_binary_column(db, table: str, binary_column: str):

    if (table, binary_column) not in {('cookies', 'encrypted_value'), ('logins', 'password_value')}:
        raise SecretReadError('unsupported_secret_table')
    projection = []
    for column in db.execute(f'PRAGMA table_info("{table}")'):
        name = column[1]
        quoted = '"' + name.replace('"', '""') + '"'
        projection.append(f'CAST({quoted} AS BLOB) AS {quoted}' if name == binary_column else quoted)
    return db.execute(f'SELECT {", ".join(projection)} FROM "{table}"')


def get_arc_key() -> bytes:

    if sys.platform != "darwin":
        raise SecretReadError("macos_required")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-w", "-s", "Arc Safe Storage"],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecretReadError("keychain_unavailable") from None
    if result.returncode:
        raise SecretReadError("keychain_access_denied")
    password = result.stdout.removesuffix(b"\n")
    if not password:
        raise SecretReadError("keychain_empty_key")
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def _aes_cbc(data: bytes, key: bytes, operation: int) -> bytes:

    if sys.platform != "darwin":
        raise SecretReadError("macos_required")
    if len(key) != 16:
        raise SecretReadError("invalid_key_length")
    if not data or len(data) % 16:
        raise SecretReadError("invalid_ciphertext_length")
    try:
        common = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
        crypt = common.CCCrypt
        crypt.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        crypt.restype = ctypes.c_int
        output = ctypes.create_string_buffer(len(data) + 16)
        written = ctypes.c_size_t()
        result = crypt(
            operation, 0, 0, key, len(key), b" " * 16, data, len(data),
            output, len(output), ctypes.byref(written),
        )
        if result:
            raise SecretReadError("aes_failure")
        return output.raw[:written.value]
    except (OSError, AttributeError):
        raise SecretReadError("commoncrypto_unavailable") from None


def decrypt_blob(blob: bytes, key: bytes, host: str | None = None,
                 db_version: int = 0) -> str:

    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise SecretReadError("invalid_encrypted_value")
    blob = bytes(blob)
    if not blob.startswith(b"v10"):
        raise SecretReadError("unsupported_encryption_version")
    clear = _aes_cbc(blob[3:], key, 1)
    padding = clear[-1]
    if padding < 1 or padding > 16 or clear[-padding:] != bytes([padding]) * padding:
        raise SecretReadError("invalid_padding")
    clear = clear[:-padding]
    if host is not None and db_version >= 24:
        expected = hashlib.sha256(host.encode("utf-8")).digest()
        if len(clear) < 32 or not hmac.compare_digest(clear[:32], expected):
            raise SecretReadError("cookie_host_hash_mismatch")
        clear = clear[32:]
    try:
        return clear.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretReadError("invalid_plaintext_encoding") from None


def _chrome_seconds(value: object) -> float:
    return int(value) / 1_000_000 - CHROME_EPOCH_SECONDS


def _chrome_milliseconds(value: object, fallback: int) -> int:
    if not value or int(value) <= 0:
        return fallback
    converted = int(value) // 1000 - CHROME_EPOCH_SECONDS * 1000
    return converted if converted > 0 else fallback


def _origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        default = 80 if parsed.scheme == "http" else 443
        return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")
    except (ValueError, UnicodeError, TypeError):
        raise SecretReadError("unsupported_login_origin") from None


def _cookie_partition(row: dict) -> dict | None:
    site = row.get("top_frame_site_key", "")
    if not site:
        return None
    try:
        parsed = urlsplit(site)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError):
        raise SecretReadError("unsupported_cookie_partition") from None
    if "has_cross_site_ancestor" not in row:
        raise SecretReadError("missing_partition_ancestor_bit")
    ancestor = row["has_cross_site_ancestor"]
    if ancestor not in (0, 1):
        raise SecretReadError("unsupported_partition_ancestor_bit")
    return {"topLevelSite": site, "hasCrossSiteAncestor": bool(ancestor)}


def _cookie_from_row(row: dict, key: bytes, version: int, now: float) -> dict:
    host = row["host_key"]
    path = row["path"]
    name = row["name"]
    if (not all(isinstance(v, str) for v in (host, path, name))
            or any("\x00" in v for v in (host, path, name))
            or not host or not path.startswith("/")):
        raise SecretReadError("invalid_cookie_metadata")

    netflix_host = host.lstrip('.').lower()
    if netflix_host == 'netflix.com' or netflix_host.endswith('.netflix.com'):
        raise SecretReadError('netflix_session_requires_signin')
    same_site = {-1: 256, 0: 0, 1: 1, 2: 2}.get(row.get("samesite", -1))
    if same_site is None:
        raise SecretReadError("unsupported_cookie_same_site")
    source_scheme = row.get("source_scheme", 0)
    if source_scheme not in (0, 1, 2):
        raise SecretReadError("unsupported_cookie_scheme")
    partition = _cookie_partition(row)
    persistent = bool(row.get("is_persistent", row.get("has_expires", row["expires_utc"] > 0)))
    expiry = _chrome_seconds(row["expires_utc"]) if persistent else SESSION_EXPIRY_SECONDS
    if persistent and expiry <= now:
        raise SecretReadError("expired_cookie")
    encrypted = row.get("encrypted_value")
    if encrypted:
        value = decrypt_blob(encrypted, key, host=host, db_version=version)
    else:
        value = row.get("value", "")
        if not isinstance(value, str):
            raise SecretReadError("invalid_plaintext_encoding")
    if "\x00" in value:
        raise SecretReadError("invalid_cookie_value")
    return {
        "host": host, "path": path, "name": name, "value": value,
        "isSecure": bool(row["is_secure"]), "isHttpOnly": bool(row["is_httponly"]),
        "isSession": not persistent, "expirySeconds": expiry,
        "originAttributes": {}, "sameSite": same_site, "schemeMap": source_scheme,
        "isPartitioned": partition is not None, "partitionKey": partition,
    }


def read_cookies(profile_path: str | Path, key: bytes) -> dict:

    from arc_reader import snapshot_db

    profile = Path(profile_path)
    path = profile / "Network" / "Cookies"
    if not path.exists():
        path = profile / "Cookies"
    if not path.exists():
        return {"cookies": [], "skipped": {"cookie_database_missing": 1}}
    cookies = []
    skipped = Counter()
    with snapshot_db(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if row is None:
            raise SecretReadError("cookie_database_version_missing")
        version = int(row[0])
        now = time.time()
        for row in _rows_with_binary_column(db, 'cookies', 'encrypted_value'):
            try:
                cookies.append(_cookie_from_row(dict(row), key, version, now))
            except SecretReadError as error:
                skipped[str(error)] += 1
            except (ValueError, TypeError, KeyError, OverflowError):
                skipped["invalid_cookie_record"] += 1
    return {"cookies": cookies, "skipped": dict(skipped)}


def _login_from_row(row: dict, key: bytes, now_ms: int) -> dict:
    if row.get("blacklisted_by_user", 0):
        raise SecretReadError("blocked_login")
    if row.get("federation_url") or str(row.get("signon_realm", "")).startswith("federation:"):
        raise SecretReadError("federated_login")
    scheme = row.get("scheme", 0)
    if scheme not in (0, 1, 2):
        raise SecretReadError("unsupported_login_scheme")
    origin = _origin(row["origin_url"])
    if scheme == 0:
        action = _origin(row["action_url"]) if row.get("action_url") else ""
        realm = None
    else:
        action = None
        signon_realm = row.get("signon_realm", "")

        realm_origin = _origin(signon_realm)
        if realm_origin != origin:
            raise SecretReadError("unsupported_http_realm")
        parsed = urlsplit(signon_realm)
        realm = parsed.path.removeprefix("/")
        if parsed.query:
            realm += "?" + parsed.query
        if parsed.fragment:
            realm += "#" + parsed.fragment
    blob = row.get("password_value")
    if not blob:
        if row.get("keychain_identifier"):
            raise SecretReadError("keychain_reference_only")
        raise SecretReadError("empty_password")
    password = decrypt_blob(blob, key)
    if not password:
        raise SecretReadError("empty_password")
    username = row.get("username_value", "")
    username_field = row.get("username_element", "")
    password_field = row.get("password_element", "")
    if not all(isinstance(v, str) and "\x00" not in v
               for v in (username, password, username_field, password_field)):
        raise SecretReadError("invalid_login_record")
    created = _chrome_milliseconds(row.get("date_created"), now_ms)
    changed = _chrome_milliseconds(row.get("date_password_modified"), created)
    last_used = _chrome_milliseconds(row.get("date_last_used"), created)
    return {
        "origin": origin, "formActionOrigin": action, "httpRealm": realm,
        "username": username, "password": password,
        "usernameField": username_field, "passwordField": password_field,
        "timeCreated": created, "timePasswordChanged": changed,
        "timeLastUsed": last_used, "timesUsed": max(0, int(row.get("times_used") or 0)),
    }


def read_logins(profile_path: str | Path, key: bytes) -> dict:

    from arc_reader import snapshot_db

    path = Path(profile_path) / "Login Data"
    if not path.exists():
        return {"logins": [], "skipped": {"login_database_missing": 1}}
    logins = []
    skipped = Counter()
    with snapshot_db(path) as db:
        db.row_factory = sqlite3.Row
        now_ms = int(time.time() * 1000)
        for row in _rows_with_binary_column(db, 'logins', 'password_value'):
            try:
                logins.append(_login_from_row(dict(row), key, now_ms))
            except SecretReadError as error:
                skipped[str(error)] += 1
            except (ValueError, TypeError, KeyError, OverflowError):
                skipped["invalid_login_record"] += 1
    return {"logins": logins, "skipped": dict(skipped)}
