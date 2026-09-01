"""In-chat cards for Gecko tools — MCP Apps resources, PayBox-style.

WHAT THIS IS. MCP Apps (the ``io.modelcontextprotocol/ui`` extension) lets a tool result
render as an interactive card inside the chat: the tool's ``_meta.ui.resourceUri`` points
at a ``ui://`` resource, the client fetches that resource's HTML template once, sandboxes
it in an iframe, and hands it the tool result over postMessage JSON-RPC
(``ui/notifications/tool-result``). PayBox's wallet card is this mechanism; nothing about
it is proprietary.

THE RULES THE CARD LIVES UNDER, and they are the same rules as everything else here:

* SELF-CONTAINED. No external scripts, styles, fonts or images — the CSP would block
  them anyway, and a card that phones home is a card that leaks. Every byte ships in
  this file.
* RENDERS ONLY WHAT THE TOOL ALREADY RETURNED. The card is a projection of
  ``structuredContent`` (the same dict every non-rendering client gets as JSON text).
  Nothing new crosses the control-plane boundary because the card exists.
* THE REFUSAL IS THE CENTREPIECE. ``blocked`` renders as prominently as success, with
  the reason and every peg verdict visible — a card that made refusals look like
  failures would be selling against the product.

Clients that do not speak MCP Apps ignore ``_meta.ui`` and see exactly what they see
today. The wire shapes here follow the MCP Apps specification (2026-01-26): tool
``_meta.ui.resourceUri``, resource mimeType ``text/html;profile=mcp-app``, and the
view-side ``initialize`` → ``ui/notifications/*`` handshake.
"""

from __future__ import annotations

__all__ = [
    "CARD_MIME_TYPE",
    "PLAN_PAYMENT_RESOURCE_URI",
    "UI_TOOL_RESOURCES",
    "card_html",
    "card_resources",
]

CARD_MIME_TYPE = "text/html;profile=mcp-app"

PLAN_PAYMENT_RESOURCE_URI = "ui://gecko/plan-payment"

#: tool name -> the ui resource its results render in. The serve layer reads this to
#: stamp ``_meta.ui.resourceUri`` on the tool def and to serve the resource itself.
#:
#: EMPTY since 2026-09-01, and the friction report is why: on claude.ai, a tool
#: carrying ``_meta.ui`` is tagged ``[third_party_mcp_app]`` and gated behind a
#: connector-consent round trip. plan_payment is the tool that DECIDES whether a
#: swap is wise — read-only, keyless, no clock — and the gate made the SAFE tool
#: cost ceremony while the unguarded path (calling plan_swap directly) stayed
#: free. An inverted safety gradient: the measured web run skipped the peg check
#: on a stablecoin swap because of exactly this. The card HTML and the resource
#: stay served (clients that already know the URI can render it); only the
#: tool-def stamp is withdrawn until app-tagged tools stop costing consent.
UI_TOOL_RESOURCES: dict[str, str] = {}

#: Tools whose RESULTS also carry ``structuredContent`` (the same dict the JSON
#: text carries). Decoupled from the ui stamp above on purpose: structured
#: results cost no consent gate anywhere, so they survive the untagging.
STRUCTURED_RESULT_TOOLS: frozenset[str] = frozenset({"plan_payment"})


