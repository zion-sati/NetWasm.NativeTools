#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Build the pinned patched Jco bindgen Wasm and write a path-free receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "eng/jco-bindgen.json"
USER_HOME = re.compile(rb"(?:[A-Za-z]:\\Users\\|/(?:Users|home)/)", re.IGNORECASE)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pins() -> dict[str, object]:
    result = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    if result.get("schemaVersion") != 1:
        raise ValueError("Unsupported Jco bindgen recipe schema.")
    upstream = result.get("upstream")
    build = result.get("build")
    if not isinstance(upstream, dict) or not isinstance(build, dict) or \
            not re.fullmatch(r"[0-9a-f]{40}", upstream.get("revision", "")) or \
            not re.fullmatch(r"[0-9a-f]{64}", upstream.get("sourceArchiveSha256", "")) or \
            not re.fullmatch(r"[0-9a-f]{64}", upstream.get("sourceTreeSha256", "")):
        raise ValueError("Invalid Jco bindgen source pins.")
    return result


def acquire_source(source: dict[str, str], cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{source['sourceArchiveSha256'][:16]}-jco.tar.gz"
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            partial = Path(stream.name)
            try:
                request = urllib.request.Request(
                    source["sourceArchiveUrl"],
                    headers={"User-Agent": "NetWasm-NativeTools"},
                )
                with urllib.request.urlopen(request, timeout=120) as response:
                    shutil.copyfileobj(response, stream, 1024 * 1024)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        partial.replace(target)
    if sha256(target) != source["sourceArchiveSha256"]:
        target.unlink(missing_ok=True)
        raise ValueError("Pinned Jco source archive digest mismatch.")
    return target


def extract_source(archive_path: Path, destination: Path, revision: str) -> Path:
    top = f"jco-{revision}"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = list(archive)
        if not members or any(
            member.name != top and not member.name.startswith(f"{top}/")
            for member in members
        ):
            raise ValueError("Pinned Jco source archive has an unexpected root.")
        archive.extractall(destination, members=members, filter="data")
    source = destination / top
    if not (source / "Cargo.toml").is_file() or \
            not (source / "pnpm-lock.yaml").is_file() or \
            not (source / "crates/js-component-bindgen").is_dir():
        raise ValueError("Pinned Jco source archive is incomplete.")
    return source


def source_tree_sha256(source: Path) -> str:
    files = []
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Pinned Jco source contains a symbolic link.")
        if entry.is_file():
            files.append(entry)
        elif not entry.is_dir():
            raise ValueError("Pinned Jco source contains a non-regular entry.")
    files.sort(key=lambda entry: entry.relative_to(source).as_posix())
    digest = hashlib.sha256()
    for entry in files:
        relative = entry.relative_to(source).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(entry.stat().st_size.to_bytes(8, "big"))
        with entry.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def require_version(
    command: list[str], expected: str, *, prefix: str = "",
    environment: dict[str, str] | None = None,
) -> str:
    observed = subprocess.check_output(
        command, env=environment, text=True, errors="replace",
        stderr=subprocess.STDOUT,
    ).strip()
    candidate = observed.removeprefix(prefix).split(maxsplit=1)[0]
    if candidate != expected:
        raise ValueError(f"{command[0]} version does not match the pin.")
    return observed.splitlines()[0]


def assert_path_free(binary: bytes, forbidden: list[Path]) -> None:
    if USER_HOME.search(binary):
        raise ValueError("Jco bindgen module contains a user home-directory path.")
    for path in forbidden:
        spellings = {str(path), str(path.resolve())}
        forms = {
            value.encode()
            for spelling in spellings
            for value in (spelling, spelling.replace("\\", "/"),
                          spelling.replace("/", "\\"))
        }
        if any(value and value in binary for value in forms):
            raise ValueError("Jco bindgen module contains a build-directory path.")


def build(output: Path, receipt_path: Path, cache: Path) -> None:
    if output.exists() or receipt_path.exists():
        raise ValueError("Jco bindgen build outputs must not already exist.")
    config = pins()
    upstream = config["upstream"]
    build_config = config["build"]
    patch_config = config["patch"]
    patch_path = ROOT / patch_config["path"]
    if sha256(patch_path) != patch_config["sha256"]:
        raise ValueError("Jco bindgen source patch digest mismatch.")
    license_config = config["license"]
    if sha256(ROOT / license_config["path"]) != license_config["sha256"]:
        raise ValueError("Jco bindgen license digest mismatch.")

    tool_environment = os.environ.copy()
    tool_environment["RUSTUP_TOOLCHAIN"] = build_config["rust"]
    rustc_version = require_version(
        ["rustc", "--version"], build_config["rust"], prefix="rustc ",
        environment=tool_environment)
    cargo_version = require_version(
        ["cargo", "--version"], build_config["rust"], prefix="cargo ",
        environment=tool_environment)
    node_version = require_version(
        ["node", "--version"], build_config["node"], prefix="v")
    pnpm_version = require_version(["pnpm", "--version"], build_config["pnpm"])

    cache.mkdir(parents=True, exist_ok=True)
    archive = acquire_source(upstream, cache / "sources")
    with tempfile.TemporaryDirectory(prefix="netwasm-jco-", dir=cache) as temporary_name:
        temporary = Path(temporary_name)
        source = extract_source(archive, temporary / "source", upstream["revision"])
        if source_tree_sha256(source) != upstream["sourceTreeSha256"]:
            raise ValueError("Pinned Jco source tree digest mismatch.")
        subprocess.run(["git", "apply", "--check", str(patch_path)], cwd=source, check=True)
        subprocess.run(["git", "apply", str(patch_path)], cwd=source, check=True)

        cargo_home = cache / "cargo-home"
        cargo_home.mkdir(parents=True, exist_ok=True)
        remapping = build_config["pathRemapping"]
        rust_flags = [
            f"--remap-path-prefix={source}={remapping['source']}",
            f"--remap-path-prefix={cargo_home}={remapping['cargoHome']}",
        ]
        environment = os.environ.copy()
        environment["CARGO_HOME"] = str(cargo_home)
        environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(rust_flags)
        # The upstream source requests the moving "stable" channel. Override
        # that directory-local file so the receipt's pinned toolchain is the
        # one that actually compiles every workspace and xtask subprocess.
        environment["RUSTUP_TOOLCHAIN"] = build_config["rust"]
        subprocess.run(
            ["pnpm", "install", "--frozen-lockfile"],
            cwd=source,
            env=environment,
            check=True,
        )
        subprocess.run(
            build_config["testCommand"],
            cwd=source,
            env=environment,
            check=True,
        )
        subprocess.run(
            build_config["command"],
            cwd=source,
            env=environment,
            check=True,
        )
        built = source / config["outputPath"]
        binary = built.read_bytes()
        if len(binary) < 1_000_000 or binary[:4] != b"\0asm":
            raise ValueError("Jco bindgen build did not produce the expected Wasm module.")
        assert_path_free(binary, [source, cargo_home, cache, temporary])

        smoke_config = config["smokeTest"]
        smoke_root = temporary / "smoke"
        subprocess.run(
            [
                "node", "packages/jco/dist/jco.js", "transpile",
                smoke_config["fixture"], "--out-dir", str(smoke_root),
                "--name", "smoke", "--instantiation", "async", "--strict",
                "--bindgen-enable-wasm-exnref", "--no-wasi-shim", "--quiet",
            ],
            cwd=source,
            env=environment,
            check=True,
        )
        generated = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(smoke_root.rglob("*.js"))
        )
        if not generated or any(
            marker not in generated
            for marker in smoke_config["requiredJavaScript"]
        ):
            raise ValueError("Patched Jco bindgen transpilation smoke test failed.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(binary)

    source_commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    receipt = {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "jcoCommit": upstream["revision"],
        "effectiveVersion": config["effectiveVersion"],
        "sourceArchiveSha256": sha256(archive),
        "sourceTreeSha256": upstream["sourceTreeSha256"],
        "patchSha256": patch_config["sha256"],
        "outputSha256": hashlib.sha256(binary).hexdigest(),
        "outputSize": len(binary),
        "testCommand": build_config["testCommand"],
        "command": build_config["command"],
        "pathRemapping": remapping,
        "smokeFixture": smoke_config["fixture"],
        "rustcVersion": rustc_version,
        "cargoVersion": cargo_version,
        "nodeVersion": node_version,
        "pnpmVersion": pnpm_version,
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(f"Built patched Jco bindgen Wasm: {len(binary):,} bytes, "
          f"SHA-256 {receipt['outputSha256']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    build(args.output.resolve(), args.receipt.resolve(), args.cache.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
