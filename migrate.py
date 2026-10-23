#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote

from arc_reader import (discover_profiles, extract_bookmarks, extract_sidebar,
                        inventory, iter_history, list_extensions)
from secrets_macos import get_arc_key, read_cookies, read_logins
from favicon_reader import read_favicons
from zen_driver import Zen, ZEN, prepare_for_normal_launch
from zen_profile import resolve_default_profile, install_profile, zen_running

ROOT = Path(__file__).resolve().parent
ARC = Path.home() / 'Library/Application Support/Arc'
DATA_SCRIPT = (ROOT / 'import_data.js').read_text()
LAYOUT_SCRIPT = (ROOT / 'import_layout.js').read_text()
VERIFY_LAYOUT_SCRIPT = (ROOT / 'verify_layout.js').read_text()
FAVICON_SCRIPT = (ROOT / 'import_favicons.js').read_text()
REORDER_SCRIPT = (ROOT / 'reorder_layout.js').read_text()
LIMITATIONS = [
    'Certaines connexions exigent une nouvelle authentification, même avec les cookies.',
    'Netflix : cookies non importés ; reconnexion manuelle nécessaire. La lecture DRM reste soumise aux limites de Zen sur macOS.',
    'localStorage, IndexedDB, service workers et données internes des extensions ne sont pas convertis.',
    'Extensions Chromium à réinstaller dans leur version Firefox ; liste fournie.',
    'Passkeys, cartes bancaires, formulaires, permissions, réglages Arc, Boosts et Easels non importés.',
    'Les liens des vues partagées sont conservés ; leur disposition doit être recréée.',
    'Les téléchargements présents sur le disque restent à leur emplacement ; leur liste Arc n’est pas importée.',
    'Les onglets archivés et la navigation précédent/suivant de chaque onglet ne sont pas importés.',
]


