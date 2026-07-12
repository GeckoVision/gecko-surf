#!/usr/bin/env node
"use strict";
// Assemble a @geckovision/gecko-<platform> npm package from a built binary.
// Run once per platform in CI (from the release-built binaries), then `npm publish`
// the resulting dir. The binary is shipped as-is; `os`/`cpu` let npm install only
// the package matching the host — that's what makes the launcher's
// optionalDependencies resolve to exactly one platform.
//
//   node make-platform-package.js --platform linux-x64 \
//     --binary out/gecko-linux-x86_64 --version 0.3.0 --out build
//
// Produces  build/gecko-linux-x64/{package.json, bin/gecko}

const fs = require("node:fs");
const path = require("node:path");

const OS_CPU = {
  "linux-x64": { os: "linux", cpu: "x64" },
  "linux-arm64": { os: "linux", cpu: "arm64" },
  "darwin-arm64": { os: "darwin", cpu: "arm64" },
  // future: "win32-x64": { os: "win32", cpu: "x64" }, "darwin-x64": {...}
};

function parseArgs(argv) {
  const a = {};
  for (let i = 0; i < argv.length; i += 2) {
    const k = argv[i];
    if (!k.startsWith("--")) throw new Error(`bad arg: ${k}`);
    a[k.slice(2)] = argv[i + 1];
  }
  for (const req of ["platform", "binary", "version", "out"]) {
    if (!a[req]) throw new Error(`missing --${req}`);
  }
  return a;
}

function main() {
  const { platform, binary, version, out } = parseArgs(process.argv.slice(2));
  const meta = OS_CPU[platform];
  if (!meta) {
    throw new Error(`unknown platform '${platform}'. Known: ${Object.keys(OS_CPU).join(", ")}`);
  }
  if (!fs.existsSync(binary)) throw new Error(`binary not found: ${binary}`);

  const isWin = meta.os === "win32";
  const binName = isWin ? "gecko.exe" : "gecko";
  const dir = path.join(out, `gecko-${platform}`);
  fs.mkdirSync(path.join(dir, "bin"), { recursive: true });
  fs.copyFileSync(binary, path.join(dir, "bin", binName));
  if (!isWin) fs.chmodSync(path.join(dir, "bin", binName), 0o755);

  const pkg = {
    name: `@geckovision/gecko-${platform}`,
    version,
    description: `Prebuilt gecko binary for ${meta.os}-${meta.cpu}.`,
    license: "Apache-2.0",
    repository: "github:GeckoVision/gecko-surf",
    os: [meta.os],
    cpu: [meta.cpu],
    files: ["bin"],
  };
  fs.writeFileSync(path.join(dir, "package.json"), JSON.stringify(pkg, null, 2) + "\n");
  process.stdout.write(`wrote ${dir} (${meta.os}/${meta.cpu}, v${version})\n`);
}

main();
