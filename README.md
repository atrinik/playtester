# Atrinik playtester

Atrinik playtester is the home of an autonomous, headless end-to-end
playtester for Atrinik. Its goal is one reproducible unattended run from a new
account and character through a versioned full-game completion contract,
acting only through ordinary player capabilities and producing actionable
evidence for every failure.

The initial golden path is tracked by [issue #1](https://github.com/atrinik/playtester/issues/1):
complete all 16 formal quests, reach level 115, defeat Zechna, verify each
outcome from authoritative game state, and exit with a machine-readable result.

## Repository status

This repository is intentionally seeded without the existing
`atrinik/tools/atrinik_bot` prototype. The extraction audit in
[`atrinik/tools#19`](https://github.com/atrinik/tools/issues/19) found that the
prototype predates its tracked Git history and includes GPL-linked or
GPL-derived boundaries. Atrinik's provenance policy requires that uncertainty
to fail closed, so no prototype source, credentials, mutable state, or build
artifact has been copied or relicensed here.

Development can proceed through either a separately reviewed provenance and
license-resolution decision or a clean-room implementation from documented
behavior. Until then, the roadmap and repository policy define the intended
product without making an unsupported source-license claim.

## Product boundaries

- Behave as an ordinary player; never depend on privileged server state or
  bypass game validation.
- Keep gameplay policy separate from transports so Classic direct operation
  and a future versioned client automation API can be independent providers.
- Derive world and quest coverage from versioned inputs, with explicit
  compatibility locks and no mutable sibling-checkout assumptions.
- Keep credentials, runtime state, logs, caches, content, and downloaded
  dependencies outside the source tree.
- Treat completion as a versioned, measurable contract rather than a level
  threshold or partial quest list.

## License

New work committed to this repository is available under the [MIT License](LICENSE),
copyright 2026 Zoey Rose. That license does not apply to or relicense source in
other Atrinik repositories; see [PROVENANCE.md](PROVENANCE.md).
