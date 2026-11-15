# Learning from the world and playtester feedback

Status: design proposal, not implemented. Nothing here changes behaviour
until the phased pull requests at the end land.

The owner wants Sera to learn from what she reads and does in the world, and
to turn what confuses her into feedback that improves the game. She chose
all of the following:

1. **Doors and hints.** Sera tries doors and levers as she meets them,
   safely. She reads the hint text on locked doors, signs, NPC dialogue and
   magic mouths to work out which key or quest opens them, and remembers the
   outcomes as unlock knowledge.
2. **Books and facts.** She reads the books and scrolls she finds and pulls
   out facts (key locations, passwords, monster weaknesses, directions) into
   a persistent fact memory that the planner and combat use.
3. **Terrain tactics.** She learns from her fights which positions work:
   corridors and doorways to take packs one at a time, chokepoints for
   kiting, and no open ground near ranged or caster monsters.
4. **Unclear hints become feedback.** A door or quest she cannot work out
   from its hints becomes a feedback item, so the map can be fixed.
5. **A playtester feedback queue that the owner approves.** Sera collects
   deduplicated findings with evidence in a draft queue on her dashboard.
   Only an item the owner approves is filed as a GitHub issue: on
   `atrinik/content` for content, on `atrinik/classic` for engine and client
   bugs. The only credential is a narrowly scoped token, used only when an
   item is filed.
6. **Anything else her history shows,** including equipment comparison,
   clearer item examination (what "+N" really does), and evidence-based
   balance comparisons. She does not duplicate the balance issues that
   already exist.

Related work, which this design does not duplicate:

