from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unicodedata
from urllib.parse import urlsplit


CHROMIUM_EPOCH_US = 11_644_473_600_000_000


_SPACE_ICONS = {
    "musicalnote": "🎵", "musicnote": "🎵", "music": "🎵",
    "colorpallete": "🎨", "colorpalette": "🎨", "paintpalette": "🎨", "palette": "🎨",
    "bulb": "💡", "lightbulb": "💡", "house": "🏠", "home": "🏠",
    "briefcase": "💼", "work": "💼", "suitcase": "🧳", "book": "📖",
    "books": "📚", "graduationcap": "🎓", "pencil": "✏️", "pen": "🖊️",
    "code": "💻", "computer": "💻", "laptop": "💻", "terminal": "💻",
    "gamecontroller": "🎮", "gamepad": "🎮", "camera": "📷", "film": "🎬",
    "video": "🎬", "headphones": "🎧", "shoppingbag": "🛍️", "shoppingcart": "🛒",
    "cart": "🛒", "bag": "🛍️", "car": "🚘", "airplane": "✈️", "plane": "✈️",
    "globe": "🌍", "earth": "🌍", "heart": "❤️", "star": "⭐", "sun": "☀️",
    "moon": "🌙", "cloud": "☁️", "bolt": "⚡", "lightning": "⚡", "flame": "🔥",
    "fire": "🔥", "leaf": "🍃", "tree": "🌳", "flower": "🌸", "mountain": "⛰️",
    "coffee": "☕", "cup": "☕", "forkknife": "🍴", "food": "🍴", "pizza": "🍕",
    "dumbbell": "🏋️", "fitness": "🏋️", "running": "🏃", "bicycle": "🚲",
    "soccerball": "⚽", "basketball": "🏀", "football": "🏈", "tennisball": "🎾",
    "calendar": "📅", "clock": "🕒", "checkmark": "✅", "check": "✅",
    "folder": "📁", "document": "📄", "paperclip": "📎", "envelope": "✉️",
    "mail": "✉️", "chat": "💬", "bubble": "💬", "person": "👤", "people": "👥",
    "lock": "🔒", "key": "🔑", "gear": "⚙️", "settings": "⚙️", "flag": "🚩",
    "gift": "🎁", "rocket": "🚀", "paw": "🐾", "dog": "🐶", "cat": "🐱",
}


def _is_emoji(value):
    if not isinstance(value, str) or not value or len(value) > 32:
        return False
    has_base = False
    for char in value:
        point = ord(char)
        category = unicodedata.category(char)
        if char.isspace() or category in ("Cc", "Cs") or category.startswith("L"):
            return False
        if category.startswith("N") and not (char in "0123456789" and "\u20e3" in value):
            return False
        has_base |= (0x1F000 <= point <= 0x1FAFF or 0x2190 <= point <= 0x27FF
                     or point in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x20E3, 0x2122, 0x2139))
    return has_base


def _space_icon(icon_type, warnings):
    if not isinstance(icon_type, dict):
        return None


    modern = icon_type.get("emoji_v2")
    if _is_emoji(modern):
        return modern
    legacy = icon_type.get("emoji")
    if isinstance(legacy, int) and not isinstance(legacy, bool) and 0 <= legacy <= 0x10FFFF:
        legacy = chr(legacy)
    if _is_emoji(legacy):
        return legacy
    named = icon_type.get("icon")
    if _is_emoji(named):
        return named
    if isinstance(named, str) and named:
        key = "".join(char for char in named.casefold() if char.isalnum())
        if key in _SPACE_ICONS:
            return _SPACE_ICONS[key]
        warnings["unsupported_space_icon_replaced"] += 1
        return "🗂️"
    if modern or legacy:
        warnings["unsupported_space_icon_replaced"] += 1
        return "🗂️"
    return None


def _json(path: Path) -> dict:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Impossible de lire {path.name}.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"Structure inattendue dans {path.name}.")
    return result


def _fingerprint(path: Path):
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None


