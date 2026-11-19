"""Dialogue voyages and quest-gated exits (docs/ROUTING.md 4.2, phase P4).

Two kinds of edge that the static map graph does not see:

- **Dialogue voyages.** An NPC's interface XML can end a dialogue with
  ``<action teleport="map x y [savebed]"/>``. Each such dialogue, reached
  from ``hello`` through ``<response destination=...>`` links, compiles to a
  :class:`Voyage`: the NPC's square, the arrival square, the exact pages she
  will see and the link to choose on each, what must hold to use it
  (``Quest`` atoms from the interface's ``state``), and its side effects. A
  dialogue whose path does anything but talk and teleport (quest actions,
  objects, checks, code, inherited dialogue, a text box, another NPC) gets
  an ``Unknown`` atom and is never offered: no voyage starts or advances a
  quest, buys, sells, sets an enemy or says free text. No authored voyage
  charges a fare, and a fare can only be taken by those elements, so a paid
  voyage is never offered either. A ``savebed`` voyage moves her respawn
  point and is compiled for display but never offered (owner decision).
- **Quest-gated exits.** An exit whose event script matches the recognised
  bounce pattern (``lake_portal.py``: ``if not qm.completed("part"):`` ...
  ``SetReturnValue(1)``) gets a ``Quest`` atom for that part. An exit whose
  script is not recognised keeps its ordinary behaviour.

Both act only behind the ``dialogue_edges`` flag (``ClientConfig``), off by
default; the owner turns it on after one supervised voyage. With the flag
off, :func:`dialogue_offer` returns None and planning is exactly as before.

With the flag on, a :class:`DialogueOffer` holds, for one plan, the voyages
she may take and the gated exits that are closed to her. A voyage is
offered only when its requirement holds for her journal now, it moves no
savebed, its page was not refused recently, and its arrival has a known
route back to where she boarded or to her savebed, so a voyage cannot
strand her.

Edge identity (ROUTING 4.2, and owner blocks in route_blocks.py):
``talk:{source}:{npc}>{destination}``.
"""

from __future__ import annotations

import ast
import posixpath
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import requirements as req
from .requirements import Requirement

TALK = "talk"
DIALOGUE = "dialogue"
SAVEBED = "savebed"
# How long a refused voyage stays out of her plans, unless her unlock state
# (keys, journal) changes first (ROUTING 4.3's rejection rule; P5 persists
# such outcomes, P4 keeps them in memory).
REFUSAL_SECONDS = 24 * 3600.0

# What a dialogue on a voyage's path may contain: plain pages and links.
_PAGE_TAGS = frozenset({"message", "response"})
_TARGET_TAGS = frozenset({"message", "close", "action"})
_RESPONSE_ATTRS = frozenset({"destination", "action", "message"})
_TELEPORT = re.compile(r"([^ ]+)\s*(\d+)?\s*(\d+)?\s*(savebed)?")
_LINK = re.compile(r"\[a=([^:\]]*):([^\]]*)\](.*?)\[/a\]", re.S)
_FIELD = re.compile(r"\{[^{}]*\}")


@dataclass(frozen=True, slots=True)
class DialoguePage:
    """One page she must see, and the link she chooses on it.

    ``text`` is the authored page text, its ``{activator.name}`` style
    fields standing for any text; ``links`` are the authored links as
    (action, destination, label), in order.
    """
    text: str
    links: tuple[tuple[str, str, str], ...]
    choose: int

    def pattern(self) -> re.Pattern:
        parts = _FIELD.split(self.text)
        return re.compile(".+?".join(re.escape(part) for part in parts),
                          re.S)

    def match(self, interface: Any) -> int | None:
        """The index of the link to choose on ``interface``, or None when
        the page is not exactly the authored one (fail closed)."""
        if interface is None:
            return None
        if getattr(interface, "input_text", None) is not None:
            return None
        if getattr(interface, "objects", None):
            return None
        links = []
        for markup in getattr(interface, "links", ()) or ():
            found = _LINK.search(markup)
            if found is None:
                return None
            links.append(tuple(found.groups()))
        if tuple(links) != self.links:
            return None
        text = str(getattr(interface, "text", "") or "").strip()
        if not self.pattern().fullmatch(text):
            return None
        return self.choose


