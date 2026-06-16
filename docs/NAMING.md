# Naming consolidation — proposal (not yet adopted)

> **Status: PROPOSAL ONLY.** This document recommends a naming direction and
> scopes the work. It does **not** rename anything. No code, config, repo, or
> identifier changes have been made. Adopt deliberately, as its own task, after
> the cleanup lanes land.

## The problem

The project currently answers to **three** names, and they're used
interchangeably in ways that blur what each one *is*:

| Name | Where it shows up today | What it reads as |
|------|------------------------|------------------|
| **Brain Gateway** | README title, repo (`mexirab/brain_gateway`), most docs | Infrastructure / a router. Accurate for the engine, cold for a companion. |
| **Jess** | The assistant persona — welcome tour, voice, `/settings`, `JESS_*` env vars | The thing you actually *talk to*. Warm, human, already established in-product. |
| **ConvivialProphet(.com)** | The domain / future landing page | An umbrella/org brand. Distinctive but opaque — doesn't say what it does. |

Three names for one product is one too many. For a consumer-facing **ADHD
companion**, the name a new person meets first should feel like a *who*, not a
*what*. "Brain Gateway" describes plumbing; nobody forms an attachment to a
gateway. That's exactly backwards for this audience, where the emotional hook
("something in my corner that catches my 2 AM thoughts") is the whole pitch.

## Recommendation: **lead with "Jess"** as the product name

Promote the name users already bond with. Give each name one clear job:

1. **Jess — the product/brand name.** This is what the README leads with, what
   the website says, what you'd tell a friend. It's warm, short, already the
   persona, and reusing it collapses two of the three names into one role
   (brand = persona). Tagline carries the "what": *"Jess — a private,
   ADHD-aware assistant that runs on your own hardware."*
2. **Brain Gateway — the engine / technical name.** Keep it for the
   self-hosted backend, the repo, the dev docs, the architecture. "Powered by
   the Brain Gateway engine." Developers and self-hosters still find it; it just
   stops being the *first* thing a non-technical visitor reads.
3. **ConvivialProphet.com — the home/org.** The domain that *hosts* the Jess
   landing page and any future projects. An umbrella, not the product name.

This is the lowest-churn way to consolidate: it's a **positioning** change at
the surface (README, website, marketing copy), not a rename of the codebase. The
engine stays "Brain Gateway"; the repo, containers, and `JESS_*`/`bgw_*`
identifiers don't have to move.

### Why not the alternatives

- **Keep "Brain Gateway" as primary:** technically honest but emotionally inert
  for the target audience; it's the status quo that this proposal exists to fix.
- **Lead with "ConvivialProphet":** distinctive and ownable, but opaque (says
  nothing about ADHD or assistance), harder to spell/say aloud, and "Prophet"
  carries unrelated connotations. Better as the org/domain than the product.
- **Invent a fourth name:** adds to the problem instead of solving it. The point
  is to go from three to one front-facing name, and "Jess" is already loved.

### Caveats to weigh before committing

- **"Jess" is generic** — weak for trademark and SEO on its own. Mitigate by
  always pairing it with a descriptor in titles/metadata ("Jess, the ADHD
  assistant") and by owning the ConvivialProphet domain as the canonical home.
- **Users can rename their own assistant** (`assistant_name` in `/settings`
  defaults to "Jess"). If the *product* is also "Jess," a user who renames their
  assistant to "Max" creates a small brand/persona mismatch. Acceptable — the
  product brand and the user's personal instance name are allowed to differ
  (cf. "Alexa" the brand vs. a custom wake word) — but worth a sentence in the
  docs if adopted.
- **Heritage:** "Jess" is fine to keep as the persona (per the release-prep
  branding decision). Do **not** reintroduce the real-person identifiers behind
  the original voice clone when expanding the brand — keep it generic.

## What would need to change if adopted (scoping only — do NOT do this here)

Ordered low-disruption → high-disruption. Most of the value is in the first
tier; the deeper tiers are optional and high-cost.

**Tier 1 — surface copy (docs/marketing only, no code):**
- README title + tagline lead with "Jess"; one line explaining "built on the
  Brain Gateway engine."
- Landing page on ConvivialProphet.com positioned as "Jess."
- A short "what are all these names?" note (could be this file, trimmed) so
  contributors aren't confused.

**Tier 2 — consistent dev-facing language (docs only):**
- Sweep `docs/` so "Brain Gateway" is used specifically for the *engine/backend*
  and "Jess" for the *assistant/product*, instead of as loose synonyms.
- Reconcile `CLAUDE.md`'s framing (it already calls the assistant "Jess" in many
  places — mostly consistent already).

**Tier 3 — identifiers (CODE CHANGE — out of scope for docs; large blast radius):**
- Repo rename `mexirab/brain_gateway` → would break clone URLs, existing
  branches/PRs, and every doc link. Highest cost; lowest marginal benefit.
- Docker compose project name (`gateway_mvp`), container names
  (`brain-orchestrator`), metric namespace (`bgw_*`), env-var prefixes
  (`JESS_*` vs `BGW_*` are already mixed). These are deep and load-bearing;
  renaming them is a migration, not a rebrand, and is **explicitly not
  recommended** as part of a naming decision. Leave them.

**Bottom line:** adopting "Jess" as the front-facing name is almost entirely a
**Tier 1 docs/marketing change**. The expensive Tier 3 identifier churn is
*not* required to get the marketing benefit and should be declined.
</content>
