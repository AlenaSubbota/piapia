#!/usr/bin/env node
// Decrypts MrBlue webtoon images using their own bee.wasm module.
//
// Usage:
//   node mrblue_decode.mjs <dir>
//
// Decrypts every *.enc file in <dir>, writing the decoded bytes to the same
// basename without the .enc suffix. Requires bee.wasm and wasm_exec.js next to
// this script (or in the current working directory).
//
// Why a Node helper? MrBlue encrypts images with a position-permutation +
// per-byte transform implemented in WebAssembly (bee.wasm). Rather than
// re-implement (and chase) that cipher in Python, we feed the raw bytes to the
// real decode() the site itself uses.
import { readFileSync, writeFileSync, readdirSync, existsSync } from 'fs';
import { dirname, join, basename } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));

function findFile(name) {
  for (const d of [here, process.cwd()]) {
    const p = join(d, name);
    if (existsSync(p)) return p;
  }
  throw new Error(`${name} not found in ${here} or ${process.cwd()}`);
}

// bee.wasm checks window.location.href against the mrblue host before it will
// expose decode(), so provide a matching stub.
globalThis.window = globalThis;
globalThis.location = {
  href: 'https://viewer.mrblue.com/comics/x/1?ppt=PPT01',
  hostname: 'viewer.mrblue.com',
  origin: 'https://viewer.mrblue.com',
};

eval(readFileSync(findFile('wasm_exec.js'), 'utf8'));

const go = new Go();
const { instance } = await WebAssembly.instantiate(
  readFileSync(findFile('bee.wasm')),
  go.importObject,
);
go.run(instance);

if (typeof globalThis.decode !== 'function') {
  console.error('decode() was not exposed by bee.wasm');
  process.exit(2);
}

const dir = process.argv[2];
if (!dir) {
  console.error('usage: node mrblue_decode.mjs <dir>');
  process.exit(2);
}

let n = 0;
for (const f of readdirSync(dir)) {
  if (!f.endsWith('.enc')) continue;
  const enc = new Uint8Array(readFileSync(join(dir, f)));
  // decode(bytes, nonce, hdFlag) — nonce is unused for image decryption.
  const dec = Buffer.from(decode(enc, '0', false));
  writeFileSync(join(dir, basename(f, '.enc')), dec);
  n++;
}
console.error(`decoded ${n} file(s)`);