@dataclass(frozen=True, slots=True)
class Voyage:
    """One compiled dialogue teleport (ROUTING 4.2).

    ``edge`` is its planner edge (``navigation.MapEdge`` of kind
    ``"dialogue"``, departing from the NPC's square, ``label`` the NPC's
    name).
    """
    edge: Any
    npc: str
    interface: str
    pages: tuple[DialoguePage, ...]
    requires: Requirement = req.NONE
    side_effects: frozenset[str] = frozenset()

    @property
    def subject(self) -> str:
        return talk_subject(self.edge.source, self.npc, self.edge.destination)

    def summary(self) -> dict[str, Any]:
        edge = self.edge
        return {
            "subject": self.subject, "npc": self.npc,
            "source": edge.source, "at": [edge.x, edge.y],
            "destination": edge.destination,
            "arrival": [edge.destination_x, edge.destination_y],
            "interface": self.interface,
            "requires": describe(self.requires),
            "side_effects": sorted(self.side_effects),
        }


def talk_subject(source: str, npc: str, destination: str) -> str:
    return f"{TALK}:{source}:{npc}>{destination}"


def describe(requirement: Requirement) -> list[str]:
    """Readable atoms, for the dashboard and decisions."""
    words = []
    for atom in sorted(requirement.atoms, key=repr):
        if isinstance(atom, req.Quest):
            words.append(f"{atom.quest}: {atom.part} done" if atom.part
                         else f"{atom.quest} completed")
        elif isinstance(atom, req.Unknown):
            words.append(f"unknown: {atom.reason}")
        else:
            words.append(f"{type(atom).__name__.lower()} {atom.lock_id}")
    return words


# ---------------------------------------------------------------- quests

@dataclass(frozen=True, slots=True)
class QuestInfo:
    """A quest's journal name, its part names by uid, and the parts some
    dialogue can fail."""
    name: str
    parts: Mapping[str, str]
    failable: frozenset[str]


def load_quests(map_root: Path) -> dict[str, QuestInfo]:
    """Every quest under ``interfaces/quests``, by its uid (directory)."""
    quests: dict[str, QuestInfo] = {}
    for path in sorted((map_root / "interfaces" / "quests").glob("*/quest.xml")):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        quest = root if root.tag == "quest" else root.find("quest")
        if quest is None:
            continue
        parts = {part.get("uid", ""): part.get("name", "")
                 for part in quest.iter("part") if part.get("uid")}
        failable = frozenset(
            value.split("::")[-1] for action in quest.iter("action")
            for value in [action.get("fail") or ""] if value)
        quests[path.parent.name] = QuestInfo(
            quest.get("name", path.parent.name), parts, failable)
    return quests


def quest_atom(quests: Mapping[str, QuestInfo], uid: str,
               part: str = "") -> req.Atom:
    """The ``Quest`` atom for a quest uid and part uid, by journal names,
    or ``Unknown`` when the content does not name them."""
    info = quests.get(uid)
    if info is None:
        return req.Unknown(f"no quest {uid}")
    if not part:
        return req.Quest(info.name)
    if part not in info.parts:
        return req.Unknown(f"no part {part} in {info.name}")
    return req.Quest(info.name, info.parts[part], part,
                     part not in info.failable)


# --------------------------------------------------------------- voyages

@dataclass(frozen=True, slots=True)
class VoyageSpec:
    """A voyage compiled from interface XML, before it has a map edge."""
    destination: str
    x: int
    y: int
    pages: tuple[DialoguePage, ...]
    requires: Requirement
    side_effects: frozenset[str]


def _page(dialog: ET.Element, following: str, unknown: list[str]
          ) -> DialoguePage | None:
    messages, links, choose = [], [], -1
    for child in dialog:
        if child.tag not in _PAGE_TAGS:
            unknown.append(f"<{child.tag}> on the voyage's path")
            continue
        if child.tag == "message":
            if child.attrib or len(child):
                unknown.append("a formatted message on the voyage's path")
            messages.append((child.text or "").strip())
            continue
        if set(child.attrib) - _RESPONSE_ATTRS:
            unknown.append("a response with other attributes")
        action = child.get("action") or ""
        destination = child.get("destination") or ""
        if action and action != "close":
            unknown.append(f"a response with action {action}")
        if destination == following and not action and choose < 0:
            choose = len(links)
        links.append((action, destination, child.get("message") or ""))
    if not messages:
        # The server sends no page for a dialogue without a message.
        unknown.append("a dialogue page with no message")
    if choose < 0:
        return None
    return DialoguePage("\n\n".join(messages), tuple(links), choose)


