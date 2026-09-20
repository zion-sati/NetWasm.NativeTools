#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Package and verify one pinned Windows ARM64 wasm-ld release asset."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RECEIPT_FIELDS = {
    "schemaVersion", "hostRid", "sourceCommit", "llvmCommit",
    "sourceArchiveSha256", "sourceTreeSha256", "outputSha256", "outputSize",
    "cmakeFlags", "compilerVersion", "windowsSdkVersion", "cmakeVersion",
    "ninjaVersion",
}
ARCHIVE_FILES = ("wasm-ld.exe", "build-receipt.json", "LLVM-LICENSE.txt")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
HEX_40 = re.compile(r"[0-9a-f]{40}\Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pins() -> dict[str, object]:
    result = json.loads((ROOT / "eng/toolchain.json").read_text(encoding="utf-8"))
    if result.get("schemaVersion") != 1 or not re.fullmatch(
        r"lld-v[0-9]+\.[0-9]+\.[0-9]+-win-arm64\.[0-9]+",
        result.get("releaseTag", ""),
    ) or not re.fullmatch(
        r"wasm-ld-[0-9]+\.[0-9]+\.[0-9]+-win-arm64\.[0-9]+\.zip",
        result.get("assetName", ""),
    ):
        raise ValueError("Invalid release identity in eng/toolchain.json.")
    version = result["llvmLld"]["version"]
    if f"lld-v{version}-win-arm64." not in result["releaseTag"] or \
            not result["assetName"].startswith(f"wasm-ld-{version}-win-arm64."):
        raise ValueError("Release tag and asset name must match the pinned LLVM version.")
    return result


def assert_arm64_pe(binary: bytes) -> None:
    if binary[:2] != b"MZ" or len(binary) < 0x40:
        raise ValueError("wasm-ld is not a PE executable.")
    offset = int.from_bytes(binary[0x3c:0x40], "little")
    if offset + 6 > len(binary) or binary[offset:offset + 4] != b"PE\0\0" or \
            int.from_bytes(binary[offset + 4:offset + 6], "little") != 0xAA64:
        raise ValueError("wasm-ld is not native Windows ARM64.")


def validate_receipt(receipt: dict[str, object], binary: bytes,
                     config: dict[str, object], source_commit: str) -> None:
    assert_arm64_pe(binary)
    artifacts = config["hostArtifacts"]
    source = artifacts["hosts"]["win-arm64"]["llvmSource"]
    if set(receipt) != RECEIPT_FIELDS or receipt["schemaVersion"] != 1 or \
            receipt["hostRid"] != "win-arm64" or \
            receipt["sourceCommit"] != source_commit or \
            receipt["llvmCommit"] != config["llvmLld"]["commit"] or \
            receipt["sourceTreeSha256"] != source["treeSha256"] or \
            receipt["cmakeFlags"] != artifacts["winArm64LldCMakeFlags"] or \
            receipt["outputSha256"] != sha256(binary) or \
            receipt["outputSize"] != len(binary) or \
            not isinstance(receipt["sourceArchiveSha256"], str) or \
            not HEX_64.fullmatch(receipt["sourceArchiveSha256"]):
        raise ValueError("wasm-ld receipt does not match the pinned source and executable.")
    if not isinstance(source_commit, str) or not HEX_40.fullmatch(source_commit):
        raise ValueError("Build source commit is invalid.")
    for field in ("compilerVersion", "windowsSdkVersion", "cmakeVersion", "ninjaVersion"):
        value = receipt[field]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value):
            raise ValueError(f"Invalid path-free tool version: {field}.")


def get_license(config: dict[str, object]) -> bytes:
    source = config["hostArtifacts"]["licenseSources"]["llvm"]
    request = urllib.request.Request(source["url"], headers={"User-Agent": "NetWasm-NativeTools"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if sha256(data) != source["sha256"]:
        raise ValueError("Pinned LLVM license digest mismatch.")
    return data


def archive_bytes(binary: bytes, receipt: dict[str, object], license_bytes: bytes) -> bytes:
    from io import BytesIO

    stream = BytesIO()
    files = {
        "wasm-ld.exe": binary,
        "build-receipt.json": (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(),
        "LLVM-LICENSE.txt": license_bytes,
    }
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in ARCHIVE_FILES:
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if name == "wasm-ld.exe" else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, files[name], compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)
    return stream.getvalue()


def verify_archive(data: bytes, config: dict[str, object], source_commit: str) -> dict[str, object]:
    from io import BytesIO

    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        if names != list(ARCHIVE_FILES) or archive.testzip() is not None:
            raise ValueError("Release archive inventory or ZIP integrity is invalid.")
        binary = archive.read("wasm-ld.exe")
        receipt = json.loads(archive.read("build-receipt.json"))
        license_bytes = archive.read("LLVM-LICENSE.txt")
    validate_receipt(receipt, binary, config, source_commit)
    if sha256(license_bytes) != config["hostArtifacts"]["licenseSources"]["llvm"]["sha256"]:
        raise ValueError("Release archive LLVM license digest mismatch.")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = pins()
    source_commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    binary = args.binary.read_bytes()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    validate_receipt(receipt, binary, config, source_commit)
    observed_version = subprocess.check_output(
        [str(args.binary), "--version"], text=True, errors="replace").strip()
    if config["llvmLld"]["version"] not in observed_version:
        raise ValueError("wasm-ld version does not match the pin.")
    data = archive_bytes(binary, receipt, get_license(config))
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
