"""Focused checks for the patched Jco bindgen build and release recipe."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD = load("build_jco_bindgen", ROOT / "eng/build-jco-bindgen.py")
PACKAGE = load(
    "package_jco_bindgen_release", ROOT / "eng/package-jco-bindgen-release.py")


class JcoBindgenBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jco-bindgen-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_source_tree_digest_ignores_creation_order_and_metadata(self):
        first = self.root / "first"
        second = self.root / "second"
        for root, names in (
            (first, ("Cargo.toml", "crates/a.rs", "packages/b.ts")),
            (second, ("packages/b.ts", "crates/a.rs", "Cargo.toml")),
        ):
            for index, name in enumerate(names):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
                path.touch()
        self.assertEqual(BUILD.source_tree_sha256(first),
                         BUILD.source_tree_sha256(second))

    def test_path_audit_rejects_home_and_build_roots(self):
        BUILD.assert_path_free(b"/_/jco/src/lib.rs\0/_/cargo/src/lib.rs", [])
        with self.assertRaisesRegex(ValueError, "home-directory"):
            BUILD.assert_path_free(b"/home/builder/.cargo/src/lib.rs", [])
        build_root = self.root / "source"
        with self.assertRaisesRegex(ValueError, "build-directory"):
            BUILD.assert_path_free(str(build_root).encode(), [build_root])

    def test_pins_bind_source_patch_license_and_remapping(self):
        config = BUILD.pins()
        self.assertEqual("/_/jco", config["build"]["pathRemapping"]["source"])
        self.assertEqual("/_/cargo",
                         config["build"]["pathRemapping"]["cargoHome"])
        self.assertEqual(config["patch"]["sha256"],
                         BUILD.sha256(ROOT / config["patch"]["path"]))
        self.assertEqual(config["license"]["sha256"],
                         BUILD.sha256(ROOT / config["license"]["path"]))


class JcoBindgenPackageTests(unittest.TestCase):
    def receipt(self, config, binary, source_commit):
        return {
            "schemaVersion": 1,
            "sourceCommit": source_commit,
            "jcoCommit": config["upstream"]["revision"],
            "effectiveVersion": config["effectiveVersion"],
            "sourceArchiveSha256": config["upstream"]["sourceArchiveSha256"],
            "sourceTreeSha256": config["upstream"]["sourceTreeSha256"],
            "patchSha256": config["patch"]["sha256"],
            "outputSha256": PACKAGE.sha256(binary),
            "outputSize": len(binary),
            "testCommand": config["build"]["testCommand"],
            "command": config["build"]["command"],
            "pathRemapping": config["build"]["pathRemapping"],
            "smokeFixture": config["smokeTest"]["fixture"],
            "rustcVersion": "rustc 1.98.1 (48a229cea 2026-09-01)",
            "cargoVersion": "cargo 1.98.1 (797e8a9bc 2026-08-05)",
            "nodeVersion": "v26.7.0",
            "pnpmVersion": "10.17.1",
        }

    def test_archive_round_trips_exact_inventory_and_receipt(self):
        config = PACKAGE.pins()
        source_commit = "a" * 40
        binary = b"\0asm" + b"x" * 1_000_000
        receipt = self.receipt(config, binary, source_commit)
        patch = (ROOT / config["patch"]["path"]).read_bytes()
        license_text = (ROOT / config["license"]["path"]).read_bytes()

        archive = PACKAGE.archive_bytes(binary, receipt, patch, license_text)

        self.assertEqual(
            receipt, PACKAGE.verify_archive(archive, config, source_commit))

    def test_receipt_rejects_a_different_module(self):
        config = PACKAGE.pins()
        source_commit = "b" * 40
        binary = b"\0asm" + b"x" * 1_000_000
        receipt = self.receipt(config, binary, source_commit)

        with self.assertRaisesRegex(ValueError, "receipt"):
            PACKAGE.validate_receipt(
                receipt, binary[:-1] + b"y", config, source_commit)

    def test_receipt_rejects_unpinned_or_pathful_tool_versions(self):
        config = PACKAGE.pins()
        source_commit = "c" * 40
        binary = b"\0asm" + b"x" * 1_000_000
        receipt = self.receipt(config, binary, source_commit)
        receipt["rustcVersion"] = "rustc 0.0.0 /Users/builder/toolchain"

        with self.assertRaisesRegex(ValueError, "tool version"):
            PACKAGE.validate_receipt(receipt, binary, config, source_commit)


if __name__ == "__main__":
    unittest.main()
