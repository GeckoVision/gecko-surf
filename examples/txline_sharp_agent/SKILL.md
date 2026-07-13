---
name: txline-sharp-agent
description: Point an agent at the paywalled TxLINE World Cup odds API with Gecko (one line, no integration code), monitor the feed, and flag sharp implied-probability moves — the signal for a trading tool or an on-chain prediction market. Runs $0 in recorded mode. Use when building a Solana sports-trading agent, a prediction-market resolver, or any tool that consumes a hard, authenticated real-time API.
---

# TxLINE Sharp Movement Detector (powered by Gecko)

## Overview

TxLINE is exactly the kind of API agents fail on: paywalled, with a multi-step **on-chain**
auth handshake (guest JWT → on-chain subscribe → sign → activate → two tokens). Gecko
comprehends it into first-call-correct tools and injects the credentials invisibly, so your
agent just *reads odds* — and this skill turns that stream into **sharp-move signals**.

The whole skill runs **$0 in recorded mode** (synthetic, offline). Live data needs a TxLINE
subscription (see `SETUP.md`) — a wallet action only the user can take.

## Setup

1. **Comprehend + wire TxLINE** (one line, no Python):
   ```bash
   npx @geckovision/gecko add \
     examples/txline_demo/spec/txline_openapi.yaml \
     --base-url https://txline.txodds.com --mode recorded
   ```
2. **(Live only)** seal the TxLINE session — `SETUP.md` walks the on-chain subscription.
   Recorded mode needs nothing.

## Workflow

1. **Detect intent** — is the user building a signal tool, a market maker, or a settlement
   resolver? All three start from the same odds stream.
2. **Read the feed** — call the allow-listed odds tools (`getApiFixturesSnapshot`,
   `getApiOddsSnapshotFixtureid`, `getApiOddsUpdatesFixtureid`). Never call the guest/purchase/
   activate operations — those are not the agent's job.
3. **Flag sharp moves** — run successive snapshots through `SharpDetector` (threshold on the
   implied-probability `Pct` delta). Present each: fixture, book, market, outcome, Δpp, direction.
4. **Reason** — for a flagged move, give the single most likely read (steam / injury / lineup /
   in-play event). Never invent numbers a tool didn't return.
5. **(Optional) settle on-chain** — hand off to `../txodds_settlement`: pull TxLINE's Merkle
   proof and settle a prediction escrow trustlessly. Simulate the tx on a Surfpool mainnet-fork
   first; a real broadcast is the user's own signed action.

## Non-negotiables

- Default to **recorded mode** ($0) — never flip to live without the user's TxLINE subscription.
- **Never** expose or call the auth operations; the agent only reads odds.
- **Never** sign or broadcast a mainnet transaction on the user's behalf. Simulate; hand over.
- Present odds honestly — a flagged move is a signal, not a guarantee.

## Try it

```bash
uv run python -m examples.txline_sharp_agent.demo     # $0 recorded showcase
```