_PLAN_PAYMENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gecko — Payment Plan</title>
<style>
  :root {
    --bg: #101418; --card: #171d24; --ink: #e8edf2; --dim: #93a1af;
    --line: #232c35; --accent: #34d399; --mono: ui-monospace, SFMono-Regular,
    Menlo, Consolas, monospace;
    --ok: #34d399; --route: #60a5fa; --blocked: #f87171; --warn: #fbbf24;
  }
  [data-theme="light"] {
    --bg: #f4f6f8; --card: #ffffff; --ink: #17222b; --dim: #5b6a77;
    --line: #e3e8ee;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: transparent; font: 14px/1.45 system-ui, -apple-system,
         "Segoe UI", sans-serif; color: var(--ink); padding: 8px; }
  .card { background: var(--card); border: 1px solid var(--line);
          border-radius: 14px; padding: 18px 18px 14px; max-width: 480px; }
  .head { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
  .logo { width: 22px; height: 22px; border-radius: 6px; background: var(--accent);
          display: grid; place-items: center; color: #08110c; font-weight: 800;
          font-size: 13px; }
  .brand { font-weight: 700; letter-spacing: .01em; }
  .sub { color: var(--dim); font-size: 12px; margin-left: auto; }
  .pill { display: inline-block; padding: 4px 12px; border-radius: 999px;
          font-weight: 700; font-size: 13px; margin-bottom: 8px; }
  .pill.ok      { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
  .pill.route   { background: color-mix(in srgb, var(--route) 18%, transparent); color: var(--route); }
  .pill.blocked { background: color-mix(in srgb, var(--blocked) 18%, transparent); color: var(--blocked); }
  .reason { color: var(--dim); font-size: 12.5px; margin-bottom: 12px;
            overflow-wrap: anywhere; }
  .sect { border-top: 1px solid var(--line); padding: 10px 0 2px; }
  .sect h4 { font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
             color: var(--dim); margin-bottom: 8px; font-weight: 600; }
  .chip { display: flex; justify-content: space-between; gap: 8px; padding: 5px 0;
          font-size: 12.5px; }
  .chip .m { font-family: var(--mono); color: var(--dim); }
  .v-ok { color: var(--ok); font-weight: 600; }
  .v-bad { color: var(--blocked); font-weight: 600; }
  .v-unk { color: var(--warn); font-weight: 600; }
  .route-box { background: color-mix(in srgb, var(--route) 8%, transparent);
               border: 1px solid color-mix(in srgb, var(--route) 30%, transparent);
               border-radius: 10px; padding: 10px 12px; margin: 6px 0 10px; }
  .route-amt { font-size: 20px; font-weight: 800; font-variant-numeric: tabular-nums; }
  .route-line { font-size: 12px; color: var(--dim); font-family: var(--mono);
                overflow-wrap: anywhere; margin-top: 4px; }
  .kv { display: flex; justify-content: space-between; font-size: 12.5px;
        padding: 3px 0; }
  .kv .k { color: var(--dim); }
  .kv .v { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .foot { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 9px;
          font-size: 11px; color: var(--dim); display: flex;
          justify-content: space-between; gap: 8px; }
  .wait { color: var(--dim); font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
<div class="card" id="card">
  <div class="head">
    <div class="logo">G</div>
    <div class="brand">Gecko</div>
    <div class="sub">payment plan</div>
  </div>
  <div id="out" class="wait">Planning&hellip;</div>
  <div class="foot">
    <span>Check the call before it counts.</span>
    <span id="asof"></span>
  </div>
</div>
<script>
(function () {
  "use strict";
  var nextId = 1;
  function request(method, params) {
    var id = nextId++;
    parent.postMessage({ jsonrpc: "2.0", id: id, method: method, params: params }, "*");
  }
  function notify(method, params) {
    parent.postMessage({ jsonrpc: "2.0", method: method, params: params }, "*");
  }
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
  function short(addr) {
    var s = String(addr || "");
    return s.length > 12 ? s.slice(0, 4) + "\\u2026" + s.slice(-4) : s;
  }
  function ui(raw) { // raw base units at 6 decimals -> display
    var n = Number(raw);
    return isFinite(n) ? (n / 1e6).toFixed(n % 1e6 === 0 ? 2 : 6).replace(/0+$/, "").replace(/\\.$/, ".00") : esc(raw);
  }
  function pegClass(check) {
    if (check.blocks) return "v-bad";
    return check.outcome === "ok" ? "v-ok" : "v-unk";
  }
  function render(data) {
    var out = document.getElementById("out");
    var html = "";
    var outcome = data.outcome || "unknown";
    var cls = data.blocked ? "blocked" : (outcome === "route_found" ? "route" : "ok");
    var label = data.blocked ? "REFUSED \\u00b7 " + outcome
              : outcome === "payable_now" ? "PAYABLE NOW"
              : outcome === "route_found" ? "ROUTE FOUND" : outcome;
    html += '<span class="pill ' + cls + '">' + esc(label) + "</span>";
    html += '<div class="reason">' + esc(data.reason || "") + "</div>";

    if (data.route && data.route.quote) {
      var q = data.route.quote;
      html += '<div class="route-box">';
      html += '<div class="route-amt">convert ' + esc(ui(q.amount_in)) + "</div>";
      html += '<div class="route-line">' + esc(short(data.route.held_mint)) +
              " \\u2192 " + esc(short(data.priced_mint)) + " \\u00b7 pool " +
              esc(short(q.pool)) + " \\u00b7 " + esc(q.direction) + " \\u00b7 " +
              esc(q.slippage_bps) + " bps</div></div>";
    }

    html += '<div class="sect"><h4>Order</h4>';
    html += '<div class="kv"><span class="k">' + esc(data.product || "?") +
            " @ " + esc(data.store || "?") + '</span><span class="v">' +
            esc(ui(data.price_raw)) + "</span></div></div>";

    var checks = data.peg_checks || [];
    if (checks.length) {
      html += '<div class="sect"><h4>Peg checks</h4>';
      for (var i = 0; i < checks.length; i++) {
        var c = checks[i];
        html += '<div class="chip"><span class="m">' + esc(c.side) + " \\u00b7 " +
                esc(short(c.mint)) + '</span><span class="' + pegClass(c) + '">' +
                esc(c.outcome) + (c.blocks ? " \\u00b7 blocks" : "") + "</span></div>";
      }
      html += "</div>";
    }

    var holdings = data.holdings || {};
    var mints = Object.keys(holdings);
    if (mints.length) {
      html += '<div class="sect"><h4>Wallet</h4>';
      for (var j = 0; j < mints.length; j++) {
        html += '<div class="kv"><span class="k m">' + esc(short(mints[j])) +
                '</span><span class="v">' + esc(ui(holdings[mints[j]])) + "</span></div>";
      }
      html += "</div>";
    }
    out.className = "";
    out.innerHTML = html;
    var asof = document.getElementById("asof");
    if (data.peg_evidence_as_of) {
      asof.textContent = "peg evidence " + String(data.peg_evidence_as_of).slice(0, 16) + "Z";
    }
    notify("ui/notifications/size-changed", {
      width: document.body.scrollWidth, height: document.body.scrollHeight + 16
    });
  }
  function extract(result) {
    if (result && result.structuredContent) return result.structuredContent;
    try {
      var text = ((result || {}).content || []).filter(function (c) {
        return c.type === "text";
      })[0];
      return text ? JSON.parse(text.text) : null;
    } catch (e) { return null; }
  }
  window.addEventListener("message", function (event) {
    var msg = event.data || {};
    if (msg.method === "ui/notifications/tool-result") {
      var data = extract(msg.params);
      if (data && data.error) {
        render({ outcome: "error", blocked: true, reason: data.error });
      } else if (data) {
        render(data);
      }
    } else if (msg.method === "ui/notifications/tool-cancelled") {
      render({ outcome: "cancelled", blocked: true,
               reason: (msg.params || {}).reason || "cancelled" });
    } else if (msg.method === "ui/notifications/host-context-changed") {
      if ((msg.params || {}).theme) {
        document.documentElement.setAttribute("data-theme", msg.params.theme);
      }
    } else if (msg.id && msg.result && msg.result.hostContext) {
      var theme = msg.result.hostContext.theme;
      if (theme) document.documentElement.setAttribute("data-theme", theme);
    }
  });
  request("initialize", {
    protocolVersion: "2026-01-26",
    capabilities: {},
    clientInfo: { name: "gecko-plan-payment-card", version: "1" }
  });
})();
</script>
</body>
</html>
"""


def card_html(tool_name: str) -> str | None:
    """The card template for ``tool_name``, or None when the tool has no card."""
    if tool_name == "plan_payment":
        return _PLAN_PAYMENT_HTML
    return None


def card_resources() -> dict[str, tuple[str, str]]:
    """uri -> (mimeType, html) for every card this build ships."""
    return {PLAN_PAYMENT_RESOURCE_URI: (CARD_MIME_TYPE, _PLAN_PAYMENT_HTML)}
