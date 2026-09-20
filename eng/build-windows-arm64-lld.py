#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Build pinned native Windows ARM64 LLD in CI and write a path-free receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LLD_SOURCE_TREES = ("cmake", "libc", "libunwind", "llvm", "lld", "third-party")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acquire_source(source: dict[str, str], cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{source['treeSha256'][:16]}-llvm-project.tar.gz"
    if not target.exists():
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            partial = Path(stream.name)
            try:
                request = urllib.request.Request(
                    source["url"], headers={"User-Agent": "NetWasm-host-tools"})
                with urllib.request.urlopen(request, timeout=120) as response:
                    shutil.copyfileobj(response, stream, 1024 * 1024)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
        partial.replace(target)
    return target


def extract_llvm_and_lld(archive_path: Path, destination: Path, commit: str) -> Path:
    top = f"llvm-project-{commit}"
    with tarfile.open(archive_path, "r:gz") as archive:
        selected = [
            member for member in archive
            if member.name == top or
            any(member.name.startswith(f"{top}/{tree}/")
                for tree in LLD_SOURCE_TREES)
        ]
        if not selected:
            raise ValueError("Pinned LLVM source archive has no LLVM/LLD tree.")
        archive.extractall(destination, members=selected, filter="data")
    source = destination / top
    if not all((source / tree / "CMakeLists.txt").is_file()
               for tree in ("llvm", "lld")) or \
            not (source / "cmake/Modules/LLVMVersion.cmake").is_file():
        raise ValueError("Pinned LLVM source archive is incomplete.")
    return source


def source_tree_sha256(source: Path) -> str:
    """Hash the selected build inputs, independent of archive compression."""
    files = sorted(
        (entry for tree in LLD_SOURCE_TREES
         for entry in (source / tree).rglob("*") if not entry.is_dir()),
        key=lambda entry: entry.relative_to(source).as_posix(),
    )
    digest = hashlib.sha256()
    for entry in files:
        if not entry.is_file():
            raise ValueError("Pinned LLVM source contains an unreadable file.")
        relative = entry.relative_to(source).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(entry.stat().st_size.to_bytes(8, "big"))
        with entry.open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def visual_studio_root() -> Path:
    installer = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    if not installer.is_file():
        raise ValueError("Visual Studio vswhere.exe is required for the CI source build.")
    result = subprocess.check_output(
        [str(installer), "-latest", "-property", "installationPath"], text=True).strip()
    root = Path(result)
    if not result or not root.is_dir():
        raise ValueError("A Visual Studio C++ installation is required for Windows ARM64 LLD.")
    return root


def arm64_build_environment(studio: Path, temporary: Path) -> dict[str, str]:
    vcvars = studio / "VC/Auxiliary/Build/vcvarsall.bat"
    if not vcvars.is_file():
        raise ValueError("Visual Studio is missing vcvarsall.bat.")
    batch = temporary / "arm64-env.cmd"
    batch.write_text(
        f'@echo off\ncall "{vcvars}" arm64 >nul\nif errorlevel 1 exit /b 1\nset\n',
        encoding="utf-8",
    )
    output = subprocess.check_output(
        ["cmd.exe", "/d", "/c", str(batch)], text=True, errors="replace")
    environment = os.environ.copy()
    for line in output.splitlines():
        if "=" in line and not line.startswith("="):
            key, value = line.split("=", 1)
            if key.casefold() == "path":
                for existing in list(environment):
                    if existing.casefold() == "path":
                        del environment[existing]
            environment[key] = value
    if environment.get("VSCMD_ARG_TGT_ARCH", "").lower() != "arm64" or \
            environment.get("VSCMD_ARG_HOST_ARCH", "").lower() != "arm64":
        raise ValueError("Visual Studio did not select HostARM64/ARM64 tools.")
    return environment


def locate_build_tool(name: str, studio: Path, environment: dict[str, str]) -> Path:
    search_path = next(
        (value for key, value in environment.items() if key.casefold() == "path"),
        "",
    )
    from_path = shutil.which(name, path=search_path)
    if from_path is not None:
        return Path(from_path)
    candidates = {
        "cmake": studio / "Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe",
        "ninja": studio / "Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe",
    }
    candidate = candidates.get(name)
    if candidate is not None and candidate.is_file():
        return candidate
    raise ValueError(f"Visual Studio CI image is missing {name}.")


def version(command: list[str], environment: dict[str, str]) -> str:
    output = subprocess.check_output(
        command, env=environment, text=True, errors="replace", stderr=subprocess.STDOUT)
    match = re.search(r"\b(?:Version|version)?\s*([0-9]+(?:\.[0-9]+)+)\b", output)
    if match is None:
        raise ValueError(f"Cannot determine build tool version: {command[0]}")
    return match.group(1)


