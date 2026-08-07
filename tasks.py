"""Composable autonomous tasks and safety/economy policies."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

try:
    from tools.world_content_audit import load_archetypes
except ModuleNotFoundError:
    from world_content_audit import load_archetypes

from . import constants as c
from .client import (COIN_VALUES, AtrinikClient, map_object_visual_name,
                     update_bank_balance)
from .model import Item, MapObject
from .pathfinding import grid_search

log = logging.getLogger(__name__)


def retreat_mobility(node, point: tuple[int, int], *, blocked=(),
                     allow_locked=False, depth: int = 4) -> tuple[int, int]:
    """Measure forward escape depth/area without stepping back through danger."""
    if node is None or not getattr(node, "terrain", None):
        return depth, 8
    excluded = set(blocked) | set(getattr(node, "occupied", ()))
    excluded.discard(point)
    queue = deque(((point, 0),))
    seen = {point}
    furthest = 0
    while queue:
        current, distance = queue.popleft()
        furthest = max(furthest, distance)
        if distance >= depth:
            continue
        for dx, dy in c.DIRECTION_DELTAS.values():
            nxt = current[0] + dx, current[1] + dy
            if (nxt in seen or nxt in excluded or
                    not node.walkable(*nxt, allow_locked=allow_locked)):
                continue
            seen.add(nxt)
            queue.append((nxt, distance + 1))
    return furthest, len(seen) - 1


@dataclass(slots=True, frozen=True)
class EquipmentPrototype:
    item_type: int
    item_skill: int
    skill_name: str
    item_level: int
    base_score: int


@lru_cache(maxsize=1)
def equipment_face_catalog() -> dict[str, tuple[EquipmentPrototype, ...]]:
    """Index server equipment metadata by the face visible on ground items."""
    equipment_types = {
        c.TYPE_WEAPON, c.TYPE_BOW, c.TYPE_ARMOUR, c.TYPE_SHIELD,
        c.TYPE_HELMET, c.TYPE_PANTS, c.TYPE_AMULET, c.TYPE_RING,
        c.TYPE_LIGHT_APPLY, c.TYPE_CLOAK, c.TYPE_BOOTS,
        c.TYPE_GLOVES, c.TYPE_BRACERS,
    }
    skill_names = {
        13: "bow archery", 14: "crossbow archery",
        15: "sling archery", 16: "slash weapons",
        17: "cleave weapons", 18: "pierce weapons",
        19: "impact weapons", 20: "two-hand mastery",
        21: "polearm mastery",
    }
    catalog: dict[str, list[EquipmentPrototype]] = {}
    for record in load_archetypes().values():
        attrs = record.get("attrs", {})
        try:
            item_type = int(attrs.get("type", 0) or 0)
        except (TypeError, ValueError):
            continue
        face = str(attrs.get("face", "") or "").casefold()
        if item_type not in equipment_types or not face:
            continue

        def number(name: str) -> int:
            try:
                return int(attrs.get(name, 0) or 0)
            except (TypeError, ValueError):
                return 0

        item_skill = number("item_skill")
        protections = sum(number(name) for name in attrs
                          if name.startswith("protect_"))
        # Compare weapons by damage throughput, not damage per hit alone.
        # last_grace is the server attack delay in eighths of a second; 16 is
        # the starter shortsword's 2.0-second baseline. This preserves the old
        # score at that speed while correctly preferring faster weapons.
        damage_score = number("dam") * 20
        if item_type == c.TYPE_WEAPON and number("last_grace") > 0:
            damage_score = (
                number("dam") * 320 // number("last_grace"))
        base_score = (
            damage_score + number("wc") * 10 +
            number("ac") * 20 + number("block") * 2 +
            number("absorb") * 2 + protections
        )
        if item_type == c.TYPE_LIGHT_APPLY:
            base_score += number("last_sp") * 30 + number("food") // 10
        prototype = EquipmentPrototype(
            item_type, item_skill, skill_names.get(item_skill, ""),
            number("item_level"), base_score)
        values = catalog.setdefault(face, [])
        if prototype not in values:
            values.append(prototype)
    return {face: tuple(values) for face, values in catalog.items()}


def map_object_semantic(client: AtrinikClient, obj) -> str:
    """Return protocol and human-readable aliases for a visible object."""
    visual = map_object_visual_name(client, obj)

    def aliases(value: str) -> str:
        human = value.replace("_", " ")
        return f"{value} {human} {chr(32).join(reversed(human.split()))}"

    return f"{aliases(obj.name)} {aliases(visual)}"


def recent_hostile_contact(client: AtrinikClient, seconds: float = 5.0) -> bool:
    """Whether combat messages prove an enemy is still acting on the player."""
    cutoff = time.time() - seconds
    for timestamp, _, _, raw in reversed(
            getattr(client.state, "messages", ())):
        if timestamp < cutoff:
            break
        text = raw.casefold()
        if (" hit you" in text or " misses you" in text or
                text.startswith(("you block ", "you dodge ", "you parry "))):
            return True
    return False


def unresolved_hostile_contact(client: AtrinikClient,
                               seconds: float = 5.0) -> bool:
    """Whether the newest encounter evidence is hostile contact, not a kill."""
    cutoff = time.time() - seconds
    for timestamp, _, _, raw in reversed(
            getattr(client.state, "messages", ())):
        if timestamp < cutoff:
            break
        text = raw.casefold()
        if (" hit you" in text or " misses you" in text or
                text.startswith(("you block ", "you dodge ", "you parry "))):
            return True
        if text.startswith("you killed "):
            # A newer hit would already have returned above. The terminal
            # message therefore resolves the older contact which belonged to
            # the just-killed target; do not flee from it while pursuing loot.
            return False
    return False


def _same_semantic_identity(first: str, second: str) -> bool:
    """Match wire/archetype aliases even when adjective order is reversed."""
    def normalize(value: str) -> str:
        value = value.casefold().replace("_", " ").replace("-", " ")
        return re.sub(r"\s+", " ", re.sub(r"\.\d+$", "", value)).strip()

    left, right = normalize(first), normalize(second)
    if not left or not right:
        return False
    return left in right or right in left or sorted(left.split()) == sorted(
        right.split())


def recent_hostile_attackers(client: AtrinikClient,
                             seconds: float = 10.0) -> set[str]:
    """Names of creatures whose combat text proves they attacked recently."""
    cutoff = time.time() - seconds
    attackers = set()
    for timestamp, _, _, raw in reversed(
            getattr(client.state, "messages", ())):
        if timestamp < cutoff:
            break
        text = raw.casefold()
        match = re.match(r"(.+?) (?:hit|misses) you\b", text)
        if match:
            attackers.add(match.group(1).strip())
    return attackers


def recent_player_kill(client: AtrinikClient, seconds: float = 2.5) -> bool:
    """Whether combat text confirms that the selected target actually died."""
    cutoff = time.time() - seconds
    for timestamp, _, _, raw in reversed(
            getattr(client.state, "messages", ())):
        if timestamp < cutoff:
            break
        if raw.casefold().startswith("you killed "):
            return True
    return False


def effective_action_time(client: AtrinikClient,
                          now: float | None = None) -> float:
    """Return the server timer after locally applying its elapsed countdown."""
    value = max(0.0, float(
        client.state.stats.get("action_time", 0) or 0))
    observed_at = getattr(
        client.state, "stat_observed_at", {}).get("action_time", 0.0)
    if observed_at:
        now = time.monotonic() if now is None else now
        value = max(0.0, value - (now - observed_at))
    return value


def movement_ack_timeout(client: AtrinikClient) -> float:
    """Wait for an authoritative position update before retrying one step."""
    stats = client.state.stats
    action_time = effective_action_time(client)
    speed = float(stats.get("speed", 0) or 0)
    speed_interval = 1.0 / speed if speed > 0 else 0.0
    return max(1.5, action_time + 0.75, speed_interval + 0.50)


class TaskStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass(slots=True)
class SafetyPolicy:
    heal_below: float = 0.70
    flee_below: float = 0.55
    min_food: int = 100
    healing_patterns: tuple[str, ...] = ("healing", "cure wounds")
    food_patterns: tuple[str, ...] = ("food", "bread", "mushroom", "apple")
    heal_cooldown: float = 1.15
    food_retry_seconds: float = 3.0
    _last_heal: float = field(default=0.0, init=False, repr=False)
    _last_food: float = field(default=0.0, init=False, repr=False)
    _food_attempt_level: int = field(default=-1, init=False, repr=False)
    _last_warning: float = field(default=0.0, init=False, repr=False)

    @staticmethod
    def _find(items: list[Item], patterns: tuple[str, ...]) -> Item | None:
        for item in items:
            if any(pattern in item.name.casefold() for pattern in patterns):
                return item
        return None

    def _food(self, items: list[Item]) -> Item | None:
        """Prefer familiar staples, then any server-typed paid food."""
        safe = [
            item for item in items
            if not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)
        ]
        familiar = self._find(safe, self.food_patterns)
        if familiar is not None:
            return familiar
        return next((item for item in safe
                     if item.item_type == c.TYPE_FOOD), None)

    async def _heal(self, client: AtrinikClient) -> bool:
        heal = self._find(client.state.inventory, self.healing_patterns)
        if heal is None:
            return False
        if heal.item_type == c.TYPE_SPELL:
            cost = int(heal.extra.get("cost", 0) or 0)
            if cost > int(client.state.stats.get("sp", 0) or 0):
                return False
            # The official client's `/cast <spell>` sends FIRE direction 0
            # with the spell object's tag. Applying a spell merely readies or
            # unreadies it and was the reason Sera never recovered health.
            await client.fire(0, heal.tag)
        else:
            await client.apply(heal.tag)
        return True

    async def enforce(self, client: AtrinikClient) -> bool:
        stats = client.state.stats
        hp, maxhp = stats.get("hp", 0), stats.get("maxhp", 0)
        # Login marks the character playing just before the first stats
        # packet. Zero maximum HP means state is not ready, not that the
        # character is dying; issuing Clear Actions here can cancel the first
        # deliberate task command.
        if maxhp <= 0:
            return True
        ratio = hp / maxhp
        if ratio <= self.flee_below:
            now = time.monotonic()
            if now - self._last_heal >= self.heal_cooldown:
                await client.clear_actions()
                if await self._heal(client):
                    self._last_heal = now
            # Keep combat enabled while healing. Turning it off without a
            # real retreat left surrounded characters defenseless between
            # spell cooldowns.
            if now - self._last_warning >= 2.0:
                log.warning("safety heal: health %.0f%%", ratio * 100)
                self._last_warning = now
            return False
        if ratio <= self.heal_below:
            now = time.monotonic()
            if now - self._last_heal < self.heal_cooldown:
                return False
            # Cancel navigation before firing, so Clear Actions cannot cancel
            # the healing spell which follows it in the same tick.
            await client.clear_actions()
            if await self._heal(client):
                self._last_heal = now
                # Cancel a queued click-to-move path while the healing cast is
                # processed. Otherwise route following can immediately carry
                # a low-health character deeper into the same hostile pack.
                return False
            if self._find(client.state.inventory,
                          self.healing_patterns) is not None:
                # A known healing spell with temporarily insufficient SP is a
                # reason to wait for regeneration, not permission to walk
                # deeper into the next encounter below the recovery margin.
                return False
        food_level = int(stats.get("food", 1000) or 0)
        if food_level >= self.min_food:
            self._food_attempt_level = -1
        if food_level < self.min_food:
            now = time.monotonic()
            if (self._food_attempt_level == food_level and
                    now - self._last_food < self.food_retry_seconds):
                # The application is already in flight. Do not make travel
                # treat this acknowledgment window as an emergency and clear
                # the very action whose stat update we are awaiting.
                return True
            food = self._food(client.state.inventory)
            if food:
                await client.apply(food.tag)
                self._last_food = now
                self._food_attempt_level = food_level
                return False
        return True


class BotTask:
    def __init__(self, name: str):
        self.name = name
        self.status = TaskStatus.READY
        self.error = ""
        self.started_at = 0.0

    async def start(self, client: AtrinikClient) -> None:
        self.status = TaskStatus.RUNNING
        self.started_at = time.time()

    async def tick(self, client: AtrinikClient) -> None:
        raise NotImplementedError

    def complete(self) -> None:
        self.status = TaskStatus.COMPLETE

    def fail(self, reason: str) -> None:
        self.error = reason
        self.status = TaskStatus.FAILED


@dataclass(slots=True)
class LootPolicy:
    take_all: bool = True
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ("corpse",)

    def wants(self, item: Item) -> bool:
        name = item.name.casefold()
        if any(re.search(pattern, name) for pattern in self.exclude):
            return False
        return self.take_all or any(re.search(pattern, name) for pattern in self.include)


@dataclass(slots=True)
class InventoryPolicy:
    """Conservative local valuation used before equip/sell/deposit decisions."""

    reserve_words: tuple[str, ...] = (
        "quest", "key", "talisman", "artifact", "unique", "exceptional",
        "ring", "amulet", "spellbook", "formula", "recipe",
    )
    equipment_types: tuple[int, ...] = (
        c.TYPE_WEAPON, c.TYPE_BOW, c.TYPE_ARMOUR, c.TYPE_SHIELD,
        c.TYPE_HELMET, c.TYPE_PANTS, c.TYPE_AMULET, c.TYPE_RING,
        c.TYPE_LIGHT_APPLY, c.TYPE_CLOAK, c.TYPE_BOOTS,
        c.TYPE_GLOVES, c.TYPE_BRACERS,
    )
    equipment_slots: dict[int, tuple[int, ...]] = field(default_factory=lambda: {
        c.TYPE_WEAPON: (c.EQUIP_WEAPON,),
        c.TYPE_BOW: (c.EQUIP_WEAPON_RANGED,),
        c.TYPE_ARMOUR: (c.EQUIP_ARMOUR,),
        c.TYPE_SHIELD: (c.EQUIP_SHIELD,),
        c.TYPE_HELMET: (c.EQUIP_HELM,),
        c.TYPE_PANTS: (c.EQUIP_PANTS,),
        c.TYPE_AMULET: (c.EQUIP_AMULET,),
        c.TYPE_RING: (c.EQUIP_RING_RIGHT, c.EQUIP_RING_LEFT),
        c.TYPE_LIGHT_APPLY: (c.EQUIP_LIGHT,),
        c.TYPE_CLOAK: (c.EQUIP_CLOAK,),
        c.TYPE_BOOTS: (c.EQUIP_BOOTS,),
        c.TYPE_GLOVES: (c.EQUIP_GAUNTLETS,),
        c.TYPE_BRACERS: (c.EQUIP_BRACERS,),
    })

    identification_types: tuple[int, ...] = (
        c.TYPE_RING, c.TYPE_WAND, c.TYPE_ROD, c.TYPE_SCROLL, c.TYPE_FOOD,
        c.TYPE_POTION, c.TYPE_BOW, c.TYPE_ARROW, c.TYPE_WEAPON,
        c.TYPE_ARMOUR, c.TYPE_SHIELD, c.TYPE_HELMET, c.TYPE_PANTS,
        c.TYPE_AMULET, c.TYPE_BOOTS, c.TYPE_GLOVES, c.TYPE_BRACERS,
        c.TYPE_GIRDLE, c.TYPE_CONTAINER, c.TYPE_DRINK, c.TYPE_FLESH,
        c.TYPE_INORGANIC, c.TYPE_CLOAK, c.TYPE_GEM, c.TYPE_JEWEL,
        c.TYPE_NUGGET, c.TYPE_PEARL, c.TYPE_POWER_CRYSTAL, c.TYPE_BOOK,
        c.TYPE_LIGHT_APPLY, c.TYPE_LIGHT_REFILL, c.TYPE_SPELLBOOK,
        c.TYPE_TRINKET, c.TYPE_PAINTING,
    )

    stackable_types: tuple[int, ...] = (
        c.TYPE_POTION, c.TYPE_FOOD, c.TYPE_ARROW, c.TYPE_MONEY,
        c.TYPE_GEM, c.TYPE_LIGHT_APPLY, c.TYPE_SCROLL,
    )

    def needs_identification(self, item: Item) -> bool:
        return item.item_type in self.identification_types

    def identified(self, item: Item) -> bool:
        # The inventory protocol sends quality=255 and omits
        # condition/requirements
        # whenever FLAG_IDENTIFIED is absent. The official client uses the
        # same sentinel while parsing an inventory item.
        return not self.needs_identification(item) or item.quality != 255

    def apartment_unidentified(self, item: Item) -> bool:
        """Return an unknown item worth interrupting a farm to preserve."""
        # Ordinary food/flesh and ammunition commonly omit identification
        # metadata. They are useful supplies, not trophies worth a cross-world
        # apartment trip; carrying pressure can still trigger a later batch.
        return (
            self.needs_identification(item) and
            not self.identified(item) and
            item.item_type not in (
                c.TYPE_FOOD, c.TYPE_DRINK, c.TYPE_FLESH, c.TYPE_ARROW)
        )
    stackable_words: tuple[str, ...] = (
        "coin", "arrow", "bolt", "torch", "potion", "scroll",
        "gem", "diamond", "ruby", "sapphire", "emerald", "ore",
        "reagent", "ingredient",
    )

    def preserve(self, item: Item) -> bool:
        name = item.name.casefold()
        return (
            not self.identified(item) or
            item.item_type in (c.TYPE_SPELL, c.TYPE_SKILL, c.TYPE_SPELLBOOK) or
            item.item_type in (c.TYPE_RING, c.TYPE_AMULET, c.TYPE_GEM) or
            bool(item.flags & c.ITEM_MAGICAL) or
            any(word in name for word in self.reserve_words) or
            item.quality != 255 and item.quality >= 90
        )

    def stackable(self, item: Item) -> bool:
        """Return whether locking this item could split mergeable stacks."""
        name = item.name.casefold()
        return (
            item.quantity > 1 or
            item.item_type in self.stackable_types or
            any(re.search(rf"\b{re.escape(word)}(?:s|es)?\b", name)
                for word in self.stackable_words)
        )

    def should_lock(self, item: Item) -> bool:
        """Protect valuable discrete objects without locking stackables."""
        return (
            self.preserve(item) and
            not self.stackable(item) and
            item.item_type not in (c.TYPE_SPELL, c.TYPE_SKILL)
        )

    def apartment_valuable(self, item: Item) -> bool:
        """Return a safely storable identified trophy or rare item."""
        name = item.name.casefold()
        if (not self.identified(item) or self.stackable(item) or
                item.flags & (c.ITEM_APPLIED | c.ITEM_UNPAID) or
                item.item_type in (c.TYPE_SPELL, c.TYPE_SKILL, c.TYPE_MONEY) or
                # Power crystals are renewable combat-mana reserves, not
                # inert trophies. Keep them carried even when magical/rare.
                item.item_type == c.TYPE_POWER_CRYSTAL or
                any(word in name for word in
                    ("key", "quest", "talisman", "spellbook"))):
            return False
        valuable_types = (*self.equipment_types, c.TYPE_GEM, c.TYPE_JEWEL,
                          c.TYPE_PEARL, c.TYPE_POWER_CRYSTAL,
                          c.TYPE_TRINKET, c.TYPE_PAINTING)
        return bool(
            item.item_type in valuable_types and (
                item.flags & c.ITEM_MAGICAL or item.quality >= 90) or
            re.search(
                r"ring of (?:the )?ghost|"
                r"bow of accuracy", name))

    def gear_score(self, item: Item) -> int:
        if item.item_type not in self.equipment_types:
            return -1
        quality = 50 if item.quality == 255 else item.quality
        condition = 100 if item.condition == 255 else item.condition
        named_bonus = sum(
            int(value) for value in re.findall(r"[+]([0-9]+)", item.name))
        return (quality * 2 + condition + named_bonus * 8 +
                (120 if item.flags & c.ITEM_MAGICAL else 0))

    def weight_ratio(self, client: AtrinikClient) -> float:
        limit = float(client.state.stats.get("weight_limit", 0) or 0)
        if limit <= 0:
            return 0.0
        carried = sum(item.weight * item.quantity
                      for item in client.state.inventory)
        return carried / limit


class InventoryCapabilityTask(BotTask):
    """Use a known spell or identified consumable/device for maintenance."""

    PURPOSE_IDENTIFY = "identify"
    PURPOSE_DISEASE = "disease"
    PURPOSE_DEPLETION = "depletion"
    TIMEOUT_SECONDS = 8.0

    def __init__(self, purpose: str):
        if purpose not in (
                self.PURPOSE_IDENTIFY, self.PURPOSE_DISEASE,
                self.PURPOSE_DEPLETION):
            raise ValueError(f"unknown inventory capability {purpose!r}")
        super().__init__(f"inventory-capability:{purpose}")
        self.purpose = purpose
        self._source_tag = 0
        self._source_type = 0
        self._previous_ranged_tag = 0
        self._unknown_tags: set[int] = set()
        self._feedback_start = 0
        self._sent_at = 0.0
        self._device_apply_at = 0.0
        self._restore_at = 0.0
        self._result: tuple[bool, str] | None = None

    @staticmethod
    def _safe_item(item: Item) -> bool:
        return not item.flags & (
            c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)

    @classmethod
    def candidates(cls, client: AtrinikClient, purpose: str) -> list[Item]:
        """Return usable sources in renewable/cheap-first order."""
        policy = InventoryPolicy()
        spell_names = {
            cls.PURPOSE_IDENTIFY: ("identify",),
            cls.PURPOSE_DISEASE: ("cure disease", "restoration"),
            cls.PURPOSE_DEPLETION: ("remove depletion",),
        }[purpose]
        item_names = {
            cls.PURPOSE_IDENTIFY: ("identify",),
            cls.PURPOSE_DISEASE: ("cure illness", "cure disease"),
            cls.PURPOSE_DEPLETION: ("remove depletion",),
        }[purpose]
        sp = int(client.state.stats.get("sp", 0) or 0)
        spells = [
            item for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL and
            item.name.casefold() in spell_names and
            int(item.extra.get("cost", 0) or 0) <= sp
        ]
        devices = [
            item for item in client.state.inventory
            if item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
            policy.identified(item) and cls._safe_item(item) and
            any(name in item.name.casefold() for name in item_names)
        ]
        consumables = [
            item for item in client.state.inventory
            if item.item_type in (c.TYPE_SCROLL, c.TYPE_POTION) and
            policy.identified(item) and cls._safe_item(item) and
            any(name in item.name.casefold() for name in item_names)
        ]
        # Spells cost only regenerating SP; rods regenerate charges; wands and
        # finally one-shot consumables follow in that order.
        devices.sort(key=lambda item: item.item_type != c.TYPE_ROD)
        consumables.sort(key=lambda item: item.item_type != c.TYPE_SCROLL)
        return spells + devices + consumables

    def _remaining_unknown(self, client: AtrinikClient) -> set[int]:
        policy = InventoryPolicy()
        return {
            tag for tag in self._unknown_tags
            if tag in client.state.items and
            not policy.identified(client.state.items[tag])
        }

    def _feedback(self, client: AtrinikClient) -> str:
        return "\n".join(
            entry[3].casefold()
            for entry in client.state.messages[self._feedback_start:]
            if len(entry) > 3)

    def _succeeded(self, client: AtrinikClient) -> bool:
        if self.purpose == self.PURPOSE_IDENTIFY:
            return bool(self._unknown_tags - self._remaining_unknown(client))
        if self.purpose == self.PURPOSE_DEPLETION:
            return not any(
                item.item_type == c.TYPE_FORCE and
                item.name.casefold() == "depletion"
                for item in client.state.inventory)
        return "you are healed from disease" in self._feedback(client)

    async def _finish_after_device_restore(
            self, client: AtrinikClient, success: bool, reason: str) -> None:
        source = client.state.items.get(self._source_tag)
        if source is not None and source.flags & c.ITEM_APPLIED:
            restore = client.state.items.get(self._previous_ranged_tag)
            await client.apply(restore.tag if restore is not None else source.tag)
            self._restore_at = time.monotonic()
            self._result = success, reason
            return
        if success:
            self.complete()
        else:
            self.fail(reason)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
            self._feedback_start = len(client.state.messages)
            policy = InventoryPolicy()
            self._unknown_tags = {
                item.tag for item in client.state.inventory
                if self._safe_item(item) and not policy.identified(item)
            }
        if self._result is not None:
            source = client.state.items.get(self._source_tag)
            restore = client.state.items.get(self._previous_ranged_tag)
            restored = (
                restore is not None and restore.flags & c.ITEM_APPLIED
            ) if self._previous_ranged_tag else (
                source is None or not source.flags & c.ITEM_APPLIED)
            if restored or time.monotonic() - self._restore_at > 3.0:
                success, reason = self._result
                if success:
                    self.complete()
                else:
                    self.fail(reason)
            return
        if self._sent_at:
            if self._succeeded(client):
                await self._finish_after_device_restore(client, True, "")
            elif time.monotonic() - self._sent_at > self.TIMEOUT_SECONDS:
                await self._finish_after_device_restore(
                    client, False,
                    f"{self.purpose} capability produced no verified effect")
            return
        if self._device_apply_at:
            await self.start_device_if_ready(client)
            return
        candidates = self.candidates(client, self.purpose)
        if not candidates:
            self.fail(f"no usable {self.purpose} capability")
            return
        source = candidates[0]
        self._source_tag = source.tag
        self._source_type = source.item_type
        if source.item_type in (c.TYPE_ROD, c.TYPE_WAND):
            if not source.flags & c.ITEM_APPLIED:
                self._previous_ranged_tag = int(
                    client.state.equipment.get(c.EQUIP_WEAPON_RANGED, 0) or 0)
                await client.apply(source.tag)
                self._device_apply_at = time.monotonic()
                return
            await client.fire(0)
        elif source.item_type == c.TYPE_SPELL:
            await client.fire(0, source.tag)
        else:
            await client.apply(source.tag)
        self._sent_at = time.monotonic()
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "inventory-capability",
                f"{self.purpose} via {source.name} type={source.item_type}")

    async def start_device_if_ready(self, client: AtrinikClient) -> bool:
        """Fire a device after its applied-state update (testable helper)."""
        if not self._device_apply_at or self._sent_at:
            return False
        source = client.state.items.get(self._source_tag)
        if source is not None and source.flags & c.ITEM_APPLIED:
            await client.fire(0)
            self._sent_at = time.monotonic()
            return True
        if time.monotonic() - self._device_apply_at > 3.0:
            self.fail(f"could not ready {self.purpose} device")
        return True


class FarmTask(BotTask):
    """Farm the current zone/patrol for XP, bosses, or an item."""

    MELEE_SWING_HOLD_SECONDS = 0.30
    MELEE_GAP_TIMEOUT = 2.5
    FINISHER_FEEDBACK_GRACE_SECONDS = 2.0
    WIZARD_FINISHER_CAST_LIMIT = 6
    LURE_STALL_SECONDS = 12.0
    UNREACHABLE_TARGET_COOLDOWN_SECONDS = 5 * 60

    def __init__(self, *, zone: str = "", target: str = "", item: str = "",
                 quantity: int = 1, level_until: int = 0,
                 patrol: list[tuple[int, int]] | None = None,
                 loot: LootPolicy | None = None, safety: SafetyPolicy | None = None,
                 combat_skill: str = "", combat_spell: str = "",
                 combat_skill_until_level: int = 0,
                 priority_spawns: list[tuple[int, int, str]] | None = None,
                 neutral_targets: bool = False,
                 aggressive_detection_ranges: dict[str, int] | None = None,
                 pull_avoidance: dict[
                     tuple[int, int], set[tuple[int, int]]] | None = None,
                 allow_launchers: bool | None = None):
        super().__init__(f"farm:{target or item or zone or 'current-zone'}")
        self.zone = zone
        self.target_pattern = re.compile(target, re.I) if target else None
        self.item_pattern = re.compile(item, re.I) if item else None
        self.quantity = quantity
        self.level_until = level_until
        self.patrol = patrol or []
        self.loot = loot or LootPolicy()
        self.safety = safety or SafetyPolicy()
        self.inventory_policy = InventoryPolicy()
        self.combat_skill = combat_skill.casefold().strip()
        self.combat_spell = combat_spell.casefold().strip()
        self.allow_launchers = (
            not bool(self.combat_spell)
            if allow_launchers is None else bool(allow_launchers))
        self.combat_skill_until_level = max(
            0, int(combat_skill_until_level))
        self.priority_spawns = priority_spawns or []
        self.neutral_targets = neutral_targets
        self.aggressive_detection_ranges = {
            str(identity).casefold(): max(0, int(radius))
            for identity, radius in (
                aggressive_detection_ranges or {}).items()
        }
        self.pull_avoidance = {
            (int(goal[0]), int(goal[1])): set(blocked)
            for goal, blocked in (pull_avoidance or {}).items()
        }
        self._priority_target_ids: dict[int, int] = {}
        self._visible_target_count = 0
        self._patrol_index = 0
        self._last_action = 0.0
        self._opened_corpses: set[int] = set()
        self._unsafe_corpses: set[int] = set()
        self._corpse_take_all: dict[int, tuple[int, float]] = {}
        self._corpse_step_attempt: tuple[str, int, int, int, int, float] | None = None
        self._ignored_corpse_tiles: set[tuple[str, int, int]] = set()
        self._loot_move_attempts: dict[int, int] = {}
        self._protected_items: set[int] = set()
        self._upgrade_attempts: set[int] = set()
        self._unidentified_unapply_attempts: set[int] = set()
        # Farm circuits replace their FarmTask child at every leg.  The
        # circuit shares these sets between children so a readable is used at
        # most once per live item tag instead of reopening the same book on
        # every rotation.
        self._lore_book_attempts: set[int] = set()
        self._spellbook_attempts: set[int] = set()
        self._engaged_target: tuple[int, str, int, int, float] | None = None
        self._last_threat_origin: tuple[str, int, int, float] | None = None
        self._suspected_corpse_tiles: dict[
            tuple[str, int, int], tuple[float, int]] = {}
        self._suspected_corpse_probe: tuple[
            tuple[str, int, int], tuple[str, int, int], float] | None = None
        self._finishing_target_id = 0
        self._finisher_casts = 0
        # target id, target HP% when fired, fire time.  Spell action-time
        # becoming ready does not prove that the server has delivered the
        # resulting target-HP update yet.  Holding melee and duplicate casts
        # through that short feedback window prevents both needless mana use
        # and an in-flight Slash swing stealing the intended wizardry kill.
        self._finisher_pending_feedback: tuple[int, int, float] | None = None
        self._normal_weapon_tag = 0
        self._last_finisher_at = 0.0
        self._last_spell_at = 0.0
        # The stats packet can lag several spell commands behind.  Maintain an
        # optimistic local SP balance so two casts cannot both spend the same
        # pre-cast mana and silently consume the emergency-healing reserve.
        self._spell_budget_sp: int | None = None
        self._spell_budget_observed_sp = 0
        self._last_offensive_commit_at = 0.0
        self._last_pull_at = 0.0
        self._pull_launcher_tag = 0
        self._retreat_attempt: tuple[str, int, int, int, int, float] | None = None
        self._retreat_blocked: set[tuple[str, int, int]] = set()
        self._last_retreat_step_at = 0.0
        self._cornered_breakout_target = 0
        self._emergency_recall_source = 0
        self._emergency_recall_apply_at = 0.0
        self._emergency_recall_cast_at = 0.0
        self._emergency_recall_origin = ""
        self._safe_position_history: list[tuple[str, int, int, float]] = []
        self._invisible_escape_direction = 0
        self._invisible_finish_target = 0
        self._invisible_finish_started_at = 0.0
        self._invisible_finish_cooldown_until = 0.0
        self._lure_target_id = 0
        self._lure_target_world: tuple[int, int] | None = None
        self._lure_progress_at = 0.0
        self._last_lure_step_at = 0.0
        self._unreachable_targets: dict[int, float] = {}
        self._approach_attempt: tuple[
            str, int, int, int, int, float, int] | None = None
        self._approach_blocked: dict[tuple[str, int, int], float] = {}
        # target id -> (last hp %, observation time, weighted damage/hit,
        # sample count). This lets tiny monsters and bosses use very different
        # finisher windows without knowing their absolute maximum HP.
        self._target_damage: dict[int, tuple[int, float, float, int]] = {}
        self._melee_target_id = 0
        self._melee_cycle_anchor = 0.0
        self._melee_anchor_message_at = 0.0
        self._finish_race_targets: set[int] = set()
        self._melee_dodged_cycle = -1
        self._melee_positioned_cycle = -1
        self._dodge_clockwise = True
        self._melee_facing = 0
        self._kite_step_attempt: tuple[
            str, int, int, int, int, float, int] | None = None
        self._kite_blocked: dict[tuple[str, int, int], float] = {}
        self._kite_gap_target_id = 0
        self._kite_gap_since = 0.0
        self._caster_anchor: tuple[
            int, str, int, int, float] | None = None
        self._caster_clockwise = True
        self._patrol_step_attempt: tuple[
            str, int, int, int, int, float] | None = None
        self._patrol_blocked: set[tuple[str, int, int]] = set()
        self.map_bounds: tuple[int, int] | None = None
        # Filled by NavigateThenTask from the authored world graph. Keeping
        # the node here lets combat distinguish ground the player can enter
        # from water/void which flying monsters may occupy.
        self.map_node = None

    def within_farm_map(self, client: AtrinikClient, view_x: int,
                        view_y: int) -> bool:
        if self.map_bounds is None:
            return True
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        world_x = m.world_x + view_x - cx
        world_y = m.world_y + view_y - cy
        return (0 <= world_x < self.map_bounds[0] and
                0 <= world_y < self.map_bounds[1])

    def retreat_tile_walkable(self, path: str, x: int, y: int) -> bool:
        """Reject authored walls/fixtures before issuing a direct escape step."""
        node = self.map_node
        if node is None or node.path != path or not node.terrain:
            return True
        return node.walkable(x, y) and (x, y) not in node.occupied

    def ignore_unrequested_peaceful(self, client: AtrinikClient, obj,
                                    node=None) -> bool:
        """Avoid provoking authored non-aggressive creatures by proximity."""
        semantic = map_object_semantic(client, obj)
        if self.target_pattern and self.target_pattern.search(semantic):
            return False
        committed_id = self._engaged_target[0] if self._engaged_target else 0
        if (obj.target_id == committed_id or
                (getattr(client.state, "combat", False) and
                 obj.target_id == getattr(client.state, "target_id", 0))):
            return False
        return self.authored_peaceful(client, obj, node)

    def authored_peaceful(self, client: AtrinikClient, obj, node=None) -> bool:
        """Whether map data marks this visible creature non-aggressive."""
        authored = node or self.map_node
        identities = (
            value.casefold().strip().replace("_", " ")
            for value in (obj.name, map_object_visual_name(client, obj))
            if value
        )
        return bool(authored and any(
            re.sub(r"\.\d+$", "", identity) in
            authored.peaceful_identities for identity in identities))

    def authored_caster(self, client: AtrinikClient, obj, node=None) -> bool:
        """Whether authored map/archetype data marks this monster as a caster."""
        authored = node or self.map_node
        if authored is None or not authored.caster_identities:
            return False
        semantic = map_object_semantic(client, obj).casefold()
        return any(_same_semantic_identity(identity, semantic)
                   for identity in authored.caster_identities)

    def observe_engaged_target(self, client: AtrinikClient,
                               threats: list[tuple], *,
                               preserve_transient: bool = False) -> None:
        """Remember a vanished combat target's last tile as probable loot."""
        if self._engaged_target is None:
            return
        target_id, path, world_x, world_y, last_seen = self._engaged_target
        visible = next((entry for entry in threats
                        if entry[3].target_id == target_id), None)
        now = time.monotonic()
        if visible is not None:
            _, view_x, view_y, _ = visible
            m = client.state.map
            cx, cy = m.width // 2, m.height // 2
            self._engaged_target = (
                target_id, m.path,
                m.world_x + view_x - cx,
                m.world_y + view_y - cy, now)
            return
        # Kill text and the creature-removal packet arrive before the server
        # necessarily clears the selected target. It must override both the
        # transient viewport and still-selected preservation windows.
        kill_confirmed = recent_player_kill(client)
        # A ranged pull or water lure can briefly move the target outside the
        # 17x17 viewport while it remains selected and alive. Preserve that
        # engagement instead of misclassifying it as a corpse and allowing a
        # circuit leg change which drags the monster into the next pack.
        # MAP2 updates can omit a pulled monster for several movement cycles
        # while the viewport recentres.  A healing spell also retargets the
        # player, so neither the protocol target nor recent combat text is a
        # reliable short-term owner of the engagement.  Keep the committed
        # object ID for the same bounded 60-second pursuit window used below;
        # kill text still retires it immediately.
        if (not kill_confirmed and preserve_transient and
                (now - last_seen <= 60.0 or
                 recent_hostile_contact(client, seconds=10.0))):
            return
        if (not kill_confirmed and
                getattr(client.state, "target_id", 0) == target_id and
                now - last_seen <= 60.0):
            return
        self._engaged_target = None
        if (client.state.map.path != path or now - last_seen > 6.0):
            return
        if kill_confirmed:
            # A confirmed kill retires the synthetic off-screen threat too.
            # Keeping its origin made low-health characters flee from a dead
            # monster for the full 20-second invisible-contact grace period.
            self._last_threat_origin = None
            self._retreat_attempt = None
            self._last_retreat_step_at = 0.0
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("threat-origin-retired",
                         f"confirmed kill target={target_id}")
        distance = max(abs(world_x - client.state.map.world_x),
                       abs(world_y - client.state.map.world_y))
        if distance > 3 and not kill_confirmed:
            log.info(
                "distant target vanished without kill confirmation; "
                "skipping corpse detour at %s (%s, %s)",
                path, world_x, world_y)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "corpse-probe-skip",
                    f"unconfirmed distant vanish at {path} "
                    f"({world_x}, {world_y})")
            return
        key = (path, world_x, world_y)
        if key not in self._ignored_corpse_tiles:
            self._suspected_corpse_tiles.setdefault(key, (now, 0))
            log.info("target vanished; probing probable corpse at %s (%s, %s)",
                     path, world_x, world_y)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("corpse-probe-queued",
                         f"{path} ({world_x}, {world_y})")

    def remember_engagement(self, client: AtrinikClient, x: int, y: int,
                            target_id: int) -> None:
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        melee_gap = max(abs(x - cx), abs(y - cy))
        if (melee_gap <= 2 and
                self._melee_target_id != target_id):
            # Neutral farms normally enter melee_kite before melee_position,
            # so the latter is not a reliable place to begin the swing-cycle
            # clock. Anchor it as soon as the committed target closes to the
            # one-empty-tile kiting gap; the Wizardry handoff can then
            # pre-empt a second queued swing even when target-HP packets
            # remain stale.
            self._melee_target_id = target_id
            self._melee_cycle_anchor = time.monotonic()
            self._melee_anchor_message_at = time.time()
            self._melee_dodged_cycle = -1
            self._melee_positioned_cycle = -1
            log.info("melee cycle anchored: target=%s gap=%s",
                     target_id, melee_gap)
        self._engaged_target = (
            target_id, m.path,
            m.world_x + x - cx,
            m.world_y + y - cy,
            time.monotonic())
        self._last_threat_origin = (
            m.path, m.world_x + x - cx, m.world_y + y - cy,
            time.monotonic())

    @staticmethod
    def spell_fire_direction(spell: Item, x: int, y: int,
                             cx: int, cy: int) -> int | None:
        """Return an exact FIRE direction, or None until position is aligned."""
        flags = int(spell.extra.get("flags", 0) or 0)
        if not flags & c.SPELL_DESC_DIRECTION:
            # Targeted spells use the target selected immediately before FIRE;
            # the official client's `/cast` command sends direction zero.
            return 0
        dx, dy = x - cx, y - cy
        if dx and dy and abs(dx) != abs(dy):
            return None
        delta = (max(-1, min(1, dx)), max(-1, min(1, dy)))
        return next((direction for direction, value in c.DIRECTION_DELTAS.items()
                     if value == delta), None)

    @staticmethod
    def projectile_line_clear(client: AtrinikClient, target_x: int,
                              target_y: int, target_id: int, *,
                              origin_x: int | None = None,
                              origin_y: int | None = None) -> bool:
        """Require a neutral-farm projectile ray to contain one creature."""
        m = client.state.map
        cx = m.width // 2 if origin_x is None else origin_x
        cy = m.height // 2 if origin_y is None else origin_y
        dx = target_x - cx
        dy = target_y - cy
        if not dx and not dy:
            return False
        if dx and dy and abs(dx) != abs(dy):
            return False
        step_x = max(-1, min(1, dx))
        step_y = max(-1, min(1, dy))
        for other_x, other_y, other in m.targets(friendly=False):
            if other.target_id == target_id:
                continue
            rel_x, rel_y = other_x - cx, other_y - cy
            if ((step_x == 0 and rel_x != 0) or
                    (step_y == 0 and rel_y != 0)):
                continue
            if step_x and (rel_x == 0 or rel_x // step_x <= 0):
                continue
            if step_y and (rel_y == 0 or rel_y // step_y <= 0):
                continue
            steps_x = abs(rel_x) if step_x else abs(rel_y)
            steps_y = abs(rel_y) if step_y else steps_x
            if steps_x == steps_y:
                return False
        return True

    def combat_spell_item(self, client: AtrinikClient) -> Item | None:
        if not self.combat_spell:
            return None
        return next((
            item for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL and
            self.combat_spell in item.name.casefold()), None)

    def healing_mana_reserve(self, client: AtrinikClient) -> int:
        """Keep enough SP for three casts of the cheapest known heal."""
        costs = [
            int(item.extra.get("cost", 0) or 0)
            for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL and
            re.search(r"heal", item.name, re.I) and
            int(item.extra.get("cost", 0) or 0) > 0
        ]
        return min(costs, default=0) * 3

    def offensive_spell_affordable(self, client: AtrinikClient,
                                   spell: Item) -> bool:
        """Spend mana only when the emergency healing reserve remains."""
        cost = int(spell.extra.get("cost", 0) or 0)
        sp = self._sync_offensive_spell_budget(client)
        return cost <= sp and sp - cost >= self.healing_mana_reserve(client)

    def _sync_offensive_spell_budget(self, client: AtrinikClient) -> int:
        """Reconcile locally committed spell costs with delayed SP packets."""
        now = time.monotonic()
        observed = int(client.state.stats.get("sp", 0) or 0)
        maximum = int(client.state.stats.get("maxsp", 0) or 0)
        quiet = now - self._last_offensive_commit_at
        if (self._spell_budget_sp is None or quiet >= 10.0 or
                (maximum > 0 and observed >= maximum and quiet >= 5.0)):
            self._spell_budget_sp = observed
        else:
            delta = observed - self._spell_budget_observed_sp
            if delta < 0:
                # A cost acknowledgement cannot make already reserved mana
                # available a second time.
                self._spell_budget_sp = min(
                    self._spell_budget_sp, observed)
            elif delta > 0:
                # Credit only regeneration actually visible from the server.
                self._spell_budget_sp = min(
                    observed, self._spell_budget_sp + delta)
        self._spell_budget_observed_sp = observed
        return min(observed, self._spell_budget_sp)

    def commit_offensive_spell(self, client: AtrinikClient,
                               spell: Item) -> None:
        """Reserve an issued spell's SP before its stats acknowledgement."""
        available = self._sync_offensive_spell_budget(client)
        cost = int(spell.extra.get("cost", 0) or 0)
        self._spell_budget_sp = max(0, available - cost)
        self._spell_budget_observed_sp = int(
            client.state.stats.get("sp", 0) or 0)
        self._last_offensive_commit_at = time.monotonic()

    def training_skill_item(self, client: AtrinikClient) -> Item | None:
        if not self.combat_skill:
            return None
        return next((
            item for item in client.state.inventory
            if item.item_type == c.TYPE_SKILL and
            self.combat_skill in item.name.casefold()), None)

    def training_active(self, client: AtrinikClient) -> bool:
        skill = self.training_skill_item(client)
        if skill is None:
            return False
        level = int(skill.extra.get("level", 0) or 0)
        return (not self.combat_skill_until_level or
                level < self.combat_skill_until_level)

    def observed_target_hp(self, client: AtrinikClient, obj) -> int:
        """Return selected HP and update this target's damage-per-hit model."""
        hp = int(obj.target_hp or 0)
        if (hp <= 0 and
                getattr(client.state, "target_id", 0) == obj.target_id):
            hp = int(client.state.stats.get("target_hp", 0) or 0)
        if hp <= 0:
            return 0
        now = time.monotonic()
        previous = self._target_damage.get(obj.target_id)
        estimate, samples = 0.0, 0
        if previous is not None:
            old_hp, old_at, estimate, samples = previous
            delta = old_hp - hp
            if 0 < delta <= 60 and self._finishing_target_id != obj.target_id:
                # Stats can coalesce more than one melee swing. Normalize the
                # percentage delta by elapsed weapon cycles before feeding the
                # time-weighted moving estimate.
                interval = max(
                    0.5, float(client.state.stats.get("weapon_speed", 2) or 2))
                hits = max(1, round(max(0.0, now - old_at) / interval))
                sample = delta / hits
                weight = min(0.70, max(0.25, (now - old_at) / interval * 0.35))
                estimate = (sample if samples == 0 else
                            estimate * (1.0 - weight) + sample * weight)
                samples += 1
            elif hp > old_hp:
                estimate, samples = 0.0, 0
        self._target_damage[obj.target_id] = (hp, now, estimate, samples)
        return hp

    def finisher_window(self, target_id: int) -> int:
        """Adaptive HP% handoff covering an in-flight and one queued hit."""
        observation = self._target_damage.get(target_id)
        wizard_training = "wizardry" in self.combat_skill.casefold()
        if observation is None:
            return 18
        if observation[3] == 0:
            # A neutral can leave and re-enter the viewport between its first
            # two packets, so the first HP value observed may already be after
            # a large melee hit. Treat an unknown sub-75% neutral as ready;
            # the untouched 100% packet still remains outside this window.
            return 75 if self.neutral_targets and wizard_training else 18
        damage_per_hit = observation[2]
        if self.neutral_targets and wizard_training:
            # Targeted combat can already have the next Slash swing queued
            # when its HP update reaches us. On low-HP catch-up monsters a
            # second ordinary hit can then land while combat-off and steal the
            # kill before the first spell is accepted. Hand off after the
            # first measured hit, retaining enough margin for two in-flight
            # hits plus the buffer step used by neutral spell finishers.
            return max(18, min(
                75, round(damage_per_hit * 2.4 + 4.0)))
        return max(6, min(40, round(damage_per_hit * 2.1 + 2.0)))

    def should_finish_before_healing(
            self, client: AtrinikClient, threats: list[tuple],
            nearby_count: int, ratio: float) -> bool:
        """Finish one nearly dead neutral instead of extending its fight."""
        if (not self.neutral_targets or nearby_count != 1 or
                self._engaged_target is None):
            return False
        target_id = self._engaged_target[0]
        engaged = next((entry for entry in threats
                        if entry[3].target_id == target_id), None)
        if engaged is None:
            return False
        # Preserve a meaningful two-hit player margin even on farms whose
        # ordinary flee threshold is intentionally very conservative.
        floor = max(0.55, self.safety.flee_below - 0.15)
        target_hp = self.observed_target_hp(client, engaged[3])
        finish = floor < ratio <= self.safety.heal_below and (
            0 < target_hp <= 25)
        if finish and target_id not in self._finish_race_targets:
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "combat-finish-window",
                    f"target={target_id} target-hp={target_hp}% "
                    f"player-hp={ratio * 100:.0f}% floor={floor * 100:.0f}%")
            self._finish_race_targets.add(target_id)
        return finish

    def should_training_finish(self, client: AtrinikClient, obj) -> bool:
        if not self.training_active(client):
            return False
        hp = self.observed_target_hp(client, obj)
        return 0 < hp <= self.finisher_window(obj.target_id)

    def primary_combat_spell(self, client: AtrinikClient) -> Item | None:
        """Use configured spell pressure until finisher logic takes control."""
        if self.neutral_targets:
            # Dense passive farms are safe only while exactly one creature has
            # been deliberately pulled. Directional pressure during the chase
            # can clip another neutral spawn; retain the isolated ranged pull
            # and close-range finisher, but let melee handle the middle.
            return None
        if (self.combat_skill_until_level and self.combat_skill and
                not self.training_active(client)):
            return None
        return self.combat_spell_item(client)

    def observed_training_melee_hit(self, client: AtrinikClient, obj) -> bool:
        """Whether this committed target has received its setup melee hit."""
        if (not self.neutral_targets or
                "wizardry" not in self.combat_skill.casefold() or
                self._melee_target_id != obj.target_id):
            return False
        semantic = map_object_semantic(client, obj).casefold()
        messages = getattr(client.state, "messages", ())
        for entry in reversed(messages):
            if len(entry) <= 3:
                continue
            if entry[0] < self._melee_anchor_message_at:
                break
            match = re.match(
                r"You hit (.+?) for .+? with ([^.]+)\.$", entry[3], re.I)
            damage_type = match.group(2).casefold() if match else ""
            if (match and any(school in damage_type for school in (
                    "impact", "slash", "cleave", "pierce")) and
                    _same_semantic_identity(
                        match.group(1).casefold(), semantic)):
                return True
        return False

    async def training_finisher(self, client: AtrinikClient, x: int, y: int,
                                obj) -> bool:
        """Attempt the killing blow with the explicitly requested skill."""
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        pending_for_target = bool(
            self._finisher_pending_feedback is not None and
            self._finisher_pending_feedback[0] == obj.target_id)
        # Do not start a training handoff while already hurt, but once a spell
        # is in flight retain ownership of the finisher down to the genuine
        # flee threshold.  Dropping it at 79% previously re-enabled Slash
        # after Sera had already spent the mana and stood still for the cast.
        if (hp / maxhp <= self.safety.flee_below or
                (hp / maxhp < 0.80 and not pending_for_target)):
            return False
        target_hp = self.observed_target_hp(client, obj)
        window = self.finisher_window(obj.target_id)
        weapon_interval = max(
            0.8, float(client.state.stats.get("weapon_speed", 2.0) or 2.0))
        timed_neutral_handoff = bool(
            self.neutral_targets and
            "wizardry" in self.combat_skill.casefold() and
            self._melee_target_id == obj.target_id and
            self._melee_cycle_anchor > 0.0 and
            time.monotonic() - self._melee_cycle_anchor >=
            weapon_interval * 1.05)
        melee_hit_handoff = self.observed_training_melee_hit(
            client, obj)
        if (not self.training_active(client) or
                target_hp <= 0 or
                (target_hp > window and not timed_neutral_handoff and
                 not melee_hit_handoff)):
            return False
        skill = self.training_skill_item(client)
        if skill is None:
            return False

        if "wizardry" in skill.name.casefold():
            spell = self.combat_spell_item(client)
            if spell is None:
                return False
            if not self.offensive_spell_affordable(client, spell):
                if self.neutral_targets:
                    # Once a deliberately pulled passive has entered the
                    # Wizardry handoff, falling back to automatic melee at the
                    # healing reserve steals the kill and defeats catch-up
                    # training.  The target is safe to retain: stop attacking
                    # and preserve the gap until regeneration funds another
                    # cast (or the ordinary health gate forces a retreat).
                    await client.clear_actions()
                    await client.set_combat(False)
                    await self.melee_kite(
                        client, x, y, obj, map_node=self.map_node,
                        preserve_spell_line=spell)
                    self._last_action = time.monotonic()
                    return True
                return False
            now = time.monotonic()
            cooldown = max(
                0.8, float(client.state.stats.get("action_time", 0) or 0))
            if self._finishing_target_id != obj.target_id:
                self._finishing_target_id = obj.target_id
                self._finisher_casts = 0
                self._finisher_pending_feedback = None
                self._last_finisher_at = 0.0
            pending = self._finisher_pending_feedback
            if pending is not None and pending[0] == obj.target_id:
                _, fired_hp, fired_at = pending
                if target_hp < fired_hp:
                    # The last cast has been reflected in target state.  A
                    # further cast is now an informed decision rather than a
                    # duplicate based on the pre-cast HP packet.
                    self._finisher_pending_feedback = None
                elif now - fired_at < max(
                        self.FINISHER_FEEDBACK_GRACE_SECONDS,
                        cooldown + 0.50):
                    # The action timer is otherwise dead time.  Keep the
                    # committed monster one or more tiles away while waiting
                    # for authoritative HP feedback, preserving an available
                    # firing line where terrain permits.
                    await self.melee_kite(
                        client, x, y, obj, map_node=self.map_node,
                        preserve_spell_line=spell)
                    return True
                else:
                    # Missed spells and rounded HP percentages must not hold
                    # the fight forever.  Retry after one bounded server
                    # feedback interval.
                    self._finisher_pending_feedback = None
            if (self._finishing_target_id == obj.target_id and
                    now - self._last_finisher_at < cooldown):
                return True
            if self._finisher_casts >= self.WIZARD_FINISHER_CAST_LIMIT:
                return False
            m = client.state.map
            cx, cy = m.width // 2, m.height // 2
            distance = max(abs(x - cx), abs(y - cy))
            if (self.neutral_targets and self._finisher_casts == 0 and
                    distance <= 2):
                # A passive target is already committed and will follow.  Buy
                # a short firing buffer before the first cast so the spell's
                # action timer is spent at range rather than trading free
                # melee hits while Sera waits for target-HP feedback.
                await client.clear_actions()
                await client.set_combat(False)
                if await self.melee_kite(
                        client, x, y, obj, map_node=self.map_node,
                        preserve_spell_line=spell):
                    self._last_action = time.monotonic()
                    return True
            self._last_finisher_at = now
            log.info(
                "wizard finisher: target=%s hp=%s%% window=%s%% timed=%s "
                "melee-hit=%s spell=%s sp=%s",
                obj.target_id, target_hp, window,
                int(timed_neutral_handoff), int(melee_hit_handoff), spell.name,
                client.state.stats.get("sp", 0))
            direction = self.spell_fire_direction(
                spell, x, y, m.width // 2, m.height // 2)
            if (self.neutral_targets and direction not in (None, 0) and
                    not self.projectile_line_clear(
                        client, x, y, obj.target_id)):
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("spell-line-blocked",
                             f"target={obj.target_id} hp={target_hp}%")
                return False
            # Disable automatic melee so it cannot steal the finishing blow
            # while a directional spell is lining up.
            await client.clear_actions()
            await client.set_combat(False)
            if direction is None:
                await client.move_to_view(x, y)
            else:
                await client.fire(direction, spell.tag)
                self.commit_offensive_spell(client, spell)
                self._finisher_casts += 1
                self._finisher_pending_feedback = (
                    obj.target_id, target_hp, time.monotonic())
            self._last_action = time.monotonic()
            return True

        matching_weapon = next((
            item for item in client.state.inventory
            if item.item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and
            item.required_skill_tag == skill.tag), None)
        now = time.monotonic()
        cooldown = max(
            0.8, float(client.state.stats.get("action_time", 0) or 0))
        if (self._finishing_target_id == obj.target_id and
                now - self._last_finisher_at < cooldown):
            return True
        self._finishing_target_id = obj.target_id
        self._last_finisher_at = now
        equipped_tags = set(client.state.equipment.values())
        if matching_weapon is not None and matching_weapon.tag not in equipped_tags:
            if not self._normal_weapon_tag:
                self._normal_weapon_tag = (
                    client.state.equipment.get(c.EQUIP_WEAPON) or
                    client.state.equipment.get(c.EQUIP_WEAPON_RANGED) or 0)
            await client.apply(matching_weapon.tag)
            self._last_action = time.monotonic()
            return True
        if matching_weapon is not None:
            # Targeted combat auto-attacks as soon as the weapon timer is
            # ready; no movement command into an occupied enemy is required.
            await client.set_combat(True, force=True)
            self._last_action = time.monotonic()
            return True

        # Active combat skills such as unarmed attacks can be fired directly.
        m = client.state.map
        direction = self.spell_fire_direction(
            Item(0, extra={"flags": c.SPELL_DESC_DIRECTION}),
            x, y, m.width // 2, m.height // 2)
        if direction is not None:
            await client.set_combat(False)
            await client.clear_actions()
            await client.fire(direction, skill.tag)
            self._last_action = time.monotonic()
            return True
        return False

    @staticmethod
    def _attack_direction(player_x: int, player_y: int,
                          target_x: int, target_y: int) -> int:
        delta = (max(-1, min(1, target_x - player_x)),
                 max(-1, min(1, target_y - player_y)))
        return next((direction for direction, step in c.DIRECTION_DELTAS.items()
                     if step == delta), 0)

    def _melee_square_open(self, client: AtrinikClient, x: int, y: int,
                           target_x: int, target_y: int) -> bool:
        m = client.state.map
        if not (0 <= x < m.width and 0 <= y < m.height):
            return False
        if max(abs(x - target_x), abs(y - target_y)) != 1:
            return False
        if not self.within_farm_map(client, x, y):
            return False
        tile = m.tiles.get((x, y))
        return tile is None or not tile.targets

    def _facing_exposure(self, client: AtrinikClient, facing: int,
                         player_x: int, player_y: int) -> int:
        """Score enemy side/back rolls caused by Sera facing away."""
        if not facing:
            return 0
        score = 0
        for enemy_x, enemy_y, _ in client.state.map.targets(friendly=False):
            if max(abs(enemy_x - player_x), abs(enemy_y - player_y)) > 1:
                continue
            enemy_attack = self._attack_direction(
                enemy_x, enemy_y, player_x, player_y)
            difference = min((facing - enemy_attack) % 8,
                             (enemy_attack - facing) % 8)
            if difference == 0:
                score += 100
            elif difference == 1:
                score += 30
        return score

    def _kite_step_pending(self, client: AtrinikClient, target_id: int,
                           now: float) -> bool:
        if self._kite_step_attempt is None:
            return False
        (path, source_x, source_y, destination_x, destination_y,
         sent_at, attempted_target) = self._kite_step_attempt
        m = client.state.map
        position = (m.path, m.world_x, m.world_y)
        if attempted_target != target_id or m.path != path:
            self._kite_step_attempt = None
            return False
        if position == (path, destination_x, destination_y):
            self._kite_blocked.pop(
                (path, destination_x, destination_y), None)
            self._kite_step_attempt = None
            return False
        if (position != (path, source_x, source_y) or
                now - sent_at >= movement_ack_timeout(client)):
            self._kite_blocked[(path, destination_x, destination_y)] = now
            self._kite_step_attempt = None
            return False
        return True

    async def melee_kite(self, client: AtrinikClient, x: int, y: int,
                         obj, *, map_node: MapNode | None = None,
                         preserve_spell_line: Item | None = None) -> bool:
        """Move as soon as a pursuer closes to one empty intervening tile."""
        m = client.state.map
        current_node = map_node
        if current_node is None and self.map_node is not None:
            if self.map_node.path == m.path:
                current_node = self.map_node
        cx, cy = m.width // 2, m.height // 2
        distance = max(abs(x - cx), abs(y - cy))
        target_hp = int(getattr(obj, "target_hp", 0) or
                        client.state.stats.get("target_hp", 0) or 0)
        if (distance not in (1, 2, 3, 4) or target_hp <= 0 or target_hp > 100 or
                self._engaged_target is None or
                self._engaged_target[0] != obj.target_id):
            self._kite_gap_target_id = 0
            self._kite_gap_since = 0.0
            return False
        now = time.monotonic()
        if self._kite_step_pending(client, obj.target_id, now):
            return True
        action_time = effective_action_time(client, now)
        maintain_gap_step = False
        if distance in (2, 3, 4):
            if self._kite_gap_target_id != obj.target_id:
                self._kite_gap_target_id = obj.target_id
                self._kite_gap_since = now
            maintain_gap_step = (
                distance in (2, 3) and
                (preserve_spell_line is not None or
                 action_time > self.MELEE_SWING_HOLD_SECONDS))
            if maintain_gap_step:
                # Maintain two empty tiles rather than trusting a distance-two
                # viewport snapshot. Fast pursuers can advance twice between
                # 500 ms control ticks; this extra protocol-latency buffer
                # keeps their coalesced update from becoming adjacency before
                # Sera's next escape packet reaches the server.
                self._kite_gap_since = now
            # Let the committed monster close the empty tile. If it cannot,
            # fall back to the normal collision-aware approach rather than
            # deadlocking against an immobile or obstructed target. A
            # A move can briefly show three empty tiles, so distance four is
            # held rather than immediately approaching back into danger.
            if (not maintain_gap_step and
                    now - self._kite_gap_since <= self.MELEE_GAP_TIMEOUT):
                return True
            if not maintain_gap_step:
                self._kite_gap_target_id = 0
                self._kite_gap_since = 0.0
                return False
        else:
            self._kite_gap_target_id = 0
            self._kite_gap_since = 0.0
        if (distance == 1 and
                action_time <= self.MELEE_SWING_HOLD_SECONDS and
                preserve_spell_line is None):
            # Remain adjacent just long enough for automatic melee to fire.
            return True
        if distance == 4:
            self._kite_gap_target_id = 0
            self._kite_gap_since = 0.0
            return False
        self._kite_blocked = {
            key: blocked_at for key, blocked_at in self._kite_blocked.items()
            if now - blocked_at <= 20.0
        }
        other_targets = [
            (tx, ty) for tx, ty, other in m.targets(friendly=False)
            if other.target_id != obj.target_id]
        candidates = []
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            if not direction:
                continue
            nx, ny = cx + dx, cy + dy
            desired_distance = distance + 1 if maintain_gap_step else 2
            if max(abs(x - nx), abs(y - ny)) != desired_distance:
                continue
            if not (0 <= nx < m.width and 0 <= ny < m.height):
                continue
            world_x, world_y = m.world_x + dx, m.world_y + dy
            if current_node is not None:
                if not (0 <= world_x < current_node.width and
                        0 <= world_y < current_node.height):
                    continue
            elif not self.within_farm_map(client, nx, ny):
                continue
            if (m.path, world_x, world_y) in self._kite_blocked:
                continue
            if (current_node is not None and
                    not current_node.walkable(world_x, world_y)):
                continue
            tile = m.tiles.get((nx, ny))
            if tile is not None and tile.targets:
                continue
            separation = min((max(abs(tx - nx), abs(ty - ny))
                              for tx, ty in other_targets), default=99)
            mobility = retreat_mobility(
                current_node, (world_x, world_y), depth=2)[1]
            aligned = int(
                preserve_spell_line is not None and
                self.spell_fire_direction(
                    preserve_spell_line, x, y, nx, ny) is not None)
            candidates.append((aligned, separation, mobility, random.random(),
                               direction, world_x, world_y))
        if not candidates:
            return False
        _, _, _, _, direction, world_x, world_y = max(candidates)
        await client.clear_actions()
        await client.move(direction, run=False)
        self._kite_step_attempt = (
            m.path, m.world_x, m.world_y, world_x, world_y, now,
            obj.target_id)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "melee-kite-gap" if maintain_gap_step else "melee-kite",
                f"target={obj.target_id} action-time={action_time:.2f} "
                f"step={world_x},{world_y}")
        return True

    def stationary_caster(self, client: AtrinikClient, x: int, y: int,
                          obj, now: float | None = None) -> bool:
        """Require two observations on one authored tile before orbiting."""
        if not self.authored_caster(client, obj):
            self._caster_anchor = None
            return False
        now = time.monotonic() if now is None else now
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        anchor = (
            obj.target_id, m.path,
            m.world_x + x - cx, m.world_y + y - cy)
        if self._caster_anchor is None or self._caster_anchor[:4] != anchor:
            self._caster_anchor = (*anchor, now)
            return False
        return now - self._caster_anchor[4] >= 0.45

    async def caster_orbit(self, client: AtrinikClient, x: int, y: int,
                           obj) -> bool:
        """Circle an isolated stationary caster, pausing only for a swing."""
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        if max(abs(x - cx), abs(y - cy)) != 1:
            return False
        now = time.monotonic()
        if self._kite_step_pending(client, obj.target_id, now):
            return True
        action_time = effective_action_time(client, now)
        if action_time <= self.MELEE_SWING_HOLD_SECONDS:
            return True
        ring = (
            (-1, -1), (0, -1), (1, -1), (1, 0),
            (1, 1), (0, 1), (-1, 1), (-1, 0),
        )
        relative = (cx - x, cy - y)
        if relative not in ring:
            return False
        index = ring.index(relative)
        directions = (1, -1) if self._caster_clockwise else (-1, 1)
        target_world_x = m.world_x + x - cx
        target_world_y = m.world_y + y - cy
        other_targets = {
            (tx, ty) for tx, ty, other in m.targets(friendly=False)
            if other.target_id != obj.target_id
        }
        for step in directions:
            offset_x, offset_y = ring[(index + step) % len(ring)]
            world_x = target_world_x + offset_x
            world_y = target_world_y + offset_y
            view_x = cx + world_x - m.world_x
            view_y = cy + world_y - m.world_y
            direction = self._attack_direction(cx, cy, view_x, view_y)
            if not direction or (view_x, view_y) in other_targets:
                continue
            if not self.within_farm_map(client, view_x, view_y):
                continue
            if (self.map_node is not None and
                    (not self.map_node.walkable(world_x, world_y) or
                     (world_x, world_y) in self.map_node.occupied)):
                continue
            tile = m.tiles.get((view_x, view_y))
            if tile is not None and tile.targets:
                continue
            await client.clear_actions()
            await client.move(direction, run=False)
            self._kite_step_attempt = (
                m.path, m.world_x, m.world_y, world_x, world_y, now,
                obj.target_id)
            if step != directions[0]:
                self._caster_clockwise = not self._caster_clockwise
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "caster-orbit",
                    f"target={obj.target_id} action-time={action_time:.2f} "
                    f"step={world_x},{world_y} clockwise="
                    f"{int(self._caster_clockwise)}")
            return True
        return False

    async def melee_position(self, client: AtrinikClient, x: int, y: int,
                             obj) -> bool:
        """Dodge mid-cycle, then seek a safe side/back attack position."""
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        if max(abs(x - cx), abs(y - cy)) != 1:
            return False
        now = time.monotonic()
        interval = max(0.8, float(
            client.state.stats.get("weapon_speed", 2.0) or 2.0))
        target_facing = self._attack_direction(cx, cy, x, y)
        if self._melee_target_id != obj.target_id:
            self._melee_target_id = obj.target_id
            self._melee_cycle_anchor = now
            self._melee_dodged_cycle = -1
            self._melee_positioned_cycle = -1
            # Automatic melee is immediately eligible on a new target and
            # attack_object faces Sera toward it before resolving the roll.
            self._melee_facing = target_facing
            return False
        elapsed = max(0.0, now - self._melee_cycle_anchor)
        cycle = int(elapsed // interval)
        phase = (elapsed % interval) / interval
        if phase < 0.15:
            # The predicted automatic swing faces Sera back toward the target.
            self._melee_facing = target_facing

        # (attack direction from candidate, x, y, movement direction). Include
        # holding the current square so a safe backstab position is preserved.
        candidates = [(target_facing, cx, cy, 0)]
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            px, py = x - dx, y - dy
            move_direction = self._attack_direction(cx, cy, px, py)
            if (max(abs(px - cx), abs(py - cy)) <= 1 and
                    (px, py) != (cx, cy) and move_direction and
                    self._melee_square_open(client, px, py, x, y)):
                candidates.append((direction, px, py, move_direction))

        # attack_object() faces Sera toward the target before rolling. Matching
        # the monster facing is therefore a backstab (+5); either neighbour is
        # a sidestab (+2). Defensive exposure is ranked first so gaining that
        # bonus never deliberately gives an adjacent enemy the same bonus.
        facing = int(getattr(obj, "direction", 0) or 0)
        if phase >= 0.68 and self._melee_positioned_cycle != cycle and facing:
            desired = [facing, ((facing - 2) % 8) + 1, (facing % 8) + 1]
            ranked = sorted(candidates, key=lambda entry: (
                self._facing_exposure(
                    client, entry[3] or self._melee_facing,
                    entry[1], entry[2]),
                desired.index(entry[0]) if entry[0] in desired else 99,
                bool(entry[3]),
            ))
            _, _, _, move_direction = ranked[0]
            self._melee_positioned_cycle = cycle
            if move_direction:
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    exposure = self._facing_exposure(
                        client, move_direction,
                        cx + c.DIRECTION_DELTAS[move_direction][0],
                        cy + c.DIRECTION_DELTAS[move_direction][1])
                    recorder(
                        "melee-position",
                        f"target={obj.target_id} phase={phase:.2f} "
                        f"monster_facing={facing} exposure={exposure}")
                await client.move(move_direction)
                self._melee_facing = move_direction
                return True
            return False

        # One lateral step in the middle of each weapon cycle changes the
        # monster attack vector. Its remembered enemy coordinate then grants
        # the monster-turn penalty (-6) on the next roll.
        movable = [entry for entry in candidates if entry[3]]
        if (0.25 <= phase < 0.68 and
                self._melee_dodged_cycle != cycle and movable):
            current_attack = target_facing
            preferred_delta = 1 if self._dodge_clockwise else 7
            ranked = sorted(movable, key=lambda entry: (
                self._facing_exposure(
                    client, entry[3], entry[1], entry[2]),
                (entry[0] - current_attack) % 8 != preferred_delta,
                min((entry[0] - current_attack) % 8,
                    (current_attack - entry[0]) % 8),
            ))
            _, px, py, move_direction = ranked[0]
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "melee-dodge",
                    f"target={obj.target_id} phase={phase:.2f} "
                    f"exposure={self._facing_exposure(client, move_direction, px, py)}")
            await client.move(move_direction)
            self._melee_facing = move_direction
            self._melee_dodged_cycle = cycle
            self._dodge_clockwise = not self._dodge_clockwise
            return True
        return False

    async def approach_target(self, client: AtrinikClient, x: int, y: int,
                              obj) -> bool:
        """Take one collision-aware step toward a visible hostile."""
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        now = time.monotonic()
        if self._approach_attempt is not None:
            (path, source_x, source_y, destination_x, destination_y,
             attempted_at, target_id) = self._approach_attempt
            if (path == m.path and target_id == obj.target_id and
                    (m.world_x, m.world_y) == (source_x, source_y)):
                if now - attempted_at < 0.8:
                    return True
                self._approach_blocked[(
                    path, destination_x, destination_y)] = now
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder(
                        "approach-blocked",
                        f"target={target_id} tile={destination_x},{destination_y}")
            elif (path == m.path and
                  (m.world_x, m.world_y) ==
                  (destination_x, destination_y)):
                self._approach_blocked.pop(
                    (path, destination_x, destination_y), None)
            self._approach_attempt = None
        self._approach_blocked = {
            key: blocked_at for key, blocked_at in self._approach_blocked.items()
            if now - blocked_at <= 20.0
        }
        current_distance = max(abs(x - cx), abs(y - cy))
        threats = [(tx, ty) for tx, ty, other in m.targets(friendly=False)
                   if other.target_id != obj.target_id]
        candidates = []
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < m.width and 0 <= ny < m.height):
                continue
            if not self.within_farm_map(client, nx, ny):
                continue
            tile = m.tiles.get((nx, ny))
            if tile is not None and tile.targets:
                continue
            world_x, world_y = m.world_x + dx, m.world_y + dy
            if (m.path, world_x, world_y) in self._approach_blocked:
                continue
            if self.map_node is not None and not self.map_node.walkable(
                    world_x, world_y):
                continue
            distance = max(abs(x - nx), abs(y - ny))
            if distance >= current_distance:
                continue
            separation = min((max(abs(tx - nx), abs(ty - ny))
                              for tx, ty in threats), default=99)
            candidates.append((
                distance,
                abs(x - nx) + abs(y - ny),
                self._facing_exposure(client, direction, nx, ny),
                -separation,
                direction,
            ))
        if not candidates:
            return False
        _, _, _, _, direction = min(candidates)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("approach-step",
                     f"target={obj.target_id} direction={direction}")
        dx, dy = c.DIRECTION_DELTAS[direction]
        await client.clear_actions()
        await client.move(direction)
        self._approach_attempt = (
            m.path, m.world_x, m.world_y,
            m.world_x + dx, m.world_y + dy, now, obj.target_id)
        return True

    async def pursue_selected_target_last_seen(
            self, client: AtrinikClient, nearest_threat) -> bool:
        """Follow an off-screen ranged pull briefly, then abandon it safely."""
        if self._engaged_target is None:
            return False
        target_id, path, world_x, world_y, last_seen = self._engaged_target
        now = time.monotonic()
        if (target_id == self._lure_target_id and
                self._lure_progress_at and
                now - self._lure_progress_at >= self.LURE_STALL_SECONDS):
            target_world = self._lure_target_world or (world_x, world_y)
            return await self.abandon_unreachable_lure(
                client, target_id, target_world, now)
        if (getattr(client.state, "target_id", 0) != target_id or
                int(client.state.stats.get("target_hp", 0) or 0) <= 0 or
                client.state.map.path != path):
            return False
        age = now - last_seen
        if nearest_threat is not None and nearest_threat[0] <= 4:
            return False
        if age <= 1.5:
            # MAP2 can omit the moving target for one recentering frame. Hold
            # the deliberately created pull distance instead of immediately
            # pursuing and undoing the ranged setup.
            return True
        direction = self.patrol_step_direction(
            (client.state.map.world_x, client.state.map.world_y),
            (world_x, world_y))
        if age > 12.0 or not direction:
            await client.clear_actions()
            await client.set_combat(False)
            await client.clear_target()
            self._engaged_target = None
            self._last_action = time.monotonic()
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("engagement-abandon",
                         f"target={target_id} last_seen={age:.1f}s")
            return True
        await client.clear_actions()
        await client.move(direction)
        self._last_action = time.monotonic()
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("engagement-pursue",
                     f"target={target_id} direction={direction} "
                     f"last_seen={age:.1f}s")
        return True

    async def restore_normal_weapon(self, client: AtrinikClient,
                                    live_target_ids: set[int]) -> bool:
        if (not self._finishing_target_id or
                self._finishing_target_id in live_target_ids):
            return False
        self._finishing_target_id = 0
        tag, self._normal_weapon_tag = self._normal_weapon_tag, 0
        item = client.state.items.get(tag)
        if item is not None and not item.flags & c.ITEM_APPLIED:
            await client.apply(tag)
            self._last_action = time.monotonic()
            return True
        return False

    async def restore_combat_loadout(self, client: AtrinikClient) -> bool:
        """Unready stale launchers and one-shot maintenance devices."""
        ranged_item = next((
            item for item in client.state.inventory
            if (item.item_type in (c.TYPE_ROD, c.TYPE_WAND) or
                not self.allow_launchers and
                item.item_type in (c.TYPE_BOW, c.TYPE_ARROW)) and
            item.flags & c.ITEM_APPLIED), None)
        if ranged_item is None:
            return False
        await client.apply(ranged_item.tag)
        self._pull_launcher_tag = 0
        self._last_action = time.monotonic()
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            action = (
                "device-unready" if ranged_item.item_type in
                (c.TYPE_ROD, c.TYPE_WAND) else
                "launcher-unready" if ranged_item.item_type == c.TYPE_BOW else
                "ammunition-unready")
            recorder(action,
                     f"{ranged_item.name}: restore configured combat loadout")
        return True

    async def maintain_inventory(self, client: AtrinikClient) -> bool:
        """Protect rare loot and apply only clear, usable gear upgrades."""
        level = int(client.state.stats.get("level", 0))
        # Reconnect inventory replay can restore a previous process's applied
        # ranged state.  Establish the configured combat loadout before lore,
        # spellbook, lock, or upgrade work so a long optional-maintenance queue
        # cannot postpone shield-safe combat readiness.
        if await self.restore_combat_loadout(client):
            return True
        for item in client.state.inventory:
            lore_attempted = getattr(client, "lore_book_attempted", None)
            if (item.item_type == c.TYPE_BOOK and
                    item.tag not in self._lore_book_attempts and
                    not item.flags & c.ITEM_NO_SKILL_IDENT and
                    not (lore_attempted is not None and
                         lore_attempted(item)) and
                    not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED |
                                      c.ITEM_DAMNED)):
                # A readable book grants literacy XP only once. Applying an
                # already-read or too-difficult book is harmless, while doing
                # this before sale/storage makes otherwise discarded lore a
                # permanent character and identification improvement.
                await client.apply(item.tag)
                self._lore_book_attempts.add(item.tag)
                marker = getattr(client, "mark_lore_book_attempted", None)
                if marker is not None:
                    marker(item)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("read-lore-book", item.name)
                self._last_action = time.monotonic()
                return True
            if (item.item_type == c.TYPE_SPELLBOOK and
                    item.tag not in self._spellbook_attempts and
                    self.inventory_policy.identified(item) and
                    not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED |
                                      c.ITEM_DAMNED)):
                # The server rejects already-known or above-level spells
                # without consuming the book. Unknown/cursed spellbooks stay
                # protected until identification makes learning safe.
                await client.apply(item.tag)
                self._spellbook_attempts.add(item.tag)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("learn-spellbook", item.name)
                self._last_action = time.monotonic()
                return True
            if (item.tag not in self._unidentified_unapply_attempts and
                    item.flags & c.ITEM_APPLIED and
                    item.item_type in (
                        *self.inventory_policy.equipment_types,
                        c.TYPE_ARROW) and
                    not self.inventory_policy.identified(item)):
                # Older bot runs may already have equipped unknown gear. Try
                # to remove it before storage; a truly cursed item may refuse,
                # but it must never be silently accepted as safe equipment.
                await client.apply(item.tag)
                self._unidentified_unapply_attempts.add(item.tag)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("unidentified-unequip", item.name)
                self._last_action = time.monotonic()
                return True
            # Locking makes an otherwise compatible stack distinct. Undo locks
            # left by older bot versions on all known stackable item classes.
            if (item.flags & c.ITEM_LOCKED and
                    self.inventory_policy.stackable(item)):
                await client.lock_item(item.tag)
                self._protected_items.discard(item.tag)
                self._last_action = time.monotonic()
                return True
            if (item.flags & c.ITEM_LOCKED and
                    not item.flags & c.ITEM_APPLIED and
                    not self.inventory_policy.should_lock(item)):
                # Unknown loot is locked while its properties are unsafe.
                # Reconcile that lock after identification so an ordinary
                # item can enter the batched sale path instead of consuming
                # carrying capacity forever.
                await client.lock_item(item.tag)
                self._protected_items.discard(item.tag)
                self._last_action = time.monotonic()
                return True
            if (item.tag not in self._protected_items and
                    self.inventory_policy.should_lock(item) and
                    not item.flags & c.ITEM_LOCKED):
                await client.lock_item(item.tag)
                self._protected_items.add(item.tag)
                self._last_action = time.monotonic()
                return True
        equipped = [client.state.items[tag]
                    for tag in client.state.equipment.values()
                    if tag in client.state.items]
        equipped_by_tag = {item.tag: item for item in equipped}

        def score_item(item: Item) -> int:
            score = self.inventory_policy.gear_score(item)
            prototype = BuyShopUpgradeTask._prototype(client, item)
            return score + (prototype.base_score
                            if prototype is not None else 0)

        for candidate in sorted(
                client.state.inventory,
                key=score_item, reverse=True):
            # The owning circuit decides whether launchers belong to this
            # build.  Do not let the generic optimizer briefly ready an
            # incidental bow from loot or a restored inventory: on the live
            # hybrid character that suppressed the equipped shield's
            # protections between spell-training milestones.  Explicit
            # ranged tasks retain normal launcher upgrade behavior.
            if (not self.allow_launchers and
                    candidate.item_type in (c.TYPE_BOW, c.TYPE_ARROW)):
                continue
            if (candidate.tag in self._upgrade_attempts or
                    not self.inventory_policy.identified(candidate) or
                    candidate.flags & (c.ITEM_APPLIED | c.ITEM_UNPAID |
                                       c.ITEM_CURSED | c.ITEM_DAMNED) or
                    candidate.required_level > level):
                continue
            score = score_item(candidate)
            if score < 0:
                continue
            slots = self.inventory_policy.equipment_slots.get(
                candidate.item_type, ())
            current = [equipped_by_tag[tag]
                       for slot in slots
                       if (tag := client.state.equipment.get(slot))
                       in equipped_by_tag]
            # Weapon-school concentration is more valuable than a modest
            # generic score increase. Never switch the trained weapon or
            # launcher family as an incidental inventory-maintenance action.
            if (candidate.item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and
                    current and candidate.required_skill_tag and
                    any(item.required_skill_tag and
                        item.required_skill_tag != candidate.required_skill_tag
                        for item in current)):
                continue
            if current and score <= min(
                    score_item(item) for item in current) + 15:
                continue
            await client.apply(candidate.tag)
            self._upgrade_attempts.add(candidate.tag)
            self._last_action = time.monotonic()
            return True
        return False

    def _item_count(self, client: AtrinikClient) -> int:
        if self.item_pattern is None:
            return 0
        return sum(item.quantity for item in client.state.inventory
                   if self.item_pattern.search(item.name))

    async def loot_nearby(self, client: AtrinikClient) -> bool:
        """Reach/open a nearby corpse and transfer its wanted contents."""
        now = time.monotonic()
        if time.monotonic() - self._last_action < 0.30:
            return False
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        visible_corpse_tiles = set()
        for (view_x, view_y), tile in m.tiles.items():
            if any("corpse" in
                   map_object_semantic(client, obj).casefold()
                   for obj in tile.objects.values()):
                visible_corpse_tiles.add((
                    m.path, m.world_x + view_x - cx,
                    m.world_y + view_y - cy))
        if any("corpse" in item.name.casefold()
               for item in client.state.ground):
            visible_corpse_tiles.add((m.path, m.world_x, m.world_y))
        # A tile ignore belongs to the visible empty corpse, not to that map
        # coordinate forever. Once an observed tile is corpse-free, expire it
        # so a later respawn on the same boss/filler square can be looted.
        for key in tuple(self._ignored_corpse_tiles):
            if key[0] != m.path:
                continue
            view = cx + key[1] - m.world_x, cy + key[2] - m.world_y
            if (view in m.tiles and key not in visible_corpse_tiles):
                self._ignored_corpse_tiles.discard(key)
        corpses = [item for item in client.state.ground
                   if "corpse" in item.name.casefold()]
        for corpse in corpses:
            if getattr(client.state, "combat", False):
                await client.clear_actions()
                await client.set_combat(False)
                self._last_action = now
                return True
            self._suspected_corpse_tiles.pop(
                (client.state.map.path, client.state.map.world_x,
                 client.state.map.world_y), None)
            hp = client.state.stats.get("hp", 0)
            maxhp = max(1, client.state.stats.get("maxhp", 1))
            if hp / maxhp < 0.50:
                # Server-side trap resolution can still be hazardous on a
                # failed automatic disarm. Leave the corpse in place until
                # healing/regeneration makes opening it safe.
                return True
            contents = [client.state.items[tag]
                        for tag in client.state.inventories.get(corpse.tag, [])
                        if tag in client.state.items]
            corpse_tile = (client.state.map.path,
                           client.state.map.world_x,
                           client.state.map.world_y)
            if corpse.tag in self._unsafe_corpses:
                continue
            if "searched, empty" in corpse.name.casefold():
                # This server-authored suffix survives bot restarts and is
                # stronger evidence than our per-process tag memory. Avoid a
                # new trap/apply/take cycle for a corpse already exhausted by
                # Sera, but only suppress the tile if every stacked corpse is
                # likewise terminal so fresh loot beneath it remains reachable.
                first_observation = corpse.tag not in self._opened_corpses
                self._opened_corpses.add(corpse.tag)
                self._corpse_take_all.pop(corpse.tag, None)
                if all("searched, empty" in candidate.name.casefold()
                       for candidate in corpses):
                    self._ignored_corpse_tiles.add(corpse_tile)
                record = getattr(client, "record_action", None)
                if first_observation and record is not None:
                    record("corpse-empty-skip", f"corpse={corpse.tag}")
                continue
            if (corpse_tile in self._ignored_corpse_tiles and
                    corpse.tag not in self._opened_corpses):
                # The old corpse decayed and a new tagged corpse now occupies
                # the same spawn tile.
                self._ignored_corpse_tiles.discard(corpse_tile)
            if corpse_tile in self._ignored_corpse_tiles and not contents:
                self._corpse_take_all.pop(corpse.tag, None)
                continue
            if (corpse.tag in self._opened_corpses and
                    corpse.tag not in self._corpse_take_all and not contents):
                # Terminal completion belongs to the corpse tag, not merely
                # its tile. A newer corpse on this tile may clear the tile
                # ignore; never recreate a zero-age take timer for the older
                # searched corpse on every tick.
                continue
            if corpse.tag in self._opened_corpses:
                take_phase, take_at = self._corpse_take_all.get(
                    corpse.tag, (0, now))
                if take_phase == 0 and now - take_at >= 0.55:
                    await client.execute_client_command("/take all")
                    self._corpse_take_all[corpse.tag] = (1, now)
                    self._last_action = now
                    log.info("corpse %s: opened; sent /take all", corpse.tag)
                    return True
                if take_phase == 0 or (
                        take_phase == 1 and now - take_at < 0.80):
                    return True
                if not contents:
                    self._ignored_corpse_tiles.add(
                        (client.state.map.path,
                         client.state.map.world_x,
                         client.state.map.world_y))
                    # Completed phases must not keep service detours (food,
                    # banking, selling) marked busy for the rest of a farm.
                    self._corpse_take_all.pop(corpse.tag, None)
            wanted = [item for item in contents
                      if self.loot.wants(item) and
                      self._loot_move_attempts.get(item.tag, 0) < 3]
            if wanted:
                for item in wanted[:5]:
                    await client.move_item(
                        client.state.player_tag, item.tag, item.quantity)
                    self._loot_move_attempts[item.tag] = (
                        self._loot_move_attempts.get(item.tag, 0) + 1)
                self._last_action = time.monotonic()
                return True
            if corpse.tag not in self._opened_corpses:
                # Current servers run traps_auto_disarm() inside container
                # opening before sending the corpse inventory. Explicit find
                # and remove-traps skill fires only delay looting and can be
                # rejected while the preceding melee timer is active.
                await client.apply(corpse.tag)
                self._opened_corpses.add(corpse.tag)
                self._corpse_take_all[corpse.tag] = (0, now)
                self._last_action = time.monotonic()
                log.info("corpse %s: opening with server trap handling",
                         corpse.tag)
                return True
        # Map packets identify visible objects by face rather than item tag.
        # The corpse tag only appears in the below inventory after standing
        # on it, so first take one direct step onto an adjacent corpse.
        if self._corpse_step_attempt is not None:
            path, from_x, from_y, target_x, target_y, sent_at = (
                self._corpse_step_attempt)
            if (m.path, m.world_x, m.world_y) == (path, target_x, target_y):
                self._corpse_step_attempt = None
            elif ((m.path, m.world_x, m.world_y) !=
                  (path, from_x, from_y) or now - sent_at >= 0.65):
                self._ignored_corpse_tiles.add((path, target_x, target_y))
                self._corpse_step_attempt = None
            else:
                return True
        visible_corpses = []
        for (view_x, view_y), tile in m.tiles.items():
            if max(abs(view_x - cx), abs(view_y - cy)) != 1:
                continue
            if not self.within_farm_map(client, view_x, view_y):
                continue
            if not any(
                    "corpse" in
                    map_object_semantic(client, obj).casefold()
                    for obj in tile.objects.values()):
                continue
            world_x = m.world_x + view_x - cx
            world_y = m.world_y + view_y - cy
            if (m.path, world_x, world_y) in self._ignored_corpse_tiles:
                continue
            visible_corpses.append((view_x, view_y, world_x, world_y))
        if visible_corpses:
            hp = client.state.stats.get("hp", 0)
            maxhp = max(1, client.state.stats.get("maxhp", 1))
            if hp / maxhp < 0.50:
                return True
            view_x, view_y, world_x, world_y = visible_corpses[0]
            delta = (view_x - cx, view_y - cy)
            direction = next((
                direction for direction, value in c.DIRECTION_DELTAS.items()
                if value == delta), 0)
            if direction:
                await client.clear_actions()
                await client.move(direction)
                self._corpse_step_attempt = (
                    m.path, m.world_x, m.world_y, world_x, world_y, now)
                self._last_action = now
                return True
        # A corpse can be hidden in the map packet by a higher-priority object
        # on the same layer. Probe the last tile of a target which disappeared
        # while engaged; arriving there exposes every below-inventory object.
        position = (m.path, m.world_x, m.world_y)
        if self._suspected_corpse_probe is not None:
            key, start, sent_at = self._suspected_corpse_probe
            if position != start:
                self._suspected_corpse_probe = None
            elif now - sent_at < movement_ack_timeout(client):
                return True
            else:
                created, attempts = self._suspected_corpse_tiles.get(
                    key, (now, 5))
                self._suspected_corpse_probe = None
                attempts += 1
                if attempts >= 5:
                    self._suspected_corpse_tiles.pop(key, None)
                    self._ignored_corpse_tiles.add(key)
                    log.info("abandoned unreachable probable corpse at %s", key)
                else:
                    self._suspected_corpse_tiles[key] = (created, attempts)
        candidates = []
        for key, (created, attempts) in self._suspected_corpse_tiles.items():
            if key[0] != m.path or attempts >= 5:
                continue
            distance = max(abs(key[1] - m.world_x), abs(key[2] - m.world_y))
            if distance <= 7:
                candidates.append((distance, created, attempts, key))
        if candidates:
            distance, created, attempts, key = min(candidates)
            if distance == 0:
                if now - created < 0.75:
                    return True
                self._suspected_corpse_tiles.pop(key, None)
                self._ignored_corpse_tiles.add(key)
                log.info("no corpse found at inferred death tile %s", key)
            else:
                living = {
                    (m.world_x + view_x - cx,
                     m.world_y + view_y - cy)
                    for view_x, view_y, obj in m.targets(friendly=False)
                    if obj.target_id
                }
                if self.map_node is None:
                    delta = (
                        max(-1, min(1, key[1] - m.world_x)),
                        max(-1, min(1, key[2] - m.world_y)),
                    )
                    direction = next((
                        value for value, step in c.DIRECTION_DELTAS.items()
                        if step == delta), 0)
                else:
                    direction = self.patrol_step_direction(
                        (m.world_x, m.world_y), (key[1], key[2]), living)
                if not direction:
                    # A passive creature can temporarily occupy the corpse or
                    # a one-tile chokepoint. Do not turn that transient
                    # occupancy into a permanent unreachable-corpse verdict.
                    if living and now - created < 30.0:
                        recorder = getattr(client, "record_action", None)
                        if recorder is not None:
                            recorder(
                                "corpse-probe-postponed",
                                f"{key[0]} ({key[1]}, {key[2]}) "
                                "blocked-by-living-occupant")
                        return False
                    attempts += 1
                    if attempts >= 5:
                        self._suspected_corpse_tiles.pop(key, None)
                        self._ignored_corpse_tiles.add(key)
                        log.info(
                            "abandoned unroutable probable corpse at %s",
                            key)
                    else:
                        self._suspected_corpse_tiles[key] = (
                            created, attempts)
                    return False
                dx, dy = c.DIRECTION_DELTAS[direction]
                await client.clear_actions()
                mover = getattr(client, "move", None)
                if mover is not None:
                    await mover(direction)
                else:
                    await client.move_to_view(cx + dx, cy + dy)
                self._suspected_corpse_probe = (key, position, now)
                self._last_action = now
                return True
        wanted_ground = [item for item in client.state.ground
                         if self.item_pattern is not None and
                         self.item_pattern.search(item.name) and
                         self.loot.wants(item) and not re.search(
                             r"^(wood|passage|floor|wall|stairs?|ladder|"
                             r"portal|exit)$", item.name.strip(), re.I)]
        if wanted_ground:
            for item in wanted_ground[:5]:
                await client.move_item(
                    client.state.player_tag, item.tag, item.quantity)
            self._last_action = time.monotonic()
            return True
        return False

    def pull_spell_item(self, client: AtrinikClient) -> Item | None:
        configured = self.combat_spell_item(client)
        if configured is not None:
            return configured
        return next((item for item in client.state.inventory
                     if item.item_type == c.TYPE_SPELL and
                     int(item.extra.get("flags", 0) or 0) &
                     (c.SPELL_DESC_DIRECTION | c.SPELL_DESC_ENEMY) and
                     not re.search(r"heal|cure|protection", item.name, re.I)),
                    None)

    async def ranged_pull(self, client: AtrinikClient,
                          targets: list[tuple]) -> bool:
        """Aggro one aligned distant target before walking into its pack."""
        now = time.monotonic()
        if now - self._last_pull_at < 2.5:
            return False
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        aligned = []
        isolated = []
        all_hostiles = list(m.targets(friendly=False))
        direction_spell = Item(
            0, extra={"flags": c.SPELL_DESC_DIRECTION})
        for distance, x, y, obj in targets:
            if not 2 < distance <= 7:
                continue
            direction = self.spell_fire_direction(
                direction_spell, x, y, cx, cy)
            neighbours = sum(
                other.target_id != obj.target_id and
                max(abs(other_x - x), abs(other_y - y)) <= 2
                for other_x, other_y, other in all_hostiles)
            if neighbours == 0:
                entry = (distance, x, y, obj, direction)
                isolated.append(entry)
                if (direction is not None and
                        (not self.neutral_targets or
                         self.projectile_line_clear(
                             client, x, y, obj.target_id))):
                    aligned.append(entry)
        spell = self.pull_spell_item(client)
        spell_ready = bool(
            spell is not None and
            self.offensive_spell_affordable(client, spell))
        # Launcher eligibility is a durable build choice, not merely a
        # consequence of whether this leg currently trains with a spell.  If
        # ranged pressure is disabled, approach with the shielded melee
        # loadout instead of briefly readying an untrained launcher and
        # suppressing shield protections.
        allow_launcher = self.allow_launchers
        launcher = next((item for item in client.state.inventory
                         if allow_launcher and
                         item.item_type == c.TYPE_BOW and
                         item.flags & c.ITEM_APPLIED), None)
        if allow_launcher and launcher is None:
            launcher = next((item for item in client.state.inventory
                             if item.item_type == c.TYPE_BOW), None)
        def compatible_ammunition(item: Item) -> bool:
            if (launcher is None or item.item_type != c.TYPE_ARROW or
                    not self.inventory_policy.identified(item)):
                return False
            launcher_name = launcher.name.casefold()
            ammo_name = item.name.casefold()
            if "crossbow" in launcher_name:
                return "bolt" in ammo_name
            if "bow" in launcher_name:
                return "arrow" in ammo_name and "bolt" not in ammo_name
            if "sling" in launcher_name:
                return "stone" in ammo_name or "bullet" in ammo_name
            return True
        ammo = next((
            item for item in client.state.inventory
            if item.flags & c.ITEM_APPLIED and
            compatible_ammunition(item)), None)
        if not spell_ready and launcher is not None and ammo is None:
            ammo = max(
                (item for item in client.state.inventory
                 if compatible_ammunition(item)),
                key=lambda item: (
                    "accuracy" not in item.name.casefold(),
                    item.quantity, -item.quality),
                default=None)
            if ammo is not None:
                await client.apply(ammo.tag)
                self._last_action = now
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("pull-ammo-equip", ammo.name)
                return True
        pull_source_ready = bool(
            (spell is not None and
             self.offensive_spell_affordable(client, spell)) or
            (launcher is not None and ammo is not None))
        if not aligned and isolated and pull_source_ready:
            # Passive targets do not close the gap until damaged. Walk
            # laterally into one of the eight exact FIRE lines instead of
            # approaching straight into melee range. Each tick must strictly
            # improve alignment and preserve at least a two-tile buffer.
            alignment_steps = []
            for distance, x, y, obj, _ in isolated:
                target_dx, target_dy = x - cx, y - cy
                current_error = min(
                    abs(target_dx), abs(target_dy),
                    abs(abs(target_dx) - abs(target_dy)))
                for move_direction, (step_x, step_y) in (
                        c.DIRECTION_DELTAS.items()):
                    if not move_direction:
                        continue
                    next_cx, next_cy = cx + step_x, cy + step_y
                    next_distance = max(
                        abs(x - next_cx), abs(y - next_cy))
                    if not 3 <= next_distance <= 7:
                        continue
                    fire_direction = self.spell_fire_direction(
                        direction_spell, x, y, next_cx, next_cy)
                    if (fire_direction is not None and
                            self.neutral_targets and
                            not self.projectile_line_clear(
                                client, x, y, obj.target_id,
                                origin_x=next_cx, origin_y=next_cy)):
                        continue
                    next_dx, next_dy = x - next_cx, y - next_cy
                    next_error = min(
                        abs(next_dx), abs(next_dy),
                        abs(abs(next_dx) - abs(next_dy)))
                    if fire_direction is None and next_error >= current_error:
                        continue
                    world_x = m.world_x + step_x
                    world_y = m.world_y + step_y
                    if (self.map_node is not None and
                            (not self.map_node.walkable(world_x, world_y) or
                             (world_x, world_y) in self.map_node.occupied)):
                        continue
                    if (self.map_node is None and
                            not self.within_farm_map(
                                client, next_cx, next_cy)):
                        continue
                    tile = m.tiles.get((next_cx, next_cy))
                    if tile is not None and tile.targets:
                        continue
                    separation = min((
                        max(abs(other_x - next_cx),
                            abs(other_y - next_cy))
                        for other_x, other_y, other in all_hostiles
                        if other.target_id != obj.target_id), default=99)
                    alignment_steps.append((
                        fire_direction is None, next_error, -separation,
                        abs(next_distance - distance), random.random(),
                        move_direction, obj.target_id, fire_direction))
            if alignment_steps:
                (*_, move_direction, target_id,
                 fire_direction) = min(alignment_steps)
                await client.clear_actions()
                await client.set_combat(False)
                await client.move(move_direction, run=False)
                self._last_action = now
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder(
                        "ranged-pull-align",
                        f"target={target_id} direction={move_direction} "
                        f"fire-direction={fire_direction or 0}")
                return True
        if not aligned:
            return False
        _, x, y, obj, direction = min(
            aligned, key=lambda value: value[0])
        if spell_ready:
            assert spell is not None
            await client.clear_actions()
            await client.set_combat(False)
            await client.target(x, y, obj.target_id)
            self.remember_engagement(client, x, y, obj.target_id)
            await client.fire(direction, spell.tag)
            self.commit_offensive_spell(client, spell)
            self._last_pull_at = self._last_action = now
            return True
        if launcher is not None and ammo is not None:
            if not launcher.flags & c.ITEM_APPLIED:
                await client.apply(launcher.tag)
            else:
                await client.clear_actions()
                await client.set_combat(False)
                await client.target(x, y, obj.target_id)
                self.remember_engagement(client, x, y, obj.target_id)
                await client.fire(direction, launcher.tag)
                self._last_pull_at = now
            self._pull_launcher_tag = launcher.tag
            self._last_action = now
            return True
        return False

    def retreat_ack_pending(self, client: AtrinikClient,
                            now: float | None = None) -> bool:
        """Resolve or wait for the one outstanding direct retreat step."""
        m = client.state.map
        now = time.monotonic() if now is None else now
        position = (m.path, m.world_x, m.world_y)
        if self._retreat_attempt is not None:
            path, from_x, from_y, to_x, to_y, sent_at = self._retreat_attempt
            if position == (path, to_x, to_y):
                self._retreat_attempt = None
            elif (position != (path, from_x, from_y) or
                  now - sent_at >= movement_ack_timeout(client)):
                self._retreat_blocked.add((path, to_x, to_y))
                self._retreat_attempt = None
            else:
                return True
        return False

    async def low_health_retreat(self, client: AtrinikClient) -> bool:
        """Run one tile away from the visible pack, learning blocked steps."""
        m = client.state.map
        now = time.monotonic()
        if self.retreat_ack_pending(client, now):
            return True
        cx, cy = m.width // 2, m.height // 2
        engaged_id = self._engaged_target[0] if self._engaged_target else 0
        proven_attackers = recent_hostile_attackers(client)
        visible_threats = [
            (x, y, obj) for x, y, obj in m.targets(friendly=False)
            if (not self.neutral_targets or
                obj.target_id == engaged_id or
                any(name in (
                    map_object_semantic(client, obj)
                ).casefold() for name in proven_attackers))]
        breakout = next((
            (x, y, obj) for x, y, obj in visible_threats
            if obj.target_id == self._cornered_breakout_target and
            max(abs(x - cx), abs(y - cy)) <= 1), None)
        if breakout is not None:
            # With no walkable escape square, repeatedly alternating one
            # forced swing with combat-off retreat ticks makes neither
            # progress and lets the pack win. Keep attacking the selected
            # adjacent blocker until it dies or moves far enough to expose
            # the exit. Safety may still spend a newly regenerated heal on
            # intervening ticks, after which this latch restores the breakout.
            x, y, obj = breakout
            self.remember_engagement(client, x, y, obj.target_id)
            await client.clear_actions()
            await client.target(x, y, obj.target_id)
            await client.set_combat(True, force=True)
            self._last_action = now
            return True
        if self._cornered_breakout_target:
            self._cornered_breakout_target = 0
        threats = [(x, y) for x, y, _ in visible_threats]
        origin = None
        if self._engaged_target is not None:
            _, target_path, target_x, target_y, observed_at = (
                self._engaged_target)
            origin = (target_path, target_x, target_y, observed_at)
        elif (self._last_threat_origin is not None and
              now - self._last_threat_origin[3] <= 20.0):
            origin = self._last_threat_origin
        if not threats and origin is not None:
            target_path, target_x, target_y, _ = origin
            if target_path == m.path:
                threats = [(cx + target_x - m.world_x,
                            cy + target_y - m.world_y)]
            else:
                current_grid = re.search(
                    r"/world_(-?\d+)_(-?\d+)(?:_|$)", m.path)
                target_grid = re.search(
                    r"/world_(-?\d+)_(-?\d+)(?:_|$)", target_path)
                if current_grid and target_grid:
                    grid_dx = ((int(current_grid.group(1)) >
                                int(target_grid.group(1))) -
                               (int(current_grid.group(1)) <
                                int(target_grid.group(1))))
                    grid_dy = ((int(current_grid.group(2)) >
                                int(target_grid.group(2))) -
                               (int(current_grid.group(2)) <
                                int(target_grid.group(2))))
                    # Put a synthetic threat toward the previous map seam;
                    # maximizing separation then runs farther into this map.
                    threats = [(cx - grid_dx, cy - grid_dy)]
        if not threats:
            return False
        if (len(threats) == 1 and self._last_retreat_step_at and
                now - self._last_retreat_step_at < max(
                    2.0, movement_ack_timeout(client))):
            # A fighting retreat needs a dodge cadence, not continuous
            # movement. Give the committed monster time to follow and Sera’s
            # automatic attack/heal cycle time to resolve between steps.
            return True
        choices = []
        mobility_blocked = {
            (x, y) for path, x, y in self._retreat_blocked
            if path == m.path}
        mobility_blocked.add((m.world_x, m.world_y))
        mobility_blocked.update(
            (m.world_x + x - cx, m.world_y + y - cy)
            for x, y in threats)
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            if not direction:
                continue
            view_x, view_y = cx + dx, cy + dy
            world_x, world_y = m.world_x + dx, m.world_y + dy
            if ((m.path, world_x, world_y) in self._retreat_blocked or
                    not self.within_farm_map(client, view_x, view_y) or
                    not self.retreat_tile_walkable(
                        m.path, world_x, world_y)):
                continue
            tile = m.tiles.get((view_x, view_y))
            if tile is not None and any(
                    obj.target_id for obj in tile.objects.values()):
                continue
            separation = min(max(abs(view_x - x), abs(view_y - y))
                             for x, y in threats)
            escape_depth, escape_area = retreat_mobility(
                self.map_node, (world_x, world_y),
                blocked=mobility_blocked)
            choices.append((int(escape_depth >= 2), separation,
                            escape_depth, escape_area, random.random(),
                            direction, world_x, world_y))
        if not choices:
            # If every legal exit is occupied, continued flee commands can
            # never succeed. Fight the adjacent blocker to open one escape
            # square, while keeping the rest of the pack out of combat.
            blockers = [
                (max(abs(x - cx), abs(y - cy)), x, y, obj)
                for x, y, obj in visible_threats
                if max(abs(x - cx), abs(y - cy)) <= 1]
            if blockers:
                _, x, y, obj = min(blockers, key=lambda entry: entry[0])
                self._cornered_breakout_target = obj.target_id
                self.remember_engagement(client, x, y, obj.target_id)
                await client.clear_actions()
                await client.target(x, y, obj.target_id)
                await client.set_combat(True, force=True)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("cornered-breakout",
                             f"blocking_target={obj.target_id}")
                self._last_action = now
                return True
            return False
        (viable, separation, escape_depth, escape_area, _, direction,
         world_x, world_y) = max(choices)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "retreat-choice",
                f"direction={direction} separation={separation} "
                f"escape_depth={escape_depth} escape_area={escape_area} "
                f"continuation={viable}")
        await client.clear_actions()
        engaged_id = self._engaged_target[0] if self._engaged_target else 0
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        healing_costs = [
            int(item.extra.get("cost", 0) or 0)
            for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL and
            re.search(r"heal", item.name, re.I) and
            int(item.extra.get("cost", 0) or 0) > 0
        ]
        next_heal = min(healing_costs, default=0)
        sp = int(client.state.stats.get("sp", 0) or 0)
        emergency_disengage = (
            hp / maxhp <= self.safety.flee_below or
            (next_heal > 0 and sp < next_heal)
        )
        if emergency_disengage and recorder is not None:
            recorder(
                "emergency-disengage",
                f"hp={hp}/{maxhp} sp={sp} next-heal={next_heal} "
                f"target={engaged_id}")
        defensive = next((
            (x, y, obj) for x, y, obj in m.targets(friendly=False)
            if not emergency_disengage and len(threats) == 1 and
            obj.target_id == engaged_id), None)
        if defensive is None:
            await client.set_combat(False)
        else:
            x, y, obj = defensive
            await client.target(x, y, obj.target_id)
            await client.set_combat(True, force=True)
        # The protocol run bit is persistent directional running, not a
        # faster single step. Use one controlled step so fleeing cannot cross
        # map seams or plough through another pack before the next assessment.
        await client.move(direction, run=False)
        self._retreat_attempt = (m.path, m.world_x, m.world_y,
                                 world_x, world_y, now)
        self._last_retreat_step_at = now
        self._last_action = now
        return True

    @staticmethod
    def emergency_recall_candidates(client: AtrinikClient) -> list[Item]:
        """Return paid, identified recall sources in survival-first order."""
        policy = InventoryPolicy()
        candidates = [
            item for item in client.state.inventory
            if re.search(r"\bword of recall\b", item.name, re.I) and
            not item.flags & (c.ITEM_UNPAID | c.ITEM_CURSED | c.ITEM_DAMNED)
            and (
                item.item_type == c.TYPE_SPELL and
                int(item.extra.get("cost", 0) or 0) <=
                int(client.state.stats.get("sp", 0) or 0) or
                item.item_type in (c.TYPE_ROD, c.TYPE_WAND) and
                policy.identified(item)
            )
        ]
        # A rechargeable rod preserves both mana and finite wand charges. A
        # wand is still preferable to spending the last healing mana.
        priority = {c.TYPE_ROD: 0, c.TYPE_WAND: 1, c.TYPE_SPELL: 2}
        return sorted(candidates, key=lambda item: priority[item.item_type])

    async def emergency_recall(self, client: AtrinikClient, *, ratio: float,
                               nearby_count: int,
                               hostile_contact: bool) -> bool:
        """Start Word of Recall during a likely-fatal retreat.

        The server's recall force keeps building while the player moves and
        takes damage, so subsequent ticks continue normal retreat instead of
        standing still. This method returns true only while readying/firing a
        device consumes the current action tick.
        """
        now = time.monotonic()
        if self._emergency_recall_cast_at:
            if client.state.map.path != self._emergency_recall_origin:
                self._emergency_recall_cast_at = 0.0
                self._emergency_recall_source = 0
                return False
            if now - self._emergency_recall_cast_at <= 30.0:
                return False
            # A no-magic square can fizzle the delayed force. Permit a later
            # retry instead of suppressing recall forever in this FarmTask.
            self._emergency_recall_cast_at = 0.0
            self._emergency_recall_source = 0
        if self._emergency_recall_apply_at:
            source = client.state.items.get(self._emergency_recall_source)
            if source is not None and source.flags & c.ITEM_APPLIED:
                await client.fire(0)
                self._emergency_recall_cast_at = now
                self._emergency_recall_apply_at = 0.0
                self._emergency_recall_origin = client.state.map.path
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("emergency-recall", f"via {source.name}")
                return True
            if now - self._emergency_recall_apply_at <= 3.0:
                return True
            self._emergency_recall_apply_at = 0.0
            self._emergency_recall_source = 0
        if not hostile_contact:
            return False
        healing_costs = [
            int(item.extra.get("cost", 0) or 0)
            for item in client.state.inventory
            if item.item_type == c.TYPE_SPELL and
            re.search(r"heal", item.name, re.I) and
            int(item.extra.get("cost", 0) or 0) > 0
        ]
        next_heal = min(healing_costs, default=0)
        sp = int(client.state.stats.get("sp", 0) or 0)
        healing_exhausted = bool(next_heal and sp < next_heal)
        critical = (
            ratio <= 0.35 or
            nearby_count >= 2 and ratio <= self.safety.flee_below or
            healing_exhausted and ratio <= self.safety.flee_below
        )
        if not critical:
            return False
        candidates = self.emergency_recall_candidates(client)
        if not candidates:
            return False
        source = candidates[0]
        self._emergency_recall_source = source.tag
        await client.clear_actions()
        await client.set_combat(False)
        if source.item_type in (c.TYPE_ROD, c.TYPE_WAND):
            if not source.flags & c.ITEM_APPLIED:
                await client.apply(source.tag)
                self._emergency_recall_apply_at = now
                return True
            await client.fire(0)
        else:
            await client.fire(0, source.tag)
        self._emergency_recall_cast_at = now
        self._emergency_recall_origin = client.state.map.path
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("emergency-recall", f"via {source.name}")
        return True

    def remember_safe_position(self, client: AtrinikClient) -> None:
        """Retain a short breadcrumb trail from before hostile contact."""
        m = client.state.map
        now = time.monotonic()
        position = (m.path, m.world_x, m.world_y)
        self._safe_position_history = [
            entry for entry in self._safe_position_history
            if now - entry[3] <= 20.0]
        if (not self._safe_position_history or
                self._safe_position_history[-1][:3] != position):
            self._safe_position_history.append((*position, now))
            self._safe_position_history = self._safe_position_history[-12:]

    async def invisible_contact_retreat(self, client: AtrinikClient) -> bool:
        """Backtrack toward recent safe ground when an attacker is off-screen."""
        m = client.state.map
        now = time.monotonic()
        if self.retreat_ack_pending(client, now):
            return True
        if (self._last_retreat_step_at and
                now - self._last_retreat_step_at < max(
                    2.0, movement_ack_timeout(client))):
            return True
        current = (m.world_x, m.world_y)
        anchors = [entry for entry in self._safe_position_history
                   if entry[0] == m.path and entry[1:3] != current and
                   now - entry[3] <= 20.0]
        direction = 0
        if anchors:
            direction = self.patrol_step_direction(
                current, (anchors[0][1], anchors[0][2]))
        if not direction:
            direction = self._invisible_escape_direction
        if not direction:
            # Login/restart may resume inside an attack before a breadcrumb
            # exists. Move away from the nearest authored farm waypoints; if
            # none exist, any clear controlled step is safer than standing in
            # an unseen melee attack indefinitely.
            choices = []
            for candidate, (dx, dy) in c.DIRECTION_DELTAS.items():
                if not candidate:
                    continue
                view_x, view_y = m.width // 2 + dx, m.height // 2 + dy
                world_x, world_y = m.world_x + dx, m.world_y + dy
                if (not self.within_farm_map(client, view_x, view_y) or
                        (m.path, world_x, world_y) in self._retreat_blocked or
                        not self.retreat_tile_walkable(
                            m.path, world_x, world_y)):
                    continue
                tile = m.tiles.get((view_x, view_y))
                if tile is not None and any(
                        obj.target_id for obj in tile.objects.values()):
                    continue
                separation = (min(max(abs(world_x - x), abs(world_y - y))
                                  for x, y in self.patrol)
                              if self.patrol else 0)
                choices.append((separation, random.random(), candidate))
            if choices:
                direction = max(choices)[2]
        delta = c.DIRECTION_DELTAS.get(direction)
        if not direction or delta is None:
            return False
        dx, dy = delta
        view_x, view_y = m.width // 2 + dx, m.height // 2 + dy
        world_x, world_y = m.world_x + dx, m.world_y + dy
        if (not self.within_farm_map(client, view_x, view_y) or
                (m.path, world_x, world_y) in self._retreat_blocked or
                not self.retreat_tile_walkable(m.path, world_x, world_y)):
            return False
        tile = m.tiles.get((view_x, view_y))
        if tile is not None and any(
                obj.target_id for obj in tile.objects.values()):
            return False
        await client.clear_actions()
        await client.set_combat(False)
        await client.move(direction, run=False)
        self._retreat_attempt = (m.path, m.world_x, m.world_y,
                                 world_x, world_y, now)
        self._last_retreat_step_at = now
        self._invisible_escape_direction = direction
        self._last_action = now
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("invisible-retreat",
                     f"direction={direction} anchor=" +
                     (f"{anchors[0][1]},{anchors[0][2]}" if anchors else
                      "continued"))
        return True

    async def invisible_finisher_hold(
            self, client: AtrinikClient, engaged_id: int,
            health_ratio: float) -> bool:
        """Briefly let a nearly-dead off-screen pursuer enter auto-swing range."""
        target_hp = int(client.state.stats.get("target_hp", 0) or 0)
        action_time = float(client.state.stats.get("action_time", 0) or 0)
        now = time.monotonic()
        if (engaged_id <= 0 or
                getattr(client.state, "target_id", 0) != engaged_id or
                not 0 < target_hp <= 15 or health_ratio < 0.80 or
                action_time > 0.80 or
                now < self._invisible_finish_cooldown_until):
            return False
        if self._invisible_finish_target != engaged_id:
            self._invisible_finish_target = engaged_id
            self._invisible_finish_started_at = now
        if now - self._invisible_finish_started_at > 1.5:
            self._invisible_finish_target = 0
            self._invisible_finish_started_at = 0.0
            self._invisible_finish_cooldown_until = now + 2.0
            return False
        await client.clear_actions()
        await client.set_combat(True, force=True)
        self._last_action = now
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("invisible-finish-hold",
                     f"target={engaged_id} target-hp={target_hp}% "
                     f"action-time={action_time:.2f}")
        return True

    def target_tile_walkable(self, client: AtrinikClient, x: int,
                             y: int) -> bool:
        """Whether the authored player graph permits standing on a target."""
        if self.map_node is None:
            return True
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        return self.map_node.walkable(
            m.world_x + x - cx, m.world_y + y - cy)

    def patrol_step_direction(
            self, start: tuple[int, int], goal: tuple[int, int],
            excluded: set[tuple[int, int]] | None = None) -> int:
        """Find one authored-map step without crossing a coordinate seam."""
        if self.map_node is None or start == goal:
            return 0
        width = self.map_node.width
        height = self.map_node.height
        if not all(0 <= x < width and 0 <= y < height
                   for x, y in (start, goal)):
            return 0
        blocked = set(excluded or ()) | set(self.map_node.occupied)
        blocked.discard(start)
        blocked.discard(goal)
        walkable = bytes(
            (x, y) == start or (
                (x, y) not in blocked and self.map_node.walkable(x, y))
            for y in range(height) for x in range(width))
        result = grid_search(
            width, height, walkable, start[1] * width + start[0],
            (goal[1] * width + goal[0],))
        if result.status != "found" or len(result.path) < 2:
            return 0
        step = result.path[1] % width, result.path[1] // width
        delta = step[0] - start[0], step[1] - start[1]
        return next((direction for direction, value in
                     c.DIRECTION_DELTAS.items() if value == delta), 0)

    async def lure_off_unwalkable_tile(self, client: AtrinikClient, x: int,
                                       y: int, threats: list[tuple]) -> bool:
        """Stop attacking and step back until a flying target follows ashore."""
        if self.map_node is None:
            return False
        m = client.state.map
        now = time.monotonic()
        if self.retreat_ack_pending(client, now):
            return True
        cx, cy = m.width // 2, m.height // 2
        target_world = (m.world_x + x - cx, m.world_y + y - cy)
        target_id = next((
            obj.target_id for _, view_x, view_y, obj in threats
            if (view_x, view_y) == (x, y)), 0)
        if (target_id != self._lure_target_id or
                self._lure_target_world is None):
            self._lure_progress_at = now
        elif target_world != self._lure_target_world:
            # Only movement by the target is progress. Player shoreline
            # oscillation must not reset this timer.
            self._lure_progress_at = now
        if (target_id and self._lure_progress_at and
                now - self._lure_progress_at >= self.LURE_STALL_SECONDS):
            return await self.abandon_unreachable_lure(
                client, target_id, target_world, now)
        if (target_id and target_id == self._lure_target_id and
                target_world == self._lure_target_world and
                now - self._last_lure_step_at < 3.5):
            # Give the flyer time to enter the player-walkable square which
            # was just vacated. Moving again on every position update outruns
            # it forever and turns a one-tile lure into a map-wide chase.
            return True
        candidates = []
        for direction, (dx, dy) in c.DIRECTION_DELTAS.items():
            if not direction:
                continue
            point = (m.world_x + dx, m.world_y + dy)
            if (not self.map_node.walkable(*point) or
                    point in self.map_node.occupied or
                    (m.path, *point) in self._retreat_blocked):
                continue
            tile = m.tiles.get((cx + dx, cy + dy))
            if tile is not None and any(
                    obj.target_id for obj in tile.objects.values()):
                continue
            target_distance = max(
                abs(point[0] - target_world[0]),
                abs(point[1] - target_world[1]))
            pack_distance = min((
                max(abs(cx + dx - tx), abs(cy + dy - ty))
                for _, tx, ty, _ in threats), default=target_distance)
            candidates.append((target_distance, pack_distance,
                               random.random(), direction, point))
        await client.clear_actions()
        await client.set_combat(False)
        if not candidates:
            self._last_action = time.monotonic()
            return True
        _, _, _, direction, point = max(candidates)
        await client.move(direction, run=False)
        self._retreat_attempt = (
            m.path, m.world_x, m.world_y, point[0], point[1], now)
        self._lure_target_id = target_id
        self._lure_target_world = target_world
        self._last_lure_step_at = now
        self._last_retreat_step_at = now
        self._last_action = now
        log.info("luring target from unwalkable tile %s to player ground",
                 target_world)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("lure-to-ground",
                     f"target={target_world[0]},{target_world[1]} "
                     f"step={point[0]},{point[1]}")
        return True

    def target_temporarily_unreachable(self, target_id: int) -> bool:
        now = time.monotonic()
        expiry = self._unreachable_targets.get(target_id, 0.0)
        if expiry <= now:
            self._unreachable_targets.pop(target_id, None)
            return False
        return True

    async def abandon_unreachable_lure(
            self, client: AtrinikClient, target_id: int,
            target_world: tuple[int, int], now: float | None = None) -> bool:
        """Release a passive water target that made no pursuit progress."""
        now = time.monotonic() if now is None else now
        stalled_for = max(0.0, now - self._lure_progress_at)
        await client.clear_actions()
        await client.set_combat(False)
        await client.clear_target()
        self._unreachable_targets[target_id] = (
            now + self.UNREACHABLE_TARGET_COOLDOWN_SECONDS)
        if (self._engaged_target is not None and
                self._engaged_target[0] == target_id):
            self._engaged_target = None
        self._lure_target_id = 0
        self._lure_target_world = None
        self._lure_progress_at = 0.0
        self._last_lure_step_at = 0.0
        self._last_action = now
        log.info(
            "abandoning unreachable water target %s at %s after %.1fs "
            "without target movement", target_id, target_world, stalled_for)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder(
                "water-lure-abandon",
                f"target={target_id} tile={target_world[0]},{target_world[1]} "
                f"stalled={stalled_for:.1f}s")
        return True

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self.status != TaskStatus.RUNNING or client.state.phase != "playing":
            return
        if self.level_until and client.state.stats.get("level", 0) >= self.level_until:
            self.complete()
            return
        if self.item_pattern and self._item_count(client) >= self.quantity:
            self.complete()
            return
        if self.zone and self.zone.casefold() not in (
                client.state.map.path + " " + client.state.map.region).casefold():
            self.fail(f"outside requested zone {self.zone!r}; a route is required")
            return
        if time.monotonic() - self._last_action < 0.30:
            return
        hp = int(client.state.stats.get("hp", 0) or 0)
        maxhp = max(1, int(client.state.stats.get("maxhp", 1) or 1))
        ratio = hp / maxhp
        m = client.state.map
        cx, cy = m.width // 2, m.height // 2
        initial_threats = [
            (max(abs(x - cx), abs(y - cy)), x, y, obj)
            for x, y, obj in m.targets(friendly=False)
            if self.within_farm_map(client, x, y) and
            not self.target_temporarily_unreachable(obj.target_id) and
            not self.ignore_unrequested_peaceful(client, obj)
        ]
        # A kill is terminal evidence for older contact. Keep the longer raw
        # contact window elsewhere for transient selected-target tracking, but
        # do not let a dead monster manufacture off-screen retreat steps before
        # its corpse is collected. Any hit which arrives after the kill still
        # takes precedence and triggers the normal escape path.
        hostile_contact = unresolved_hostile_contact(client, seconds=10.0)
        proven_attackers = recent_hostile_attackers(client)

        def detection_radius(obj: MapObject) -> int:
            semantic = map_object_semantic(client, obj).casefold()
            return max((
                radius for identity, radius in
                self.aggressive_detection_ranges.items()
                if identity and identity in semantic
            ), default=4)

        def active_threat(entry) -> bool:
            obj = entry[3]
            semantic = map_object_semantic(client, obj).casefold()
            direct_detection = entry[0] <= detection_radius(obj)
            current_engaged = (
                self._engaged_target[0] if self._engaged_target else 0)
            if obj.target_id == current_engaged:
                return True
            matched = [
                name for name in proven_attackers
                if _same_semantic_identity(name, semantic)
            ]
            if matched:
                engaged_semantic = next((
                    map_object_semantic(client, candidate[3]).casefold()
                    for candidate in initial_threats
                    if candidate[3].target_id == current_engaged), "")
                if engaged_semantic and any(
                        _same_semantic_identity(name, engaged_semantic)
                        for name in matched):
                    return bool(
                        not self.neutral_targets and
                        not self.authored_peaceful(client, obj) and
                        direct_detection)
                if self.neutral_targets:
                    # Combat messages provide a species name, not an object
                    # ID. If the committed neutral briefly leaves the viewport,
                    # never attribute one same-species hit to every visible
                    # bystander. Select one representative per ambiguous name;
                    # differently named adds still become separate threats.
                    candidates = [
                        candidate for candidate in initial_threats
                        if any(_same_semantic_identity(
                            name, map_object_semantic(
                                client, candidate[3]).casefold())
                            for name in matched)
                    ]
                    representative = min(
                        candidates,
                        key=lambda candidate: (
                            candidate[3].target_id != getattr(
                                client.state, "target_id", 0),
                            candidate[0]),
                        default=None)
                    return bool(
                        representative and
                        obj.target_id == representative[3].target_id)
                return True
            if self.neutral_targets:
                return False
            return (not self.authored_peaceful(client, obj) and
                    direct_detection)

        if not hostile_contact and not initial_threats:
            self.remember_safe_position(client)
        if (hostile_contact and self._engaged_target is None and
                initial_threats):
            _, x, y, obj = min(initial_threats, key=lambda entry: entry[0])
            # An aggressive monster can land its first hit before the normal
            # target scan. Commit it before emergency healing preempts combat.
            self.remember_engagement(client, x, y, obj.target_id)
        engaged_id = self._engaged_target[0] if self._engaged_target else 0
        nearby_count = sum(
            entry[0] <= 4 and active_threat(entry)
            for entry in initial_threats)
        if await self.emergency_recall(
                client, ratio=ratio, nearby_count=nearby_count,
                hostile_contact=hostile_contact):
            self._last_action = time.monotonic()
            return
        if (hostile_contact and self._engaged_target is None and
                not initial_threats):
            # An off-screen first contact can occur just after a map seam.
            # A position recorded moments before the hit is not necessarily
            # safe: once we start escaping, never navigate back toward it.
            last_heal = self.safety._last_heal
            await self.safety.enforce(client)
            if self.safety._last_heal != last_heal:
                self._last_action = time.monotonic()
                return
            now = time.monotonic()
            origin = self._last_threat_origin
            current = (m.world_x, m.world_y)
            older_anchors = [
                entry for entry in self._safe_position_history
                if entry[0] == m.path and entry[1:3] != current]
            if origin is None or now - origin[3] > 20.0:
                if older_anchors:
                    await self.invisible_contact_retreat(client)
                    return
                self._last_threat_origin = (
                    m.path, m.world_x, m.world_y, now)
            if await self.low_health_retreat(client):
                return
            await self.invisible_contact_retreat(client)
            return
        dynamic_flee = max(
            self.safety.flee_below,
            0.94 if nearby_count >= 3 else
            0.80 if nearby_count >= 2 else 0.0)
        isolated_kite = bool(
            self.neutral_targets and nearby_count == 1 and
            self._engaged_target is not None and
            any(entry[3].target_id == self._engaged_target[0]
                for entry in initial_threats) and
            ratio > dynamic_flee)
        finish_before_healing = self.should_finish_before_healing(
            client, initial_threats, nearby_count, ratio)
        if ratio <= dynamic_flee and not finish_before_healing:
            last_heal = self.safety._last_heal
            await self.safety.enforce(client)
            if self.safety._last_heal != last_heal:
                self._last_action = time.monotonic()
                return
            if await self.low_health_retreat(client):
                return
            self._last_action = time.monotonic()
            return
        if (not finish_before_healing and not isolated_kite and
                not await self.safety.enforce(client)):
            self._last_action = time.monotonic()
            return

        cx, cy = client.state.map.width // 2, client.state.map.height // 2
        targets = []
        threats = []
        for x, y, obj in client.state.map.targets(friendly=False):
            if not self.within_farm_map(client, x, y):
                continue
            if self.target_temporarily_unreachable(obj.target_id):
                continue
            if self.ignore_unrequested_peaceful(client, obj):
                continue
            semantic = map_object_semantic(client, obj)
            distance = max(abs(x - cx), abs(y - cy))
            threats.append((distance, x, y, obj))
            world_x = client.state.map.world_x + x - cx
            world_y = client.state.map.world_y + y - cy
            priority_rank = None
            for rank, (spawn_x, spawn_y, named) in enumerate(
                    self.priority_spawns):
                if (named.casefold() in semantic.casefold() or
                        max(abs(world_x - spawn_x),
                            abs(world_y - spawn_y)) <= 1):
                    priority_rank = (rank if priority_rank is None else
                                     min(priority_rank, rank))
                    previous = self._priority_target_ids.get(obj.target_id, rank)
                    self._priority_target_ids[obj.target_id] = min(previous, rank)
            # Some authored monsters disguise themselves until disturbed.
            # Their live face/name therefore cannot match the farm pattern,
            # but a hostile occupying the authored named-spawn point is the
            # exact reroll target and must not be skipped (Fahrgorm appears as
            # a tied bollard while dormant).
            if (self.target_pattern and
                    not self.target_pattern.search(semantic) and
                    priority_rank is None):
                continue
            targets.append((distance, x, y, obj))
        self._visible_target_count = len(targets)
        self.observe_engaged_target(
            client, threats, preserve_transient=True)
        if await self.restore_combat_loadout(client):
            return
        if await self.restore_normal_weapon(
                client, {entry[3].target_id for entry in threats}):
            return
        nearest_threat = min(threats, key=lambda value: value[0], default=None)
        engaged_id = self._engaged_target[0] if self._engaged_target else 0
        engaged = next((entry for entry in threats
                        if entry[3].target_id == engaged_id), None)
        nearby_pack = [
            entry for entry in threats
            if entry[0] <= 4 and active_threat(entry)]
        if len(nearby_pack) >= 2:
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("farm-pack-split", "targets=" + ",".join(
                    str(entry[3].target_id) for entry in nearby_pack))
            await self.low_health_retreat(client)
            return
        if not self.neutral_targets and not engaged_id:
            # Aggressive residents visible beyond their authored acquisition
            # radius are route context, not yet combatants. Continue to the
            # isolated pull square instead of selecting one early and walking
            # directly across intervening detection zones.
            targets = [entry for entry in targets if active_threat(entry)]
        if (engaged_id and engaged is None and
                recent_hostile_contact(client)):
            # Healing temporarily targets the player and viewport scrolling
            # may hide the committed monster. Incoming combat text proves it
            # is still active: keep escaping that attacker and never replace
            # it with newly visible wildlife.
            if await self.invisible_finisher_hold(
                    client, engaged_id, ratio):
                return
            await self.low_health_retreat(client)
            return
        # Finish loot when safe, but never probe a corpse through an attacker
        # already in melee range.
        if (engaged is None and
                (nearest_threat is None or nearest_threat[0] > 2)) and \
                await self.loot_nearby(client):
            return
        if (self.combat_skill_until_level and self.combat_skill and
                not self.training_active(client) and engaged is None and
                not recent_hostile_contact(client) and
                not self._corpse_step_attempt and
                not self._corpse_take_all and
                not self._suspected_corpse_tiles and
                not self._suspected_corpse_probe):
            # The circuit parent can only review a newly reached skill cap
            # after this farm child becomes idle. Do that between completed
            # loot and the next passive pull, otherwise a dense respawn loop
            # can keep reacquiring now-grey targets forever.
            await client.clear_actions()
            await client.set_combat(False)
            self.complete()
            return
        if engaged is not None:
            # Finish the creature deliberately pulled/engaged instead of
            # switching to whichever add happens to step one tile closer.
            targets = [engaged]
        elif nearest_threat is not None and nearest_threat[0] <= 3:
            if self.neutral_targets:
                active = [entry for entry in threats if active_threat(entry)]
                if active:
                    targets = active
            else:
                targets = threats
        if (engaged is None and nearest_threat is not None and
                nearest_threat[0] > 2):
            pull_candidate = min(
                targets, key=lambda value: value[0], default=None)
            finisher_ready = bool(
                pull_candidate and
                self.should_training_finish(client, pull_candidate[3]))
            if not finisher_ready and await self.ranged_pull(client, targets):
                return
            # Distinguish an actually clustered target from an isolated
            # target which merely needs one alignment step for ranged fire.
            candidate = pull_candidate
            clustered = bool(not self.neutral_targets and candidate and any(
                other[3].target_id != candidate[3].target_id and
                max(abs(other[1] - candidate[1]),
                    abs(other[2] - candidate[2])) <= 2
                for other in threats))
            if clustered and await self.low_health_retreat(client):
                return
        if (nearest_threat is not None and nearest_threat[0] <= 2 and
                self._pull_launcher_tag):
            launcher = client.state.items.get(self._pull_launcher_tag)
            self._pull_launcher_tag = 0
            if launcher is not None and launcher.flags & c.ITEM_APPLIED:
                await client.apply(launcher.tag)
                self._last_action = time.monotonic()
                return
        if targets:
            distance, x, y, obj = min(
                targets, key=lambda value: (
                    self._priority_target_ids.get(
                        value[3].target_id, len(self.priority_spawns)),
                    value[0]))
            self.remember_engagement(client, x, y, obj.target_id)
            if getattr(client.state, "target_id", 0) != obj.target_id:
                await client.target(x, y, obj.target_id)
            if not self.target_tile_walkable(client, x, y):
                # Never land a killing blow over water/void: the corpse and
                # rare drop would be unreachable. Keep the target selected,
                # disable auto-melee and make the flyer follow onto shore.
                await self.lure_off_unwalkable_tile(client, x, y, threats)
                return
            self._lure_target_id = 0
            self._lure_target_world = None
            self._lure_progress_at = 0.0
            self._last_lure_step_at = 0.0
            if await self.training_finisher(client, x, y, obj):
                return
            semantic = map_object_semantic(client, obj)
            force = bool(
                self.target_pattern and self.target_pattern.search(semantic))
            await client.set_combat(True, force=force)
            spell_alignment_pending = False
            if self.combat_spell:
                spell = self.primary_combat_spell(client)
                if spell is not None:
                    direction = self.spell_fire_direction(
                        spell, x, y, cx, cy)
                    if (direction is not None and
                            self.offensive_spell_affordable(client, spell)):
                        now = time.monotonic()
                        if now - self._last_spell_at >= max(
                                0.8, float(client.state.stats.get(
                                    "action_time", 0) or 0)):
                            await client.fire(direction, spell.tag)
                            self.commit_offensive_spell(client, spell)
                            self._last_spell_at = now
                        self._last_action = now
                        return
                    spell_alignment_pending = direction is None
            active_visible = [
                entry for entry in threats if active_threat(entry)]
            isolated_caster = bool(
                len(active_visible) == 1 and
                active_visible[0][3].target_id == obj.target_id and
                self.authored_caster(client, obj))
            stationary_caster = bool(
                isolated_caster and
                self.stationary_caster(client, x, y, obj))
            if not spell_alignment_pending:
                if (stationary_caster and
                        await self.caster_orbit(client, x, y, obj)):
                    self._last_action = time.monotonic()
                    return
                if (not stationary_caster and
                        await self.melee_kite(client, x, y, obj)):
                    self._last_action = time.monotonic()
                    return
            if distance <= 1:
                # Target + combat mode drives automatic melee. Movement is
                # reserved for deliberate dodging and positional bonuses.
                await self.melee_position(client, x, y, obj)
            else:
                # One approach/alignment step per safety tick avoids a queued
                # path traversing several unseen aggro radii.
                await self.approach_target(client, x, y, obj)
            self._last_action = time.monotonic()
            return

        if await self.pursue_selected_target_last_seen(
                client, nearest_threat):
            return

        if await self.maintain_inventory(client):
            return

        # A just-seen engaged target may be absent from one scrolling
        # MAP2 frame. Hold position instead of cancelling the pull with
        # a patrol path.
        if self._engaged_target is not None:
            self._last_action = time.monotonic()
            return

        if self.patrol:
            now = time.monotonic()
            position = (client.state.map.path, client.state.map.world_x,
                        client.state.map.world_y)
            if self._patrol_step_attempt is not None:
                path, from_x, from_y, to_x, to_y, sent_at = (
                    self._patrol_step_attempt)
                if position == (path, to_x, to_y):
                    self._patrol_step_attempt = None
                elif (position != (path, from_x, from_y) or
                      now - sent_at >= movement_ack_timeout(client)):
                    self._patrol_blocked.add((path, to_x, to_y))
                    self._patrol_step_attempt = None
                else:
                    return
            world_x, world_y = self.patrol[self._patrol_index % len(self.patrol)]
            if (client.state.map.world_x, client.state.map.world_y) == (world_x, world_y):
                self._patrol_index += 1
                world_x, world_y = self.patrol[self._patrol_index % len(self.patrol)]
            if self.map_node is not None:
                direction = self.patrol_step_direction(
                    (client.state.map.world_x, client.state.map.world_y),
                    (world_x, world_y),
                    ({(x, y) for path, x, y in self._patrol_blocked
                      if path == client.state.map.path} |
                     self.pull_avoidance.get((world_x, world_y), set())))
                if direction:
                    await client.clear_actions()
                    mover = getattr(client, "move", None)
                    if mover is not None:
                        dx, dy = c.DIRECTION_DELTAS[direction]
                        await mover(direction)
                        self._patrol_step_attempt = (
                            client.state.map.path,
                            client.state.map.world_x,
                            client.state.map.world_y,
                            client.state.map.world_x + dx,
                            client.state.map.world_y + dy, now)
                    else:
                        dx, dy = c.DIRECTION_DELTAS[direction]
                        await client.move_to_view(cx + dx, cy + dy)
                else:
                    self._patrol_index += 1
                self._last_action = time.monotonic()
                return
            # Patrol waypoints are stable authored-map coordinates, not
            # viewport cells. This lets a policy survive scrolling viewports.
            x = max(1, min(client.state.map.width - 2,
                           cx + world_x - client.state.map.world_x))
            y = max(1, min(client.state.map.height - 2,
                           cy + world_y - client.state.map.world_y))
        else:
            margin = 3
            if self.map_bounds is not None:
                # Explore in short local hops. Viewport-edge patrol clicks can
                # traverse several aggro radii before the next safety tick.
                world_x = random.randint(
                    max(0, client.state.map.world_x - 3),
                    min(self.map_bounds[0] - 1,
                        client.state.map.world_x + 3))
                world_y = random.randint(
                    max(0, client.state.map.world_y - 3),
                    min(self.map_bounds[1] - 1,
                        client.state.map.world_y + 3))
                x = max(1, min(client.state.map.width - 2,
                               cx + world_x - client.state.map.world_x))
                y = max(1, min(client.state.map.height - 2,
                               cy + world_y - client.state.map.world_y))
            else:
                x = random.randint(
                    margin, client.state.map.width - margin - 1)
                y = random.randint(
                    margin, client.state.map.height - margin - 1)
        await client.move_to_view(x, y)
        self._last_action = time.monotonic()


