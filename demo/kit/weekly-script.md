# Weekly update — what to say

## The 30-second pitch, spoken

76 words. About 30 seconds at a normal pace. Say it once, straight to camera, before
the screen recording plays.

> **The problem.** Point an AI agent at an API and it guesses. Which call to make,
> which account, what it will cost. When money moves, a guess is expensive.
>
> **What we do.** Gecko makes it check first. We say what a transaction will do before
> anyone signs it. And we refuse when we cannot vouch for something.
>
> **The proof.** This week we ran eleven transactions on Solana mainnet. Every one, we
> predicted the cost before signing. All eleven landed exact.
>
> **What is next.** The app, and our first design partners onboarding.

## The diagram walkthrough, spoken

Six boxes, about 30 seconds, over `gecko-architecture.png`. One sentence each — the boxes
are on screen, so the words should not repeat them. Use this INSTEAD of the pitch above
when you want to explain the mechanism, or after it when you have a minute.

| beat | on screen | say |
|---|---|---|
| ~0:00 | **Your agent** | "It asks in one sentence. It does not know the program, the accounts, or the price." |
| ~0:05 | **Comprehend** | "Gecko reads the surface itself — not the docs, the thing. What it does, which accounts, which mint." |
| ~0:11 | **Prepare** | "It builds the exact bytes, runs them, and hands back a receipt: this is what it will cost, this is what it moves. Nothing is signed." |
| ~0:18 | **Your signer** | "This one is outside us. Your key, your wallet, your rules. We never see it." |
| ~0:23 | **Verify** | "The signed bytes come back and we check they are the same ones we ran. Byte for byte, or it does not go." |
| ~0:28 | **Solana mainnet** | "It lands. And the number we said beforehand is the number the chain charged." |

**The line to land on**, over the dashed boundary:

> "Everything inside that box reads and checks. Nothing inside it has ever held a key."

### Why each line says what it says

- **"not the docs, the thing"** — this is the whole differentiator in four words. Docs go
  stale; the surface cannot.
- **"Nothing is signed"** on Prepare — say it there, not later. It is the sentence that
  makes the signer box make sense when it appears.
- **"outside us"** on the signer — the boundary is the product decision, so name it the
  moment the box is on screen rather than saving it for the end.
- **"Byte for byte, or it does not go"** — Verify is the least intuitive box. Do not explain
  the binding; explain the refusal.
- **"the number we said beforehand"** — the proof lands harder as a comparison than as a
  figure. The figure goes on the card.

### What NOT to say while walking the diagram

- Do not read the sublabels aloud. They are already on screen and repeating them is the
  fastest way to sound like a slide deck.
- Do not say "simply" or "just". Nothing here is simple; that is why it is worth showing.
- Do not name PayBox unless you want to explain what it is. "Your signer" is the point —
  the box works with any of them.

### Notes on delivery

- The three beats are problem, mechanism, proof. Do not reorder them. The proof only
  lands after the mechanism is understood.
- "We refuse when we cannot vouch for something" is the line to slow down on. It is the
  part competitors cannot fake in a demo, and this week the product did it on camera.
- If you need to cut to 20 seconds, drop "What is next". Never drop the refusal line.

### Swaps, if a beat feels flat

- Opening: *"An agent that can spend money should not be guessing what it costs."*
- Proof: *"Eleven for eleven, and the number we said beforehand is the number the chain charged."*

---

## Cue sheet for the screen recording

For `docs/assets/weekly-paybox-coffee.mp4`, **8.7s**. It is one continuous take with no
scene breaks — the rows appear in the order they happened, then the two tables.

*(The older `weekly-usdg-coffee.mp4`, 55.3s, is the previous cut: a terminal take that
ends at the signer boundary plus a receipt read-back. Superseded, kept for reference.)*

**How to run it.** The 30-second pitch is spoken first, to camera, with nothing on screen.
Then the take plays. Nothing is narrated over the take — it is 8.7 seconds and the rows
are the point. Land on the closing card.

What is on screen, in order:

| what appears | why it matters |
|---|---|
| the prompt, as a bubble | one sentence, no integration code |
| `List stores and menus` | it found the shop itself |
| `Ask PayBox which wallet it signs for` | it asked the signer who it is; nobody told it |
| `Read token balances over raw RPC` | no Gecko tool answers this, so it shelled out — a gap, visible |
| `Check what the wallet can pay with` -> `Plan a token swap` | short of the price, so it derived a route |
| `Prepare the unsigned swap` -> `PayBox signs` -> `Submit to mainnet` | Gecko prepared, PayBox signed, neither is the other |
| the same three again for the purchase | the loop, twice |
| menu table, then transactions table | prices it saw, and what it landed with compute units |

## The closing card

Hold three seconds. Read it or don't; it stands on its own.

    11 mainnet transactions today. Compute predicted before signing, exact on chain.
    Verified-exact record: 20 of 20.
    2 design partners in conversation, neither from the waitlist. Onboarding starts this week.
    Waitlist: 1. Users on the app: 0.

## Lines to not say

Cutting these is not modesty, it is the difference between a claim that survives a
question and one that does not.

- **Not "it bought the coffee."** In the take it stopped at the signer boundary. The
  purchase in the second half was signed separately with a local key. Say "we signed it",
  never let the video imply the agent did.
- **Not "free" or "no fees."** Nothing in this video is gasless. That is next week's, and
  only if a fee payer actually runs.
- **Not "20 of 20" without "verified".** 55 rows are in the ledger; 20 are predicted AND
  re-read from the chain. The denominator is the honest part.
- **Not any partner's name.** Neither has agreed to be named publicly, and one has not
  onboarded yet.
- **Not "users" for the waitlist.** One signup, and we do not know she is an API provider.
- **Not "the agent chose Orca."** It derived the only venue that fits; there was no menu of
  pools to choose from. "Nobody chose the pool" is the true and stronger line.
