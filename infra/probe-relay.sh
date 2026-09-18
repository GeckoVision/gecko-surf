#!/usr/bin/env bash
# The six wire probes from the fork runbook, against a live relay. A config is a claim;
# 401 and 405 are evidence. Exit 1 on the first wrong code.
#   KORA_RPC_URL=https://mcp.geckovision.tech:8443/ KORA_API_KEY=... ./infra/probe-relay.sh
set -euo pipefail
K="${KORA_RPC_URL:?set KORA_RPC_URL}"
KEY="${KORA_API_KEY:?set KORA_API_KEY (not printed)}"
fail=0
probe() { # method  expected  with_key(1/0)
  local m="$1" want="$2" auth=(); [[ "$3" == 1 ]] && auth=(-H "x-api-key:$KEY")
  local got; got=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$K" -H 'Content-Type: application/json' \
    "${auth[@]}" -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"$m\",\"params\":{}}")
  if [[ "$got" == "$want" ]]; then echo "ok    $m ($([[ $3 == 1 ]] && echo key || echo no key)) -> $got"
  else echo "WRONG $m -> $got, expected $want"; fail=1; fi
}
echo "liveness -> $(curl -s -o /dev/null -w '%{http_code}' "${K%/}/liveness") (expected 200)"
probe getPayerSigner          200 1
probe getSupportedTokens      200 1
probe signAndSendTransaction  405 1
probe transferTransaction     405 1
probe getPayerSigner          401 0
echo "signer: $(curl -s -X POST "$K" -H 'Content-Type: application/json' -H "x-api-key:$KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getPayerSigner","params":{}}' | head -c 200)"
exit $fail
