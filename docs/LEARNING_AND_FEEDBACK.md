# Learning from the world and playtester feedback

Status: design proposal. Phases F1 (the observation ledger), F2 (fact
parsers and tile facts), F3 (effective item stats), F5 (the draft
feedback queue) and F10 (fights by terrain) are implemented; "F1 as
built", "F2 as built", "F3 as built", "F5 as built" and "F10 as built" in
section 6 record where they differ from this text. Nothing else here is
implemented, and nothing here changes behaviour until the phased pull
requests at the end land.

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
   bugs, and on `atrinik/playtester` for findings about the bot itself. The
   only credential is a small GitHub App with Issues write, whose key only
   the separate filer reads, and only when an item is filed.
6. **Anything else her history shows,** including equipment comparison,
   clearer item examination (what "+N" really does), and evidence-based
   balance comparisons. She does not duplicate the balance issues that
   already exist.

Related work, which this design does not duplicate:

- **Routing** ([`docs/ROUTING.md`](ROUTING.md), merged in
  [PR #95](https://github.com/atrinik/playtester/pull/95)). It owns the edge
  model, `UnlockState` (its 4.3, phase P2), dialogue and quest-gated edges
  (4.2, P4), owner blocks and care crossings (4.7, P3), the route tables
  including `route_requirements` (4.4, P5), the cost model (P6) and the
  probe limits (4.6). Unlock knowledge from this design is an input to P2,
  P4 and P5 (4.5 below); it never decides routes itself.
- **Idle survival** ([PR #97](https://github.com/atrinik/playtester/pull/97),
  `IdleSurvival` and `spell_blocked_here`). It fixes the starvation loop and
  the futile casting described in section 2. This design builds on it and
  does not redesign it.
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
  (4.2), and its dialogue sweep (F13) is the first piece of #17.
- **Combat** ([`COMBAT.md`](../COMBAT.md)). Terrain tactics (4.7) are
  tie-breakers inside the existing pull, kite and retreat logic, not a new
  fight loop.

## How the evidence was gathered

- Source: this repository at `9c61751` (origin/main, after #94 and #95).
  Line numbers refer to that revision.
- Content: the local `atrinik/content` checkout at `b9657299a`. An
  independent review confirmed that its maps match the locked content input
  (v1.14.0, `49f5aab`). All maps were scanned offline with a small parser
  for doors (type 20), keys (21), inventory checkers (64), handles (93),
  signs and magic mouths (98), books (8), scrolls (111) and the objects
  each handle's `connected` value drives, including objects nested in
  monster inventories.
- Classic server and client: the `classic` worktree at `f2273c85f`
  (`server/src`, `client/src`).
- Live state: one `GET /api/state` and one `GET /metrics` of the running bot
  (build v1.64.0, `479efb5`, uptime 20.9 h) on 2026-11-15 at about
  16:23 UTC. Nothing was posted to the bot. Her service has been stopped
  since.
- GitHub: read-only searches of `atrinik/classic` and `atrinik/content`
  issues and labels, and of the playtester's own issue list.
- The "+N", book experience and content claims were checked by an
  independent reviewer.

The probe scripts are not part of this change. Where something is
unverified, the text says so.

## 1. Inventory: what exists today

### 1.1 Server text (`messages`)

- Every DRAWINFO line goes into `GameState.messages` as
  `(time, type, color, text)`, bounded to the last 500
  (`model.py:308-313`), with a monotonic position counter so watchers can
  read "messages since" (`model.py:315-332`). `messages_since` drops player
  chat types and any line carrying the player-name link
  `PLAYER_NAME_LINK` (`[a=#charname]`, `model.py:104`, `model.py:331`)
  unless asked, so other players cannot forge server feedback.
- `_handle_drawinfo` (`client.py:1415-1511`) parses a fixed set of lines:
  bank balances, party setup, the savebed notice, deaths, depletion stat
  losses and heals. It also feeds the post-mortem review buffer
  (`review_messages`, bounded to the review window and to
  `post_mortem.MAX_TEXT` characters a line).
- Nothing records which object a line came from. DRAWINFO carries no source
  object. Hint text arrives as an ordinary game line: a locked door's `msg`
  in navy (`00ffff`, `door.c:246`, `door.c:297`), a sign's or magic mouth's
  `msg` in navy (`sign.c:107`), a lever's `msg` or "You turn the big lever."
  in white (`handle.c:64-71`). A sign or magic mouth fired by a connection
  broadcasts its text to everyone near it (`draw_info_map`,
  `sign.c:189-199`), so she can receive text that another player or a lever
  caused.
- Typed notifications (`CLIENT_CMD_NOTIFICATION`, which a sign can send,
  `sign.c:110-145`) are not dispatched at all
  ([#21](https://github.com/atrinik/playtester/issues/21) records this too).

### 1.2 Interfaces and NPC dialogue (`interface`)

- `_handle_interface` (`client.py:2318-2362`) decodes the interface packet
  into `InterfaceState` (`model.py:190-198`): title, joined text, links,
  input metadata and objects. Each new interface replaces the previous one;
  no dialogue page is kept. The server has already substituted
  `{activator.name}` with her character name in the text she receives.
- `choose_interface_link` (`client.py:2538-2565`) follows a link.
  `DialogTask` (`quest_tasks.py:13-112`) says `hello` and follows reviewed
  choices; quest executors in `catalog_quest_tasks.py` drive scripted
  dialogue. Vendors are found from interface XML (`navigation.py:144`).
- Dialogue text is used only by the task that opened it, and for the bank
  balance (`client.py:2360`). Nothing is parsed for keys, places or
  passwords.

### 1.3 Books and scrolls

- Book packets (`CLIENT_CMD_BOOK`) are appended raw to `GameState.books`,
  last 20 kept, in memory only (`client.py:2364-2375`, `model.py:287`). Only
  the quest book is parsed (`quests.parse_quest_book`, `quests.py:143-182`).
  A book a player wrote and signed carries the signer's name and date after
  its text (`book.c:136-142`; signing sets them in `book_edit.c:476-486`).
- `FarmTask.maintain_inventory` applies each unread inventory book once for
  Literacy experience (`tasks.py:3196-3217`). Attempts persist as
  `lore_attempt_counts`, keyed by Literacy level, face, type and name
  (`client.py:776-813`). The text she then receives is discarded.
- Books are then sold as junk once identified
  (`InventoryPolicy.sellable_junk`, `tasks.py:787-800`). Map books on
  shelves or corpses are never picked up for reading.
- Scrolls (type 111) are identified and used only as spell scrolls; none of
  the four authored scrolls is read for text.
- Players can rename items (`commands/player/rename.c`); examine then says
  "You name it ..." (`types/player.c`, `examine`). Nothing in the
  playtester notices this.

### 1.4 Quest journal

`request_quests` asks for the quest book; `parse_quest_book` turns it into
`QuestProgress` with parts, descriptions, status and kill counts
(`quests.py:143-182`). The dashboard shows it (`web_server.py:260`). The
catalog (`quests.load_catalog`, `quests.py:119`) reads `quest.xml` for
formal quest executors. Part descriptions are not mined for facts, and
nothing ties a journal part to a door (ROUTING 1.3 says the same for
routing).

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
(`client.py:862-907`); action history keeps 500 in memory. ROUTING P5 adds
`route_events`, `route_traversals`, `route_blocks` and `route_requirements`
(ROUTING 4.4); none exists yet.

### 1.6 Doors, gates, levers and signs

- **Graph.** `WorldGraph.build` marks authored locked doors and blocking
  inventory checkers as locked *tiles* with a requirement or exclusion
  (`navigation.py:699-722`); `access_item_names` maps lock ids to key names
  (`navigation.py:723-732`); one conditional magic mouth carries a gate's
  key id (`navigation.py:516-532`). `MapNode.lock_allowed` checks one tile
  (`navigation.py:256-270`). Locked doors are not edges; ROUTING 1.2
  inventories this in full.
- **Ordinary doors** are left to the server's click path, which opens them
  (`navigation.py:557-559` comment). `_escape_closed_door`
  (`navigation.py:2399-2450`) and `_bump_boundary_door`
  (`navigation.py:2452`) bump a closed door when the click path stalls.
- **Locked doors** are avoided by the planner unless her inventory carries a
  matching key name (`_allow_locked`, `navigation.py:1734-1742`). She never
  bumps one to read its message, and never records the outcome.
- **Levers and switches** (type 93): no code applies one. The only map-object
  applies are exits (`navigation.py:2719-2727`) and two scripted quest
  steps (`catalog_quest_tasks.py:139`, `catalog_quest_tasks.py:293`).
- **Signs and magic mouths**: magic mouths fire when she walks over them,
  and their text lands unattributed in `messages`. Signs are never applied.

### 1.7 Item examination

- `AtrinikClient.examine(tag)` sends ITEM_EXAMINE (`client.py:2458`). The
  answer is a set of ordinary game lines; the Classic client shows them in
  the message window and nothing else
  (`client/src/gui/widgets/inventory.c:1112-1123`). The client's inventory
  row shows only name, `Con: c/q`, required level and weight
  (`inventory.c:285-331`).
- Three examine readers exist, each for one purpose:
  - recall-wand charges, "It has N charges left." (`tasks.py:348-370`,
    `tasks.py:4552-4586`);
  - the mana crystal's capacity and charge (`ManaCrystal.read_examine`,
    `tasks.py:1033-1048`);
  - shop stock, where `_apply_examine_metadata` (`tasks.py:5816-5904`)
    reads `(wc+N)(dam+N)(ac+N)(block+N)(absorb+N%)`, weapon delay,
    protections, attacks, stats and caster bonuses into
    `examined_gear_score`.
- Carried gear is scored from its *name* alone
  (`InventoryPolicy.gear_score`, `tasks.py:876-897`): quality, condition,
  `named_bonus * 8` for every "+N" in the name, 120 if magical, and the
  caster bonuses. `worthless_plus` (`tasks.py:776-785`) knows that "+N" does
  nothing on a light source, and nothing else.

### 1.8 Fight review and positions

`self_improvement.review_failure` (`self_improvement.py:106-173`) classifies
a death or recall as pack, resources, damage type, out-damaged or route, and
`FarmCircuitTask._learn_from_failure` (`navigation.py:5074`) raises the
spot's adaptation level. Pull squares are chosen from authored aggro radii
in `FarmCircuitTask._new_child` (`navigation.py:5452`, the pull-square
search through about line 5543; COMBAT.md). Position quality is not
recorded: nothing knows whether she stood in a corridor, a doorway or the
open when she won or lost a fight. `low_health_retreat` (`tasks.py:4068`)
scores steps by separation and escape area, not by how many attackers can
reach a square.

### 1.9 Dashboard and metrics

- `GET /api/state`, `GET /metrics`, `GET /api/chat`, `GET /api/face/*` and
  `POST /api/task`, `POST /api/action` (`web_server.py:842-899`). The
  listener must be loopback (`web_server.py:730-772`). A POST is refused only
  when it carries a cross-origin `Origin` header (`web_server.py:819-822`);
  a request with no `Origin` is accepted.
- The live dashboard is reachable from the home lab at
  `playtester.atrinik.orion.milkyway`, so a reverse proxy forwards to the
  loopback listener with a loopback `Host`. Whether that proxy
  authenticates, and whether it forwards, strips or rewrites `Origin`, was
  not checked: the bot is stopped and contact was limited to GET. The
  check is a single owner-run request (open question 1). The design does
  not depend on the answer (4.10.5).
- The page builds HTML with template strings and an `esc` helper
  (`web_static/app.js:8`). Responses send `X-Content-Type-Options: nosniff`
  (`web_server.py:795`) but no Content-Security-Policy.

## 2. What her history shows

The snapshot on 2026-11-15 is a poor day, and it is the most useful evidence
in this document.

**An idle starvation loop.** The operator stopped `autoplay:level-115` at
20:54 UTC on 2026-11-14. Since then Sera had no task. She starved to death
every 51 to 54 minutes, the first time on `world_12_76` and from then on
at her Clearhaven savebed (`world_7_79` (16,3)): 22 starvation deaths in
the process lifetime (`deaths_total{cause="starvation"}`), all of them in
the decision ledger. Each respawn restores 500 food
(`types/player.c:1226-1229`) and each death drains stats. Her depletion
count reached 66, and the stats the dashboard reported were Str 3, Dex 3,
Con 3, Int 1, Pow 1, with max mana 20. She carried no food and no money.
The service has been stopped since, and
[PR #97](https://github.com/atrinik/playtester/pull/97) fixes the loop
(`IdleSurvival`: eat, else the temple food trip, with back-off and an
alert).

**Futile casting.** The bed square is `no_magic`
(`world_7_79` lines 2392-2398: `bed_1b.111`, `no_magic 1`). The server
answers every cast there with "Something blocks your spellcasting."
(`spell_util.c:423-429`). She ordered 11,641 minor healing casts
(`spells_cast_total`), and 135 of her last 150 messages were that line.
PR #97's `spell_blocked_here` marks the square for an hour and heals there
with items only. This design makes that a persistent tile fact (4.11.1).

**Before the stop.** The last 500 decisions hold 25 deaths. 22 are the
starvation loop; the other three were route deaths in the Underground City
(`underground_city_0_0` and `_0_2`), followed by an out-damaged recall from
a charred treant on the Burnt Forest circuit. The process counters add 5
pack deaths and 1 resources death. ROUTING section 2 covers the Underground
City incident. `navigation-occupant-blocked` is the commonest decision (165
of 500), mostly on `world_7_64`; `retreat-choice` (98) and
`emergency-disengage` (80) follow.

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
the clearest case: the cape archetype has no AC
(`arch/items/armour/cloak/cape.arc`), so its "+2" adds nothing to combat
(4.8), yet `gear_score` gives it 16 points for the "+2".

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
- **G4. Locks are binary.** A lock tile is open only with a matching key
  name in inventory. Quest-granted forces, NPC-held keys and "this one never
  opens" are not known; ROUTING P2 and P4 add the content side, but not what
  she learned by trying.
- **G5. No safe trial.** She never bumps a locked door or applies a lever to
  find out, and has no rules for doing so safely.
- **G6. Tile facts are short-lived.** PR #97 remembers a magic-blocking
  square for an hour; trap squares and other refusals are not remembered.
- **G7. No position learning.** Fights are not recorded by terrain, so
  corridors and doorways cannot be preferred on evidence.
- **G8. "+N" is a guess.** Gear scoring counts every "+N" as 8 points,
  whatever the item type; carried gear is never examined.
- **G9. No feedback channel.** Confusing hints, typos, impossible doors and
  engine oddities she meets go nowhere.
- **G10. Book experience is wasted at low Int** (4.11.2), and she reads
  books without checking.
- **G11. Other players' text is not separated** from the world's: signed
  books, renamed items and player names in dialogue would flow into any
  store built naively.

## 4. Design

### 4.1 Principles

1. **Her own observations only.** Every stored fact is something her client
   saw or did: a line she received, a page she read, an outcome she caused.
   No other player's text, name or actions are stored. That excludes chat,
   lines with a player-name link, books a player signed, items a player
   renamed, and anything from a player's corpse (4.3.1, 4.10.4). This
   matches the repository's ordinary-player rule.
2. **Two kinds of knowledge, kept apart.** The playtester already plans from
   content-derived data (the world graph, quest catalog, analyzer). That
   *content truth* may keep guiding routes, as ROUTING does, and it is
   **required for safety**: before she touches a lever or a scripted sign,
   what it is connected to is resolved from authored content (4.4).
   *Player-visible knowledge* is what the hints, books and outcomes told
   her. Hint clarity is judged only from player-visible knowledge; content
   truth is used only to score whether her reading was right and as
   evidence in a report. Otherwise she could never find a hint unclear.
3. **Deterministic first.** Parsers are regular expressions and small
   grammars over known server phrasings and authored text, with fixtures
   from real content. An LLM is optional, offline-capable, out of process
   and never required (4.3.3).
4. **Hypotheses are not unlocks.** A fact read from a book or sign is a
   candidate until an outcome confirms it. Only a confirmed outcome becomes
   learned access for routing (4.5), and none ever satisfies a `Consumes`
   atom.
5. **Shadow before switch.** Each behaviour change (trying locks, persistent
   tile facts, terrain tie-breakers, new gear scoring, gear measurement)
   first runs as "would do" decisions, behind a flag that the owner sets in
   the bot's configuration on CT130. Recording and the draft queue change no
   behaviour.
6. **Nothing leaves the machine without the owner.** Drafts stay local. No
   GitHub request of any kind, searches included, happens before the owner
   approves an item. The GitHub App key is read only by the filer (4.10.6).
7. **The world is shared.** She never changes the world in a way another
   player would notice and could not undo: no one-shot levers, no using up a
   sign's limited readings, no marking a shared book as read (4.4, 4.6).

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
  server TEXT NOT NULL, account TEXT NOT NULL, character TEXT NOT NULL,
  first_at REAL NOT NULL, last_at REAL NOT NULL,
  seen INTEGER NOT NULL DEFAULT 1,  -- times the same text came from it
  map TEXT, x INTEGER, y INTEGER,   -- where she stood the first time
  source_kind TEXT NOT NULL,   -- door | gate | lever | sign | magic_mouth |
                               -- npc | book | scroll | examine | journal |
                               -- system
  source_key TEXT NOT NULL,    -- stable id, see below; never NULL
  source_name TEXT,            -- "big lever", "Warden Argoth", book title
  attribution TEXT NOT NULL,   -- exact | timing | content-match | none
  authored INTEGER NOT NULL,   -- 1 when the text equals authored content
  text_hash TEXT NOT NULL,     -- sha256 of normalized text
  text TEXT NOT NULL,          -- bounded to 4000 characters
  color TEXT, msg_type INTEGER
);
CREATE UNIQUE INDEX observations_dedup ON observations
  (server, account, character, source_key, text_hash);
```

`source_key` is never NULL, because SQLite treats NULLs as distinct and the
unique index would not deduplicate them. Keys are stable across restarts:
`door:/map:x,y`, `lever:/map:x,y`, `sign:/map:x,y`, `npc:/map:Name`,
`book:Title:text_hash`, and `system:<map>` for an unattributed line. A
repeat of the same text from the same source is an upsert that raises
`seen` and `last_at`.

```sql
-- Facts extracted from observations (G3); candidates until confirmed.
CREATE TABLE facts (
  id INTEGER PRIMARY KEY,
  server TEXT, account TEXT, character TEXT,
  kind TEXT NOT NULL,      -- key-name | key-holder | lock-colour | password |
                           -- direction | weakness | immunity | trap-warning |
                           -- tile-property | quest-gate | unopenable
  subject TEXT NOT NULL,   -- a source_key, map cell, monster or item name
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
                              -- suspected-unopenable | forbidden
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
  source_key TEXT, action TEXT,   -- bump | apply | restore | read | examine
  preconditions_json TEXT,        -- hp, sp, threats, recall, keys, players
  resolved_targets_json TEXT,     -- the lever's connected objects (4.4)
  outcome TEXT,                   -- opened | refused | toggled | no-effect |
                                  -- spawned | trap | teleported | damaged |
                                  -- unknown
  effects_json TEXT,              -- tiles changed, monsters appeared, hp delta
  observation_ids TEXT            -- the lines in its window
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
  target TEXT NOT NULL,       -- new-issue | comment:<issue url>
  title TEXT NOT NULL, body_md TEXT NOT NULL,
  labels_json TEXT NOT NULL, issue_type TEXT,
  evidence_json TEXT NOT NULL,
  status TEXT NOT NULL,       -- draft | approved | checking | needs-review |
                              -- filing | filed | filed-unlabelled |
                              -- rejected | duplicate | snoozed | failed
  occurrences INTEGER NOT NULL,
  first_seen REAL, last_seen REAL,
  remote_candidates_json TEXT,-- possible existing issues, after approval
  decided_at REAL, decided_via TEXT,  -- dashboard | cli
  issue_url TEXT, error TEXT,
  UNIQUE (server, account, character, fingerprint)
);
CREATE TABLE feedback_filings (          -- append-only audit
  id INTEGER PRIMARY KEY, item_id INTEGER, at REAL, request TEXT,
  repo TEXT, http_status INTEGER, issue_url TEXT, error TEXT
);
```

Locks also write into ROUTING's `route_requirements` (4.5), which stays the
only table the planner reads for requirement outcomes. Its columns keep
ROUTING's meaning: `inputs` is ROUTING's fingerprint of the related quest and
item state, and the key's name goes in `evidence`.

Caps (starting points): observations 50,000 rows a character, oldest
unreferenced first; interactions 20,000; fight positions 20,000; facts and
locks capped at 20,000 each (one per object met in practice); filed and
rejected feedback kept, drafts capped at 500 with the lowest-occurrence
drafts dropped first.

These tables are a subset of what
[#15](https://github.com/atrinik/playtester/issues/15) asks for:
`observations` is its source ledger and `facts` its claim store, with the
narrower kinds above. #15 can add kinds, contradictions and provenance
queries without migrating these rows.

### 4.3 Text understanding

#### 4.3.1 Ingestion and attribution

Text passes an ingestion filter before anything else sees it:

- **Player text is dropped.** Player chat types never enter
  `observations`, however they are phrased (`c.PLAYER_CHAT_TYPES`, as in
  `_handle_drawinfo`). Any line carrying `PLAYER_NAME_LINK` is dropped, the
  rule `messages_since` already applies (`model.py:331`).
- **Player books are dropped.** A book packet with a signer and date after
  its text (`book.c:136-142`), or whose examine shows "(signed; permanently
  finalized)" (`item.c:840`), was written by a player and is not stored.
  An unsigned book whose text matches no authored or generated text may be
  player-edited, so it is dropped too.
- **Renamed items are dropped.** An item whose examine says "You name it
  ..." keeps only its base archetype name in any record.
- **Her name and other players' names are replaced.** Her own character
  name in dialogue (the server's `{activator.name}`) becomes the literal
  `{activator.name}`, so dialogue matches its authored form and her name
  never reaches a draft. Names of other characters she has seen (chat
  senders, party members, player objects in view) cause the line to be
  dropped (4.10.4).

Then attribution. DRAWINFO has no source, so it uses what she just did, in
order of strength:

1. **Exact.** Book packets, interface pages and quest books carry their own
   identity (book title, interface title = NPC name, journal).
2. **Timing, only for her own action.** After she bumps a door, applies a
   lever or sign, or steps on a square, the server's answer arrives in the
   same tick or the next. The first non-chat line within 1.5 s whose colour
   matches the source type (navy for door, sign and magic-mouth text, white
   for levers and "You open the ... with the ...") is attributed to that
   object. Fixed server phrasings ("The %s is locked.", "The %s won't
   budge.", "You turn the %s.", "Nothing is written on it.", "You are unable
   to decipher the strange symbols.", "You open the %s with the %s.")
   confirm the kind.
3. **Content match.** When she is on or next to a square whose authored
   object has a `msg` equal to the received text (normalized), that object
   is the source. This uses content only to name the source, not to decide
   whether the hint was clear. It also attributes magic mouths she walked
   over.
4. **None.** The line is stored as `system:<map>` only if a parser finds a
   fact in it; otherwise it is not stored.

Sign and magic-mouth text can be *broadcast* by a connection that someone
else fired (1.1). Such a line is attributed only when she caused the
trigger herself in its window, or when content match names a source next to
her and no other player was in view. Otherwise it is `none` and never used
as a lock hint.

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
  elsewhere, and the dashboard says which interpreter is active. Only
  `authored = 1` observations are ever sent to it.
- LLM facts are hypotheses only. They can rank which lock to try, never
  open an edge, file feedback or decide a fight on their own. A hint-clarity
  verdict (4.9) never counts an LLM fact as "the hint was clear".

### 4.4 Trying doors, levers, signs and magic mouths safely

A trial is a `LockTrialTask`, offered by navigation and farming when she
passes within a few tiles of an untried object. Behind the flag
`try_locks`, default off; with it off, it logs `lock-trial-shadow`
decisions saying what it would have tried and why it would or would not.

#### 4.4.1 Resolve before touching

Before the first trial of any lever, switch, button or scripted sign, the
object's effect is resolved from authored content (principle 2):

- **Connections.** Every object on the same map with the same `connected`
  value is a target. The trial is refused, and the lock marked `forbidden`,
  unless every target is a gate, door, plain sign or handle, and when the
  map's difficulty or any spawned resident is above her level. The rule
  fails closed: an unknown target type refuses too. In the content, 8
  handles drive refused targets (spawn points, firewalls, creators, exits,
  a duplicator). The Altar of Loki (`tol_0101` (11,8)) drives 8 spawn
  points of level 93-95 monsters, and its twin (11,10) drives two
  firewalls. Zechna's temple lever drives 4 firewalls; a Greyton jail
  switch drives a creator; the other four are on the editor demonstration
  map `/planes/creation/creation_0801`: (4,19) a creator, (6,1) an exit,
  (10,7) a duplicator and (21,7) an exit.
- **One-shot levers.** A handle with `exp -1` stays down once pulled ("won't
  budge" afterwards, `handle.c:50-58`); in the content there is one. It is
  never tried: the change cannot be undone for other players.
- **Scripts.** A sign, magic mouth or handle with an event script (65 signs
  and mouths: 55 `trigger`, 8 `apply` and 2 `say` events, for example
  `start_lost_memories.py`) is never applied by a trial. Quest policy can
  use it on purpose, as it does today.
- **Allowed** targets are therefore only gates, doors, plain signs and
  other handles (37 handles drive gates), on maps at or below her level.

The 10 s watch after a pull (4.4.4) stays as a backstop, not as the safety
rule: by the time a level-94 monster appears it is too late.

#### 4.4.2 What she tries

- *Locked door or gate* (a door on a locked tile, or a closed door whose
  bump the server refused): bump it once from an adjacent square. The
  refusal text is its hint. If she holds a candidate key she has not tried,
  the bump itself tests it: the server searches her whole inventory,
  containers included (`key.c:48-66`).
- *Plain sign* (no event script, no read limit): apply it.
- *Magic mouth*: nothing to try; its text is recorded when she walks over
  it. A mouth with a read limit (`stats.food`, shared by all players,
  `sign.c:59-68`; one in the content) is never walked over twice on
  purpose.
- *Allowed lever or switch*: apply once, watch, then restore (4.4.4).
- *Inventory checker*: nothing to try; ROUTING owns checkers.
- *Any other appliable object* (owner decision, 2026-11-18): every 4.4.1
  resolution and refusal applies to it as well, so an object with a
  connection to a refused target, an event script or a one-shot effect is
  never tried. In addition to that offline resolution, she examines it in
  game first, and reads plain signs nearby and only those books that
  4.6 allows (never an unread world book). She records what they say. She
  applies it only when the examine text or a hint positively states that
  its only effect is opening or closing a gate or door, or showing text;
  one that toggles a gate or door counts as a lever for 4.4.3 and 4.4.4.
  She records what happened.
  Everything else is shadow-only: a `lock-trial-shadow` decision with what
  she read, until a supervised session.

#### 4.4.3 Preconditions, all required

- **ROUTING's probe limits** (ROUTING 4.6): full HP and SP; every map and
  cell on the approach below `PROBE_MAX_DANGER_SECONDS` (300 s); at most
  `PROBES_PER_DAY = 3` trials a day and once per subject a day; and the
  day's trials stop at the first failure (damage, a spawn, a trap or a
  recall). ROUTING ships that budget with its probes in P10, so F7 defines
  the shared daily counter itself and P10 reuses it, whichever lands first.
  A door's refusal is not a failure here: it does not stop the day's
  trials. Before F8 it is recorded only as this lock's hint and refusal
  count; from F8 on it also writes ROUTING's `rejected` row (4.5).
- No hostile within its detection radius of her or of the object (the
  `_lone_target` radii, COMBAT.md), and no unseen-contact hit in the last
  10 s.
- Food above the hunger warning (PR #97's threshold) and a ready recall
  source with known charges (`known_charge`, COMBAT.md "Safety").
- Not inside an owner block or a hazard cell (ROUTING 4.7, 4.4).
- **For anything that changes the world** (a lever, switch or button, or
  any other object that toggles a gate or door, 4.4.2): no
  other player on the map. She cannot see a whole map, and `/who` does not
  say where players are (`commands/player/who.c:40-81`), so this means
  `/who` reports her as the only player online ("There is 1 player
  online."). Only that count line is parsed; the ingestion filter (4.3.1)
  drops `/who`'s per-player lines unread, because each names a player. A
  door bump changes nothing for others and needs only "no other player in
  view".
- Not on a map whose region is an apartment or house other than her own
  (player housing), and not a shop's staff door.
- She has spent no consumable to get here and will spend none: she never
  drops, gives, burns or uses an item to test a hypothesis. Keys are tested
  only by carrying them. A checker that consumes its key
  (ROUTING's `Consumes`) is never "tried".
- Nothing is retried within 7 days once `opened`, `toggled`,
  `suspected-unopenable` or `forbidden`.

#### 4.4.4 Traps, watching and restoring

- **Traps.** Opening a door springs any rune in its inventory
  (`door.c:119-124`). Before her first trial of a door, chest or lever she
  uses Find Traps if she has it (she does, level 5). Its roll is stable per
  trap (`rune.c:99-116`), so repeating it never helps, and "nothing found"
  does not mean "no trap". A found trap marks the object `trapped`. She uses
  Remove Traps only when the trap's level is at or below her Remove Traps
  level, because a failed attempt sets the trap off with a chance of at
  least 50% (55% plus 5% a level of difference, capped at 90%,
  `rune.c:195-199`). Otherwise she leaves the object alone.
- **Watching.** For 10 s after a lever, she records: monsters that appear
  within 8 tiles that were not in view before (`spawned`), any HP loss
  (`damaged`), a map change (`teleported`), tiles in view whose objects
  changed (a gate opening or closing, `toggled`), and the lever's own text.
  She does not walk into a newly opened passage as part of the trial;
  navigation sees the change on its next plan. On `spawned` or `damaged` she
  leaves at once with the existing retreat and recall, and the lever is
  marked `forbidden`.
- **Restoring.** A lever with `exp` > 0 resets itself after `exp` ticks
  (`handle.c:76-81`, `handle.c:104-120`). Any other allowed lever is pulled
  again after the watch, so the shared world is as she found it, and the
  second pull is recorded as `restore`. Any other object that toggled a gate
  or door (4.4.2) is restored the same way, by applying it again. A lever she
  cannot restore (refused, or a threat arrived) raises an alert.

**What a trial writes.** One `interactions` row with the resolved targets;
the `locks` row's state, attempts and hint; the hint observation; and, for a
lever, which tiles changed. A lever whose effect was seen becomes a
candidate `lever-opens:<tile>` fact, confirmed when a later trial repeats it.

### 4.5 Unlock knowledge into routing

This design produces knowledge; ROUTING consumes it. The plug points are
ROUTING's P2 (`UnlockState`, 4.3 there), P4 (dialogue and quest-gated
edges, 4.2 there) and P5 (`route_requirements`, 4.4 there):

- **Tile-level learned access.** Locked doors and checkers are tiles in
  `MapNode`, not edges, so ROUTING's edge-keyed `Learned` atom does not
  reach them. `UnlockState` gains `learned_tiles`, a map from
  `(path, x, y)` to `passed` or `rejected`, read by `lock_allowed` in
  addition to the item-name access set. Its source is `route_requirements`
  rows whose subject is `lock:{path}:{x},{y}` in the table's `edge` column.
  ROUTING's own edge keys are unchanged. This is a new contract: ROUTING
  4.4's loader ignores subjects that are not edges in the current content,
  and its 4.3 knows only edge-keyed `Learned` atoms. F8 therefore amends
  ROUTING.md (4.3 for `learned_tiles` and `lock_allowed`, 4.4 for `lock:`
  subjects, which the loader keeps while the tile is still a locked tile)
  in the same pull request.
- **Confirmed openings.** A trial that opens a door writes a `passed`
  `route_requirements` row for its lock subject, with the server's "You open
  the ... with the ..." line, which names the key, as `evidence`, and
  ROUTING's quest and item state fingerprint as `inputs`. An opening "for
  you" (a force, `door.c:259-260`) writes the same with "The ... is opened
  for you." as `evidence`. A `passed` row never satisfies a `Consumes`
  atom.
- **Refusals.** A refusal writes `rejected` with the door's own text as the
  message evidence ROUTING 4.3 requires, and lasts as ROUTING says: until a
  related input changes (a matching item arrives, a quest part completes),
  and at most 24 h.
- **Hypotheses.** Candidate `key-name`, `key-holder` and `npc-key` facts do
  not open anything. They give the unlock-acquisition planner (which ROUTING
  leaves out of scope) and the quest policy a ranked list: "the orange
  stone giant key is held by Xuhrexal on `old_outpost_a_0203`".
- **Quest gates.** A `quest-gate` fact (a door or portal whose text or
  outcome ties it to a quest part) is offered to P4 as a hint for which
  journal part the edge's `Quest` atom should read. P4's own parsing of
  interface XML stays authoritative.
- **Unopenable.** `suspected-unopenable` (refused 3 times on 3 days while
  she held every candidate she knew) changes nothing in routing by itself;
  it is a feedback input (4.9) and stops further trials.

None of this lets a learned fact override a fail-closed `Unknown` atom
without a confirmed outcome, per ROUTING 4.1.

### 4.6 Books and facts for planning and combat

**One rule for low Int.** Book experience is lost below Int 11 (4.11.2).
So while her Int is 10 or less she does not read unread books at all. She
keeps them unsold (stored in her apartment when weight requires) until her
Int is 11 or more, then reads them for experience and facts together. A
book is readable at all only when its level is at most Literacy + 12 +
`book_level_mod[Int]` (`BOOK_LEVEL_DIFF`, `book.c:50`, `book.c:63-65`,
`book.c:104-106`): with Literacy 3 that is level 15 at Int 11-15, but only
level 7 at her depleted Int 1.

**Where books come from.**

- *Inventory books*: `maintain_inventory` already applies them (subject to
  the Int rule). The book packet that follows passes the ingestion filter
  (4.3.1) and is stored as an observation with `source_kind = book`.
- *World books* (on shelves, desks and map-placed corpses) are shared:
  reading one sets `FLAG_NO_SKILL_IDENT` on the object itself
  (`book.c:175`), so the next player gets no experience from it until the
  map resets. She reads a world book only when its examine already says
  "(read)" (`item.c:1059`); an unread world book is recorded as seen and
  left alone (open question 11). She never takes an unpaid item.
- *Player corpses and player-dropped items*: never taken, opened or read. A
  corpse named for a character, or any container on a square where another
  player dropped things, is out of bounds.
- *Scrolls*: only text scrolls (no spell) are read; spell scrolls stay
  untouched, because reading one casts it.
- *Beyond comprehension*: "This book is ... beyond your comprehension"
  keeps the book unread; she stores the attempt and its level gap.

**Using the facts.**

- *Planner.* `trap-warning` facts mark a cell as hazardous in ROUTING's
  learned-cost terms, as a prior, not a wall ("STAY OFF THE BRIDGE" on
  `underground_city_2_3_-2`). `direction` and `key-holder` facts feed the
  quest policy's hints, never its required steps.
- *Combat.* `weakness` and `immunity` facts join the element choice for
  storm farming (COMBAT.md "Next phase"): a confirmed immunity (her hits
  show 0 damage of that type) outranks a read one, and both outrank the
  default. `tile-property` facts extend PR #97's one-hour
  `spell_blocked_here` mark into a persistent fact (4.11.1).
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

A monster attacks diagonally past a wall corner: the server's `can_hit`
checks only distance (F10 as built).

**Recording.** Each engagement writes one `fight_positions` row: the
feature at the moment the second attacker arrived (or at the start, for one),
the peak simultaneous attackers from the same 8 s window that
`review_failure` uses, ranged attackers, damage taken and outcome.
Aggregates per (feature, attackers in view, ranged present) give her a
learned damage rate and death rate, decayed with a 7-day half-life.

**Using it,** behind the flag `terrain_tactics`, after a shadow soak:

- *Packs of melee attackers.* When two or more are coming, `melee_kite`,
  `pack-split` retreat and `low_health_retreat` prefer a step toward a
  `corridor` or `doorway` square within 4 steps whose learned peak attackers
  is lower, as a tie-breaker after separation and before the pull anchor.
- *Ranged or caster monsters in view.* Prefer squares with `cover` from them
  and avoid `open` squares, again as a tie-breaker. This is the first
  path-level use of ranged positions, which COMBAT.md lists as a non-goal
  today; it stays a tie-breaker, never a reason to walk into a pack.
- *Pull squares.* `FarmCircuitTask._new_child` already chooses pull squares
  by clearance; among equally clear squares it prefers the one with the
  lowest learned peak attackers.
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
| Rings, amulets, trinkets | N is never shown in the name (the description is shown instead) | `item.c:210-228` |

So "+N" adds nothing to combat on an item whose archetype has no AC, WC,
damage, block or absorb. In the locked content those are: capes and robes,
plain girdles, sandals and low boots, feathered and blue hats and the
generic crown, cloth trousers, shorts and skirts (not chain pants, chaps or
chausses, which have AC), plain gloves, and every light source
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
  only after a shadow phase in which both scores are logged (F4).
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
  from what wearing it changed on her player doll. The measurement reads
  the stats packet's AC, WC and damage before and after she applies it. It
  runs behind the flag `measure_gear`, default off, only for identified,
  uncursed items she would wear anyway (the upgrade the gear policy already
  chose, or the item she already wears, re-applied), and only at a safe spot
  with no threat in view. She never puts on an unidentified or cursed item
  to measure it.

Whether these are bugs or intended presentation is the owner's call; the
draft says "confusing", not "wrong".

### 4.9 Hint clarity, and when it becomes feedback

A lock or quest step gets a clarity verdict once she has had a fair chance:
she has met it at least twice on different days, read every plain sign and
magic mouth within 8 tiles of it, and talked to every NPC with dialogue on
its map and the maps next to it. The NPC part needs the dialogue sweep in
F13.

Verdicts, from player-visible knowledge only (4.1 principle 2):

- `clear`: a deterministic fact named the requirement (key name, holder or
  quest part) and the outcome confirmed it.
- `generic`: the only text was the archetype's default ("You need a special
  key to open this door/gate."), no other source named a key, and a key
  exists.
- `false-promise`: the text tells her to find a key, but the lock is one no
  key opens (`slaying no_entry`). This uses content truth and is marked so
  in the draft.
- `misleading`: a fact named something the content truth contradicts (the
  yellow force field that needs an orange key, section 5.3).
- `no-source`: the text implies it opens ("You need a ... key") but the
  content has no key, archetype, artifact, quest reward or script that
  provides the lock id (section 5.2). Uses content truth, marked so.
- `unexplained`: she found no fact at all and is not `generic`.

`misleading`, `no-source` and `unexplained` become drafts on their own, one
per lock. `generic` and `false-promise` locks are aggregated: one draft per
region listing its locks with coordinates, because 86 openable locks and 21
`no_entry` locks use the default text (section 5.1), and one issue per door
would swamp the queue. Many are staff doors and vaults that are meant to
stay shut; the owner can reject an aggregate, and a rejected aggregate is
not redrafted for that region.

Quests get the same treatment at the part level: a part whose journal text
and dialogue gave her no fact that led to the place, item or NPC that
completed it (as recorded by the quest executor) is `unexplained`.

### 4.10 The feedback queue

#### 4.10.1 Kinds

| Kind | Meaning | Usual repo | Examples |
| --- | --- | --- | --- |
| `bug` | something that cannot work as written | content or classic | a door whose key exists nowhere; book experience lost at low Int |
| `hint` | an unclear or misleading hint or quest text | content | yellow field, orange key; aggregated default lock texts |
| `balance` | evidence on a balance question | classic or content | melee vs. spells (as evidence on the existing issue) |
| `qol` | a quality-of-life improvement | classic or content | an NPC parked on a one-tile path; no message for a `no_magic` savebed |
| `clarity` | text or presentation that misleads | classic or content | "+N" with no effect; examine without magic; typos |

Routing to a repo is by producer: lock, sign, book, dialogue and quest
findings go to `atrinik/content`; item presentation, client display and
server formulas go to `atrinik/classic`; findings about the playtester
itself (section 2's starvation loop, futile casts) go to
`atrinik/playtester` through the same approval (owner decision,
2026-11-18). The owner can change the repo before approving.

#### 4.10.2 Deduplication

- **Local.** `fingerprint` = sha256 of (kind, repo, subject, normalized
  claim), shown as `pt-` plus its first 12 hex digits. Subjects are stable:
  `lock:/map:x,y`, `region-generic:<region>`, `item-family:<archetype>`,
  `formula:book_exp_mod`. A new occurrence of an existing fingerprint
  increments `occurrences` and `last_seen`; it never creates a second
  draft. A rejected fingerprint stays rejected; a filed one only gains
  occurrences. The local record of filed items (`issue_url`) is the
  authoritative dedup source, so remote search is a second check only.
- **Remote, after approval only.** Nothing is searched before the owner
  approves (principle 6). After approval the filer searches the target repo,
  with its installation token, for the fingerprint and for the subject's map
  path, object or item name in titles. The fingerprint is a visible line in
  the issue footer (`Playtester fingerprint: pt-4f1c2a9b0d3e`), because
  whether GitHub search indexes HTML comments was not verified. A match
  outside a code block sets `needs-review` with the candidates; the owner then
  marks it a duplicate or files anyway. No match proceeds to filing.
- **Existing balance issues.** A `balance` item whose subject matches an
  existing issue (for example
  [atrinik/classic#628](https://github.com/atrinik/classic/issues/628),
  "magic cannot miss") is drafted as an evidence *comment* on that issue
  (`target = comment:<url>`), not a new issue (4.11.3).

#### 4.10.3 Evidence format

Every draft body is generated from `evidence_json` with one template. Text
from the game is never pasted as Markdown: excerpts are fenced code blocks
whose fence is longer than any backtick run in them, with `<!--` and `-->`
removed. Inside a code block, `@name` and `#123` neither notify nor link,
and a forged fingerprint line is ignored, because the filer reads
fingerprints only outside code blocks. Item, NPC and map names outside
excerpts are wrapped in inline code for the same reason.

````markdown
### What happened
The force field on the well says it is yellow, and the key that opens it
is the orange stone giant key.

### Where
Map `/shattered_islands/world_5_57_-2` (13,14) and (14,14), content v1.14.0,
server build v5.99.0, playtester v1.64.0 (479efb5).

### What she tried
- 2026-11-18 14:02 UTC: walked onto (13,14) without a stone giant key.
- 2026-11-20 09:40 UTC: walked onto (13,14) carrying `orange stone giant key`;
  no message, and she passed (14,14).

### What she saw
```
The yellow force field is preventing you from reaching the well bucket...
```
The square (14,14) shows an object named `yellow force field`.

### What she expected
A field and hint that name the key colour that opens it.

### Her state
Level 36; Slash 34, Wizardry 15, Literacy 3; HP 275/275.

### Occurrences
2 times on 2 days; first 2026-11-18, last 2026-11-20.

---
Playtester fingerprint: pt-4f1c2a9b0d3e
Filed by the Atrinik playtester after owner review.
````

(The magic mouth has `last_sp 1`, so it speaks only to a player without the
key, `sign.c:79-85`: she sees the yellow text while she lacks the key and
silence once she has it. The arch at (14,14) is `forcefield_yellow_0`,
named "yellow force field", so the fix may be the field's arch rather than
the text; the draft says what she saw and leaves the fix open.)

Message excerpts are server and NPC lines only, at most 10 lines and 1,500
characters, and only from `authored = 1` observations. Content truth, when
used (`false-promise`, `no-source`, `misleading`), is in its own "What the
content says" section with file paths, so a reader can tell what she saw
from what the maps say.

#### 4.10.4 Privacy

- The ingestion filter (4.3.1) already excludes chat, player-name links,
  signed books and renamed items, and replaces her own name.
- Every excerpt line is checked again against the names of other characters
  she has seen (chat senders, party members, player objects in view, PvP
  kill lines), case-folded; a line containing one is dropped, not masked.
- No account name, server address, invite code, certificate or token ever
  appears in a draft. Her character name appears only as "she" or
  "the playtester".
- A draft from a PvP or shared area records only that other players were
  present, as a count.
- The owner sees the exact body that will be filed and can edit it.

#### 4.10.5 Approval and dashboard security

- Drafts appear on a dashboard **Feedback** tab: list, filters by kind and
  repo, the rendered body, and buttons to approve, reject, snooze (7 days)
  and edit title, body, labels and repo. After approval it shows the
  filer's duplicate candidates for items in `needs-review`.
- **Rendering.** All game-derived text is escaped with `esc`
  (`web_static/app.js:8`). The body preview uses a Markdown renderer that
  emits no raw HTML, links or images. Every response gains a
  Content-Security-Policy (`default-src 'self'; script-src 'self';
  object-src 'none'; frame-ancestors 'none'`). Its default `style-src`
  blocks inline `style=` attributes, which existing panels use
  (`app.js:83`, `:280`, `:294`, `:488`), so F1 converts them to CSSOM
  assignments as `app.js:101` already does, rather than allowing inline
  styles.
- **Endpoints.** `GET /api/feedback`, and `POST /api/feedback/<id>` with an
  action, under the existing loopback and Origin rules
  (`web_server.py:800-822`).
- **Approval needs more than reaching the dashboard.** A POST without an
  `Origin` passes today, and the dashboard is reachable through the lab's
  reverse proxy, which may or may not forward `Origin` (1.9). So approval,
  edit and reject actions also require an approval key: a random secret in
  an owner-only file (mode 0600) outside the repository and state
  directory, sent in an `X-Playtester-Approval` header that the dashboard
  asks for once a browser session and keeps in `sessionStorage`. Without
  the file, the Feedback tab is read-only.
- **The CLI.** `atrinik-playtester feedback list|show|approve|reject|edit`
  runs on CT130 and writes the same SQLite rows directly through
  `BotMemory` (WAL, short lock timeout), with `decided_via = cli`. It works
  whether or not the bot is running; the bot reads the queue on its next
  poll.
- **Behaviour flags are not dashboard-controlled.** `try_locks`,
  `persist_tile_facts`, `terrain_tactics`, `effective_gear_scoring` and
  `measure_gear` are read from the bot's configuration on CT130 at start.
  The dashboard shows them read-only; no POST, with or without an Origin,
  changes them.

#### 4.10.6 Filing and the credential

- **A separate process.** The filer is `atrinik-playtester feedback file`,
  run by its own systemd unit (a oneshot on a timer, or by hand). It is the
  only process that can read the credential: the game process never has it
  in its environment, files or memory.
- **Credential.** A small GitHub App (owner decision, 2026-11-18) installed
  on `atrinik/content`, `atrinik/classic` and `atrinik/playtester` with
  Issues read and write and Metadata read, nothing else. Its private key is
  a systemd credential of the filer unit only (`LoadCredential=`), or a file
  such as `/etc/atrinik-playtester/feedback-app.pem` (mode 0400, owned by
  the filer's user). At start the filer signs a short-lived JWT with it,
  mints an installation token for the one repository it is filing to,
  limited to `issues: write`, uses that token, and exits. Neither the key,
  the JWT nor a token is ever in dashboard state, logs, checkpoints,
  metrics, decisions or `doctor` output. The existing `GITHUB_TOKEN` for
  dependency downloads is a different credential and is not used.
- **Steps.** For each `approved` item, one at a time: set `checking`; run
  the remote dedup (4.10.2); on candidates, set `needs-review` and stop;
  otherwise set `filing`, then `POST /repos/{repo}/issues` (or
  `/issues/{n}/comments` for a comment target) with title, body, labels and
  type. Record the URL and a `feedback_filings` row. On failure, `failed`
  with the error; the owner can retry.
- **Labels and types.** The filer never creates labels; it uses only labels
  that already exist. `atrinik/content` has `bug`, `enhancement`, `maps`,
  `question`; `atrinik/classic` has `bug`, `enhancement`, `component: server`,
  `component: client`; the labels of `atrinik/playtester` are read when the
  filer starts. The organization has issue types `Bug`, `Feature` and `Task`.
  GitHub's REST API documents that labels set by a caller without push access
  are dropped silently rather than refused, and other triage fields such as
  the type may behave the same way, so the filer reads the issue back after
  creating it. If labels or type are missing, it marks the item
  `filed-unlabelled` and the dashboard shows an alert; it does not retry. A
  shared `playtester` label is open question 3.
- **Rate limits.** At most 5 filings an hour and 20 a day, at least 60 s
  apart, and no request while GitHub's `x-ratelimit-remaining` is under 50.
  A 403 or 429 honours `Retry-After` and backs off exponentially from 10
  minutes. These limits are well inside GitHub's secondary limits for
  content creation.
- **Dry run.** Without the App key, an approved item stays `approved` and the
  dashboard and CLI offer "copy as Markdown" so the owner can file it
  herself.

### 4.11 Other pain points from her history

#### 4.11.1 Tile facts

PR #97's `spell_blocked_here` already marks a square for an hour after
"Something blocks your spellcasting." This design keeps the same signal as
a persistent `tile-property: no_magic` fact, confirmed on first sight, so a
savebed or quest square she returns to days later is known at once. The
same treatment covers the server's no-prayer and no-harm refusals. Behind
the flag `persist_tile_facts`: with it off, the persistent fact is written
and shown, and PR #97's one-hour mark alone decides; with it on, casting
code also reads the fact. A `qol` draft is offered for savebeds that block
magic with no message explaining why, as a low-priority item the owner may
reject.

#### 4.11.2 Book experience at low Int

`book_exp_mod` (`types/book.c:73-76`) is -3.00 at Int 0 and -2.00 at Int 1,
rising to -1.10 at Int 10, then 1.00 from Int 11. The book's experience is
multiplied by it (`book.c:163`), so it becomes negative; `calc_skill_exp`
returns 0 for negative experience (`server/skill_util.c:192-194`); and the
book is still marked read, so its one-time experience is gone
(`book.c:175`). At Int 10, a "below average" character gets nothing from any
book, ever. The values read as if the sign is wrong (0.9 down to 0.0 would
match the comment "less experience"). This is drafted as a `bug` for
`atrinik/classic`, and the playtester holds unread books until Int 11 (4.6).

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
("an NPC stands on a one-tile path"). Monsters and players are not
reported.

#### 4.11.5 Feedback producers

From the above, the producers and the phase that ships each: "+N with no
effect", the book experience formula and typos in lock and sign text,
using a small dictionary of common slips such as "a the" (F5); "examine
omits magic" (F11b); lock and quest-part clarity (F9); magic-blocking
savebeds (F14); path-blocking NPCs (F15); balance evidence (F16).

### 4.12 Metrics and dashboard

New Prometheus series (same labels and caps as today):

| Metric | Labels | Counts |
| --- | --- | --- |
| `observations_total` | `source` | observations stored, by source kind |
| `observations_dropped_total` | `reason` | chat, player link, signed book, renamed item, player name |
| `facts_total` | `kind`, `status` | facts created and their status changes |
| `lock_trials_total` | `kind`, `outcome` | doors, levers and signs tried |
| `lock_trials_refused_total` | `reason` | trials refused by 4.4.1 or 4.4.3 |
| `locks_known` (gauge) | `state` | locks by state |
| `fight_positions_total` | `feature`, `outcome` | engagements by terrain |
| `feedback_items` (gauge) | `status`, `kind` | queue size |
| `futile_actions_total` | `reason` | casts refused by a tile property, reads beyond comprehension |

The filer reports its own results in `feedback_filings`, not in the game
process's metrics. An alert fires when `futile_actions_total` passes 20 in
10 minutes.

Dashboard tabs: **Knowledge** (recent readings, facts by kind, locks with
state and hints, lever outcomes and refusals), **Equipment** (4.8.2),
**Feedback** (4.10.5) and a **Terrain** panel on the Farming tab (learned
rates by feature).

### 4.13 Tests

- Parser fixtures from every real text quoted in section 5, with expected
  facts, and negative fixtures (player chat that quotes a door message, a
  line with `[a=#charname]`, a signed book and a renamed item never become
  observations).
- Attribution: a recorded message stream after a bump attributes the navy
  line to the door; a magic mouth line on the square is attributed by
  content match; a broadcast she did not cause is not attributed.
- Trial safety: the Altar of Loki fixture is refused for its spawn points
  before any apply; a one-shot lever is refused; a scripted sign is
  refused; each precondition alone prevents a trial; `/who` with two
  players blocks a lever but not a door; a spawned monster ends the trial
  with a retreat and marks the lever forbidden; a non-resetting lever is
  restored.
- "+N" model: table-driven against archetype fixtures (cape, cloak, chain
  pants, ball and chain, bow, arrow, torch); a stats-packet before/after
  fixture for the measured mismatch.
- Feedback: fingerprints are stable across restarts; redaction drops a line
  naming a seen player; an excerpt containing a backtick fence, `<!--`,
  `@someone` and a fingerprint line renders inert; no network call happens
  before approval; mocked GitHub for duplicate, success, labels dropped, 403
  with `Retry-After` and the hourly cap; neither the App key, its file path, a
  JWT nor a token appears in any state the game process serializes (a test
  serializes all of it and searches); a POST without the approval key is
  refused; no POST changes a behaviour flag.

## 5. Concrete examples from the content

All from the content maps (see "How the evidence was gathered").

### 5.1 Default lock texts and doors no key opens

152 authored locked doors and gates carry a `slaying`. 107 use the
archetype's default text: 72 "You need a special key to open this door."
and 35 "You need a special key to open this gate." 30 have the `no_entry`
lock id, which no key opens. 21 of those 30 also use the default "find a
key" text, so they are inside the 107: 86 default-text locks have a key
somewhere, and 21 promise a key that does not exist (`false-promise`,
4.9). The other 9 `no_entry` locks say what they are: "The prison cell is
locked." (`world_3_59`, three), "The Casino is currently closed."
(`world_10_45` (4,18)), "LandShare Industries is currently not open."
(`world_10_44` (2,23)), "The door appears to be blocked by something..."
(two, Underground City), "The entry way is locked." and "The door is
locked." Those are probably decoration, and her verdict would be
`suspected-unopenable`, not feedback, unless the owner wants "currently
closed" doors flagged (open question 6).

### 5.2 A door whose key exists nowhere, with a typo

`/shattered_islands/world_4_58` (Aris) (19,12), `celestial_door_bar1`,
`slaying bar_key_aris`, text "You need a the key to open this door." No map,
archetype, artifact, interface or script mentions `bar_key_aris`. Verdict
`no-source`, plus a typo. One `bug` draft for `atrinik/content`. (34 other
locks have no key source either, but most are bank, vault and staff doors,
or force-based doors that markers open, so "no source" alone is not a bug;
only the text decides.)

### 5.3 A misleading colour

`/shattered_islands/world_5_57_-2` (13,14) is a magic mouth: "The yellow
force field is preventing you from reaching the well bucket..." It has
`last_sp 1`, so only a player without the key sees it. The square (14,14)
holds `forcefield_yellow_0`, named "yellow force field", and a checker for
the **orange** stone giant key, which is what opens it. In the Old Outpost
the colours agree: `old_outpost_a_0203` (22,10) says "The orange force
field prevents you from reaching the door. [i]Xuhrexal, the stone
giant[/i] is engraved on the door.", and Xuhrexal (level 26) carries the
orange stone giant key on the same map. A player told "yellow", and shown a
yellow field, looks for a yellow key that does not exist. Verdict
`misleading`, a `hint` draft for content. The fix could be the field's arch
(an orange one) rather than the text. COMBAT.md already names the well as
needing the orange key, so the playtester knows; a human player would not.

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
  `password` fact with no value, which is itself a clarity signal. It is a
  world book: she reads it only if it already shows "(read)" (4.6).

### 5.5 A lever that must never be pulled

`/shattered_islands/berri_lur/temple_loki/tol_0101` (11,8): a
`handle_lever` named "Altar of Loki", text "You appear to have company...".
Its connection 23 drives 8 spawn points at (17,4), (17,6), (17,13),
(17,15), (21,4), (21,6), (21,13) and (21,15), each holding a "living dead
girl" of level 93 to 95. The twin lever at (11,10) drives two firewalls.
Both are refused by 4.4.1 before she touches them; watching after a pull
would come too late.

Other levers carry a lock: 28 levers and switches have a `slaying`, 19 of
them Asteria's "big lever"s (`slaying asteria_walls_gate`). Without the
key they answer "The big lever is locked." (`handle.c:45-48`), with no hint
of what unlocks them. Their connections drive gates, so they pass 4.4.1.

### 5.6 A book with a useful fact

`underground_city_2_3_-2`, "note: Day 6 - 7" on a corpse (level 10, so
Literacy 3 can read it from Int 4, where 3 + 12 − 5 = 10; Int 11 matters
only for its experience): "a ---ic bul--- was needed. It must
be cast northwest from a special location. We have marked the spot with a
fake torch" and "STAY OFF THE BRIDGE, IT IS A TRAP". The map has pedestals
that react to `race bullet` at (7,2) and (7,4), and one green wall torch
among ordinary ones at (23,2). Facts: `direction` (magic bullet,
northwest, from a marked torch), `trap-warning` (the bridge). She knows
magic bullet. Whether the green torch is the marked spot was not verified.
The note is a world book on a map-placed corpse, so the shared-book rule in
4.6 applies.

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
stats as evidence once `measure_gear` has run.

## 6. Phased plan

Each phase is one small pull request that can ship alone, with tests, and
changes no behaviour unless it says so. Idle survival is
[PR #97](https://github.com/atrinik/playtester/pull/97) and is not
redesigned here; it should merge before any feature below is judged on her
live behaviour.

**F1. `feat(memory)`: an observation ledger.** The `observations` table,
the ingestion filter and attribution (4.3.1) for bumps, applies, walk-ons,
interfaces, inventory book packets and examines, caps, and a read-only
Knowledge tab with a CSP, with the existing inline styles converted to
CSSOM (4.10.5).
*Accept:* a recorded stream yields the expected attributed rows; chat,
player-link lines, signed books, unmatched unsigned books, renamed items and
`/who` player lines are never stored; every existing panel (vitals, skills,
task trace, messages) renders unchanged under the CSP; her
name is replaced in dialogue; book packets parse into title and body; an
unattributed fact line deduplicates; a full table evicts the oldest
unreferenced rows; no behaviour change.

*F1 as built* (`observations.py`, `memory.py`, the Knowledge tab). Where
the code and the server differ from the text above, the code follows the
server:

- Magic mouths use the key `mouth:/map:x,y` (the prefix the `locks` table
  in 4.2 already uses), and the row's `x`, `y` are where she stood; the key
  carries the source's square.
- Players in view are told from creatures by the MAP2 name colour: the
  server colours players white, green (her party) or red (PvP), and
  creatures by level (`request.c`), so a white name, or a green or red name
  that no authored object carries, is a player. MAP2 has no player flag.
- **Only what matches authored content exactly is kept.** Review showed
  that each looser rule let other players' text through, so F1 is
  narrower than 4.3.1:
  - Sign, door, magic-mouth and custom lever text must equal the authored
    message of an object on or next to her square; timing only labels
    such a match `timing`. The timing window alone never attributes a
    line: the server sends unrelated lines in it that name players
    without a link ("%s starved to death.", "%s says: ..." from NPC talk
    and `Object.Say`, "%s casts ... on you", guard shouts).
  - Fixed phrasings ("You open the %s with the %s.", "The %s is opened
    for you.", "The %s is locked.", "The %s won't budge.", "You turn the
    %s.", "Nothing is written on it.", "You are unable to decipher the
    strange symbols.") are kept only when an authored door or gate of that
    name stands at or next to her bump's target, or an authored lever of
    that name (or a sign) under her apply. Each consumes the one bump or
    ground apply of its kind in the window; with two in the window
    nothing is kept. Lines ending "and start reading." or "and find it
    empty." (`book.c`, whose book name a player sets) are never a door's,
    and the key named in "You open the %s with the %s." must be an
    authored name too.
  - Books are kept only when the body's hash is authored text, under the
    authored title from the index (the name of the map or archetype book
    carrying that message; empty for the book message file's texts),
    never the received title, which a player can set on an unsigned copy.
    The stored text is the normalized form that matched, so the received
    spacing (a possible side channel) is not kept.
  - An examine is kept only when the shown name, less count, item race,
    material, "+N" and parenthesised states, is an authored object or
    archetype name (or plural); the row and its first line use that
    authored name. A player-titled book or a guild-ranked container is
    dropped (`unauthored-item`). Examines of books are never kept
    (`book-examine`): a player can name a book anything, an authored name
    included (`book_edit.c`), and books are captured from their packets.
    Without the index, examines are dropped as `examine-unverified`.
  - **Dialogue pages are not kept in F1.** NPC scripts build pages from
    player data (post-office senders, auction sellers, guild ranks,
    jailed players), and no word-level check separates it from authored
    text. Dialogue capture moves to F13.
- The authored-text index holds the messages of maps, archetypes, artifacts
  and the book message file (`arch/messages`), authored book titles,
  object and archetype names and plurals, and material names
  (`arch/materials`). The server's other generated books (bestiary,
  artifact and spell path texts, `readable.c`) are not indexed, so they are
  dropped as unmatched, like player-edited books. The index is scanned once
  when `web` or `autoplay` starts (about 1.3 s); without it only
  unattributed fact lines are kept.
- "Characters she has seen" are chat senders, `/who` names and players in
  view since the process started; they are kept only in memory, so the set
  starts empty after a restart.
- Until F2's parsers exist, the only unattributed fact lines are the
  no-magic refusals "Something blocks your spellcasting." and "Something
  blocks your spell." The variant naming her item ("...the magic of your
  %s") is not kept, because a player-renamed item could carry its name.
- An examine is kept only when every line after "That is ..." is one of
  the forms `types/player.c` `examine` sends, an item's story is authored
  text, and its closing blank line arrives within 1.5 s. Any other line
  inside it drops the whole answer, as do "You name it ...", "... soulbound
  to NAME." and a signed book, because these name a player. Answers pair
  with her examines oldest first.
- Book packets answer only her own apply, so they need no timing window.
  The quest journal is not stored (`quests.parse_quest_book` reads it).
- "Oldest" for eviction means least recently seen (`last_at`); rows cited
  by `facts.observation_id` or `locks.hint_observation_id` are kept once
  those tables exist.
- The CSP adds `base-uri 'none'` to the policy in 4.10.5.

**F2. `feat(knowledge)`: deterministic fact parsers and persistent tile
facts.** The `facts` table and the parsers in 4.3.2. Tile facts persist
PR #97's `spell_blocked_here` signal behind `persist_tile_facts`, default
off (shadow: the fact is written and shown, #97 alone decides).
*Depends on* PR #97 (open at `a1f408a`).
*Accept:* every section 5 text gives its expected facts; the section 2
message stream produces one `no_magic` fact; with the flag on, a reconnect
a day later casts nothing on that square; no LLM dependency.

*F2 as built* (`facts.py`, `memory.py`, `tasks.spell_blocked_here`, the
Knowledge tab):

- `facts` has the columns of 4.2 plus `detail`, the sentence a fact came
  from (at most 300 characters of the stored observation's text, or the
  fixed refusal), so the dashboard can show why without the whole book. A
  unique index on (server, account, character, kind, subject, value) makes
  a repeat an upsert: the higher confidence wins, `candidate` can become
  `confirmed` but never the reverse, and the first observation stays the
  source. Only tile facts get an `expires_at` (below).
- Subjects are the observation's `source_key` for text facts, the folded
  creature name for `weakness` and `immunity`, and `tile:/map:x,y` or, for
  a whole map, `map:/map` for tile facts. `generic-lock` values are
  `special-key` (the archetype defaults), `unnamed-key` (5.2's "a the
  key") and `locked` ("The prison cell is locked.", "The big lever is
  locked."); `unopenable` values are `from-this-side` (0.6), `closed` and
  `blocked` (0.4, the 5.1 decoration doors). A `password` with no value is
  stored with an empty value.
- Parsers run on each observation as it is stored; a repeat stores facts
  again only when an older parser set (or none) read it, so rows F1 kept before
  this phase gain facts the next time she sees them;
  there is no bulk backfill. They read only kept observations, so dialogue
  (`npc-key`, F13) and the journal (`quest-part`, not stored) have no input
  yet; both are tested on fixtures. Lock parsers read door, gate, lever,
  sign and magic-mouth text; `quest-gate` reads books, scrolls, dialogue
  and the journal; system lines and examines give no text facts.
- `key-holder` reads only the engraved form (`[i]Name, the creature[/i] is
  engraved ...`): free prose such as "each guard in Asteria possesses the
  first of these keys" named places as holders. `weakness` and `immunity`
  need a creature name the authored index knows (so "its wearer" is no
  subject) and give nothing without the index; no authored text states one
  today, since the bestiary books are generated and not indexed.
- Tile facts come from `spell_blocked_here`'s attribution, not from the
  observation row: system lines are deduplicated per map, so a second
  no-magic square on the same map adds no observation. A refusal is kept
  only when her recent cast explains it and it certainly belongs to that
  cast: no step was sent after a map update last moved her before the
  cast (a step and a cast in one server tick move her first; a map update
  that does not change her square, sent when monsters move, does not
  count), and for a square
  she still stood on the square she cast from as the line arrived, for a
  map she was still on the cast's map. Otherwise nothing is kept. (#97's
  one-hour mark keeps its own attribution, unchanged: a heal cast just
  before an exit, then a potion on a no-magic map, still marks the first
  map for that hour, but never makes a fact.) A path walk (`S_MOVE_PATH`)
  counts as one step: a cast later in the same walk, after a map update,
  is taken as certain. Each fact cites the stored observation of the same
  line when there is one; the variant naming her item ("...the magic of
  your %s") is not stored (F1), so its fact cites none and keeps no
  detail. The countermagic lines join F1's fact lines. Classic has no
  no-prayer refusal; "Powerful countermagic cancels all harmful magic
  here!" becomes `no_harm` at the cast square (its message does not tell
  a no-harm map from a no-harm square) and changes nothing.
- Tile facts expire 7 days after the last refusal confirmed them
  (`expires_at`), and carry the locked content revision in a new `inputs`
  column (`content:<revision>`); a fact from another revision is ignored
  until a refusal confirms it again under the new one. A repeat refusal is
  written at most once an hour; a failed write waits a minute.
- **The flag only reorders healing.** This narrows the acceptance above:
  `spell_blocked_here` answers exactly as before this phase, flag or not,
  so a remembered fact never stops a cast by itself, and never the
  emergency recall. With `--persist-tile-facts` (or
  `ATRINIK_BOT_PERSIST_TILE_FACTS=1`), `SafetyPolicy._known_heal` puts a
  non-magical heal (a potion) before a spell, scroll, rod or wand on a
  remembered square or map. A reconnect a day later with a potion casts
  nothing there; with no other heal she casts, at any health, because a
  refused cast costs nothing (`cast_spell` refuses before spending mana)
  and a stale fact must not leave her without healing. Off, healing is
  unchanged and a `tile-fact-no-magic-shadow` decision records once a
  square a process where she would have put another heal first.
- **Nothing refutes a tile fact.** A cast the server drops without a word
  (her action timer, `check_skill_to_fire`, an unready device) or refuses
  for another reason first (a broken wand, a scroll she cannot read) looks
  like one that worked, so silence proves nothing. A fact is refreshed
  only by another no-magic refusal attributed to its square or map, and
  otherwise lapses 7 days after the last one, or at once when the content
  revision changes. With the flag on, a stale fact can therefore make her
  drink potions instead of casting on that square for up to 7 days; the
  flag is off by default.
- Tile-fact decisions save the decision ledger with a 0.25 s timeout and
  never raise.
- An observation records the parser set that last read it
  (`observations.facts_version`, added to F1's table where missing, a
  column another process added at the same moment being no error). Facts
  are parsed before the observation is written and stored with it in one
  transaction, inside a savepoint: a fact that fails to store loses that
  observation's facts (read again on its next sighting), never the
  observation. Eviction runs after the facts are stored, so a new row is
  cited before it could be evicted. A repeat the same parser set read
  stores no facts again.
- Facts are capped at 20,000 a character, candidates first, least recently
  updated first; `facts_total{kind,status}` counts new facts.

**F3. `feat(inventory)`: effective item stats and an Equipment panel.** The
"+N" model (4.8.1), examining carried gear once, `EffectiveStats`, the
panel, and shadow logging of old and new gear scores. No scoring change.
*Accept:* table tests for each archetype family in 4.8.1; the live
inventory in section 2 shows the cape's "+2" as no AC.

*F3 as built* (`equipment.py`, the Equipment tab). The "+N" rules were
checked again against the Classic server (`server/src` at `f2273c85f`) and
the locked content (v1.14.0). Where the server says more than 4.8.1, the
code follows the server:

- **A light adds nothing at all**, not only nothing from its "+N": the
  light slot is skipped when her stats are summed (`living.c:1007-1009`),
  so its archetype values (a torch's `maxhp`) count for nothing. So is the
  ammunition slot: an arrow's "+N" counts only when it is fired.
- **A launcher's protections, attacks and speed do not count**, and it
  adds no hit points, mana or regeneration: those are summed for
  non-ranged items only (`living.c:578-624`, `living.c:628-635`). Its base
  AC, block and absorb, and their "+N", do count; its stats (Str and so on)
  count too.
- **A weapon's base block and absorb gain its "+N" too**: the 4.8.1 rows
  for AC, block and absorb apply to every worn item, and many weapons have
  them (the falchion has block 5 and absorb 7).
- **The examine text's speed cap already includes the "+N"**: the treasure
  code stores the raised cap on the item (`treasure.c:846-847`), so only an
  unexamined armour's archetype cap would need the 4.8.1 factor; the panel
  shows the cap from examine and from the archetype as is.
- **Not modelled:** a shield gives nothing while a two-handed weapon or
  launcher is wielded (`living.c:997-1005`); the panel values the shield
  on its own. The protections of several items combine with diminishing
  returns (`living.c:596-606`); the panel shows each item's own value,
  scaled by its condition. A "(speed+N)" item's speed is not counted. An
  unexamined body armour's speed cap is its archetype's, without the
  "+N" factor (`treasure.c:846-847`). A ring's, amulet's or trinket's
  "+N" is never shown, so it is taken as 0, although the server adds it to
  any AC, WC, damage, block or absorb they have (`living.c:564-593`).

What was built:

- `EffectiveStats`, from the base stats of her examine (else the archetype
  her item's face and type name, when all archetypes sharing that face have
  the same base) and the "+N" in her item's name: AC, WC, damage, block and
  absorb, each arrow's WC and damage for launchers and arrows, protections
  and attacks scaled by condition (a wielded weapon's damage too), stats,
  hit points, mana and regeneration, delay, speed cap and special flags,
  with notes such as "+2 on a cape adds no AC: it has no base AC, WC,
  damage, block or absorb". The examine reader is a new, general parser
  (`parse_description`); the shop's `_apply_examine_metadata` is unchanged
  until F4.
- **Examining carried gear once** (`GearExaminer`): on a quiet farm tick
  (no threat in view or contact, the recall-wand rule), only after any
  recall-wand examine has its answer and while no mana-crystal examine
  waits, one identified, worn or carried gear item at a time, what she
  wears first; at least 2 seconds apart; it waits up to 5 seconds for the
  answer ("That is " and her item's shown name, followed only by its
  state flags and description) before the next. The recall wand and mana
  crystal examines in turn wait for a gear examine's answer, and the tick
  that settles it sends no new gear examine, so a waiting crystal or wand
  goes next. So normally only one examine is in flight. The one
  exception: the walk out of a farm, which holds until her wands are
  examined, abandons a waiting gear examine at once rather than wait for
  it. No attempt is counted, and the gear examiner then sends nothing for
  7 seconds (the 5-second answer wait and the 2-second spacing), so the
  abandoned answer, if it comes, arrives before her next gear examine is
  asked; nothing reads it, since each reader reads only lines after its own
  examine. F1's recorder may then pair that answer, if it is later than
  its 1.5-second window, with the wand's examine action instead. That is
  benign: the row's name comes from the answer's own line and only the
  item type is taken from the action, so the gear examine is kept under
  its authored name, or dropped, and the wand's own answer may go
  unrecorded. An item is examined again when its name changes (identification, a new "+N"), after a relog (new tags) or,
  with no answer, up to three times five minutes apart; a name that is not
  authored (most likely her own rename, which examine never shows) gets
  one try; the name is also tried without a title ("of Fire") or what
  follows its "+N", since the authored-name index holds no artifact titles.
  That leniency also lets a rename ending " of <anything>" after an
  authored name ("sword of Bob") have its three tries.
  An item name longer than the protocol's 127 bytes is truncated in her
  inventory (`socket/item.c:56-70`, `ITEM_NAME_SIZE`), so its examine
  never matches and times out; an untitled ring's or amulet's stats past
  the cut are lost. Lights are not examined. An untitled ring's, amulet's or
  trinket's description is in its name, which examine does not repeat, so
  it is read from the name.
- **Shadow logging.** Every 30 seconds at most, on the same quiet tick, she
  compares, per gear family, what the current score (`gear_score` plus the
  archetype's base score, as the equip loop ranks) and the effective-stats
  score would put on, with the equip loop's margin of 15. Each change in
  where they disagree is a `gear-shadow` decision (in the decision ledger
  and the Activity tab) naming both items and both scores; "no
  divergence" when they agree again. The effective score uses the shop
  examine score's weights on the effective values (protections at 8 a
  percent) and the caster weights when she scores as a caster; F4 settles
  the weights, including recent damage taken, after the owner reviews the
  log. The comparison does not apply the equip loop's per-task rules: it
  compares launchers even when the farm never readies one
  (`allow_launchers`), and it does not skip items the loop already tried
  once (`_upgrade_attempts`). Girdles and lights are shown but not
  compared, since the loop never chooses them by score.
- **The Equipment tab** lists each gear family she carries: what she wears
  and the best identified, uncursed candidate she could wear, each with
  both scores, its effective stats and notes, the per-stat deltas, and
  where the scores disagree, with the recent shadow log. It is built from
  DOM nodes and text only, under the dashboard's CSP.
- No choice changes: `gear_score`, the equip loop and the shop buyer are as
  before.

**F4. `feat(inventory)`: score gear by effective stats.** Switch
`gear_score` to the model behind `effective_gear_scoring`, after the owner
reviews a day of shadow logs.
*Accept:* the shadow divergence list is attached to the PR; with the flag
off, choices are unchanged.

**F5. `feat(feedback)`: the draft queue.** `feedback_items`, fingerprints,
the evidence builder and template, excerpt quoting, redaction, the
Feedback tab (read-only drafts) and the CLI's `list` and `show`, and the
first producers: "+N with no effect", the book experience formula and
typos. No network at all.
*Accept:* drafts are deduplicated across restarts; redaction and quoting
tests pass (4.13); a test proves no socket is opened; nothing is filed.

*F5 as built* (`feedback.py`, `feedback_producers.py`, `memory.py`, the
Feedback tab, `atrinik-playtester feedback list|show`). Where the code
differs from the text above:

- **Storage.** `feedback_items` has the columns of 4.2 plus `subject` (for
  filters and the CLI), `days` and `occurrence_keys_json`. `feedback_filings`
  is not created yet; F6 adds it with the filer. Only `draft` rows are made.
  The cap drops the drafts with the fewest occurrences, then the least
  recently seen; other statuses are never dropped.
- **Occurrences.** A finding carries an occurrence key; a key already
  counted (the newest 200 are kept) adds nothing, so a restart, a repeated
  sighting or the login sweep never inflates the count. Typos count once a
  day a source, a low-Int book once a book, "+N" once a day. `days` grows
  when an occurrence falls on a later UTC day than the last one. A draft's
  title and body are rendered again from its merged evidence on every
  occurrence (its `tried` lines, newest 10, and an aggregate's items, newest
  50, accumulate); a rejected or filed item only gains occurrences.
- **Evidence.** Prose in `evidence_json` is a list of parts: the
  playtester's own fixed wording, or `{"code": ...}` for a name or value
  from the game, always rendered as inline code; game text is only ever an
  excerpt. Server formulas go in their own "What the server source says"
  section, beside the "What the content says" of 4.10.3. "Where" names the
  locked content and Classic server releases and this playtester's build:
  her client cannot read the live server's build.
- **Quoting.** Besides `<!--` and `-->` (removed until none is left, so
  `<!<!---->--` cannot rebuild one), control characters and invisible
  formatting characters (zero-width and bidirectional controls) are removed
  from everything rendered. `fingerprints_outside_code` reads fingerprint
  lines only outside fenced blocks, for F6's filer.
- **Privacy.** Excerpt lines naming another character are dropped, against
  the names she has seen in this process and every chat sender her memory
  keeps for the account (so a restart does not forget them). Then the whole
  title and body are checked against every private term: her account,
  character name, the server's host, the password, the invite code, the
  certificate pin, the party name, the operator character and those names.
  A draft containing one is not stored, and only its fingerprint is logged.
  Other players present are recorded only as a count of those in view.
- **"+N with no effect"** reads her own carried items: an identified item
  whose name shows "+N" counts when its effective stats come from her
  examine (an artifact can give a cape the AC its archetype lacks) and
  F3's note says the "+N" adds nothing; a light counts without an examine,
  since it adds nothing at all. A shop's item counts only once it is in her
  inventory (unpaid, after she picks it up). The family is the authored archetype name (or the
  generic family), never the shown name, which a player could have set.
  One aggregate draft, subject `presentation:plus-n-no-effect`, rather than
  one per `item-family:`. It runs on the gear examiner's quiet tick, at
  most every 30 seconds, and writes only when the set of qualifying items
  changes in a process or a day passes.
- **Book experience** is drafted when an authored book observation is
  stored while her Int is 10 or lower. Whether the server still owed her
  the book's experience (an unread copy) is not visible to the client; the
  draft states the formula, her Int and Literacy, and the factor, and
  quotes no book text.
- **Typos** read only `authored` door, gate, lever, sign and magic-mouth
  observations, one draft a source (`subject` is its `source_key`, such as
  `door:/map:x,y`). The slips are a fixed list: a repeated small word ("the
  the"), two articles ("a the") and about thirty common misspellings
  ("recieve"). Slips are looked for only in the lines that may be quoted.
  Each new observation is read as it is stored, and the newest 1,000 of each
  kind once a login.
- **Labels and types** are proposals until open question 3 is answered:
  typos `bug`, `maps`; the book formula `bug`, `component: server`, type
  `Bug`; "+N" `enhancement`, `component: server`.
- **The Feedback tab** shows each draft's body as the plain Markdown text
  that would be filed, in a `pre` built with `textContent`, not rendered;
  there is no approval key and no POST route yet. The CLI reads the same
  rows and needs `--character`; it never creates a database.
- **Not built in F5:** the `feedback_items` gauge of 4.12, drafting from
  shop items she has not picked up, and the other producers of 4.11.5.

**F6. `feat(feedback)`: owner approval and GitHub filing.** The approval key,
CLI `approve|reject|edit`, the separate filer unit and command, App key and
installation-token handling, post-approval dedup and `needs-review`, labels
and types with read-back, rate limits, dry run, comments for balance targets
and audit rows.
*Accept:* with no App key, approval leaves the item approved with a Markdown
copy; mocked GitHub tests for success, duplicate, labels dropped, 403 with
`Retry-After` and the hourly cap; the game process never reads the App key
or a token;
a POST without the approval key is refused.

**F7. `feat(exploration)`: safe lock and lever trials.** `locks`,
`interactions`, `LockTrialTask` with connection resolution (4.4.1), the
preconditions including ROUTING's probe limits and the `/who` count,
Find Traps before first trials, the watch and restore. Flag `try_locks`,
default off; shadow decisions while off. It defines the shared
`PROBES_PER_DAY` counter if ROUTING P10 has not shipped it. *Depends on*
ROUTING P3 (blocks and care) and P5 and P6 (the route tables and the
danger costs that `PROBE_MAX_DANGER_SECONDS` is measured in).
*Accept:* the 4.13 trial-safety tests; an unknown target type is refused;
trials and ROUTING probes share one 3-a-day budget; a refusal does not
stop the day's trials and, before F8, writes no `rejected` row; another
appliable object is applied only when its examine text or a hint states
that its only effect is opening or closing a gate or door, or showing
text, and an unread world book is never read for a hint; the
owner turns the flag on after a supervised session.

**F8. `feat(navigation)`: learned unlocks into routing.** Trial outcomes
write `route_requirements` rows for `lock:` subjects, `UnlockState` gains
`learned_tiles`, and quest-gate facts are offered to P4. *Depends on*
ROUTING P2, P4 and P5, and on F7.
It also amends ROUTING.md 4.3 (`UnlockState.learned_tiles`, read by
`lock_allowed`) and 4.4 (`lock:` subjects in `route_requirements`, kept by
the loader while the tile is still locked in the current content), so the
two documents state one contract.
*Accept:* the ROUTING.md amendment is in the PR; a door opened with a key
not in `access_item_names` becomes routable for her; a rejected trial
outcome with message evidence blocks the tile until a related input
changes or 24 h pass; `inputs` holds ROUTING's state fingerprint and the
key name is only in `evidence`; a `lock:` row survives a reload; a
candidate fact alone opens nothing; no `Consumes` atom is ever satisfied by
a learned record.

**F9. `feat(feedback)`: hint and quest clarity.** The fair-chance rule and
verdicts (4.9), regional aggregation of default texts, and quest-part
clarity. *Depends on* F13 for the NPC part of the fair-chance rule.
*Accept:* fixtures for 5.1 (`false-promise`), 5.2 (`no-source`), 5.3
(`misleading`) and 5.4 (`clear`) produce those verdicts; default-text locks
aggregate to one draft per region; rejected aggregates are not redrafted.

**F10. `feat(combat)`: record fights by terrain.** Terrain features and
`fight_positions`, the Terrain panel. No behaviour change.
*Accept:* a synthetic corridor map yields `reach` 2 and `corridor`; peak
attackers match `review_failure`'s on recorded fights.

*F10 as built* (`terrain.py`, `memory.py`, `tasks.observe_fight`, the
Terrain panel on the Farming tab). Where the code and the server differ
from 4.2 and 4.7, the code follows the server:

- **Diagonal reach is settled.** The Classic server lets a monster hit
  whatever is within one square in both axes on its own level
  (`can_hit`, `server/src/types/monster.c`) with no wall check, so a
  diagonal neighbour reaches her past a wall corner. `reach` counts all
  8 neighbours; a diagonal pair of opposite squares is a `corridor`. A
  door counts as standable: monsters that open doors
  (`BEHAVIOR_OPEN_DOORS`, `types/door.c`) stand in the open doorway.
  Locked gates and static blockers do not. Walkability is the player's
  (`MapNode.walkable`), so a flying or swimming monster on water is not
  counted. Off the map counts as standable only in a direction another
  map is tiled to (`tile_path_1` to `_8` or a coordinate neighbour, by
  the side or corner the world graph puts its edge on).
- **Features** are taken in order: `corridor` (reach 2, opposite each
  other), `doorway` (a door on or next to her square, or walls on two
  opposite sides with at least 7 of the 10 squares in the two rows beyond
  one of the openings standable), `open`, `room`; `unknown` when her map
  is not in the world graph (the CLI passes it for `web` and `autoplay`).
  A dead end has reach 1 and is a `room`.
- **A fight** starts at her first combat line (a hit or miss on her, a
  block, dodge or parry, her own hit or kill; server lines only) and ends
  10 seconds after the last, when she leaves the map other than over a
  tile seam (a map tiled to the current one continues the fight), or 2
  seconds after her `death` decision: hits until then count, as in
  `review_failure`'s window, and the row ends at the death. The outcome is
  `died`, `recalled` after an `emergency-recall` decision in the fight,
  `won` when she killed something, else `retreated`; leaving the map
  follows the same rule. A fight is cut after 15 minutes.
- **Damage over time is no attacker.** A poisoning pulse ("poison hit you
  for N (M) poison damage.", `types/poisoning.c` through `attack.c`, under
  the poisoning archetype's name) and a disease symptom
  (`types/symptom.c`) name the force as the hitter, and the server names
  each symptom after its disease (`disease.c`): "flu hit you ...",
  "pneumonic plague hit you ...". Diseases and symptoms are system
  objects the client is never sent, so the names come from the locked
  content: every authored disease archetype's name and plural (type 158;
  no artifact is a disease), loaded once at start-up
  (`tasks.disease_names`), plus `poison` and the names of the poisoning
  and force objects she carries. Both sides are compared folded
  (`observations.plain`). Such a line neither starts a fight nor counts
  as an attacker or damage. A disease a map renames is not covered.
  `review_failure` is unchanged and still counts them, so a fatal fight
  with such a pulse can show more peak attackers there than in its row.
- **The feature is read** when the fight starts and again, with her
  square, when a second distinct attacker hits her within 8 seconds.
  `peak_attackers` uses `review_failure`'s own count
  (`self_improvement.peak_attackers`, now shared) over the fight's hits,
  so on a fatal fight within the review's 60-second window both agree
  (damage over time aside, above); a longer fight counts all its hits.
- **More columns than 4.2:** `x`, `y` (her square at the snapshot),
  `ranged_covered` (ranged or caster monsters in view at the snapshot
  with a wall or door on a cell an aimed bullet crosses before them:
  `tasks.cover_cells`, the same cells `bullet_ray_clear` tests, so a
  corner cell covers a diagonal neighbour), `hostiles_in_view` and `players_in_view` (the most at once),
  and `kills`. Ranged means a name marker (`RANGED_RESIDENT_MARKERS`) or
  an authored caster on the map. `ranged_attackers` is the most ranged
  monsters in view, not attackers proven by combat text, which names
  only species.
- **Privacy:** only counts are stored. Attacker names (combat text, which
  can name a player in a PvP area) stay in memory for the pack count and
  are dropped with the fight; players in view are only counted.
- **Bounds:** the engine feeds the recorder once a step, before the
  task's tick; it reads only new server lines, scans the creatures in
  view only during a fight, keeps at most 2,000 hits a fight and 16
  pending decisions, and never raises (an error forgets the fight, and
  is logged once, then counted every 10 minutes; the next step that
  works reports the count and re-arms the full log). A row waits at most 0.25 s for a locked database and is
  otherwise dropped. Rows are capped at 20,000 a character, oldest
  evicted first, checked every 100 rows; there is no time retention.
- **Rates** on the panel are summed in SQLite per feature, peak attackers
  (0, 1, 2, 3+), ranged in view and whole day of age over the last 60
  days, then weighted with the 7-day half-life at the middle of each day.
  `fight_positions_total{feature,outcome}` counts stored rows.

**F11. `feat(combat)`: terrain-aware tie-breakers.** The rules in 4.7 behind
`terrain_tactics`, after a shadow soak and the owner's approval.
*Accept:* in a synthetic map, a two-monster pack retreat picks the corridor
when separation ties; with ranged monsters in view, a covered square wins a
tie; separation still wins over terrain; with the flag off, steps are
unchanged.

**F11b. `feat(inventory)`: measure what gear really adds.** The
before/after doll measurement behind `measure_gear`, and the "examine omits
magic" producer.
*Accept:* only identified, uncursed items the gear policy chose are
measured, only with no threat in view; the ball and chain fixture yields
the measured AC 4, WC 3, block 4, absorb 7%.

**F12. `feat(knowledge)`: world books and text scrolls.** Reading map books
under the shared-book rule (only already-read ones), text scrolls, the
player-corpse ban, and holding unread inventory books until Int 11.
*Accept:* an unread world book is recorded and left; a read one is read and
put back; a spell scroll is never read; at Int 10 no inventory book is
applied or sold.

**F13. `feat(exploration)`: an NPC dialogue sweep.** Say `hello` to each
unfamiliar NPC with dialogue on maps she visits, and follow only
non-consequential links, in coordination with
[#17](https://github.com/atrinik/playtester/issues/17): never a link whose
interface action is `teleport`, `enemy`, a quest start or turn-in, a
purchase, or free text. Records pages as observations, but only pages of
an explicit allowlist of NPC scripts whose text is literal authored
strings: the index gains the hashes of those scripts' string literals
(with `{activator.name}` and other substitutions resolved to fixed forms),
and a page is kept only when it matches them exactly. F1 keeps no
dialogue, because scripts build pages from player data (4.10.4).
*Accept:* Warden Argoth's fixture records the "key" page and never follows
the insult chain (its last step sets `enemy="player"`); a teleporting link
is never followed.

**F14. `feat(feedback)`: magic-blocking savebeds.** The `qol` producer from
4.11.1, using F2's persistent facts. *Depends on* F2, F5 and PR #97.
*Accept:* the Clearhaven bed yields one draft; a non-savebed `no_magic`
square yields none.

**F15. `feat(feedback)`: path-blocking NPCs.** The producer from 4.11.4.
*Depends on* F5.
*Accept:* ten blocks by one named NPC on one square in a day yield one
draft; monsters and players yield none.

**F16. `feat(feedback)`: balance comparison reports.** The report in
4.11.3, drafted as comments on matching issues. *Depends on* F5, and on F6
for the comment target.
*Accept:* a recorded two-hour fixture per mode produces the table; fewer
than two hours of a mode says "not enough data"; at most one refresh a week.

**F17. `feat(knowledge)`: optional offline interpreter.** The
`Interpreter` interface, the no-op default, an out-of-process local-model
adapter, queueing and the dashboard indicator.
*Accept:* with no endpoint configured, nothing runs and no text leaves the
process; only authored observations are sent; LLM facts are capped at 0.5
confidence and never produce a clarity verdict, unlock or draft by
themselves.

## 7. Open questions for the owner

Settled on 2026-11-18. These answers override the questions below and are
folded into the sections cited:

- **Token form (question 2).** A small GitHub App with Issues write only,
  installed on `atrinik/content`, `atrinik/classic` and `atrinik/playtester`.
  Only the separate filer process reads its key; the game process never
  does (4.10.6). The filer still reads every issue back and keeps
  `filed-unlabelled`, since dropped labels and types are unverified (section
  8).
- **Findings about the playtester itself (question 4).** Drafted like any
  other feedback and, once the owner approves them, filed to
  `atrinik/playtester` through the same queue (4.10.1).
- **Trial scope (question 8).** All of these are allowed, under 4.4.1 and
  the preconditions in 4.4.3:
  - door trials in the wild and in dungeons;
  - door trials in towns and quest maps;
  - lever trials only while she is the only player online, restoring the
    lever afterwards (4.4.3, 4.4.4).

  Other appliable objects she meets are in scope too (4.4.2, F7). Every
  4.4.1 refusal still applies. She examines the object and reads nearby
  plain signs and books (only those that 4.6 allows) before applying it.
  She applies it only when they positively state that its only effect is
  opening or closing a gate or door, or showing text; anything else stays
  shadow-only.
- **LLM (question 9).** Allowed, local only: an offline model on a lab
  host, so no game text leaves the lab. Which host is decided when F17 is
  next. Every limit in 4.3.3 still applies.
- **Live character (question 10).** Overtaken by events: on 2026-11-18,
  after an owner-triggered operator assist gave her one gold coin, Sera paid
  the temple and was cured of depletion; she was not restarted. Her
  dashboard and metrics already mark her operator-assisted (the operator
  assist's own `operator-assist` alert and `operator_assisted` metric),
  which matters when these features are evaluated.

Questions 1, 3, 5, 6, 7 and 11 remain open. Each is asked before the phase
that needs it: question 1 before F6, question 3 before F6, questions 5 and
6 before F9, question 7 before F16, and question 11 before F12.

1. **Approval security.** Does the lab proxy in front of
   `playtester.atrinik.orion.milkyway` authenticate, and does it forward,
   strip or rewrite `Origin`? One request answers the second part without
   changing anything: a POST to a path that does not exist, with
   `Origin: http://example.invalid`, returns 403 if `Origin` reaches the bot
   and 404 if it is stripped. The approval key (4.10.5) is needed either
   way; is the owner-only file acceptable, or should approval be CLI-only?
2. **Token form.** A fine-grained personal token, or a small GitHub App
   installed on `atrinik/content` and `atrinik/classic` with Issues write?
   Whose account files the issues? (A token without push access may get its
   labels and type dropped, 4.10.6.)
3. **Labels and types.** Create a `playtester` label in all three repositories,
   and use the organization issue types (`Bug`, `Feature`, `Task`)?
4. **Findings about the playtester itself** (the starvation loop, futile
   casts): dashboard-only, or also filed to `atrinik/playtester` through the
   same approval?
5. **Default lock texts.** One aggregated draft per region, a single global
   one, or none (they are intended)? Should the 21 `false-promise` locks be
   reported separately from the 86 with a key?
6. **"Currently closed" doors** (`no_entry` locks with their own text):
   report, or treat as decoration?
7. **Balance evidence.** Comments on existing issues such as
   [atrinik/classic#628](https://github.com/atrinik/classic/issues/628),
   or a separate report artifact?
8. **Trial scope.** Lever trials need her to be the only player online. Is
   that acceptable, or should levers be left out entirely? Are door trials
   allowed in towns and quest maps?
9. **LLM.** Allowed at all? If so, local only, and on which host?
10. **Live character.** After PR #97, should Sera (66 depletion points) be
    restored or restarted before the new features are evaluated?
11. **Shared world books.** Reading an unread world book takes its one-time
    experience from the next player until the map resets. Leave unread world
    books alone (the default here), or read them when she is the only player
    online?

## 8. Known uncertainties

- Monster diagonal attacks past wall corners: checked for F10. The
  server's `can_hit` has no wall test, so they happen and `reach` counts
  all 8 neighbours. Whether a monster's *path* can get to a diagonal
  square past a corner was not checked; her fights confirm it.
- Whether the client sees the contents of her closed key ring was not
  verified (ROUTING section 7). Key trials rely on the server searching
  containers (`key.c:52-62`), which it does.
- Attribution by timing assumes the server answers a bump or apply in the
  same or next tick; heavy lag could misattribute a line. Content matching
  backs it up only where authored text exists.
- Whether the client can tell an unsigned, player-edited book from an
  authored one is unknown; such books are dropped because their text
  matches nothing authored, not detected as player-written.
- Whether GitHub search indexes HTML comments, and exactly which fields the
  issue API drops for a token without push access, were not verified; the
  design reads back every created issue instead of relying on either.
- The green torch in 5.6 being the "fake torch" is an inference.
- The stats the dashboard reported (Str 3, Int 1) were read from
  `/api/state` and not cross-checked against the server.
- The proxy's authentication and `Origin` handling (1.9) were not checked.
- 4.11.2 reads the book experience table as a sign error; the original
  intent was not checked in Classic history.
