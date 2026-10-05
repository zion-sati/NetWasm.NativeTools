"""Focused checks for the multithreaded browser wasm-opt build and release recipe."""

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


BUILD = load("build_browser_wasm_opt", ROOT / "eng/build-browser-wasm-opt.py")
PACKAGE = load(
    "package_browser_wasm_opt_release",
    ROOT / "eng/package-browser-wasm-opt-release.py",
)


def uleb(value):
    result = bytearray()
    while True:
        current = value & 0x7f
        value >>= 7
        result.append(current | (0x80 if value else 0))
        if not value:
            return bytes(result)


def wasm_with_memory(size=1_000_100, *, shared=True):
    memory_flags = b"\x03" if shared else b"\x01"
    imported_memory = b"\x01\x01a\x01b\x02" + memory_flags + b"\x01\x02"
    module = b"\0asm\x01\0\0\0\x02" + uleb(len(imported_memory)) + imported_memory
    padding = b"\0" * max(0, size - len(module) - 5)
    custom_payload = b"\0" + padding
    return module + b"\0" + uleb(len(custom_payload)) + custom_payload


def adapted_javascript(config, size=10_100):
    build = config["build"]
    generated = (
        f"var pthreadPoolSize={build['pthreadPoolBuildSize']};" +
        BUILD.ENV_ORIGINAL + "worker=" + BUILD.WORKER_ORIGINAL +
        ";PThread.unusedWorkers.push(worker);"
    )
    return BUILD.adapt_javascript(generated, build).encode() + b"x" * size


class BrowserWasmOptBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="browser-wasm-opt-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_source_tree_digest_ignores_creation_order_and_metadata(self):
        first = self.root / "first"
        second = self.root / "second"
        for root, names in (
            (first, ("CMakeLists.txt", "src/a.cpp", "src/b.cpp")),
            (second, ("src/b.cpp", "src/a.cpp", "CMakeLists.txt")),
        ):
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
                path.touch()
        self.assertEqual(BUILD.source_tree_sha256(first),
                         BUILD.source_tree_sha256(second))

    def test_generated_javascript_adaptation_is_exact_and_idempotence_is_rejected(self):
        generated = (
            f"var pthreadPoolSize={BUILD.pins()['build']['pthreadPoolBuildSize']};" +
            BUILD.ENV_ORIGINAL + "worker=" + BUILD.WORKER_ORIGINAL +
            ";PThread.unusedWorkers.push(worker);"
        )
        adapted = BUILD.adapt_javascript(generated, BUILD.pins()["build"])
        self.assertIn(BUILD.ENV_ADAPTED, adapted)
        self.assertIn(BUILD.WORKER_ADAPTED, adapted)
        self.assertIn(BUILD.WORKER_CREATION_ADAPTED, adapted)
        with self.assertRaisesRegex(ValueError, "adaptation"):
            BUILD.adapt_javascript(adapted, BUILD.pins()["build"])

    def test_threaded_wasm_detection_requires_shared_memory(self):
        self.assertTrue(BUILD.has_shared_memory(wasm_with_memory()))
        unshared_import = b"\x01\x01a\x01b\x02\x01\x01\x02"
        module = b"\0asm\x01\0\0\0\x02" + uleb(len(unshared_import)) + unshared_import
        self.assertFalse(BUILD.has_shared_memory(module))

    def test_pins_bind_source_toolchain_parallelism_and_license(self):
        config = BUILD.pins()
        self.assertEqual(8, config["build"]["maximumWorkerCount"])
        self.assertEqual(2, config["build"]["reservedProcessorCount"])
        self.assertIn("navigator.hardwareConcurrency",
                      config["build"]["runtimeWorkerCountExpression"])
        self.assertEqual("6.0.7", config["build"]["emscriptenVersion"])
        self.assertEqual(config["license"]["sha256"],
                         BUILD.sha256(ROOT / config["license"]["path"]))


class BrowserWasmOptPackageTests(unittest.TestCase):
    def receipt(self, config, javascript, wasm, source_commit):
        build = config["build"]
        return {
            "schemaVersion": 1,
            "sourceCommit": source_commit,
            "binaryenCommit": config["upstream"]["revision"],
            "binaryenVersion": config["upstream"]["version"],
            "effectiveVersion": config["effectiveVersion"],
            "sourceArchiveSha256": config["upstream"]["sourceArchiveSha256"],
            "sourceTreeSha256": config["upstream"]["sourceTreeSha256"],
            "javascriptSha256": PACKAGE.sha256(javascript),
            "javascriptSize": len(javascript),
            "wasmSha256": PACKAGE.sha256(wasm),
            "wasmSize": len(wasm),
            "configureArguments": BUILD.normalized_configure_arguments(
                build["pthreadPoolBuildSize"]),
            "buildTarget": build["target"],
            "pthreadPoolBuildSize": build["pthreadPoolBuildSize"],
            "maximumWorkerCount": build["maximumWorkerCount"],
            "reservedProcessorCount": build["reservedProcessorCount"],
            "runtimeWorkerCountExpression": build["runtimeWorkerCountExpression"],
            "binaryenCoresEnvironmentVariable": build["binaryenCoresEnvironmentVariable"],
            "adaptationVersion": build["adaptationVersion"],
            "emccVersion": (
                "emcc (Emscripten gcc/clang-like replacement + linker emulating GNU ld) "
                f"{build['emscriptenVersion']} ({build['emscriptenRevision']})"
            ),
            "cmakeVersion": f"cmake version {build['cmakeVersion']}",
            "ninjaVersion": build["ninjaVersion"],
        }

    def test_archive_round_trips_exact_inventory_and_receipt(self):
        config = PACKAGE.pins()
        source_commit = "a" * 40
        javascript = adapted_javascript(config)
        wasm = wasm_with_memory()
        receipt = self.receipt(config, javascript, wasm, source_commit)
        license_text = (ROOT / config["license"]["path"]).read_bytes()

        archive = PACKAGE.archive_bytes(javascript, wasm, receipt, license_text)

        self.assertEqual(
            receipt, PACKAGE.verify_archive(archive, config, source_commit))

    def test_receipt_rejects_a_different_wasm_module(self):
        config = PACKAGE.pins()
        source_commit = "b" * 40
        javascript = adapted_javascript(config)
        wasm = wasm_with_memory()
        receipt = self.receipt(config, javascript, wasm, source_commit)

        with self.assertRaisesRegex(ValueError, "receipt"):
            PACKAGE.validate_receipt(
                receipt, javascript, wasm + b"x", config, source_commit)

    def test_receipt_rejects_single_threaded_wasm(self):
        config = PACKAGE.pins()
        source_commit = "c" * 40
        javascript = adapted_javascript(config)
        wasm = wasm_with_memory(shared=False)
        receipt = self.receipt(config, javascript, wasm, source_commit)

        with self.assertRaisesRegex(ValueError, "receipt"):
            PACKAGE.validate_receipt(
                receipt, javascript, wasm, config, source_commit)


if __name__ == "__main__":
    unittest.main()