def _teleport(dialog: ET.Element, unknown: list[str]
              ) -> tuple[str, int, int, bool] | None:
    teleport, closes = None, False
    for child in dialog:
        if child.tag not in _TARGET_TAGS:
            unknown.append(f"<{child.tag}> beside the teleport")
        elif child.tag == "close":
            closes = True
        elif child.tag == "message":
            if child.attrib or len(child):
                unknown.append("a formatted message beside the teleport")
        elif child.tag == "action":
            if set(child.attrib) != {"teleport"} or (child.text or "").strip():
                unknown.append("another action beside the teleport")
            elif teleport is None:
                teleport = child.get("teleport") or ""
            else:
                unknown.append("two teleports")
    if teleport is None:
        return None
    found = _TELEPORT.match(teleport.strip())
    if found is None:
        return None
    path, x, y, savebed = found.groups()
    if not closes:
        unknown.append("the teleport does not close the dialogue")
    if not path.startswith("/") or x is None or y is None:
        unknown.append("a teleport without an absolute map and square")
        return None
    return posixpath.normpath(path), int(x), int(y), bool(savebed)


def compile_voyages(xml_path: Path, npc: str,
                    quests: Mapping[str, QuestInfo]) -> list[VoyageSpec]:
    """The voyages one NPC named ``npc`` offers through one interface file.

    Interfaces for another NPC (``npc=``) are skipped. Each dialogue with a
    teleport, reached from ``hello`` by response links, is one voyage; its
    pages are the dialogues on that path. The interface's ``state`` becomes
    a ``Quest`` atom when it is ``completed`` on the quest itself; any other
    state, and a quest file's default (stateless) interface, is ``Unknown``.
    """
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return []
    parents = {child: parent for parent in root.iter() for child in parent}
    quest_uid = xml_path.parent.name
    in_quest_file = any(element.tag == "quest" for element in root.iter())
    voyages: list[VoyageSpec] = []

    def hers(interface: ET.Element) -> bool:
        named = interface.get("npc")
        if interface.get("npc_id") or (
                named and named.casefold() != npc.casefold()):
            return False
        return bool(named) or not in_quest_file

    mine = [interface for interface in root.iter("interface") if hers(interface)]
    for interface in mine:
        base: list[req.Atom] = []
        # The server's dispatch (InterfaceBuilder.finish) tries regex
        # matchers before the named dialogue, and an <and>/<or> directly in
        # any of her interfaces compiles to the module's preconds(), which
        # can pick another dialogue class. <precond> and <code> belong to
        # their own interface's class, so they count only in this one (as
        # non-dialogue children). Only plain named dialogues, with no such
        # dispatch for this NPC in the file, are modelled.
        if any(child.tag != "dialog" for child in interface):
            base.append(req.Unknown("an interface with more than dialogues"))
        names = [dialog.get("name") or "" for dialog in interface.findall("dialog")]
        if any(dialog.get("regex") is not None
               for dialog in interface.findall("dialog")):
            base.append(req.Unknown("a regex dialogue"))
        if len(names) != len(set(names)):
            base.append(req.Unknown("two dialogues with one name"))
        if any(child.tag in ("and", "or")
               for other in mine if other is not interface for child in other):
            base.append(req.Unknown("another of the NPC's interfaces picks "
                                    "its dialogue by condition"))
        state = interface.get("state") or ""
        parent = parents.get(interface)
        if interface.get("inherit"):
            base.append(req.Unknown("an inherited dialogue"))
        if in_quest_file:
            if state == "completed" and parent is not None and parent.tag == "quest":
                base.append(quest_atom(quests, quest_uid))
            elif state:
                base.append(req.Unknown(f"dialogue state {state}"))
            else:
                base.append(req.Unknown("the quest's default dialogue"))
        elif state:
            base.append(req.Unknown(f"dialogue state {state}"))
        dialogs: dict[str, ET.Element] = {}
        for dialog in interface.findall("dialog"):
            dialogs.setdefault(dialog.get("name") or "", dialog)
        if "hello" not in dialogs:
            continue
        # Breadth-first from hello along response links, as quests.py does.
        paths: dict[str, tuple[str, ...]] = {"hello": ("hello",)}
        queue = ["hello"]
        while queue:
            name = queue.pop(0)
            for response in dialogs[name].findall("response"):
                target = response.get("destination") or ""
                if (target in dialogs and target not in paths and
                        not response.get("action")):
                    paths[target] = paths[name] + (target,)
                    queue.append(target)
        for name, path in paths.items():
            unknown: list[str] = []
            teleport = _teleport(dialogs[name], unknown)
            if teleport is None:
                continue
            pages = []
            for here, following in zip(path, path[1:]):
                dialog = dialogs[here]
                if set(dialog.attrib) - {"name"}:
                    unknown.append("a dialogue with a title, regex or inherit")
                page = _page(dialog, following, unknown)
                if page is None:
                    unknown.append("no link leads on")
                    break
                pages.append(page)
            if set(dialogs[name].attrib) - {"name"}:
                unknown.append("a dialogue with a title, regex or inherit")
            if not pages:
                # A teleport straight from hello: no page to check first.
                unknown.append("the teleport runs on hello")
            atoms = list(base) + [req.Unknown(reason)
                                  for reason in dict.fromkeys(unknown)]
            destination, x, y, savebed = teleport
            voyages.append(VoyageSpec(
                destination, x, y, tuple(pages), req.requires(*atoms),
                frozenset({SAVEBED}) if savebed else frozenset()))
    return voyages