def assert_native_arm64_pe(binary: bytes) -> None:
    if binary[:2] != b"MZ" or len(binary) < 0x40:
        raise ValueError("LLD build did not produce a PE executable.")
    offset = int.from_bytes(binary[0x3c:0x40], "little")
    if binary[offset:offset + 4] != b"PE\0\0" or \
            int.from_bytes(binary[offset + 4:offset + 6], "little") != 0xAA64:
        raise ValueError("LLD build did not produce native ARM64 machine code.")


def build(output: Path, receipt_path: Path, cache: Path) -> None:
    if platform.system() != "Windows" or not (
        platform.machine().lower() == "arm64" or
        os.environ.get("PROCESSOR_ARCHITEW6432", "").lower() == "arm64"
    ):
        raise ValueError("Native Windows ARM64 CI is required for this build.")
    if output.exists() or receipt_path.exists():
        raise ValueError("LLD build outputs must not already exist.")
    pins = json.loads((ROOT / "eng/toolchain.json").read_text(encoding="utf-8"))
    artifacts = pins["hostArtifacts"]
    source_pin = artifacts["hosts"]["win-arm64"]["llvmSource"]
    cache.mkdir(parents=True, exist_ok=True)
    studio = visual_studio_root()
    with tempfile.TemporaryDirectory(prefix="netwasm-lld-", dir=cache) as temporary_name:
        temporary = Path(temporary_name)
        environment = arm64_build_environment(studio, temporary)
        cmake = locate_build_tool("cmake", studio, environment)
        ninja = locate_build_tool("ninja", studio, environment)
        compiler = locate_build_tool("cl.exe", studio, environment)
        compiler_probe = temporary / "compiler-version.c"
        compiler_probe.write_text("int main(void) { return 0; }\n")
        compiler_version = version(
            [str(compiler), "/nologo", "/Bv", "/c",
             f"/Fo{temporary / 'compiler-version.obj'}", str(compiler_probe)],
            environment,
        )
        cmake_version = version([str(cmake), "--version"], environment)
        ninja_version = version([str(ninja), "--version"], environment)
        for attempt in range(2):
            archive = acquire_source(source_pin, cache)
            try:
                source = extract_llvm_and_lld(
                    archive, temporary / f"source-{attempt}",
                    pins["llvmLld"]["commit"])
                tree_digest = source_tree_sha256(source)
                if tree_digest != source_pin["treeSha256"]:
                    raise ValueError("Pinned LLVM source tree digest mismatch.")
                break
            except (tarfile.TarError, EOFError, OSError, ValueError):
                archive.unlink(missing_ok=True)
                if attempt == 1:
                    raise
        build_root = temporary / "build"
        flags = artifacts["winArm64LldCMakeFlags"]
        subprocess.run(
            [str(cmake), "-S", str(source / "llvm"), "-B", str(build_root),
             "-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={ninja}", *flags],
            check=True, env=environment,
        )
        subprocess.run(
            [str(cmake), "--build", str(build_root), "--target", "lld", "--parallel", "4"],
            check=True, env=environment,
        )
        built = build_root / "bin/lld.exe"
        binary = built.read_bytes()
        assert_native_arm64_pe(binary)
        home_component = b"User" + b"s"
        home_pattern = rb"[A-Za-z]:\\" + home_component + rb"\\|/" + home_component + rb"/"
        if re.search(home_pattern, binary, flags=re.IGNORECASE):
            raise ValueError("LLD executable contains a user home-directory path.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(binary)
        observation = subprocess.check_output(
            [str(output), "--version"], text=True, errors="replace")
        if pins["llvmLld"]["version"] not in observation:
            output.unlink()
            raise ValueError("Native Windows ARM64 LLD version does not match the pin.")
        source_commit = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        receipt = {
            "schemaVersion": 1,
            "hostRid": "win-arm64",
            "sourceCommit": source_commit,
            "llvmCommit": pins["llvmLld"]["commit"],
            "sourceArchiveSha256": sha256(archive),
            "sourceTreeSha256": tree_digest,
            "outputSha256": hashlib.sha256(binary).hexdigest(),
            "outputSize": len(binary),
            "cmakeFlags": flags,
            "compilerVersion": compiler_version,
            "windowsSdkVersion": environment.get("WindowsSDKVersion", "").strip("\\"),
            "cmakeVersion": cmake_version,
            "ninjaVersion": ninja_version,
        }
        if not receipt["windowsSdkVersion"]:
            output.unlink()
            raise ValueError("Visual Studio did not report a Windows SDK version.")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        print(f"Built native Windows ARM64 wasm-ld: {len(binary):,} bytes, "
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
