#!/usr/bin/env python3
"""Conservative tooling for an existing Sikarugir EU5 wrapper.

The command is intentionally small in scope.  It inspects a wrapper without
writing by default, and can apply a pinned Wine runtime as a reversible
engine/plist change.  It never installs Steam, runs wineboot, launches EU5, or
touches game saves and user credentials.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


PINNED_WINE_URL = (
    "https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.13/"
    "wine-devel-11.13-osx64.tar.xz"
)
PINNED_WINE_SHA256 = "214e2044d32870688c715c9edb1005a61beb7ba21ffe8e819da485163f754bd0"
PINNED_WINE_SIZE = 189855828
TEMPLATE_VERSION = "1.0.21"
EU5_APP_ID = "3450310"
EXPECTED_FLAGS = "-silent -applaunch 3450310 -vulkan"
EXPECTED_WINEDEBUG = "-all,err+all"
VERSION_MARKER = "wine gcenx 11.13 (verified)\n"


class Eu5MacError(RuntimeError):
    """A user-actionable validation or safety error."""


class RunningWrapperError(Eu5MacError):
    """Raised when mutation is unsafe while a relevant process is running."""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str


@dataclass(frozen=True)
class ArchiveInfo:
    path: Path
    sha256: str
    size: int
    runtime_root: PurePosixPath


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise Eu5MacError(f"refusing symlinked {label}: {path}")


def resolve_wrapper(value: str | Path) -> Path:
    """Resolve a wrapper while rejecting an app bundle supplied as a symlink."""

    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if raw.is_symlink():
        raise Eu5MacError("wrapper path must not be a symlink")
    if not raw.exists() or not raw.is_dir():
        raise Eu5MacError(f"wrapper directory does not exist: {raw}")
    wrapper = raw.resolve(strict=True)
    if not (wrapper / "Contents").is_dir():
        raise Eu5MacError("wrapper is missing Contents/")
    for path, label in (
        (wrapper / "Contents", "wrapper Contents directory"),
        (wrapper / "Contents/Info.plist", "wrapper Info.plist"),
        (wrapper / "Contents/SharedSupport", "wrapper SharedSupport directory"),
    ):
        _reject_symlink(path, label)
    return wrapper


def wrapper_paths(wrapper: Path) -> dict[str, Path]:
    contents = wrapper / "Contents"
    shared = contents / "SharedSupport"
    return {
        "contents": contents,
        "plist": contents / "Info.plist",
        "shared": shared,
        "engine": shared / "wine",
        "prefix": shared / "prefix",
        "icd": contents / "Resources/vulkan/icd.d/MoltenVK_icd.json",
    }


def _require_real_dir(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise Eu5MacError(f"{label} must be a real directory: {path}")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise Eu5MacError(f"cannot resolve {label}: {exc}") from exc


def _reject_existing_symlink_components(path: Path, parent: Path, label: str) -> None:
    """Reject a path that reaches an existing symlink below a trusted root."""

    parent = parent.resolve(strict=True)
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise Eu5MacError(f"{label} escapes its trusted root") from exc
    current = parent
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise Eu5MacError(f"refusing symlinked {label} component: {current}")


def _validate_prefix(wrapper: Path) -> Path:
    paths = wrapper_paths(wrapper)
    prefix = _require_real_dir(paths["prefix"], "wrapper prefix")
    if not _path_is_within(prefix, wrapper):
        raise Eu5MacError("wrapper prefix resolves outside the app bundle")
    _reject_existing_symlink_components(prefix, wrapper, "wrapper prefix")
    return prefix


def _load_plist(wrapper: Path) -> dict[str, Any]:
    path = wrapper_paths(wrapper)["plist"]
    if not path.is_file():
        raise Eu5MacError("wrapper is missing Contents/Info.plist")
    try:
        value = plistlib.loads(path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise Eu5MacError(f"cannot read wrapper Info.plist: {exc}") from exc
    if not isinstance(value, dict):
        raise Eu5MacError("wrapper Info.plist is not a dictionary")
    return value


def _recursive_string_values(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _recursive_string_values(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, str):
        yield prefix, value


def _contains_bytes(path: Path, needle: bytes, limit: int = 64 * 1024 * 1024) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    overlap = max(0, len(needle) - 1)
    remaining = limit
    previous = b""
    try:
        with path.open("rb") as handle:
            while remaining > 0:
                block = handle.read(min(1024 * 1024, remaining))
                if not block:
                    break
                data = previous + block
                if needle in data:
                    return True
                previous = data[-overlap:] if overlap else b""
                remaining -= len(block)
    except OSError:
        return False
    return False


def moltenvk_evidence(wrapper: Path, plist: dict[str, Any] | None = None) -> list[str]:
    """Return bounded evidence that the template carries MoltenVK 1.4.1."""

    evidence: list[str] = []
    plist = plist if plist is not None else _load_plist(wrapper)
    for key, value in _recursive_string_values(plist):
        if "moltenvk" in key.lower() or "moltenvk" in value.lower():
            if "1.4.1" in value:
                evidence.append("Info.plist MoltenVK 1.4.1 marker")
    resources = wrapper / "Contents/Resources"
    for candidate in (
        resources / "MoltenVK.version",
        resources / "vulkan/MoltenVK.version",
        resources / "vulkan/icd.d/MoltenVK.version",
    ):
        if candidate.is_file():
            try:
                if "1.4.1" in candidate.read_text(encoding="utf-8", errors="replace")[:4096]:
                    evidence.append("MoltenVK version file 1.4.1")
            except OSError:
                pass
    framework = wrapper / "Contents/Frameworks/libMoltenVK.dylib"
    if _contains_bytes(framework, b"1.4.1"):
        evidence.append("libMoltenVK.dylib 1.4.1 marker")
    return list(dict.fromkeys(evidence))


def _parse_manifest(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Eu5MacError(f"cannot read EU5 app manifest: {exc}") from exc
    result: dict[str, str] = {}
    for key in ("appid", "StateFlags", "buildid"):
        match = re.search(rf'"{re.escape(key)}"\s+"([^"]*)"', text)
        if match:
            result[key] = match.group(1)
    return result


def _pe_machine(path: Path) -> str | None:
    """Read only the PE headers needed to identify EU5's architecture."""

    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return None
            handle.seek(0x3C)
            offset_bytes = handle.read(4)
            if len(offset_bytes) != 4:
                return None
            offset = int.from_bytes(offset_bytes, "little")
            if offset < 0 or offset > 16 * 1024 * 1024:
                return None
            handle.seek(offset)
            if handle.read(4) != b"PE\0\0":
                return None
            machine_bytes = handle.read(2)
            if len(machine_bytes) != 2:
                return None
            machine = int.from_bytes(machine_bytes, "little")
    except OSError:
        return None
    return {0x014C: "i386", 0x8664: "AMD64", 0xAA64: "ARM64"}.get(
        machine, f"0x{machine:04x}"
    )


