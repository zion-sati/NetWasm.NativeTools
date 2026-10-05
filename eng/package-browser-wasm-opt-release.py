#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Package and verify one pinned multithreaded browser wasm-opt release asset."""

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
JAVASCRIPT_NAME = "wasm-opt.js"
WASM_NAME = "wasm-opt.wasm"
RECEIPT_NAME = "build-receipt.json"
LICENSE_NAME = "LICENSE.upstream"
ARCHIVE_FILES = (JAVASCRIPT_NAME, WASM_NAME, RECEIPT_NAME, LICENSE_NAME)
RECEIPT_FIELDS = {
    "schemaVersion", "sourceCommit", "binaryenCommit", "binaryenVersion",
    "effectiveVersion", "sourceArchiveSha256", "sourceTreeSha256",
    "javascriptSha256", "javascriptSize", "wasmSha256", "wasmSize",
    "configureArguments", "buildTarget", "pthreadPoolBuildSize",
    "maximumWorkerCount", "reservedProcessorCount", "runtimeWorkerCountExpression",
    "binaryenCoresEnvironmentVariable", "adaptationVersion", "emccVersion",
    "cmakeVersion", "ninjaVersion",
}
HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pins() -> dict[str, object]:
    result = json.loads(
        (ROOT / "eng/browser-wasm-opt.json").read_text(encoding="utf-8"))
    if result.get("schemaVersion") != 1 or \
            not re.fullmatch(
                r"binaryen-wasm-opt-v[0-9]+-browser\.[0-9]+",
                result.get("releaseTag", ""),
            ) or \
            not re.fullmatch(
                r"binaryen-wasm-opt-[0-9]+-browser\.[0-9]+\.zip",
                result.get("assetName", ""),
            ):
        raise ValueError("Invalid browser wasm-opt release identity.")
    return result


def validate_receipt(
    receipt: dict[str, object], javascript: bytes, wasm: bytes,
    config: dict[str, object], source_commit: str,
) -> None:
    upstream = config["upstream"]
    build = config["build"]
    from importlib import util
    build_path = ROOT / "eng/build-browser-wasm-opt.py"
    spec = util.spec_from_file_location("build_browser_wasm_opt_validation", build_path)
    build_module = util.module_from_spec(spec)
    spec.loader.exec_module(build_module)
    if set(receipt) != RECEIPT_FIELDS or receipt["schemaVersion"] != 1 or \
            receipt["sourceCommit"] != source_commit or \
            receipt["binaryenCommit"] != upstream["revision"] or \
            receipt["binaryenVersion"] != upstream["version"] or \
            receipt["effectiveVersion"] != config["effectiveVersion"] or \
            receipt["sourceArchiveSha256"] != upstream["sourceArchiveSha256"] or \
            receipt["sourceTreeSha256"] != upstream["sourceTreeSha256"] or \
            receipt["javascriptSha256"] != sha256(javascript) or \
            receipt["javascriptSize"] != len(javascript) or \
            receipt["wasmSha256"] != sha256(wasm) or \
            receipt["wasmSize"] != len(wasm) or \
            receipt["configureArguments"] != build_module.normalized_configure_arguments(
                build["pthreadPoolBuildSize"]) or \
            receipt["buildTarget"] != build["target"] or \
            receipt["pthreadPoolBuildSize"] != build["pthreadPoolBuildSize"] or \
            receipt["maximumWorkerCount"] != build["maximumWorkerCount"] or \
            receipt["reservedProcessorCount"] != build["reservedProcessorCount"] or \
            receipt["runtimeWorkerCountExpression"] != build["runtimeWorkerCountExpression"] or \
            receipt["binaryenCoresEnvironmentVariable"] != build["binaryenCoresEnvironmentVariable"] or \
            receipt["adaptationVersion"] != build["adaptationVersion"] or \
            not HEX_40.fullmatch(str(source_commit)) or \
            not HEX_64.fullmatch(str(receipt["javascriptSha256"])) or \
            not HEX_64.fullmatch(str(receipt["wasmSha256"])) or \
            len(javascript) < 10_000 or len(wasm) < 1_000_000 or \
            not build_module.has_shared_memory(wasm):
        raise ValueError("Browser wasm-opt receipt does not match the pinned build outputs.")
    if build_module.ENV_ADAPTED.encode() not in javascript or \
            build_module.WORKER_ADAPTED.encode() not in javascript or \
            build_module.WORKER_CREATION_ADAPTED.encode() not in javascript or \
            f"var pthreadPoolSize={build['runtimeWorkerCountExpression']};".encode() not in javascript:
        raise ValueError("Browser wasm-opt JavaScript lacks the pinned browser adaptation.")
    expected_versions = {
        "emccVersion": (
            "emcc (Emscripten gcc/clang-like replacement + linker emulating GNU ld) "
            f"{build['emscriptenVersion']} ({build['emscriptenRevision']})"
        ),
        "cmakeVersion": f"cmake version {build['cmakeVersion']}",
    }
    if any(receipt[field] != expected for field, expected in expected_versions.items()) or \
            not build_module.is_pinned_ninja_version(
                str(receipt["ninjaVersion"]), build["ninjaVersion"]):
        raise ValueError("Browser wasm-opt tool versions do not match the pins.")


def archive_bytes(
    javascript: bytes, wasm: bytes, receipt: dict[str, object], license_text: bytes,
) -> bytes:
    stream = BytesIO()
    files = {
        JAVASCRIPT_NAME: javascript,
        WASM_NAME: wasm,
        RECEIPT_NAME: (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode(),
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
            raise ValueError("Browser wasm-opt archive inventory or integrity is invalid.")
        javascript = archive.read(JAVASCRIPT_NAME)
        wasm = archive.read(WASM_NAME)
        receipt = json.loads(archive.read(RECEIPT_NAME))
        license_text = archive.read(LICENSE_NAME)
    validate_receipt(receipt, javascript, wasm, config, source_commit)
    if sha256(license_text) != config["license"]["sha256"]:
        raise ValueError("Browser wasm-opt upstream license digest mismatch.")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = pins()
    source_commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    javascript = (args.tool / JAVASCRIPT_NAME).read_bytes()
    wasm = (args.tool / WASM_NAME).read_bytes()
    receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
    validate_receipt(receipt, javascript, wasm, config, source_commit)
    license_text = (ROOT / config["license"]["path"]).read_bytes()
    data = archive_bytes(javascript, wasm, receipt, license_text)
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