# ------------------------------------------------------ quest-gated exits

def quest_gate(source_code: str) -> tuple[str, str] | None:
    """The (quest uid, part uid) an exit script bounces on, or None.

    Recognised, and only this: ``qm = QuestManager(activator, uid)``, and an
    ``if not qm.completed("part"):`` whose body calls ``SetReturnValue(1)``
    (refusing the exit), with no other ``SetReturnValue`` or ``TeleportTo``
    anywhere else in the script (``lake_portal.py``).
    """
    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return None
    managers: dict[str, str] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and
                isinstance(node.targets[0], ast.Name) and
                isinstance(node.value, ast.Call) and
                isinstance(node.value.func, ast.Name) and
                node.value.func.id == "QuestManager" and
                len(node.value.args) == 2 and
                all(isinstance(arg, ast.Name) for arg in node.value.args) and
                node.value.args[0].id == "activator"):
            managers[node.targets[0].id] = node.value.args[1].id

    def calls(nodes: Iterable[ast.AST], name: str) -> list[ast.Call]:
        found = []
        for root in nodes:
            for node in ast.walk(root):
                if isinstance(node, ast.Call):
                    func = node.func
                    called = (func.id if isinstance(func, ast.Name) else
                              func.attr if isinstance(func, ast.Attribute)
                              else "")
                    if called == name:
                        found.append(node)
        return found

    gates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and
                isinstance(test.operand, ast.Call)):
            continue
        call = test.operand
        if not (isinstance(call.func, ast.Attribute) and
                call.func.attr == "completed" and
                isinstance(call.func.value, ast.Name) and
                call.func.value.id in managers and len(call.args) == 1 and
                isinstance(call.args[0], ast.Constant) and
                isinstance(call.args[0].value, str)):
            continue
        refusals = calls(node.body, "SetReturnValue")
        if not any(len(item.args) == 1 and
                   isinstance(item.args[0], ast.Constant) and
                   item.args[0].value == 1 for item in refusals):
            continue
        gates.append((node, managers[call.func.value.id], call.args[0].value))
    if len(gates) != 1:
        return None
    gate, quest, part = gates[0]
    inside = {id(item) for item in calls(gate.body, "SetReturnValue") +
              calls(gate.body, "TeleportTo")}
    outside = [item for item in calls([tree], "SetReturnValue") +
               calls([tree], "TeleportTo") if id(item) not in inside]
    return None if outside else (quest, part)


# ------------------------------------------------------------- the offer

