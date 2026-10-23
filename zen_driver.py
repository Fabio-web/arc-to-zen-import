from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time


ZEN = Path('/Applications/Zen.app/Contents/MacOS/zen')


class ZenError(RuntimeError):
    pass


class Zen:
    def __init__(self, profile: Path, binary: Path = ZEN):
        self.profile, self.binary = profile, binary
        self.process = self.sock = None
        self.counter = 0

    def __enter__(self):
        self.profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.profile, 0o700)
        prefs = {
            'marionette.port': 0,
            'marionette.log.level': 'Fatal',
            'browser.shell.checkDefaultBrowser': False,
            'browser.startup.homepage': 'about:blank',
            'browser.startup.page': 3,
            'browser.startup.firstrunSkipsHomepage': True,
            'browser.aboutwelcome.enabled': False,
            'browser.sessionstore.restore_on_demand': True,
            'browser.sessionstore.restore_pinned_tabs_on_demand': True,
            'browser.sessionstore.privacy_level': 0,
            'browser.sessionstore.max_tabs_undo': 0,
            'browser.tabs.warnOnClose': False,
            'zen.welcome-screen.seen': True,
            'zen.workspaces.force-container-workspace': False,
            'datareporting.policy.dataSubmissionEnabled': False,
            'datareporting.healthreport.uploadEnabled': False,
            'toolkit.telemetry.enabled': False,
            'app.update.auto': False,
            'extensions.update.enabled': False,
            'network.prefetch-next': False,
            'network.dns.disablePrefetch': True,

            'network.proxy.type': 1,
            'network.proxy.http': '127.0.0.1',
            'network.proxy.http_port': 9,
            'network.proxy.ssl': '127.0.0.1',
            'network.proxy.ssl_port': 9,
            'network.proxy.no_proxies_on': '',
        }
        (self.profile / 'user.js').write_text(''.join(
            f'user_pref({json.dumps(k)}, {json.dumps(v)});\n' for k, v in prefs.items()
        ))
        env = dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS='1', MOZ_HEADLESS='1')
        self.process = subprocess.Popen(
            [str(self.binary), '--headless', '--no-remote', '--profile', str(self.profile),
             '--marionette', 'about:blank'], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ZenError('Zen a quitté avant de démarrer le profil de migration.')
                pref = self.profile / 'prefs.js'
                match = re.search(r'user_pref\("marionette.port", (\d+)\)',
                                  pref.read_text(errors='replace') if pref.exists() else '')
                if match and int(match[1]):
                    try:
                        self.sock = socket.create_connection(('127.0.0.1', int(match[1])), 1)
                        self.sock.settimeout(180)
                        break
                    except OSError:
                        pass
                time.sleep(.15)
            if self.sock is None:
                raise ZenError('Le démarrage du profil Zen a dépassé 45 secondes.')
            greeting = self.receive()
            if greeting.get('marionetteProtocol') != 3:
                raise ZenError('Version Marionette non prise en charge.')
            self.command('WebDriver:NewSession', {'capabilities': {'alwaysMatch': {
                'acceptInsecureCerts': False, 'timeouts': {'script': 180000}}}})
            self.command('Marionette:SetContext', {'value': 'chrome'})
            self.js('Services.io.offline = true; return Services.appinfo.version;')
            return self
        except BaseException:
            self.close()
            raise

    def receive(self):
        size = bytearray()
        while True:
            ch = self.sock.recv(1)
            if not ch:
                raise ZenError('Connexion locale à Zen interrompue.')
            if ch == b':':
                break
            size.extend(ch)
            if len(size) > 10 or not ch.isdigit():
                raise ZenError('Réponse Marionette invalide.')
        length = int(size)
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(min(length - len(data), 65536))
            if not chunk:
                raise ZenError('Réponse Marionette incomplète.')
            data.extend(chunk)
        return json.loads(data)

    def command(self, name, params):
        self.counter += 1
        raw = json.dumps([0, self.counter, name, params], ensure_ascii=False).encode()
        self.sock.sendall(str(len(raw)).encode() + b':' + raw)
        response = self.receive()
        if response[:2] != [1, self.counter]:
            raise ZenError('Identifiant de réponse Marionette incorrect.')
        if response[2]:

            raise ZenError('Zen a refusé une opération (' + response[2]['error'] + ').')
        return response[3]

    def js(self, body, *args):
        wrapped = """
const done = arguments[arguments.length - 1];
const args = Array.from(arguments).slice(0, -1);
(async () => { BODY })().then(
  result => done({ok: true, result}),
  error => done({ok: false, kind: error.name || 'Error'})
);
""".replace('BODY', body)
        reply = self.command('WebDriver:ExecuteAsyncScript', {
            'script': wrapped, 'args': list(args), 'sandbox': 'system',
            'newSandbox': True, 'scriptTimeout': 180000,
        })['value']
        if not reply.get('ok'):
            raise ZenError('Échec de l’import Zen (' + reply.get('kind', 'Error') + ').')
        return reply.get('result')

    def close(self):
        clean = True
        if self.sock:
            try:
                self.command('Marionette:Quit', {'flags': ['eAttemptQuit']})
            except (OSError, ZenError):
                pass
            self.sock.close()
            self.sock = None
        if self.process:
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                clean = False
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            clean = clean and self.process.returncode == 0
            self.process = None
        prepare_for_normal_launch(self.profile)
        return clean

    def __exit__(self, *exc):
        clean = self.close()
        if not clean and exc[0] is None:
            raise ZenError('Zen ne s’est pas fermé correctement ; la conservation des données reste à vérifier.')


def prepare_for_normal_launch(profile: Path):

    path = profile / 'prefs.js'
    if path.exists():
        lines = path.read_text().splitlines()
        lines = [line for line in lines if not any(
            line.startswith(f'user_pref("{prefix}') for prefix in (
                'marionette.', 'network.proxy.', 'network.online', 'browser.offline',
                'app.update.auto', 'extensions.update.enabled',
            ))]
        path.write_text('\n'.join(lines) + '\n')
    (profile / 'user.js').unlink(missing_ok=True)
