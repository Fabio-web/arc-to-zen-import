from __future__ import annotations

import configparser
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


CONFIG_FILES = ('profiles.ini', 'installs.ini')
LOCK_FILES = ('lock', '.parentlock', 'parent.lock', 'MarionetteActivePort')


def _config(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    if path.exists():
        try:
            parser.read_string(path.read_text(encoding='utf-8'))
        except (UnicodeError, configparser.Error):
            raise RuntimeError('Configuration des profils Zen invalide ; aucune modification effectuée.') from None
    return parser


def _profile_path(root, value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _section_path(root, section):
    value = section.get('Path', '')
    if not value:
        return None
    path = Path(value).expanduser()
    if section.get('IsRelative', '1') == '1':
        path = root / path
    return path.resolve()


def _matches_binary(profile, binary):
    compatibility = _config(profile / 'compatibility.ini')
    if not compatibility.has_section('Compatibility'):
        return False
    expected = (binary.resolve().parents[1] / 'Resources').resolve()
    value = compatibility['Compatibility'].get('LastPlatformDir', '')
    return bool(value) and Path(value).resolve() == expected


def _selection(root, binary):
    profiles = _config(root / 'profiles.ini')
    installs = _config(root / 'installs.ini')
    entries = {}
    for name in installs.sections():
        if installs[name].get('Default'):
            entries[name] = _profile_path(root, installs[name]['Default'])
    for name in profiles.sections():
        if name.startswith('Install') and profiles[name].get('Default'):
            entries[name[7:]] = _profile_path(root, profiles[name]['Default'])
    if entries:
        targets = set(entries.values())
        if len(targets) == 1:
            target = next(iter(targets))
        else:
            compatible = {path for path in targets if _matches_binary(path, binary)}
            if len(compatible) != 1:
                raise RuntimeError('Plusieurs installations Zen détectées : impossible de choisir leur profil par défaut sans ambiguïté.')
            target = next(iter(compatible))
        return target, [key for key, path in entries.items() if path == target], profiles, installs
    registered = [(name, _section_path(root, profiles[name])) for name in profiles.sections()
                  if name.startswith('Profile') and profiles[name].get('Path')]
    defaults = [path for name, path in registered if profiles[name].get('Default') == '1']
    if len(set(defaults)) == 1:
        return defaults[0], [], profiles, installs
    if len(registered) == 1:
        return registered[0][1], [], profiles, installs
    if registered:
        raise RuntimeError('Zen ne désigne aucun profil par défaut unique. Ouvrir Zen une fois puis le quitter.')


    target = root / 'Profiles/default'
    if target.exists():
        raise RuntimeError('Un dossier de profil Zen non enregistré existe déjà ; aucune donnée écrasée.')
    return target, [], profiles, installs


def resolve_default_profile(zen_root: Path, binary: Path) -> Path:

    root = Path(zen_root).expanduser().resolve()
    target = _selection(root, Path(binary))[0]
    if target == root or target in root.parents:
        raise RuntimeError('Le chemin du profil Zen est invalide.')
    return target


def zen_running(binary: Path) -> bool:
    result = subprocess.run(['/usr/bin/pgrep', '-x', Path(binary).name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode == 0:
        return True
    if result.returncode != 1:
        raise RuntimeError('Impossible de vérifier que Zen est fermé ; profil laissé intact.')
    return False


def ensure_zen_closed(binary: Path):
    if zen_running(binary):
        raise RuntimeError('Quitte Zen avec Cmd+Q, puis relance la migration.')


def _atomic_write(path, data):
    temporary = path.with_name(path.name + '.arc-import-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _serialize(parser):
    buffer = io.StringIO()

    parser.write(buffer, space_around_delimiters=False)
    return buffer.getvalue().encode('utf-8')


def install_profile(staged: Path, target: Path, zen_root: Path, backup_dir: Path,
                    binary: Path | None = None) -> dict:

    binary = Path(binary or '/Applications/Zen.app/Contents/MacOS/zen')
    root = Path(zen_root).expanduser().resolve()
    staged, target = Path(staged).resolve(), Path(target).resolve()
    backup_dir = Path(backup_dir).resolve()
    ensure_zen_closed(binary)
    chosen, install_ids, profiles, installs = _selection(root, binary)
    if chosen != target or target == root or target in root.parents:
        raise RuntimeError('Le profil Zen par défaut a changé ; remplacement annulé.')
    if target == staged or target in staged.parents or staged in target.parents:
        raise RuntimeError('La préparation et le profil Zen doivent être des dossiers distincts.')
    if target == backup_dir or target in backup_dir.parents or backup_dir in target.parents:
        raise RuntimeError('La sauvegarde doit être extérieure au profil Zen remplacé.')
    for filename in ('prefs.js', 'places.sqlite', 'cookies.sqlite', 'key4.db', 'compatibility.ini'):
        if not (staged / filename).is_file():
            raise RuntimeError('Le profil importé est incomplet ; profil Zen laissé intact.')
    original = {name: (root / name).read_bytes() if (root / name).exists() else None
                for name in CONFIG_FILES}
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, content in original.items():
        if content is not None:
            _atomic_write(backup_dir / name, content)
    existed = target.exists()
    if existed:
        shutil.copytree(target, backup_dir / 'profil-zen-original', symlinks=True)
    _atomic_write(backup_dir / 'restauration.json', json.dumps({
        'profile_path': str(target), 'original_profile_existed': existed,
        'original_configuration_files': [name for name, content in original.items() if content is not None],
    }, ensure_ascii=False, indent=2).encode('utf-8'))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    incoming = target.with_name('.arc-import-incoming-' + nonce)
    previous = target.with_name('.arc-import-previous-' + nonce)
    matching = [name for name in profiles.sections() if name.startswith('Profile')
                and _section_path(root, profiles[name]) == target]
    if matching:
        profile_section = matching[0]
    else:
        index = 0
        while profiles.has_section('Profile' + str(index)):
            index += 1
        profile_section = 'Profile' + str(index)
        try:
            path_value, relative = str(target.relative_to(root)), '1'
        except ValueError:
            path_value, relative = str(target), '0'
        profiles[profile_section] = {'Name': 'Default (release)', 'IsRelative': relative, 'Path': path_value}
    for name in profiles.sections():
        if name.startswith('Profile'):
            profiles[name].pop('Default', None)
    profiles[profile_section]['Default'] = '1'
    if not profiles.has_section('General'):
        profiles.add_section('General')
    profiles['General']['StartWithLastProfile'] = '1'
    profiles['General']['Version'] = '2'
    path_value = profiles[profile_section]['Path']
    for install_id in install_ids:
        section = 'Install' + install_id
        if not profiles.has_section(section):
            profiles.add_section(section)
        profiles[section]['Default'] = path_value
        profiles[section]['Locked'] = '1'
        if not installs.has_section(install_id):
            installs.add_section(install_id)
        installs[install_id]['Default'] = path_value
        installs[install_id]['Locked'] = '1'
    moved_old = moved_new = False
    try:
        shutil.copytree(staged, incoming, symlinks=True, ignore=shutil.ignore_patterns(*LOCK_FILES))
        incoming.chmod(0o700)
        ensure_zen_closed(binary)
        for name, content in original.items():
            current = (root / name).read_bytes() if (root / name).exists() else None
            if current != content:
                raise RuntimeError('La configuration Zen a changé pendant la préparation ; remplacement annulé.')
        if existed:
            os.replace(target, previous)
            moved_old = True
        os.replace(incoming, target)
        moved_new = True
        _atomic_write(root / 'installs.ini', _serialize(installs))
        _atomic_write(root / 'profiles.ini', _serialize(profiles))
    except BaseException:
        rollback_errors = []
        try:
            if moved_new:
                shutil.rmtree(target)
            if moved_old:
                os.replace(previous, target)
            if moved_old or moved_new:
                for name, content in original.items():
                    if content is None:
                        (root / name).unlink(missing_ok=True)
                    else:
                        _atomic_write(root / name, content)
        except BaseException as error:
            rollback_errors.append(type(error).__name__)
        if rollback_errors:
            raise RuntimeError('Installation interrompue ; restauration automatique incomplète. La sauvegarde Zen reste disponible.') from None
        raise
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)
    if previous.exists():
        shutil.rmtree(previous)
    return {'profile': str(target), 'backup': str(backup_dir),
            'replaced_existing': existed, 'installation_ids': install_ids,
            'registered_profile_count': sum(name.startswith('Profile') for name in profiles.sections())}
