# Arc → Zen

Import Arc into your **default Zen profile** on macOS. Spaces, tabs, favorites, history, passwords, and compatible cookies are transferred locally.

This tool is built for **[Zen Browser — official repository: zen-browser/desktop](https://github.com/zen-browser/desktop)** on macOS.

**Independent, unofficial project.** The author is not affiliated with or partnered with the Zen Browser team. This tool is not developed, sponsored, or endorsed by Zen Browser.

> Your default Zen profile is backed up, then replaced. Arc remains untouched. This tool is intended for a fresh Zen installation.

## Quick start

**[Download Arc → Zen for Apple Silicon Macs](https://github.com/Fabio-web/arc-to-zen-import/releases/latest/download/Arc-vers-Zen-macOS.zip)**

1. Install **Zen**, then quit both Arc and Zen with **⌘Q**.
2. Extract the ZIP and double-click **Arc-vers-Zen.command**.
3. Allow access to **Arc Safe Storage** if macOS prompts you.
4. Wait for the import to finish, then open Zen normally from the Dock or Applications.

**No Python, Homebrew, or Git installation required.** To share the tool, send the ZIP: the recipient does not need a GitHub account. The ZIP preserves the file's executable permissions.

The standalone download is for **Apple Silicon Macs**. Intel Mac users can run the source code with Python 3.9 or later.

The file is not notarized by Apple. If macOS blocks it, manually allow it in **System Settings → Privacy & Security**, only if you trust its source.

## What gets transferred

| Arc | Zen |
| --- | --- |
| Spaces, emojis, folders | Matching spaces and folders |
| Pinned and open tabs | Unloaded tabs, in Arc's original order |
| Favorites | Essentials |
| Cached favicons | Icons displayed without loading websites |
| Bookmarks | The “Arc importé” folder |
| History | Compatible visits |
| Passwords | Zen's password manager |
| Cookies | Compatible sessions |
| Extensions | A list to help find their Firefox equivalents |

The script replaces the default profile at its existing location. It does not create an extra profile to select, and no special launcher is needed afterward. If Zen has never been opened, its default profile is initialized.

If Arc has multiple profiles, `Default` is preferred. Sessions from multiple profiles are not merged.

## Limitations

- Some websites require you to sign in again. **Netflix cookies are excluded** so you can start a fresh session; this does not resolve any DRM limitations in Zen.
- Favicons missing from Arc's cache will appear when you visit the website. Split Views become folders of links.
- Not transferred: website and extension data, passkeys, payment cards, autofill, permissions, Arc settings, Boosts, Easels, archived tabs, and each tab's back/forward history. Downloaded files stay where they are.

Excluded items are listed in the report. Compatibility is checked before any profile changes; the tool tells you if your installed Zen version is unsupported.

## Backups

Arc and Zen backups, the report, and the extension list are saved here:

```text
~/Library/Application Support/Arc to Zen/Transferts/<date>/
```

This folder contains personal data: **do not share it**. If an error occurs while replacing the profile, the tool automatically restores the Zen profile and its configuration.

Decryption happens in memory, without exporting passwords or cookies as plain text. Websites are not loaded during the import. The new profile then stores its data in Zen's standard files.

## Run from source

Requires Python **3.9 or later**, with no additional dependencies:

```sh
git clone https://github.com/Fabio-web/arc-to-zen-import.git
cd arc-to-zen-import
python3 migrate.py --apply
```

Run an audit without making changes:

```sh
python3 migrate.py
```

Available options:

```sh
python3 migrate.py --apply --profile Default
python3 migrate.py --apply --skip-history
python3 migrate.py --apply --zen-app /path/to/Zen.app
python3 migrate.py --help
```

With Python installed, you can also double-click `Migrer.command` or `Audit.command` to run the source scripts.

Local checks:

```sh
python3 -B -m unittest discover -s tests -v
```

To rebuild the standalone download on an Apple Silicon Mac using [uv](https://docs.astral.sh/uv/):

```sh
arc_zen_build_root="$(mktemp -d /private/tmp/arc-to-zen-build.XXXXXXXX)"
export UV_PYTHON_INSTALL_DIR="$arc_zen_build_root/python"
uv venv --managed-python --python 3.13.13 "$arc_zen_build_root/venv"
uv pip install --python "$arc_zen_build_root/venv/bin/python" -r build_tools/requirements.txt
"$arc_zen_build_root/venv/bin/python" build_tools/build_mac.py
```

The executable and ZIP are generated in `dist/`. Using a temporary build directory keeps personal user paths out of the bundled Python runtime.
