# How we publish what we find

Written **before** the first scan, deliberately. A disclosure rule invented after you already
hold findings is a rule written to fit them.

We execute claims against the chain, so we will find defects in surfaces we do not own. This
is what we do with them.

---

## The three things, and only two are ours

### 1. Aggregate — public, names nobody

The shape of what a scan found, across a population, with the method and a command anyone can
re-run:

> *"We executed every instruction of N programs against the chain. 6% of PDA accounts cannot be
> derived without a human. X% declare at least one error code the program never raises."*

A statement about an ecosystem is not an accusation about a person. Aggregate figures need no
permission and no waiting period.

### 2. Specific — private first, named only after a fix

A finding about one surface goes to whoever maintains it before it goes anywhere else, and it
goes with a reproduction. Where we can write the patch, we send a patch rather than a report.

Naming comes after the fix, and then it reads as credit: *"found and fixed in `<PR>`."* That
is how the first one went — a compute figure 347× under what the chain charges, reported with
a reproduction, merged and deployed within the hour, and the write-up names the fix rather
than the defect.

If a maintainer does not respond, the finding stays in the aggregate and stays unnamed. We do
not have a deadline that converts silence into publication. We are not a disclosure programme
and the defects we find are not exploits; the cost of waiting is ours to carry.

**Exception, in the other direction:** a finding that puts *user funds* at risk is reported
faster and more insistently, and if a maintainer declines to act we will warn the people
exposed without naming a culprit — e.g. "this class of surface publishes a guard that does not
exist; check yours." Protecting the people downstream outranks the relationship.

### 3. Ranking — never

No leaderboard, no "worst N", no ordering of one owner against another.

We render a surface its owner owns. The moment we publish a ranking we are adversarial to the
people we exist to help, we are a marketplace-shaped thing the architecture refuses to be, and
every provider learns that engaging with us is a risk rather than a service. Aggregate yes.
Ranking never.

---

## The standard that makes any of it credible

**Every finding is reproduced before it is reported, and a wrong one is withdrawn in public.**

This is not decoration. On 2026-08-17 we reported an instruction as uncallable from a
published surface. Re-checked before shipping: the defect was in our own probe, which had
omitted an account and used the wrong argument name — both facts already stated correctly in
our own config, unread by the thing doing the measuring. The finding was withdrawn, the
withdrawal is in the spec, and the scorecard went from 5/6 to 6/6.

A scan published to that standard is a different object from a scraper's ranking, and it is
the only version worth publishing. Concretely, every reported finding carries:

* the exact command that reproduces it;
* what we compared against, and why that authority is independent of the thing being measured;
* the date, because a score is a photograph and surfaces move;
* what we could **not** establish.

## What never appears in a finding

* A wallet address, a store name, or any identity belonging to a user of the surface.
* A response payload. We hold the surface and the claims about it, never the data flowing
  through it.
* An exploit, a weaponised reproduction, or a step that would move somebody's funds.
* A competitor's name in outward-facing copy.
* An adjective where a number belongs. "Unsafe" is an opinion; "the reported figure is 347×
  under what the chain charged" is a fact, and the fact is the stronger claim.