class BuyGroundItemsTask(BotTask):
    """Buy a named unpaid stack from the shop square underfoot."""

    def __init__(self, item_pattern: str, quantity: int, *,
                 cash_reserve: int | None = None):
        super().__init__(f"buy:{item_pattern}")
        self.pattern = re.compile(item_pattern, re.I)
        self.quantity = max(1, int(quantity))
        self.cash_reserve = (None if cash_reserve is None else
                             max(0, int(cash_reserve)))
        self._initial = 0
        self._goal = 0
        self._feedback_start = 0
        self._attempt_at = 0.0
        self._attempt_inventory_quantity = 0
        self._candidate = 0
        self._examined_at = 0.0
        self._examine_attempts = 0

    def _inventory_quantity(self, client: AtrinikClient) -> int:
        return sum(item.quantity for item in client.state.inventory
                   if self.pattern.search(item.name))

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self._initial = self._inventory_quantity(client)
        self._goal = self._initial + self.quantity
        self._feedback_start = len(client.state.messages)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        feedback = "\n".join(
            entry[3].casefold() for entry in
            client.state.messages[self._feedback_start:] if len(entry) > 3)
        if re.search(r"\b(you lack|not enough money|cannot afford)\b",
                     feedback):
            # A server can place an unaffordable item in inventory but refuse
            # every shop exit. Return it immediately instead of declaring the
            # pickup complete and trapping navigation inside the shop.
            unpaid = next((item for item in client.state.inventory
                           if self.pattern.search(item.name) and
                           item.flags & c.ITEM_UNPAID), None)
            if unpaid is not None:
                await client.move_item(0, unpaid.tag, unpaid.quantity)
            self.fail("shop rejected purchase: insufficient money")
            return
        inventory_quantity = self._inventory_quantity(client)
        if inventory_quantity >= self._goal:
            self.complete()
            return
        if self._attempt_at:
            if inventory_quantity > self._attempt_inventory_quantity:
                # Shop stock is commonly authored in stacks of five.  Treat
                # each authoritative inventory increase as one completed
                # purchase and begin another quote/pickup cycle until the
                # requested reserve is actually present.
                self._attempt_at = 0.0
                self._candidate = 0
                self._examined_at = 0.0
                self._examine_attempts = 0
                self._feedback_start = len(client.state.messages)
            elif time.monotonic() - self._attempt_at > 2.5:
                self.fail("shop purchase produced no item or payment feedback")
                return
            else:
                return
        stock = next((item for item in client.state.ground
                      if self.pattern.search(item.name)), None)
        if stock is None:
            self.fail("requested shop stock is not underfoot")
            return
        now = time.monotonic()
        if self.cash_reserve is not None:
            if not self._candidate:
                self._candidate = stock.tag
                self._feedback_start = len(client.state.messages)
                await client.examine(stock.tag)
                self._examine_attempts = 1
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder("shop-cost-check", stock.name)
                self._examined_at = now
                return
            quoted = "\n".join(
                entry[3] for entry in
                client.state.messages[self._feedback_start:]
                if len(entry) > 3)
            cost = BuyShopUpgradeTask.parse_cost(quoted)
            if cost is None:
                if now - self._examined_at <= 2.0:
                    return
                if self._examine_attempts < 3:
                    self._feedback_start = len(client.state.messages)
                    await client.examine(self._candidate)
                    self._examine_attempts += 1
                    self._examined_at = now
                    recorder = getattr(client, "record_action", None)
                    if recorder is not None:
                        recorder(
                            "shop-cost-check-retry",
                            f"tag={self._candidate} attempt="
                            f"{self._examine_attempts}/3")
                    return
                self.fail("shop examination did not report a price")
                return
            wallet = BuyShopUpgradeTask.wallet_value(client)
            if cost > max(0, wallet - self.cash_reserve):
                self.fail(
                    f"shop item costs {cost}, wallet {wallet}, "
                    f"reserve {self.cash_reserve}")
                return
            stock = client.state.items.get(self._candidate)
            if stock is None or stock.location != 0:
                self.fail("shop stock disappeared before purchase")
                return
        remaining = self._goal - self._inventory_quantity(client)
        if self.cash_reserve is not None:
            BuyShopUpgradeTask.account_purchase(client, cost)
        await client.move_item(
            client.state.player_tag, stock.tag,
            min(stock.quantity, remaining))
        self._attempt_inventory_quantity = inventory_quantity
        self._attempt_at = now


