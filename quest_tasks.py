"""Adaptive formal-quest executors driven by live per-character quest state."""

from __future__ import annotations

import re
import time

from .client import AtrinikClient
from .navigation import NavigateTask, WorldGraph
from .tasks import BotTask, FarmTask, LootPolicy, TaskStatus


class DialogTask(BotTask):
    """Open a named NPC dialog and follow safe single-choice continuations."""

    def __init__(self, npc: str, *, max_links: int = 20,
                 choices: tuple[str, ...] = ()):
        super().__init__(f"dialog:{npc}")
        self.npc = npc
        self.max_links = max_links
        self.choices = tuple(re.compile(choice, re.I) for choice in choices)
        self._sent_hello = False
        self._last_text = ""
        self._followed = 0
        self._last_action = 0.0
        self._start_map = ""

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self._start_map = client.state.map.path

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if not self._sent_hello:
            await client.talk("hello", self.npc)
            self._sent_hello = True
            self._last_action = time.monotonic()
            return
        interface = client.state.interface
        if interface is None or interface.title.casefold() != self.npc.casefold():
            if interface is None and self._followed:
                # A followed action="close" link is terminal. The client
                # clears it locally because the server does not echo a close
                # interface packet in response to TALK_CLOSE.
                self.complete()
                return
            # Some final dialog actions teleport the player immediately and do
            # not send another interface packet (boats/cutscenes in particular).
            if self._start_map and client.state.map.path != self._start_map:
                self.complete()
                return
            if time.monotonic() - self._last_action > 2.0:
                self.fail(f"{self.npc} did not open a dialog")
            return
        if interface.text == self._last_text:
            if not interface.links and time.monotonic() - self._last_action > 0.6:
                self.complete()
            return
        self._last_text = interface.text
        if not interface.links:
            self.complete()
            return
        candidates = list(range(len(interface.links)))
        if len(candidates) > 1:
            commands = []
            for link in interface.links:
                match = re.search(r"\[a=([^:\]]*):([^\]]*)\]", link)
                commands.append(match.groups() if match else ("", ""))
            if all(action == "close" for action, _ in commands):
                # Differently worded goodbye links are operationally
                # identical and can be closed deterministically.
                candidates = candidates[:1]
            elif not self.choices:
                self.fail("dialog has multiple choices and no policy")
                return
            else:
                # Policy order expresses preference. Use the first policy
                # which matches the current page rather than combining every
                # match from a multi-page dialogue route.
                candidates = []
                for choice in self.choices:
                    candidates = [
                        index for index, link in enumerate(interface.links)
                        if choice.search(link)
                    ]
                    if candidates:
                        break
                if not candidates:
                    self.fail(
                        "dialog choice policy did not select exactly one link")
                    return
            if len(candidates) > 1:
                # Quest XML can intentionally present differently worded
                # answers which all lead to the same destination (Lost
                # Memories starts with two such `remember` responses). This is
                # deterministic, not an operator choice. Keep rejecting links
                # which resolve to genuinely different destinations.
                destinations = []
                for index in candidates:
                    match = re.search(r"=:([^]]+)\]", interface.links[index])
                    destinations.append(match.group(1) if match else "")
                if not destinations[0] or len(set(destinations)) != 1:
                    self.fail("dialog choice policy selected ambiguous links")
                    return
                candidates = candidates[:1]
        if self._followed >= self.max_links:
            self.fail("dialog continuation limit reached")
            return
        await client.choose_interface_link(candidates[0])
        self._followed += 1
        self._last_action = time.monotonic()


