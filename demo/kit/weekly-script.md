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

    5 mainnet transactions today. Compute predicted before signing, exact on chain.
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