class BuyShopUpgradeTask(BotTask):
    """Buy one meaningful, identified and budgeted upgrade underfoot."""

    coin_values = COIN_VALUES

    def __init__(self, *, min_improvement: int = 25,
                 cash_reserve: int = 1_000,
                 spend_fraction: float = 0.25,
                 allow_launchers: bool = True):
        super().__init__("buy-shop-upgrade")
        self.min_improvement = max(1, int(min_improvement))
        self.cash_reserve = max(0, int(cash_reserve))
        self.spend_fraction = max(0.01, min(1.0, float(spend_fraction)))
        self.allow_launchers = bool(allow_launchers)
        self.policy = InventoryPolicy()
        self._candidate = 0
        self._feedback_start = 0
        self._examined_at = 0.0
        self._move_at = 0.0
        self._rejected: set[int] = set()
        self._inventory_before: set[int] = set()
        self._purchase_name = ""
        self._purchase_face = 0

    @classmethod
    def parse_cost(cls, text: str) -> int | None:
        match = re.search(r"would cost you ([^.]+)\.", text, re.I)
        if match is None:
            return None
        total = 0
        for amount, denomination in re.findall(
                r"(\d+)\s+(copper|silver|gold|jade|mithril|amber)",
                match.group(1), re.I):
            total += int(amount) * cls.coin_values[denomination.casefold()]
        return total or None

    @classmethod
    def wallet_value(cls, client: AtrinikClient) -> int:
        return (cls.carried_wallet_value(client) +
                int(getattr(client.state, "bank_balance", 0) or 0))

    @classmethod
    def carried_wallet_value(cls, client: AtrinikClient) -> int:
        total = 0
        for item in client.state.inventory:
            if item.item_type != c.TYPE_MONEY:
                continue
            denomination = next((name for name in cls.coin_values
                                 if name in item.name.casefold()), None)
            if denomination is not None:
                total += cls.coin_values[denomination] * item.quantity
        return total

    @classmethod
    def account_purchase(cls, client: AtrinikClient, cost: int) -> None:
        """Reserve the bank-funded portion until inventory payment lands."""
        bank_spend = max(0, int(cost) - cls.carried_wallet_value(client))
        if bank_spend and getattr(client.state, "bank_balance_known", False):
            balance = max(
                0, int(client.state.bank_balance) - bank_spend)
            setter = getattr(client, "set_bank_balance", None)
            if setter is not None:
                setter(balance)
            else:
                client.state.bank_balance = balance

    @staticmethod
    def _prototype(client: AtrinikClient, item: Item
                   ) -> EquipmentPrototype | None:
        face = getattr(client, "faces", {}).get(item.face, "").casefold()
        candidates = equipment_face_catalog().get(face, ())
        if item.item_type:
            candidates = tuple(
                value for value in candidates
                if value.item_type == item.item_type)
        identities = {(value.item_type, value.item_skill)
                      for value in candidates}
        if len(identities) != 1:
            return None
        return max(candidates, key=lambda value: value.base_score,
                   default=None)

    def _hydrate_ground_type(self, client: AtrinikClient, item: Item
                             ) -> EquipmentPrototype | None:
        prototype = self._prototype(client, item)
        if prototype is None:
            return None
        if not item.item_type:
            item.item_type = prototype.item_type
        item.extra.setdefault("inferred_item_skill", prototype.item_skill)
        item.extra.setdefault("inferred_skill_name", prototype.skill_name)
        item.extra.setdefault("inferred_item_level", prototype.item_level)
        return prototype

    def _skill_name(self, client: AtrinikClient, item: Item) -> str:
        inferred = str(item.extra.get("inferred_skill_name", ""))
        if inferred:
            return inferred.casefold()
        skill = client.state.items.get(item.required_skill_tag)
        return skill.name.casefold() if skill is not None else ""

    def _structurally_eligible(self, client: AtrinikClient, candidate: Item
                               ) -> bool:
        prototype = self._hydrate_ground_type(client, candidate)
        if (candidate.item_type not in self.policy.equipment_types or
                candidate.flags & (c.ITEM_CURSED | c.ITEM_DAMNED)):
            return False
        # A configured spell build deliberately keeps its shield readied and
        # never fires physical launchers. Do not spend progression funds on
        # equipment which the combat policy is guaranteed not to use.
        if candidate.item_type == c.TYPE_BOW and not self.allow_launchers:
            return False
        required_level = candidate.required_level or (
            prototype.item_level if prototype is not None else 0)
        if required_level > int(client.state.stats.get("level", 0)):
            return False
        slots = self.policy.equipment_slots.get(candidate.item_type, ())
        current = [client.state.items[tag] for slot in slots
                   if (tag := client.state.equipment.get(slot))
                   in client.state.items]
        if candidate.item_type in (c.TYPE_WEAPON, c.TYPE_BOW) and current:
            candidate_skill = self._skill_name(client, candidate)
            current_skills = {self._skill_name(client, item)
                              for item in current}
            current_skills.discard("")
            if (candidate_skill and current_skills and
                    candidate_skill not in current_skills):
                return False
        return True

    def _gear_score(self, client: AtrinikClient, item: Item) -> int:
        prototype = self._prototype(client, item)
        examined = item.extra.get("examined_gear_score")
        base = (int(examined) if examined is not None else
                prototype.base_score if prototype is not None else 0)
        return self.policy.gear_score(item) + base

    def _upgrade_score(self, client: AtrinikClient, candidate: Item) -> int:
        if (not self._structurally_eligible(client, candidate) or
                not self.policy.identified(candidate)):
            return -1
        slots = self.policy.equipment_slots.get(candidate.item_type, ())
        current = [client.state.items[tag] for slot in slots
                   if (tag := client.state.equipment.get(slot))
                   in client.state.items]
        if len(slots) == 1:
            # A useful one-slot item may be carried but intentionally not
            # readied yet (notably a bow awaiting ammunition/training). Count
            # the best owned copy as the purchase baseline so a resumed sweep
            # cannot buy the same apparent "empty-slot upgrade" repeatedly.
            current_by_tag = {item.tag: item for item in current}
            current_by_tag.update({
                item.tag: item for item in client.state.inventory
                if item.item_type == candidate.item_type and
                not item.flags & c.ITEM_UNPAID and
                self.policy.identified(item)
            })
            current = list(current_by_tag.values())
        scores = [self._gear_score(client, item) for item in current]
        baseline = ((max(scores) if len(slots) == 1 else min(scores))
                    if scores else 0)
        improvement = self._gear_score(client, candidate) - baseline
        return improvement if improvement >= self.min_improvement else -1

    def _select(self, client: AtrinikClient) -> Item | None:
        candidates = [item for item in client.state.ground
                      if item.flags & c.ITEM_UNPAID and
                      item.tag not in self._rejected]
        known = [item for item in candidates
                 if self._upgrade_score(client, item) >= 0]
        if known:
            return max(known, key=lambda item: self._upgrade_score(client, item))
        # Ground packets omit type/quality. Examine only stock whose face maps
        # unambiguously to compatible equipment; the examine response must
        # prove identification before it can be purchased.
        unknown = [item for item in candidates
                   if item.quality == 255 and
                   self._prototype(client, item) is not None and
                   self._structurally_eligible(client, item)]
        return max(unknown, key=lambda item: self._prototype(
            client, item).base_score, default=None)

    def _apply_examine_metadata(self, client: AtrinikClient, candidate: Item,
                                feedback: str) -> bool:
        quality = re.search(
            r"Qua:\s*(\d+)\s+Con:\s*(Indestructible|\d+)",
            feedback, re.I)
        if quality is not None:
            candidate.quality = int(quality.group(1))
            candidate.condition = (candidate.quality
                                   if quality.group(2).casefold() ==
                                   "indestructible" else
                                   int(quality.group(2)))
        level = re.search(
            r"needs a level of (\d+)(?: in ([^.]+))? to use", feedback, re.I)
        if level is not None:
            candidate.required_level = int(level.group(1))
            if level.group(2):
                candidate.extra["inferred_skill_name"] = (
                    level.group(2).strip().casefold())
        # Ground-item packets deliberately omit the rolled equipment fields.
        # The examination description is authoritative for the actual shop
        # instance, including magical bonuses, protections and attack speed.
        # Preserve a normalized score on this exact object rather than
        # comparing only its static face/archetype prototype.
        fields = {
            key.casefold(): int(value)
            for key, value in re.findall(
                r"\b(wc|dam|ac|block|absorb)([+-]\d+)%?",
                feedback, re.I)
        }
        damage = fields.get("dam", 0)
        delay = re.search(r"\(([0-9]+(?:\.[0-9]+)?) sec\)",
                          feedback, re.I)
        damage_score = damage * 20
        if candidate.item_type == c.TYPE_WEAPON and delay is not None:
            seconds = max(0.125, float(delay.group(1)))
            damage_score = int(damage * 40 / seconds)
        protection_score = 0
        protections = re.search(
            r"\(Protections:\s*([^)]*)\)", feedback, re.I)
        if protections is not None:
            protection_score = sum(int(value) for value in re.findall(
                r"([+-]\d+)%", protections.group(1)))
        attack_score = 0
        attacks = re.search(r"\(Attacks:\s*([^)]*)\)", feedback, re.I)
        if attacks is not None:
            attack_parts = [
                (name.casefold(), int(value))
                for name, value in re.findall(
                    r"([a-z_ ]+)\s*[+]([0-9]+)%",
                    attacks.group(1), re.I)
            ]
            physical = {"impact", "slash", "cleave", "pierce"}
            # Every ordinary weapon reports its base physical distribution,
            # normally one school at +100%. It is not a +100 bonus and the
            # source-derived baseline does not score it. Count only physical
            # excess above 100 plus genuine secondary attack types so an
            # examined shop weapon is comparable with carried equipment.
            physical_total = sum(
                value for name, value in attack_parts if name.strip() in physical)
            attack_score = max(0, physical_total - 100) + sum(
                value for name, value in attack_parts if name.strip() not in physical)
        stat_matches = re.findall(
            r"\b(Str|Dex|Con|Int|Pow|Cha)([+-]\d+)\b",
            feedback, re.I)
        stat_score = sum(int(value) * 40 for _, value in stat_matches)
        resource_matches = re.findall(
            r"\b(hp|mana)([+-]\d+)\b", feedback, re.I)
        resource_score = sum(int(value) for _, value in resource_matches)
        special_score = 50 * sum(
            marker in feedback.casefold() for marker in (
                "(reflect spells)", "(reflect missiles)",
                "(see invisible)", "(infravision)", "(lifesaving)",
                "(flying)", "(stealth)"))
        if (fields or delay is not None or protections is not None or
                attacks is not None or stat_matches or resource_matches or
                special_score):
            candidate.extra["examined_gear_score"] = (
                damage_score + fields.get("wc", 0) * 10 +
                fields.get("ac", 0) * 20 +
                fields.get("block", 0) * 2 +
                fields.get("absorb", 0) * 2 + protection_score +
                attack_score + stat_score + resource_score + special_score)
            candidate.extra["examined_gear_detail"] = {
                **fields, "protections": protection_score,
                "attacks": attack_score, "stats": stat_score,
                "resources": resource_score, "special": special_score,
            }
        return self.policy.identified(candidate)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        now = time.monotonic()
        if self._move_at:
            item = client.state.items.get(self._candidate)
            arrived = (item is not None and
                       item.location == client.state.player_tag)
            if not arrived:
                arrived = any(
                    candidate.tag not in self._inventory_before and
                    candidate.name.casefold() == self._purchase_name and
                    (not self._purchase_face or
                     candidate.face == self._purchase_face)
                    for candidate in client.state.inventory)
            if arrived:
                self.complete()
            elif now - self._move_at > 2.5:
                self.fail("shop upgrade did not enter inventory")
            return
        if not self._candidate:
            candidate = self._select(client)
            if candidate is None:
                unpaid = [
                    item for item in client.state.ground
                    if item.flags & c.ITEM_UNPAID
                ]
                meaningful = sum(
                    self._upgrade_score(client, item) >= 0 or
                    (item.quality == 255 and
                     self._prototype(client, item) is not None and
                     self._structurally_eligible(client, item))
                    for item in unpaid)
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder(
                        "shop-upgrade-scan",
                        f"stock={len(unpaid)} meaningful={meaningful} "
                        f"budget-rejected={len(self._rejected)} "
                        "result=no-upgrade")
                self.complete()
                return
            self._candidate = candidate.tag
            self._feedback_start = len(client.state.messages)
            await client.examine(candidate.tag)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("shop-upgrade-check", candidate.name)
            self._examined_at = now
            return
        feedback = "\n".join(
            entry[3] for entry in client.state.messages[self._feedback_start:]
            if len(entry) > 3)
        cost = self.parse_cost(feedback)
        if cost is None:
            if now - self._examined_at <= 2.0:
                return
            rejected = client.state.items.get(self._candidate)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "shop-upgrade-skip",
                    f"{rejected.name if rejected is not None else self._candidate} "
                    "reason=no-identified-price")
            self._rejected.add(self._candidate)
            self._candidate = 0
            self._examined_at = 0.0
            return
        candidate = client.state.items.get(self._candidate)
        if candidate is None:
            self.fail("shop upgrade disappeared after examination")
            return
        identified = self._apply_examine_metadata(client, candidate, feedback)
        improvement = self._upgrade_score(client, candidate)
        if not identified or improvement < 0:
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "shop-upgrade-skip",
                    f"{candidate.name} reason=" +
                    ("unidentified" if not identified else "no-improvement"))
            self._rejected.add(self._candidate)
            self._candidate = 0
            self._examined_at = 0.0
            return
        wallet = self.wallet_value(client)
        budget = min(max(0, wallet - self.cash_reserve),
                     int(wallet * self.spend_fraction))
        if cost > budget:
            rejected = client.state.items.get(self._candidate)
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "shop-upgrade-reject",
                    f"{rejected.name if rejected is not None else self._candidate} "
                    f"cost={cost} budget={budget}")
            self._rejected.add(self._candidate)
            self._candidate = 0
            self._examined_at = 0.0
            return
        candidate = client.state.items.get(self._candidate)
        if candidate is None:
            self.fail("shop upgrade disappeared before purchase")
            return
        self._inventory_before = {
            item.tag for item in client.state.inventory}
        self._purchase_name = candidate.name.casefold()
        self._purchase_face = candidate.face
        self.account_purchase(client, cost)
        await client.move_item(
            client.state.player_tag, candidate.tag, candidate.quantity)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("shop-upgrade-buy",
                     f"{candidate.name} cost={cost} budget={budget}")
        self._move_at = now


