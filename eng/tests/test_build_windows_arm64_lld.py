"""Focused checks for the pinned LLVM source build recipe."""

import hashlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_windows_arm64_lld", ROOT / "eng/build-windows-arm64-lld.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SourceBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="native-lld-build-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_compiler_resolves_from_vcvars_path(self):
        compiler = self.root / "cl.exe"
        with patch.object(MODULE.shutil, "which", return_value=str(compiler)) as which:
            resolved = MODULE.locate_build_tool("cl.exe", self.root, {"Path": "C:\\VS\\bin"})
        self.assertEqual(compiler, resolved)
        which.assert_called_once_with("cl.exe", path="C:\\VS\\bin")

    def test_selected_source_tree_is_stable_across_archive_metadata(self):
        commit = "a" * 40
        files = (
            "llvm/CMakeLists.txt",
            "lld/CMakeLists.txt",
            "cmake/Modules/LLVMVersion.cmake",
            "libc/include/support.txt",
            "libunwind/include/mach-o/compact_unwind_encoding.h",
            "third-party/support.txt",
            "clang/unneeded.txt",
        )

        def make_archive(path, ordered_files, mtime):
            with tarfile.open(path, "w:gz") as archive:
                for file in ordered_files:
                    payload = file.encode()
                    member = tarfile.TarInfo(f"llvm-project-{commit}/{file}")
                    member.size = len(payload)
                    member.mtime = mtime
                    archive.addfile(member, io.BytesIO(payload))

        original = self.root / "original.tar.gz"
        reordered = self.root / "reordered.tar.gz"
        make_archive(original, files, 0)
        make_archive(reordered, reversed(files), 123456789)
        first = MODULE.extract_llvm_and_lld(original, self.root / "first", commit)
        second = MODULE.extract_llvm_and_lld(reordered, self.root / "second", commit)
        self.assertNotEqual(hashlib.sha256(original.read_bytes()).hexdigest(),
                            hashlib.sha256(reordered.read_bytes()).hexdigest())
        self.assertEqual(MODULE.source_tree_sha256(first), MODULE.source_tree_sha256(second))
        self.assertTrue((first / "cmake/Modules/LLVMVersion.cmake").is_file())
        self.assertTrue((first / "libc/include/support.txt").is_file())
        self.assertTrue((first / "libunwind/include/mach-o/compact_unwind_encoding.h").is_file())
        self.assertTrue((first / "third-party/support.txt").is_file())
        self.assertFalse((first / "clang/unneeded.txt").exists())


if __name__ == "__main__":
    unittest.main()
