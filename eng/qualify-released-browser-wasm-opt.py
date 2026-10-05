#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Download and verify the pinned browser wasm-opt release for smoke testing."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import urllib.request
import zipfile
from io import BytesIO


ROOT = Path(__file__).resolve().parent.parent


def load_packager():
    path = ROOT / "eng/package-browser-wasm-opt-release.py"
    spec = importlib.util.spec_from_file_location("browser_wasm_opt_packager", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    config = json.loads((ROOT / "eng/browser-wasm-opt.json").read_text())
    release = config["release"]
    request = urllib.request.Request(
        release["url"], headers={"User-Agent": "NetWasm.NativeTools-qualification"})
    with urllib.request.urlopen(request) as response:
        archive = response.read()
    digest = hashlib.sha256(archive).hexdigest()
    if len(archive) != release["bytes"] or digest != release["sha256"]:
        raise ValueError("Released browser wasm-opt archive changed.")

    packager = load_packager()
    receipt = packager.verify_archive(archive, config, release["sourceCommit"])
    args.output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(archive)) as package:
        for name in (packager.JAVASCRIPT_NAME, packager.WASM_NAME):
            (args.output / name).write_bytes(package.read(name))
    print(json.dumps({
        "tag": config["releaseTag"],
        "archiveBytes": len(archive),
        "archiveSha256": digest,
        "javascriptSha256": receipt["javascriptSha256"],
        "wasmSha256": receipt["wasmSha256"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
