#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';
import process from 'node:process';
import { chromium, firefox, webkit } from 'playwright';

const args = process.argv.slice(2);
const value = name => {
  const index = args.indexOf(name);
  if (index < 0 || index + 1 === args.length) throw Error(`Missing ${name}`);
  return args[index + 1];
};
const root = resolve(value('--tool'));
const browserName = value('--browser');
const browserType = { chromium, firefox, webkit }[browserName];
if (!browserType) throw Error('Invalid --browser');
const expectedWorkers = Number(value('--workers'));
if (!Number.isInteger(expectedWorkers) || expectedWorkers < 1 || expectedWorkers > 16)
  throw Error('Invalid --workers');
for (const name of ['wasm-opt.js', 'wasm-opt.wasm'])
  if (!(await stat(resolve(root, name))).isFile()) throw Error(`Missing ${name}`);

const html = `<!doctype html><meta charset="utf-8"><script type="module">
import createWasmOpt from '/wasm-opt.js';
let workers = 0;
try {
  const runtime = await createWasmOpt({
    noInitialRun: true,
    noExitRuntime: true,
    pthreadPoolSize: ${expectedWorkers},
    environment: { BINARYEN_CORES: '${expectedWorkers}' },
    pthreadWorkerUrl: new URL('/wasm-opt.js', location.href),
    locateFile: name => new URL('/' + name, location.href).href,
    onPthreadWorker: () => workers++,
    print: () => {},
    printErr: text => console.error(text),
  });
  runtime.FS.writeFile('input.wasm', new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]));
  let exitCode = 0;
  try { exitCode = runtime.callMain(['input.wasm', '-Oz', '--all-features', '-o', 'output.wasm']); }
  catch (error) { if (!Number.isInteger(error?.status)) throw error; exitCode = error.status; }
  globalThis.result = { success: exitCode === 0, exitCode, workers,
    outputBytes: runtime.FS.readFile('output.wasm').byteLength,
    isolated: crossOriginIsolated, sharedArrayBuffer: typeof SharedArrayBuffer === 'function' };
} catch (error) { globalThis.result = { success: false, workers, error: String(error), stack: error?.stack }; }
</script>`;
const server = createServer(async (request, response) => {
  response.setHeader('Cross-Origin-Opener-Policy', 'same-origin');
  response.setHeader('Cross-Origin-Embedder-Policy', 'require-corp');
  if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html; charset=utf-8');
    response.end(html);
    return;
  }
  const name = request.url === '/wasm-opt.js' ? 'wasm-opt.js'
    : request.url === '/wasm-opt.wasm' ? 'wasm-opt.wasm' : undefined;
  if (!name) { response.writeHead(404).end(); return; }
  response.setHeader('Content-Type', name.endsWith('.wasm') ? 'application/wasm' : 'text/javascript');
  response.end(await readFile(resolve(root, name)));
});
await new Promise((accept, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', accept);
});
const address = server.address();
const browser = await browserType.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${address.port}/`);
  await page.waitForFunction(() => globalThis.result, undefined, { timeout: 120_000 });
  const result = await page.evaluate(() => globalThis.result);
  if (!result.success || result.exitCode !== 0 || result.workers !== expectedWorkers ||
      result.outputBytes !== 8 || !result.isolated || !result.sharedArrayBuffer)
    throw Error(`Browser wasm-opt smoke failed: ${JSON.stringify(result)}`);
  console.log(JSON.stringify({ browser: browserName, platform: process.platform, ...result }));
} finally {
  await browser.close();
  await new Promise(accept => server.close(accept));
}
