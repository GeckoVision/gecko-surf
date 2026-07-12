"use strict";
// Pure binary-resolution logic for the @geckovision/gecko launcher.
// Kept dependency-free and side-effect-free so it unit-tests with a fake resolver.

const path = require("node:path");

// node's process.arch -> our binary arch label
const ARCH = { x64: "x64", arm64: "arm64" };

// The (platform-arch) targets we publish a binary package for. Extend as
// release.yml gains targets (win32-x64, darwin-x64, …).
const SUPPORTED = new Set(["linux-x64", "linux-arm64", "darwin-arm64"]);

/** The @geckovision platform package for this (platform, arch), or null if unsupported. */
function targetPackage(platform, arch) {
  const key = `${platform}-${ARCH[arch] || arch}`;
  return SUPPORTED.has(key) ? `@geckovision/gecko-${key}` : null;
}

/**
 * Resolve the native binary path for this platform.
 * `resolve` is an injected `require.resolve`-like fn (specifier -> absolute path).
 * Returns { binary, pkg } on success or { error } with an actionable message.
 */
function resolveBinary(platform, arch, resolve) {
  const pkg = targetPackage(platform, arch);
  if (!pkg) {
    return {
      error:
        `unsupported platform ${platform}-${arch}. ` +
        `Supported: ${[...SUPPORTED].join(", ")}. ` +
        `Download a binary from https://github.com/GeckoVision/gecko-surf/releases`,
    };
  }
  let pkgJson;
  try {
    pkgJson = resolve(`${pkg}/package.json`);
  } catch {
    return {
      error:
        `${pkg} is not installed (its optionalDependency was skipped). ` +
        `Reinstall without --no-optional / --ignore-optional.`,
    };
  }
  const binName = platform === "win32" ? "gecko.exe" : "gecko";
  return { binary: path.join(path.dirname(pkgJson), "bin", binName), pkg };
}

module.exports = { targetPackage, resolveBinary, SUPPORTED, ARCH };
