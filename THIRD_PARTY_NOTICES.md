# Third-party notices

The repository's MIT License covers the playtester source authored by Zoey
Rose. It does not relicense external libraries, tools, or Atrinik game content.

## Direct dependencies

- `atrinik-protocol` v1.0.9 is MIT-licensed. It is downloaded as a
  checksum-pinned wheel from `atrinik/legacy-protocol`
  (`sha256:78954264fa49ac8736dbfc7a72669f619e0202e6ae15445a0b0839aa8f30eb8a`)
  and is not vendored here.
- `libatrinik` v1.1.5 is GPL-2.0-or-later. Its checksum-pinned source archive
  (`sha256:3c07b178cc236881f52272df6e38bc2d4186943b782e4cee34b2e733da9c8f9a`)
  supplies the pathfinding library used by the native adapter and is not
  vendored here. The adapter source remains MIT, but a distributed wheel or
  binary linked with this library must satisfy the applicable GPL requirements
  and must not be represented as MIT-only.
- `aioquic`, when the optional QUIC feature is installed, is BSD-3-Clause.
- `scikit-build-core`, used to build the package, is Apache-2.0.

The playtester reads Atrinik content and imports content analysis tooling from
a separate content checkout. Those tools and content retain their repository
and per-file licenses; they are not covered by this repository's MIT License.

Release artifacts must record their exact resolved dependency graph and retain
all required license texts and notices. No linked GPL binary should be
published until its corresponding-source and notice obligations are satisfied.
The repository's automated semantic release publishes source history and notes,
not a prebuilt wheel.