class BuyDialogueStockTask(BotTask):
    """Inspect an identified dialogue merchant and buy matching stock."""

    LINK = re.compile(r"\[a=([^:\]]*):([^\]]*)\]", re.I)

    def __init__(self, merchant: str, item_pattern: str, *, quantity: int = 1,
                 preferred: tuple[str, ...] = (), cash_reserve: int = 1_000,
                 observation_key: str = "", max_purchases: int = 1,
                 distinct_patterns: tuple[str, ...] = ()):
        super().__init__(f"buy-dialogue-stock:{item_pattern}")
        self.merchant = merchant
        self.item_pattern = re.compile(item_pattern, re.I)
        self.quantity = max(1, int(quantity))
        self.preferred = tuple(value.casefold() for value in preferred)
        self.cash_reserve = max(0, int(cash_reserve))
        self.observation_key = observation_key
        self.max_purchases = max(1, int(max_purchases))
        self.distinct_patterns = tuple(
            re.compile(value, re.I) for value in distinct_patterns)
        self.checked = False
        self.purchased = False
        self.purchase_count = 0
        self._bought_names: set[str] = set()
        self._bought_groups: set[int] = set()
        self._stage = ""
        self._sent_at = 0.0
        self._item_name = ""
        self._cost = 0
        self._inventory_before: set[int] = set()
        self._inventory_quantity_before = 0

    @classmethod
    def _destinations(cls, client: AtrinikClient) -> list[str]:
        interface = client.state.interface
        if interface is None:
            return []
        result = []
        for markup in interface.links:
            match = cls.LINK.search(markup)
            if match is not None:
                result.append(match.group(2))
        return result

    @staticmethod
    def _interface_payload(client: AtrinikClient) -> str:
        interface = client.state.interface
        if interface is None:
            return ""
        parts = [interface.text]
        for item in interface.objects:
            parts.extend((item.name, str(item.extra.get("message", ""))))
        return "\n".join(parts)

    @staticmethod
    def _coin_value(text: str) -> int | None:
        total = sum(
            int(amount) * COIN_VALUES[denomination.casefold()]
            for amount, denomination in re.findall(
                r"(\d+)\s+(copper|silver|gold|jade|mithril|amber)",
                text, re.I)
        )
        return total or None

    def _select_stock(self, client: AtrinikClient,
                      destinations: list[str]) -> str:
        choices = [
            destination[4:] for destination in destinations
            if destination.casefold().startswith("buy ") and
            not re.match(r"buy\s+\d+\s", destination, re.I) and
            self.item_pattern.search(destination[4:]) and
            destination[4:].casefold() not in self._bought_names and
            not any(index in self._bought_groups and pattern.search(
                        destination[4:])
                    for index, pattern in enumerate(self.distinct_patterns))
        ]
        wizardry = next((
            item for item in client.state.inventory
            if item.item_type == c.TYPE_SKILL and
            item.name.casefold() == "wizardry spells"), None)
        wizardry_level = int(
            wizardry.extra.get("level", 0) if wizardry is not None else 0)

        def priority(name: str) -> tuple[int, str]:
            folded = name.casefold()
            preferred = next((
                index for index, pattern in enumerate(self.preferred)
                if pattern in folded), len(self.preferred))
            if self.preferred:
                return preferred, folded
            if wizardry_level >= 12:
                order = ("spellbook", "rod", "wand")
            else:
                order = ("rod", "wand", "spellbook")
            return (next((index for index, word in enumerate(order)
                          if word in folded), len(order)), folded)

        return min(choices, key=priority, default="")

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        now = time.monotonic()
        if not self._stage:
            self._inventory_before = {
                item.tag for item in client.state.inventory}
            client.state.interface = None
            await client.talk("hello", self.merchant)
            self._stage = "stock"
            self._sent_at = now
            return
        if self._stage == "stock":
            destinations = self._destinations(client)
            if not destinations:
                if now - self._sent_at <= 5.0:
                    return
                self.fail("merchant stock dialogue did not arrive")
                return
            self.checked = True
            self._item_name = self._select_stock(client, destinations)
            if not self._item_name:
                self.complete()
                return
            client.state.interface = None
            await client.talk(f"buy {self._item_name}", self.merchant)
            self._stage = "quote"
            self._sent_at = now
            return
        if self._stage == "quote":
            destinations = self._destinations(client)
            quantity = next((
                destination for destination in destinations
                if destination.casefold() ==
                f"buy {self.quantity} {self._item_name}".casefold()), "")
            payload = self._interface_payload(client)
            cost = self._coin_value(payload)
            if not quantity or cost is None:
                if now - self._sent_at <= 5.0:
                    return
                self.fail("merchant utility quote was incomplete")
                return
            wallet = BuyShopUpgradeTask.wallet_value(client)
            total_cost = cost * self.quantity
            if total_cost > max(0, wallet - self.cash_reserve):
                self.fail(
                    f"merchant stock costs {total_cost}, wallet {wallet}, "
                    f"reserve {self.cash_reserve}")
                return
            self._cost = total_cost
            quote_recorder = getattr(client, "record_vendor_quote", None)
            if quote_recorder is not None and self.observation_key:
                quote_recorder(
                    self.observation_key, self._item_name, int(cost))
            self._inventory_quantity_before = sum(
                item.quantity for item in client.state.inventory
                if item.name.casefold() == self._item_name.casefold())
            BuyShopUpgradeTask.account_purchase(client, total_cost)
            client.state.interface = None
            await client.talk(quantity, self.merchant)
            self._stage = "buy"
            self._sent_at = now
            return
        if self._stage == "buy":
            arrived = next((
                item for item in client.state.inventory
                if self.item_pattern.search(item.name) and (
                    item.tag not in self._inventory_before or
                    item.name.casefold() == self._item_name.casefold() and
                    sum(candidate.quantity
                        for candidate in client.state.inventory
                        if candidate.name.casefold() ==
                        self._item_name.casefold()) >
                    self._inventory_quantity_before)), None)
            if arrived is not None:
                self.purchased = True
                self.purchase_count += 1
                self._bought_names.add(self._item_name.casefold())
                self._bought_groups.update(
                    index for index, pattern in enumerate(
                        self.distinct_patterns)
                    if pattern.search(self._item_name))
                recorder = getattr(client, "record_action", None)
                if recorder is not None:
                    recorder(
                        "dialogue-stock-buy",
                        f"{arrived.name} cost={self._cost}")
                if self.purchase_count < self.max_purchases:
                    # Dynamic merchants may expose several independently
                    # rolled utility items. Re-open the stock after each
                    # successful purchase so one daily visit can inspect all
                    # distinct useful rolls without buying duplicate names.
                    self._stage = ""
                    self._sent_at = 0.0
                    self._item_name = ""
                    self._cost = 0
                else:
                    self.complete()
                return
            payload = self._interface_payload(client).casefold()
            if "don't have enough money" in payload or \
                    "do not have enough money" in payload:
                self.fail("merchant rejected stock purchase")
            elif now - self._sent_at > 8.0:
                self.fail("merchant stock did not enter inventory")


