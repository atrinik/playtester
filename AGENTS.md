# Atrinik playtester repository guide

- This MIT Python 3.11+ repository owns Atrinik's autonomous ordinary-player
  playtester: transports, live observations, campaign policy, content-derived
  planning, persistent checkpoints, and deterministic completion evidence.
- The goal is a reproducible unattended run from a new account and character
  through a versioned full-game contract. Never equate a level threshold or a
  partial quest list with finishing.
- Keep transport details behind a narrow provider interface. Classic direct
  operation and a future client automation API must not own campaign policy.
- Behave only through ordinary player capabilities. Never depend on privileged
  server state, save-file edits, or validation bypasses.
- Source modules remain at repository root and CMake assembles the installable
  `atrinik_bot` package with its native extension under `build/`.
- Keep `dependencies.lock.json`, the atomic compatibility cache installer,
  CMake fetch coordinates, package metadata, CI inputs, and
  `THIRD_PARTY_NOTICES.md` synchronized. External libraries and content retain
  their own licenses; linked GPL artifacts are not MIT-only.
- Keep credentials, certificates, databases, logs, collected content, caches,
  downloaded dependencies, and all mutable state outside the source tree.
- Validate changes with the strict-warning native build, CTest, the complete
  Python regression suite against the verified pinned compatibility bundle, a
  clean-directory wheel install/doctor smoke test, compileall, and
  `git diff --check`.
- Dependency review is advisory, including operational errors. Before merge, the
  implementing Codex must inspect the latest run for the current PR head and
  read its job summary even when a bot comment exists, fix actionable introduced
  risk, or record explicit maintainer acceptance of existing risk. A comment
  from an earlier run is not current evidence; unavailable reports leave results
  unknown. Reporting applies to eligible PR runs where this workflow exists in
  the relevant PR/merge revision; removing branch filters does not backport it.
  License metadata reporting does not automate compatibility policy: existing
  repository license and provenance constraints remain authoritative. Validate
  workflow changes with actionlint and `git diff --check`.
- Commits and pull-request titles use Conventional Commits. Preserve unrelated
  work. For nonclosing cross-repository linkage, use a full GitHub issue URL;
  shorthand `owner/repository#number` is reserved for an intentional closing
  reference because the release-note generator can render it as “closes.”
  Semantic-release owns source tags, notes, and GitHub releases; do not
  attach the GPL-linked wheel unless its corresponding-source obligations are
  satisfied. Update this guide when ownership, dependency, interface, or
  validation contracts change.