def find_eu5_installation(wrapper: Path) -> dict[str, Any]:
    """Inspect only standard Steam roots inside the wrapper prefix."""

    prefix = _validate_prefix(wrapper)
    roots = [
        prefix / "drive_c/Program Files (x86)/Steam/steamapps",
        prefix / "drive_c/Program Files/Steam/steamapps",
    ]
    for steamapps in roots:
        _reject_existing_symlink_components(steamapps, prefix, "Steam library path")
        manifest = steamapps / f"appmanifest_{EU5_APP_ID}.acf"
        if not manifest.is_file() or manifest.is_symlink():
            continue
        fields = _parse_manifest(manifest)
        if fields.get("appid") != EU5_APP_ID:
            raise Eu5MacError("EU5 app manifest has a missing or wrong appid")
        if "StateFlags" not in fields or "buildid" not in fields or not fields.get("buildid"):
            raise Eu5MacError("EU5 app manifest is missing StateFlags or buildid")
        if fields["StateFlags"] != "4":
            return {
                "installed": False,
                "reason": f"EU5 manifest StateFlags={fields['StateFlags']}",
                "state_flags": fields["StateFlags"],
                "buildid": fields["buildid"],
                "manifest_present": True,
            }
        steam_root = steamapps.parent
        _reject_existing_symlink_components(steam_root / "steam.exe", prefix, "Steam executable path")
        steam_executable = steam_root / "steam.exe"
        if not steam_executable.is_file() or steam_executable.is_symlink():
            return {
                "installed": False,
                "reason": "steam.exe missing beside the Steam library",
                "state_flags": fields["StateFlags"],
                "buildid": fields["buildid"],
                "manifest_present": True,
                "steam_executable_present": False,
            }
        game_dir = steamapps / "common/Europa Universalis V"
        _reject_existing_symlink_components(game_dir, prefix, "EU5 game path")
        executable = game_dir / "binaries/eu5.exe"
        _reject_existing_symlink_components(executable, prefix, "EU5 executable path")
        return {
            "installed": executable.is_file() and not executable.is_symlink(),
            "state_flags": fields.get("StateFlags"),
            "buildid": fields.get("buildid"),
            "manifest_present": True,
            "steam_executable_present": True,
            "pe_machine": _pe_machine(executable) if executable.is_file() else None,
            "executable_present": executable.is_file() and not executable.is_symlink(),
            "compound_settings_present": (
                game_dir / "clausewitz/loading_screen/compound_settings.txt"
            ).is_file(),
        }
    return {"installed": False, "reason": "appmanifest_3450310.acf not found"}


def _process_name(command: str) -> str:
    lower = command.lower()
    for needle in ("eu5.exe", "steamwebhelper.exe", "steam.exe", "wineserver", "contents/macos/launcher"):
        if needle in lower:
            return needle
    return "relevant process"


def detect_running_processes(wrapper: Path) -> list[ProcessInfo]:
    """Detect relevant Wine/Steam processes without reading their environments."""

    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,comm=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Eu5MacError(f"cannot inspect running processes safely: {exc}") from exc
    if result.returncode != 0:
        raise Eu5MacError("process inspection failed; refusing mutation")
    wrapper_token = str(wrapper).lower()
    records: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        command = parts[3]
        lower = command.lower()
        relevant = any(
            needle in lower
            for needle in ("eu5.exe", "steam.exe", "steamwebhelper.exe", "wineserver", "contents/macos/launcher")
        )
        if not relevant:
            continue
        # Windows .exe names are Wine processes.  A native Steam helper does
        # not carry the .exe suffix; this avoids stopping the native client.
        if any(needle in lower for needle in ("eu5.exe", "steam.exe", "steamwebhelper.exe")) or wrapper_token in lower:
            records.append(ProcessInfo(pid=pid, name=_process_name(command)))
    return records


