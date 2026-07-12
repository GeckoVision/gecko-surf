#!/usr/bin/env bash
# ONE-TIME bootstrap publish (token-free).
#
# Trusted Publishing (OIDC) is configured per package, and npm requires a package
# to already EXIST before you can set its trusted publisher. Our packages are new,
# so someone has to create v-first. This does that with your interactive `npm login`
# session (your 2FA) — no stored token, ever. After it runs once and you configure
# Trusted Publishing on the 4 packages, every future release publishes via OIDC from
# CI automatically.
#
# Steps:
#   1. Cut the release so CI builds the binaries:
#        git tag vX.Y.Z && git push origin vX.Y.Z      # release.yaml -> GitHub Release
#   2. Log in to npm (your account + 2FA; NOT a token):
#        npm login
#   3. Run this:
#        ./npm/scripts/bootstrap-publish.sh X.Y.Z      # version WITHOUT the leading v
#   4. On npmjs.com, for EACH of the 4 packages (@geckovision/gecko and the three
#      @geckovision/gecko-<plat>): Settings -> Trusted publishing -> add GitHub Actions,
#      repo GeckoVision/gecko-surf, workflow file release.yaml, action "npm publish".
#   Done — future `git tag && push` releases publish via OIDC, no token.
set -euo pipefail

export V="${1:?usage: bootstrap-publish.sh <version-without-leading-v>}"
REPO="GeckoVision/gecko-surf"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

command -v gh >/dev/null || { echo "need the GitHub CLI (gh)"; exit 1; }
npm whoami >/dev/null 2>&1 || { echo "run 'npm login' first (your account + 2FA)"; exit 1; }

echo "→ downloading v$V binaries from the GitHub Release…"
mkdir -p npm/out npm/build
gh release download "v$V" --repo "$REPO" \
  --pattern 'gecko-linux-*' --pattern 'gecko-darwin-*' --dir npm/out

publish_platform() {  # <package-platform> <binary-asset-name>
  node npm/scripts/make-platform-package.js \
    --platform "$1" --binary "npm/out/$2" --version "$V" --out npm/build
  ( cd "npm/build/gecko-$1" && npm publish --access public )
}
publish_platform linux-x64    gecko-linux-x86_64
publish_platform linux-arm64  gecko-linux-arm64
publish_platform darwin-arm64 gecko-darwin-arm64

# Stamp the launcher's version + optionalDependency pins to $V, then publish it last.
node -e '
  const fs = require("fs"), p = "npm/gecko/package.json";
  const j = JSON.parse(fs.readFileSync(p));
  j.version = process.env.V;
  for (const k of Object.keys(j.optionalDependencies || {})) {
    j.optionalDependencies[k] = "=" + process.env.V;
  }
  fs.writeFileSync(p, JSON.stringify(j, null, 2) + "\n");
'
( cd npm/gecko && npm publish --access public )

echo ""
echo "✓ bootstrapped v$V. Next: configure Trusted Publishing on all 4 packages (see the header),"
echo "  then future releases publish from CI via OIDC — no token."