@dataclass(slots=True)
class JunkPolicy:
    patterns: tuple[str, ...] = ()
    tags: frozenset[int] = frozenset()
    keep_patterns: tuple[str, ...] = (
        "quest", "key", "unique", "talisman", "mana crystal", "ring", "amulet"
    )

    def junk(self, item: Item) -> bool:
        name = item.name.casefold()
        if item.flags & (c.ITEM_APPLIED | c.ITEM_LOCKED):
            return False
        if not InventoryPolicy().identified(item):
            return False
        if InventoryPolicy().preserve(item):
            return False
        if any(re.search(pattern, name) for pattern in self.keep_patterns):
            return False
        return (item.tag in self.tags or
                bool(self.patterns) and any(re.search(pattern, name)
                                            for pattern in self.patterns))


class SellJunkTask(BotTask):
    """Sell configured junk by dropping it on an Atrinik shop floor."""

    def __init__(self, merchant: str, policy: JunkPolicy):
        super().__init__(f"sell-junk:{merchant}")
        self.merchant = merchant
        self.policy = policy
        self._queue: list[int] = []
        self._last_action = 0.0
        self._pending: int | None = None
        self._feedback_start = 0

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self._queue = [item.tag for item in client.state.inventory
                       if self.policy.junk(item)]

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self._pending is not None:
            if await _confirm_shop_sale(self, client):
                self._pending = None
            return
        while self._queue and self._queue[0] not in client.state.items:
            self._queue.pop(0)
        if not self._queue:
            self.complete()
            return
        if time.monotonic() - self._last_action < 0.6:
            return
        item = client.state.items[self._queue.pop(0)]
        if item.location != client.state.player_tag:
            self.fail(f"{item.name} is no longer in inventory")
            return
        self._pending = item.tag
        self._feedback_start = len(client.state.messages)
        await client.move_item(0, item.tag, item.quantity)
        self._last_action = time.monotonic()