def _assert_no_running(wrapper: Path) -> None:
    running = detect_running_processes(wrapper)
    if running:
        names = ", ".join(item.name for item in running)
        raise RunningWrapperError(f"wrapper or Wine processes are running ({names}); quit the wrapper first")


def _engine_version(engine: Path) -> str | None:
    marker = engine / "version"
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        return marker.read_text(encoding="utf-8", errors="replace").strip()[:256]
    except OSError:
        return None


def inspect_wrapper(wrapper: Path) -> dict[str, Any]:
    plist = _load_plist(wrapper)
    paths = wrapper_paths(wrapper)
    template_version = plist.get("CFBundleVersion")
    evidence = moltenvk_evidence(wrapper, plist)
    eu5 = find_eu5_installation(wrapper)
    engine = paths["engine"]
    layout = all((engine / name).exists() for name in ("bin", "lib", "share"))
    wine_binary = engine / "bin/wine"
    running = detect_running_processes(wrapper)
    return {
        "wrapper": str(wrapper),
        "template_version": template_version,
        "template_version_supported": template_version == TEMPLATE_VERSION,
        "moltenvk_icd_present": paths["icd"].is_file() and not paths["icd"].is_symlink(),
        "moltenvk_1_4_1_evidence": evidence,
        "engine_layout": layout and wine_binary.exists(),
        "engine_version_marker": _engine_version(engine),
        "eu5": eu5,
        "running_processes": [{"pid": item.pid, "name": item.name} for item in running],
        "default_action_is_read_only": True,
        "planned_apply_changes": [
            "replace Contents/SharedSupport/wine with verified Gcenx Wine 11.13",
            "set the wrapper's Wine/Vulkan launch plist values",
            "preserve a private Info.plist, engine, and registry snapshot",
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normal_member_name(name: str) -> PurePosixPath:
    if not name or "\0" in name:
        raise Eu5MacError("archive contains an invalid member name")
    path = PurePosixPath(name)
    if path.is_absolute():
        raise Eu5MacError("archive contains an absolute member path")
    if ".." in path.parts:
        raise Eu5MacError("archive contains a parent-directory member path")
    normalized = PurePosixPath(posixpath.normpath(name))
    if normalized == PurePosixPath("..") or str(normalized).startswith("../"):
        raise Eu5MacError("archive contains a path traversal member")
    return normalized


def _archive_link_target(member: tarfile.TarInfo, normalized: PurePosixPath) -> PurePosixPath:
    if not member.linkname or PurePosixPath(member.linkname).is_absolute():
        raise Eu5MacError(f"archive contains an unsafe link: {member.name}")
    if member.islnk():
        # Tar hardlink names are rooted at the archive, unlike symlink names.
        return _normal_member_name(member.linkname)
    target = PurePosixPath(
        posixpath.normpath(posixpath.join(posixpath.dirname(str(normalized)), member.linkname))
    )
    if target == PurePosixPath("..") or str(target).startswith("../"):
        raise Eu5MacError(f"archive link escapes its root: {member.name}")
    return target


def _validated_archive_members(
    members: list[tarfile.TarInfo],
) -> tuple[dict[PurePosixPath, tarfile.TarInfo], PurePosixPath]:
    member_map: dict[PurePosixPath, tarfile.TarInfo] = {}
    for member in members:
        normalized = _normal_member_name(member.name)
        if normalized in member_map:
            raise Eu5MacError(f"archive contains duplicate member paths: {member.name}")
        if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise Eu5MacError(f"archive contains an unsupported special file: {member.name}")
        member_map[normalized] = member

    link_targets: dict[PurePosixPath, PurePosixPath] = {}
    for normalized, member in member_map.items():
        if member.issym() or member.islnk():
            target = _archive_link_target(member, normalized)
            if target not in member_map:
                raise Eu5MacError(f"archive link target is missing: {member.name}")
            link_targets[normalized] = target

    # A symlink or hardlink may not be used as a directory containing later
    # members.  This prevents extractall from writing through a link.
    for normalized in member_map:
        for ancestor in normalized.parents:
            if ancestor == PurePosixPath("."):
                continue
            parent = member_map.get(ancestor)
            if parent is not None and not parent.isdir():
                raise Eu5MacError(f"archive link has descendants: {ancestor}")

    # Resolve link chains with a small bound so cycles and long malicious
    # traversals cannot reach extraction.
    for start in link_targets:
        current = start
        seen: set[PurePosixPath] = set()
        for _ in range(64):
            if current not in link_targets:
                break
            if current in seen:
                raise Eu5MacError(f"archive contains a link cycle: {start}")
            seen.add(current)
            current = link_targets[current]
        else:
            raise Eu5MacError(f"archive link chain is too long: {start}")
        final = member_map[current]
        if not (final.isfile() or final.isdir()):
            raise Eu5MacError(f"archive link chain ends at an unsupported member: {start}")

    roots = [
        name
        for name in member_map
        if str(name).endswith("/Contents/Resources/wine")
        or str(name) == "Contents/Resources/wine"
    ]
    if len(roots) != 1:
        raise Eu5MacError("archive must contain one Wine Devel.app runtime root")
    runtime_root = roots[0]
    required = [runtime_root / "bin/wine", runtime_root / "lib", runtime_root / "share"]
    if any(item not in member_map for item in required):
        raise Eu5MacError("archive is missing the expected Wine bin/lib/share layout")
    return member_map, runtime_root


def validate_archive(
    path: Path,
    expected_sha256: str = PINNED_WINE_SHA256,
    expected_size: int = PINNED_WINE_SIZE,
) -> ArchiveInfo:
    if path.is_symlink() or not path.is_file():
        raise Eu5MacError("Wine archive must be a regular file, not a symlink")
    size = path.stat().st_size
    if size != expected_size:
        raise Eu5MacError("Wine archive size does not match the pinned value")
    digest = _sha256(path)
    if digest.lower() != expected_sha256.lower():
        raise Eu5MacError("Wine archive SHA256 does not match the pinned value")
    try:
        with tarfile.open(path, mode="r:xz") as archive:
            members = archive.getmembers()
            _, runtime_root = _validated_archive_members(members)
    except (OSError, tarfile.TarError) as exc:
        raise Eu5MacError(f"cannot validate Wine archive: {exc}") from exc
    return ArchiveInfo(path=path, sha256=digest, size=size, runtime_root=runtime_root)


def _ensure_private_parent(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise Eu5MacError(f"download parent must be a real directory: {path}")
        return path.resolve(strict=True)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if parent.is_symlink():
        raise Eu5MacError("download parent reaches a symlink")
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path.resolve(strict=True)


def _download_target(destination: str | Path | None, cache_dir: str | Path | None) -> Path:
    if bool(destination) == bool(cache_dir):
        raise Eu5MacError("download requires exactly one of --destination or --cache-dir")
    if destination is not None:
        target = Path(destination).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
    else:
        cache = Path(cache_dir).expanduser()  # type: ignore[arg-type]
        if not cache.is_absolute():
            cache = Path.cwd() / cache
        target = cache / "wine-devel-11.13-osx64.tar.xz"
    if target.is_symlink():
        raise Eu5MacError("download destination must not be a symlink")
    repo = Path(__file__).resolve().parent
    if _path_is_within(target, repo):
        raise Eu5MacError("download destination must be outside the toolkit repository")
    _ensure_private_parent(target.parent)
    return target


def download_archive(
    destination: str | Path | None = None,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Download and verify the pinned archive, reusing an exact cached file."""

    target = _download_target(destination, cache_dir)
    if target.exists():
        try:
            info = validate_archive(
                target,
                expected_sha256=PINNED_WINE_SHA256,
                expected_size=PINNED_WINE_SIZE,
            )
        except Eu5MacError as exc:
            raise Eu5MacError(
                f"existing destination is not the verified pinned archive; refusing to clobber: {target}"
            ) from exc
        return {
            "status": "reused",
            "path": str(target),
            "sha256": info.sha256,
            "size": info.size,
        }

    temporary: Path | None = None
    try:
        request = urllib.request.Request(PINNED_WINE_URL, headers={"User-Agent": "eu5-mac/0.1"})
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except (OSError, urllib.error.URLError) as exc:
            raise Eu5MacError(f"pinned archive download failed: {exc}") from exc
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".eu5-mac-download-", dir=str(target.parent), delete=False
            ) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                total = 0
                digest = hashlib.sha256()
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > PINNED_WINE_SIZE:
                        raise Eu5MacError("pinned archive response exceeds the expected size")
                    handle.write(chunk)
                    digest.update(chunk)
        finally:
            response.close()
        if total != PINNED_WINE_SIZE or digest.hexdigest() != PINNED_WINE_SHA256:
            raise Eu5MacError("downloaded archive failed the pinned size or SHA256 check")
        validate_archive(
            temporary,
            expected_sha256=PINNED_WINE_SHA256,
            expected_size=PINNED_WINE_SIZE,
        )
        if target.exists() or target.is_symlink():
            if target.is_symlink():
                raise Eu5MacError("download destination became a symlink during download")
            try:
                info = validate_archive(
                    target,
                    expected_sha256=PINNED_WINE_SHA256,
                    expected_size=PINNED_WINE_SIZE,
                )
            except Eu5MacError as exc:
                raise Eu5MacError("download destination changed and is not the pinned archive") from exc
            return {"status": "reused", "path": str(target), "sha256": info.sha256, "size": info.size}
        os.replace(temporary, target)
        temporary = None
        os.chmod(target, 0o600)
        info = validate_archive(
            target,
            expected_sha256=PINNED_WINE_SHA256,
            expected_size=PINNED_WINE_SIZE,
        )
        return {"status": "downloaded", "path": str(target), "sha256": info.sha256, "size": info.size}
    except Eu5MacError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise Eu5MacError(f"pinned archive download failed: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _extract_archive(info: ArchiveInfo, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(info.path, mode="r:xz") as archive:
            members = archive.getmembers()
            _, runtime_root = _validated_archive_members(members)
            if runtime_root != info.runtime_root:
                raise Eu5MacError("archive runtime root changed between validation and extraction")
            # validate_archive already checked names and links; re-check the
            # extraction destination to keep this function safe if reused.
            for member in members:
                normalized = _normal_member_name(member.name)
                target = destination / Path(*normalized.parts)
                if not _path_is_within(target, destination):
                    raise Eu5MacError("archive extraction path escapes its staging directory")
            archive.extractall(destination, members=members)
    except (OSError, tarfile.TarError) as exc:
        raise Eu5MacError(f"cannot extract Wine archive: {exc}") from exc
    runtime = destination.joinpath(*info.runtime_root.parts)
    if not runtime.is_dir() or runtime.is_symlink():
        raise Eu5MacError("extracted Wine runtime root is unsafe or missing")
    for name in ("bin/wine", "lib", "share"):
        candidate = runtime / name
        if not candidate.exists():
            raise Eu5MacError(f"extracted Wine runtime is missing {name}")
    return runtime


def _probe_runtime(runtime: Path) -> str:
    binary = runtime / "bin/wine"
    if not binary.exists() or binary.is_symlink() and not binary.resolve().exists():
        raise Eu5MacError("staged Wine runtime has no usable bin/wine")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "WINEDEBUG": "-all",
    }
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            cwd=str(runtime),
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Eu5MacError(f"Wine version probe failed: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0 or not re.search(r"(?:wine|Wine)[^\n]*11\.13", output):
        raise Eu5MacError("staged runtime did not report Wine 11.13")
    return output.splitlines()[0][:120]


def _private_backup_root(wrapper: Path, requested: str | Path | None) -> Path:
    repo = Path(__file__).resolve().parent
    supplied = requested is not None
    base = Path(requested).expanduser() if supplied else wrapper.parent / ".eu5-mac-backups"
    if not base.is_absolute():
        base = Path.cwd() / base
    if base.is_symlink():
        raise Eu5MacError("private backup root must not be a symlink")
    if base.exists() and not base.is_dir():
        raise Eu5MacError("private backup root is not a directory")
    prospective = base.resolve(strict=False)
    if _path_is_within(prospective, wrapper) or _path_is_within(prospective, repo):
        raise Eu5MacError("private backups must be outside the wrapper and toolkit repository")

    # --backup-dir names a parent when it already exists.  Keep an existing
    # user directory (for example ~/Downloads) untouched and create a private
    # toolkit directory below it.  A missing explicit path is itself the new
    # private root.  In both cases only directories created here are chmod'ed.
    if supplied and base.exists():
        base = base / ".eu5-mac-backups"
        if base.is_symlink():
            raise Eu5MacError("private backup root must not be a symlink")
        if base.exists() and not base.is_dir():
            raise Eu5MacError("private backup root is not a directory")
        prospective = base.resolve(strict=False)
        if _path_is_within(prospective, wrapper) or _path_is_within(prospective, repo):
            raise Eu5MacError("private backups must be outside the wrapper and toolkit repository")
    if not base.exists():
        parent = base.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if parent.is_symlink():
            raise Eu5MacError("private backup parent must not be a symlink")
        base.mkdir(parents=True, mode=0o700)
        os.chmod(base, 0o700)
    try:
        base = base.resolve(strict=True)
    except OSError as exc:
        raise Eu5MacError(f"cannot resolve private backup root: {exc}") from exc
    if _path_is_within(base, wrapper) or _path_is_within(base, repo):
        raise Eu5MacError("private backups must be outside the toolkit repository")
    if stat.S_IMODE(base.stat().st_mode) != 0o700:
        raise Eu5MacError("private backup root must have mode 0700")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = base / f"apply-{stamp}"
    index = 2
    while candidate.exists():
        candidate = base / f"apply-{stamp}-r{index}"
        index += 1
    candidate.mkdir(mode=0o700)
    os.chmod(candidate, 0o700)
    if _path_is_within(candidate, wrapper) or _path_is_within(candidate, repo):
        raise Eu5MacError("private backup snapshot is inside a forbidden path")
    return candidate


def _same_device(first: Path, second: Path) -> bool:
    try:
        return os.stat(first).st_dev == os.stat(second).st_dev
    except OSError as exc:
        raise Eu5MacError(f"cannot compare backup and wrapper filesystems: {exc}") from exc


def _copy_private_file(source: Path, destination: Path) -> None:
    _reject_symlink(source, "private backup source")
    if not source.is_file() or destination.exists() or destination.is_symlink():
        raise Eu5MacError(f"private backup file collision or invalid source: {destination}")
    shutil.copy2(source, destination)
    os.chmod(destination, 0o600)


def _copy_tree(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise Eu5MacError(f"expected a real directory: {source}")
    shutil.copytree(source, destination, symlinks=True)


def _load_backup_for_wrapper(wrapper: Path, backup: Path) -> tuple[Path, Path, dict[str, Any]]:
    if backup.is_symlink() or not backup.is_dir():
        raise Eu5MacError("backup path must be a real directory")
    try:
        backup = backup.resolve(strict=True)
    except OSError as exc:
        raise Eu5MacError(f"cannot resolve backup path: {exc}") from exc
    repo = Path(__file__).resolve().parent
    if _path_is_within(backup, wrapper) or _path_is_within(backup, repo):
        raise Eu5MacError("backup must be outside the wrapper and toolkit repository")
    backup_plist = backup / "Info.plist"
    backup_engine = backup / "engine-original"
    manifest_path = backup / "BACKUP_MANIFEST.json"
    for path, label in (
        (backup_plist, "backup Info.plist"),
        (backup_engine, "backup engine"),
        (manifest_path, "backup manifest"),
    ):
        _reject_symlink(path, label)
    if not backup_plist.is_file() or not backup_engine.is_dir():
        raise Eu5MacError("backup is missing Info.plist or engine-original")
    if not manifest_path.is_file():
        raise Eu5MacError("backup manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Eu5MacError(f"cannot read backup manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise Eu5MacError("backup manifest is not an object")
    if manifest.get("wrapper_path") != str(wrapper.resolve(strict=True)):
        raise Eu5MacError("backup belongs to a different wrapper")
    recorded_hash = manifest.get("original_plist_sha256")
    if not isinstance(recorded_hash, str) or _sha256(backup_plist) != recorded_hash:
        raise Eu5MacError("backup Info.plist hash does not match its manifest")
    if stat.S_IMODE(backup.stat().st_mode) != 0o700:
        raise Eu5MacError("backup snapshot must have mode 0700")
    return backup_plist, backup_engine, manifest


def _write_plist(path: Path, value: dict[str, Any]) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    os.close(fd)
    temp = Path(temp_name)
    try:
        with temp.open("wb") as handle:
            plistlib.dump(value, handle, sort_keys=False)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _desired_plist(wrapper: Path, original: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(original)
    value["Program Name and Path"] = "/Program Files (x86)/Steam/steam.exe"
    value["Program Flags"] = EXPECTED_FLAGS
    value["D3DMETAL"] = 0
    value["D9VK"] = 0
    value["DXVK"] = 0
    value["WINEESYNC"] = 0
    value["WINEMSYNC"] = 0
    value["Symlinks In User Folder"] = 0
    value["WINEDEBUG"] = EXPECTED_WINEDEBUG
    environment = value.get("LSEnvironment")
    if not isinstance(environment, dict):
        environment = {}
    else:
        environment = copy.deepcopy(environment)
    environment["SikarugirAppWine11"] = "1"
    environment["VK_DRIVER_FILES"] = str(wrapper / "Contents/Resources/vulkan/icd.d/MoltenVK_icd.json")
    value["LSEnvironment"] = environment
    return value


def _is_already_configured(wrapper: Path, plist: dict[str, Any]) -> bool:
    paths = wrapper_paths(wrapper)
    engine_version = _engine_version(paths["engine"]) or ""
    environment = plist.get("LSEnvironment")
    return (
        "11.13" in engine_version
        and plist.get("Program Name and Path") == "/Program Files (x86)/Steam/steam.exe"
        and plist.get("Program Flags") == EXPECTED_FLAGS
        and plist.get("D3DMETAL") == 0
        and plist.get("D9VK") == 0
        and plist.get("DXVK") == 0
        and plist.get("WINEESYNC") == 0
        and plist.get("WINEMSYNC") == 0
        and plist.get("Symlinks In User Folder") == 0
        and plist.get("WINEDEBUG") == EXPECTED_WINEDEBUG
        and isinstance(environment, dict)
        and environment.get("SikarugirAppWine11") == "1"
        and environment.get("VK_DRIVER_FILES") == str(paths["icd"])
    )


def _backup_registry(prefix: Path, destination: Path) -> list[str]:
    if destination.exists() or destination.is_symlink():
        raise Eu5MacError("registry backup destination already exists")
    destination.mkdir(mode=0o700)
    os.chmod(destination, 0o700)
    missing: list[str] = []
    for name in ("system.reg", "user.reg", "userdef.reg"):
        source = prefix / name
        if source.is_file() and not source.is_symlink():
            _copy_private_file(source, destination / name)
        else:
            missing.append(name)
    return missing


def _write_backup_manifest(destination: Path, data: dict[str, Any]) -> None:
    target = destination / "BACKUP_MANIFEST.json"
    if target.exists() or target.is_symlink():
        raise Eu5MacError("backup manifest already exists")
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)


def _rollback_engine(wrapper: Path, backup_engine: Path) -> None:
    active = wrapper_paths(wrapper)["engine"]
    if active.exists() or active.is_symlink():
        if active.is_symlink() or not active.is_dir():
            active.unlink()
        else:
            shutil.rmtree(active)
    if backup_engine.exists():
        os.replace(backup_engine, active)


def _copy_stage_file(source: Path, destination: Path) -> None:
    _reject_symlink(source, "staged file source")
    if not source.is_file() or destination.exists() or destination.is_symlink():
        raise Eu5MacError(f"invalid staged file or destination collision: {destination}")
    shutil.copy2(source, destination)
    os.chmod(destination, stat.S_IMODE(source.stat().st_mode))


def _write_plist_file(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    if path.exists() or path.is_symlink():
        raise Eu5MacError(f"staged plist destination already exists: {path}")
    with path.open("wb") as handle:
        plistlib.dump(value, handle, sort_keys=False)
    os.chmod(path, mode)


def _rollback_swap(
    active_engine: Path,
    backup_engine: Path,
    plist: Path,
    old_plist_stage: Path,
    failed_stage: Path,
) -> None:
    errors: list[str] = []
    try:
        if backup_engine.exists():
            if active_engine.exists() or active_engine.is_symlink():
                if failed_stage.exists():
                    raise Eu5MacError("rollback staging collision")
                os.replace(active_engine, failed_stage)
            os.replace(backup_engine, active_engine)
    except (OSError, Eu5MacError) as exc:
        errors.append(f"engine rollback failed: {exc}")
    try:
        if old_plist_stage.exists():
            os.replace(old_plist_stage, plist)
    except OSError as exc:
        errors.append(f"plist rollback failed: {exc}")
    if errors:
        raise Eu5MacError("; ".join(errors))


def apply_wrapper(
    wrapper: Path,
    archive: Path | None,
    backup_dir: str | Path | None = None,
    *,
    expected_sha256: str = PINNED_WINE_SHA256,
    expected_size: int = PINNED_WINE_SIZE,
) -> dict[str, Any]:
    paths = wrapper_paths(wrapper)
    active_engine = _require_real_dir(paths["engine"], "active Wine engine")
    plist = _load_plist(wrapper)
    report = inspect_wrapper(wrapper)
    if report["template_version"] != TEMPLATE_VERSION:
        raise Eu5MacError(f"expected Sikarugir Template {TEMPLATE_VERSION}")
    if not report["moltenvk_icd_present"] or not report["moltenvk_1_4_1_evidence"]:
        raise Eu5MacError("wrapper lacks the expected MoltenVK ICD or 1.4.1 evidence")
    if not report["eu5"].get("installed"):
        raise Eu5MacError("EU5 app 3450310 is not installed in the wrapper's standard Steam root")
    if report["eu5"].get("state_flags") != "4":
        raise Eu5MacError("EU5 Steam manifest is not in the installed state")
    if report["running_processes"]:
        names = ", ".join(item["name"] for item in report["running_processes"])
        raise RunningWrapperError(f"wrapper or Wine processes are running ({names}); quit the wrapper first")
    if _is_already_configured(wrapper, plist) and archive is None:
        return {"status": "already_configured", "wrapper": str(wrapper)}
    if archive is None:
        raise Eu5MacError("apply requires --wine-archive unless the wrapper is already configured")
    archive = archive.expanduser()
    if archive.is_symlink():
        raise Eu5MacError("Wine archive must not be a symlink")
    try:
        archive = archive.resolve(strict=True)
    except OSError as exc:
        raise Eu5MacError(f"Wine archive does not exist: {archive}") from exc
    info = validate_archive(archive, expected_sha256=expected_sha256, expected_size=expected_size)
    backup = _private_backup_root(wrapper, backup_dir)
    backup_engine = backup / "engine-original"
    _validate_prefix(wrapper)
    if not _same_device(active_engine, backup):
        raise Eu5MacError("private backup must be on the same filesystem as the wrapper engine")
    original_plist_hash = _sha256(paths["plist"])
    _copy_private_file(paths["plist"], backup / "Info.plist")
    missing_registry = _backup_registry(paths["prefix"], backup / "prefix-registry")
    with tempfile.TemporaryDirectory(prefix=".eu5-mac-stage-", dir=str(paths["shared"])) as temp_name:
        stage_root = Path(temp_name)
        os.chmod(stage_root, 0o700)
        staged = _extract_archive(info, stage_root / "runtime")
        probe = _probe_runtime(staged)
        version = staged / "version"
        if not version.is_file() or "11.13" not in version.read_text(encoding="utf-8", errors="replace")[:512]:
            version.write_text(VERSION_MARKER, encoding="utf-8")
        old_plist_stage = stage_root / "Info.plist.original"
        _copy_stage_file(paths["plist"], old_plist_stage)
        desired_stage = stage_root / "Info.plist.new"
        try:
            desired = _desired_plist(wrapper, plist)
            _write_plist_file(desired_stage, desired, stat.S_IMODE(paths["plist"].stat().st_mode))
        except Eu5MacError:
            raise
        except Exception as exc:
            raise Eu5MacError(f"could not prepare wrapper plist: {exc}") from exc
        _write_backup_manifest(
            backup,
            {
                "tool": "eu5-mac",
                "schema": 1,
                "wrapper_path": str(wrapper.resolve(strict=True)),
                "original_plist_sha256": original_plist_hash,
                "archive_url": PINNED_WINE_URL,
                "archive_sha256": info.sha256,
                "archive_size": info.size,
                "runtime_probe": probe,
                "template_version": TEMPLATE_VERSION,
                "registry_files_missing": missing_registry,
                "restore_scope": "Info.plist and engine only; registry snapshots are retained but not auto-restored",
            },
        )
        _assert_no_running(wrapper)
        _require_real_dir(active_engine, "active Wine engine")
        if backup_engine.exists() or backup_engine.is_symlink():
            raise Eu5MacError("backup engine destination already exists")
        os.replace(active_engine, backup_engine)
        try:
            os.replace(staged, active_engine)
            os.replace(desired_stage, paths["plist"])
        except Exception as exc:
            try:
                _rollback_swap(active_engine, backup_engine, paths["plist"], old_plist_stage, stage_root / "failed-engine")
            except Eu5MacError as rollback_error:
                raise Eu5MacError(f"apply failed and rollback failed: {rollback_error}") from exc
            raise Eu5MacError(f"apply failed and was rolled back: {exc}") from exc
    return {
        "status": "applied",
        "wrapper": str(wrapper),
        "backup": str(backup),
        "wine_probe": probe,
        "archive_sha256": info.sha256,
        "archive_size": info.size,
    }


def restore_wrapper(wrapper: Path, backup: Path) -> dict[str, Any]:
    paths = wrapper_paths(wrapper)
    active_engine = _require_real_dir(paths["engine"], "active Wine engine")
    backup_plist, backup_engine, manifest = _load_backup_for_wrapper(wrapper, backup)
    if not _same_device(active_engine, backup):
        raise Eu5MacError("restore backup must be on the same filesystem as the wrapper engine")
    _assert_no_running(wrapper)
    current_name = "engine-before-restore"
    current = backup / current_name
    index = 2
    while current.exists():
        current = backup / f"{current_name}-r{index}"
        index += 1
    if current.is_symlink():
        raise Eu5MacError("restore destination is a symlink")
    with tempfile.TemporaryDirectory(prefix=".eu5-mac-restore-", dir=str(paths["shared"])) as temp_name:
        stage_root = Path(temp_name)
        os.chmod(stage_root, 0o700)
        staged_engine = stage_root / "engine"
        _copy_tree(backup_engine, staged_engine)
        old_plist_stage = stage_root / "Info.plist.original"
        _copy_stage_file(paths["plist"], old_plist_stage)
        new_plist_stage = stage_root / "Info.plist.new"
        _copy_stage_file(backup_plist, new_plist_stage)
        _assert_no_running(wrapper)
        _require_real_dir(active_engine, "active Wine engine")
        os.replace(active_engine, current)
        try:
            os.replace(staged_engine, active_engine)
            os.replace(new_plist_stage, paths["plist"])
        except Exception as exc:
            try:
                _rollback_swap(active_engine, current, paths["plist"], old_plist_stage, stage_root / "failed-engine")
            except Eu5MacError as rollback_error:
                raise Eu5MacError(f"restore failed and rollback failed: {rollback_error}") from exc
            raise Eu5MacError(f"restore failed and was rolled back: {exc}") from exc
    return {
        "status": "restored",
        "wrapper": str(wrapper),
        "backup": str(backup),
        "registry_restored": False,
        "previous_engine": str(current),
        "manifest_schema": manifest.get("schema"),
    }


def _print_result(value: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    for key, item in value.items():
        if isinstance(item, (dict, list)):
            print(f"{key}: {json.dumps(item, sort_keys=True)}")
        else:
            print(f"{key}: {item}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", help="existing Sikarugir .app wrapper")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("inspect", help="read-only wrapper inventory (default)")
    subparsers.add_parser("plan", help="read-only dry-run plan (alias for inspect)")
    apply_parser = subparsers.add_parser("apply", help="apply the verified Wine 11.13 runtime")
    apply_parser.add_argument("--wine-archive", type=Path, help="already downloaded pinned Wine archive")
    apply_parser.add_argument("--backup-dir", type=Path, help="private backup parent outside this repository")
    restore_parser = subparsers.add_parser("restore", help="restore engine and plist from a private backup")
    restore_parser.add_argument("--backup", required=True, type=Path, help="private backup directory from apply")
    download_parser = subparsers.add_parser("download", help="download and verify the pinned Wine archive")
    destination_group = download_parser.add_mutually_exclusive_group(required=True)
    destination_group.add_argument("--destination", type=Path, help="final archive path outside this repository")
    destination_group.add_argument("--cache-dir", type=Path, help="private cache directory for the pinned archive")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        command = args.command or "inspect"
        if command == "download":
            result = download_archive(args.destination, args.cache_dir)
            _print_result(result, args.json)
            return 0
        if not args.wrapper:
            parser.error("--wrapper is required for inspect, plan, apply, and restore")
        wrapper = resolve_wrapper(args.wrapper)
        if command in ("inspect", "plan"):
            result = inspect_wrapper(wrapper)
        elif command == "apply":
            result = apply_wrapper(wrapper, args.wine_archive, args.backup_dir)
        elif command == "restore":
            backup = args.backup.expanduser()
            if not backup.is_absolute():
                backup = Path.cwd() / backup
            result = restore_wrapper(wrapper, backup)
        else:
            parser.error(f"unknown command: {command}")
            return 2
        _print_result(result, args.json)
        return 0
    except Eu5MacError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: filesystem operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
