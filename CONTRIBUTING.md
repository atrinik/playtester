# Contributing

Use a Conventional Commits pull-request title:
`type(optional-scope)!: concise description`.

Until the provenance hold in [PROVENANCE.md](PROVENANCE.md) is resolved, do not
copy, translate, decompile, or mechanically recreate implementation from
`atrinik/tools/atrinik_bot`, its bytecode, secret-bearing snapshots, or other
historical artifacts. Work from documented public behavior and newly authored
tests, or submit a separately reviewed provenance/license decision first.

New implementation must act through ordinary player capabilities, keep
transport details behind a narrow interface, use versioned content/protocol
inputs, and keep credentials and mutable state outside the source tree.

Before submitting a change, run:

```sh
git diff --check
```

Add language- and component-specific validation as implementation lands. Keep
the required `Playtester validation` job stable when expanding CI.