def private_json(path, data):
    with path.open('x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)


def arc_running():
    return subprocess.run(['/usr/bin/pgrep', '-x', 'Arc'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def print_inventory(audit):
    for p in audit['profiles']:
        if p['name'] == '__ARC_SYSTEM_PROFILE':
            continue
        print(f"{p['id']} : {p['spaces']} espaces, {p['pinned_tabs']} épinglés, "
              f"{p['open_tabs']} onglets ouverts, {p['favorites']} favoris.")
        print(f"  {p.get('bookmarks', 0)} marque-pages, {p['history_visits']} visites, "
              f"{p['passwords']} mots de passe, {p['cookies']} cookies, {p['extensions']} extensions.")
        if p.get('warnings'):
            print('  Informations : ' + json.dumps(p['warnings'], ensure_ascii=False))


def backup_arc(arc_root, destination, profiles):

    if arc_running():
        raise RuntimeError('Quitte Arc avec Cmd+Q, puis relance la migration.')
    destination.mkdir(mode=0o700)
    names = ['StorableSidebar.json', 'StorableProfiles.json', 'User Data/Local State']
    for profile in profiles:
        for name in ['History', 'Login Data', 'Cookies', 'Network/Cookies', 'Favicons',
                     'Bookmarks', 'Preferences', 'Secure Preferences']:
            names.append(f'User Data/{profile["id"]}/{name}')
    for name in names:
        source = arc_root / name
        if not source.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
        wal = Path(str(source) + '-wal')
        if wal.exists():
            shutil.copyfile(wal, str(target) + '-wal')
            os.chmod(str(target) + '-wal', 0o600)
    if arc_running():
        raise RuntimeError('Arc a été rouvert pendant la sauvegarde. Relance après avoir quitté Arc.')


def write_extensions(path, extensions):
    items = []
    for extension in extensions:
        name = extension.get('name', extension['id'])
        link = 'https://addons.mozilla.org/fr/firefox/search/?q=' + quote(name)
        items.append('<li><a href="' + html.escape(link, quote=True) + '">' +
                     html.escape(name) + '</a> — ' + html.escape(extension.get('version', '')) + '</li>')
    path.write_text('<!doctype html><meta charset="utf-8"><title>Extensions Arc</title>'
                    '<h1>Extensions à réinstaller dans Zen</h1>'
                    '<p>Chaque lien recherche une version Firefox. Vérifier son éditeur avant installation.</p>'
                    '<ul>' + ''.join(items) + '</ul>', encoding='utf-8')
    os.chmod(path, 0o600)


def history_batches(profile):
    batch, size = [], 0
    for page in iter_history(profile):

        visits = page['visits']
        for start in range(0, len(visits), 2000):
            part = dict(page, visits=visits[start:start + 2000])
            batch.append(part)
            size += len(part['visits'])
            if size >= 2000 or len(batch) >= 500:
                yield batch
                batch, size = [], 0
    if batch:
        yield batch


def sidebar_urls(sidebar):
    def walk(nodes):
        for node in nodes:
            if 'url' in node:
                yield node['url']
            yield from walk(node.get('children', []))
    urls = set(walk(sidebar['favorites']))
    urls.update(walk(sidebar.get('extraBookmarks', [])))
    for space in sidebar['spaces']:
        urls.update(walk(space['bookmarks']))
        urls.update(walk(space['tabs']))
    return urls


def migrate_one(source, destination, profile_id, key, extensions, skip_history, binary=ZEN):
    profile = destination / 'profil-zen'
    sidebar = extract_sidebar(source, profile_id)
    source_profile = source / 'User Data' / profile_id
    sidebar['extraBookmarks'] = extract_bookmarks(source_profile)
    favicons = read_favicons(source_profile, sidebar_urls(sidebar))
    result = {'source_profile': profile_id, 'limitations': LIMITATIONS,
              'source_warnings': sidebar.get('warnings', []), 'status': 'in_progress'}
    destination.mkdir(mode=0o700)
    cookies = read_cookies(source_profile, key)
    logins = read_logins(source_profile, key)
    result['cookies_skipped'] = cookies['skipped']
    result['logins_skipped'] = logins['skipped']
    result['eligible'] = {'cookies': len(cookies['cookies']), 'logins': len(logins['logins'])}
    with Zen(profile, binary=binary) as zen:
        print('  Création des espaces, dossiers et onglets suspendus…', flush=True)
        result['layout'] = zen.js(LAYOUT_SCRIPT, sidebar)
        result['tab_order'] = zen.js(REORDER_SCRIPT, sidebar)
        if any(result['tab_order'].get(field, 0) for field in ('missing_spaces', 'missing_folders', 'missing_tabs')):
            raise RuntimeError('L’ordre de certains éléments Arc n’a pas pu être rétabli ; Zen reste inchangé.')
        print('  Récupération des favicons du cache Arc…', flush=True)
        result['favicons'] = {'source': favicons['stats'], 'import': zen.js(FAVICON_SCRIPT, favicons['icons'])}
        print('  Import des cookies et mots de passe…', flush=True)
        result['cookies'] = zen.js(DATA_SCRIPT, 'cookies', cookies['cookies'])
        result['logins'] = zen.js(DATA_SCRIPT, 'logins', logins['logins'])
        cookies['cookies'].clear()
        logins['logins'].clear()
        history = {'submitted_visits': 0, 'imported_visits': 0, 'rejected': 0,
                   'skipped_by_request': skip_history}
        if not skip_history:
            print('  Import de tout l’historique compatible…', flush=True)
            last_progress = 0
            for batch in history_batches(source_profile):
                imported = zen.js(DATA_SCRIPT, 'history', batch)
                history['submitted_visits'] += imported['visits']
                history['imported_visits'] += imported['imported_visits']
                history['rejected'] += imported['rejected']
                if history['submitted_visits'] - last_progress >= 20000:
                    last_progress = history['submitted_visits']
                    print(f"    {last_progress:,} visites traitées".replace(',', ' '), flush=True)
        result['history'] = history
        result['before_restart'] = zen.js(DATA_SCRIPT, 'verify', None)
        result['layout_before_restart'] = zen.js(VERIFY_LAYOUT_SCRIPT)
    print('  Vérification des données préparées…', flush=True)
    with Zen(profile, binary=binary) as zen:
        result['after_restart'] = zen.js(DATA_SCRIPT, 'verify', None)
        result['layout_after_restart'] = zen.js(VERIFY_LAYOUT_SCRIPT)
        result['favicons']['after_restart'] = zen.js(FAVICON_SCRIPT, favicons['icons'], 'verify')

    prepare_for_normal_launch(profile)
    before, after = result['before_restart'], result['after_restart']
    checked = ('cookies', 'session_cookies', 'logins', 'history_visits', 'history_urls', 'spaces')
    mismatches = [key for key in checked if before[key] != after[key]]
    if result['favicons']['after_restart']['tab_images_missing']:
        mismatches.append('favicon_images')
    if result['favicons']['after_restart']['pages_cached'] != result['favicons']['import']['pages_cached']:
        mismatches.append('favicon_cache')
    first_layout, restored_layout = result['layout_before_restart'], result['layout_after_restart']
    if first_layout['fingerprints'] != restored_layout['fingerprints']:
        mismatches.append('layout_fingerprints')
    for field in ('folders', 'pinned_tabs', 'open_tabs', 'favorites', 'bookmarks'):
        if restored_layout['counts'].get(field) != result['layout'][field]:
            mismatches.append('layout_' + field)
    for field in ('workspace_mismatches', 'detached_from_store'):
        if restored_layout['counts'][field]:
            mismatches.append(field)
    result['restart_mismatches'] = mismatches
    exclusions = {**{'cookies_' + k: v for k, v in cookies['skipped'].items()
                      if k not in ('expired_cookie', 'netflix_session_requires_signin')},
                  **{'logins_' + k: v for k, v in logins['skipped'].items()
                      if k != 'blocked_login'},
                  **{'layout_' + k: v for k, v in result['layout']['skipped'].items()}}
    for label, expected, actual in [
        ('cookies_not_stored', result['eligible']['cookies'], before['cookies']),
        ('logins_not_stored', result['eligible']['logins'] - result['logins'].get('no_change', 0), before['logins']),
        ('history_not_stored', history['submitted_visits'], before['history_visits']),
    ]:
        if expected != actual:
            exclusions[label] = abs(expected - actual)
    for label, count in result['logins'].items():
        if label not in ('added', 'modified', 'no_change', 'stored'):
            exclusions['logins_result_' + label] = count
    if result['cookies']['rejected']:
        exclusions['cookies_rejected'] = result['cookies']['rejected']
    if history['rejected']:
        exclusions['history_rejected'] = history['rejected']
    result['additional_exclusions'] = exclusions
    result['status'] = ('verification_failed' if mismatches else
                        'partial' if exclusions or skip_history else 'ready_with_limitations')
    private_json(destination / 'rapport.json', result)
    write_extensions(destination / 'extensions.html', extensions)
    if mismatches:
        raise RuntimeError('La vérification après redémarrage a échoué. Voir rapport.json ; Arc est intact.')
    label = 'Import partiel' if result['status'] == 'partial' else 'Prêt'
    print(f'  {label} pour le remplacement du profil Zen par défaut.', flush=True)
    print(f"  Conservés : {after['cookies']} cookies, {after['logins']} mots de passe, "
          f"{after['history_visits']} visites.", flush=True)
    if exclusions:
        print('  Exclusions supplémentaires : ' + json.dumps(exclusions, ensure_ascii=False))
    print('  Les exclusions et limites figurent dans rapport.json.', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description="Transférer Arc dans le profil Zen par défaut.")
    parser.add_argument('--apply', action='store_true', help='Remplacer le profil Zen par défaut ; Arc et Zen doivent être fermés.')
    parser.add_argument('--arc-root', type=Path, default=ARC)
    parser.add_argument('--output', type=Path, help='Nouveau dossier destination, qui ne doit pas déjà exister.')
    parser.add_argument('--profile', help='Profil Arc à importer (Default si présent).')
    parser.add_argument('--zen-app', type=Path, help='Chemin de Zen.app si installé ailleurs.')
    parser.add_argument('--skip-history', action='store_true', help='Omettre explicitement l’historique.')
    args = parser.parse_args()
    os.umask(0o077)
    if not args.apply:
        print_inventory(inventory(args.arc_root))
        print('Audit terminé. Aucun import et aucun accès au trousseau.')
        return 0
    candidates = ([args.zen_app.expanduser() / 'Contents/MacOS/zen'] if args.zen_app else
                  [ZEN, Path.home() / 'Applications/Zen.app/Contents/MacOS/zen'])
    binary = next((candidate for candidate in candidates if candidate.is_file()), None)
    if binary is None:
        raise RuntimeError('Installe Zen dans Applications avant de lancer la migration.')
    version = plistlib.loads((binary.parents[1] / 'Info.plist').read_bytes()).get('CFBundleShortVersionString')
    if version != '1.23.4b':
        raise RuntimeError('Cet outil a été validé avec Zen 1.23.4b. Adapter et revalider avant une autre version.')
    if arc_running():
        raise RuntimeError('Quitte Arc avec Cmd+Q, puis relance Migrer.command.')
    if zen_running(binary):
        raise RuntimeError('Quitte Zen avec Cmd+Q, puis relance Migrer.command.')
    profiles = [p for p in discover_profiles(args.arc_root) if p['name'] != '__ARC_SYSTEM_PROFILE']
    if args.profile:
        known = {p['id'] for p in profiles}
        if args.profile not in known:
            raise RuntimeError('Profil Arc demandé introuvable.')
        profiles = [p for p in profiles if p['id'] == args.profile]
    if not profiles:
        raise RuntimeError('Aucun profil Arc à transférer.')
    selected = next((p for p in profiles if p['id'] == 'Default'), profiles[0])
    if len(profiles) > 1:
        print(f"Plusieurs profils Arc détectés : import de {selected['id']}. Pour un autre, utiliser --profile.", flush=True)
    zen_root = Path.home() / 'Library/Application Support/zen'
    target = resolve_default_profile(zen_root, binary)
    destination = args.output or (Path.home() / 'Library/Application Support/Arc to Zen/Transferts' /
                                  datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    destination = destination.expanduser().resolve()
    for protected in [args.arc_root.resolve(), zen_root, target]:
        same_directory = protected.exists() and any(
            parent.exists() and parent.samefile(protected)
            for parent in (destination, *destination.parents)
        )
        if same_directory or destination == protected or protected in destination.parents:
            raise RuntimeError('Le dossier destination doit être extérieur aux profils Arc et Zen existants.')
    if destination.exists():
        raise RuntimeError('Le dossier destination existe déjà. Choisir un nouveau dossier.')
    destination.mkdir(parents=True, mode=0o700)

    extensions = list_extensions(selected['path'])
    backup = destination / 'sauvegarde-arc'
    print('Sauvegarde privée des fichiers Arc utilisés…', flush=True)
    backup_arc(args.arc_root, backup, [selected])
    print('Accès local au trousseau « Arc Safe Storage » ; macOS peut demander ton autorisation.', flush=True)
    key = get_arc_key()
    try:
        print(f"Import du profil Arc {selected['id']} dans le profil Zen par défaut.", flush=True)
        with tempfile.TemporaryDirectory(prefix='preparation-', dir=destination) as temporary:
            preparation = Path(temporary) / 'import'
            result = migrate_one(backup, preparation, selected['id'], key,
                                 extensions, args.skip_history, binary=binary)
            print('Sauvegarde et remplacement du profil Zen par défaut…', flush=True)
            installation = install_profile(preparation / 'profil-zen', target, zen_root,
                                           destination / 'sauvegarde-zen', binary=binary)
            result['installation'] = installation
            private_json(destination / 'rapport.json', result)
            write_extensions(destination / 'extensions.html', extensions)
    finally:
        del key
    print('Terminé : ouvre Zen normalement depuis Applications ou le Dock.', flush=True)
    print(f'Sauvegardes et bilan : {destination}', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nInterrompu. Arc est intact ; consulte la sauvegarde Zen si le remplacement avait commencé.', file=sys.stderr)
        sys.exit(130)
    except Exception as error:

        safe = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print(f'Échec : {safe}', file=sys.stderr)
        print('Arc est intact. Le remplacement de Zen est sauvegardé et restauré automatiquement en cas d’échec.', file=sys.stderr)
        sys.exit(1)
