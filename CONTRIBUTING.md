# Contributing

Use a Conventional Commits pull-request title:
`type(optional-scope)!: concise description`.

Use a full `https://github.com/OWNER/REPOSITORY/issues/NUMBER` URL for
nonclosing cross-repository linkage. Reserve `owner/repository#number`
shorthand for an intentional closing reference: the release-note generator can
otherwise render a nonclosing mention as “closes.” Local `#number` references
remain supported.

Playtester behavior must use ordinary player capabilities, keep transport
details behind a narrow interface, consume versioned protocol/content inputs,
and keep credentials and mutable state outside the source tree. Update
`dependencies.lock.json` and `THIRD_PARTY_NOTICES.md` together when a pinned
input or license boundary changes.

Before submitting a change, build the strict native adapter, run CTest, the
complete content-aware Python suite, and the clean-directory installed-wheel
smoke test described in [README.md](README.md), then run:

```sh
python3 -W error -m compileall -q -f .
git diff --check
```

Keep the required `Playtester validation` job name stable when expanding CI.