class SellItemsTask(BotTask):
    """Sell selected inventory items by dropping them on a shop floor."""

    def __init__(self, merchant: str, tags: tuple[int, ...]):
        super().__init__(f"sell-items:{merchant}")
        self.merchant = merchant
        self.tags = list(dict.fromkeys(tags))
        self._last_action = 0.0
        self._pending: int | None = None
        self._feedback_start = 0

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if self._pending is not None:
            if await _confirm_shop_sale(self, client):
                self._pending = None
            return
        if time.monotonic() - self._last_action < 0.6:
            return
        while self.tags and self.tags[0] not in client.state.items:
            self.tags.pop(0)
        if not self.tags:
            self.complete()
            return
        item = client.state.items[self.tags.pop(0)]
        if item.location != client.state.player_tag:
            self.fail(f"{item.name} is no longer in inventory")
            return
        if item.flags & (c.ITEM_APPLIED | c.ITEM_LOCKED):
            self.fail(f"refusing to sell applied or locked item {item.name}")
            return
        if not InventoryPolicy().identified(item):
            self.fail(f"refusing to sell unidentified item {item.name}")
            return
        self._pending = item.tag
        self._feedback_start = len(client.state.messages)
        await client.move_item(0, item.tag, item.quantity)
        self._last_action = time.monotonic()