@dataclass(frozen=True, slots=True)
class DialogueOffer:
    """The voyages and closed gated exits of one plan (flag on).

    ``voyages`` maps each offered planner edge to the one voyage it takes;
    several compiled voyages can share an edge (the Lost Memories Sam on
    ``world_4_85`` has the ``completed`` voyage and ``the_journey``'s
    savebed one), so the edge alone never names the pages to follow.
    """
    edges: tuple = ()
    closed_exits: frozenset[tuple[str, int, int]] = frozenset()
    voyages: Mapping[Any, Voyage] = field(default_factory=dict, compare=False)
    by_source: Mapping[str, tuple] = field(default_factory=dict, compare=False)

    def from_map(self, path: str) -> tuple:
        return self.by_source.get(path, ())

    def voyage(self, edge: Any) -> Voyage | None:
        return self.voyages.get(edge)

    def closes(self, edge: Any) -> bool:
        """Whether an exit's quest gate is closed to her."""
        return (getattr(edge, "kind", "") == "exit" and
                (edge.source, edge.x, edge.y) in self.closed_exits)


def make_offer(voyages: Iterable[Voyage],
               closed_exits: Iterable[tuple[str, int, int]]) -> DialogueOffer:
    """An offer of these voyages. Two voyages on one edge (one NPC, one
    arrival, different dialogues) would leave the pages to follow unclear:
    neither is offered."""
    by_edge: dict[Any, list[Voyage]] = defaultdict(list)
    for voyage in voyages:
        by_edge[voyage.edge].append(voyage)
    chosen = {edge: items[0] for edge, items in by_edge.items() if len(items) == 1}
    by_source: dict[str, list] = defaultdict(list)
    for edge in chosen:
        by_source[edge.source].append(edge)
    return DialogueOffer(tuple(chosen), frozenset(closed_exits), chosen,
                         {path: tuple(items) for path, items in by_source.items()})


def enabled(client: Any) -> bool:
    """The owner's ``dialogue_edges`` flag (default off)."""
    return bool(getattr(getattr(client, "config", None), "dialogue_edges", False))


def _refusals(client: Any, fingerprint: str, now: float) -> dict[str, dict]:
    """Her live voyage refusals; a changed unlock state lifts them all."""
    refusals = getattr(client, "_dialogue_refusals", None)
    if not isinstance(refusals, dict):
        return {}
    live = {subject: row for subject, row in refusals.items()
            if row.get("until", 0) > now and row.get("inputs") == fingerprint}
    if len(live) != len(refusals):
        try:
            client._dialogue_refusals = live
        except AttributeError:
            pass
    return live


def refuse(client: Any, graph: Any, voyage: Voyage, reason: str, *,
           now: float | None = None) -> None:
    """Keep a voyage out of her plans after a page that did not match, a
    voyage that did not leave or one that arrived elsewhere."""
    from .unlocks import unlock_state
    now = time.time() if now is None else now
    refusals = getattr(client, "_dialogue_refusals", None)
    if not isinstance(refusals, dict):
        refusals = {}
    refusals[voyage.subject] = {
        "at": now, "until": now + REFUSAL_SECONDS, "reason": reason[:200],
        "inputs": unlock_state(client, graph).fingerprint}
    try:
        client._dialogue_refusals = refusals
    except AttributeError:
        pass


def _evaluate(client: Any, graph: Any) -> tuple[DialogueOffer, list[dict]]:
    """The offer for her state now, and each voyage's status."""
    from .unlocks import unlock_state
    state = unlock_state(client, graph)
    now = time.time()
    refused = _refusals(client, state.fingerprint, now)
    savebed = str(getattr(getattr(client, "state", None), "savebed_map", "") or "")
    voyages: list[Voyage] = list(getattr(graph, "voyages", ()))
    status: dict[int, str] = {}
    candidates = []
    for voyage in voyages:
        if SAVEBED in voyage.side_effects:
            status[id(voyage)] = "never: moves her savebed"
        elif not state.quests_loaded and any(
                isinstance(atom, req.Quest) for atom in voyage.requires.atoms):
            status[id(voyage)] = "journal not loaded"
        elif not state.met(voyage.requires):
            status[id(voyage)] = "requirement not met"
        elif voyage.subject in refused:
            status[id(voyage)] = f"refused: {refused[voyage.subject]['reason']}"
        else:
            candidates.append(voyage)
    closed = frozenset(key for key, requirement in
                       getattr(graph, "gated_exits", {}).items()
                       if not state.met(requirement))
    # No voyage may strand her: from its arrival there must be a route,
    # over the static graph and the other voyages offered, back to where
    # she boarded or to her savebed. Dropping one can strand another, so
    # repeat until nothing changes.
    while True:
        offer = make_offer(candidates, closed)
        kept = []
        for voyage in candidates:
            if offer.voyage(voyage.edge) is not voyage:
                status[id(voyage)] = "another dialogue shares its NPC and arrival"
                continue
            edge = voyage.edge
            homes = {edge.source} | ({savebed} if savebed else set())
            try:
                reached = graph.reachable_maps(
                    edge.destination, (edge.destination_x, edge.destination_y),
                    allow_locked=state.access, offer=offer, stop_maps=homes)
            except ValueError:
                reached = frozenset()
            if reached & homes:
                kept.append(voyage)
            else:
                status[id(voyage)] = "no known route back"
        if len(kept) == len(candidates):
            break
        candidates = kept
    for voyage in candidates:
        status[id(voyage)] = "offered"
    rows = [{**voyage.summary(), "status": status.get(id(voyage), "")}
            for voyage in voyages]
    return offer, rows


