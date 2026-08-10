# Atrinik playtester repository guide

- This repository owns an autonomous ordinary-player playtester: session
  providers, live observations, campaign policy, content-derived planning,
  persistent checkpoints, and deterministic completion evidence.
- The target is a reproducible unattended run from a new account and character
  through a versioned full-game contract. Never equate a level threshold or a
  partial quest list with finishing.
- The source hold in `PROVENANCE.md` is fail-closed. Do not copy, translate,
  decompile, or reconstruct `atrinik/tools/atrinik_bot` implementation until a
  reviewed provenance and license decision admits an exact separable boundary.
- Independent implementation from documented behavior is the default. Do not
  copy wire implementation, GPL headers, authored content catalogs, quest/map
  coordinates, or other license-bearing material into MIT source.
- Behave only through ordinary player capabilities; never depend on privileged
  server state, save-file edits, or validation bypasses.
- Keep transport details behind a narrow provider interface. Classic direct
  operation and a future client automation API must not own campaign policy.
- Keep credentials, certificates, databases, logs, collected content, caches,
  downloaded dependencies, and all mutable state outside the source tree.
- Pull-request titles use Conventional Commits. Preserve unrelated work and
  finish with `git diff --check` plus the nearest component validation.