class EscapingDesertedIslandTask(BotTask):
    """Resume and finish the first formal quest from any recorded quest state."""

    QUEST = "Escaping the Deserted Island"
    SAM_MAP = "/shattered_islands/world_-7_76"
    # NPC landmark coordinates are occupied by the NPC. Use a known walkable
    # interaction square within TALK_NPC's size-3 search radius instead.
    SAM_XY = (8, 13)
    LAKE_MAP = "/shattered_islands/world_-6_76"
    LAKE_XY = (19, 14)
    WATER_XY = (21, 16)  # Walkable square adjacent to clean water at 22,16.
    TREE_XY = (16, 17)  # Adjacent to the thick-branch tree at 17,17.
    CAVE_MAP = "/shattered_islands/deserted_tutorial_island/mushroom_cavern"
    MUSHROOM_PATROL = (
        (3, 1), (5, 1), (2, 2), (3, 2), (4, 2), (1, 3),
        (2, 3), (3, 3), (2, 4), (1, 5), (2, 5), (1, 6),
    )

    def __init__(self, graph: WorldGraph):
        super().__init__("quest:Escaping the Deserted Island")
        self.graph = graph
        self.child: BotTask | None = None
        self._last_quest_request = 0.0
        self._applied_for_part = ""

    def _active_part(self, client: AtrinikClient) -> str:
        quest = client.state.quests.get(self.QUEST)
        if quest is None:
            return ""
        for part in reversed(quest.parts):
            if part.status not in ("done", "completed"):
                return part.name
        return quest.parts[-1].name if quest.parts else ""

    def _has(self, client: AtrinikClient, pattern: str, quantity: int = 1):
        regex = re.compile(pattern, re.I)
        found = [item for item in client.state.inventory if regex.search(item.name)]
        return sum(item.quantity for item in found) >= quantity, (found[0] if found else None)

    @staticmethod
    def _at(client: AtrinikClient, map_path: str,
            xy: tuple[int, int]) -> bool:
        return (client.state.map.path == map_path and
                (client.state.map.world_x, client.state.map.world_y) == xy)

    async def _return_to_sam(self, client: AtrinikClient) -> None:
        if self._at(client, self.SAM_MAP, self.SAM_XY):
            await self._set_child(client, DialogTask("Sam Goodberry"))
        else:
            await self._navigate(client, self.SAM_MAP, self.SAM_XY)

    async def _set_child(self, client: AtrinikClient, child: BotTask) -> None:
        self.child = child
        await child.start(client)

    async def _navigate(self, client: AtrinikClient, map_path: str,
                        xy: tuple[int, int] | None = None) -> None:
        await self._set_child(client, NavigateTask(self.graph, map_path, xy))

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.phase != "playing":
            return
        if not client.state.quests_loaded:
            if time.monotonic() - self._last_quest_request > 1.0:
                await client.request_quests()
                self._last_quest_request = time.monotonic()
            return
        quest = client.state.quests.get(self.QUEST)
        if quest and quest.status == "completed":
            self.complete()
            return
        if self.child is not None:
            await self.child.tick(client)
            if self.child.status == TaskStatus.FAILED:
                self.fail(f"{self.child.name}: {self.child.error}")
                return
            if self.child.status != TaskStatus.COMPLETE:
                return
            self.child = None
            await client.request_quests()
            self._last_quest_request = time.monotonic()
            return
        if time.monotonic() - self._last_quest_request > 3.0:
            await client.request_quests()
            self._last_quest_request = time.monotonic()

        part = self._active_part(client)
        if not part:
            if client.state.map.path != self.SAM_MAP or (
                    client.state.map.world_x, client.state.map.world_y) != self.SAM_XY:
                await self._navigate(client, self.SAM_MAP, self.SAM_XY)
            else:
                await self._set_child(client, DialogTask("Sam Goodberry"))
            return
        if part == "Clean Water Source":
            await self._navigate(client, self.LAKE_MAP, self.LAKE_XY)
            return
        if part == "Reporting To Sam Goodberry":
            if client.state.map.path != self.SAM_MAP or (
                    client.state.map.world_x, client.state.map.world_y) != self.SAM_XY:
                await self._navigate(client, self.SAM_MAP, self.SAM_XY)
            else:
                await self._set_child(client, DialogTask("Sam Goodberry"))
            return
        if part == "Collecting Clean Water":
            has_filled, _ = self._has(client, r"^water barrel$")
            if has_filled:
                await self._return_to_sam(client)
                return
            if client.state.map.path != self.LAKE_MAP or (
                    client.state.map.world_x, client.state.map.world_y) != self.WATER_XY:
                await self._navigate(client, self.LAKE_MAP, self.WATER_XY)
                return
            _, barrel = self._has(client, "empty barrel")
            if barrel and self._applied_for_part != part:
                await client.apply(barrel.tag)
                self._applied_for_part = part
                return
            return
        if part == "Collecting Mushrooms":
            have_mushrooms, _ = self._has(client, "wild white mushroom", 70)
            if have_mushrooms:
                await self._return_to_sam(client)
            elif client.state.map.path != self.CAVE_MAP:
                await self._navigate(client, self.CAVE_MAP)
            else:
                await self._set_child(client, FarmTask(
                    zone=self.CAVE_MAP, item="wild white mushroom", quantity=70,
                    patrol=list(self.MUSHROOM_PATROL),
                    loot=LootPolicy(take_all=False, include=("wild white mushroom",)),
                ))
            return
        if part == "Collecting Branches":
            have_branches, _ = self._has(client, "thick tree branch", 10)
            if have_branches:
                await self._return_to_sam(client)
                return
            if client.state.map.path != self.LAKE_MAP or (
                    client.state.map.world_x, client.state.map.world_y) != self.TREE_XY:
                await self._navigate(client, self.LAKE_MAP, self.TREE_XY)
                return
            _, saw = self._has(client, "saw")
            if saw:
                await client.apply(saw.tag)
                return
        self.fail(f"unsupported or inconsistent quest part: {part!r}")
