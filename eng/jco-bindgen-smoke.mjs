#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import { readFile, writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';

const [bindgenPath, componentPath, outputPath] = process.argv.slice(2);
if (!bindgenPath || !componentPath || !outputPath) {
    throw new Error('Expected bindgen module, component fixture, and output paths.');
}

// Import the wrapper generated beside the newly built core Wasm. This avoids
// accidentally exercising the released @bytecodealliance/jco-transpile copy
// that pnpm installs for the Jco CLI while building the pinned source tree.
const { $init, generate } = await import(pathToFileURL(bindgenPath));
await $init;

const generated = generate(await readFile(componentPath), {
    name: 'smoke',
    map: [],
    instantiation: { tag: 'async' },
    validLiftingOptimization: false,
    tracing: false,
    noNodejsCompat: false,
    noTypescript: false,
    tlaCompat: false,
    base64Cutoff: 5000,
    noNamespacedExports: false,
    multiMemory: false,
    bindgenEnableWasmExnref: true,
    strict: true,
    idlImports: false,
    asmjs: false,
});

const javascript = generated.files
    .filter(([name]) => name.endsWith('.js'))
    .map(([, contents]) => new TextDecoder().decode(contents))
    .join('\n');
await writeFile(outputPath, javascript, 'utf8');
