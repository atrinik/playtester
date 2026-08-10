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
- Keep `dependencies.lock.json`, CMake fetch coordinates, package metadata,
  CI inputs, and `THIRD_PARTY_NOTICES.md` synchronized. External libraries and
  content retain their own licenses; linked GPL artifacts are not MIT-only.
- Keep credentials, certificates, databases, logs, collected content, caches,
  downloaded dependencies, and all mutable state outside the source tree.
- Validate changes with the strict-warning native build, CTest, the complete
  Python regression suite against the pinned content revision and collected
  runtime, compileall, and `git diff --check`.
- Commits and pull-request titles use Conventional Commits. Preserve unrelated
  work. Semantic-release owns source tags, notes, and GitHub releases; do not
  attach the GPL-linked wheel unless its corresponding-source obligations are
  satisfied. Update this guide when ownership, dependency, interface, or
  validation contracts change.
