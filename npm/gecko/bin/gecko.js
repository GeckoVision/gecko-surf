#!/usr/bin/env node
"use strict";
// Launcher: resolve the native gecko binary for this platform and exec it,
// forwarding argv, stdio, and the exit code. No Python, no download.

const { execFileSync } = require("node:child_process");
const { resolveBinary } = require("../lib/resolve.js");

const { binary, error } = resolveBinary(process.platform, process.arch, (s) =>
  require.resolve(s),
);

if (error) {
  process.stderr.write(`gecko: ${error}\n`);
  process.exit(1);
}

try {
  execFileSync(binary, process.argv.slice(2), { stdio: "inherit" });
} catch (e) {
  // execFileSync throws on a non-zero exit; propagate the child's code.
  process.exit(typeof e.status === "number" ? e.status : 1);
}
