import copy
import hashlib
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("package_release", ROOT / "eng/package-release.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseArchiveTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(MODULE.pins())
        self.commit = "a" * 40
        self.binary = bytearray(128)
        self.binary[:2] = b"MZ"
        self.binary[0x3c:0x40] = (64).to_bytes(4, "little")
        self.binary[64:68] = b"PE\0\0"
        self.binary[68:70] = (0xAA64).to_bytes(2, "little")
        self.binary = bytes(self.binary)
        self.receipt = {
            "schemaVersion": 1,
            "hostRid": "win-arm64",
            "sourceCommit": self.commit,
            "llvmCommit": self.config["llvmLld"]["commit"],
            "sourceArchiveSha256": "b" * 64,
            "sourceTreeSha256": self.config["hostArtifacts"]["hosts"]["win-arm64"]["llvmSource"]["treeSha256"],
            "outputSha256": MODULE.sha256(self.binary),
            "outputSize": len(self.binary),
            "cmakeFlags": self.config["hostArtifacts"]["winArm64LldCMakeFlags"],
            "compilerVersion": "14.51.36231",
            "windowsSdkVersion": "10.0.26100.0",
            "cmakeVersion": "4.4.3",
            "ninjaVersion": "1.13.2",
        }
        self.notices = {
            "LLVM-LICENSE.txt": (ROOT / "LICENSE.txt").read_bytes(),
            "LLD-LICENSE.txt": b"pinned LLD notice",
            "LLVM-BLAKE3-LICENSE": b"pinned BLAKE3 notice",
            "LLVM-ThirdParty-NOTICES.txt": b"pinned embedded third-party notices",
        }
        sources = self.config["hostArtifacts"]["licenseSources"]
        for name, key in MODULE.NOTICE_SOURCES.items():
            sources[key]["sha256"] = MODULE.sha256(self.notices[name])
        sources["llvmThirdPartyNoticeSha256"] = MODULE.sha256(
            self.notices["LLVM-ThirdParty-NOTICES.txt"])

    def test_round_trip_preserves_pinned_payload_and_notices(self):
        archive = MODULE.archive_bytes(self.binary, self.receipt, self.notices)
        self.assertEqual(self.receipt,
                         MODULE.verify_archive(archive, self.config, self.commit))

    def test_repository_license_matches_upstream_pin(self):
        self.assertEqual(MODULE.pins()["hostArtifacts"]["licenseSources"]["llvm"]["sha256"],
                         hashlib.sha256((ROOT / "LICENSE.txt").read_bytes()).hexdigest())

    def test_tampered_executable_or_receipt_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "receipt"):
            MODULE.validate_receipt(self.receipt, self.binary + b"x", self.config, self.commit)
        stale = dict(self.receipt, sourceCommit="c" * 40)
        with self.assertRaisesRegex(ValueError, "receipt"):
            MODULE.verify_archive(
                MODULE.archive_bytes(self.binary, stale, self.notices),
                self.config, self.commit)

    def test_wrong_architecture_or_notice_is_rejected(self):
        wrong_arch = bytearray(self.binary)
        wrong_arch[68:70] = (0x8664).to_bytes(2, "little")
        with self.assertRaisesRegex(ValueError, "ARM64"):
            MODULE.validate_receipt(self.receipt, bytes(wrong_arch), self.config, self.commit)
        wrong = dict(self.notices, **{"LLD-LICENSE.txt": b"wrong license"})
        with self.assertRaisesRegex(ValueError, "LLD-LICENSE"):
            MODULE.verify_archive(
                MODULE.archive_bytes(self.binary, self.receipt, wrong),
                self.config, self.commit)
        wrong = dict(self.notices, **{"LLVM-ThirdParty-NOTICES.txt": b"wrong notices"})
        with self.assertRaisesRegex(ValueError, "third-party notice"):
            MODULE.verify_archive(
                MODULE.archive_bytes(self.binary, self.receipt, wrong),
                self.config, self.commit)

    def test_pathful_tool_version_is_rejected(self):
        pathful = dict(self.receipt, compilerVersion="C:/build/compiler.exe")
        with self.assertRaisesRegex(ValueError, "path-free"):
            MODULE.validate_receipt(pathful, self.binary, self.config, self.commit)


if __name__ == "__main__":
    unittest.main()
