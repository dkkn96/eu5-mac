from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import eu5_mac  # noqa: E402


class WrapperFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.wrapper = root / "Europa Universalis V.app"
        contents = self.wrapper / "Contents"
        shared = contents / "SharedSupport"
        plist = {
            "CFBundleVersion": "1.0.21",
            "CFBundleIdentifier": "com.example.eu5",
            "UnrelatedSetting": "keep-me",
            "LSEnvironment": {"KEEP_ME": "yes"},
        }
        (contents).mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps(plist, sort_keys=False))
        (contents / "Resources/vulkan/icd.d").mkdir(parents=True)
        (contents / "Resources/vulkan/icd.d/MoltenVK_icd.json").write_text("{}\n", encoding="utf-8")
        (contents / "Frameworks").mkdir()
        (contents / "Frameworks/libMoltenVK.dylib").write_bytes(b"MoltenVK 1.4.1\0")

        engine = shared / "wine"
        (engine / "bin").mkdir(parents=True)
        (engine / "lib").mkdir()
        (engine / "share").mkdir()
        self._write_wine(engine / "bin/wine", "wine-old\n")
        (engine / "version").write_text("wine 11.0\n", encoding="utf-8")
        prefix = shared / "prefix"
        steamapps = prefix / "drive_c/Program Files (x86)/Steam/steamapps"
        steamapps.mkdir(parents=True)
        (steamapps.parent / "steam.exe").write_bytes(b"MZ")
        (steamapps / "appmanifest_3450310.acf").write_text(
            '"AppState"\n{\n\t"appid"\t"3450310"\n\t"StateFlags"\t"4"\n\t"buildid"\t"24187685"\n}\n',
            encoding="utf-8",
        )
        game = steamapps / "common/Europa Universalis V"
        (game / "binaries").mkdir(parents=True)
        (game / "clausewitz/loading_screen").mkdir(parents=True)
        (game / "clausewitz/loading_screen/compound_settings.txt").write_text("fixture\n", encoding="utf-8")
        self._write_pe(game / "binaries/eu5.exe")
        for name in ("system.reg", "user.reg", "userdef.reg"):
            (prefix / name).write_text(f"fixture-{name}\n", encoding="utf-8")

        self.archive = root / "wine-devel-11.13-osx64.tar.xz"
        source = root / "archive-source/Wine Devel.app/Contents/Resources/wine"
        (source / "bin").mkdir(parents=True)
        (source / "lib").mkdir()
        (source / "share").mkdir()
        self._write_wine(source / "bin/wine", "wine-11.13\n")
        (source / "version").write_text("wine-11.13\n", encoding="utf-8")
        with tarfile.open(self.archive, "w:xz") as archive:
            archive.add(source.parents[3], arcname="Wine Devel.app")
        self.archive_sha256 = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.process_patch = mock.patch.object(eu5_mac, "detect_running_processes", return_value=[])
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _write_wine(path: Path, output: str) -> None:
        path.write_text(f"#!/bin/sh\nprintf '%s' '{output.rstrip()}\\n'\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def _write_pe(path: Path) -> None:
        data = bytearray(0x100)
        data[:2] = b"MZ"
        data[0x3C:0x40] = (0x80).to_bytes(4, "little")
        data[0x80:0x84] = b"PE\0\0"
        data[0x84:0x86] = (0x8664).to_bytes(2, "little")
        path.write_bytes(data)

    def test_default_is_read_only_inspect(self) -> None:
        plist_before = (self.wrapper / "Contents/Info.plist").read_bytes()
        version_before = (self.wrapper / "Contents/SharedSupport/wine/version").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = eu5_mac.main(["--wrapper", str(self.wrapper)])
        self.assertEqual(result, 0)
        self.assertIn("default_action_is_read_only", output.getvalue())
        self.assertEqual(plist_before, (self.wrapper / "Contents/Info.plist").read_bytes())
        self.assertEqual(version_before, (self.wrapper / "Contents/SharedSupport/wine/version").read_bytes())

    def test_inspect_reports_template_and_eu5(self) -> None:
        report = eu5_mac.inspect_wrapper(self.wrapper)
        self.assertTrue(report["template_version_supported"])
        self.assertTrue(report["moltenvk_icd_present"])
        self.assertTrue(report["moltenvk_1_4_1_evidence"])
        self.assertTrue(report["eu5"]["installed"])
        self.assertEqual(report["eu5"]["buildid"], "24187685")
        self.assertEqual(report["eu5"]["pe_machine"], "AMD64")

    def test_running_wrapper_refuses_apply(self) -> None:
        with mock.patch.object(eu5_mac, "detect_running_processes", return_value=[eu5_mac.ProcessInfo(9, "eu5.exe")]):
            with self.assertRaises(eu5_mac.RunningWrapperError):
                eu5_mac.apply_wrapper(
                    self.wrapper,
                    self.archive,
                    expected_sha256=self.archive_sha256,
                    expected_size=self.archive.stat().st_size,
                )

    def test_archive_hash_and_traversal_are_rejected(self) -> None:
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.validate_archive(
                self.archive,
                expected_sha256="0" * 64,
                expected_size=self.archive.stat().st_size,
            )
        bad = Path(self.tempdir.name) / "bad.tar.xz"
        with tarfile.open(bad, "w:xz") as archive:
            info = tarfile.TarInfo("../escape")
            payload = b"unsafe"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(bad.read_bytes()).hexdigest()
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.validate_archive(bad, expected_sha256=digest, expected_size=bad.stat().st_size)

    def test_apply_and_restore_preserve_unrelated_data(self) -> None:
        plist_path = self.wrapper / "Contents/Info.plist"
        prefix = self.wrapper / "Contents/SharedSupport/prefix"
        registry_before = {name: (prefix / name).read_bytes() for name in ("system.reg", "user.reg", "userdef.reg")}
        before_plist = plistlib.loads(plist_path.read_bytes())
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        self.assertEqual(result["status"], "applied")
        backup = Path(result["backup"])
        self.assertEqual(eu5_mac._engine_version(self.wrapper / "Contents/SharedSupport/wine"), "wine-11.13")
        applied_plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(applied_plist["UnrelatedSetting"], "keep-me")
        self.assertEqual(applied_plist["LSEnvironment"]["KEEP_ME"], "yes")
        self.assertEqual(applied_plist["Program Flags"], eu5_mac.EXPECTED_FLAGS)
        self.assertTrue((backup / "engine-original/version").is_file())
        self.assertEqual(registry_before, {name: (prefix / name).read_bytes() for name in registry_before})

        restored = eu5_mac.restore_wrapper(self.wrapper, backup)
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(eu5_mac._engine_version(self.wrapper / "Contents/SharedSupport/wine"), "wine 11.0")
        self.assertEqual(plistlib.loads(plist_path.read_bytes()), before_plist)
        self.assertEqual(registry_before, {name: (prefix / name).read_bytes() for name in registry_before})

    def test_failed_apply_rolls_back_engine_and_plist(self) -> None:
        plist_path = self.wrapper / "Contents/Info.plist"
        before_plist = plist_path.read_bytes()
        before_version = (self.wrapper / "Contents/SharedSupport/wine/version").read_bytes()
        with mock.patch.object(eu5_mac, "_desired_plist", side_effect=RuntimeError("fixture failure")):
            with self.assertRaises(eu5_mac.Eu5MacError):
                eu5_mac.apply_wrapper(
                    self.wrapper,
                    self.archive,
                    expected_sha256=self.archive_sha256,
                    expected_size=self.archive.stat().st_size,
                )
        self.assertEqual(plist_path.read_bytes(), before_plist)
        self.assertEqual((self.wrapper / "Contents/SharedSupport/wine/version").read_bytes(), before_version)

    def test_relative_wrapper_symlink_is_rejected(self) -> None:
        alias = Path(self.tempdir.name) / "relative-wrapper.app"
        alias.symlink_to(self.wrapper, target_is_directory=True)
        old_cwd = Path.cwd()
        os.chdir(self.tempdir.name)
        self.addCleanup(lambda: os.chdir(old_cwd))
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.resolve_wrapper("relative-wrapper.app")

    def test_private_backup_modes_and_invalid_placement(self) -> None:
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        backup = Path(result["backup"])
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((backup / "Info.plist").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((backup / "BACKUP_MANIFEST.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((backup / "prefix-registry").stat().st_mode), 0o700)
        for path in (backup / "prefix-registry").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

        inside = self.wrapper / "private-backups"
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.apply_wrapper(
                self.wrapper,
                self.archive,
                backup_dir=inside,
                expected_sha256=self.archive_sha256,
                expected_size=self.archive.stat().st_size,
            )
        linked_root = Path(self.tempdir.name) / "linked-backups"
        real_root = Path(self.tempdir.name) / "real-backups"
        real_root.mkdir()
        linked_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.apply_wrapper(
                self.wrapper,
                self.archive,
                backup_dir=linked_root,
                expected_sha256=self.archive_sha256,
                expected_size=self.archive.stat().st_size,
            )

    def test_existing_backup_parent_is_not_chmodded(self) -> None:
        parent = Path(self.tempdir.name) / "Downloads"
        parent.mkdir(mode=0o755)
        os.chmod(parent, 0o755)
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            backup_dir=parent,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)
        private_root = parent / ".eu5-mac-backups"
        self.assertEqual(Path(result["backup"]).parent, private_root.resolve())
        self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)

    def test_invalid_backup_mode_is_not_chmodded(self) -> None:
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        backup = Path(result["backup"])
        os.chmod(backup, 0o755)
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.restore_wrapper(self.wrapper, backup)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o755)

    def test_restore_cli_rejects_backup_symlink_before_resolution(self) -> None:
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        alias = Path(self.tempdir.name) / "backup-alias"
        alias.symlink_to(Path(result["backup"]), target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()):
            status = eu5_mac.main(
                ["--wrapper", str(self.wrapper), "restore", "--backup", str(alias)]
            )
        self.assertEqual(status, 2)
        self.assertTrue(alias.is_symlink())

    def test_restore_rejects_wrong_wrapper(self) -> None:
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        other = Path(self.tempdir.name) / "Other.app"
        shutil.copytree(self.wrapper, other, symlinks=True)
        before = (other / "Contents/SharedSupport/wine/version").read_bytes()
        with self.assertRaises(eu5_mac.Eu5MacError):
            eu5_mac.restore_wrapper(other, Path(result["backup"]))
        self.assertEqual((other / "Contents/SharedSupport/wine/version").read_bytes(), before)

    def test_restore_plist_swap_failure_restores_engine_and_plist(self) -> None:
        result = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        plist_path = self.wrapper / "Contents/Info.plist"
        before_plist = plist_path.read_bytes()
        before_engine = (self.wrapper / "Contents/SharedSupport/wine/version").read_bytes()
        real_replace = eu5_mac.os.replace
        failed = {"done": False}

        def fail_plist_once(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
            if Path(source).name == "Info.plist.new" and not failed["done"]:
                failed["done"] = True
                raise OSError("fixture plist swap failure")
            real_replace(source, destination)

        with mock.patch.object(eu5_mac.os, "replace", side_effect=fail_plist_once):
            with self.assertRaises(eu5_mac.Eu5MacError):
                eu5_mac.restore_wrapper(self.wrapper, Path(result["backup"]))
        self.assertEqual(plist_path.read_bytes(), before_plist)
        self.assertEqual((self.wrapper / "Contents/SharedSupport/wine/version").read_bytes(), before_engine)

    def test_process_inspection_failure_blocks_before_backup(self) -> None:
        plist_path = self.wrapper / "Contents/Info.plist"
        before_plist = plist_path.read_bytes()
        before_engine = (self.wrapper / "Contents/SharedSupport/wine/version").read_bytes()
        with mock.patch.object(eu5_mac, "detect_running_processes", side_effect=eu5_mac.Eu5MacError("ps failed")):
            with self.assertRaises(eu5_mac.Eu5MacError):
                eu5_mac.apply_wrapper(
                    self.wrapper,
                    self.archive,
                    expected_sha256=self.archive_sha256,
                    expected_size=self.archive.stat().st_size,
                )
        self.assertEqual(plist_path.read_bytes(), before_plist)
        self.assertEqual((self.wrapper / "Contents/SharedSupport/wine/version").read_bytes(), before_engine)

    def test_special_file_link_descendant_and_duplicate_archive_members_rejected(self) -> None:
        root = "Wine Devel.app/Contents/Resources/wine"
        variants = []
        special = Path(self.tempdir.name) / "special.tar.xz"
        with tarfile.open(special, "w:xz") as archive:
            info = tarfile.TarInfo(f"{root}/bin/wine")
            info.type = tarfile.FIFOTYPE
            archive.addfile(info)
        variants.append(special)

        link_descendant = Path(self.tempdir.name) / "link-descendant.tar.xz"
        with tarfile.open(link_descendant, "w:xz") as archive:
            for name in (root, f"{root}/bin", f"{root}/lib", f"{root}/share"):
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            link = tarfile.TarInfo(f"{root}/bin/wine")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside"
            archive.addfile(link)
            payload = tarfile.TarInfo(f"{root}/bin/wine/child")
            payload.size = 1
            archive.addfile(payload, io.BytesIO(b"x"))
        variants.append(link_descendant)

        duplicate = Path(self.tempdir.name) / "duplicate.tar.xz"
        with tarfile.open(duplicate, "w:xz") as archive:
            for name in (root, f"{root}/bin", f"{root}/lib", f"{root}/share", f"{root}/bin/wine"):
                info = tarfile.TarInfo(name)
                if name.endswith("/bin/wine"):
                    info.size = 1
                    archive.addfile(info, io.BytesIO(b"x"))
                else:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
            duplicate_info = tarfile.TarInfo(f"./{root}/bin/wine")
            duplicate_info.size = 1
            archive.addfile(duplicate_info, io.BytesIO(b"x"))
        variants.append(duplicate)

        for archive_path in variants:
            with self.subTest(archive=archive_path.name):
                digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                with self.assertRaises(eu5_mac.Eu5MacError):
                    eu5_mac.validate_archive(
                        archive_path,
                        expected_sha256=digest,
                        expected_size=archive_path.stat().st_size,
                    )

    def test_apply_is_idempotent_when_already_configured(self) -> None:
        first = eu5_mac.apply_wrapper(
            self.wrapper,
            self.archive,
            expected_sha256=self.archive_sha256,
            expected_size=self.archive.stat().st_size,
        )
        plist_before = (self.wrapper / "Contents/Info.plist").read_bytes()
        second = eu5_mac.apply_wrapper(self.wrapper, None)
        self.assertEqual(second["status"], "already_configured")
        self.assertEqual((self.wrapper / "Contents/Info.plist").read_bytes(), plist_before)
        self.assertTrue(Path(first["backup"]).is_dir())


class DownloadFixture(unittest.TestCase):
    _write_wine = staticmethod(WrapperFixture._write_wine)
    _write_pe = staticmethod(WrapperFixture._write_pe)

    class Response:
        def __init__(self, payload: bytes) -> None:
            self.stream = io.BytesIO(payload)

        def read(self, size: int = -1) -> bytes:
            return self.stream.read(size)

        def close(self) -> None:
            self.stream.close()

    def setUp(self) -> None:
        WrapperFixture.setUp(self)
        self.download_size = self.archive.stat().st_size
        self.download_hash = self.archive_sha256

    def tearDown(self) -> None:
        WrapperFixture.tearDown(self)

    def test_downloads_verified_archive(self) -> None:
        destination = Path(self.tempdir.name) / "cache/wine-devel-11.13-osx64.tar.xz"
        with mock.patch.object(eu5_mac, "PINNED_WINE_SIZE", self.download_size), mock.patch.object(
            eu5_mac, "PINNED_WINE_SHA256", self.download_hash
        ), mock.patch.object(
            eu5_mac.urllib.request, "urlopen", return_value=self.Response(self.archive.read_bytes())
        ) as open_mock:
            result = eu5_mac.download_archive(destination=destination)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(destination.read_bytes(), self.archive.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        open_mock.assert_called_once()

    def test_download_mismatch_leaves_no_final_file(self) -> None:
        destination = Path(self.tempdir.name) / "cache/wine-devel-11.13-osx64.tar.xz"
        with mock.patch.object(eu5_mac, "PINNED_WINE_SIZE", self.download_size), mock.patch.object(
            eu5_mac, "PINNED_WINE_SHA256", "0" * 64
        ), mock.patch.object(
            eu5_mac.urllib.request, "urlopen", return_value=self.Response(self.archive.read_bytes())
        ):
            with self.assertRaises(eu5_mac.Eu5MacError):
                eu5_mac.download_archive(destination=destination)
        self.assertFalse(destination.exists())

    def test_download_reuses_verified_cache_without_network(self) -> None:
        destination = Path(self.tempdir.name) / "cache/wine-devel-11.13-osx64.tar.xz"
        destination.parent.mkdir()
        destination.write_bytes(self.archive.read_bytes())
        before = destination.read_bytes()
        with mock.patch.object(eu5_mac, "PINNED_WINE_SIZE", self.download_size), mock.patch.object(
            eu5_mac, "PINNED_WINE_SHA256", self.download_hash
        ), mock.patch.object(eu5_mac.urllib.request, "urlopen") as open_mock:
            result = eu5_mac.download_archive(destination=destination)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(destination.read_bytes(), before)
        open_mock.assert_not_called()

    def test_download_cli_returns_success_without_wrapper(self) -> None:
        destination = Path(self.tempdir.name) / "cli-cache/wine-devel-11.13-osx64.tar.xz"
        output = io.StringIO()
        with mock.patch.object(eu5_mac, "PINNED_WINE_SIZE", self.download_size), mock.patch.object(
            eu5_mac, "PINNED_WINE_SHA256", self.download_hash
        ), mock.patch.object(
            eu5_mac.urllib.request, "urlopen", return_value=self.Response(self.archive.read_bytes())
        ):
            with contextlib.redirect_stdout(output):
                result = eu5_mac.main(["download", "--destination", str(destination)])
        self.assertEqual(result, 0)
        self.assertIn("status: downloaded", output.getvalue())
        self.assertTrue(destination.is_file())


if __name__ == "__main__":
    unittest.main()