async def _confirm_shop_sale(task: SellJunkTask | SellItemsTask,
                             client: AtrinikClient) -> bool:
    """Confirm shop-floor payment; recover an item dropped elsewhere."""
    feedback = "\n".join(
        entry[3].casefold() for entry in
        client.state.messages[task._feedback_start:] if len(entry) > 3)
    if "you receive" in feedback or "we're not interested" in feedback:
        return True
    item = client.state.items.get(task._pending)
    if item is None:
        # Removal is authoritative if chat was coalesced or scrolled away.
        return True
    if item.location == client.state.player_tag:
        return False
    if time.monotonic() - task._last_action < 1.5:
        return False
    await client.move_item(client.state.player_tag, item.tag, item.quantity)
    task.fail("item was dropped but not sold; stand on an actual shop floor")
    return False


class BankTask(BotTask):
    def __init__(self, banker: str, deposit: str = "all"):
        super().__init__(f"bank:{banker}")
        self.banker, self.deposit = banker, deposit
        self._sent = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if not self._sent:
            await client.talk(f"deposit {self.deposit}", self.banker)
            self._sent = True
            return
        feedback = [entry[3].casefold()
                    for entry in client.state.messages[-10:]]
        if client.state.interface is not None:
            feedback.append(client.state.interface.text.casefold())
        combined = "\n".join(feedback)
        if "you deposit" in combined or "don't have any money" in combined:
            update_bank_balance(client.state, combined)
            self.complete()
        elif ("didn't quite catch" in combined or
              "don't have that many" in combined):
            self.fail("bank rejected deposit request")


