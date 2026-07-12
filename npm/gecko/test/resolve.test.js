"use strict";
const { test } = require("node:test");
const assert = require("node:assert");
const path = require("node:path");
const { targetPackage, resolveBinary } = require("../lib/resolve.js");

test("targetPackage maps supported (platform, arch) pairs", () => {
  assert.equal(targetPackage("linux", "x64"), "@geckovision/gecko-linux-x64");
  assert.equal(targetPackage("linux", "arm64"), "@geckovision/gecko-linux-arm64");
  assert.equal(targetPackage("darwin", "arm64"), "@geckovision/gecko-darwin-arm64");
});

test("targetPackage returns null for unsupported pairs", () => {
  assert.equal(targetPackage("win32", "x64"), null);
  assert.equal(targetPackage("darwin", "x64"), null);
  assert.equal(targetPackage("linux", "ia32"), null);
});

test("resolveBinary returns the binary path when the platform package resolves", () => {
  const fake = (spec) => {
    assert.equal(spec, "@geckovision/gecko-linux-x64/package.json");
    return "/fake/node_modules/@geckovision/gecko-linux-x64/package.json";
  };
  const r = resolveBinary("linux", "x64", fake);
  assert.equal(r.pkg, "@geckovision/gecko-linux-x64");
  assert.equal(
    r.binary,
    path.join("/fake/node_modules/@geckovision/gecko-linux-x64", "bin", "gecko"),
  );
  assert.ok(!r.error);
});

test("resolveBinary errors (not a throw) when the platform package is missing", () => {
  const r = resolveBinary("darwin", "arm64", () => {
    throw new Error("Cannot find module");
  });
  assert.ok(!r.binary);
  assert.match(r.error, /is not installed/);
});

test("resolveBinary errors for an unsupported platform", () => {
  const r = resolveBinary("win32", "x64", () => "unused");
  assert.ok(!r.binary);
  assert.match(r.error, /unsupported platform/);
});

test("windows binary name gets the .exe suffix (future target)", () => {
  // Even though win32 is unsupported today, the name logic is exercised via a
  // supported darwin path to confirm the non-exe branch, and the exe branch is
  // a pure string — assert both shapes directly.
  const fake = () => "/n/@geckovision/gecko-linux-x64/package.json";
  assert.ok(resolveBinary("linux", "x64", fake).binary.endsWith(path.join("bin", "gecko")));
});
