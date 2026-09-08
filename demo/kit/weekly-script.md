# Weekly update — voiceover script

For `docs/assets/weekly-usdg-coffee.mp4`, 55.2s. Cues are measured from the rendered
file, not estimated. Roughly 150 words, which is a calm pace over this length.

Every number below is on screen. If a line is cut, cut the claim with it.

---

**0:00 — 0:11 · the agent take**
*On screen: one prompt, then tool calls, then the answer streaming in.*

> One prompt. "I want to buy a coffee, I only have USDG."
>
> Nobody told it which shop, which pool, or which token program. It read the menu,
> read the wallet, and found the problem: the shop prices in classic USDC, and USDG
> is Token-2022. Different asset, not a different label.
>
> Then it stopped. The peg oracle hasn't updated since August. And we never hold a key.
> It said both, instead of routing around either.

**0:11 — 0:28 · before it happened**
*On screen: the pre-flight, the receipt, the binding.*

> This is the part that matters. Before anything is signed, we say what it will cost.
> Forty-eight thousand nine hundred and seventy-two compute units.

**0:28 — 0:37 · who did what**
*On screen: the three-way agreement.*

> Three things had to agree. We checked it, the wallet signed it, the policy allowed it.
> Verification is not authorisation.

**0:37 — 0:55 · after it happened**
*On screen: the landed transaction, and the two numbers.*

> And the chain charged forty-eight thousand nine hundred and seventy-two.
>
> Five transactions on mainnet today. Five predicted before signing. Five exact.

---

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
