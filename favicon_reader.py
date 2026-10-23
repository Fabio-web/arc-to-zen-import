from __future__ import annotations

import base64
from collections import Counter, defaultdict
from pathlib import Path
import sqlite3
import struct
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from arc_reader import snapshot_db


MAX_ICON_BYTES = 128 * 1024


def _url_key(value):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        scheme = parsed.scheme.lower()
        host = parsed.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        port = parsed.port
        authority = host + (':' + str(port) if port and port != (443 if scheme == 'https' else 80) else '')
        origin = scheme + '://' + authority
        return urlunsplit((scheme, authority, parsed.path or '/', parsed.query, '')), origin
    except (ValueError, UnicodeError):
        return None


def _text(value):
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8')
        except UnicodeDecodeError:
            return None
    return value if isinstance(value, str) else None


def _image_type(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and data[12:16] == b'IHDR':
        width, height = struct.unpack('>II', data[16:24])
        return ('image/png', width, height) if 0 < width <= 1024 and 0 < height <= 1024 else None
    if data.startswith(b'\xff\xd8\xff') and len(data) >= 4:
        return 'image/jpeg', None, None
    if data.startswith(b'\x00\x00\x01\x00') and len(data) >= 22:
        count = int.from_bytes(data[4:6], 'little')
        if 0 < count <= 64 and len(data) >= 6 + count * 16:
            return 'image/x-icon', None, None
    return None


def _chunks(values, size=400):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def read_favicons(profile_path: Path, wanted_urls: Iterable[str]) -> dict:

    wanted = {}
    invalid = 0
    for value in wanted_urls:
        key = _url_key(value)
        if key is None:
            invalid += 1
        else:
            wanted[value] = key
    stats = {'requested': len(wanted), 'invalid_urls': invalid, 'exact': 0,
             'normalized': 0, 'same_origin': 0, 'missing': len(wanted),
             'invalid_bitmaps': 0, 'oversized_bitmaps': 0}
    result = {'icons': {}, 'stats': stats}
    path = Path(profile_path) / 'Favicons'
    if not path.is_file():
        stats['database_missing'] = 1
        return result
    if not wanted:
        return result
    keys = {key for key, _ in wanted.values()}
    origins = {origin for _, origin in wanted.values()}
    exact_ids, normalized_ids = defaultdict(set), defaultdict(set)
    origin_counts = defaultdict(Counter)
    bitmaps = {}
    try:
        with snapshot_db(path) as db:

            for row in db.execute('SELECT CAST(page_url AS BLOB) AS page_url, icon_id FROM icon_mapping'):
                page_url = _text(row['page_url'])
                key = _url_key(page_url)
                if key is None or key[1] not in origins:
                    continue
                icon_id = row['icon_id']
                origin_counts[key[1]][icon_id] += 1
                if page_url in wanted:
                    exact_ids[page_url].add(icon_id)
                if key[0] in keys:
                    normalized_ids[key[0]].add(icon_id)
            fallback_ids = {origin: [icon for icon, _ in counts.most_common(4)]
                            for origin, counts in origin_counts.items()}
            useful_ids = set()
            for values in list(exact_ids.values()) + list(normalized_ids.values()) + list(fallback_ids.values()):
                useful_ids.update(values)
            for batch in _chunks(sorted(useful_ids)):
                placeholders = ','.join('?' for _ in batch)
                query = ('SELECT icon_id, CAST(image_data AS BLOB) AS image_data, width, height, last_updated '
                         f'FROM favicon_bitmaps WHERE icon_id IN ({placeholders})')
                for row in db.execute(query, batch):
                    data = row['image_data']
                    if not isinstance(data, bytes) or not data:
                        stats['invalid_bitmaps'] += 1
                        continue
                    if len(data) > MAX_ICON_BYTES:
                        stats['oversized_bitmaps'] += 1
                        continue
                    image = _image_type(data)
                    if image is None:
                        stats['invalid_bitmaps'] += 1
                        continue
                    mime, actual_width, actual_height = image
                    width, height = actual_width or row['width'], actual_height or row['height']
                    if not isinstance(width, int) or not isinstance(height, int) or not (0 < width <= 1024 and 0 < height <= 1024):
                        stats['invalid_bitmaps'] += 1
                        continue
                    edge = min(width, height)
                    score = (edge >= 32, edge >= 64, -abs(edge - 64), row['last_updated'] or 0)
                    previous = bitmaps.get(row['icon_id'])
                    if previous is None or score > previous[0]:
                        bitmaps[row['icon_id']] = (score, mime, data)
    except (sqlite3.Error, OSError, RuntimeError):

        stats['database_unavailable'] = 1
        return result
    encoded = {}
    for page_url, (normalized, origin) in wanted.items():
        chosen, source = None, None
        for source, ids in [('exact', exact_ids.get(page_url, ())),
                            ('normalized', normalized_ids.get(normalized, ())),
                            ('same_origin', fallback_ids.get(origin, ()))]:
            available = [icon for icon in ids if icon in bitmaps]
            if not available:
                continue
            if source == 'same_origin':
                chosen = available[0]
            else:
                chosen = max(available, key=lambda icon: bitmaps[icon][0])
            break
        if chosen is None:
            continue
        if chosen not in encoded:
            _, mime, data = bitmaps[chosen]
            encoded[chosen] = 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')
        result['icons'][page_url] = encoded[chosen]
        stats[source] += 1
    stats['missing'] = len(wanted) - len(result['icons'])
    return result
