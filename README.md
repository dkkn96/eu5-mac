# eu5-mac

> **Experimental status:** one session on the local M4 Pro froze for more than
> ten minutes after a graphics quality/preset change. After resetting graphics
> preferences and restarting, the user confirmed that in-game time advances
> normally without further settings changes. The cause of the freeze remains
> unproven; graphics-setting stability is unresolved.

`eu5-mac` is a small helper for an **existing** [Sikarugir Template
1.0.21](https://github.com/Sikarugir-App/Template) Steam wrapper. It checks the
wrapper, downloads or verifies a pinned Gcenx Wine 11.13 archive, and can
replace the wrapper's Wine engine with a private backup and reversible plist
update.

This is not a native macOS port. EU5 remains a Windows build running through
Wine, Rosetta, Vulkan, and MoltenVK. The helper does not install Steam, install
EU5, run `wineboot`, launch the game, or change a system-wide environment.

## Setup path

You need an Apple Silicon Mac with Rosetta installed, Python 3.10 or newer,
and your own Steam copy of Europa Universalis V. This helper uses only the
Python standard library and requires an existing Template 1.0.21 wrapper.

1. Follow the [official Sikarugir installation workflow](https://github.com/Sikarugir-App/Sikarugir)
   to create a Template 1.0.21 app. Keep its stock Wine 11.0 engine while
   preparing Steam; this project does not recreate the Creator or Launcher.
2. Use that wrapper's normal Windows Steam flow to install Steam and Europa
   Universalis V. The user supplies the Steam account, owns the game, and
   handles sign-in.
3. In Steam, set EU5's launch option to `-vulkan`, then run the game once if
   desired so the wrapper has a normal installed library. Quit only this
   dedicated Sikarugir wrapper and its Windows Steam processes before changing
   its engine.
4. Download the pinned runtime into a private cache, inspect the wrapper, then
   apply the reversible update. Choose the app's final location first: the
   Vulkan configuration and recovery backup are tied to that path.

   ```sh
   python3 eu5_mac.py download --cache-dir "$HOME/Library/Caches/eu5-mac"
   python3 eu5_mac.py --wrapper "/path/to/Europa Universalis V.app" plan
   python3 eu5_mac.py --wrapper "/path/to/Europa Universalis V.app" apply \
     --wine-archive "$HOME/Library/Caches/eu5-mac/wine-devel-11.13-osx64.tar.xz"
   ```

   Review the private backup path printed by `apply`, then open the wrapper
   through the normal Sikarugir workflow. The helper does not promise a clean
   installation, a Finder relaunch, sustained gameplay, or save/reload.

Keep the wrapper outside this repository. `plan` and the default command are
read-only. `apply` and `restore` refuse a running wrapper, symlinked paths,
unknown Template versions, malformed or uninstalled Steam manifests, unsafe
archives, and files on a different filesystem from the wrapper engine.

## Commands

The default action is a read-only inventory. `plan` is an explicit alias.

```sh
python3 eu5_mac.py --wrapper "/path/to/Europa Universalis V.app"
python3 eu5_mac.py --wrapper "/path/to/Europa Universalis V.app" plan
```

`download` is the only network command. It uses the official pinned HTTPS
source and accepts either a final path or a cache directory. It streams to a
private temporary file, checks the exact size and SHA256, validates the archive
layout, and only then renames it into place. An existing exact file is reused;
an existing mismatch is never clobbered.

```sh
python3 eu5_mac.py download --destination "/private/cache/wine-devel-11.13-osx64.tar.xz"
```

The pinned source, size, and checksum are recorded in
[`assets/pinned-wine.json`](assets/pinned-wine.json). The archive is
Gcenx Wine 11.13, 189855828 bytes, SHA256
`214e2044d32870688c715c9edb1005a61beb7ba21ffe8e819da485163f754bd0`.

Restore the engine and plist from a private backup when needed:

```sh
python3 eu5_mac.py --wrapper "/path/to/Europa Universalis V.app" restore \
  --backup "/path/to/private/.eu5-mac-backups/apply-YYYYMMDDTHHMMSSZ"
```

Restore deliberately does not restore registry snapshots or all prefix and
user data. Those snapshots remain private for inspection and manual recovery.

## Scope

The managed values are the wrapper's Wine engine, its small `version` marker,
and the known EU5/Vulkan plist values:

- Windows Steam program path `/Program Files (x86)/Steam/steam.exe`;
- `-silent -applaunch 3450310 -vulkan` program flags;
- `D3DMETAL`, `D9VK`, `DXVK`, `WINEESYNC`, and `WINEMSYNC` set to `0`;
- `Symlinks In User Folder` set to `0` and `WINEDEBUG` set to
  `-all,err+all`;
- `LSEnvironment` values `SikarugirAppWine11=1` and `VK_DRIVER_FILES` pointing
  to the exact ICD inside the wrapper.

It does not edit `compound_settings.txt`, shader caches, executable files, EU5
content, saves, Steam credentials, or account files. It does not remove
quarantine attributes or change security settings.

## Evidence and limits

The local reference run used an Apple M4 Pro with 48 GB RAM on macOS 27.0.1,
EU5 1.3.11 build 24187685, Template 1.0.21, Wine 11.13, and MoltenVK 1.4.1.
The game reached a playable state, then froze after a graphics-setting
change. After resetting graphics preferences and restarting, the user
confirmed that in-game time advances normally. The exact UI value is unknown
and the cause of the freeze is unproven.

Clean installation, normal Finder relaunch, sustained campaigns, save/reload,
performance, and other Macs remain unverified. See
[`docs/TESTING.md`](docs/TESTING.md) and
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md).

Original code and documentation are MIT licensed; third-party notices are in
[`THIRD_PARTY.md`](THIRD_PARTY.md) and [`LICENSE`](LICENSE).

## Development

Python 3.10+ and the standard library are required. Run the fixture suite
without touching an installed wrapper or using the network:

```sh
python3 -m unittest discover -s tests -v
```

The tests cover read-only inspection, process refusal, pinned archive checks,
mocked download/reuse/mismatch cases, unsafe archive members, private backup
placement and modes, reversible apply/restore, plist preservation, and
failure rollback.