@contextmanager
def snapshot_db(path: Path):

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Base absente : {path.name}")
    wal = Path(str(path) + "-wal")
    with tempfile.TemporaryDirectory(prefix="arc-to-zen-db-") as dirname:
        directory = Path(dirname)
        directory.chmod(0o700)
        target = directory / "snapshot.sqlite"
        target_wal = Path(str(target) + "-wal")
        stable = False
        for _ in range(3):
            before = (_fingerprint(path), _fingerprint(wal))
            if before[0] is None:
                raise FileNotFoundError(f"Base absente : {path.name}")
            try:
                with path.open("rb") as source, target.open("wb") as output:
                    os.chmod(target, 0o600)
                    shutil.copyfileobj(source, output)
                if before[1] is not None:
                    with wal.open("rb") as source, target_wal.open("wb") as output:
                        os.chmod(target_wal, 0o600)
                        shutil.copyfileobj(source, output)
                else:
                    target_wal.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            if before == (_fingerprint(path), _fingerprint(wal)):
                stable = True
                break
        if not stable:
            raise RuntimeError(f"{path.name} change pendant la lecture. Fermer Arc puis réessayer.")
        connection = sqlite3.connect(target, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")

            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            yield connection
        finally:
            connection.close()


def discover_profiles(arc_root: Path) -> list[dict]:

    data_root = Path(arc_root) / "User Data"
    if not data_root.is_dir():
        raise FileNotFoundError("Le dossier User Data d’Arc est introuvable.")
    local_state = data_root / "Local State"
    cache = _json(local_state).get("profile", {}).get("info_cache", {}) if local_state.exists() else {}
    if not isinstance(cache, dict):
        cache = {}
    candidates = list(cache)
    for directory in sorted(data_root.iterdir()):
        if directory.is_dir() and (directory.name == "Default" or directory.name.startswith("Profile ")):
            if directory.name not in candidates:
                candidates.append(directory.name)
    result = []
    for profile_id in candidates:

        if not isinstance(profile_id, str) or Path(profile_id).name != profile_id or profile_id in (".", ".."):
            continue
        directory = data_root / profile_id
        if not directory.is_dir() or directory.resolve().parent != data_root.resolve():
            continue
        metadata = cache.get(profile_id, {})
        name = metadata.get("name") if isinstance(metadata, dict) else None
        result.append({"id": profile_id, "name": name or profile_id, "path": directory})
    return sorted(result, key=lambda profile: (profile["id"] != "Default", profile["id"]))


def _pairs(value):
    if isinstance(value, dict):
        return list(value.items())
    if not isinstance(value, list):
        return []
    if value and all(isinstance(item, dict) and "id" in item for item in value):
        return [(item["id"], item) for item in value]
    if len(value) % 2:
        raise ValueError("Liste Arc de clés et valeurs incomplète.")
    return list(zip(value[0::2], value[1::2]))


def _records(value):
    result = []
    for key, record in _pairs(value):
        if isinstance(record, dict) and isinstance(record.get("value"), dict):
            record = record["value"]
        if isinstance(key, str) and isinstance(record, dict):
            result.append((key, record))
    return result


def _profile_key(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    if "default" in value:
        return "Default"
    for key in ("custom", "profile", "_0", "id"):
        if key in value:
            found = _profile_key(value[key])
            if found:
                return found
    return None


def _profile_aliases(arc_root: Path, profile_id: str):
    aliases = {profile_id}
    path = arc_root / "StorableProfiles.json"
    if not path.exists():
        return aliases
    profiles = _json(path).get("profiles", [])
    if not isinstance(profiles, list):
        return aliases
    directory_keys = ("directoryBasename", "directoryName", "profileDirectory", "chromiumProfileDirectory", "path")
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if any(profile.get(key) == profile_id for key in directory_keys):
            identifier = _profile_key(profile.get("id"))
            if identifier:
                aliases.add(identifier)
    return aliases


def _valid_url(value, *, history=False):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        allowed = {"http", "https", "ftp"} if history else {"http", "https", "ftp", "file"}
        return parsed.scheme.lower() in allowed and (bool(parsed.hostname) or parsed.scheme.lower() == "file")
    except ValueError:
        return False


def extract_sidebar(arc_root: Path, profile_id="Default") -> dict:

    arc_root = Path(arc_root)
    document = _json(arc_root / "StorableSidebar.json")
    sidebar = document.get("sidebar")
    if not isinstance(sidebar, dict) or not isinstance(sidebar.get("containers"), list):
        raise ValueError("Format de barre latérale Arc non reconnu.")
    aliases = _profile_aliases(arc_root, profile_id)
    warnings = Counter()
    result = {"spaces": [], "favorites": [], "warnings": []}
    for container in sidebar["containers"]:
        if not isinstance(container, dict) or not ("spaces" in container or "items" in container):
            continue
        records = _records(container.get("items"))
        items = dict(records)
        parent_children = defaultdict(list)
        for identifier, item in records:
            if isinstance(item.get("parentID"), str):
                parent_children[item["parentID"]].append(identifier)

        def children(identifier):
            item = items.get(identifier, {})
            listed = item.get("childrenIds", [])
            if not isinstance(listed, list):
                listed = []
                warnings["malformed_children"] += 1
            ordered = list(dict.fromkeys(child for child in listed if isinstance(child, str)))
            for child in parent_children[identifier]:
                if child not in ordered:
                    ordered.append(child)
                    warnings["children_recovered_from_parent"] += 1
            return ordered

        def walk(identifier, ancestors=frozenset(), depth=0):
            if identifier in ancestors or depth > 100:
                warnings["cyclic_or_excessive_nesting"] += 1
                return []
            item = items.get(identifier)
            if not isinstance(item, dict):
                warnings["missing_items"] += 1
                return []
            data = item.get("data", {})
            if not isinstance(data, dict):
                warnings["malformed_items"] += 1
                return []
            title = item.get("title")
            title = title if isinstance(title, str) else ""
            tab = data.get("tab")
            if isinstance(tab, dict):
                url = next((tab.get(key) for key in ("url", "savedURL") if _valid_url(tab.get(key))), None)
                if url is None:
                    warnings["unsupported_or_missing_urls"] += 1
                    return []
                saved_title = tab.get("savedTitle")
                return [{"title": title or (saved_title if isinstance(saved_title, str) else "") or url, "url": url}]
            nested = []
            for child in children(identifier):
                nested.extend(walk(child, ancestors | {identifier}, depth + 1))
            if "itemContainer" in data:
                return nested
            if any(kind in data for kind in ("list", "folder", "splitView")):
                if "splitView" in data:
                    warnings["split_views_converted_to_folders"] += 1
                return [{"title": title or ("Vue partagée" if "splitView" in data else "Dossier"), "children": nested}]
            warnings["unknown_item_types"] += 1
            return nested

        for profile, identifier in _pairs(container.get("topAppsContainerIDs")):
            if _profile_key(profile) in aliases and isinstance(identifier, str):
                result["favorites"].extend(walk(identifier))
        for identifier, space in _records(container.get("spaces")):

            profile = _profile_key(space.get("profile", {"default": True}))
            if profile not in aliases:
                continue
            roots = {"pinned": [], "unpinned": []}
            for kind, item_id in _pairs(space.get("newContainerIDs")):
                if not isinstance(kind, dict) or not isinstance(item_id, str):
                    continue
                for category in roots:
                    if category in kind:
                        roots[category].append(item_id)
            for kind, item_id in _pairs(space.get("containerIDs")):
                if isinstance(kind, str) and kind in roots and not roots[kind] and isinstance(item_id, str):
                    roots[kind].append(item_id)
            custom = space.get("customInfo", {})
            icon_type = custom.get("iconType", {}) if isinstance(custom, dict) else {}
            icon = _space_icon(icon_type, warnings)
            entry = {
                "id": identifier,
                "title": space.get("title") if isinstance(space.get("title"), str) else "Espace Arc",
                "icon": icon,
                "bookmarks": [],
                "tabs": [],
            }
            for category, destination in (("pinned", "bookmarks"), ("unpinned", "tabs")):
                for root in dict.fromkeys(roots[category]):
                    entry[destination].extend(walk(root))
            result["spaces"].append(entry)
    result["warnings"] = [{"kind": key, "count": value} for key, value in sorted(warnings.items())]
    return result


def _extension_name(manifest, directory):
    name = manifest.get("name")
    if not isinstance(name, str):
        return directory.parent.name
    if name.startswith("__MSG_") and name.endswith("__"):
        key = name[6:-2].lower()
        locale = manifest.get("default_locale")
        if isinstance(locale, str) and Path(locale).name == locale:
            path = directory / "_locales" / locale / "messages.json"
            if path.is_file():
                try:
                    messages = {k.lower(): v for k, v in _json(path).items()}
                    message = messages.get(key, {}).get("message")
                    if isinstance(message, str):
                        return message
                except (OSError, ValueError, AttributeError):
                    pass
    return name


def extract_bookmarks(profile_path: Path) -> list[dict]:

    path = Path(profile_path) / "Bookmarks"
    if not path.is_file():
        return []
    roots = _json(path).get("roots")
    if not isinstance(roots, dict):
        raise ValueError("Format de favoris Chromium non reconnu.")

    def walk(node, depth=0):
        if depth > 100:
            raise ValueError("Imbrication excessive dans les favoris Chromium.")
        if not isinstance(node, dict):
            raise ValueError("Favori Chromium non reconnu.")
        title = node.get("name") if isinstance(node.get("name"), str) else ""
        if node.get("type") == "url":
            url = node.get("url")
            return {"title": title or url, "url": url} if _valid_url(url) else None
        if node.get("type") == "folder":
            children = node.get("children", [])
            if not isinstance(children, list):
                raise ValueError("Dossier de favoris Chromium non reconnu.")
            result = []
            for child in children:
                value = walk(child, depth + 1)
                if value is not None:
                    result.append(value)
            return {"title": title or "Favoris Chromium", "children": result}
        raise ValueError("Type de favori Chromium non reconnu.")

    result = []
    for root in roots.values():

        if isinstance(root, dict) and root.get("type") in ("folder", "url"):
            value = walk(root)
            if value is not None:
                result.append(value)
    return result


def list_extensions(profile_path: Path) -> list[dict]:

    root = Path(profile_path) / "Extensions"
    if not root.is_dir():
        return []
    result = []
    for extension in sorted(root.iterdir()):
        if not extension.is_dir() or extension.is_symlink():
            continue
        candidates = []
        for directory in extension.iterdir():
            manifest_path = directory / "manifest.json"
            if directory.is_symlink() or not manifest_path.is_file():
                continue
            try:
                manifest = _json(manifest_path)
                version = str(manifest.get("version", "0"))
                version_key = tuple(int(part) if part.isdigit() else 0 for part in version.split("."))
                candidates.append((version_key, directory, manifest))
            except (OSError, ValueError):
                continue
        if candidates:
            _, directory, manifest = max(candidates, key=lambda candidate: candidate[0])
            result.append({"id": extension.name, "name": _extension_name(manifest, directory), "version": str(manifest.get("version", ""))})
    return result


def _transition(value):


    numeric = int(value or 0)


    if numeric & 0xC0000000:
        return 6
    return {0: 1, 1: 2, 2: 3, 3: 4, 4: 8, 5: 2, 6: 1, 7: 1, 8: 9, 9: 2, 10: 1}.get(numeric & 0xFF, 1)


def iter_history(profile_path: Path):

    path = Path(profile_path) / "History"
    if not path.is_file():
        return
    with snapshot_db(path) as connection:
        query = """
            SELECT u.id, u.url, u.title, v.visit_time, v.transition
            FROM urls u JOIN visits v ON v.url = u.id
            ORDER BY u.id, v.visit_time, v.id
        """
        current_id = None
        entry = None
        for row in connection.execute(query):
            if row["id"] != current_id:
                if entry and entry["visits"]:
                    yield entry
                current_id = row["id"]
                entry = {"url": row["url"], "title": row["title"] or "", "visits": []} if _valid_url(row["url"], history=True) else None
            if entry is None:
                continue
            try:
                date = (int(row["visit_time"]) - CHROMIUM_EPOCH_US) // 1000
            except (ValueError, TypeError):
                continue
            if date >= 0:
                entry["visits"].append({"date": date, "transition": _transition(row["transition"])})
        if entry and entry["visits"]:
            yield entry


def _node_count(nodes):
    return sum(1 if "url" in node else _node_count(node.get("children", [])) for node in nodes)


def inventory(arc_root: Path) -> dict:

    result = {"profiles": [], "warnings": []}
    for profile in discover_profiles(arc_root):
        path = profile["path"]
        summary = {"id": profile["id"], "name": profile["name"], "warnings": []}
        databases = [
            ("History", [("history_urls", "SELECT COUNT(*) FROM urls"), ("history_visits", "SELECT COUNT(*) FROM visits")]),
            ("Login Data", [("passwords", "SELECT COUNT(*) FROM logins WHERE blacklisted_by_user = 0 AND length(password_value) > 0")]),
            ("Network/Cookies" if (path / "Network/Cookies").is_file() else "Cookies", [("cookies", "SELECT COUNT(*) FROM cookies")]),
        ]
        for filename, counts in databases:
            for key, _ in counts:
                summary[key] = 0
            if not (path / filename).is_file():
                continue
            try:
                with snapshot_db(path / filename) as connection:
                    for key, query in counts:
                        summary[key] = connection.execute(query).fetchone()[0]
            except (sqlite3.Error, OSError, RuntimeError):
                for key, _ in counts:
                    summary[key] = None
                summary["warnings"].append(f"Comptage indisponible : {filename}.")
        summary["extensions"] = len(list_extensions(path))
        try:
            summary["bookmarks"] = _node_count(extract_bookmarks(path))
        except (OSError, ValueError) as exc:
            summary["bookmarks"] = None
            summary["warnings"].append(str(exc))
        try:
            sidebar = extract_sidebar(arc_root, profile["id"])
            summary["spaces"] = len(sidebar["spaces"])
            summary["pinned_tabs"] = sum(_node_count(space["bookmarks"]) for space in sidebar["spaces"])
            summary["open_tabs"] = sum(_node_count(space["tabs"]) for space in sidebar["spaces"])
            summary["favorites"] = _node_count(sidebar["favorites"])
            summary["warnings"].extend(sidebar["warnings"])
        except (OSError, ValueError) as exc:
            summary["warnings"].append(str(exc))
            for key in ("spaces", "pinned_tabs", "open_tabs", "favorites"):
                summary[key] = None
        result["profiles"].append(summary)
    return result
