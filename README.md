# NetWasm NativeTools

Pinned native build recipes and release assets for tools used to build NetWasm applications. The repository builds `wasm-ld` for Windows ARM64 and NetWasm's patched Jco bindgen Wasm on GitHub Actions. Built tools belong in versioned GitHub Releases, not Git.

This repository and its `wasm-ld` release assets use LLVM's [Apache-2.0 license with LLVM Exceptions](LICENSE.txt). Each release archive includes the pinned upstream LLVM and LLD licenses, bundled third-party notices, and a build receipt.

## Windows ARM64 wasm-ld

[`eng/toolchain.json`](eng/toolchain.json) is the one place to update the LLVM revision, selected source-tree digest, build flags, notice digests, and next release tag. The [release workflow](.github/workflows/release.yml) runs only when started manually from `main`. It builds on a native Windows ARM64 runner, verifies the PE machine type and version, packages the executable with its path-free build receipt and pinned notices, checks the uploaded archive, then publishes the GitHub Release. A failed build never publishes a release; a failed upload check leaves a draft for inspection.

The release contains `wasm-ld-<LLVM version>-win-arm64.<build>.zip` and `SHA256SUMS`. Consumers must pin the exact tag, asset name, and archive SHA-256. Do not use `latest` or replace an existing asset. A new LLVM source, build recipe, or Windows toolchain qualification gets a new build revision and release tag.

The receipt records the LLVM commit and selected source-tree digest, the actual downloaded source archive hash, compiler/CMake/Ninja/Windows SDK versions, build flags, builder commit, and executable hash. Separate builds have produced different executable hashes from the same source and toolchain versions, so this repository does not claim byte-for-byte reproducibility. The published release asset is the immutable input for NetWasm's host-tools package.

## Patched Jco bindgen Wasm

[`eng/jco-bindgen.json`](eng/jco-bindgen.json) pins the upstream Jco revision and
source tree, NetWasm patch, license, Rust, Node, pnpm, canonical source paths,
and next release identity. The
[`release-jco-bindgen`](.github/workflows/release-jco-bindgen.yml) workflow runs
only when started manually from `main`. It builds on GitHub's Linux runner,
remaps the checkout and Cargo registry roots before Rust records panic
locations, rejects user-home and build-directory paths in the resulting Wasm,
and packages a path-free build receipt with the patch and upstream license.

The release contains
`jco-bindgen-<Jco version>-netwasm.<patch>.<build>.zip` and `SHA256SUMS`.
Consumers pin the tag, asset name, archive hash, module hash, and builder commit.
A changed source, patch, recipe, or toolchain gets a new immutable build
revision.
