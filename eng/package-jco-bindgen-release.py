#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Package and verify one pinned patched Jco bindgen release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_NAME = "js-component-bindgen-component.core.wasm"
RECEIPT_NAME = "build-receipt.json"
PATCH_NAME = "jco-worker-bindings.patch"
LICENSE_NAME = "LICENSE.upstream"
ARCHIVE_FILES = (MODULE_NAME, RECEIPT_NAME, PATCH_NAME, LICENSE_NAME)
RECEIPT_FIELDS = {
    "schemaVersion", "sourceCommit", "jcoCommit", "effectiveVersion",
    "sourceArchiveSha256", "sourceTreeSha256", "patchSha256",
    "outputSha256", "outputSize", "testCommand", "command", "pathRemapping",
    "smokeFixture",
    "rustcVersion", "cargoVersion", "nodeVersion", "pnpmVersion",
}
HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pins() -> dict[str, object]:
    result = json.loads((ROOT / "eng/jco-bindgen.json").read_text(encoding="utf-8"))
    if result.get("schemaVersion") != 1 or \
            not re.fullmatch(
                r"jco-bindgen-v[0-9]+\.[0-9]+\.[0-9]+-netwasm\.[0-9]+\.[0-9]+",
                result.get("releaseTag", ""),
            ) or \
            not re.fullmatch(
                r"jco-bindgen-[0-9]+\.[0-9]+\.[0-9]+-netwasm\.[0-9]+\.[0-9]+\.zip",
                result.get("assetName", ""),
            ):
        raise ValueError("Invalid Jco bindgen release identity.")
    return result


def validate_receipt(
    receipt: dict[str, object], binary: bytes, config: dict[str, object],
    source_commit: str,
) -> None:
    upstream = config["upstream"]
    build = config["build"]
    patch = config["patch"]
    if set(receipt) != RECEIPT_FIELDS or receipt["schemaVersion"] != 1 or \
            receipt["sourceCommit"] != source_commit or \
            receipt["jcoCommit"] != upstream["revision"] or \
            receipt["effectiveVersion"] != config["effectiveVersion"] or \
            receipt["sourceArchiveSha256"] != upstream["sourceArchiveSha256"] or \
            receipt["sourceTreeSha256"] != upstream["sourceTreeSha256"] or \
            receipt["patchSha256"] != patch["sha256"] or \
            receipt["outputSha256"] != sha256(binary) or \
            receipt["outputSize"] != len(binary) or \
            receipt["testCommand"] != build["testCommand"] or \
            receipt["command"] != build["command"] or \
            receipt["pathRemapping"] != build["pathRemapping"] or \
            receipt["smokeFixture"] != config["smokeTest"]["fixture"] or \
            not HEX_40.fullmatch(str(source_commit)) or \
            not HEX_64.fullmatch(str(receipt["outputSha256"])) or \
            len(binary) < 1_000_000 or binary[:4] != b"\0asm":
        raise ValueError("Jco bindgen receipt does not match the pinned source and module.")
    escaped_rust = re.escape(build["rust"])
    expected_versions = {
        "rustcVersion": rf"rustc {escaped_rust} \([0-9a-f]{{9,40}} [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\)",
        "cargoVersion": rf"cargo {escaped_rust} \([0-9a-f]{{9,40}} [0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\)",
        "nodeVersion": re.escape(f"v{build['node']}"),
        "pnpmVersion": re.escape(build["pnpm"]),
    }
    for field, pattern in expected_versions.items():
        value = receipt[field]
        if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
            raise ValueError(f"Invalid path-free tool version: {field}.")


def archive_bytes(
    binary: bytes, receipt: dict[str, object], patch: bytes, license_text: bytes,
) -> bytes:
    stream = BytesIO()
    files = {
        MODULE_NAME: binary,
        RECEIPT_NAME: (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(),
        PATCH_NAME: patch,
        LICENSE_NAME: license_text,
    }
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for name in ARCHIVE_FILES:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, files[name], compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)
    return stream.getvalue()


def verify_archive(
    data: bytes, config: dict[str, object], source_commit: str,
) -> dict[str, object]:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        if archive.namelist() != list(ARCHIVE_FILES) or archive.testzip() is not None:
            raise ValueError("Jco bindgen release archive inventory or integrity is invalid.")
        binary = archive.read(MODULE_NAME)
        receipt = json.loads(archive.read(RECEIPT_NAME))
        patch = archive.read(PATCH_NAME)
        license_text = archive.read(LICENSE_NAME)
    validate_receipt(receipt, binary, config, source_commit)
    if sha256(patch) != config["patch"]["sha256"] or \
            sha256(license_text) != config["license"]["sha256"]:
        raise ValueError("Jco bindgen release provenance input digest mismatch.")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = pins()
    source_commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    binary = args.module.read_bytes()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    validate_receipt(receipt, binary, config, source_commit)
    patch = (ROOT / config["patch"]["path"]).read_bytes()
    license_text = (ROOT / config["license"]["path"]).read_bytes()
    data = archive_bytes(binary, receipt, patch, license_text)
    verify_archive(data, config, source_commit)
    args.output.mkdir(parents=True, exist_ok=True)
    asset = args.output / config["assetName"]
    checksums = args.output / "SHA256SUMS"
    if asset.exists() or checksums.exists():
        raise ValueError("Release outputs already exist; never overwrite a release candidate.")
    asset.write_bytes(data)
    checksums.write_text(f"{sha256(data)}  {asset.name}\n", encoding="ascii")
    print(f"Release asset: {asset.name} ({len(data):,} bytes), SHA-256 {sha256(data)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