def _cached(client: Any, graph: Any) -> tuple[DialogueOffer, list[dict]]:
    from .unlocks import unlock_state
    state = unlock_state(client, graph)
    refusals = getattr(client, "_dialogue_refusals", None) or {}
    key = (id(graph), state.access, state.quests, state.quests_loaded,
           str(getattr(getattr(client, "state", None), "savebed_map", "") or ""),
           tuple(sorted((subject, row.get("until")) for subject, row in refusals.items())))
    cache = getattr(client, "_dialogue_offer_cache", None)
    if (isinstance(cache, tuple) and cache[0] == key and cache[1] is graph and
            all(row.get("until", 0) > time.time() for row in refusals.values())):
        return cache[2]
    result = _evaluate(client, graph)
    try:
        client._dialogue_offer_cache = (key, graph, result)
    except AttributeError:
        pass
    return result


def dialogue_offer(client: Any, graph: Any) -> DialogueOffer | None:
    """What one plan may use: None with the flag off (plans unchanged)."""
    if not enabled(client) or not getattr(graph, "voyages", None) and \
            not getattr(graph, "gated_exits", None):
        return None
    return _cached(client, graph)[0]


def dialogue_summary(client: Any, graph: Any) -> dict[str, Any]:
    """A read-only view for the dashboard: the flag, each voyage and why it
    is or is not offered, and the quest-gated exits."""
    on = enabled(client)
    if on:
        _, rows = _cached(client, graph)
    else:
        rows = [{**voyage.summary(), "status": "flag off"}
                for voyage in getattr(graph, "voyages", ())]
    from .unlocks import unlock_state
    state = unlock_state(client, graph) if on else None
    gates = [{"exit": f"{source}:{x},{y}", "requires": describe(requirement),
              "open": (state.met(requirement) if state is not None else None)}
             for (source, x, y), requirement in sorted(
                 getattr(graph, "gated_exits", {}).items())]
    return {"enabled": on, "voyages": rows, "gated_exits": gates}


def compile_graph(graph: Any, npcs: Iterable[tuple[str, int, int, str, str]],
                  gated: Mapping[tuple[str, int, int], tuple[str, str]],
                  make_edge: Callable[..., Any]) -> None:
    """Fill ``graph.voyages`` and ``graph.gated_exits``.

    ``npcs`` are (map, x, y, name, interface resource) for every NPC with an
    interface event; ``gated`` maps an exit square to its recognised quest
    gate; ``make_edge(source, destination, x, y, dx, dy, npc)`` builds the
    planner edge.
    """
    map_root = Path(graph.map_root)
    quests = load_quests(map_root)
    voyages: list[Voyage] = []
    seen = set()
    for source, x, y, npc, resource in npcs:
        path = map_root / resource.lstrip("/")
        for spec in compile_voyages(path, npc, quests):
            if spec.destination not in graph.nodes or spec.destination == source:
                continue
            edge = make_edge(source, spec.destination, x, y, spec.x, spec.y, npc)
            identity = (edge, spec.pages)
            if identity in seen:
                continue
            seen.add(identity)
            voyages.append(Voyage(edge, npc, resource, spec.pages,
                                  spec.requires, spec.side_effects))
    graph.voyages = voyages
    graph.gated_exits = {key: req.requires(quest_atom(quests, quest, part))
                         for key, (quest, part) in gated.items()}
