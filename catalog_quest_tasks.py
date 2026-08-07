"""Catalog-driven executors for Atrinik's formal authored quests."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import constants as c
from .client import AtrinikClient
from .navigation import NavigateTask, NavigateThenTask, WorldGraph
from .quest_tasks import DialogTask, EscapingDesertedIslandTask
from .quests import (QuestDefinition, QuestObjective, dialogue_choices,
                     flatten_parts, load_catalog)
from .tasks import BotTask, FarmTask, LootPolicy, TaskStatus


@dataclass(frozen=True, slots=True)
class Place:
    map_path: str
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class Action:
    """How to make progress when a quest objective is not yet satisfied."""

    kind: str
    place: Place | None = None
    npc: str = ""
    item: str = ""
    target: str = ""
    quantity: int = 1
    patrol: tuple[tuple[int, int], ...] = ()
    object_arch: str = ""
    object_name: str = ""
    goal_action: str = ""
    goal_uid: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PartPolicy:
    action: Action
    turnin_npc: str = ""
    turnin_place: Place | None = None
    turnin_uid: str = ""
    turnin_choices: tuple[str, ...] = ()
    objective_pattern: str = ""


@dataclass(frozen=True, slots=True)
class QuestPolicy:
    name: str
    start: Action
    parts: dict[str, PartPolicy]
    priority: tuple[str, ...] = ()


def _p(path: str, x: int, y: int) -> Place:
    return Place(path, x, y)


NPC: dict[str, Place] = {
    "Albar": _p("/shattered_islands/world_13_6", 22, 5),
    "Jonaslen": _p("/shattered_islands/world_0_69", 2, 5),
    "Silmedsen": _p("/shattered_islands/world_5_52", 7, 10),
    "Crimdon Crazymix": _p("/shattered_islands/world_4_42", 19, 22),
    "Gwenty": _p("/shattered_islands/world_4_53_-1", 4, 11),
    "Brownrott": _p("/shattered_islands/world_4_51_-2", 18, 14),
    "Frevia": _p("/shattered_islands/world_8_59", 6, 6),
    "Tomboyish Fairy": _p("/shattered_islands/world_7_61", 8, 8),
    "Galann Strongfist": _p("/shattered_islands/world_1_67", 22, 16),
    "Gandyld": _p("/shattered_islands/world_6_58", 19, 10),
    "Lairwenn": _p("/shattered_islands/world_2_68", 3, 20),
    "Melanye": _p("/shattered_islands/world_1_67", 6, 19),
    "Farmer Maggot": _p("/shattered_islands/world_1_47", 19, 19),
    "Maplevale": _p("/shattered_islands/world_9_69", 8, 6),
    "Talthor Redeye": _p("/shattered_islands/world_0_68_1", 23, 10),
    "Lynren": _p("/shattered_islands/strakewood_island/zechna_temple/zechna_temple_0_0", 10, 18),
    "Steve Bruck": _p("/shattered_islands/world_5_48", 9, 20),
    "Gashir": _p("/shattered_islands/world_6_48", 3, 21),
    "Rienn Howell": _p("/shattered_islands/world_5_67", 9, 6),
    "Tortwald Howell": _p("/shattered_islands/strakewood_island/underground_city/underground_city_2_3_-1", 3, 2),
    # Dockside Sam owns the Lost Memories quest interface. The other Incuna
    # Sam on world_0_83 is the later voyage NPC for sailing to Strakewood.
    "Sam Goodberry (Incuna)": _p("/shattered_islands/world_4_85", 4, 13),
    "Brelend Lee": _p("/shattered_islands/world_4_84", 7, 7),
    "Ken Berger": _p("/shattered_islands/world_4_84", 20, 9),
    "John Aldman": _p("/shattered_islands/world_4_83_-1", 6, 17),
    "Tom Istrad": _p("/shattered_islands/world_3_83", 11, 10),
    "Arvend Blessed": _p("/shattered_islands/world_3_84", 15, 17),
    "Angela Sawthrow": _p("/shattered_islands/world_3_83_-1", 14, 16),
    "Rick Grizley": _p("/shattered_islands/world_3_83", 9, 15),
    "Pedro": _p(
        "/shattered_islands/eld_woods_island/clearhaven/mine/mine_unique",
        9, 12),
}

def _dialog(npc: str, *, place: Place | None = None,
            object_arch: str = "", object_name: str = "",
            goal_action: str = "", goal_uid: str = "",
            choices: tuple[str, ...] = ()) -> Action:
    return Action("dialog", place or NPC[npc], npc, object_arch=object_arch,
                  object_name=object_name, goal_action=goal_action,
                  goal_uid=goal_uid, choices=choices)


def _part(action: Action, npc: str = "", uid: str = "", *,
          turnin_choices: tuple[str, ...] = (),
          objective_pattern: str = "") -> PartPolicy:
    return PartPolicy(action, npc, NPC.get(npc), uid, turnin_choices,
                      objective_pattern)


POLICIES: dict[str, QuestPolicy] = {
    "Clearhaven Mine": QuestPolicy(
        "Clearhaven Mine", _dialog("Pedro"), {
            "A Miner Supply Problem": _part(Action(
                "container_sweep",
                _p("/shattered_islands/eld_woods_island/clearhaven/mine/mine_unique_a",
                   2, 12),
                item="small bomb", quantity=10,
                patrol=((2, 12), (4, 3), (4, 17), (8, 3), (8, 12),
                        (9, 16), (11, 12), (16, 13), (17, 22), (22, 5)),
                object_name="bomb chest"),
                "Pedro", "recover_bombs"),
        }),
    "Construction of Telescope": QuestPolicy(
        "Construction of Telescope", _dialog("Albar"), {
            "The Shard": _part(_dialog("Jonaslen", object_arch="blue_crystal_fragment"), "Albar", "get_shard"),
            "Information Gathering": _part(_dialog("Jonaslen"), "Jonaslen", "ask_flash"),
            "The Flash": _part(_dialog("Albar"), "Albar", "report_flash"),
            "A Clear Crystal": _part(Action("buy", NPC["Albar"], "Morg'eean", "clear crystal"), "Albar", "get_clear_crystal"),
            "Ancient Wood": _part(_dialog("Silmedsen", object_arch="silmedsen_potion_bottle"), "Albar", "get_wood"),
            "The Thirsty Tree": _part(Action("apply", _p("/shattered_islands/world_13_5", 7, 10), item="potion bottle"), "Silmedsen", "get_morliana_water"),
        }),
    "Crazymix's Alchemical Reagents": QuestPolicy(
        "Crazymix's Alchemical Reagents", _dialog("Crimdon Crazymix"), {
            "Mythical Scarlet Pimpernel flower": _part(
                Action("dependency", item="Frevia's Tomboyish Fairy",
                       target="Mythical Scarlet Pimpernel flower"),
                "Crimdon Crazymix", "the_flower"),
        }),
    "Fort Sether Illness": QuestPolicy(
        "Fort Sether Illness", _dialog("Gwenty"), {
            "Poisoned Waters": _part(
                _dialog("Brownrott"), "Brownrott", "figure",
                turnin_choices=(r"=:garden\]", r"=:whynot\]")),
            "The Kobold Gardener": _part(_dialog("Gwenty"), "Gwenty", "report"),
            "Delivery of a Cure": _part(_dialog(
                "Brownrott", goal_action="start", goal_uid="get_hearts",
                choices=(r"=:want\]",))),
            "The Kobold Delicacy": _part(Action(
                "farm", _p("/shattered_islands/world_4_52_-2", 3, 17),
                item="sword spider's heart", target="sword spider", quantity=10,
                patrol=((3, 17), (7, 4), (11, 20), (14, 15), (17, 2), (20, 20))),
                "Brownrott", "get_hearts"),
            "The Reward": _part(_dialog("Gwenty"), "Gwenty", "reward"),
        }),
    "Frevia's Tomboyish Fairy": QuestPolicy(
        "Frevia's Tomboyish Fairy", _dialog("Frevia"), {
            "The Fairy": _part(_dialog("Tomboyish Fairy"), "Tomboyish Fairy", "find_fairy"),
            "Report to Frevia": _part(_dialog("Frevia"), "Frevia", "report"),
        }),
    "Galann's Revenge": QuestPolicy(
        "Galann's Revenge", _dialog("Galann Strongfist"), {
            "Enemy of the Past": _part(Action(
                "kill", _p("/shattered_islands/strakewood_island/old_outpost/old_outpost_0202", 12, 20),
                target="Torathog", patrol=((12, 20),)), "Galann Strongfist", "kill_torathog"),
        }),
    "Gandyld's Mana Crystal": QuestPolicy(
        "Gandyld's Mana Crystal", _dialog("Gandyld"), {
            "A Better Crystal": _part(Action(
                "visit", _p("/shattered_islands/strakewood_island/old_outpost/old_outpost_a_0201", 7, 8)),
                "Gandyld", "enhance_crystal_alchemists"),
            "A Stronger Crystal": _part(Action(
                "visit", _p("/shattered_islands/strakewood_island/old_outpost/old_outpost_a_0204", 7, 17)),
                "Gandyld", "enhance_crystal_rhun"),
        }),
    "Lairwenn's Notes": QuestPolicy(
        "Lairwenn's Notes", _dialog("Lairwenn"), {
            "Finding the Notes": _part(Action(
                "container", _p("/shattered_islands/world_2_68_2", 6, 19),
                item="Lairwenn's Notes", object_name="luggage"),
                "Lairwenn", "get_notes"),
        }),
    "Melanye's Lost Walking Stick": QuestPolicy(
        "Melanye's Lost Walking Stick", _dialog("Melanye"), {
            "The Stick": _part(Action(
                "farm", _p("/shattered_islands/world_3_68", 10, 14),
                item="Melanye's Walking Stick", target="evil treant", patrol=((10, 14),)),
                "Melanye", "get_stick"),
        }),
    "The Mushroom Demon": QuestPolicy(
        "The Mushroom Demon", _dialog("Farmer Maggot"), {
            "The Demon": _part(Action(
                "kill", _p("/shattered_islands/world_2_47_-1", 13, 8),
                target="mushroom demon", patrol=((13, 8),)), "Farmer Maggot", "kill_demon"),
        }),
    "Portal of Llwyfen": QuestPolicy(
        "Portal of Llwyfen", Action(
            "visit", _p("/shattered_islands/strakewood_island/underground_city/underground_city_0_1", 22, 12)), {
            "A Strange Portal": _part(_dialog("Maplevale"), "Maplevale", "portal_found"),
            "Investigation of the Portal": _part(Action(
                "farm", _p("/shattered_islands/world_1_68_-1", 23, 8),
                item="Letter from Nyhelobo", target="oty captain", patrol=((23, 8),)),
                "Maplevale", "portal_investigate"),
            "Talthor Redeye": _part(_dialog("Talthor Redeye"), "Talthor Redeye", "speak_captain"),
            "The Attack": _part(Action(
                "kill", _p("/shattered_islands/strakewood_island/brynknot/sewers/lab_nyhelobo", 10, 4),
                target="Nyhelobo", patrol=((10, 4),)), "Talthor Redeye", "kill_boss"),
        }),
    "Rescuing Lynren": QuestPolicy(
        "Rescuing Lynren", _dialog("Lynren"), {
            "The Book": _part(Action(
                "container", _p("/shattered_islands/world_7_44", 13, 10), item="Lynren's book"),
                "Lynren", "rescue"),
        }),
    "Shipment of Charob Beer": QuestPolicy(
        "Shipment of Charob Beer", _dialog("Steve Bruck"), {
            "Deliver the Shipment": _part(Action(
                "container", _p("/shattered_islands/world_5_48", 7, 11), item="shipment of Charob Beer"),
                "Gashir", "deliver"),
            "The Reward": _part(_dialog("Steve Bruck"), "Steve Bruck", "reward"),
        }),
    "Two Lovers Doomed": QuestPolicy(
        "Two Lovers Doomed", _dialog("Tortwald Howell"), {
            "Tortwald's Letter": _part(_dialog("Rienn Howell"), "Rienn Howell", "deliver_tortwalds_letter"),
            "Rienn's Letter": _part(_dialog("Tortwald Howell"), "Tortwald Howell", "deliver_rienns_letter"),
        }),
}


# Main-story policy. The lethal ant route is deterministic; optional training
# is completed before departure when those parts remain active.
POLICIES["Lost Memories"] = QuestPolicy(
    "Lost Memories", _dialog("Sam Goodberry", place=NPC["Sam Goodberry (Incuna)"]), {
        # The heal response completes speak_priest and immediately renders the
        # first healed_body page without closing the interface. Keep one
        # ordered policy across that authored part boundary.
        "The Priest": _part(_dialog(
            "Brelend Lee", choices=(
                r"=:helprecover\]", r"=:storm\]", r"=:bumped\]",
                r"=:heal\]", r"=:headhealed\]", r"=:nodifferent\]",
                r"=:noremember\]",
            )), "Brelend Lee", "speak_priest"),
        "Healed Body...": _part(_dialog("Brelend Lee"), "Brelend Lee", "healed_body"),
        "... Broken Spirit": _part(_dialog("Sam Goodberry", place=NPC["Sam Goodberry (Incuna)"]), "Sam Goodberry", "broken_spirit"),
        "Gearing Up": _part(_dialog(
            "Ken Berger", goal_action="complete", goal_uid="gear",
            # Prefer the forward confirmation over the confirmation page's
            # `gearup` back-edge, while retaining the complete route for
            # whichever page is currently open after a resumed task.
            choices=(r"=:confirm_sword\]", r"=:sword\]", r"=:gearup\]"))),
        "The Arcane": _part(_dialog("John Aldman"), "John Aldman", "sorcery"),
        "Shooting Practice": _part(_dialog(
            "Tom Istrad", goal_action="complete", goal_uid="archery",
            choices=(r"=:confirm_bow\]", r"=:bow\]", r"=:archery\]"))),
        "Helping Out": _part(_dialog(
            "Arvend Blessed", goal_action="start", goal_uid="ant_trouble")),
        "Ant Trouble": _part(_dialog(
            "Arvend Blessed", goal_action="start", goal_uid="the_priestess")),
        "The Priestess": _part(_dialog(
            "Angela Sawthrow", goal_action="start", goal_uid="slay_queen",
            choices=(r"=:infestation\]", r"=:tellmore\]", r"=:tellslay\]",
                     r"=:sure\]"))),
        "The Ants": _part(Action(
            "farm", _p("/shattered_islands/world_4_85_-2", 7, 12),
            item="ant queen head", target="ant queen", patrol=((7, 12),)),
            "Arvend Blessed", "the_ants"),
        "To Slay A Queen": _part(Action(
            "farm", _p("/shattered_islands/world_4_85_-2", 7, 12),
            item="ant queen head", target="ant queen", patrol=((7, 12),)),
            "Arvend Blessed", "slay_queen"),
        "Making Friends": _part(Action(
            "apply", _p("/shattered_islands/world_4_85_-2", 2, 6),
            item="Angela's calming tonic")),
        "Report To Angela": _part(_dialog(
            "Angela Sawthrow", goal_action="complete", goal_uid="report_angela",
            choices=(r"=:resolved\]",))),
        "Report To Arvend": _part(_dialog("Arvend Blessed"), "Arvend Blessed", "report_arvend"),
        "Back To You, Sam": _part(_dialog("Sam Goodberry", place=NPC["Sam Goodberry (Incuna)"]), "Sam Goodberry", "report_sam"),
        "The Solution?": _part(_dialog("Brelend Lee"), "Brelend Lee", "the_solution"),
        "The Kobolds": _part(Action(
            "key_container", _p("/shattered_islands/world_3_81_-1", 19, 1),
            npc="Rick Grizley", item="Blue Crystal Talisman",
            choices=(r"=:out\]", r"=:brelend\]")),
            "Brelend Lee", "the_kobolds",
            objective_pattern="Blue Crystal Talisman"),
        "The Presence": _part(_dialog("Brelend Lee"), "Brelend Lee", "the_presence"),
        "The Journey": _part(_dialog("Sam Goodberry", place=NPC["Sam Goodberry (Incuna)"]), "Sam Goodberry", "the_journey"),
    }, priority=(
        "The Priest", "Healed Body...", "... Broken Spirit", "Gearing Up",
        "The Arcane", "Shooting Practice", "The Priestess",
        # The authored tonic route is safe for the intended level-1 character.
        # Killing the queen remains the fallback if the peaceful branch is no
        # longer active.
        "Making Friends", "Report To Angela", "Report To Arvend",
        "To Slay A Queen", "The Ants",
        "Ant Trouble", "Helping Out",
        "Back To You, Sam", "The Solution?", "The Kobolds",
        "The Presence", "The Journey",
    ))


class DialogAtTask(BotTask):
    def __init__(self, graph: WorldGraph, place: Place, npc: str,
                 choices: tuple[str, ...]):
        super().__init__(f"dialog-at:{npc}")
        self.graph = graph
        self.place = place
        self.navigation = NavigateTask(
            graph, place.map_path, (place.x, place.y), tolerance=1)
        self.dialog = DialogTask(npc, choices=choices)
        self._dialogue_prepared = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.navigation.status != TaskStatus.COMPLETE:
            try:
                await self.navigation.tick(client)
            except ValueError as exc:
                if (self.navigation.tolerance == 1 and
                        "no component route" in str(exc)):
                    self.navigation = NavigateTask(
                        self.graph, self.place.map_path,
                        (self.place.x, self.place.y), tolerance=2)
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder("dialog-approach-expand", self.dialog.npc)
                    return
                raise
            if self.navigation.status == TaskStatus.FAILED:
                if (self.navigation.tolerance == 1 and
                        "no component route" in self.navigation.error):
                    self.navigation = NavigateTask(
                        self.graph, self.place.map_path,
                        (self.place.x, self.place.y), tolerance=2)
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder("dialog-approach-expand", self.dialog.npc)
                else:
                    self.fail(self.navigation.error)
            return
        if not self._dialogue_prepared:
            await client.clear_actions()
            await client.set_combat(False)
            await client.clear_target()
            self._dialogue_prepared = True
            return
        await self.dialog.tick(client)
        if self.dialog.status == TaskStatus.FAILED:
            self.fail(self.dialog.error)
        elif self.dialog.status == TaskStatus.COMPLETE:
            self.complete()


class LostMemoriesArrivalTask(BotTask):
    """Finish the scripted Deserted Island voyage before normal routing.

    The ocean map is deliberately disconnected from the static world graph:
    Sam's dialog teleports the player below deck, and the gangway then leads
    onto Incuna. Treating it as an ordinary map route makes the navigator
    repeatedly walk into shipboard Sam instead of advancing the voyage.
    """

    OCEAN_MAP = "/shattered_islands/world_-6_79"
    LOWER_DECKS = (
        "/shattered_islands/incuna/ship_lower_deck",
        "/shattered_islands/incuna/ship_lower_deck_to_incuna",
    )
    INCUNA_ARRIVAL_MAP = "/shattered_islands/world_4_85"
    SHIP_SAM = _p(OCEAN_MAP, 4, 7)

    def __init__(self, graph: WorldGraph):
        super().__init__("voyage:deserted-island-to-incuna")
        self.graph = graph
        self.child: BotTask | None = None

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.phase != "playing":
            return
        if self.child is not None:
            await self.child.tick(client)
            if self.child.status == TaskStatus.FAILED:
                self.fail(self.child.error)
            elif self.child.status == TaskStatus.COMPLETE:
                self.child = None
            return

        current = client.state.map.path
        if current == self.OCEAN_MAP:
            # "Very well" is the authored choice to rest below deck. Its
            # script teleports directly to ship_lower_deck at (2, 2).
            self.child = DialogAtTask(
                self.graph, self.SHIP_SAM, "Sam Goodberry",
                (r"=:verywell\]",),
            )
            return
        if current in self.LOWER_DECKS:
            # Both stairs at x=6 lead through the Incuna gangway. Routing to
            # the destination map lets NavigateTask select a valid stair.
            self.child = NavigateTask(
                self.graph, self.INCUNA_ARRIVAL_MAP)
            return

        # Once off the scripted ship, the normal graph can route to Incuna
        # Sam and the ordinary Lost Memories start policy takes over.
        self.complete()


class ApplyAtTask(BotTask):
    def __init__(self, graph: WorldGraph, place: Place, item_pattern: str):
        super().__init__(f"apply-at:{item_pattern}")
        self.graph = graph
        self.place = place
        self.pattern = re.compile(item_pattern, re.I)
        # Area-triggered items should enter any reachable component first and
        # evaluate their authored proximity requirements there. Retain the
        # exact coordinate only as the fallback for ordinary apply actions.
        area_triggered = bool(re.search(
            r"calming tonic", self.pattern.pattern, re.I))
        self.navigation = NavigateTask(
            graph, place.map_path,
            None if area_triggered else (place.x, place.y))
        self.applied = False

    def _requirements_met(self, client: AtrinikClient) -> bool:
        """Recognize authored area-use requirements before a fixed fallback."""
        if (client.state.map.path != self.place.map_path or
                not re.search(r"calming tonic", self.pattern.pattern, re.I)):
            return False
        px, py = client.state.map.world_x, client.state.map.world_y
        nearby = [point for point in self.graph.landmarks
                  if point.map_path == self.place.map_path and
                  max(abs(point.x - px), abs(point.y - py)) <= 5]
        has_food = any(point.name.casefold() in ("straw", "grain")
                       for point in nearby)
        has_ant = any(
            "ant" in point.name.casefold() or
            point.archetype.casefold().startswith("ant_")
            for point in nearby)
        return has_food and has_ant

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if (self.navigation.status != TaskStatus.COMPLETE and
                not self._requirements_met(client)):
            await self.navigation.tick(client)
            if self.navigation.status == TaskStatus.FAILED:
                self.fail(self.navigation.error)
            return
        if self.applied:
            self.complete()
            return
        item = next((i for i in client.state.inventory if self.pattern.search(i.name)), None)
        if item is None:
            self.fail("required item is missing")
            return
        await client.apply(item.tag)
        self.applied = True


class AcquireContainerItemTask(BotTask):
    def __init__(self, graph: WorldGraph, place: Place, item_pattern: str,
                 container_pattern: str = "", required_quantity: int = 1):
        super().__init__(f"acquire:{item_pattern}")
        self.navigation = NavigateTask(graph, place.map_path, (place.x, place.y))
        self.pattern = re.compile(item_pattern, re.I)
        self.container_pattern = (re.compile(container_pattern, re.I)
                                  if container_pattern else None)
        self.required_quantity = required_quantity
        self.opened: set[int] = set()
        self.last_action = 0.0

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if sum(i.quantity for i in client.state.inventory
               if self.pattern.search(i.name)) >= self.required_quantity:
            self.complete()
            return
        if self.navigation.status != TaskStatus.COMPLETE:
            await self.navigation.tick(client)
            return
        if time.monotonic() - self.last_action < 0.5:
            return
        nested = next((i for i in client.state.items.values()
                       if i.location in self.opened and self.pattern.search(i.name)), None)
        if nested:
            await client.move_item(client.state.player_tag, nested.tag, nested.quantity)
            self.last_action = time.monotonic()
            return
        direct = next((i for i in client.state.ground if self.pattern.search(i.name)), None)
        if direct:
            await client.move_item(client.state.player_tag, direct.tag, direct.quantity)
            self.last_action = time.monotonic()
            return
        container_names = re.compile(
            r"\b(chest|box|luggage|cabinet|drawer|barrel|sack|bag|bookcase|bookshelf)\b",
            re.I)
        container = next((i for i in client.state.ground
                          if i.tag not in self.opened and
                          (i.item_type == c.TYPE_CONTAINER or
                           container_names.search(i.name)) and
                          (self.container_pattern is None or
                           self.container_pattern.search(i.name))), None)
        if container:
            await client.apply(container.tag)
            self.opened.add(container.tag)
            self.last_action = time.monotonic()
            return
        self.fail("quest container is not visible at the authored location")


class AcquireContainerItemsTask(BotTask):
    """Visit authored one-shot containers until an item quantity is met."""

    def __init__(self, graph: WorldGraph, action: Action):
        super().__init__(f"acquire:{action.quantity}:{action.item}")
        assert action.place is not None
        self.graph = graph
        self.action = action
        self.places = tuple(
            Place(action.place.map_path, x, y) for x, y in action.patrol)
        self.pattern = re.compile(action.item, re.I)
        self.index = 0
        self.child: AcquireContainerItemTask | None = None

    def _count(self, client: AtrinikClient) -> int:
        return sum(item.quantity for item in client.state.inventory
                   if self.pattern.search(item.name))

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        count = self._count(client)
        if count >= self.action.quantity:
            self.complete()
            return
        if self.child is not None:
            await self.child.tick(client)
            if self.child.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                self.child = None
                self.index += 1
            return
        if self.index >= len(self.places):
            self.fail(
                f"authored containers yielded {count}/{self.action.quantity} "
                f"{self.action.item}")
            return
        self.child = AcquireContainerItemTask(
            self.graph, self.places[self.index], self.action.item,
            self.action.object_name, required_quantity=count + 1)


class BuyItemTask(BotTask):
    def __init__(self, graph: WorldGraph, place: Place, npc: str, item: str):
        super().__init__(f"buy:{item}")
        self.navigation = NavigateTask(graph, place.map_path, (place.x, place.y), tolerance=2)
        self.npc, self.item = npc, item
        self.sent = False
        self.sent_at = 0.0

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if any(self.item.casefold() in i.name.casefold() for i in client.state.inventory):
            self.complete()
            return
        if self.navigation.status != TaskStatus.COMPLETE:
            await self.navigation.tick(client)
            return
        if not self.sent:
            await client.talk(f"buy 1 {self.item}", self.npc)
            self.sent = True
            self.sent_at = time.monotonic()
            return
        if time.monotonic() - self.sent_at < 2.0:
            return
        self.fail(f"could not buy {self.item}; check money and merchant stock")


class QuestKillTask(FarmTask):
    def __init__(self, quest: str, part: str, **kwargs):
        super().__init__(**kwargs)
        self.quest, self.part = quest, part
        self.last_request = 0.0

    async def tick(self, client: AtrinikClient) -> None:
        progress = client.state.quests.get(self.quest)
        live = next((p for p in (progress.parts if progress else [])
                     if p.name == self.part), None)
        if live is None or live.status in ("done", "completed") or (
                live.required is not None and live.current is not None and
                live.current >= live.required):
            self.complete()
            return
        if time.monotonic() - self.last_request > 2.0:
            await client.request_quests()
            self.last_request = time.monotonic()
        await super().tick(client)


class KeyThenContainerTask(BotTask):
    """Acquire an authored access key, then loot a specific quest chest."""

    TUNNEL_STAGING_MAP = "/shattered_islands/world_3_82_-1"
    TUNNEL_STAGING_XY = (13, 3)

    def __init__(self, graph: WorldGraph, action: Action):
        super().__init__(f"key-container:{action.item}")
        self.graph, self.action = graph, action
        self.child: BotTask | None = None
        self.acquiring = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.child is not None:
            await self.child.tick(client)
            if self.child.status == TaskStatus.FAILED:
                if (self.acquiring and client.state.map.path ==
                        "/shattered_islands/world_4_85"):
                    # Death returns the character to Incuna. Re-enter through
                    # the keyed gate and rebuild the northern approach instead
                    # of retrying the tunnel-only route from the bind point.
                    self.child = None
                    self.acquiring = False
                    return
                self.fail(self.child.error)
            elif self.child.status == TaskStatus.COMPLETE:
                if self.acquiring:
                    self.complete()
                else:
                    self.child = None
            return
        has_key = any("western gate key" in item.name.casefold()
                      for item in client.state.inventory)
        if not has_key:
            self.child = DialogAtTask(
                self.graph, NPC[self.action.npc], self.action.npc,
                self.action.choices)
            return
        assert self.action.place is not None
        try:
            self.graph.route_points(
                client.state.map.path,
                (client.state.map.world_x, client.state.map.world_y),
                self.action.place.map_path,
                [(self.action.place.x, self.action.place.y)],
                allow_locked=False,
            )
        except ValueError:
            # Incuna's north gate legitimately needs the key already in the
            # inventory, while the south shortcut is locked by the key the
            # chief drops. First cross the city gate and enter the tunnels
            # with owned keys enabled. From the stairs onward the quest chest
            # is reachable through the unlocked northern loop.
            self.child = NavigateTask(
                self.graph, self.TUNNEL_STAGING_MAP,
                self.TUNNEL_STAGING_XY, allow_locked=True)
            return
        acquire = AcquireContainerItemTask(
            self.graph, self.action.place, self.action.item)
        self.child = NavigateThenTask(
            self.graph, self.action.place.map_path, acquire,
            (self.action.place.x, self.action.place.y),
            # The chief only drops the south-shortcut key; the talisman is in
            # the chest at (19, 1). Clear threats encountered on the unlocked
            # northern route, but do not farm the chief as the objective.
            allow_locked=False,
            combat_approach=True)
        self.acquiring = True


class DependencyQuestTask(BotTask):
    """Finish another formal quest that supplies a required unique item."""

    def __init__(self, graph: WorldGraph, quest_name: str, item_pattern: str,
                 catalog: dict[str, QuestDefinition]):
        super().__init__(f"dependency:{quest_name}")
        self.pattern = re.compile(item_pattern, re.I)
        self.child = CatalogQuestTask(graph, POLICIES[quest_name], catalog)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if any(self.pattern.search(item.name) for item in client.state.inventory):
            self.complete()
            return
        await self.child.tick(client)
        if self.child.status == TaskStatus.FAILED:
            self.fail(self.child.error)
        elif self.child.status == TaskStatus.COMPLETE:
            self.fail("dependency is complete but its unique reward item is missing")


class CatalogQuestTask(BotTask):
    """Resume one compiled formal quest from its live per-character state."""

    def __init__(self, graph: WorldGraph, policy: QuestPolicy,
                 catalog: dict[str, QuestDefinition] | None = None, *,
                 stop_after_start: bool = False,
                 start_reward_pattern: str = ""):
        super().__init__(f"quest:{policy.name}")
        self.graph, self.policy = graph, policy
        self.catalog = catalog or load_catalog()
        self.definition = self.catalog[policy.name]
        self.parts = {part.name: part for part in flatten_parts(self.definition.parts)}
        self.stop_after_start = bool(stop_after_start)
        self.start_reward_pattern = re.compile(
            start_reward_pattern, re.I) if start_reward_pattern else None
        self.child: BotTask | None = None
        self.last_request = 0.0

    def _active_part(self, client: AtrinikClient) -> str:
        quest = client.state.quests.get(self.policy.name)
        active = [part.name for part in (quest.parts if quest else [])
                  if part.status not in ("done", "completed", "failed")]
        for name in self.policy.priority:
            if name in active:
                return name
        return active[-1] if active else ""

    @staticmethod
    def _has_objective(client: AtrinikClient, objective: QuestObjective,
                       pattern_override: str = "") -> bool:
        if objective.kind != "item":
            return False
        pattern = re.compile(pattern_override or objective.name or
                             objective.archetype.replace("_", " "), re.I)
        return sum(i.quantity for i in client.state.inventory
                   if pattern.search(i.name)) >= objective.quantity

    def _choices(self, npc: str, *, action: str = "", uid: str = "",
                 spec: Action | None = None) -> tuple[str, ...]:
        if spec and spec.choices:
            return spec.choices
        return dialogue_choices(
            self.definition, npc, action=action, uid=uid,
            object_arch=(spec.object_arch if spec else ""),
            object_name=(spec.object_name if spec else ""),
        )

    def _dialog_task(self, spec: Action, *, action: str = "", uid: str = "") -> BotTask:
        assert spec.place is not None
        return DialogAtTask(self.graph, spec.place, spec.npc,
                            self._choices(spec.npc, action=action, uid=uid, spec=spec))

    def _action_task(self, action: Action, part_name: str) -> BotTask:
        if action.kind == "dialog":
            part = self.parts.get(part_name)
            uid = part.uid if part else ""
            goal = action.goal_action or (
                "start" if action.object_arch or action.object_name else "complete")
            return self._dialog_task(
                action, action=goal, uid=action.goal_uid or uid)
        assert action.place is not None
        if action.kind == "visit":
            return NavigateTask(self.graph, action.place.map_path,
                                (action.place.x, action.place.y))
        if action.kind == "apply":
            return ApplyAtTask(self.graph, action.place, action.item)
        if action.kind == "container":
            return AcquireContainerItemTask(
                self.graph, action.place, action.item, action.object_name,
                action.quantity)
        if action.kind == "container_sweep":
            return AcquireContainerItemsTask(self.graph, action)
        if action.kind == "buy":
            # The clear-crystal trader has a separate authored landmark.
            trader = NPC.get(action.npc, _p("/shattered_islands/world_6_51", 15, 16))
            return BuyItemTask(self.graph, trader, action.npc, action.item)
        if action.kind == "dependency":
            return DependencyQuestTask(
                self.graph, action.item, action.target, self.catalog)
        if action.kind == "key_container":
            return KeyThenContainerTask(self.graph, action)
        if action.kind == "farm":
            task = FarmTask(
                zone=action.place.map_path, target=action.target, item=action.item,
                quantity=action.quantity, patrol=list(action.patrol),
                loot=LootPolicy(take_all=False, include=(action.item,)),
                priority_spawns=[(
                    action.place.x, action.place.y, action.target)],
            ) if action.item else QuestKillTask(
                self.policy.name, part_name, zone=action.place.map_path,
                target=action.target, patrol=list(action.patrol),
                loot=LootPolicy(take_all=True),
                priority_spawns=[(
                    action.place.x, action.place.y, action.target)],
            )
            return NavigateThenTask(
                self.graph, action.place.map_path, task,
                combat_approach=True)
        if action.kind == "kill":
            task = QuestKillTask(
                self.policy.name, part_name, zone=action.place.map_path,
                target=action.target, patrol=list(action.patrol),
                loot=LootPolicy(take_all=True),
                priority_spawns=[(
                    action.place.x, action.place.y, action.target)],
            )
            return NavigateThenTask(
                self.graph, action.place.map_path, task,
                combat_approach=True)
        raise ValueError(f"unknown quest action kind {action.kind}")

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.phase != "playing":
            return
        if not client.state.quests_loaded:
            if time.monotonic() - self.last_request > 1.0:
                await client.request_quests()
                self.last_request = time.monotonic()
            return
        quest = client.state.quests.get(self.policy.name)
        if quest and quest.status == "completed":
            self.complete()
            return
        # Some opening conversations grant a valuable reward while the
        # remaining objective deliberately points into a much harder area.
        # Autoplay can collect that safe reward without allowing this generic
        # executor to continue into the newly started quest part.
        if quest is not None and self.stop_after_start:
            if self.start_reward_pattern is not None:
                interface = getattr(client.state, "interface", None)
                offered = (
                    interface.objects if interface is not None else [])
                if not any(
                        self.start_reward_pattern.search(item.name)
                        for item in (*client.state.inventory, *offered)):
                    return
            self.complete()
            return
        if self.child is not None:
            await self.child.tick(client)
            if self.child.status == TaskStatus.FAILED:
                self.fail(f"{self.child.name}: {self.child.error}")
                return
            if self.child.status != TaskStatus.COMPLETE:
                return
            await client.clear_actions()
            await client.set_combat(False)
            await client.clear_target()
            self.child = None
            await client.request_quests()
            self.last_request = time.monotonic()
            return
        if time.monotonic() - self.last_request > 3.0:
            await client.request_quests()
            self.last_request = time.monotonic()
        if quest is None:
            if (self.policy.name == "Lost Memories" and
                    client.state.map.path in (
                        LostMemoriesArrivalTask.OCEAN_MAP,
                        *LostMemoriesArrivalTask.LOWER_DECKS,
                    )):
                self.child = LostMemoriesArrivalTask(self.graph)
                return
            first = flatten_parts(self.definition.parts)[0]
            if self.policy.start.kind == "dialog":
                self.child = self._dialog_task(
                    self.policy.start, action="start", uid=first.uid)
            else:
                self.child = self._action_task(self.policy.start, first.name)
            return
        part_name = self._active_part(client)
        if not part_name:
            return
        policy = self.policy.parts.get(part_name)
        if policy is None:
            self.fail(f"no route policy for active part {part_name!r}")
            return
        definition = self.parts.get(part_name)
        objectives = definition.objectives if definition else []
        live = next((p for p in quest.parts if p.name == part_name), None)
        satisfied = bool(objectives) and all(
            self._has_objective(client, objective, policy.objective_pattern)
            if objective.kind == "item"
            else bool(live and live.current is not None and live.required is not None
                      and live.current >= live.required)
            for objective in objectives
        )
        if objectives and not satisfied:
            self.child = self._action_task(policy.action, part_name)
            return
        if not objectives and (policy.action.kind != "dialog" or
                               policy.action.goal_action):
            self.child = self._action_task(policy.action, part_name)
            return
        npc = policy.turnin_npc or policy.action.npc
        place = policy.turnin_place or policy.action.place
        if not npc or place is None:
            self.child = self._action_task(policy.action, part_name)
            return
        uid = policy.turnin_uid or (definition.uid if definition else "")
        spec = Action("dialog", place, npc)
        self.child = DialogAtTask(
            self.graph, place, npc,
            policy.turnin_choices or
            policy.action.choices or
            self._choices(npc, action="complete", uid=uid, spec=spec),
        )


RECOMMENDED_QUEST_ORDER = (
    "Escaping the Deserted Island", "Lost Memories", "Lairwenn's Notes",
    "Melanye's Lost Walking Stick", "Shipment of Charob Beer",
    "Frevia's Tomboyish Fairy", "Crazymix's Alchemical Reagents",
    "Fort Sether Illness", "Clearhaven Mine", "Galann's Revenge",
    "Gandyld's Mana Crystal",
    "Construction of Telescope", "The Mushroom Demon", "Two Lovers Doomed",
    "Rescuing Lynren", "Portal of Llwyfen",
)


class AllFormalQuestsTask(BotTask):
    """Complete all formal quests, resuming from live character state."""

    def __init__(self, graph: WorldGraph,
                 catalog: dict[str, QuestDefinition] | None = None):
        super().__init__("quests:all-formal")
        self.graph = graph
        self.catalog = catalog or load_catalog()
        self.index = 0
        self.child: BotTask | None = None

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.phase != "playing":
            return
        if not client.state.quests_loaded:
            await client.request_quests()
            return
        while self.index < len(RECOMMENDED_QUEST_ORDER):
            name = RECOMMENDED_QUEST_ORDER[self.index]
            quest = client.state.quests.get(name)
            if quest and quest.status == "completed":
                self.index += 1
                continue
            break
        if self.index >= len(RECOMMENDED_QUEST_ORDER):
            self.complete()
            return
        if self.child is None:
            name = RECOMMENDED_QUEST_ORDER[self.index]
            self.child = (EscapingDesertedIslandTask(self.graph)
                          if name == "Escaping the Deserted Island" else
                          CatalogQuestTask(
                              self.graph, POLICIES[name], self.catalog))
        await self.child.tick(client)
        if self.child.status == TaskStatus.FAILED:
            self.fail(f"{self.child.name}: {self.child.error}")
        elif self.child.status == TaskStatus.COMPLETE:
            self.child = None
            self.index += 1
            await client.request_quests()
