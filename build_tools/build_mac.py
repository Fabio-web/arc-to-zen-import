#!/usr/bin/env python3
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import platform
from pathlib import Path
import subprocess
import sys
import textwrap
import zipfile

import PyInstaller


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.build'
DIST = ROOT / 'dist'
NOTICES = ROOT / 'build_tools/THIRD_PARTY_NOTICES.txt'
DATA_FILES = (
    'import_data.js',
    'import_layout.js',
    'verify_layout.js',
    'reorder_layout.js',
    'import_favicons.js',
)
SOURCE_FILES = (
    'migrate.py',
    'arc_reader.py',
    'favicon_reader.py',
    'secrets_macos.py',
    'zen_driver.py',
    'zen_profile.py',
    *DATA_FILES,
    'build_tools/THIRD_PARTY_NOTICES.txt',
)


def main():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Construire ce fichier sur un Mac Apple Silicon.')
    DIST.mkdir(exist_ok=True)
    command = [
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--onefile', '--console', '--target-architecture', 'arm64',
        '--name', 'arc-to-zen', '--log-level', 'WARN',
        '--distpath', str(BUILD / 'binary'),
        '--workpath', str(BUILD / 'pyinstaller'),
        '--specpath', str(BUILD),
        '--paths', str(ROOT),
    ]
    for name in DATA_FILES:
        command.extend(['--add-data', str(ROOT / name) + ':.'])
    command.extend(['--add-data', str(NOTICES) + ':.'])
    subprocess.run([*command, str(ROOT / 'migrate.py')], cwd=ROOT, check=True)
    executable = BUILD / 'binary/arc-to-zen'
    content = executable.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    payload = base64.b64encode(gzip.compress(content, mtime=0)).decode('ascii')
    header = '''#!/bin/zsh
set -u
set -o pipefail
umask 077

if [[ "$(/usr/bin/uname -s)" != "Darwin" || ( "$(/usr/bin/uname -m)" != "arm64" && "$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" != "1" ) ]]; then
  echo 'Ce fichier nécessite un Mac Apple Silicon (M1 ou plus récent).'
  exit 1
fi

arc_zen_tmp=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/arc-to-zen.XXXXXXXX") || exit 1
trap '/bin/rm -rf -- "$arc_zen_tmp"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
arc_zen_line=$(/usr/bin/awk '/^__ARC_ZEN_PAYLOAD__$/ {print NR + 1; exit}' "$0")
if [[ -z "$arc_zen_line" ]]; then
  echo 'Fichier incomplet. Télécharge-le de nouveau.'
  exit 1
fi
if ! /usr/bin/tail -n +"$arc_zen_line" "$0" | /usr/bin/base64 -D | /usr/bin/gzip -dc > "$arc_zen_tmp/arc-to-zen"; then
  echo 'Impossible de préparer le programme. Télécharge-le de nouveau.'
  exit 1
fi
arc_zen_hash=$(/usr/bin/shasum -a 256 "$arc_zen_tmp/arc-to-zen")
if [[ "${arc_zen_hash%% *}" != "__BINARY_SHA256__" ]]; then
  echo 'Le fichier est endommagé. Télécharge-le de nouveau.'
  exit 1
fi
/bin/chmod 700 "$arc_zen_tmp/arc-to-zen" || exit 1

if (( $# == 0 )); then
  echo 'Arc → Zen — remplacement du profil Zen par défaut, avec sauvegarde.'
  echo 'Arc et Zen doivent être complètement fermés.'
  "$arc_zen_tmp/arc-to-zen" --apply
  arc_zen_result=$?
  if [[ -t 0 ]]; then
    echo
    read -r '?Appuie sur Entrée pour fermer cette fenêtre.'
  fi
elif [[ "$1" == '--audit' ]]; then
  shift
  "$arc_zen_tmp/arc-to-zen" "$@"
  arc_zen_result=$?
else
  "$arc_zen_tmp/arc-to-zen" "$@"
  arc_zen_result=$?
fi
exit "$arc_zen_result"
__ARC_ZEN_PAYLOAD__
'''.replace('__BINARY_SHA256__', digest)
    output = DIST / 'Arc-vers-Zen.command'
    output.write_text(header + '\n'.join(textwrap.wrap(payload, 76)) + '\n', encoding='utf-8')
    output.chmod(0o755)
    archive = DIST / 'Arc-vers-Zen-macOS.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(output, output.name)
        bundle.write(NOTICES, NOTICES.name)
    manifest = {
        'architecture': 'arm64',
        'python': platform.python_version(),
        'pyinstaller': PyInstaller.__version__,
        'binary_sha256': digest,
        'file_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'file_bytes': output.stat().st_size,
        'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
        'archive_bytes': archive.stat().st_size,
        'sources': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES},
    }
    (DIST / 'build-info.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'{output.name} : {output.stat().st_size:,} octets, Apple Silicon, Python embarqué.')


if __name__ == '__main__':
    main()
