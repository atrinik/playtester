"""Formal quest catalog and character-specific quest-list parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .model import QuestPartProgress, QuestProgress

try:
    from tools.world_content_audit import ROOT as CONTENT_ROOT
except ModuleNotFoundError:
    from world_content_audit import ROOT as CONTENT_ROOT


@dataclass(slots=True)
class QuestObjective:
    kind: str
    name: str = ""
    archetype: str = ""
    quantity: int = 1


@dataclass(slots=True)
class QuestPart:
    name: str
    uid: str
    info: str = ""
    objectives: list[QuestObjective] = field(default_factory=list)
    children: list["QuestPart"] = field(default_factory=list)


@dataclass(slots=True)
class QuestDefinition:
    name: str
    source: Path
    parts: list[QuestPart] = field(default_factory=list)


def flatten_parts(parts: list[QuestPart]) -> list[QuestPart]:
    """Return parent and nested quest parts in authored order."""
    result: list[QuestPart] = []
    for part in parts:
        result.append(part)
        result.extend(flatten_parts(part.children))
    return result


def dialogue_choices(definition: QuestDefinition, npc: str, *,
                     action: str = "", uid: str = "",
                     object_arch: str = "", object_name: str = "") -> tuple[str, ...]:
    """Compile the response destinations leading to a progress-producing dialog.

    ADS interfaces are small directed graphs. Following the shortest path from
    ``hello`` to the requested action/object is safer than accepting arbitrary
    first links, particularly where a refusal or postponement is also offered.
    """
    root = ET.parse(definition.source).getroot()
    quest = root if root.tag == "quest" else root.find("quest")
    if quest is None:
        return ()

    def value_matches(value: str | None) -> bool:
        return bool(value and (value == uid or value.endswith("::" + uid)))

    for interface in quest.iter("interface"):
        if interface.get("npc", "").casefold() != npc.casefold():
            continue
        dialogs = {node.get("name", ""): node for node in interface.findall("dialog")}
        goals: set[str] = set()
        for name, dialog in dialogs.items():
            if action and uid and any(value_matches(node.get(action))
                                      for node in dialog.iter("action")):
                goals.add(name)
            if object_arch and any(node.get("arch") == object_arch
                                   for node in dialog.iter("object")):
                goals.add(name)
            if object_name and any(node.get("name") == object_name
                                   for node in dialog.iter("object")):
                goals.add(name)
        if not goals or "hello" not in dialogs:
            continue
        queue = deque([("hello", ())])
        visited = {"hello"}
        while queue:
            name, path = queue.popleft()
            if name in goals:
                return tuple(r"=:" + re.escape(destination) + r"\]"
                             for destination in path)
            for response in dialogs[name].findall("response"):
                destination = response.get("destination")
                if destination and destination in dialogs and destination not in visited:
                    visited.add(destination)
                    queue.append((destination, path + (destination,)))
    return ()


def _parse_part(node: ET.Element) -> QuestPart:
    info_node = node.find("info")
    info = "" if info_node is None else "".join(info_node.itertext()).strip()
    part = QuestPart(node.get("name", ""), node.get("uid", ""), info)
    for item in node.findall("item"):
        part.objectives.append(QuestObjective(
            "item", item.get("name", ""), item.get("arch", ""),
            int(item.get("nrof", "1")),
        ))
    for kill in node.findall("kill"):
        part.objectives.append(QuestObjective(
            "kill", kill.get("name", ""), kill.get("arch", ""),
            int(kill.get("nrof", "1")),
        ))
    part.children = [_parse_part(child) for child in node.findall("part")]
    return part


def load_catalog(root: Path | None = None) -> dict[str, QuestDefinition]:
    if root is None:
        root = CONTENT_ROOT / "maps" / "interfaces" / "quests"
    catalog: dict[str, QuestDefinition] = {}
    for path in sorted(root.glob("*/quest.xml")):
        xml_root = ET.parse(path).getroot()
        quest_node = xml_root if xml_root.tag == "quest" else xml_root.find("quest")
        if quest_node is None:
            continue
        definition = QuestDefinition(
            quest_node.get("name", path.parent.name), path,
            [_parse_part(node) for node in quest_node.findall("part")],
        )
        catalog[definition.name] = definition
    return catalog


_MARKUP = re.compile(r"\[[^]]+\]")
_PART = re.compile(
    r"^\[b\](.*?)\[/b\](?::\s*(.*?))?(?:\s+\[\](done|failed)\])?$"
)
_KILL = re.compile(r"^\[x=10\]Status:\s*(\d+)\s*/\s*(\d+)")


def parse_quest_book(data: bytes) -> dict[str, QuestProgress]:
    text = data.rstrip(b"\0").decode("utf-8", "replace")
    if "No quests to speak of" in text:
        return {}
    quests: dict[str, QuestProgress] = {}
    section = "active"
    current_quest: QuestProgress | None = None
    current_part: QuestPartProgress | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "Completed quests:" in line:
            section = "completed"
            line = line.split("Completed quests:", 1)[1]
        titles = re.findall(r"\[title\](.*?)\[/title\]", line)
        for title in titles:
            if title in ("Incomplete quests:", "Completed quests:",
                         "No quests to speak of."):
                continue
            current_quest = QuestProgress(title, section)
            quests[title] = current_quest
            current_part = None
        match = _PART.match(line)
        if match and current_quest:
            name, description, marker = match.groups()
            current_part = QuestPartProgress(
                _MARKUP.sub("", name),
                _MARKUP.sub("", description or ""),
                marker or "active",
            )
            current_quest.parts.append(current_part)
            continue
        match = _KILL.match(line)
        if match and current_part:
            current_part.current, current_part.required = map(int, match.groups())
    return quests


MAIN_STORY = ("Escaping the Deserted Island", "Lost Memories")


def next_formal_quest(quests: dict[str, QuestProgress],
                      catalog: dict[str, QuestDefinition]) -> QuestDefinition | None:
    """Return the next unfinished formal quest in repository/report order."""
    for name, definition in catalog.items():
        progress = quests.get(name)
        if progress is None or progress.status != "completed":
            return definition
    return None
