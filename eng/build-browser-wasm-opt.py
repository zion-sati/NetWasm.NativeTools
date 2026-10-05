#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
"""Build the pinned multithreaded browser wasm-opt and write a path-free receipt."""

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
PINS_PATH = ROOT / "eng/browser-wasm-opt.json"
USER_HOME = re.compile(rb"(?:[A-Za-z]:\\Users\\|/Users/)", re.IGNORECASE)
ENV_ORIGINAL = "var ENV={};"
ENV_ADAPTED = 'var ENV=Module["environment"]||{};'
WORKER_ORIGINAL = (
    'new Worker(new URL("wasm-opt.js",import.meta.url),'
    '{type:"module",name:"em-pthread"})'
)
WORKER_ADAPTED = (
    'new Worker(Module["pthreadWorkerUrl"]||'
    'new URL("wasm-opt.js",import.meta.url),'
    '{type:"module",name:"em-pthread"})'
)
WORKER_CREATION_ORIGINAL = WORKER_ORIGINAL + ";PThread.unusedWorkers.push(worker)"
WORKER_CREATION_ADAPTED = (
    WORKER_ADAPTED +
    ';Module["onPthreadWorker"]?.(worker);PThread.unusedWorkers.push(worker)'
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pins() -> dict[str, object]:
    result = json.loads(PINS_PATH.read_text(encoding="utf-8"))
    upstream = result.get("upstream")
    build = result.get("build")
    if result.get("schemaVersion") != 1 or \
            not isinstance(upstream, dict) or not isinstance(build, dict) or \
            not re.fullmatch(r"[0-9a-f]{40}", upstream.get("revision", "")) or \
            not re.fullmatch(r"[0-9a-f]{64}", upstream.get("sourceArchiveSha256", "")) or \
            not re.fullmatch(r"[0-9a-f]{64}", upstream.get("sourceTreeSha256", "")) or \
            not re.fullmatch(r"[0-9a-f]{40}", build.get("emsdkRevision", "")) or \
            not re.fullmatch(r"[0-9a-f]{40}", build.get("emscriptenRevision", "")) or \
            build.get("pthreadPoolBuildSize") != build.get("maximumWorkerCount") or \
            not isinstance(build.get("pthreadPoolBuildSize"), int) or \
            build["pthreadPoolBuildSize"] < 2 or \
            not isinstance(build.get("reservedProcessorCount"), int) or \
            build["reservedProcessorCount"] < 1 or \
            build.get("binaryenCoresEnvironmentVariable") != "BINARYEN_CORES" or \
            not isinstance(build.get("runtimeWorkerCountExpression"), str):
        raise ValueError("Invalid browser wasm-opt source or build pins.")
    return result


def acquire_source(source: dict[str, str], cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{source['sourceArchiveSha256'][:16]}-binaryen.tar.gz"
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
        raise ValueError("Pinned Binaryen source archive digest mismatch.")
    return target


def extract_source(archive_path: Path, destination: Path, revision: str) -> Path:
    top = f"binaryen-{revision}"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = list(archive)
        if not members or any(
            member.name != top and not member.name.startswith(f"{top}/")
            for member in members
        ):
            raise ValueError("Pinned Binaryen source archive has an unexpected root.")
        archive.extractall(destination, members=members, filter="data")
    source = destination / top
    if not (source / "CMakeLists.txt").is_file() or \
            not (source / "src/passes/pass.cpp").is_file() or \
            not (source / "src/support/threads.cpp").is_file():
        raise ValueError("Pinned Binaryen source archive is incomplete.")
    return source


def source_tree_sha256(source: Path) -> str:
    files = []
    for entry in source.rglob("*"):
        if entry.is_symlink():
            raise ValueError("Pinned Binaryen source contains a symbolic link.")
        if entry.is_file():
            files.append(entry)
        elif not entry.is_dir():
            raise ValueError("Pinned Binaryen source contains a non-regular entry.")
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


def first_line(command: list[str], environment: dict[str, str] | None = None) -> str:
    return subprocess.check_output(
        command, env=environment, text=True, errors="replace",
        stderr=subprocess.STDOUT,
    ).splitlines()[0]


def require_tools(config: dict[str, object], environment: dict[str, str]) -> dict[str, str]:
    emcc_version = first_line(["emcc", "--version"], environment)
    match = re.fullmatch(
        r"emcc \(Emscripten gcc/clang-like replacement \+ linker emulating GNU ld\) "
        r"([^ ]+) \(([0-9a-f]{40})\)", emcc_version)
    if match is None or match.groups() != (
        config["emscriptenVersion"], config["emscriptenRevision"]
    ):
        raise ValueError("Emscripten version does not match the pin.")
    cmake_version = first_line(["cmake", "--version"], environment)
    if cmake_version != f"cmake version {config['cmakeVersion']}":
        raise ValueError("CMake version does not match the pin.")
    ninja_version = first_line(["ninja", "--version"], environment)
    if ninja_version != config["ninjaVersion"]:
        raise ValueError("Ninja version does not match the pin.")
    return {
        "emccVersion": emcc_version,
        "cmakeVersion": cmake_version,
        "ninjaVersion": ninja_version,
    }


def adapt_javascript(source: str, build_config: dict[str, object]) -> str:
    pool_original = f"var pthreadPoolSize={build_config['pthreadPoolBuildSize']};"
    pool_adapted = f"var pthreadPoolSize={build_config['runtimeWorkerCountExpression']};"
    if source.count(ENV_ORIGINAL) != 1 or \
            source.count(WORKER_CREATION_ORIGINAL) != 1 or \
            source.count(pool_original) != 1:
        raise ValueError("Generated wasm-opt JavaScript no longer matches adaptation v1.")
    result = source.replace(ENV_ORIGINAL, ENV_ADAPTED, 1)
    result = result.replace(WORKER_CREATION_ORIGINAL, WORKER_CREATION_ADAPTED, 1)
    result = result.replace(pool_original, pool_adapted, 1)
    if ENV_ORIGINAL in result or WORKER_ORIGINAL in result or \
            result.count(WORKER_CREATION_ADAPTED) != 1 or \
            result.count(pool_adapted) != 1:
        raise ValueError("Generated wasm-opt JavaScript adaptation was incomplete.")
    return result


def read_uleb(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift >= 35:
            raise ValueError("Invalid Wasm unsigned LEB128 value.")
        current = data[offset]
        offset += 1
        value |= (current & 0x7f) << shift
        if current & 0x80 == 0:
            return value, offset
        shift += 7


def skip_name(data: bytes, offset: int) -> int:
    length, offset = read_uleb(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("Invalid Wasm name.")
    return end


def read_limits(data: bytes, offset: int) -> tuple[int, int]:
    flags, offset = read_uleb(data, offset)
    _, offset = read_uleb(data, offset)
    if flags & 1:
        _, offset = read_uleb(data, offset)
    return flags, offset


def has_shared_memory(data: bytes) -> bool:
    if data[:8] != b"\0asm\x01\0\0\0":
        return False
    offset = 8
    while offset < len(data):
        section_id = data[offset]
        size, content = read_uleb(data, offset + 1)
        end = content + size
        if end > len(data):
            raise ValueError("Invalid Wasm section length.")
        if section_id == 2:
            count, cursor = read_uleb(data, content)
            for _ in range(count):
                cursor = skip_name(data, cursor)
                cursor = skip_name(data, cursor)
                kind = data[cursor]
                cursor += 1
                if kind == 0:
                    _, cursor = read_uleb(data, cursor)
                elif kind == 1:
                    cursor += 1
                    _, cursor = read_limits(data, cursor)
                elif kind == 2:
                    flags, cursor = read_limits(data, cursor)
                    if flags & 2:
                        return True
                elif kind == 3:
                    cursor += 2
                elif kind == 4:
                    _, cursor = read_uleb(data, cursor)
                    _, cursor = read_uleb(data, cursor)
                else:
                    raise ValueError("Unsupported Wasm import kind.")
        elif section_id == 5:
            count, cursor = read_uleb(data, content)
            for _ in range(count):
                flags, cursor = read_limits(data, cursor)
                if flags & 2:
                    return True
        offset = end
    return False


def assert_path_free(data: bytes, forbidden: list[Path]) -> None:
    if USER_HOME.search(data):
        raise ValueError("Browser wasm-opt output contains a user home-directory path.")
    for path in forbidden:
        spellings = {str(path), str(path.resolve())}
        forms = {
            value.encode()
            for spelling in spellings
            for value in (spelling, spelling.replace("\\", "/"),
                          spelling.replace("/", "\\"))
        }
        if any(value and value in data for value in forms):
            raise ValueError("Browser wasm-opt output contains a build-directory path.")


def normalized_configure_arguments(pool_size: int) -> list[str]:
    return [
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_TESTS=OFF",
        "-DBUILD_TOOLS=ON",
        "-DINSTALL_LIBS=OFF",
        "-DBUILD_FOR_BROWSER=ON",
        "-DEMSCRIPTEN_ENABLE_PTHREADS=ON",
        "-DEMSCRIPTEN_ENABLE_SINGLE_FILE=OFF",
        "-DBYN_ENABLE_LTO=ON",
        "-DCMAKE_C_FLAGS=-ffile-prefix-map=<SOURCE>=/_/binaryen -ffile-prefix-map=<BUILD>=/_/build",
        "-DCMAKE_CXX_FLAGS=-ffile-prefix-map=<SOURCE>=/_/binaryen -ffile-prefix-map=<BUILD>=/_/build",
        f"-DCMAKE_EXE_LINKER_FLAGS=-sPTHREAD_POOL_SIZE={pool_size}",
    ]


def build(output: Path, receipt_path: Path, cache: Path) -> None:
    if output.exists() or receipt_path.exists():
        raise ValueError("Browser wasm-opt build outputs must not already exist.")
    config = pins()
    upstream = config["upstream"]
    build_config = config["build"]
    license_config = config["license"]
    if sha256(ROOT / license_config["path"]) != license_config["sha256"]:
        raise ValueError("Binaryen license digest mismatch.")

    environment = os.environ.copy()
    environment["EMSDK_QUIET"] = "1"
    versions = require_tools(build_config, environment)
    cache.mkdir(parents=True, exist_ok=True)
    archive = acquire_source(upstream, cache / "sources")
    with tempfile.TemporaryDirectory(prefix="netwasm-browser-wasm-opt-", dir=cache) as name:
        temporary = Path(name)
        source = extract_source(archive, temporary / "source", upstream["revision"])
        if source_tree_sha256(source) != upstream["sourceTreeSha256"]:
            raise ValueError("Pinned Binaryen source tree digest mismatch.")
        build_root = temporary / "build"
        remap = (
            f"-ffile-prefix-map={source}=/_/binaryen "
            f"-ffile-prefix-map={build_root}=/_/build"
        )
        configure = [
            "emcmake", "cmake", "-S", str(source), "-B", str(build_root),
            "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_TESTS=OFF",
            "-DBUILD_TOOLS=ON",
            "-DINSTALL_LIBS=OFF",
            "-DBUILD_FOR_BROWSER=ON",
            "-DEMSCRIPTEN_ENABLE_PTHREADS=ON",
            "-DEMSCRIPTEN_ENABLE_SINGLE_FILE=OFF",
            "-DBYN_ENABLE_LTO=ON",
            f"-DCMAKE_C_FLAGS={remap}",
            f"-DCMAKE_CXX_FLAGS={remap}",
            f"-DCMAKE_EXE_LINKER_FLAGS=-sPTHREAD_POOL_SIZE={build_config['pthreadPoolBuildSize']}",
        ]
        subprocess.run(configure, env=environment, check=True)
        subprocess.run(
            ["cmake", "--build", str(build_root), "--target", build_config["target"],
             "--parallel"],
            env=environment,
            check=True,
        )
        built_js = build_root / "bin/wasm-opt.js"
        built_wasm = build_root / "bin/wasm-opt.wasm"
        javascript = adapt_javascript(
            built_js.read_text(encoding="utf-8"), build_config)
        wasm = built_wasm.read_bytes()
        if len(wasm) < 1_000_000 or not has_shared_memory(wasm):
            raise ValueError("Browser wasm-opt is not a substantial threaded Wasm module.")
        javascript_bytes = javascript.encode("utf-8")
        forbidden = [source, build_root, temporary, cache, Path.home()]
        assert_path_free(javascript_bytes, forbidden)
        assert_path_free(wasm, forbidden)
        output.mkdir(parents=True)
        (output / "wasm-opt.js").write_bytes(javascript_bytes)
        (output / "wasm-opt.wasm").write_bytes(wasm)

    source_commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True,
    ).strip()
    receipt = {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "binaryenCommit": upstream["revision"],
        "binaryenVersion": upstream["version"],
        "effectiveVersion": config["effectiveVersion"],
        "sourceArchiveSha256": sha256(archive),
        "sourceTreeSha256": upstream["sourceTreeSha256"],
        "javascriptSha256": hashlib.sha256(javascript_bytes).hexdigest(),
        "javascriptSize": len(javascript_bytes),
        "wasmSha256": hashlib.sha256(wasm).hexdigest(),
        "wasmSize": len(wasm),
        "configureArguments": normalized_configure_arguments(
            build_config["pthreadPoolBuildSize"]),
        "buildTarget": build_config["target"],
        "pthreadPoolBuildSize": build_config["pthreadPoolBuildSize"],
        "maximumWorkerCount": build_config["maximumWorkerCount"],
        "reservedProcessorCount": build_config["reservedProcessorCount"],
        "runtimeWorkerCountExpression": build_config["runtimeWorkerCountExpression"],
        "binaryenCoresEnvironmentVariable": build_config["binaryenCoresEnvironmentVariable"],
        "adaptationVersion": build_config["adaptationVersion"],
        **versions,
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(
        f"Built browser wasm-opt: {len(wasm):,} Wasm bytes and "
        f"{len(javascript_bytes):,} JavaScript bytes; "
        f"maximum pthread pool {receipt['maximumWorkerCount']}"
    )


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