class BankBalanceTask(BotTask):
    """Ask a nearby banker for the authoritative persistent balance."""

    def __init__(self, banker: str):
        super().__init__(f"bank-balance:{banker}")
        self.banker = banker
        self._sent = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        if client.state.bank_balance_known:
            self.complete()
            return
        if not self._sent:
            await client.talk("balance", self.banker)
            self._sent = True
            return
        feedback = "\n".join(
            entry[3] for entry in client.state.messages[-10:]
            if len(entry) > 3)
        if client.state.interface is not None:
            feedback += "\n" + client.state.interface.text
        if update_bank_balance(client.state, feedback):
            self.complete()


class TempleServiceTask(BotTask):
    """Purchase one priest service and verify its status effect disappeared."""

    def __init__(self, priest: str, service: str, condition: str, cost: int):
        super().__init__(f"temple:{service}:{priest}")
        self.priest = priest
        self.service = service
        self.condition = condition.casefold()
        self.cost = max(0, int(cost))
        self._sent_at = 0.0
        self._feedback_start = 0

    def _condition_present(self, client: AtrinikClient) -> bool:
        return any(item.item_type == c.TYPE_FORCE and
                   item.name.casefold() == self.condition
                   for item in client.state.inventory)

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
            self._feedback_start = len(client.state.messages)
        if not self._condition_present(client):
            setter = getattr(client, "set_depletion_points", None)
            if setter is not None and self.condition == "depletion":
                setter(0)
            self.complete()
            return
        feedback = "\n".join(
            entry[3].casefold() for entry in
            client.state.messages[self._feedback_start:] if len(entry) > 3)
        if client.state.interface is not None:
            feedback += "\n" + client.state.interface.text.casefold()
        if "do not have enough money" in feedback:
            self.fail(f"cannot afford temple service costing {self.cost}")
            return
        if not self._sent_at:
            wallet = BuyShopUpgradeTask.wallet_value(client)
            if wallet < self.cost:
                self.fail(f"temple service costs {self.cost}, wallet {wallet}")
                return
            BuyShopUpgradeTask.account_purchase(client, self.cost)
            await client.talk(f"buy {self.service}", self.priest)
            self._sent_at = time.monotonic()
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder("temple-service",
                         f"{self.service} from {self.priest} cost={self.cost}")
            return
        if time.monotonic() - self._sent_at > 8.0:
            self.fail(f"temple service did not remove {self.condition}")


class MassIdentifyTask(BotTask):
    """Buy a smith's flat-price inventory identification service."""

    def __init__(self, smith: str, cost: int, *, cash_reserve: int = 1_000):
        super().__init__(f"mass-identify:{smith}")
        self.smith = smith
        self.cost = max(0, int(cost))
        self.cash_reserve = max(0, int(cash_reserve))
        self._unknown_tags: set[int] = set()
        self._sent_at = 0.0
        self._acknowledged_at = 0.0
        self._feedback_start = 0

    async def tick(self, client: AtrinikClient) -> None:
        policy = InventoryPolicy()
        if self.status == TaskStatus.READY:
            await self.start(client)
            self._feedback_start = len(client.state.messages)
            self._unknown_tags = {
                item.tag for item in client.state.inventory
                if not item.flags & c.ITEM_UNPAID and
                not policy.identified(item)
            }
        remaining = [
            tag for tag in self._unknown_tags
            if tag in client.state.items and
            not policy.identified(client.state.items[tag])
        ]
        if self._unknown_tags and not remaining:
            self.complete()
            return
        feedback = "\n".join(
            entry[3].casefold() for entry in
            client.state.messages[self._feedback_start:] if len(entry) > 3)
        if client.state.interface is not None:
            feedback += "\n" + client.state.interface.text.casefold()
        if "don't have enough money" in feedback or \
                "do not have enough money" in feedback:
            self.fail(f"cannot afford mass identification costing {self.cost}")
            return
        if self._sent_at and not self._acknowledged_at and \
                "thank you for your business" in feedback:
            # A large inventory is described back to the client one item at a
            # time after the service succeeds.  Keep waiting for those item
            # updates instead of treating that resynchronization as failure.
            self._acknowledged_at = time.monotonic()
        if not self._unknown_tags:
            self.complete()
            return
        if not self._sent_at:
            wallet = BuyShopUpgradeTask.wallet_value(client)
            required = self.cost + self.cash_reserve
            if wallet < required:
                self.fail(
                    f"mass identification needs {required} copper including "
                    f"reserve, wallet {wallet}")
                return
            BuyShopUpgradeTask.account_purchase(client, self.cost)
            client.state.interface = None
            await client.talk("buy identify_all", self.smith)
            self._sent_at = time.monotonic()
            recorder = getattr(client, "record_action", None)
            if recorder is not None:
                recorder(
                    "mass-identify",
                    f"smith={self.smith} cost={self.cost} "
                    f"items={len(self._unknown_tags)}")
            return
        timeout = 60.0 if self._acknowledged_at else 10.0
        started = self._acknowledged_at or self._sent_at
        if time.monotonic() - started > timeout:
            self.fail(
                f"mass identification left {len(remaining)} items unknown")


class DepositItemsTask(BotTask):
    """Deposit matching items in an already visible/open house container."""

    def __init__(self, container_name: str, patterns: tuple[str, ...] = (),
                 *, unidentified_only: bool = False,
                 valuable_only: bool = False):
        super().__init__(f"deposit:{container_name}")
        self.container_name = container_name.casefold()
        self.patterns = tuple(re.compile(pattern, re.I) for pattern in patterns)
        self.unidentified_only = unidentified_only
        self.valuable_only = valuable_only
        self._opened = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        container = next((item for item in (*client.state.ground,
                                            *client.state.inventory)
                          if self.container_name in item.name.casefold()), None)
        if container is None:
            self.fail("storage container is not visible")
            return
        if not self._opened and not container.flags & c.ITEM_CONTAINER_OPEN:
            await client.apply(container.tag)
            self._opened = True
            return
        policy = InventoryPolicy()
        items = [
            item for item in client.state.inventory
            if (self.unidentified_only and
                policy.apartment_unidentified(item)) or
               (self.valuable_only and policy.apartment_valuable(item)) or
               (not self.unidentified_only and not self.valuable_only and
                any(pattern.search(item.name) for pattern in self.patterns))
        ]
        # Unidentified valuables may have been locked by an older policy.
        # Unlock one first, then deposit it on the next authoritative update.
        locked = next((item for item in items
                       if item.flags & c.ITEM_LOCKED and
                       not item.flags & c.ITEM_APPLIED), None)
        if locked is not None:
            await client.lock_item(locked.tag)
            return
        items = [item for item in items
                 if not item.flags & (c.ITEM_APPLIED | c.ITEM_LOCKED)]
        if not items:
            self.complete()
            return
        for item in items[:5]:
            await client.move_item(container.tag, item.tag, item.quantity)


class RetrieveItemsTask(BotTask):
    """Retrieve matching items from an already visible house container."""

    def __init__(self, container_name: str, patterns: tuple[str, ...], *,
                 require_match: bool = False):
        super().__init__(f"retrieve:{container_name}")
        self.container_name = container_name.casefold()
        self.patterns = tuple(re.compile(pattern, re.I) for pattern in patterns)
        self.require_match = bool(require_match)
        self._matched = False
        self._opened = False
        self._opened_at = 0.0
        self._feedback_start = 0
        self._reopen_attempted = False

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        container = next((item for item in (*client.state.ground,
                                            *client.state.inventory)
                          if self.container_name in item.name.casefold()), None)
        if container is None:
            self.fail("storage container is not visible")
            return
        now = time.monotonic()
        contents_known = container.tag in client.state.inventories
        if not self._opened:
            if contents_known:
                self._opened = True
                self._opened_at = now - 1.0
            else:
                # The ground item's open-face flag is shared map state: after
                # a reconnect it can be set even though this player has no
                # active container. Applying that chest opens it rather than
                # closing it. Therefore always request it once and use the
                # authoritative item packet (or explicit close feedback
                # below), never infer player state from the visual flag.
                self._feedback_start = len(client.state.messages)
                await client.apply(container.tag)
                self._opened = True
                self._opened_at = now
                return
        # Never equate a not-yet-delivered inventory with an empty chest.
        if not contents_known:
            feedback = "\n".join(
                entry[3].casefold() for entry in
                client.state.messages[self._feedback_start:]
                if len(entry) > 3)
            if (not self._reopen_attempted and
                    re.search(r"\byou (?:close|leave) (?:the )?chest\b",
                              feedback)):
                # This process really did already have the chest selected;
                # the first application closed it. Apply once more to open it
                # and establish a fresh response window.
                self._reopen_attempted = True
                self._feedback_start = len(client.state.messages)
                await client.apply(container.tag)
                self._opened_at = now
                return
            if now - self._opened_at > 5.0:
                self.fail("storage container contents did not load")
            return
        if now - self._opened_at < 0.5:
            return
        items = [
            item for tag in client.state.inventories.get(container.tag, [])
            if (item := client.state.items.get(tag)) is not None and
            any(pattern.search(item.name) for pattern in self.patterns)
        ]
        if not items:
            if self.require_match and not self._matched:
                self.fail("required storage item was not found")
            else:
                self.complete()
            return
        self._matched = True
        for item in items[:5]:
            await client.move_item(
                client.state.player_tag, item.tag, item.quantity)


class BindSavebedTask(BotTask):
    """Apply an underfoot apartment bed and verify server confirmation."""

    CONFIRM = re.compile(r"save bed location is updated", re.I)

    def __init__(self):
        super().__init__("bind-apartment-savebed")
        self.message_index = 0
        self.applied_at = 0.0
        self.arrived_at = 0.0

    async def start(self, client: AtrinikClient) -> None:
        await super().start(client)
        self.message_index = len(client.state.messages)
        self.arrived_at = time.monotonic()

    async def tick(self, client: AtrinikClient) -> None:
        if self.status == TaskStatus.READY:
            await self.start(client)
        for entry in client.state.messages[self.message_index:]:
            text = entry[3] if len(entry) > 3 else ""
            if self.CONFIRM.search(text):
                setter = getattr(client, "set_apartment_bed_bound", None)
                if setter is not None:
                    setter(True)
                else:
                    client.state.apartment_bed_bound = True
                self.complete()
                return
        self.message_index = len(client.state.messages)
        now = time.monotonic()
        if self.applied_at:
            if now - self.applied_at > 4.0:
                self.fail("savebed application was not confirmed")
            return
        bed = next((item for item in client.state.ground
                    if "bed to reality" in item.name.casefold() or
                    item.item_type == 106), None)
        if bed is None:
            if now - self.arrived_at > 3.0:
                self.fail("apartment bed is not underfoot")
            return
        await client.apply(bed.tag)
        recorder = getattr(client, "record_action", None)
        if recorder is not None:
            recorder("bind-savebed", bed.name)
        self.applied_at = now


class TaskEngine:
    def __init__(self, client: AtrinikClient, tick_rate: float = 0.25):
        self.client = client
        self.tick_rate = tick_rate
        self.task: BotTask | None = None
        self.last_task: dict[str, str | float] | None = None
        self.running = True
        self.inventory_policy = InventoryPolicy()
        self.idle_safety = SafetyPolicy()
        self._normalized_stack_locks: set[int] = set()

    def set_task(self, task: BotTask | None) -> None:
        self.task = task
        if task is not None:
            recorder = getattr(self.client, "record_action", None)
            if recorder is not None:
                recorder("task-start", task.name)

    def chat_context(self) -> dict[str, str]:
        """Summarize active intent for factual player-chat responses."""
        context = {"destination": "", "combat_skill": "", "farm_target": ""}
        pending = [self.task]
        seen = set()
        while pending:
            current = pending.pop(0)
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if current.__class__.__name__ == "NavigateTask":
                destination = str(getattr(current, "destination", ""))
                if destination:
                    context["destination"] = destination
            if isinstance(current, FarmTask):
                if current.combat_skill:
                    context["combat_skill"] = current.combat_skill
                if current.target_pattern is not None:
                    context["farm_target"] = current.target_pattern.pattern
            for attribute in ("child", "navigation", "task"):
                child = getattr(current, attribute, None)
                if child is not None:
                    pending.append(child)
        return context

    async def _normalize_inventory_locks(self) -> bool:
        if self.client.state.phase != "playing":
            return False
        for item in self.client.state.inventory:
            if (item.tag not in self._normalized_stack_locks and
                    item.flags & c.ITEM_LOCKED and
                    self.inventory_policy.stackable(item)):
                await self.client.lock_item(item.tag)
                self._normalized_stack_locks.add(item.tag)
                log.info("unlocked stackable inventory item %s x%s",
                         item.name, item.quantity)
                return True
        return False

    async def _protect_idle(self) -> bool:
        """Keep an idle or just-completed character alive between tasks."""
        if (self.task is not None or
                self.client.state.phase != "playing"):
            return False
        self.client.action_context = "idle-safety"
        try:
            return not await self.idle_safety.enforce(self.client)
        finally:
            self.client.action_context = ""

    async def run(self) -> None:
        while self.running:
            if await self._normalize_inventory_locks():
                await asyncio.sleep(self.tick_rate)
                continue
            if await self._protect_idle():
                await asyncio.sleep(self.tick_rate)
                continue
            if self.task is not None:
                self.client.action_context = self.task.name
                try:
                    await self.task.tick(self.client)
                except Exception as exc:
                    log.exception("task %s failed", self.task.name)
                    self.task.fail(str(exc))
                if self.task.status in (TaskStatus.COMPLETE, TaskStatus.FAILED):
                    log.info("task %s ended: %s %s", self.task.name,
                             self.task.status.value, self.task.error)
                    self.last_task = {
                        "name": self.task.name,
                        "status": self.task.status.value,
                        "error": self.task.error,
                        "started_at": self.task.started_at,
                        "ended_at": time.time(),
                    }
                    recorder = getattr(self.client, "record_action", None)
                    if recorder is not None:
                        recorder(
                            "task-end",
                            f"{self.task.name} {self.task.status.value}"
                            + (f": {self.task.error}"
                               if self.task.error else ""))
                    self.task = None
                self.client.action_context = ""
            await asyncio.sleep(self.tick_rate)