- **Routing** ([`docs/ROUTING.md`](ROUTING.md), draft
  [PR #95](https://github.com/atrinik/playtester/pull/95)). It owns the edge
  model, `UnlockState` (its 4.3 and phase P2), dialogue and quest-gated
  edges (its 4.2 and P4), and the route event tables (P5). Unlock knowledge
  from this design plugs into P2 and P4 as an input (4.5 below); it never
  decides routes itself. Until PR #95 merges, the relative link above
  resolves only on its branch.
- **Resident-mode issues**
  [#15](https://github.com/atrinik/playtester/issues/15) (observation and
  knowledge memory),
  [#17](https://github.com/atrinik/playtester/issues/17) (NPC dialogue and
  books),
  [#21](https://github.com/atrinik/playtester/issues/21) (signs and
  notifications) and
  [#22](https://github.com/atrinik/playtester/issues/22) (claims from prose).
  Those issues describe a general, source-attributed lore memory for a
  resident character. This design is the narrow, campaign-useful slice of
  it: unlocks, facts that planning and combat consume, and feedback. Its
  tables are built so that #15 can grow from them rather than replace them
  (4.2).
- **Combat** ([`COMBAT.md`](../COMBAT.md)). Terrain tactics (4.7) are
  tie-breakers inside the existing pull, kite and retreat logic, not a new
  fight loop.

## How the evidence was gathered

- Source: this repository at `995685d` (origin/main). Line numbers refer to
  that revision.
- Content: the local `atrinik/content` checkout at `b9657299a`
  (`v1.14.0-4`). The playtester's lock names content v1.14.0, so the
  examples should match the locked input, but they were not re-checked
  against the exact locked archive. All maps were scanned offline with a
  small parser for doors (type 20), keys (21), inventory checkers (64),
  handles (93), signs and magic mouths (98), books (8) and scrolls (111),
  including objects nested in monster inventories.
- Classic server and client: the `classic` worktree at `f2273c85f`
  (`server/src`, `client/src`).
- Live state: one `GET /api/state` and one `GET /metrics` of the running bot
  (build v1.64.0, `479efb5`, uptime 20.9 h) on 2026-11-15 at about
  16:23 UTC. Nothing was posted to the bot.
- GitHub: read-only searches of `atrinik/classic` and `atrinik/content`
  issues and labels, and of the playtester's own issue list.

The probe scripts are not part of this change. Where something is
unverified, the text says so.

## 1. Inventory: what exists today

### 1.1 Server text (`messages`)

- Every DRAWINFO line goes into `GameState.messages` as
  `(time, type, color, text)`, bounded to the last 500
  (`model.py:308-313`), with a monotonic position counter so watchers can
  read "messages since" (`model.py:315-330`). `messages_since` drops player
  chat unless asked, so other players cannot forge server feedback.
- `_handle_drawinfo` (`client.py:1415-1511`) parses a fixed set of lines:
  bank balances, party setup, the savebed notice, deaths, depletion stat
  losses and heals. It also feeds the post-mortem review buffer
  (`review_messages`, bounded to the review window and to
  `post_mortem.MAX_TEXT` characters a line).
- Nothing records which object a line came from. DRAWINFO carries no source
  object. Hint text arrives as an ordinary game line: a locked door's `msg`
  in navy (`00ffff`, `door.c:246`, `door.c:297`), a sign's or magic mouth's
  `msg` in navy (`sign.c:107`, `sign.c:191`), a lever's `msg` or "You turn
  the big lever." in white (`handle.c:64-71`).
- Typed notifications (`CLIENT_CMD_NOTIFICATION`, which a sign can send,
  `sign.c:110-145`) are not dispatched at all
  ([#21](https://github.com/atrinik/playtester/issues/21) records this too).

### 1.2 Interfaces and NPC dialogue (`interface`)

- `_handle_interface` (`client.py:2318-2362`) decodes the interface packet
  into `InterfaceState` (`model.py:190-198`): title, joined text, links,
  input metadata and objects. Each new interface replaces the previous one;
  no dialogue page is kept.
- `choose_interface_link` (`client.py:2538-2565`) follows a link.
  `DialogTask` (`quest_tasks.py:13-112`) says `hello` and follows reviewed
  choices; quest executors in `catalog_quest_tasks.py` drive scripted
  dialogue. Vendors are found from interface XML (`navigation.py:143`).
- Dialogue text is used only by the task that opened it, and for the bank
  balance (`client.py:2360`). Nothing is parsed for keys, places or
  passwords.

### 1.3 Books and scrolls

- Book packets (`CLIENT_CMD_BOOK`) are appended raw to `GameState.books`,
  last 20 kept, in memory only (`client.py:2364-2375`, `model.py:287`). Only
  the quest book is parsed (`quests.parse_quest_book`, `quests.py:143-182`).
- `FarmTask.maintain_inventory` applies each unread inventory book once for
  Literacy experience (`tasks.py:3196-3217`). Attempts persist as
  `lore_attempt_counts`, keyed by Literacy level, face, type and name
  (`client.py:776-813`). The text she then receives is discarded.
- Books are then sold as junk once identified
  (`InventoryPolicy.sellable_junk`, `tasks.py:787-800`). Map books on
  shelves or corpses are never picked up for reading; there is no "read and
  put back".
- Scrolls (type 111) are identified and used only as spell scrolls; none of
  the four authored scrolls is read for text.

### 1.4 Quest journal

`request_quests` asks for the quest book; `parse_quest_book` turns it into
`QuestProgress` with parts, descriptions, status and kill counts
(`quests.py:143-182`). The dashboard shows it (`web_server.py:260`). The
catalog (`quests.load_catalog`, `quests.py:119`) reads `quest.xml` for
formal quest executors. Part descriptions are not mined for facts, and
nothing ties a journal part to a door (ROUTING G-list and 1.3 say the same
for routing).

### 1.5 What `memory.py` persists

`BotMemory` is one SQLite file with three tables (`memory.py:30-68`):

- `memory`: one JSON value per (server, account, character, kind). Kinds in
  use: `state`, `decisions`, `post_mortems`, `trends`
  (`client.py:289-527`). `state` holds the scalar and dictionary facts
  listed in `load_character_memory`: shop sweep cursors, vendor quotes,
  route hazards, farm quarantine and adaptations, savebed, apartment
  returns, `lore_attempt_counts`, bank balance and more. It is rewritten
  whole on each save.
- `metadata`: import markers.
- `chat_log`: player chat she saw and her replies, a year and 300,000 rows
  at most (`memory.py:13-20`, `memory.py:119-145`).

There is no observation table, no fact table, and no table keyed by a map
object. The decision ledger keeps the last 2,000 decisions
(`client.py:862-907`); action history keeps 500 in memory.

### 1.6 Doors, gates and levers

- **Graph.** `WorldGraph.build` marks authored locked doors and blocking
  inventory checkers as locked tiles with a requirement or exclusion
  (`navigation.py:698-721`); `access_item_names` maps lock ids to key names
  (`navigation.py:722-731`); one conditional magic mouth carries a gate's
  key id (`navigation.py:515-531`). `MapNode.lock_allowed` checks one tile
  (`navigation.py:255-269`). ROUTING 1.2 inventories this in full.
- **Ordinary doors** are left to the server's click path, which opens them
  (`navigation.py:556-558` comment). `_escape_closed_door`
  (`navigation.py:2365-2416`) and `_bump_boundary_door`
  (`navigation.py:2418`) bump a closed door when the click path stalls.
- **Locked doors** are avoided by the planner unless her inventory carries a
  matching key name (`_allow_locked`, `navigation.py:1700-1708`). She never
  bumps one to read its message, and never records the outcome.
- **Levers and switches** (type 93): no code applies one. The only map-object
  applies are exits (`navigation.py:2685-2693`) and two scripted quest
  steps (`catalog_quest_tasks.py:139`, `catalog_quest_tasks.py:293`).
- **Signs and magic mouths**: magic mouths fire when she walks over them,
  and their text lands unattributed in `messages`. Signs are never applied.

### 1.7 Item examination

- `AtrinikClient.examine(tag)` sends ITEM_EXAMINE (`client.py:2458`). The
  answer is a set of ordinary game lines; the Classic client shows them in
  the message window and nothing else (`client/src/gui/widgets/inventory.c:1112-1123`).
  The client's inventory row shows only name, `Con: c/q`, required level
  and weight (`inventory.c:285-331`).
- Three examine readers exist, each for one purpose:
  - recall-wand charges, "It has N charges left." (`tasks.py:348-370`,
    `tasks.py:4523-4557`);
  - the mana crystal's capacity and charge (`ManaCrystal.read_examine`,
    `tasks.py:1033-1048`);
  - shop stock, where `_apply_examine_metadata` (`tasks.py:5787-5875`)
    reads `(wc+N)(dam+N)(ac+N)(block+N)(absorb+N%)`, weapon delay,
    protections, attacks, stats and caster bonuses into
    `examined_gear_score`.
- Carried gear is scored from its *name* alone
  (`InventoryPolicy.gear_score`, `tasks.py:876-897`): quality, condition,
  `named_bonus * 8` for every "+N" in the name, 120 if magical, and the
  caster bonuses. `worthless_plus` (`tasks.py:776-785`) knows that "+N" does
  nothing on a light source, and nothing else.

### 1.8 Fight review and positions

`self_improvement.review_failure` (`self_improvement.py:101-168`) classifies
a death or recall as pack, resources, damage type, out-damaged or route, and
`FarmCircuitTask._learn_from_failure` (`navigation.py:4869`) raises the
spot's adaptation level. Pull squares are chosen from authored aggro radii
(`navigation.py:3716`, COMBAT.md). Position quality is not recorded: nothing
knows whether she stood in a corridor, a doorway or the open when she won or
lost a fight. `low_health_retreat` (`tasks.py:4068`) scores steps by
separation and escape area, not by how many attackers can reach a square.

### 1.9 Dashboard and metrics

- `GET /api/state`, `GET /metrics`, `GET /api/chat`, `GET /api/face/*` and
  `POST /api/task`, `POST /api/action` (`web_server.py:838-895`). The
  listener must be loopback (`web_server.py:726-768`). A POST is refused only
  when it carries a cross-origin `Origin` header (`web_server.py:815-818`);
  a request with no `Origin` is accepted.
- The live dashboard is reachable from the home lab at
  `playtester.atrinik.orion.milkyway`, so a reverse proxy forwards to the
  loopback listener with a loopback `Host`. Whether that proxy
  authenticates was not checked. This matters for the approval endpoint
  (4.10.5).

## 2. What her history shows

The snapshot on 2026-11-15 is a poor day, and it is the most useful evidence
in this document.

**An idle starvation loop.** The operator stopped `autoplay:level-115` at
20:54 UTC on 2026-11-14. Since then Sera has had no task. She starved to death
every 51 to 54 minutes, the first time on `world_12_76` and from then on
at her Clearhaven savebed (`world_7_79` (16,3)): 22 starvation deaths in
the process lifetime (`deaths_total{cause="starvation"}`), all of them in
the decision ledger. Each
respawn restores 500 food (`types/player.c:1226-1229`) and each death drains
stats. Her depletion count is now 66, and the stats the dashboard reports
are Str 3, Dex 3, Con 3, Int 1, Pow 1, with max mana 20. She carries no
food and no money. Nothing in the playtester eats, buys food or logs out
while no task runs. This is a playtester defect, outside this design, and
it is still running; it should be fixed and the character reviewed before
any feature below is judged on her live behaviour.

**Futile casting.** The bed square is `no_magic`
(`world_7_79` lines 2392-2398: `bed_1b.111`, `no_magic 1`). The server
answers every cast there with "Something blocks your spellcasting."
(`spell_util.c:423-429`). She has ordered 11,641 minor healing casts
(`spells_cast_total`), and 135 of her last 150 messages are that line. She
never learns that the square blocks magic.

**Before the stop.** The last 500 decisions hold 25 deaths. 22 are the
starvation loop; the other three were route deaths in the Underground City
(`underground_city_0_0` and `_0_2`), followed by an out-damaged recall from
a charred treant on the Burnt Forest circuit. The process counters add 5
pack deaths and 1 resources death. ROUTING section 2 covers the Underground City incident.
`navigation-occupant-blocked` is the commonest decision (165 of 500), mostly
on `world_7_64`; `retreat-choice` (98) and `emergency-disengage` (80)
follow.

**Combat totals in this process.** Slash dealt 12,201 damage in 260 hits
(47 a hit, `damage_dealt_total{attack="slash"}`); magic bullet 3 hits.
Damage taken was mostly impact (1,914) and fire (1,384). These are the raw
inputs a balance comparison would use (4.11.3); one day of one character is
not yet a comparison.

**Her kit.** She carries several unworn pieces that her name-only scoring
cannot compare with what she wears: `black iron ring mail +2` against a
worn `silksteel chain mail +1`, `forged meteoric steel shootingstar +3`
against a wielded `shear steel falchion +2`, a `chimera leather cloak`
against a worn `silk cape +2`, and `gargoyle leather combat boots of acid
protection` against worn `manticore leather combat boots +2`. The cape is
the clearest case: the cape archetype has no AC (`arch/items/armour/cloak/cape.arc`),
so its "+2" adds nothing to combat (4.8), yet `gear_score` gives it 16
points for the "+2".

**Keys she holds.** `Morg'eean's Ship Key`, `Forgotten Tunnels Kobold Key`,
`Incuna Western Gate Key`, `Incuna Tavern Barn Key` and a `forged meteoric
steel key ring` container. Whether the client sees the key ring's contents
is unverified (ROUTING section 7).

## 3. Gaps

- **G1. No attribution.** A hint line cannot be tied to the door, sign,
  magic mouth or lever that produced it, so nothing can learn from it.
- **G2. No durable reading.** Books, dialogue pages, signs and examine text
  are discarded after use.
- **G3. No facts.** Nothing extracts a key name, a holder, a colour, a
  password, a direction, a weakness or a warning from any text.
- **G4. Locks are binary.** A lock is open only with a matching key name in
  inventory. Quest-granted forces, NPC-held keys and "this one never opens"
  are not known; ROUTING P2 and P4 add the content side, but not what she
  learned by trying.
- **G5. No safe trial.** She never bumps a locked door or applies a lever to
  find out, and has no rules for doing so safely.
- **G6. No tile facts.** A `no_magic` square, a trap square or a square
  where she was ganked is not remembered (section 2).
- **G7. No position learning.** Fights are not recorded by terrain, so
  corridors and doorways cannot be preferred on evidence.
- **G8. "+N" is a guess.** Gear scoring counts every "+N" as 8 points,
  whatever the item type; carried gear is never examined.
- **G9. No feedback channel.** Confusing hints, typos, impossible doors and
  engine oddities she meets go nowhere.
- **G10. Book experience is wasted at low Int** (4.11.2), and she reads
  books without checking.

## 4. Design

### 4.1 Principles

1. **Her own observations only.** Every stored fact is something her client
   saw or did: a line she received, a page she read, an outcome she caused.
   No other player's text, name or actions are stored (4.10.4). This matches
   the repository's ordinary-player rule.
2. **Two kinds of knowledge, kept apart.** The playtester already plans from
   content-derived data (the world graph, quest catalog, analyzer). That
   *content truth* may keep guiding routes, as ROUTING does. *Player-visible
   knowledge* is what the hints, books and outcomes told her. Hint clarity
   is judged only from player-visible knowledge; content truth is used only
   to score whether her reading was right and as evidence in a report.
   Otherwise she could never find a hint unclear.
3. **Deterministic first.** Parsers are regular expressions and small
   grammars over known server phrasings and authored text, with fixtures
   from real content. An LLM is optional, offline-capable, out of process
   and never required (4.3.3).
4. **Hypotheses are not unlocks.** A fact read from a book or sign is a
   candidate until an outcome confirms it. Only a confirmed outcome becomes
   a `Learned` atom for routing (ROUTING 4.3), and none ever satisfies a
   `Consumes` atom.
5. **Shadow before switch.** Each behaviour change (trying locks, terrain
   tie-breakers, new gear scoring) first runs as "would do" decisions,
   behind a flag that the owner turns on. Recording and the draft queue
   change no behaviour.
6. **Nothing leaves the machine without the owner.** Drafts stay local. The
   GitHub token is read only to file an approved item.

### 4.2 Storage

New tables in the same `memory.sqlite3`, created by `BotMemory.__init__`
with `CREATE TABLE IF NOT EXISTS`, all scoped by (server, account,
character) like `chat_log`. Each table has row caps and retention so a
flood cannot grow the file without bound. Unlike `state`, rows are
appended or upserted, never rewritten whole. Saves are best effort with a
short lock timeout, like post-mortems: a locked database never stalls a
tick.

```sql
-- What she read or was told, attributed where possible (G1, G2).
CREATE TABLE observations (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  at REAL NOT NULL,
  map TEXT, x INTEGER, y INTEGER,          -- where she stood
  source_kind TEXT NOT NULL,   -- door | gate | lever | sign | magic_mouth |
                               -- npc | book | scroll | examine | journal |
                               -- system
  source_key TEXT,             -- stable id, see below; NULL if unattributed
  source_name TEXT,            -- "big lever", "Warden Argoth", book title
  attribution TEXT NOT NULL,   -- exact | timing | content-match | none
  text_hash TEXT NOT NULL,     -- sha256 of normalized text
  text TEXT NOT NULL,          -- bounded to 4000 characters
  color TEXT, msg_type INTEGER
);
CREATE INDEX observations_by_source ON observations
  (server, account, character, source_key, at);
CREATE UNIQUE INDEX observations_dedup ON observations
  (server, account, character, source_key, text_hash);
```

`source_key` is stable across restarts: `door:/map:x,y`, `lever:/map:x,y`,
`sign:/map:x,y`, `npc:/map:Name`, `book:Title:text_hash`. A repeat of the
same text from the same source updates nothing but a `seen` counter kept in
`facts`/`locks`; the unique index keeps one row.

```sql
-- Facts extracted from observations (G3); candidates until confirmed.
CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  kind TEXT NOT NULL,      -- key-name | key-holder | lock-colour | password |
                           -- direction | weakness | immunity | trap-warning |
                           -- tile-property | quest-gate | unopenable
  subject TEXT NOT NULL,   -- what it is about: a source_key, map cell,
                           -- monster name, item name
  value TEXT NOT NULL,     -- "orange stone giant key", "fire", "no_magic"
  confidence REAL NOT NULL,-- 0..1, parser-assigned, raised by outcomes
  status TEXT NOT NULL,    -- candidate | confirmed | refuted | expired
  observation_id INTEGER,  -- the observation it came from
  parser TEXT NOT NULL,    -- parser id and version, e.g. "key-name/1"
  created_at REAL, updated_at REAL, expires_at REAL
);

-- One row per lock-like object she has met (G4, G5).
CREATE TABLE locks (
  server TEXT, account TEXT, character TEXT,
  source_key TEXT NOT NULL,   -- door:/map:x,y | lever:... | mouth:...
  kind TEXT NOT NULL,         -- door | gate | force-field | lever | checker
  first_seen REAL, last_seen REAL,
  hint_observation_id INTEGER, -- the text it showed her when refused
  state TEXT NOT NULL,        -- unknown | refused | opened | toggled |
                              -- suspected-unopenable
  opened_with TEXT,           -- item name, "force", "none"
  attempts INTEGER, refusals INTEGER, last_attempt REAL,
  hypotheses_json TEXT,       -- ranked fact ids and her verdict on each
  clarity TEXT,               -- see 4.9
  PRIMARY KEY (server, account, character, source_key)
);

-- Each deliberate trial and what it caused (G5).
CREATE TABLE interactions (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  at REAL, map TEXT, x INTEGER, y INTEGER,
  source_key TEXT, action TEXT,   -- bump | apply | read | examine | say
  preconditions_json TEXT,        -- hp, sp, threats, recall ready, keys held
  outcome TEXT,                   -- opened | refused | toggled | no-effect |
                                  -- spawned | trap | teleported | damaged |
                                  -- unknown
  effects_json TEXT,              -- tiles changed, monsters appeared, hp delta
  observation_ids TEXT            -- the lines in its 3 s window
);

-- Fights by terrain (G7), section 4.7.
CREATE TABLE fight_positions (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  started REAL, ended REAL, map TEXT, cell TEXT,   -- 8x8 cell, as ROUTING
  feature TEXT,            -- corridor | doorway | room | open
  reach INTEGER,           -- squares from which a melee attacker reaches her
  peak_attackers INTEGER, ranged_attackers INTEGER,
  damage_taken INTEGER, seconds REAL,
  outcome TEXT             -- won | retreated | recalled | died
);

-- The draft feedback queue (G9), section 4.10.
CREATE TABLE feedback_items (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  fingerprint TEXT NOT NULL,  -- dedup key, 4.10.2
  repo TEXT NOT NULL,         -- atrinik/content | atrinik/classic
  kind TEXT NOT NULL,         -- bug | hint | balance | qol | clarity
  title TEXT NOT NULL, body_md TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL,       -- draft | approved | filing | filed |
                              -- rejected | duplicate | snoozed | failed
  occurrences INTEGER NOT NULL,
  first_seen REAL, last_seen REAL,
  remote_candidates_json TEXT,-- possible existing issues, 4.10.2
  decided_at REAL, issue_url TEXT, error TEXT,
  UNIQUE (server, account, character, fingerprint)
);
CREATE TABLE feedback_filings (          -- append-only audit
  id INTEGER PRIMARY KEY, item_id INTEGER, at REAL,
  repo TEXT, http_status INTEGER, issue_url TEXT, error TEXT
);
```

Caps (starting points): observations 50,000 rows a character, oldest
unreferenced first; interactions 20,000; fight positions 20,000; facts and
locks unbounded in practice (one per object met), capped at 20,000; filed
and rejected feedback kept, drafts capped at 500 with the lowest-occurrence
drafts dropped first.

These tables are a subset of what
[#15](https://github.com/atrinik/playtester/issues/15) asks for:
`observations` is its source ledger and `facts` its claim store, with the
narrower kinds above. #15 can add kinds, contradictions and provenance
queries without migrating these rows.

### 4.3 Text understanding

#### 4.3.1 Attribution

DRAWINFO has no source, so attribution uses what she just did, in order of
strength:

1. **Exact.** Book packets, interface pages and quest books carry their own
   identity (book title, interface title = NPC name, journal).
2. **Timing.** After she bumps a door, applies a lever or sign, or steps on
   a square, the server's answer arrives in the same tick or the next. The
   first non-chat line within 1.5 s whose colour matches the source type
   (navy for door, sign and magic-mouth text, white for levers and
   "You open the ... with the ...") is attributed to that object. Fixed
   server phrasings ("The %s is locked.", "The %s won't budge.",
   "You turn the %s.", "Nothing is written on it.", "You are unable to
   decipher the strange symbols.", "You open the %s with the %s.") confirm
   the kind.
3. **Content match.** When she is on or next to a square whose authored
   object has a `msg` equal to the received text (normalized), that object
   is the source. This uses content only to name the source, not to decide
   whether the hint was clear. It also attributes magic mouths she walked
   over, which she never chose to trigger.
4. **None.** The line is stored unattributed (`source_kind = system`) only
   if a parser finds a fact in it; otherwise it is not stored.

Player chat types never enter `observations` (`c.PLAYER_CHAT_TYPES`, as in
`_handle_drawinfo`), however they are phrased.

#### 4.3.2 Deterministic parsers

Each parser is a pure function `(observation) -> list[fact]` with a
version, unit fixtures from real content (section 5) and no network. The
first set:

| Parser | Finds | Example input (real) |
| --- | --- | --- |
| `key-name` | an item name a lock asks for | "You need Asteria Dungeon Key 2 to pass." |
| `key-holder` | a creature or NPC named as the key's keeper | "[i]Xuhrexal, the stone giant[/i] is engraved on the door." |
| `lock-colour` | a colour word tied to a force field or key | "The orange force field prevents you from reaching the door." |
| `npc-key` | an NPC who mentions a key in dialogue | "I don't suppose you've come about the key." |
| `password` | a quoted or stressed word after "password", "say", "speak" | the drow intelligence report mentions a password, and gives none |
| `direction` | a compass direction tied to a place or action | "It must be cast northwest from a special location." |
| `trap-warning` | a place to avoid | "STAY OFF THE BRIDGE, IT IS A TRAP" |
| `weakness` / `immunity` | an attack type a creature is weak or immune to | `(monster) ... weak to fire`, "cold-immune" |
| `tile-property` | a fixed server refusal tied to her square | "Something blocks your spellcasting." gives `no_magic` at her square |
| `unopenable` | lock text that says it does not open from here | "It seems like the door is not possible to open from this side..." |
| `generic-lock` | the archetype's default text, which names nothing | "You need a special key to open this door." |
| `quest-part` | journal part text naming a place, item or NPC | "...his outpost is at the very peak of the mountains" |

Names found in text are matched against the names she can see
(inventory, ground, visible creatures, NPCs she has talked to) and against
the names the world graph knows (`landmarks`, monster names), so "Xuhrexal"
becomes a creature fact rather than an unknown word. Matching is
case-folded and markup-stripped (`[i]`, `[b]`, `[title]`).

#### 4.3.3 An LLM, optionally

An LLM can help with free prose that the parsers miss: long books, NPC
monologues, quest descriptions. It is **optional and off by default**:

- It is an `Interpreter` behind a narrow interface: input one observation's
  text and the names she knows, output candidate facts in the same `facts`
  schema with `parser = "llm/<model>/<prompt version>"` and confidence at
  most 0.5.
- It runs out of process, like the farming analyzer, on a bounded queue of
  observations no parser explained, never in the tick loop. Its results
  arrive minutes later and nothing waits for them.
- **Offline-capable:** the default implementation is a no-op. A local model
  endpoint (on the same host or the lab) can be configured by the owner. No
  text leaves the machine unless she configures an endpoint that sends it
  elsewhere, and the dashboard says which interpreter is active.
- LLM facts are hypotheses only. They can rank which lock to try, never
  open an edge, file feedback or decide a fight on their own. A hint-clarity
  verdict (4.9) never counts an LLM fact as "the hint was clear".

### 4.4 Trying doors, levers, signs and magic mouths safely

A trial is a `LockTrialTask`, offered by navigation and farming when she
passes within a few tiles of an untried object. Behind the flag
`try_locks`, default off; with it off, it logs `lock-trial-shadow`
decisions saying what it would have tried.

**What she tries.**

- *Locked door or gate* (a door on a locked tile, or a closed door whose
  bump the server refused): bump it once from an adjacent square. The
  refusal text is its hint. If she holds a candidate key she has not tried,
  the bump itself tests it: the server searches her whole inventory,
  containers included (`key.c:48-66`).
- *Sign*: apply it (no risk; `sign.c:43-108`).
- *Magic mouth*: nothing to try; its text is recorded when she walks over it.
- *Lever or switch*: apply once, observe for 10 s, record the outcome.
- *Inventory checker*: nothing to try; ROUTING owns checkers.

**Preconditions, all required (the "safe" in the owner's request).**

- No hostile within its detection radius of her or of the object (the
  `_lone_target` radii, COMBAT.md), and no unseen-contact hit in the last
  10 s.
- HP and SP at or above the cautious farm thresholds (heal at 95%), food
  above the hunger warning, and a ready recall source with known charges
  (`known_charge`, COMBAT.md "Safety").
- Not inside an owner block or a hazard cell (ROUTING 4.7, 4.4).
- No other player in view. A lever can close a gate on someone else; a
  spawn trap can kill someone else. Players are counted, never named.
- Not on a map whose region is an apartment or house other than her own
  (player housing), and not a shop's staff door.
- She has spent no consumable to get here and will spend none: she never
  drops, gives, burns or uses an item to test a hypothesis. Keys are tested
  only by carrying them. A checker that consumes its key
  (ROUTING's `Consumes`) is never "tried".
- Budget: at most 1 trial per object a day, 20 a day overall, and none for
  an object already `opened`, `toggled` or `suspected-unopenable` within
  7 days.

**Traps.** Opening a door springs any rune in its inventory
(`door.c:119-124`). Before her first trial of a door, chest or lever she
uses Find Traps if she has it (she does, level 5) and waits for its result;
a found trap makes the object `trapped` and she uses Remove Traps only if
its level is within her skill (otherwise she leaves it). Without the skill,
doors still get tried, at the cautious thresholds.

**Watching the outcome.** For 10 s after a lever, she records:
monsters that appear within 8 tiles that were not in view before
(`spawned`), any HP loss (`damaged`), a map change (`teleported`), tiles in
view whose objects changed (a gate opening or closing, `toggled`), and the
lever's own text. She does not walk into a newly opened passage as part of
the trial; navigation sees the change on its next plan. On `spawned` or
`damaged` she leaves at once with the existing retreat and recall, and the
lever is marked `hazardous` and never applied again without the owner.
The Altar of Loki (section 5.4) is the case this rule exists for.

**What a trial writes.** One `interactions` row; the `locks` row's state,
attempts and hint; the hint observation; and, for a lever, which tiles
changed. A lever whose effect was seen becomes a candidate
`lever-opens:<tile>` fact, confirmed when the same toggle repeats it.

### 4.5 Unlock knowledge into routing

This design produces knowledge; ROUTING consumes it. The plug points are
ROUTING's P2 (`UnlockState`, 4.3 there) and P4 (dialogue and quest-gated
edges, 4.2 there):

- **Confirmed openings.** A `locks` row with state `opened` and
  `opened_with = <item name>` adds that item's name as an alias for the
  lock's tile in `UnlockState`, the same way map-placed key names do today.
  `opened_with = force` (a door that opened "for you", `door.c:259-260`)
  becomes ROUTING's `Learned(edge key)` for that door's edge.
- **Refusals.** A refusal is evidence for ROUTING's
  `blocked-for-requirement`, with the door's own text as the message
  evidence ROUTING 4.3 requires. The rejection clears when a related input
  changes (a matching item arrives, a quest part completes), as there.
- **Hypotheses.** Candidate `key-name`, `key-holder` and `npc-key` facts do
  not open anything. They give the unlock-acquisition planner (which ROUTING
  leaves out of scope) and the quest policy a ranked list: "the orange
  stone giant key is held by Xuhrexal on `old_outpost_a_0203`".
- **Quest gates.** A `quest-gate` fact (a door or portal whose text or
  outcome ties it to a quest part) is offered to P4 as a hint for which
  journal part the edge's `Quest` atom should read. P4's own parsing of
  interface XML stays authoritative.
- **Unopenable.** `suspected-unopenable` (refused 3 times on 3 days while
  she held every candidate she knew) lowers nothing in ROUTING by itself; it
  is a feedback input (4.9) and stops further trials.

None of this lets a learned fact override a fail-closed `Unknown` atom
without a confirmed outcome, per ROUTING 4.1.

### 4.6 Books and facts for planning and combat

**Reading.** Books she finds are read once, before they are sold, as today,
but the text is kept:

- *Inventory books*: `maintain_inventory` already applies them. The book
  packet that follows is parsed into title and body and stored as an
  observation with `source_kind = book`.
- *World books* (on shelves, desks and corpses): when the container is open
  and safe, she takes the book, reads it and puts it back, unless it is
  unpaid. She never takes an unpaid item.
- *Scrolls*: only text scrolls (no spell) are read; spell scrolls stay
  untouched, because reading one casts it.
- *Low Int.* While her Int is 10 or less, reading forfeits the book's
  one-time Literacy experience (4.11.2). She still reads books for facts,
  but books are marked "read without experience" so the dashboard shows the
  loss.
- *Beyond comprehension.* "This book is ... beyond your comprehension"
  (`book.c:104-126`) keeps the book unread; she stores the attempt and its
  level gap, and keeps the book (instead of selling it) only if it was found
  on a quest map.

**Using the facts.**

- *Planner.* `trap-warning` facts mark a cell as hazardous in ROUTING's
  learned-cost terms, as a prior, not a wall ("STAY OFF THE BRIDGE" on
  `underground_city_2_3_-2`). `direction` and `key-holder` facts feed the
  quest policy's hints, never its required steps.
- *Combat.* `weakness` and `immunity` facts join the element choice for
  storm farming (COMBAT.md "Next phase"): a confirmed immunity (her hits
  show 0 damage of that type) outranks a read one, and both outrank the
  default. `tile-property: no_magic` makes the caster logic stop ordering
  spells on that square and step off it to heal, fixing section 2's 11,641
  futile casts. `tile-property` facts are confirmed by the server's own
  refusal, so they are confirmed on first sight.
- *Passwords.* A `password` fact is said only to an NPC, in a dialogue the
  quest policy chose, never in public chat.

### 4.7 Terrain tactics

**Features.** For each square she fights from, a terrain feature is computed
from the authored walkability she already plans with (`MapNode.blocked`,
doors), over a 5x5 window:

- `reach`: how many of the 8 neighbours a walking monster could stand on.
  1 or 2 means only one or two attackers can reach her at once.
- `corridor`: `reach` at most 2, and those squares are opposite each other.
- `doorway`: she stands on or beside a door or a one-wide gap between walls,
  with a room on one side.
- `open`: `reach` 7 or 8 and at least 20 walkable squares in the window.
- `room`: anything else.
- `cover`: whether a wall blocks the straight line to each ranged or caster
  monster in view, using the same ray cells as `bullet_ray_clear`
  (COMBAT.md "Aimed bullets").

Whether a monster can attack diagonally past a wall corner was not checked
in the server; that is exactly why `reach` is learned rather than assumed.

**Recording.** Each engagement writes one `fight_positions` row: the
feature at the moment the second attacker arrived (or at the start, for one),
the peak simultaneous attackers from the same 8 s window that
`review_failure` uses, ranged attackers, damage taken and outcome.
Aggregates per (feature, attackers in view, ranged present) give her a
learned damage rate and death rate, decayed with a 7-day half-life.

**Using it,** behind a flag `terrain_tactics`, after a shadow soak:

- *Packs of melee attackers.* When two or more are coming, `melee_kite`,
  `pack-split` retreat and `low_health_retreat` prefer a step toward a
  `corridor` or `doorway` square within 4 steps whose learned peak attackers
  is lower, as a tie-breaker after separation and before the pull anchor.
- *Ranged or caster monsters in view.* Prefer squares with `cover` from them
  and avoid `open` squares, again as a tie-breaker. This is the first
  path-level use of ranged positions, which COMBAT.md lists as a non-goal
  today; it stays a tie-breaker, never a reason to walk into a pack.
- *Pull squares.* `_new_child` already chooses pull squares by clearance;
  among equally clear squares it prefers the one with the lowest learned
  peak attackers.
- *Caster kiting.* `caster_kite` keeps its 4 to 8 tile band; a corridor
  behind her also lengthens the time a pack needs to reach her, which the
  shadow data can measure.

**Acceptance evidence before the switch**: the shadow log shows how often
the tie-breaker would have changed a step, and the replay of the 2026-11-14
Underground City deaths (ROUTING section 2) shows whether a corridor was
within reach.

### 4.8 Item examination and equipment comparison

#### 4.8.1 What "+N" does, per item type

Server facts, from `server/living.c`, `server/item.c`, `server/treasure.c`
and `types/arrow.c`:

| Item | What +N (`magic`) adds | Where |
| --- | --- | --- |
| Any worn item with base AC ≠ 0 | +N AC | `living.c:565-567` |
| Any worn item with base block ≠ 0 | +N block | `living.c:569-571` |
| Any worn item with base absorb ≠ 0 | +N absorb (percent) | `living.c:573-575` |
| Non-ranged worn item with base WC ≠ 0 | +N WC | `living.c:578-582` |
| Non-ranged worn item with base damage ≠ 0 | +N damage (and damage bonus if not a weapon) | `living.c:586-593` |
| Bow | +N to the fired missile's WC and damage | `arrow.c:205`, `arrow.c:240-241` |
| Arrow | +N damage and WC when it is fired | `arrow.c:96-98` |
| Body armour (type 16) | speed cap ×(1 + N/10) | `treasure.c:846-847` |
| Every item with +N | weight −10% a point, shop value +5,300 / +12,300 / +24,300 / +52,300 / +88,300 copper for +1 to +5 | `treasure.c:833-854` |
| Negative N | cursed | `treasure.c:829-830` |
| Rings, amulets, trinkets | N is never shown in the name (the description is shown instead) | `item.c:210-226` |

So "+N" adds nothing to combat on an item whose archetype has no AC, WC,
damage, block or absorb. In the locked content those are: capes and robes,
plain girdles, sandals and low boots, hats and generic crowns, all trousers
and skirts, plain gloves, and every light source
(`arch/items/armour/...`, `arch/items/light/...`). Artifacts can add base
stats to such an item; then "+N" works again.

The examine text shows base `(wc+N)(dam+N)(ac+N)(block+N)(absorb+N%)`
values (`item.c:1003-1011`, `item.c:1102-1107`), **without** the magic,
while the name shows the "+N" separately. A `forged silksteel ball and
chain +2` (a shield: base AC 2, WC 1, block 2, absorb 5,
`ball_and_chain.arc`) examines as `(wc+1)(ac+2)(block+2)(absorb+5%)` and
actually gives AC 4, WC 3, block 4 and absorb 7%. A `silk cape +2` examines
with no stats at all and gives only protections and a lighter weight.
Item condition scales weapon damage (`living.c:1173`) and every protection
(`living.c:596`); the client shows it only as `Con: c/q` with no meaning
attached (`inventory.c:302`).

#### 4.8.2 Equipment comparison

- **Examine carried gear once.** On a quiet tick (the wand-examine rule) she
  examines every carried, identified, unworn equipment item she has not
  examined at its current tag, and every worn item once a session, and parses
  the answer with the shop parser generalized from `_apply_examine_metadata`.
- **Effective stats.** An `EffectiveStats` model computes, per item, what it
  adds when worn, from the examine values, the "+N" rules above and the
  condition: AC, WC, damage, block, absorb, protections, attacks, stats,
  mana and regeneration (COMBAT.md "Caster gear scoring"), weight and speed
  cap.
- **Comparison.** For each slot, the difference between the best candidate
  and what she wears, valued at her current stats: protections against the
  damage types she has taken recently (`damage_taken_total` by type: impact
  and fire in section 2), AC and block against her recent hit rate, and
  caster value while Wizardry trains. `gear_score` is replaced by this model
  only after a shadow phase in which both scores are logged.
- **Dashboard.** An Equipment panel lists each slot: worn item, best
  candidate, per-stat deltas, and why ("+2 on a cape adds no AC").

#### 4.8.3 Feedback from examination

Two kinds of item finding are drafted, both aimed at `atrinik/classic`
(server item description or client):

- **"+N with no effect"**: an item she carries or sees in a shop whose name
  shows "+N" and whose type and base stats give it no combat effect. One
  item per archetype family, aggregated into a single draft listing the
  families she has met.
- **"Examine omits magic"**: an examined item whose base `(ac/wc/…)` differ
  from what wearing it changed on her player doll. She can measure this
  directly: the stats packet's AC, WC and damage before and after she
  applies it. A measured mismatch is the evidence.

Whether these are bugs or intended presentation is the owner's call; the
draft says "confusing", not "wrong".

### 4.9 Hint clarity, and when it becomes feedback

A lock or quest step gets a clarity verdict once she has had a fair chance:
she has met it at least twice on different days, read every sign and magic
mouth within 8 tiles of it, and talked to every NPC on its map and the maps
next to it that have dialogue.

Verdicts, from player-visible knowledge only (4.1 principle 2):

- `clear`: a deterministic fact named the requirement (key name, holder or
  quest part) and the outcome confirmed it.
- `generic`: the only text was the archetype's default ("You need a special
  key to open this door/gate.") and no other source named a key.
- `misleading`: a fact named something the content truth contradicts (the
  yellow force field that needs an orange key, section 5.3).
- `no-source`: the text implies it opens ("You need a ... key") but the
  content has no key, archetype, artifact, quest reward or script that
  provides the lock id (section 5.2). This verdict uses content truth and is
  marked so in the draft.
- `unexplained`: she found no fact at all and is not `generic`.

Only `misleading`, `no-source` and `unexplained` become drafts on their own,
one per lock. `generic` locks are aggregated: one draft per region listing
its generic locks with coordinates, because 107 of the 152 authored locked
doors use the default text (section 5.1) and one issue per door would
swamp the queue. Many are staff doors and vaults that are meant to stay
shut; the owner can reject the aggregate, and a rejected aggregate is not
redrafted for that region.

Quests get the same treatment at the part level: a part whose journal text
and dialogue gave her no fact that led to the place, item or NPC that
completed it (as recorded by the quest executor) is `unexplained`.

### 4.10 The feedback queue

#### 4.10.1 Kinds

| Kind | Meaning | Usual repo | Examples |
| --- | --- | --- | --- |
| `bug` | something that cannot work as written | content or classic | a door whose key exists nowhere; book experience lost at low Int |
| `hint` | an unclear or misleading hint or quest text | content | yellow field, orange key; aggregated generic lock texts |
| `balance` | evidence on a balance question | classic or content | melee vs. spells (as evidence on the existing issue) |
| `qol` | a quality-of-life improvement | classic | an NPC parked on a one-tile path; no message for a `no_magic` savebed |
| `clarity` | text or presentation that misleads | classic or content | "+N" with no effect; examine without magic; typos |

Routing to a repo is by producer: lock, sign, book, dialogue and quest
findings go to `atrinik/content`; item presentation, client display and
server formulas go to `atrinik/classic`. The owner can change the repo
before approving. Findings about the playtester itself (section 2's
starvation loop) are a separate internal list on the dashboard, never filed
by this pipeline (open question 4).

#### 4.10.2 Deduplication

- **Local.** `fingerprint` = sha256 of (kind, repo, subject, normalized
  claim). Subjects are stable: `lock:/map:x,y`, `region-generic:<region>`,
  `item-family:<archetype>`, `formula:book_exp_mod`. A new occurrence of an
  existing fingerprint increments `occurrences` and `last_seen`; it never
  creates a second draft. A rejected fingerprint stays rejected; a filed one
  only gains occurrences.
- **Remote.** Each filed issue carries a hidden marker,
  `<!-- playtester-fingerprint: <hex> -->`. Before an item can be approved,
  the dashboard shows possible duplicates: open and closed issues in the
  target repo containing the marker, and the top title matches for the
  subject's map path, object or item name. The search runs once when the
  draft is created, unauthenticated (public repositories, the search API's
  unauthenticated limit of 10 requests a minute, queued and rate-limited),
  and again with the token at filing. A marker match blocks filing and marks
  the item `duplicate` with the issue's link.
- **Existing balance issues.** A `balance` item whose subject matches an
  existing issue (for example
  [atrinik/classic#628](https://github.com/atrinik/classic/issues/628),
  "magic cannot miss") is drafted as an evidence *comment* on that issue,
  not a new issue, if the owner approves (4.11.3).

#### 4.10.3 Evidence format

Every draft body is generated from `evidence_json` with one template:

```markdown
### What happened
One or two sentences in plain words.

### Where
Map `/shattered_islands/world_5_57_-2` (13,14) and (14,14), content v1.14.0,
server build v5.99.0, playtester v1.64.0 (479efb5).

### What she tried
- 2026-11-20 14:02 UTC: walked onto (13,14) holding the orange stone giant key.
- ...

### What she saw
> The yellow force field is preventing you from reaching the well bucket...

### What she expected
The hint names the key colour that opens it.

### Her state
Level 36; Slash 34, Wizardry 15, Literacy 3; HP 275/275; carried keys:
orange stone giant key.

### Occurrences
3 times on 2 days; first 2026-11-18, last 2026-11-20.

<!-- playtester-fingerprint: 4f1c... -->
Filed by the Atrinik playtester after owner review.
```

Message excerpts are server and NPC lines only, at most 10 lines and 1,500
characters. Content truth, when used (a `no-source` verdict), is in its own
"What the content says" section with file paths, so a reader can tell what
she saw from what the maps say.

#### 4.10.4 Privacy

- Player chat types never enter observations, drafts or excerpts.
- Every excerpt line is checked against the names of other characters she
  has seen (chat senders, party members, player objects in view, PvP kill
  lines), case-folded; a line containing one is dropped, not masked.
- No account name, server address, invite code, certificate or token ever
  appears in a draft. Her character name appears only as "she" or
  "the playtester".
- A draft from a PvP or shared area records only that other players were
  present, as a count.
- The owner sees the exact body that will be filed and can edit it.

#### 4.10.5 Approval

- Drafts appear on a dashboard **Feedback** tab: list, filters by kind and
  repo, the rendered body, possible duplicates, and buttons to approve,
  reject, snooze (7 days) and edit title, body, labels and repo.
- New endpoints: `GET /api/feedback`, `POST /api/feedback/<id>` with an
  action. They follow the existing loopback and Origin rules
  (`web_server.py:796-818`).
- **Approval needs more than reaching the dashboard.** Today a POST with no
  `Origin` passes, and the dashboard is reachable through the lab's reverse
  proxy (1.9). Approval and edit actions therefore also require an approval
  key: a random secret in an owner-only file (mode 0600) outside the
  repository, sent in an `X-Playtester-Approval` header that the dashboard
  asks for once a browser session and keeps in `sessionStorage`. Without
  the file, the Feedback tab is read-only and approval is possible only
  from the CLI, `atrinik-playtester feedback approve <id>`, run on CT130.
  Whether the proxy already authenticates is open question 1.
- Approval sets `approved`; filing is a separate step, so a click never
  waits on GitHub.

#### 4.10.6 Filing and the token

- A `FeedbackFiler` runs in the bot process as a background task (or as
  the `feedback file` CLI). It takes one `approved` item at a time.
- **Token.** The only credential is a GitHub token scoped to
  `atrinik/content` and `atrinik/classic`: Issues read and write and
  Metadata read, nothing else (a fine-grained personal token or a GitHub
  App installation token; open question 2). It lives outside the repository
  and the state directory, for example as a systemd credential
  (`LoadCredential=`) or `/etc/atrinik-playtester/feedback-token` (mode
  0400, owned by the service user). The filer reads it at filing time,
  uses it, and drops it. It is never in the environment of the game
  process, in dashboard state, logs, checkpoints, metrics, decisions or
  `doctor` output. The existing `GITHUB_TOKEN` for dependency downloads is
  a different credential and is not used.
- **Steps.** Re-run remote dedup with the token; on a marker match, mark
  `duplicate`. Otherwise `POST /repos/{repo}/issues` with title, body,
  labels and, for org issue types (`Bug`, `Feature`, `Task`), the type. Record
  the URL and a `feedback_filings` row. On failure, `failed` with the error;
  the owner can retry.
- **Labels.** Only labels that already exist are used; the token cannot
  create labels. `atrinik/content` has `bug`, `enhancement`, `maps`,
  `question`; `atrinik/classic` has `bug`, `enhancement`,
  `component: server`, `component: client`. A shared `playtester` label
  would make her issues findable; whether to create one is open
  question 3.
- **Rate limits.** At most 5 filings an hour and 20 a day, at least 60 s
  apart, and no filing while GitHub's `x-ratelimit-remaining` is under 50.
  A 403 or 429 honours `Retry-After` and backs off exponentially from 10
  minutes. These limits are well inside GitHub's secondary limits for
  content creation.
- **Dry run.** Without a token, an approved item stays `approved` and the
  dashboard offers "copy as Markdown" so the owner can file it herself.

### 4.11 Other pain points from her history

#### 4.11.1 Tile facts

The `no_magic` savebed (section 2) is the general case: fixed server
refusals tied to her square are tile facts, confirmed on first sight, kept
in `facts` with `kind = tile-property`. The first set: "Something blocks
your spellcasting." (`no_magic`), "You are unable to read while blind."
(not a tile, ignored), and the server's no-prayer and no-harm refusals.
Caster code reads them before ordering a cast. A `qol` draft is offered for
savebeds that block magic with no message explaining why, as a low-priority
item the owner may reject.

#### 4.11.2 Book experience at low Int

`book_exp_mod` (`types/book.c:73-76`) is negative for Int 1 to 10, from
-3.00 to -1.10, then 1.00 from Int 11. The book's experience is multiplied
by it (`book.c:163`), so it becomes negative; `calc_skill_exp` returns 0 for
negative experience (`server/skill_util.c:192-194`); and the book is still
marked read, so its one-time experience is gone (`book.c:175`). At Int 10, a
"below average" character gets nothing from any book, ever. The values
read as if the sign is wrong (0.9 to 0.0 would match the comment "less
experience"). This is drafted as a `bug` for `atrinik/classic`, and the
playtester stops reading for experience below Int 11 (4.6).

#### 4.11.3 Balance evidence

The owner noted that issues already cover melee against spells; the one
found is [atrinik/classic#628](https://github.com/atrinik/classic/issues/628).
Sera's job is evidence, not opinions:

- A comparison report per level band and mode (melee, single-target spell,
  area spell): damage per second, damage per mana, kills per hour, damage
  taken per kill and deaths per hour, from her own hit lines and the
  existing counters (`damage_dealt_total` by attack, `spells_cast_total`,
  `kills_total`, `deaths_total`), over windows of at least 2 hours of
  fighting per mode against the same monster set.
- Firestorm and icestorm numbers come once storm farming runs (COMBAT.md
  "Next phase"); until then the report says so.
- It is drafted as a comment on the matching existing issue, with the table
  and its windows, and refreshed at most once a week.

#### 4.11.4 Recurring navigation friction

`navigation-occupant-blocked` was a third of her recent decisions, mostly
on `world_7_64`. When the same square blocks her 10 times in a day and the
occupant is a named NPC, a `qol` draft for content lists the square and NPC
("an NPC stands on a one-tile path"). Monsters are not reported.

#### 4.11.5 Feedback categories this adds

From the above, the producers in the first release are: lock clarity
(4.9), quest-part clarity (4.9), "+N with no effect" and "examine omits
magic" (4.8.3), typos in lock and sign text (a small dictionary of common
slips such as "a the"), the book experience formula (4.11.2), magic-blocking
savebeds (4.11.1), path-blocking NPCs (4.11.4) and balance evidence
(4.11.3).

### 4.12 Metrics and dashboard

New Prometheus series (same labels and caps as today):

| Metric | Labels | Counts |
| --- | --- | --- |
| `observations_total` | `source` | observations stored, by source kind |
| `facts_total` | `kind`, `status` | facts created and their status changes |
| `lock_trials_total` | `kind`, `outcome` | doors, levers and signs tried |
| `locks_known` (gauge) | `state` | locks by state |
| `fight_positions_total` | `feature`, `outcome` | engagements by terrain |
| `feedback_items` (gauge) | `status`, `kind` | queue size |
| `feedback_filed_total` | `repo`, `result` | filing attempts |
| `futile_actions_total` | `reason` | casts refused by a tile property, reads beyond comprehension |

`futile_actions_total{reason="no_magic"}` would have shown section 2's loop
within minutes; an alert fires when it passes 20 in 10 minutes.

Dashboard tabs: **Knowledge** (recent readings, facts by kind, locks with
state and hints, lever outcomes), **Equipment** (4.8.2), **Feedback**
(4.10.5) and a **Terrain** panel on the Farming tab (learned rates by
feature).

### 4.13 Tests

- Parser fixtures from every real text quoted in section 5, with expected
  facts, and negative fixtures (player chat that quotes a door message
  never becomes an observation).
- Attribution: a recorded message stream after a bump attributes the navy
  line to the door; a magic mouth line on the square is attributed by
  content match.
- Trial safety: each precondition alone prevents a trial; a spawned monster
  ends the trial with a retreat and marks the lever hazardous; no trial
  with a player in view.
- "+N" model: table-driven against archetype fixtures (cape, cloak, ball and
  chain, bow, arrow, torch); a stats-packet before/after fixture for the
  measured mismatch.
- Feedback: fingerprints are stable across restarts; redaction drops a line
  naming a seen player; mocked GitHub for duplicate, success, 403 with
  `Retry-After`, and the hourly cap; the token is never in any serialized
  state.

## 5. Concrete examples from the content

All from the local content checkout (see "How the evidence was gathered").

### 5.1 Generic lock texts

152 authored locked doors and gates carry a `slaying`. 107 of them use the
archetype's default text: 72 "You need a special key to open this door."
and 35 "You need a special key to open this gate." 30 more have the
`no_entry` lock id, which no key opens: "The prison cell is locked."
(`world_3_59`), "The Casino is currently closed." (`world_10_45` (4,18)),
"LandShare Industries is currently not open." (`world_10_44` (2,23)). These
are probably decoration, and her verdict would be `suspected-unopenable`,
not feedback, unless the owner wants "currently closed" doors flagged.

### 5.2 A door whose key exists nowhere, with a typo

`/shattered_islands/world_4_58` (Aris) (19,12), `celestial_door_bar1`,
`slaying bar_key_aris`, text "You need a the key to open this door." No map,
archetype, artifact, interface or script in the checkout mentions
`bar_key_aris`. Verdict `no-source`, plus a typo. One `bug` draft for
`atrinik/content`. (34 other locks have no key source either, but most are
bank, vault and staff doors, or force-based doors that markers open, so
"no source" alone is not a bug; only the text decides.)

### 5.3 A misleading colour

`/shattered_islands/world_5_57_-2` (13,14) is a magic mouth: "The yellow
force field is preventing you from reaching the well bucket..." The square
(14,14) holds `forcefield_yellow_0` and a checker for the **orange** stone
giant key, which is what opens it. In the Old Outpost the colours agree:
`old_outpost_a_0203` (22,10) says "The orange force field prevents you from
reaching the door. [i]Xuhrexal, the stone giant[/i] is engraved on the
door.", and Xuhrexal (level 26) carries the orange stone giant key on the
same map. A player told "yellow" looks for a yellow key that does not
exist. Verdict `misleading`, a `hint` draft for content. COMBAT.md already
names the well as needing the orange key, so the playtester knows; a human
player would not.

### 5.4 Clear hints, for contrast and for facts

- `old_outpost_a_0203` (22,10) and (22,15): colour plus the holder's name
  engraved on the door. `key-holder` facts: Xuhrexal, Ksha'sha (the troll,
  on `old_outpost_a_0202`). Verdict `clear` once she opens one.
- Asteria's gate room `world_5_44_-1`: gates say "You need Asteria Dungeon
  Key 1/2/3". Key 1 is carried by Warden Argoth (level 20) on the same map
  (19,17), whose dialogue begins "I don't suppose you've come about the
  key. Sorry, I can't give that to you..." and turns him hostile after six
  insults (`interfaces/strakewood_island/asteria/warden_argoth.xml`). Key 2
  drops from the level-30 "stair guardian golem" on `world_6_44_-1`. Facts:
  `npc-key` (Argoth), `key-name` (three keys).
- The drow intelligence report (`world_1_49_-3`, on a desk) explains the
  same system from the other side: three layers of locked doors, "each guard
  in Asteria possesses the first of these keys", and keys that teleport back
  unless exchanged at an "exchange point". It also says the gates open only
  for "an, as yet, unknown password". Facts: a `quest-gate` hint and a
  `password` fact with no value, which is itself a clarity signal.

### 5.5 A lever that is a trap

`/shattered_islands/berri_lur/temple_loki/tol_0101` (11,8): a
`handle_lever` named "Altar of Loki", text "You appear to have company...",
connected to spawn points that release level-94 "living dead girl"
monsters. The lever at (11,10) toggles magic walls instead. This is the
case the lever rules in 4.4 are for: full health, no players near, observe
10 s, retreat and never again on `spawned`.

Other levers carry a lock: 28 levers and switches have a `slaying`, 19 of
them Asteria's "big lever"s (`slaying asteria_walls_gate`). Without the
key they answer "The big lever is locked." (`handle.c:45-48`), with no hint
of what unlocks them.

### 5.6 A book with a useful fact

`underground_city_2_3_-2`, "note: Day 6 - 7" on a corpse (level 10, so
Literacy 3 at Int 11 or more can read it): "a ---ic bul--- was needed. It
must be cast northwest from a special location. We have marked the spot
with a fake torch" and "STAY OFF THE BRIDGE, IT IS A TRAP". The map has
pedestals that react to `race bullet` (7,2) and (7,4), and one green wall
torch among ordinary ones at (23,2). Facts: `direction` (magic bullet,
northwest, from a marked torch), `trap-warning` (the bridge). She knows
magic bullet. Whether the green torch is the marked spot was not verified.

The blood-stained letter on `old_outpost_a_0202` ("He is guarded by magic
doors. We will never find the keys...") gives a weaker `quest-gate` hint for
King Rhun's vault, which COMBAT.md lists as the unfinished second crystal
upgrade.

### 5.7 A confusing item stat

Her own `silk cape +2` (section 2): capes have no base AC, so "+2" adds only
lighter weight and shop value, and the examine text shows no stats. Her
`forged silksteel ball and chain +2` examines as `(wc+1)(ac+2)(block+2)
(absorb+5%)` and gives AC 4, WC 3, block 4 and absorb 7%. Both become one
`clarity` draft for `atrinik/classic`, with the measured before/after doll
stats as evidence.

## 6. Phased plan

Each phase is one small pull request that can ship alone, with tests, and
changes no behaviour unless it says so. S0 is not part of this design but
blocks judging any of it on the live character.

**S0. `fix(survival)`: keep an idle character alive.** With no task
running, she eats what she carries, buys food at the temple with what she
has, or logs out before starving. Track separately (see the open
`fix/starvation-temple-route` branch first).

**F1. `feat(memory)`: an observation ledger.** The `observations` table,
attribution (4.3.1) for bumps, applies, walk-ons, interfaces, book packets
and examines, the privacy filter, caps, and a read-only Knowledge tab.
*Accept:* a recorded stream yields the expected attributed rows; chat is
never stored; book packets parse into title and body; a full table evicts
the oldest unreferenced rows; no behaviour change.

**F2. `feat(knowledge)`: deterministic fact parsers and tile facts.** The
`facts` table and the parsers in 4.3.2. Caster code stops casting on a
confirmed `no_magic` square and steps off it to heal (behaviour change,
small and safe). The `futile_actions_total` counter and alert.
*Accept:* every section 5 text gives its expected facts; the section 2
message stream produces one `no_magic` fact and at most 3 refused casts on
that square afterwards; no LLM dependency.

**F3. `feat(inventory)`: effective item stats and an Equipment panel.** The
"+N" model (4.8.1), examining carried gear once, `EffectiveStats`, the
panel, and shadow logging of old and new gear scores. No scoring change.
*Accept:* table tests for each archetype family in 4.8.1; the live
inventory in section 2 shows the cape's "+2" as no AC; doll before/after
fixture matches the model.

**F4. `feat(inventory)`: score gear by effective stats.** Switch
`gear_score` to the model behind a flag, after the owner reviews a day of
shadow logs.
*Accept:* the shadow divergence list is attached to the PR; with the flag
off, choices are unchanged.

**F5. `feat(feedback)`: the draft queue.** `feedback_items`, fingerprints,
the evidence builder and template, redaction, the Feedback tab (read-only
drafts), unauthenticated remote dedup, and the first producers: "+N with no
effect", "examine omits magic", the book experience formula, typos.
*Accept:* drafts are deduplicated across restarts; redaction tests pass; no
network call except rate-limited public search; nothing is filed.

**F6. `feat(feedback)`: owner approval and GitHub filing.** Approval key or
CLI, the filer, token handling, labels, issue types, rate limits, dry run,
audit rows, and balance evidence as comments.
*Accept:* with no token, approval leaves the item approved with a Markdown
copy; mocked GitHub tests for success, duplicate marker, 403 with
`Retry-After` and the hourly cap; the token never appears in state, logs or
metrics (a test serializes all of them and searches); a POST without the
approval key is refused.

**F7. `feat(exploration)`: safe lock and lever trials.** `locks`,
`interactions`, `LockTrialTask` with the preconditions and lever
observation in 4.4, Find Traps before first trials. Flag `try_locks`,
default off; shadow decisions while off.
*Accept:* each precondition alone blocks a trial; a synthetic spawn after a
lever ends the trial with a retreat and marks it hazardous; budgets hold;
the owner turns the flag on after a supervised session.

**F8. `feat(navigation)`: learned unlocks into routing.** Confirmed
openings and refusals feed ROUTING's `UnlockState` and `Learned` atoms;
quest-gate facts are offered to P4. Depends on PR #95's P2 (and P4 for the
quest part).
*Accept:* a door opened with a key not in `access_item_names` becomes
routable for her; a refusal with message evidence blocks the edge until a
related input changes or 24 h pass; a candidate fact alone opens nothing;
no `Consumes` atom is ever satisfied by a learned record.

**F9. `feat(feedback)`: hint and quest clarity.** The fair-chance rule and
verdicts (4.9), regional aggregation of generic texts, and quest-part
clarity.
*Accept:* fixtures for 5.2 (`no-source`), 5.3 (`misleading`) and 5.4
(`clear`) produce those verdicts; generic locks aggregate to one draft per
region; rejected aggregates are not redrafted.

**F10. `feat(combat)`: record fights by terrain.** Terrain features and
`fight_positions`, the Terrain panel. No behaviour change.
*Accept:* a synthetic corridor map yields `reach` 2 and `corridor`; peak
attackers match `review_failure`'s on recorded fights.

**F11. `feat(combat)`: terrain-aware tie-breakers.** The rules in 4.7 behind
`terrain_tactics`, after a shadow soak and the owner's approval.
*Accept:* in a synthetic map, a two-monster pack retreat picks the corridor
when separation ties; with ranged monsters in view, a covered square wins a
tie; separation still wins over terrain; with the flag off, steps are
unchanged.

**F12. `feat(knowledge)`: optional offline interpreter.** The
`Interpreter` interface, the no-op default, an out-of-process local-model
adapter, queueing and the dashboard indicator.
*Accept:* with no endpoint configured, nothing runs and no text leaves the
process; LLM facts are capped at 0.5 confidence and never produce a clarity
verdict, unlock or draft by themselves.

## 7. Open questions for the owner

1. **Approval security.** Does the lab proxy in front of
   `playtester.atrinik.orion.milkyway` authenticate? If it does not,
   is the approval key in an owner-only file (4.10.5) acceptable, or should
   approval be CLI-only on CT130?
2. **Token form.** A fine-grained personal token, or a small GitHub App
   installed on `atrinik/content` and `atrinik/classic` with Issues write?
   Whose account files the issues?
3. **Labels and types.** Create a `playtester` label in both repositories,
   and use the organization issue types (`Bug`, `Feature`, `Task`)?
4. **Findings about the playtester itself** (the starvation loop, futile
   casts): dashboard-only, or also filed to `atrinik/playtester` through the
   same approval?
5. **Generic lock texts.** One aggregated draft per region, a single global
   one, or none (they are intended)?
6. **"Currently closed" doors** (`no_entry` locks that suggest they might
   open): report, or treat as decoration?
7. **Balance evidence.** Comments on existing issues such as
   [atrinik/classic#628](https://github.com/atrinik/classic/issues/628),
   or a separate report artifact?
8. **Trial scope.** Are lever and door trials allowed in towns and quest
   maps, or only away from other players' usual paths? Is 20 trials a day
   right?
9. **LLM.** Allowed at all? If so, local only, and on which host?
10. **Live character.** Sera has 66 depletion points from the idle
    starvation loop. Should S0 come first, and should she be restored or
    restarted before the new features are evaluated?

## 8. Known uncertainties

- The examples come from content `v1.14.0-4` (local checkout), not the
  exact locked archive.
- Monster diagonal attacks past wall corners were not checked, so `reach`
  in 4.7 is a model to be confirmed by her fights.
- Whether the client sees the contents of her closed key ring was not
  verified (ROUTING section 7). Key trials rely on the server searching
  containers (`key.c:52-62`), which it does.
- Attribution by timing assumes the server answers a bump or apply in the
  same or next tick; heavy lag could misattribute a line. Content matching
  backs it up only where authored text exists.
- The green torch in 5.6 being the "fake torch" is an inference.
- The stats the dashboard reports (Str 3, Int 1) were read from
  `/api/state` and not cross-checked against the server.
- The proxy's authentication (1.9) was not checked.
- 4.11.2 reads the book experience table as a sign error; the original
  intent was not checked in Classic history.
