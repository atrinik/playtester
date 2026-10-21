import asyncio
import collections
import json
import time
import tempfile
import shutil
import unittest
from unittest.mock import patch
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

from .client import logical_map_path, map_object_visual_name
from .protocol import Cursor, Packet, ProtocolError
from .transport import (QUIC_ALPN, QUIC_GAME_STREAM_PREFACE,
                        normalize_certificate_sha256)
from .quests import dialogue_choices, flatten_parts, load_catalog, parse_quest_book
from .navigation import (BuySpellTask, FarmCircuitTask, FoodProfile,
                         MapEdge, MapNode,
                         MorgeeanShipKeyTask, NamedSpawn,
                         NavigateTask, NavigateThenTask, ServerGameClock,
                         ShopUpgradeSweepTask, WorldGraph, _coordinate_name,
                         content_attr, CONTENT_ROOT)
from .catalog_quest_tasks import (ApplyAtTask, CatalogQuestTask, DialogAtTask,
                                  AcquireContainerItemTask,
                                  AcquireContainerItemsTask,
                                  IncunaApartmentTutorialTask,
                                  KeyThenContainerTask, LostMemoriesArrivalTask,
                                  NPC, POLICIES, RECOMMENDED_QUEST_ORDER)
from .client import AtrinikClient, ClientConfig
from .tasks import (BankBalanceTask, BankTask, BindSavebedTask,
                    BuyDialogueStockTask, BuyGroundItemsTask,
                    BuyShopUpgradeTask,
                    DepositItemsTask, FarmTask, InventoryCapabilityTask,
                    InventoryPolicy, JunkPolicy,
                    MassIdentifyTask, RetrieveItemsTask, SafetyPolicy,
                    SellItemsTask, SellJunkTask, TaskEngine, TaskStatus,
                    TempleFoodTask, TempleServiceTask,
                    equipment_face_catalog)
from .model import (STARTER_APARTMENT, STARTER_APARTMENT_BED, GameState, Item, MapObject,
                    MapState)
from .model import QuestProgress
from .model import InterfaceState
from .quest_tasks import DialogTask, EscapingDesertedIslandTask
from .web_server import (DashboardState, WebControlServer,
                         experience_progress, experience_thresholds)
from .navigation_spots import NAVIGATION_SPOTS
from . import constants as c
from .autoplay import AutoplayTask


@lru_cache(maxsize=1)
def built_graph():
    return WorldGraph().build()


# Literal circuits for farm-circuit mechanics tests. Production farm spots come
# only from the farming analyzer (see analyzer_farm_selection).
EARLY_LEGS = (
    ("/shattered_islands/world_3_69", "Thrakir|lost soul"),
    ("/shattered_islands/world_3_68", "Fahrgorm|evil treant"),
    ("/shattered_islands/world_3_69", "giant wasp|wasp_giant|wasp giant"),
    ("/shattered_islands/world_4_68", "killer bee|bee_killer|bee killer"),
    ("/shattered_islands/world_10_78", "evil treant|quickwood"),
)
LOST_SOUL_LEGS = (("/shattered_islands/world_3_69", "Thrakir|lost soul"),)
MUD_HAND_LEGS = (
    ("/shattered_islands/world_12_79", "mud hand"),
    ("/shattered_islands/world_12_78", "mud hand"),
)


def analyzer_spot(name, paths, monsters, *, aggressive=0):
    """One farming-analyzer result in its JSON shape."""
    return {"path": "|".join(paths), "name": name, "region": "test",
            "map_paths": ["maps" + path for path in paths], "monsters": list(monsters),
            "xp_hour": 1.0, "money_hour": 1.0, "loot_hour": 1.0, "danger": 1.0,
            "aggressive_spawns": aggressive}


class StubAnalyzer:
    """A finished farming analyzer with fixed rankings per objective."""

    def __init__(self, rankings):
        self.rankings = rankings
        self.status = "ready"

    def ranking(self, objective):
        return list(self.rankings.get(objective, ()))

    def snapshot(self, objective):
        return {"status": self.status, "objective": objective,
                "ranking": self.ranking(objective)[:5]}


class ProtocolTests(unittest.TestCase):
    def test_full_player_inventory_packet_releases_replay_barrier(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())

            async def request_quests():
                return None

            client.request_quests = request_quests
            client.load_character_memory = lambda _name: None
            player = Packet(0).u32(7).u32(0).u32(0).string("Sera")
            await client._handle_player(
                Cursor(bytes(player.data)), bytes(player.data))
            self.assertFalse(client.state.inventory_replay_complete)

            client.state.place_item(
                Item(9, item_type=c.TYPE_SKILL, name="slash weapons"), 7)
            # One empty delete-and-replace packet is still a complete
            # authoritative inventory.
            packet = Packet(0).u8(1).u32(7).u32(7).u8(1)
            await client._handle_item(
                Cursor(bytes(packet.data)), bytes(packet.data))

            self.assertTrue(client.state.inventory_replay_complete)
            self.assertEqual(client.state.inventory, [])

        asyncio.run(scenario())

    def test_a_cure_outside_the_bot_resets_the_depletion_count(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.player_tag = 7
            client._persist_character_memory = lambda: None
            force = Item(22, item_type=c.TYPE_FORCE, name="depletion")
            client.state.place_item(force, 7)
            client.state.place_item(Item(23, name="apple"), 7)
            client.set_depletion_points(32)
            delete = Packet(0).u32(23)
            await client._handle_item_delete(
                Cursor(bytes(delete.data)), bytes(delete.data))
            self.assertEqual(client.state.depletion_points, 32)
            # The force itself goes: cured by an item or the dashboard.
            delete = Packet(0).u32(22)
            await client._handle_item_delete(
                Cursor(bytes(delete.data)), bytes(delete.data))
            self.assertEqual(client.state.depletion_points, 0)
            # A new loss counted before its force arrives survives unrelated
            # deletions, so a fresh episode starts from one.
            client.set_depletion_points(1)
            client.state.place_item(Item(24, name="pear"), 7)
            delete = Packet(0).u32(24)
            await client._handle_item_delete(
                Cursor(bytes(delete.data)), bytes(delete.data))
            self.assertEqual(client.state.depletion_points, 1)
            # Cured while offline: the replay on login has no force.
            client.set_depletion_points(32)
            replay = Packet(0).u8(1).u32(7).u32(7).u8(1)
            await client._handle_item(
                Cursor(bytes(replay.data)), bytes(replay.data))
            self.assertEqual(client.state.depletion_points, 0)

        asyncio.run(scenario())

    def test_client_checkpoint_wakes_read_loop_for_clean_reconnect(self):
        class Writer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        client = AtrinikClient(ClientConfig(account="a", password="b"))
        client.reader = object()
        writer = Writer()
        client.writer = writer
        client.state.phase = "playing"

        self.assertTrue(asyncio.run(client.checkpoint_reconnect()))
        self.assertTrue(client._reconnect_requested)
        self.assertTrue(writer.closed)
        self.assertIsNone(client.reader)
        self.assertIsNone(client.writer)
        self.assertEqual(client.state.phase, "disconnected")

    def test_stalled_send_forces_reconnect_without_killing_task(self):
        class Writer:
            def __init__(self):
                self.closed = False
                self.payload = b""

            def write(self, payload):
                self.payload = payload

            async def drain(self):
                await asyncio.sleep(3600)  # flow control never drains

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        async def run():
            client = AtrinikClient(ClientConfig(connect_timeout=0.01))
            client.SEND_STALL_SECONDS = 0.01
            writer = Writer()
            client.writer = writer
            client.state.phase = "playing"
            await client.command("/time")
            self.assertTrue(writer.payload)
            self.assertTrue(writer.closed)
            self.assertIsNone(client.writer)
            self.assertTrue(client._reconnect_requested)
            self.assertEqual(client.state.phase, "disconnected")
            self.assertEqual(
                client.action_history[-1]["action"], "transport-reconnect")

        asyncio.run(run())

    def test_certificate_fingerprint_normalization(self):
        raw = ":".join(["AB"] * 32)
        self.assertEqual(normalize_certificate_sha256(raw), "ab" * 32)
        for invalid in ("", "ab" * 31, "xy" * 32):
            with self.assertRaises(ValueError):
                normalize_certificate_sha256(invalid)

    def test_quic_alpn_matches_current_classic_server(self):
        self.assertEqual(QUIC_ALPN, "atrinik/2")

    def test_quic_game_stream_preface_matches_current_classic_server(self):
        self.assertEqual(QUIC_GAME_STREAM_PREFACE, b"ATRN\x01\x00\x00\x01")

    def test_unique_map_path(self):
        self.assertEqual(
            logical_map_path(
                "./data/players/a/au/aut/autobot one/"
                "$shattered_islands$deserted_tutorial_island$mushroom_cavern"
            ),
            "/shattered_islands/deserted_tutorial_island/mushroom_cavern",
        )
        self.assertEqual(logical_map_path("/shattered_islands/world_-7_76"),
                         "/shattered_islands/world_-7_76")

    def test_animated_map_visual_uses_animation_namespace(self):
        client = AtrinikClient(ClientConfig())
        treant_id = next(index for index, name in client.animations.items()
                          if name == "treant_evil")
        # The same numeric ID is valid in bmaps but names an unrelated sprite;
        # this was the source of the dashboard's tied-bollard diagnosis.
        self.assertEqual(client.faces[treant_id], "bollard_tied_e_1.101")
        obj = MapObject(5, treant_id, 0, c.MAP_FLAG_ANIMATION,
                        animation=treant_id)
        self.assertEqual(map_object_visual_name(client, obj), "treant_evil")
        static = MapObject(5, treant_id, 0, 0)
        self.assertEqual(map_object_visual_name(client, static),
                         "bollard_tied_e_1.101")

    def test_packet(self):
        self.assertEqual(c.SOCKET_VERSION, 1084)
        self.assertEqual(
            Packet(c.S_VERSION).add("I", c.SOCKET_VERSION).encode(),
            b"\x00\x05\x03\x00\x00\x04\x3c",
        )

    def test_target_select_uses_depth_aware_exact_payload(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            sent = []

            async def send(packet):
                sent.append(packet.encode())

            client.send = send
            await client.target(7, 8, 0x12345678, -6)
            await client.clear_target()
            self.assertEqual(sent, [
                b"\x00\x09\x16\x01\x07\x08\xfa\x12\x34\x56\x78",
                b"\x00\x02\x16\x02",
            ])
            for depth in (-7, 7):
                with self.assertRaisesRegex(ValueError, "relative_depth"):
                    await client.target(7, 8, 1, depth)
            with self.assertRaisesRegex(ValueError, "actor_count"):
                await client.target(7, 8, 0)

        asyncio.run(scenario())

    def test_targets_the_server_never_acknowledges_are_set_aside(self):
        from . import targeting
        from .model import MapObject
        from .targeting import select_target

        async def scenario():
            client = AtrinikClient(ClientConfig())
            sent = []

            async def send(packet):
                sent.append(packet.encode())

            client.send = send
            frog = MapObject(5, 315, 0, 0)
            frog.name, frog.target_id = "giant frog", 1343754
            client.state.map.tile(8, 7).objects[5] = frog
            clock = [100.0]

            def ack(actor):
                client.state.target_id = actor
                client.state.target_ack = (actor, clock[0])

            with patch("atrinik_bot.targeting.time.monotonic", lambda: clock[0]), \
                    patch("atrinik_bot.model.time.monotonic", lambda: clock[0]):
                # A fight: selected once, then fought for 30 s without asking
                # again, until a heal targets Sera herself.
                self.assertTrue(await select_target(client, 8, 7, 1343754))
                ack(1343754)
                clock[0] += 30.0
                client.state.target_id = 0
                # The first retry after losing it is still sent.
                self.assertTrue(await select_target(client, 8, 7, 1343754))
                self.assertEqual(len(sent), 2)
                # It never comes back (it was killed, but its view lingers).
                for _ in range(40):
                    clock[0] += 0.5
                    if not await select_target(client, 8, 7, 1343754):
                        break
                # Requests ran from 130 s; it is set aside six seconds later.
                self.assertEqual(clock[0], 130.0 + targeting.TARGET_ACK_TIMEOUT)
                self.assertEqual(len(sent), 2 + 11)
                self.assertEqual(client.action_history[-1]["action"], "target-unresponsive")
                self.assertTrue(client.state.map.target_ignored(1343754))
                # It is still seen (threat detection counts it), but not
                # requested again while set aside.
                self.assertEqual(len(list(client.state.map.targets(friendly=False))), 1)
                self.assertFalse(await select_target(client, 8, 7, 1343754))
                self.assertEqual(len(sent), 13)
                # It is offered again after the pause.
                clock[0] += targeting.UNRESPONSIVE_TARGET_SECONDS
                self.assertFalse(client.state.map.target_ignored(1343754))
                # A creature that wandered out of view for a while gets a
                # fresh window when it returns.
                self.assertTrue(await select_target(client, 8, 7, 1343754))
                clock[0] += targeting.TARGET_ACK_TIMEOUT + 1.0
                self.assertTrue(await select_target(client, 8, 7, 1343754))

        asyncio.run(scenario())

    def test_reconnects_restart_target_windows(self):
        client = AtrinikClient(ClientConfig())
        client.state.target_request = (1343754, 10.0)
        client.state.target_ack = (1343754, 9.0)
        client._reset_target_identity()
        self.assertEqual(client.state.target_request, (0, 0.0, 0.0))
        self.assertEqual(client.state.target_ack, (0, 0.0))

    def test_feedback_watchers_ignore_player_chat(self):
        state = GameState()
        position = state.message_count
        state.add_message(c.CHAT_TYPE_LOCAL, "ffffff", "Mallory says: It would cost you 1 copper coin.")
        state.add_message(c.CHAT_TYPE_PRIVATE, "ffffff", "It would cost you 1 copper coin.")
        state.add_message(2, "ffffff", "It would cost you 4 gold coins.")
        self.assertEqual([entry[3] for entry in state.messages_since(position)],
                         ["It would cost you 4 gold coins."])
        self.assertEqual(len(state.messages_since(position, chat=True)), 3)

    def test_target_replies_record_their_acknowledgement(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            with patch("atrinik_bot.client.time.monotonic", lambda: 42.0):
                payload = (bytes([c.TARGET_ENEMY]) + b"ff0000\0giant frog\0" +
                           bytes([5]) + (1343754).to_bytes(4, "big") + bytes([0, 1, 0]))
                await client._handle_target(Cursor(payload), payload)
            self.assertEqual(client.state.target_ack, (1343754, 42.0))

        asyncio.run(scenario())

    def test_targeted_talk_uses_exact_actor_identity_without_selection(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.target_id = 99
            client.state.target_relative_depth = 4
            sent = []

            async def send(packet):
                sent.append(packet.encode())

            client.send = send
            await client.talk_to_target(7, 8, -6, 0x12345678, "hello")
            self.assertEqual(sent, [
                b"\x00\x0f\x14\x07\x07\x08\xfa\x12\x34\x56\x78hello\0",
            ])
            self.assertEqual(client.state.target_id, 99)
            self.assertEqual(client.state.target_relative_depth, 4)
            await client.talk("hello", "Guard")
            self.assertEqual(sent[1], b"\x00\x0e\x14\x05Guard\0hello\0")
            await client.talk_to_target(
                7, 8, 6, 1, "x" * c.TALK_NPC_TARGET_MESSAGE_MAX)
            self.assertEqual(len(sent[-1]), 2 + 1 + 8 +
                             c.TALK_NPC_TARGET_MESSAGE_MAX + 1)

            invalid = (
                (True, 8, 0, 1, "hello"),
                (-1, 8, 0, 1, "hello"),
                (client.state.map.width, 8, 0, 1, "hello"),
                (7, -1, 0, 1, "hello"),
                (7, client.state.map.height, 0, 1, "hello"),
                (7, 8, -7, 1, "hello"),
                (7, 8, 7, 1, "hello"),
                (7, 8, 0, 0, "hello"),
                (7, 8, 0, 0x1_0000_0000, "hello"),
                (7, 8, 0, 1, ""),
                (7, 8, 0, 1, "not\0a command"),
                (7, 8, 0, 1, "x" * (c.TALK_NPC_TARGET_MESSAGE_MAX + 1)),
                (7, 8, 0, 1, "🙂" * 1024),
            )
            for args in invalid:
                with self.assertRaises((TypeError, ValueError)):
                    await client.talk_to_target(*args)
            with self.assertRaisesRegex(ValueError, "invalid item talk type"):
                await client.talk_to_item(c.TALK_NPC_TARGET, 1, "hello")
            self.assertEqual(len(sent), 3)

        asyncio.run(scenario())

    @staticmethod
    def _map2_actor_tile(x, y, layer, actor_count, friend=False, face=321,
                         mask=0):
        """Build one MAP2 tile carrying a target-bearing map object."""
        mask |= (x << 11) | (y << 6)
        return (
            mask.to_bytes(2, "big") + bytes((1, layer)) +
            face.to_bytes(2, "big") + bytes((0, c.MAP_FLAG_MORE)) +
            c.MAP_FLAG2_TARGET.to_bytes(4, "big") +
            actor_count.to_bytes(4, "big") + bytes((int(friend), 0))
        )

    @staticmethod
    def _map2_packet(update, levels, *, marker=0, world_x=4, world_y=5,
                     sublayer=0, width=11, height=9, connected=None,
                     timed=False):
        """Build a MAP2 packet directly from protocol constants and fields."""
        packet = bytearray((update,))
        if update not in (c.MAP_PARTIAL, c.MAP_SAME):
            packet.extend(b"Map Two\0music\0clear\0")
            packet.extend((0, 0))
            packet.extend(b"region\0Region Long\0/maps/two\0")
            if update == c.MAP_NEW:
                packet.extend((width, height))
            elif update == c.MAP_CONNECTED:
                if connected is None:
                    raise AssertionError("MAP_CONNECTED needs tile and offsets")
                tile, xoff, yoff, zoff = connected
                packet.extend((tile, xoff & 0xff, yoff & 0xff, zoff & 0xff))
        packet.extend((world_x, world_y, sublayer))
        packet.extend((marker | (c.MAP2_CONTINUATION_TIMED_LIGHT if timed else 0)).to_bytes(2, "big"))
        if timed:
            packet.extend((1).to_bytes(8, "big"))
            packet.extend((7200).to_bytes(8, "big"))
            packet.extend((10800).to_bytes(8, "big"))
            packet.append(1)
        packet.append(len(levels))
        for depth, body in levels:
            packet.extend(depth.to_bytes(1, "big", signed=True))
            packet.extend(len(body).to_bytes(4, "big"))
            packet.extend(body)
        return bytes(packet)

    def test_map2_new_tracks_depth_targets_and_preserves_depth_zero_tiles(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            payload = self._map2_packet(c.MAP_NEW, [
                (0, self._map2_actor_tile(5, 4, 5, 100)),
                (-6, self._map2_actor_tile(2, 1, 5, 101, True)),
                (6, self._map2_actor_tile(8, 7, 5, 102)),
            ])
            client.state.phase = "playing"
            client.progress_watch.moved_at = 0.0
            await client._handle_map(Cursor(payload), payload)

            game_map = client.state.map
            self.assertEqual((game_map.width, game_map.height), (17, 17))
            self.assertIs(game_map.tiles, game_map.level_tiles[0])
            # The map packet itself records movement for the stuck alert.
            self.assertEqual(client.progress_watch.position,
                             (game_map.path, game_map.world_x, game_map.world_y))
            self.assertGreater(client.progress_watch.moved_at, 0.0)
            self.assertEqual(
                [(depth, x, y, obj.target_id, obj.is_friend)
                 for depth, x, y, obj in game_map.depth_targets()],
                [(0, 5, 4, 100, False), (-6, 2, 1, 101, True),
                 (6, 8, 7, 102, False)],
            )
            self.assertEqual(
                [(x, y, obj.target_id) for x, y, obj in game_map.targets()],
                [(5, 4, 100)],
            )
            self.assertEqual(
                [(depth, x, y, obj.target_id)
                 for depth, x, y, obj in game_map.depth_targets(True)],
                [(-6, 2, 1, 101)],
            )

        asyncio.run(scenario())

    def test_mapstats_time_record_is_transactional(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.game_seconds = 7
            client.state.game_millis_per_minute = 11
            payload = bytes((5,)) + (123456).to_bytes(8, "big") + (250).to_bytes(4, "big")
            await client._handle_mapstats(Cursor(payload), payload)
            self.assertEqual(client.state.game_seconds, 123456)
            self.assertEqual(client.state.game_millis_per_minute, 250)
            for end in range(1, len(payload)):
                with self.assertRaises(ProtocolError):
                    await client._handle_mapstats(Cursor(payload[:end]), payload[:end])
                self.assertEqual(client.state.game_seconds, 123456)
                self.assertEqual(client.state.game_millis_per_minute, 250)

        asyncio.run(scenario())

    def test_engine_waits_for_the_first_map_after_a_reconnect(self):
        from .tasks import BotTask

        async def scenario():
            client = AtrinikClient(ClientConfig())
            engine = TaskEngine(client, tick_rate=0.01)
            ticks = []

            class Probe(BotTask):
                async def tick(self, client):
                    # Without a connection this send raises ConnectionError,
                    # which would permanently fail the task.
                    await client.talk("hello", "sage")
                    ticks.append(client.state.map.path)

            probe = Probe("probe")
            engine.set_task(probe)
            engine.running = True
            runner = asyncio.create_task(engine.run())
            # The whole reconnect holds the task: disconnected, connecting
            # (even with a stale map), then playing before the first MAP2.
            for phase, path in (("disconnected", ""), ("connecting", "/stale"),
                                ("playing", "")):
                client.state.phase = phase
                client.state.map.path = path
                await asyncio.sleep(0.05)
                self.assertEqual(ticks, [], phase)
                self.assertIs(engine.task, probe, phase)
            sent = []

            async def send(packet):
                sent.append(packet)

            client.send = send
            client.state.map.path = "/shattered_islands/world_14_77"
            await asyncio.sleep(0.05)
            engine.running = False
            await runner
            self.assertTrue(ticks)
            self.assertEqual(set(ticks), {"/shattered_islands/world_14_77"})

        asyncio.run(scenario())

    def test_engine_keeps_the_task_when_the_connection_drops_mid_tick(self):
        from .tasks import BotTask

        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.phase = "playing"
            client.state.map.path = "/shattered_islands/world_14_77"
            engine = TaskEngine(client, tick_rate=0.01)
            ticks = []

            class Probe(BotTask):
                survives_disconnect = True

                async def tick(self, client):
                    ticks.append(client.state.phase)
                    # Not connected: the send raises ConnectionError.
                    await client.clear_actions()

            probe = Probe("probe")
            engine.set_task(probe)
            engine.running = True
            runner = asyncio.create_task(engine.run())
            await asyncio.sleep(0.05)
            self.assertTrue(ticks)
            self.assertIs(engine.task, probe)
            self.assertNotEqual(probe.status, TaskStatus.FAILED)
            # After the reconnect the same task carries on.
            sent = []

            async def send(packet):
                sent.append(packet)

            client.send = send
            await asyncio.sleep(0.05)
            engine.running = False
            await runner
            self.assertIs(engine.task, probe)
            self.assertTrue(sent)

            # A task that cannot rebuild its in-flight work still fails.
            class Plain(BotTask):
                async def tick(self, client):
                    await client.clear_actions()

            client.send = AtrinikClient.send.__get__(client)
            plain = Plain("plain")
            engine.set_task(plain)
            engine.running = True
            runner = asyncio.create_task(engine.run())
            await asyncio.sleep(0.05)
            engine.running = False
            await runner
            self.assertIsNone(engine.task)
            self.assertEqual(plain.status, TaskStatus.FAILED)

        asyncio.run(scenario())

    def test_autoplay_rebuilds_its_child_after_a_lost_connection(self):
        from .autoplay import AutoplayTask
        from .tasks import BotTask

        class Child(BotTask):
            async def tick(self, client):
                raise ConnectionError("not connected")

        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        autoplay = AutoplayTask(WorldGraph(), target_level=115)
        self.assertTrue(autoplay.survives_disconnect)
        autoplay.status = TaskStatus.RUNNING
        autoplay.phase = "adaptive-farming"
        autoplay.child = Child("half-done purchase")
        with self.assertRaises(ConnectionError):
            asyncio.run(autoplay.tick(client))
        self.assertIsNone(autoplay.child)
        self.assertEqual(autoplay.status, TaskStatus.RUNNING)
        self.assertIn("autoplay-disconnect",
                      [entry["action"] for entry in client.decision_history])
        # An interrupted mana-crystal recovery may be tried again.
        autoplay._retry_at = 0.0
        autoplay.phase = "mana-crystal-recovery"
        autoplay._mana_recovery_attempted = True
        autoplay.child = Child("mana-crystal recovery")
        with self.assertRaises(ConnectionError):
            asyncio.run(autoplay.tick(client))
        self.assertFalse(autoplay._mana_recovery_attempted)
        # ... and so may one whose setup lost the connection.
        autoplay.phase = "adaptive-farming"
        autoplay.child = None
        with self.assertRaises(ConnectionError):
            asyncio.run(autoplay._start_mana_crystal_recovery(client))
        self.assertFalse(autoplay._mana_recovery_attempted)

    def test_transport_os_errors_are_reported_as_lost_connections(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())

            class Writer:
                def write(self, data):
                    raise OSError(101, "Network is unreachable")

                async def drain(self):
                    pass

                def close(self):
                    pass

                async def wait_closed(self):
                    raise OSError(32, "Broken pipe")

            client.writer = Writer()
            client.state.phase = "playing"
            with self.assertRaises(ConnectionError) as raised:
                await client.clear_actions()
            self.assertIsInstance(raised.exception.__cause__, OSError)
            # The transport is detached so the read loop wakes and reconnects.
            self.assertIsNone(client.writer)
            self.assertEqual(client.state.phase, "disconnected")
            self.assertTrue(client._reconnect_requested)

            # Resets, broken pipes and transport timeouts tear down the
            # transport the same way; ETIMEDOUT is not a stalled drain.
            for error in (ConnectionResetError, BrokenPipeError, TimeoutError):
                class Broken(Writer):
                    def write(self, data, error=error):
                        raise error()

                client.writer = Broken()
                client.state.phase = "playing"
                client._reconnect_requested = False
                with self.assertRaises(ConnectionError):
                    await client.clear_actions()
                self.assertIsNone(client.writer, error)
                self.assertEqual(client.state.phase, "disconnected", error)
                self.assertTrue(client._reconnect_requested, error)

            # A late failure on an already replaced transport leaves the new
            # connection alone.
            class Late(Writer):
                def write(self, data):
                    client.writer = fresh
                    raise ConnectionResetError()

            fresh = Writer()
            client.writer = Late()
            client.state.phase = "playing"
            client._reconnect_requested = False
            with self.assertRaises(ConnectionError):
                await client.clear_actions()
            self.assertIs(client.writer, fresh)
            self.assertEqual(client.state.phase, "playing")
            self.assertFalse(client._reconnect_requested)

        asyncio.run(scenario())

    def test_map2_soft_clear_and_fog_keep_remembered_geometry_like_the_client(self):
        from .model import MapObject

        async def scenario():
            client = AtrinikClient(ClientConfig())
            body = self._map2_actor_tile(5, 4, 5, 103)
            await client._handle_map(Cursor(self._map2_packet(c.MAP_NEW, [(0, body)])), b"")
            tile = client.state.map.tile(5, 4)
            # floor (0), wall (4), item (2) and a second sub-layer floor (7).
            for layer in (0, 4, 2, 7):
                tile.objects[layer] = MapObject(layer, 10 + layer, 0, 0)
            self.assertTrue(tile.targets)

            def clear(mask):
                return ((5 << 11) | (4 << 6) | mask).to_bytes(2, "big")

            soft = self._map2_packet(c.MAP_SAME, [(0, clear(c.MAP2_MASK_CLEAR))])
            await client._handle_map(Cursor(soft), soft)
            tile = client.state.map.tile(5, 4)
            # The client keeps floor/mask/wall as fog memory, drops the rest.
            self.assertEqual(sorted(tile.objects), [0, 4, 7])
            self.assertTrue(tile.fogged)
            self.assertEqual(tile.targets, [])

            hard = self._map2_packet(c.MAP_SAME, [(0, clear(c.MAP2_MASK_CLEAR | c.MAP2_MASK_HARD_CLEAR))])
            await client._handle_map(Cursor(hard), hard)
            self.assertEqual(client.state.map.tile(5, 4).objects, {})

        asyncio.run(scenario())

    def test_map2_support_height_and_fog_alias_revoke_target_authority(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            body = self._map2_actor_tile(
                5, 4, 5, 103,
                mask=c.MAP2_MASK_SUPPORT_HEIGHT | c.MAP2_MASK_FOW)
            # MAP2 orders support height then FOW before the layer records.
            body = body[:2] + b"\xff\xfb\x01" + body[2:]
            payload = self._map2_packet(c.MAP_NEW, [(0, body)])
            await client._handle_map(Cursor(payload), payload)
            tile = client.state.map.tile(5, 4)
            self.assertEqual(tile.support_height, -5)
            self.assertTrue(tile.fogged)
            self.assertTrue(tile.fog_of_war)
            self.assertEqual(list(client.state.map.targets()), [])

        asyncio.run(scenario())

    def test_mapstats_metadata_and_clock_publish_atomically(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.map.name = "old"
            client.state.game_seconds = 7
            client.state.game_millis_per_minute = 11
            old_map = client.state.map
            payload = (b"\x01new\0\x05" + (123456).to_bytes(8, "big") +
                       (250).to_bytes(4, "big"))
            # A boundary after the complete name record is itself a valid
            # one-field MAPSTATS packet; every actual partial field must fail.
            for end in (*range(1, 5), *range(6, len(payload))):
                with self.assertRaises(ProtocolError):
                    await client._handle_mapstats(
                        Cursor(payload[:end]), payload[:end])
                self.assertIs(client.state.map, old_map)
                self.assertEqual(client.state.map.name, "old")
                self.assertEqual((client.state.game_seconds,
                                  client.state.game_millis_per_minute), (7, 11))
            await client._handle_mapstats(Cursor(payload), payload)
            self.assertIsNot(client.state.map, old_map)
            self.assertEqual(client.state.map.name, "new")
            self.assertEqual((client.state.game_seconds,
                              client.state.game_millis_per_minute), (123456, 250))

        asyncio.run(scenario())

    def test_map2_continuations_publish_only_after_ordered_final_packet(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            old_map = client.state.map
            old_map.path = "/old"
            old_map.tile(1, 1).objects[2] = MapObject(2, 1, 0, 0)

            initial = self._map2_packet(
                c.MAP_NEW, [(0, b""), (-6, b""), (6, b"")], marker=2)
            await client._handle_map(Cursor(initial), initial)
            self.assertIs(client.state.map, old_map)
            self.assertEqual(client.state.map.path, "/old")

            first = self._map2_packet(
                c.MAP_PARTIAL,
                [(-6, self._map2_actor_tile(2, 1, 5, 101, True))], marker=1)
            await client._handle_map(Cursor(first), first)
            self.assertIs(client.state.map, old_map)
            self.assertEqual(client.state.map.path, "/old")

            final = self._map2_packet(
                c.MAP_PARTIAL,
                [(0, self._map2_actor_tile(5, 4, 5, 100)),
                 (6, self._map2_actor_tile(8, 7, 5, 102))], marker=2)
            await client._handle_map(Cursor(final), final)
            self.assertIsNot(client.state.map, old_map)
            self.assertEqual(client.state.map.path, "/maps/two")
            self.assertEqual(
                [(depth, obj.target_id)
                 for depth, _x, _y, obj in client.state.map.depth_targets()],
                [(0, 100), (-6, 101), (6, 102)],
            )

        asyncio.run(scenario())

    def test_map2_malformed_packets_do_not_publish_partial_map(self):
        async def scenario():
            async def expect_rejected(payload):
                client = AtrinikClient(ClientConfig())
                old_map = client.state.map
                old_map.path = "/old"
                old_map.tile(1, 1).objects[2] = MapObject(2, 1, 0, 0)
                with self.assertRaises(ProtocolError):
                    await client._handle_map(Cursor(payload), payload)
                self.assertIs(client.state.map, old_map)
                self.assertEqual(client.state.map.path, "/old")
                self.assertIn((1, 1), client.state.map.tiles)

            valid = self._map2_packet(
                c.MAP_NEW, [(0, self._map2_actor_tile(5, 4, 5, 100))])
            await expect_rejected(valid[:-1])
            await expect_rejected(self._map2_packet(c.MAP_NEW, [(0, b""),
                                                                  (0, b"")]))
            await expect_rejected(self._map2_packet(c.MAP_NEW, [(7, b"")]))
            await expect_rejected(self._map2_packet(c.MAP_PARTIAL,
                                                    [(0, b"")], marker=1))

            client = AtrinikClient(ClientConfig())
            old_map = client.state.map
            pending = self._map2_packet(c.MAP_NEW, [(0, b"")], marker=2)
            await client._handle_map(Cursor(pending), pending)
            invalid_continuation = self._map2_packet(
                c.MAP_PARTIAL, [(0, self._map2_actor_tile(5, 4, 5, 100))],
                marker=2)
            with self.assertRaises(ProtocolError):
                await client._handle_map(Cursor(invalid_continuation),
                                         invalid_continuation)
            self.assertIs(client.state.map, old_map)

            # A new complete header aborts the old generation.  A late
            # continuation cannot replace the newly published state.
            client = AtrinikClient(ClientConfig())
            first_generation = self._map2_packet(c.MAP_NEW, [(0, b"")],
                                                 marker=1)
            await client._handle_map(Cursor(first_generation), first_generation)
            replacement = self._map2_packet(
                c.MAP_NEW, [(0, self._map2_actor_tile(5, 4, 5, 200))])
            await client._handle_map(Cursor(replacement), replacement)
            published = client.state.map
            late = self._map2_packet(
                c.MAP_PARTIAL, [(0, self._map2_actor_tile(5, 4, 5, 100))],
                marker=1)
            with self.assertRaises(ProtocolError):
                await client._handle_map(Cursor(late), late)
            self.assertIs(client.state.map, published)
            self.assertEqual(
                [obj.target_id for _, _, obj in client.state.map.targets()],
                [200])

            # A malformed complete header cannot supersede a previously
            # validated generation; its ordered continuation still commits.
            client = AtrinikClient(ClientConfig())
            pending = self._map2_packet(c.MAP_NEW, [(0, b"")], marker=1)
            await client._handle_map(Cursor(pending), pending)
            malformed_complete = self._map2_packet(
                c.MAP_NEW, [(0, self._map2_actor_tile(5, 4, 5, 300))])[:-1]
            with self.assertRaises(ProtocolError):
                await client._handle_map(Cursor(malformed_complete),
                                         malformed_complete)
            completion = self._map2_packet(
                c.MAP_PARTIAL, [(0, self._map2_actor_tile(5, 4, 5, 400))],
                marker=1)
            await client._handle_map(Cursor(completion), completion)
            self.assertEqual(
                [obj.target_id for _, _, obj in client.state.map.targets()],
                [400])

            # Oversized forged envelopes cannot discard a validated pending
            # generation, whose matching continuation remains authoritative.
            client = AtrinikClient(ClientConfig())
            pending = self._map2_packet(c.MAP_NEW, [(0, b"")], marker=1)
            await client._handle_map(Cursor(pending), pending)
            forged = (self._map2_packet(c.MAP_NEW, [(0, b"")]) + b"\0" *
                      (c.MAP2_ENVELOPE_MAX + 1))
            with self.assertRaises(ProtocolError):
                await client._handle_map(Cursor(forged), forged)
            completion = self._map2_packet(
                c.MAP_PARTIAL, [(0, self._map2_actor_tile(5, 4, 5, 500))],
                marker=1)
            await client._handle_map(Cursor(completion), completion)
            self.assertEqual(
                [obj.target_id for _, _, obj in client.state.map.targets()],
                [500])

        asyncio.run(scenario())

    def test_map2_target_authority_fog_and_living_clear(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            nonliving = self._map2_actor_tile(2, 2, 4, 101)
            faceless = self._map2_actor_tile(3, 2, 5, 102, face=0)
            payload = self._map2_packet(c.MAP_NEW, [(0, nonliving + faceless)])
            await client._handle_map(Cursor(payload), payload)
            self.assertEqual(list(client.state.map.targets()), [])

            living = self._map2_actor_tile(5, 4, 5, 103)
            same = self._map2_packet(c.MAP_SAME, [(0, living)])
            await client._handle_map(Cursor(same), same)
            self.assertEqual([obj.target_id for _, _, obj in client.state.map.targets()], [103])

            fogged = self._map2_actor_tile(5, 4, 5, 103,
                                           mask=c.MAP2_MASK_FOW)
            fogged = fogged[:2] + b"\x01" + fogged[2:]
            same = self._map2_packet(c.MAP_SAME, [(0, fogged)])
            await client._handle_map(Cursor(same), same)
            self.assertEqual(list(client.state.map.targets()), [])

            # A clear for the actionable living layer revokes a prior target.
            unfogged = self._map2_actor_tile(5, 4, 5, 103,
                                             mask=c.MAP2_MASK_FOW)
            unfogged = unfogged[:2] + b"\x00" + unfogged[2:]
            same = self._map2_packet(c.MAP_SAME, [(0, unfogged)])
            await client._handle_map(Cursor(same), same)
            self.assertEqual(
                [obj.target_id for _, _, obj in client.state.map.targets()],
                [103])
            clear = ((5 << 11) | (4 << 6)).to_bytes(2, "big") + bytes(
                (1, c.MAP_LAYER_CLEAR, 5, 0))
            same = self._map2_packet(c.MAP_SAME, [(0, clear)])
            await client._handle_map(Cursor(same), same)
            self.assertEqual(list(client.state.map.targets()), [])

        asyncio.run(scenario())

    def test_map2_same_connected_and_full_headers_replace_cached_levels(self):
        async def scenario():
            client = AtrinikClient(ClientConfig(account="a", password="b"))
            initial = self._map2_packet(c.MAP_NEW, [
                (0, self._map2_actor_tile(5, 4, 5, 100)),
                (-1, self._map2_actor_tile(3, 3, 5, 101)),
                (-6, self._map2_actor_tile(2, 1, 5, 106)),
            ])
            await client._handle_map(Cursor(initial), initial)
            same = self._map2_packet(c.MAP_SAME, [(0, b""), (-1, b"")],
                                     world_x=5)
            await client._handle_map(Cursor(same), same)
            self.assertEqual([(x, y, obj.target_id)
                              for x, y, obj in client.state.map.targets()],
                             [(4, 4, 100)])

            connected = self._map2_packet(
                c.MAP_CONNECTED, [(0, b""), (-1, b""), (-2, b"")],
                world_x=5, connected=(9, 0, 0, 1))
            await client._handle_map(Cursor(connected), connected)
            self.assertEqual([(depth, obj.target_id)
                              for depth, _x, _y, obj in client.state.map.depth_targets()],
                             [(-1, 100), (-2, 101)])

            replacement = self._map2_packet(
                c.MAP_NEW, [(0, self._map2_actor_tile(6, 4, 5, 200))])
            await client._handle_map(Cursor(replacement), replacement)
            self.assertEqual(set(client.state.map.level_tiles), {0})
            self.assertEqual([obj.target_id for _, _, obj in client.state.map.targets()],
                             [200])

            client.config.transport = "invalid"
            with self.assertRaisesRegex(ValueError, "encrypted QUIC"):
                await client.connect()
            self.assertEqual(client.state.map.level_tiles, {0: {}})

        asyncio.run(scenario())

    def test_map2_complete_depth_range_timed_extensions_and_rejections(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            rgb = self._map2_actor_tile(5, 4, 5, 100)
            rgb = (rgb[:-1] + bytes((c.MAP_EXT_LIGHT_RGB |
                                      c.MAP_EXT_LIGHT_KEYFRAME, 1)) +
                   b"\x00\x01\x00\x02\x00\x03" +
                   b"\x01\x00\x04\x01\x00\x05\x00\x06\x00\x07")
            complete = self._map2_packet(
                c.MAP_NEW,
                [(depth, rgb if depth == 0 else b"")
                 for depth in range(-c.MAP2_MAX_DEPTH, c.MAP2_MAX_DEPTH + 1)],
                timed=True)
            await client._handle_map(Cursor(complete), complete)
            self.assertEqual(set(client.state.map.level_tiles), set(range(-6, 7)))
            self.assertEqual([obj.target_id for _, _, obj in client.state.map.targets()],
                             [100])

            published = client.state.map
            async def rejected(packet):
                with self.assertRaises(ProtocolError):
                    await client._handle_map(Cursor(packet), packet)
                self.assertIs(client.state.map, published)

            await rejected(self._map2_packet(c.MAP_NEW, [(0, b"")], marker=4097))
            first = self._map2_actor_tile(5, 4, 5, 101)
            second = self._map2_actor_tile(5, 4, 5, 102)
            duplicate_layer = first[:2] + b"\x02" + first[3:] + second[3:]
            await rejected(self._map2_packet(c.MAP_NEW, [(0, duplicate_layer)]))
            bad_string = (((5 << 11) | (4 << 6)).to_bytes(2, "big") +
                          bytes((1, 5)) + (1).to_bytes(2, "big") +
                          bytes((0, c.MAP_FLAG_NAME)) + b"x" * 64 + b"\0")
            await rejected(self._map2_packet(c.MAP_NEW, [(0, bad_string)]))
            oversized_level = self._map2_packet(c.MAP_NEW, [(0, b"")])
            oversized_level = oversized_level[:-4] + (c.MAP2_LEVEL_PAYLOAD_MAX + 1).to_bytes(4, "big")
            await rejected(oversized_level)
            await rejected(self._map2_packet(c.MAP_NEW, [(0, b"")]) +
                           b"\0" * (c.MAP2_ENVELOPE_MAX + 1))
            await rejected(complete[:-1])
            bad_keyframe = self._map2_actor_tile(5, 4, 5, 201)
            bad_keyframe = (bad_keyframe[:-1] +
                            bytes((c.MAP_EXT_LIGHT_KEYFRAME, 0, 1)))
            await rejected(self._map2_packet(c.MAP_NEW, [(0, bad_keyframe)]))

        asyncio.run(scenario())

    def test_target_reply_is_transactional_and_uses_server_identity(self):
        async def scenario():
            client = AtrinikClient(ClientConfig())
            client.state.target_name = "old target"
            client.state.target_level = 13
            client.state.target_actor_count = 99
            client.state.target_relative_depth = 4
            client.state.target_id = 99
            payload = (
                bytes((c.TARGET_ENEMY,)) + b"red\0orc\0" + bytes((12,)) +
                (0x12345678).to_bytes(4, "big") + b"\xfa\x01\x00"
            )
            await client._handle_target(Cursor(payload), payload)
            self.assertEqual(client.state.target_name, "orc")
            self.assertEqual(client.state.target_level, 12)
            self.assertEqual(client.state.target_actor_count, 0x12345678)
            self.assertEqual(client.state.target_relative_depth, -6)
            self.assertEqual(client.state.target_id, 0x12345678)
            for end in range(len(payload)):
                truncated = payload[:end]
                with self.assertRaises(ProtocolError):
                    await client._handle_target(Cursor(truncated), truncated)
                self.assertEqual(client.state.target_name, "orc")
                self.assertEqual(client.state.target_actor_count, 0x12345678)
                self.assertEqual(client.state.target_relative_depth, -6)
            unknown = bytes((255,)) + payload[1:]
            with self.assertRaises(ProtocolError):
                await client._handle_target(Cursor(unknown), unknown)
            self.assertEqual(client.state.target_name, "orc")
            self.assertEqual(client.state.target_actor_count, 0x12345678)
            self.assertEqual(client.state.target_relative_depth, -6)
            with self.assertRaises(ProtocolError):
                await client._handle_target(Cursor(payload + b"\0"), payload + b"\0")
            self.assertEqual(client.state.target_name, "orc")
            self.assertEqual(client.state.target_actor_count, 0x12345678)
            self.assertEqual(client.state.target_relative_depth, -6)
            for combat in (0, 1, 2, 255):
                for combat_force in (0, 1, 2, 255):
                    actor_count = 0x10000000 + combat * 0x100 + combat_force
                    reply = (
                        bytes((c.TARGET_NEUTRAL,)) + b"blue\0target\0" +
                        bytes((17,)) + actor_count.to_bytes(4, "big") +
                        b"\x06" + bytes((combat, combat_force))
                    )
                    await client._handle_target(Cursor(reply), reply)
                    self.assertEqual(client.state.target_code, c.TARGET_NEUTRAL)
                    self.assertEqual(client.state.target_color, "blue")
                    self.assertEqual(client.state.target_name, "target")
                    self.assertEqual(client.state.target_level, 17)
                    self.assertEqual(client.state.target_actor_count, actor_count)
                    self.assertEqual(client.state.target_relative_depth, 6)
                    self.assertEqual(client.state.target_id, actor_count)
                    self.assertEqual(client.state.combat, bool(combat))
                    self.assertEqual(client.state.combat_force, bool(combat_force))

        asyncio.run(scenario())

    def test_reconnect_resets_target_identity(self):
        client = AtrinikClient(ClientConfig(
            account="a", password="b", transport="invalid"))
        client.state.target_actor_count = 99
        client.state.target_relative_depth = -6
        client.state.target_id = 99
        with self.assertRaisesRegex(ValueError, "encrypted QUIC"):
            asyncio.run(client.connect())
        self.assertEqual(client.state.target_actor_count, 0)
        self.assertEqual(client.state.target_relative_depth, 0)
        self.assertEqual(client.state.target_id, 0)

    def test_merchant_timeout_reports_what_the_dialogue_offered(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        task = BuyDialogueStockTask("sage", r"word of recall")
        self.assertEqual(task._dialogue_diagnostic(client),
                         " (no dialogue opened by 'sage')")
        client.state.interface = InterfaceState(
            title="Sage", links=["[a=#news]News[/a]", "[a=quests]Quests[/a]"])
        self.assertEqual(
            task._dialogue_diagnostic(client),
            " ('sage' dialogue 'Sage' offered no stock links; 2 link(s): News; Quests)")

    def test_item_decoder_retains_authoritative_read_flag(self):
        client = AtrinikClient(ClientConfig())
        item = Item(17)
        payload = c.ITEM_NO_SKILL_IDENT.to_bytes(4, "big")
        client._decode_item_fields(Cursor(payload), c.UPD_FLAGS, item)
        self.assertEqual(item.flags, c.ITEM_NO_SKILL_IDENT)

    def test_item_extra_matches_server_for_effect_objects(self):
        # A poison pulse sends UPD_EXTRA for the poisoning object; the server
        # writes no extra fields for it, so the glow must follow directly.
        client = AtrinikClient(ClientConfig())
        for item_type in (c.TYPE_POISONING, c.TYPE_FORCE):
            with self.subTest(item_type=item_type):
                item = Item(31, item_type=item_type)
                cur = Cursor(b"blue\x00\x07")
                client._decode_item_fields(cur, c.UPD_EXTRA | c.UPD_GLOW, item)
                self.assertEqual((item.glow, item.glow_speed), ("blue", 7))
                self.assertEqual(cur.remaining, 0)
                self.assertNotIn("seconds", item.extra)

    def test_item_extra_still_decodes_spells_and_skills(self):
        client = AtrinikClient(ClientConfig())
        spell = Item(32, item_type=c.TYPE_SPELL)
        cur = Cursor((5).to_bytes(2, "big") + (3).to_bytes(4, "big") +
                     (9).to_bytes(4, "big") + b"zap\x00")
        client._decode_item_fields(cur, c.UPD_EXTRA, spell)
        self.assertEqual((spell.extra["cost"], spell.extra["path"],
                          spell.extra["flags"], spell.extra["message"]),
                         (5, 3, 9, "zap"))
        self.assertEqual(cur.remaining, 0)
        skill = Item(33, item_type=c.TYPE_SKILL)
        cur = Cursor(bytes([12]) + (4567).to_bytes(8, "big", signed=True) + b"hit\x00")
        client._decode_item_fields(cur, c.UPD_EXTRA, skill)
        self.assertEqual((skill.extra["level"], skill.extra["experience"],
                          skill.extra["message"]), (12, 4567, "hit"))
        self.assertEqual(cur.remaining, 0)

    def test_version_waits_for_access_policy_before_setup(self):
        client = AtrinikClient(ClientConfig())
        client.state.phase = "version"
        sent = []

        async def send(packet):
            sent.append(packet.encode())

        client.send = send
        raw = c.SOCKET_VERSION.to_bytes(4, "big")
        asyncio.run(client._handle_version(Cursor(raw), raw))
        self.assertEqual(sent, [])
        self.assertEqual(client.state.phase, "access-policy")

    def test_connect_rejects_plaintext_transport_before_network(self):
        client = AtrinikClient(ClientConfig(
            account="a", password="b", transport="tcp"))
        with self.assertRaisesRegex(ValueError, "encrypted QUIC"):
            asyncio.run(client.connect())

    def test_characters_connection_ids(self):
        client = AtrinikClient(ClientConfig())
        current = "0123456789abcdef" * 2
        previous = "fedcba9876543210" * 2
        raw = (b"account\0" + current.encode() + b"\0" +
               previous.encode() + b"\0" + (1234).to_bytes(8, "big"))
        asyncio.run(client._handle_characters(Cursor(raw), raw))
        self.assertEqual(client.state.connection_id, current)
        self.assertEqual(client.state.previous_connection_id, previous)
        self.assertEqual(client.state.previous_connection_time, 1234)

        malformed = b"account\0NOT-AN-ID\0\0" + (0).to_bytes(8, "big")
        with self.assertRaises(ProtocolError):
            asyncio.run(client._handle_characters(Cursor(malformed),
                                                  malformed))

    def test_party_membership_packets_track_authoritative_state(self):
        client = AtrinikClient(ClientConfig())
        joined = bytes((c.CMD_PARTY_JOIN,)) + b"Sera\0"
        asyncio.run(client._handle_party(Cursor(joined), joined))
        self.assertEqual(client.state.party_name, "Sera")
        self.assertEqual(client.action_history[-1]["action"], "party-ready")

        left = bytes((c.CMD_PARTY_LEAVE,))
        asyncio.run(client._handle_party(Cursor(left), left))
        self.assertEqual(client.state.party_name, "")

        malformed = bytes((c.CMD_PARTY_JOIN,)) + b"unterminated"
        with self.assertRaises(ProtocolError):
            asyncio.run(client._handle_party(Cursor(malformed), malformed))

    def test_death_records_a_post_mortem_of_the_last_minute(self):
        import struct
        from . import post_mortem

        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.map.path = "/shattered_islands/world_6_46"
        client.state.map.world_x, client.state.map.world_y = 12, 14

        payload = b"".join(bytes((stat,)) + struct.pack(">" + c.STAT_FORMATS[stat], value)
                           for stat, value in ((c.STAT_HP, 13), (c.STAT_MAXHP, 155),
                                               (c.STAT_SP, 3), (c.STAT_MAXSP, 39)))
        asyncio.run(client._handle_stats(Cursor(payload), payload))
        self.assertEqual(client.vitals[-1][1:5], [13, 155, 3, 39])
        # A rolling window: expired samples leave from the front in O(1).
        self.assertIsInstance(client.vitals, collections.deque)
        stale = time.time() - post_mortem.VITALS_SECONDS - 5
        client.vitals.appendleft([stale - 5, 1, 155, 0, 39, 900])
        client.vitals.appendleft([stale - 10, 2, 155, 0, 39, 900])
        asyncio.run(client._handle_stats(Cursor(payload), payload))
        # Older expired samples go; the newest expired one stays as the
        # baseline (samples come only on change).
        cutoff = time.time() - post_mortem.VITALS_SECONDS
        self.assertEqual(sum(sample[0] < cutoff for sample in client.vitals), 1)
        self.assertLess(client.vitals[0][0], cutoff)

        def say(text, msg_type=2):
            data = bytes((msg_type,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._handle_drawinfo(Cursor(data), data))

        # A death sends its stat losses, then "YOU HAVE DIED." last.
        for line in ("The spell heals you for 12 hp!", "Sera starved to death.",
                     "You feel clumsy!", "YOU HAVE DIED."):
            say(line)
        self.assertEqual(len(client.post_mortems), 1)
        death = client.post_mortems[0]
        self.assertEqual((death["map"], death["x"], death["y"]),
                         ("/shattered_islands/world_6_46", 12, 14))
        self.assertEqual((death["cause"], death["detail"]), ("starvation", "ran out of food"))
        self.assertEqual(death["vitals"][-1][1:5], [13, 155, 3, 39])
        texts = [text for _, text in death["messages"]]
        self.assertIn("Sera starved to death.", texts)
        self.assertIn("You feel clumsy!", texts)
        self.assertIn("death", [entry["action"] for entry in death["actions"]])
        # The stat loss buffered before the death is counted with it, and the
        # post-mortem includes that bookkeeping.
        self.assertIn("depletion", [entry["action"] for entry in death["actions"]])
        self.assertIn("depletion", [entry["action"] for entry in death["decisions"]])
        # A player's chat quoting a notice is no death.
        say("[a=#charname]Mallory[/a]: YOU HAVE DIED.", c.CHAT_TYPE_CHAT)
        say("[a=#charname]Mallory[/a] tells you: You have been defeated in combat!",
            c.CHAT_TYPE_PRIVATE)
        self.assertEqual(len(client.post_mortems), 1)
        # Chat quoting her starvation, or another player starving, is not hers.
        say("[a=#charname]Mallory[/a]: Sera starved to death.", c.CHAT_TYPE_CHAT)
        say("Mallory starved to death.")
        say("YOU HAVE DIED.")
        self.assertEqual(len(client.post_mortems), 2)
        self.assertNotEqual(client.post_mortems[-1]["cause"], "starvation")
        # Each notice is its own defeat, however soon: a PvP-area defeat
        # sends only "defeated in combat", a death only "YOU HAVE DIED.".
        say("You have been defeated in combat!")
        say("YOU HAVE DIED.")
        self.assertEqual(len(client.post_mortems), 4)
        # Only the latest KEEP deaths are kept.
        for index in range(post_mortem.KEEP + 3):
            say("YOU HAVE DIED.")
        self.assertEqual(len(client.post_mortems), post_mortem.KEEP)

    def test_a_busy_minute_is_reviewed_whole_past_the_message_buffer(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        # More hits than the dashboard's 500-message buffer holds.
        for _ in range(520):
            say("giant rat hit you for 1 (-1) impact damage.")
        say("YOU HAVE DIED.")
        self.assertEqual(len(client.state.messages), 500)
        death = client.post_mortems[-1]
        self.assertEqual(death["damage_taken"], 520)
        self.assertEqual(death["attackers"], {"giant rat": 520})

    def test_restoring_post_mortems_inspects_a_bounded_number_of_candidates(self):
        from . import post_mortem
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client._memory_store().save("Sera", "post_mortems", [{"time": "invalid"}] * 10000)
            calls = []
            original = post_mortem.restore
            with patch.object(post_mortem, "restore", lambda value: calls.append(1) or original(value)):
                client.load_character_memory("Sera")
            self.assertLessEqual(len(calls), 4 * post_mortem.KEEP)
            self.assertEqual(client.post_mortems, [])

    def test_a_failed_post_mortem_save_never_stops_the_client(self):
        import sqlite3
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.player_name = "Sera"
        client.state.phase = "playing"

        class LockedDatabase:
            def save(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        client._memory_store = lambda: LockedDatabase()
        with self.assertLogs("atrinik_bot.client", "WARNING"):
            client._record_post_mortem()
        self.assertEqual(len(client.post_mortems), 1)

    def test_a_sudden_death_after_a_long_steady_spell_keeps_its_baseline(self):
        import struct
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
        # Healthy for longer than the window, then one fatal packet.
        client.vitals.append([time.time() - 900, 155, 155, 39, 39, 900])
        payload = b"".join(bytes((stat,)) + struct.pack(">" + c.STAT_FORMATS[stat], value)
                           for stat, value in ((c.STAT_HP, 0), (c.STAT_MAXHP, 155)))
        asyncio.run(client._handle_stats(Cursor(payload), payload))
        data = bytes((2,)) + b"ffffff\0" + b"YOU HAVE DIED.\0"
        asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        vitals = client.post_mortems[-1]["vitals"]
        # Charted from the healthy state at the window start to the fatal hit.
        self.assertEqual(vitals[0][1], 155)
        self.assertEqual(vitals[-1][1], 0)

    def test_report_fields_from_the_decision_ledger_are_sanitized(self):
        from . import post_mortem
        when = time.time()
        report = post_mortem.build(
            when, "/a", 1, 2, [], [], [],
            [{"time": when - 5, "action": float("nan"), "detail": "\ud800", "map": ["x"]}],
            name="Sera")
        decision = report["decisions"][-1]
        self.assertEqual(decision["action"], "nan")
        json.dumps(report, allow_nan=False).encode("utf-8")
        self.assertEqual(decision["map"], "")

    def test_a_slow_bookkeeping_save_never_moves_the_death_time(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
        clock = [1000.0]
        original = client.record_action

        def slow_record(*args, **kwargs):
            clock[0] += 30.0  # a locked database's wait, on the wall clock
            return original(*args, **kwargs)

        client.record_action = slow_record
        data = bytes((2,)) + b"ffffff\0" + b"YOU HAVE DIED.\0"
        with patch("atrinik_bot.client.time.time", lambda: clock[0]):
            asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        self.assertEqual(client.post_mortems[-1]["time"], 1000.0)

    def test_an_oversized_hit_amount_never_breaks_the_death_review(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        # A malicious server line: an amount past int()'s digit limit.
        say("giant rat hit you for " + "9" * 5000 + " (-1) impact damage.")
        say("giant rat hit you for 3 (-3) impact damage.")
        say("YOU HAVE DIED.")
        death = client.post_mortems[-1]
        self.assertEqual((death["damage_taken"], death["attackers"]), (3, {"giant rat": 1}))

    def test_an_oversized_ledger_number_never_breaks_the_death_review(self):
        from . import post_mortem
        when = time.time()
        report = post_mortem.build(
            when, "/a", 1, 2, [], [], [],
            [{"time": when - 5, "action": "emergency-disengage",
              "detail": "sp=" + "9" * 5000 + " next-heal=" + "9" * 5000}],
            name="Sera")
        self.assertEqual(report["decisions"][-1]["action"], "emergency-disengage")
        self.assertLessEqual(len(report["decisions"][-1]["detail"]), post_mortem.MAX_TEXT)

    def test_a_locked_database_delays_a_death_at_most_briefly(self):
        import sqlite3
        from . import client as client_module
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            # Another process holds the write lock.
            holder = sqlite3.connect(client._memory_store().path)
            holder.execute("BEGIN IMMEDIATE")
            try:
                started = time.monotonic()
                with self.assertLogs("atrinik_bot.client", "WARNING"):
                    client._record_post_mortem()
                elapsed = time.monotonic() - started
            finally:
                holder.rollback()
                holder.close()
            self.assertLess(elapsed, client_module.POST_MORTEM_SAVE_TIMEOUT + 1.0)
            self.assertEqual(len(client.post_mortems), 1)

    def test_a_reconnect_keeps_a_post_mortem_whose_save_failed(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            store = client._memory_store

            class LockedDatabase:
                def save(self, *args, **kwargs):
                    raise sqlite3.OperationalError("database is locked")

            client._memory_store = lambda: LockedDatabase()
            with self.assertLogs("atrinik_bot.client", "WARNING"):
                client._record_post_mortem()
            client._memory_store = store
            # The same character reconnects: the unsaved report survives.
            client.load_character_memory("Sera")
            self.assertEqual(len(client.post_mortems), 1)
            # Another character starts with none of hers.
            client.load_character_memory("Mallory")
            self.assertEqual(client.post_mortems, [])

    def test_an_unsaved_report_survives_a_switch_to_another_character_and_back(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            store = client._memory_store

            class LockedDatabase:
                def save(self, *args, **kwargs):
                    raise sqlite3.OperationalError("database is locked")

            client._memory_store = lambda: LockedDatabase()
            with self.assertLogs("atrinik_bot.client", "WARNING"):
                client._record_post_mortem()
            client._memory_store = store
            client.load_character_memory("Mallory")
            self.assertEqual(client.post_mortems, [])
            client.load_character_memory("Sera")
            self.assertEqual(len(client.post_mortems), 1)

    def test_colliding_attacker_labels_add_their_counts(self):
        from . import post_mortem
        long = "a" * 80
        restored = post_mortem.restore({
            "time": 1000.0, "attackers": {long + "x": 9, long + "y": 2, "\ud800": 3, "\udfff": 4}})
        self.assertEqual(restored["attackers"][long], 11)
        self.assertEqual(sum(restored["attackers"].values()), 18)

    def test_a_clean_shutdown_retries_unsaved_post_mortems(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            store = client._memory_store

            class LockedDatabase:
                def save(self, *args, **kwargs):
                    raise sqlite3.OperationalError("database is locked")

            client._memory_store = lambda: LockedDatabase()
            with self.assertLogs("atrinik_bot.client", "WARNING"):
                client._record_post_mortem()
            # Mallory's unsaved report waits in the per-character cache too.
            client._live_post_mortems["Mallory"] = [dict(client.post_mortems[0], time=5.0)]
            client._memory_store = store
            asyncio.run(client.close())
            self.assertEqual(len(store().load("Sera", "post_mortems", [])), 1)
            self.assertEqual(len(store().load("Mallory", "post_mortems", [])), 1)

    def test_an_oversized_stored_post_mortem_list_is_never_decoded(self):
        from . import post_mortem
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client._memory_store().save("Sera", "post_mortems", ["x" * 1024] * 9000)
            decoded = []
            original = json.loads
            with patch("atrinik_bot.memory.json.loads", lambda text, *a, **k: decoded.append(len(text)) or original(text, *a, **k)), \
                    self.assertLogs("atrinik_bot.memory", "WARNING"):
                client.load_character_memory("Sera")
            self.assertFalse([size for size in decoded if size > post_mortem.MAX_STORED_BYTES])
            self.assertEqual(client.post_mortems, [])
            # A normal list still loads.
            client._memory_store().save("Sera", "post_mortems", [{"time": 1000.0}])
            client.load_character_memory("Sera")
            self.assertEqual(len(client.post_mortems), 1)

    def test_a_deeply_nested_stored_value_is_invalid_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            store = client._memory_store()
            with store._connect() as db:
                db.execute("UPDATE memory SET value_json = ? WHERE kind = 'post_mortems'", ("[" * 100000 + "]" * 100000,))
                db.execute(
                    "INSERT OR REPLACE INTO memory (server, account, character, kind, value_json, updated_at) "
                    "VALUES (?, ?, ?, 'post_mortems', ?, 0)",
                    (store.server, store.account, "sera", "[" * 100000 + "]" * 100000))
            with self.assertLogs("atrinik_bot.memory", "WARNING"):
                client.load_character_memory("Sera")
            self.assertEqual(client.post_mortems, [])

    def test_live_vitals_are_bounded_by_count(self):
        from . import post_mortem
        client = AtrinikClient(ClientConfig(character="Sera"))
        self.assertEqual(client.vitals.maxlen, post_mortem.MAX_LIVE_VITALS)

    def test_the_other_characters_cache_is_capped(self):
        from . import post_mortem
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            # A hostile server switching through many distinct names.
            for index in range(50):
                client.post_mortems = [{"time": float(index)}]
                client.load_character_memory(f"name-{index}")
            self.assertEqual(len(client._live_post_mortems), post_mortem.MAX_CACHED_CHARACTERS)
            self.assertIn("name-48", client._live_post_mortems)
            self.assertNotIn("Sera", client._live_post_mortems)
            # A character with no reports takes no slot.
            client.post_mortems = []
            client.load_character_memory("empty")
            self.assertNotIn("name-49", client._live_post_mortems)

    def test_review_messages_are_bounded_in_size(self):
        from . import post_mortem
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        # A huge colour field and a huge text: neither is retained whole.
        data = bytes((2,)) + b"f" * 100000 + b"\0" + b"x" * 100000 + b"\0"
        asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        stamp, msg_type, text = client.review_messages[-1]
        self.assertEqual(len(text), post_mortem.MAX_TEXT)
        self.assertEqual((stamp, msg_type), tuple(client.state.messages[-1][:2]))

    def test_downsampling_follows_time_not_sample_count(self):
        from . import post_mortem
        # Baseline at 0 s, a plateau from 30 s, then a burst in the last second.
        samples = [[0.0, 100, 100, 50, 50, 900], [30.0, 80, 100, 50, 50, 900]]
        samples += [[59.0 + index / 1000, 80 - index % 2, 100, 50, 50, 900] for index in range(500)]
        kept = post_mortem.spread(samples)
        self.assertLessEqual(len(kept), post_mortem.MAX_VITALS)
        self.assertIn(samples[1], kept)
        self.assertEqual((kept[0], kept[-1]), (samples[0], samples[-1]))

    def test_a_death_without_evidence_is_unclassified(self):
        from . import post_mortem
        when = time.time()
        report = post_mortem.build(when, "/a", 1, 2, [], [], [], [], name="Sera")
        self.assertEqual(report["cause"], "unclassified")
        report = post_mortem.build(when, "/a", 1, 2, [],
                                   [(when - 5, 2, "giant rat hit you for 9 (-9) impact damage.")], [], [],
                                   name="Sera")
        self.assertEqual(report["cause"], "out-damaged")

    def test_a_character_switch_resets_the_previous_death_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client._post_mortem_booked = time.time()
            client.load_character_memory("Mallory")
            self.assertEqual(client._post_mortem_booked, 0.0)

    def test_extreme_restored_times_never_break_downsampling(self):
        from . import post_mortem
        rows = [[-1e308 + index * 1e306, 100, 100, 50, 50, 900] for index in range(200)]
        rows[-1][0] = 1e308
        restored = post_mortem.restore({"time": 1000.0, "vitals": rows})
        self.assertLessEqual(len(restored["vitals"]), post_mortem.MAX_VITALS)

    def test_the_ledger_death_bounds_a_life_without_its_report(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
        now = time.time()
        # An earlier death (no report for it) after an earlier life's disengage.
        client.decision_history = [
            {"time": now - 40, "action": "emergency-disengage", "detail": "sp=1 next-heal=9", "map": "/a"},
            {"time": now - 30, "action": "death", "detail": "YOU HAVE DIED.", "map": "/a"},
        ]
        data = bytes((2,)) + b"ffffff\0YOU HAVE DIED.\0"
        asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        death = client.post_mortems[-1]
        self.assertNotEqual(death["cause"], "resources")
        self.assertNotIn("emergency-disengage", [entry["action"] for entry in death["decisions"]])

    def test_a_death_notice_on_a_player_channel_is_no_death(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        for channel in (3, 4, 5, 6, 7, 8):
            for notice in (b"YOU HAVE DIED.", b"You have been defeated in combat!"):
                data = bytes((channel,)) + b"ffffff\0" + notice + b"\0"
                asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        self.assertEqual(client.post_mortems, [])
        self.assertNotIn("death", [entry["action"] for entry in client.action_history])

    def test_an_oversized_ledger_death_time_never_breaks_a_death(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.decision_history = [{"time": 10 ** 1000, "action": "death", "detail": "", "map": "/a"}]
        data = bytes((2,)) + b"ffffff\0YOU HAVE DIED.\0"
        asyncio.run(client._dispatch(c.C_DRAWINFO, data))
        self.assertEqual(len(client.post_mortems), 1)

    def test_the_current_death_is_never_its_own_boundary(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
        clock = [1000.0]

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            with patch("atrinik_bot.client.time.time", lambda: clock[0]):
                asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        say("giant rat hit you for 9 (-9) impact damage.")
        clock[0] = 1040.0
        original = client.record_action

        def stepped_back(*args, **kwargs):
            clock[0] = 1030.0  # the wall clock steps back during the bookkeeping
            return original(*args, **kwargs)

        client.record_action = stepped_back
        say("YOU HAVE DIED.")
        self.assertEqual(client.post_mortems[-1]["attackers"], {"giant rat": 1})

    def test_a_clock_corrected_back_past_a_death_never_mixes_lives(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
        clock = [960.0]

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            with patch("atrinik_bot.client.time.time", lambda: clock[0]):
                asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        say("ogre hit you for 50 (-50) impact damage.")
        clock[0] = 1000.0
        say("YOU HAVE DIED.")
        # The clock is corrected back past that death; the next life dies at 990.
        clock[0] = 975.0
        say("giant rat hit you for 3 (-3) impact damage.")
        clock[0] = 990.0
        say("YOU HAVE DIED.")
        self.assertEqual(client.post_mortems[-1]["attackers"], {"giant rat": 1})
        self.assertEqual(client.post_mortems[-1]["damage_taken"], 3)

    def test_a_reload_keeps_the_life_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
            clock = [960.0]

            def say(text):
                data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
                with patch("atrinik_bot.client.time.time", lambda: clock[0]):
                    asyncio.run(client._dispatch(c.C_DRAWINFO, data))

            with patch("atrinik_bot.client.time.time", lambda: clock[0]):
                client.record_action("emergency-disengage", "sp=1 next-heal=9")
            clock[0] = 1000.0
            say("YOU HAVE DIED.")
            # A reconnect reloads the ledger as new objects; then the clock
            # is corrected back past that death.
            client.load_character_memory("Sera")
            clock[0] = 990.0
            say("YOU HAVE DIED.")
            report = client.post_mortems[-1]
            self.assertNotEqual(report["cause"], "resources")
            self.assertNotIn("emergency-disengage", [entry["action"] for entry in report["decisions"]])

    def test_a_restarted_client_starts_the_life_after_the_ledger_death(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            first = AtrinikClient(ClientConfig(character="Sera", runtime_state_path=path))
            first.load_character_memory("Sera")
            first._memory_store().save("Sera", "decisions", [
                {"time": 960.0, "action": "emergency-disengage", "detail": "sp=1 next-heal=9", "map": "/a"},
                {"time": 1000.0, "action": "death", "detail": "YOU HAVE DIED.", "map": "/a"}])
            # A new process; the clock is then corrected back past that death.
            client = AtrinikClient(ClientConfig(character="Sera", runtime_state_path=path))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            client.state.phase = "playing"
            client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)
            data = bytes((2,)) + b"ffffff\0YOU HAVE DIED.\0"
            with patch("atrinik_bot.client.time.time", lambda: 990.0):
                asyncio.run(client._dispatch(c.C_DRAWINFO, data))
            report = client.post_mortems[-1]
            self.assertNotEqual(report["cause"], "resources")
            self.assertNotIn("emergency-disengage", [entry["action"] for entry in report["decisions"]])

    def test_restored_counts_are_never_negative(self):
        from . import post_mortem
        long = "a" * 80
        restored = post_mortem.restore({"time": 1000.0, "damage_taken": -5, "peak_attackers": -1,
                                        "attackers": {long + "x": 7, long + "y": -9, "rat": -2}})
        self.assertEqual(restored["attackers"][long], 7)
        self.assertEqual(restored["attackers"]["rat"], 0)
        self.assertEqual((restored["damage_taken"], restored["peak_attackers"]), (0, 0))

    def test_a_live_report_survives_a_clock_step_back(self):
        from . import post_mortem
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            stored = [{"time": 2000.0 + index} for index in range(post_mortem.KEEP)]
            client._memory_store().save("Sera", "post_mortems", stored)
            # The newest, unsaved report, recorded after the clock stepped back.
            client.post_mortems = [{"time": 1500.0, "map": "/a"}]
            client.load_character_memory("Sera")
            self.assertEqual(len(client.post_mortems), post_mortem.KEEP)
            self.assertEqual(client.post_mortems[-1]["time"], 1500.0)

    def test_merged_counts_stay_finite(self):
        from . import post_mortem
        long = "a" * 80
        restored = post_mortem.restore({"time": 1000.0, "attackers": {long + "x": 1e308, long + "y": 1e308}})
        self.assertEqual(restored["attackers"][long], post_mortem.MAX_COUNT_VALUE)
        json.dumps(restored, allow_nan=False)

    def test_a_character_switch_drops_the_previous_ones_buffered_death_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.state.player_name = "Sera"
            payload = bytes([2]) + b"ffffff\0You feel less potent!\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            # Sera disconnects before her notice; Mallory dies at once.
            client.load_character_memory("Mallory")
            client.state.player_name = "Mallory"
            payload = bytes([2]) + b"ffffff\0YOU HAVE DIED.\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.state.depletion_points, 0)
            self.assertNotIn("depletion", [entry["action"] for entry in client.action_history])

    def test_downsampling_keeps_the_window_baseline(self):
        from . import post_mortem
        samples = [[float(index), 100, 100, 50, 50, 900] for index in range(300)]
        # Every minimum sits right after the baseline.
        samples[1] = [1.0, 1, 100, 1, 50, 1]
        kept = post_mortem.spread(samples, keep=[1, 1, 1, 299])
        self.assertEqual(kept[0], samples[0])
        self.assertIn(samples[1], kept)
        self.assertEqual(kept[-1], samples[-1])
        self.assertLessEqual(len(kept), post_mortem.MAX_VITALS)

    def test_switching_characters_drops_the_previous_ones_transient_history(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.load_character_memory("Sera")
            client.vitals.append([time.time(), 10, 155, 0, 39, 900])
            client.review_messages.append((time.time(), 2, "", "ogre hit you for 99 (-99) impact damage."))
            client.action_history.append({"time": time.time(), "action": "fire"})
            # The same character again keeps them; another one starts clean.
            client.load_character_memory("Sera")
            self.assertEqual(len(client.vitals), 1)
            client.load_character_memory("Mallory")
            self.assertEqual((list(client.vitals), list(client.review_messages), client.action_history), ([], [], []))

    def test_a_post_mortem_names_the_square_of_the_death_notice(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        swamp, bed = "/shattered_islands/world_1_50", "/shattered_islands/world_0_68"
        client.set_savebed = lambda *args: None
        client.state.savebed_map, client.state.savebed_xy = bed, (4, 7)

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        # kill_player queues the notice before the savebed transfer's map
        # update, so the square at the notice is where she fell, even one she
        # just walked onto, the savebed included.
        client.state.map = MapState(path=swamp, world_x=10, world_y=3)
        say("You have been defeated in combat!")
        client.state.map = MapState(path=bed, world_x=4, world_y=7)
        say("Local medics have saved your life...")
        death = client.post_mortems[-1]
        self.assertEqual((death["map"], death["x"], death["y"]), (swamp, 10, 3))
        # Built once, ending at the notice: the medics line and anything
        # after it (the respawn, a next fight) are not part of it.
        self.assertNotIn("Local medics have saved your life...",
                         [text for _, text in death["messages"]])
        say("YOU HAVE DIED.")
        death = client.post_mortems[-1]
        self.assertEqual((death["map"], death["x"], death["y"]), (bed, 4, 7))
        self.assertEqual(len(client.post_mortems), 2)

    def test_a_quick_second_death_stays_out_of_the_first_report(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.phase = "playing"
        client.state.map = MapState(path="/shattered_islands/world_1_50", world_x=10, world_y=3)

        def say(text):
            data = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._dispatch(c.C_DRAWINFO, data))

        # The second notice lands inside the first's post-death window: the
        # first is finished before that notice is recorded, so neither
        # report holds both deaths.
        say("YOU HAVE DIED.")
        say("YOU HAVE DIED.")
        first, second = client.post_mortems
        for report in (first, second):
            self.assertEqual([text for _, text in report["messages"]].count("YOU HAVE DIED."), 1)
            self.assertEqual([entry["action"] for entry in report["actions"]].count("death"), 1)

    def test_a_post_mortem_charts_from_a_baseline_and_stops_activity_at_death(self):
        from .post_mortem import build

        when = 1000.0
        # Steady health long before the window, then one fatal hit.
        vitals = [[when - 300, 155, 155, 39, 39, 900], [when - 1, 0, 155, 39, 39, 900]]
        decisions = [{"time": when - 5, "action": "navigate", "detail": "to the farm"},
                     {"time": when + 1, "action": "farm-failure-review", "detail": "pack"}]
        death = build(when, "/a", 1, 2, vitals, [], decisions, decisions)
        self.assertEqual(len(death["vitals"]), 2)
        self.assertEqual(death["vitals"][0][1:], [155, 155, 39, 39, 900])
        self.assertLess(death["vitals"][0][0], when - 1)
        # A death soon after the previous one takes no baseline from that
        # earlier life: its window starts at the previous death.
        again = build(when, "/a", 1, 2, [[when - 50, 0, 155, 0, 39, 0], [when - 1, 0, 155, 39, 39, 900]],
                      [], [], [], since=when - 30)
        self.assertEqual([sample[0] for sample in again["vitals"]], [when - 1])
        # Nor does a decision after the death change its cause.
        late = build(when, "/a", 1, 2, [], [], [], [
            {"time": when + 1, "action": "emergency-disengage", "detail": "sp=0 next-heal=30"}])
        self.assertNotEqual(late["cause"], "resources")
        # The review written after the notice is not what she was doing.
        self.assertEqual([entry["action"] for entry in death["decisions"]], ["navigate"])
        self.assertEqual([entry["action"] for entry in death["actions"]], ["navigate"])

    def test_stored_post_mortems_are_validated_on_load(self):
        from . import post_mortem

        self.assertIsNone(post_mortem.restore({"time": "invalid"}))
        self.assertIsNone(post_mortem.restore({"time": float("nan")}))
        self.assertIsNone(post_mortem.restore("x"))
        restored = post_mortem.restore({
            "time": "12.5", "map": None, "x": "3", "y": 4, "cause": 7,
            "attackers": {"rat": 2, "bad": "x"}, "damage_taken": "lots",
            "vitals": [[1, 2, 3, 4, 5, 6], [1]], "messages": [[1, "hi"], "x"],
            "decisions": [{"action": "heal"}, None], "actions": "x"})
        self.assertEqual((restored["time"], restored["map"], restored["x"], restored["cause"]),
                         (12.5, "", 3, "7"))
        self.assertEqual(restored["attackers"], {"rat": 2})
        self.assertEqual(restored["damage_taken"], 0)
        self.assertEqual((len(restored["vitals"]), restored["messages"], restored["actions"]),
                         (1, [[1, "hi"]], []))
        self.assertEqual(restored["decisions"], [])
        # Nested numbers must be finite too, and records keep their shape.
        restored = post_mortem.restore({
            "time": 5, "vitals": [[1, float("nan"), 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]],
            "messages": [[float("inf"), "x"], [2, "ok"]], "attackers": {"rat": float("nan")},
            "decisions": [{"time": float("nan"), "action": "x"}, {"time": 3, "action": "heal", "extra": 1}]})
        self.assertEqual(restored["vitals"], [[1, 2, 3, 4, 5, 6]])
        self.assertEqual(restored["messages"], [[2, "ok"]])
        self.assertEqual(restored["attackers"], {})
        self.assertEqual(restored["decisions"], [{"time": 3, "action": "heal", "detail": "", "map": ""}])
        json.dumps(restored, allow_nan=False)
        # An integer too large for a float is rejected, not raised.
        huge = 10 ** 400
        restored = post_mortem.restore({"time": 5, "attackers": {"rat": huge}, "damage_taken": huge,
                                        "vitals": [[huge, 1, 2, 3, 4, 5]]})
        self.assertEqual((restored["attackers"], restored["damage_taken"], restored["vitals"]), ({}, 0, []))
        # Attacker and damage maps are bounded too, keeping the largest.
        many = post_mortem.restore({"time": 5, "attackers": {f"m{index}": index for index in range(1000)}})
        self.assertEqual(len(many["attackers"]), post_mortem.MAX_COUNTS)
        self.assertIn("m999", many["attackers"])
        # Text JSON allows but UTF-8 cannot carry (a lone surrogate) is replaced.
        odd = post_mortem.restore(json.loads('{"time": 5, "detail": "\\ud800", "attackers": {"\\udfff": 1},'
                                             ' "messages": [[1, "a\\ud800b"]]}'))
        json.dumps(odd, ensure_ascii=False).encode("utf-8")
        self.assertEqual(odd["detail"], "?")
        long = post_mortem.restore({"time": 5, "detail": "x" * 10000, "messages": [[1, "y" * 10000]]})
        self.assertEqual((len(long["detail"]), len(long["messages"][0][1])), (post_mortem.MAX_TEXT,) * 2)
        # Restoring applies the builder's bounds.
        big = post_mortem.restore({
            "time": 5, "vitals": [[index, 1, 2, 3, 4, 5] for index in range(500)],
            "messages": [[index, "x"] for index in range(500)],
            "decisions": [{"time": index} for index in range(500)],
            "actions": [{"time": index} for index in range(500)]})
        self.assertEqual([len(big[key]) for key in ("vitals", "messages", "decisions", "actions")],
                         [post_mortem.MAX_VITALS, 40, 25, 25])
        # An oversized stored collection is never traversed whole: only a
        # bounded tail is looked at, so a huge value restores as quickly.
        class Huge(list):
            def __iter__(self):
                raise AssertionError("iterated the whole list")
        huge = Huge([[1, "x"]] * 100000)
        restored = post_mortem.restore({"time": 5, "messages": huge})
        self.assertEqual(len(restored["messages"]), 40)
        # A previous death in the future bounds nothing, and an oversized
        # decision time is simply outside the window.
        report = post_mortem.build(1000.0, "/a", 1, 2, [], [(999.0, 2, "", "YOU HAVE DIED.")],
                                   [], [{"time": 10 ** 400, "action": "x"}], since=5000.0)
        self.assertEqual([text for _, text in report["messages"]], ["YOU HAVE DIED."])
        # Downsampling keeps the fatal sample among many.
        when = 1000.0
        busy = [[when - 59 + index * 0.4, 100, 155, 10, 39, 900] for index in range(140)]
        busy[-3] = [when - 0.2, 0, 155, 0, 39, 900]
        busy.append([when + 1, 155, 155, 39, 39, 900])
        death = post_mortem.build(when, "/a", 1, 2, busy, [], [], [])
        self.assertLessEqual(len(death["vitals"]), post_mortem.MAX_VITALS)
        self.assertIn([when - 0.2, 0, 155, 0, 39, 900], death["vitals"])
        # The recovery after the death is not part of its report.
        self.assertTrue(all(sample[0] <= when for sample in death["vitals"]))
        # Every charted vital keeps its low: a brief SP and food dip survives.
        dips = [[when - 59 + index * 0.4, 100, 155, 30, 39, 900] for index in range(140)]
        dips[37] = [dips[37][0], 100, 155, 0, 39, 900]
        dips[71] = [dips[71][0], 100, 155, 30, 39, 5]
        kept = post_mortem.build(when, "/a", 1, 2, dips, [], [], [])["vitals"]
        self.assertIn(dips[37], kept)
        self.assertIn(dips[71], kept)
        # Only textual scalars are restored as text: a stored list never is.
        self.assertEqual(post_mortem.restore({"time": 5, "detail": list(range(100000))})["detail"], "")
        self.assertEqual(post_mortem.restore({"time": 5, "cause": 7})["cause"], "7")
        # The most distinct attackers inside any pack window, in time order.
        from .self_improvement import PACK_WINDOW_SECONDS, review_failure
        fight = [(when - 40, 2, "", "rat hit you for 1 (-1) impact damage."),
                 (when - 39, 2, "", "ant hit you for 1 (-1) impact damage."),
                 (when - 38, 2, "", "slug hit you for 1 (-1) impact damage."),
                 (when - 38 + PACK_WINDOW_SECONDS + 5, 2, "", "rat hit you for 1 (-1) impact damage.")]
        review = review_failure(fight, [], {"time": when, "action": "death"}, [])
        self.assertEqual(review.peak_attackers, 3)

    def test_post_mortem_describes_the_fight_inside_the_window(self):
        from .post_mortem import build

        now = 1000.0
        messages = [(now - 120, 2, "", "ogre hit you for 99 (-99) impact damage."),
                    (now - 20, 2, "", "giant slug hit you for 30 (-30) acid damage."),
                    (now - 10, 2, "", "giant slug hit you for 25 (-25) acid damage."),
                    (now - 5, 2, "", "red ant hit you for 10 (-10) pierce damage.")]
        vitals = [[now - 300, 155, 155, 39, 39, 900], [now - 20, 80, 155, 10, 39, 900],
                  [now - 1, 2, 155, 0, 39, 900]]
        death = build(now, "/shattered_islands/world_13_73", 5, 6, vitals, messages,
                      [{"time": now - 3, "action": "fire", "detail": "minor healing",
                        "map": "/shattered_islands/world_13_73", "x": 5}], [])
        self.assertEqual(death["cause"], "pack")
        self.assertEqual(death["attackers"], {"giant slug": 2, "red ant": 1})
        self.assertEqual(death["damage_taken"], 65)
        self.assertEqual(death["damage_by_type"], {"acid": 55, "pierce": 10})
        # The steady state before the window charts from its start.
        self.assertEqual([sample[0] for sample in death["vitals"]], [now - 60, now - 20, now - 1])
        self.assertEqual(death["vitals"][0][1], 155)
        self.assertEqual(death["actions"], [{"time": now - 3, "action": "fire",
                                             "detail": "minor healing",
                                             "map": "/shattered_islands/world_13_73"}])
        # A death soon after a starvation death describes only this life.
        again = build(now, "/shattered_islands/world_13_73", 5, 6, vitals,
                      [(now - 40, 2, "", "Sera starved to death."), *messages], [], [],
                      since=now - 8)
        self.assertEqual(again["cause"], "out-damaged")
        self.assertEqual((again["attackers"], again["damage_taken"]), ({"red ant": 1}, 10))
        # A busy minute (500 samples) keeps 120 spread over all of it.
        busy = [[now - 60 + i * 0.12, 100, 155, 30, 39, 900] for i in range(500)]
        dense = build(now, "/shattered_islands/world_13_73", 5, 6, busy, [], [], [])
        self.assertEqual(len(dense["vitals"]), 120)
        self.assertEqual((dense["vitals"][0], dense["vitals"][-1]), (busy[0], busy[-1]))

    def test_client_records_map_arrivals(self):
        client = AtrinikClient(ClientConfig())
        client._observe_map_change("/a", MapState(path="/b"))
        client._observe_map_change("/b", MapState(path="/b"))
        self.assertEqual([path for _, path in client.map_arrivals], ["/b"])

    def test_trends_bucket_by_minute_and_derive_hourly_rates(self):
        from .trends import KEEP_SECONDS, Trends

        trends = Trends()
        state = GameState(phase="playing", player_tag=7)
        state.depletion_points_known = True
        state.depletion_points = 32
        start = 1_000_020.0
        for minute in range(61):
            state.stats.update(exp=1000 + 100 * minute, food=900 - minute, hp=100, maxhp=155)
            self.assertTrue(trends.observe(state, start + 60 * minute))
            # A second sample in the same minute starts nothing new.
            self.assertFalse(trends.observe(state, start + 60 * minute + 30))
            if minute in (30, 59):
                trends.count("deaths", start + 60 * minute + 40)
        for _ in range(5):
            trends.count("heals", start + 60 * 60)
        last = trends.summary(start + 3600)["points"][-1]
        self.assertEqual(last["xp_per_hour"], 6000)
        self.assertEqual((last["deaths_per_hour"], last["heals_per_hour"]), (2, 5))
        self.assertEqual((last["depletion"], last["food"], last["hp"]), (32, 840, 0.645))
        # The hour is by time: after a five-hour gap, old deaths drop out.
        state.stats.update(exp=8000)
        trends.observe(state, start + 3600 + 5 * 3600)
        late = trends.summary(start + 3600 + 5 * 3600)["points"][-1]
        self.assertEqual((late["deaths_per_hour"], late["xp_per_hour"]), (0, None))
        # The chart shows the day before the poll; older minutes drop out of it.
        trends.observe(state, start + KEEP_SECONDS + 3600)
        shown = trends.summary(start + KEEP_SECONDS + 3600)["points"]
        self.assertGreaterEqual(shown[0]["t"], start + 3600 - 60)
        # At most a day of minute buckets is stored.
        from .trends import MAX_BUCKETS
        many = Trends()
        for minute in range(MAX_BUCKETS + 5):
            many.observe(state, start + 60 * minute)
        self.assertEqual(len(many.buckets), MAX_BUCKETS)
        self.assertEqual(many.buckets[0]["t"], int((start + 60 * 5) // 60 * 60))
        # A clock that jumps two days ahead and back again keeps the history,
        # and the outlier minute is dropped rather than replayed later.
        jumpy = Trends()
        for minute in range(10):
            jumpy.observe(state, start + 60 * minute)
        jumpy.count("deaths", start + 3600)
        jumpy.observe(state, start + 2 * KEEP_SECONDS)
        jumpy.observe(state, start + 60 * 10)
        self.assertEqual(len(jumpy.buckets), 11)
        self.assertEqual(len(jumpy.summary(start + 60 * 10)["points"]), 11)
        jumpy.observe(state, start + 3600)
        self.assertEqual(jumpy.summary(start + 3600)["points"][-1]["deaths_per_hour"], 0)
        # Restored buckets survive; garbage does not.
        self.assertEqual(len(Trends([{"t": 60, "exp": 1}, "x", {"exp": 2}]).buckets), 1)

    def test_trend_rates_age_with_the_poll_and_survive_clock_steps(self):
        from .trends import Trends

        trends = Trends()
        state = GameState(phase="playing", player_tag=7)
        state.stats.update(exp=1000, food=500, hp=100, maxhp=100)
        start = 2_000_040.0
        trends.observe(state, start)
        trends.count("deaths", start + 10)
        self.assertEqual(trends.summary(start + 30)["points"][-1]["deaths_per_hour"], 1)
        # No new minute is recorded for two hours: the latest point is the
        # poll time's minute, and the death has aged out of its hour.
        quiet = trends.summary(start + 2 * 3600)
        self.assertEqual(quiet["points"][-1]["t"], int((start + 2 * 3600) // 60 * 60))
        self.assertEqual(quiet["points"][-1]["deaths_per_hour"], 0)
        self.assertEqual(quiet["points"][-1]["food"], 500)
        # The wall clock steps back two minutes and catches up again: every
        # minute keeps one bucket, the buckets stay in time order, and the
        # minute the clock ran ahead to is not double counted.
        trends.observe(state, start + 600)
        trends.observe(state, start + 480)
        trends.count("heals", start + 600)
        times = [bucket["t"] for bucket in trends.buckets]
        self.assertEqual(times, sorted(set(times)))
        self.assertEqual(sum(bucket["heals"] for bucket in trends.buckets), 1)

    def test_a_failed_trend_save_never_stops_the_client(self):
        client = AtrinikClient(ClientConfig())
        client.state.player_name = "Sera"

        class FullDisk:
            def save(self, *args, **kwargs):
                raise OSError(28, "No space left on device")

        client._memory_store = lambda: FullDisk()
        with self.assertLogs("atrinik_bot.client", "WARNING"):
            client._persist_trends()

    def test_client_counts_heals_and_deaths_into_trends(self):
        client = AtrinikClient(ClientConfig())

        def say(text, msg_type=2):
            data = bytes((msg_type,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._handle_drawinfo(Cursor(data), data))

        say("The spell heals you for 12 hp!")
        say("The spell heals you for 9 hp!")
        say("You have been defeated in combat!")
        say("YOU HAVE DIED.")
        # A player's chat quoting a notice is no death.
        say("[a=#charname]Mallory[/a]: YOU HAVE DIED.", c.CHAT_TYPE_CHAT)
        say("[a=#charname]Mallory[/a] tells you: You have been defeated in combat!",
            c.CHAT_TYPE_PRIVATE)
        bucket = client.trends.buckets[-1]
        # Each notice is its own defeat: a PvP-area defeat sends only
        # "defeated in combat", a death only "YOU HAVE DIED.".
        self.assertEqual((bucket["heals"], bucket["deaths"]), (2, 2))
        # Dying again right after respawning is another death.
        say("YOU HAVE DIED.")
        self.assertEqual(client.trends.buckets[-1]["deaths"], 3)
        # Depletion changes between stats packets reach the bucket.
        client.set_depletion_points(7)
        self.assertEqual(client.trends.buckets[-1]["depletion"], 7)
        # Closing flushes the current minute.
        saved = []
        client.state.player_name = "Sera"
        client._memory_store = lambda: type("M", (), {
            "save": lambda self, character, kind, value: saved.append((character, kind))})()
        asyncio.run(client.close())
        self.assertIn(("Sera", "trends"), saved)

    def test_reconnect_reload_keeps_the_live_trend_minute(self):
        client = AtrinikClient(ClientConfig())
        start = client.trends.buckets[0]["t"] if client.trends.buckets else None
        self.assertIsNone(start)

        class Memory:
            def __init__(self, trends):
                self.trends = trends

            def import_legacy(self, *args):
                pass

            def load(self, character, kind, default, **kwargs):
                return self.trends if kind == "trends" else default

            def save(self, *args, **kwargs):
                pass

        now = time.time()
        minute = int(now // 60 * 60)
        # Login: the saved snapshot has an older, partial copy of this minute.
        client._memory_store = lambda: Memory([{"t": minute - 60, "deaths": 1, "heals": 0},
                                               {"t": minute, "deaths": 0, "heals": 1}])
        client.load_character_memory("Sera")
        client.trends.count("heals", now)
        client.trends.count("deaths", now)
        # A reconnect reloads the same character: the live minute survives.
        client.load_character_memory("Sera")
        bucket = client.trends.buckets[-1]
        self.assertEqual((bucket["t"], bucket["heals"], bucket["deaths"]), (minute, 2, 1))
        self.assertEqual(client.trends.buckets[0]["deaths"], 1)
        # Another character starts from its own saved snapshot.
        client._memory_store = lambda: Memory([])
        client.load_character_memory("Other")
        self.assertEqual(client.trends.buckets, [])

    def test_alerts_flag_deaths_starvation_stuck_and_disconnects(self):
        from .alerts import (DISCONNECTED_SECONDS, STUCK_SECONDS, ProgressWatch, active)

        now = 10_000.0
        state = GameState(phase="playing", player_tag=7)
        state.stats.update(exp=500, food=300)
        state.map = MapState(path="/a", world_x=1, world_y=1)
        watch = ProgressWatch(now)
        watch.observe(state, now)

        def ids(at, decisions=(), autoplay=True, started=0.0):
            return [alert["id"] for alert in active(
                state, decisions, watch, autoplay_started_at=started if autoplay else None,
                now=at)]

        self.assertEqual(ids(now), [])
        deaths = [{"action": "death", "time": now - 3000}, {"action": "death", "time": now - 60},
                  {"action": "death", "time": now - 7200}]
        self.assertEqual(ids(now, deaths), ["deaths"])
        self.assertEqual(ids(now, deaths[:1]), [])
        # Each notice is its own defeat: a PvP-area defeat sends only
        # "defeated in combat", a death only "YOU HAVE DIED.".
        self.assertEqual(ids(now, [
            {"action": "death", "time": now - 61, "detail": "You have been defeated in combat!"},
            {"action": "death", "time": now - 60, "detail": "YOU HAVE DIED."}]), ["deaths"])
        self.assertEqual(ids(now, [{"action": "death", "time": now - 9, "detail": "YOU HAVE DIED."},
                                   {"action": "death", "time": now - 1, "detail": "YOU HAVE DIED."}]),
                         ["deaths"])
        state.stats["food"] = 0
        self.assertEqual(ids(now), ["starving"])
        state.stats["food"] = 300
        # Idle before autoplay started is not being stuck; the task's own
        # start time anchors it, whenever the dashboard looks.
        self.assertEqual(ids(now + STUCK_SECONDS, started=now + 60), [])
        # No XP and no movement for the stuck window while autoplay runs.
        later = now + STUCK_SECONDS
        self.assertEqual(ids(later), ["stuck"])
        self.assertEqual(ids(later, autoplay=False), [])
        state.map.world_x = 2
        watch.observe(state, later)
        self.assertEqual(ids(later + 60), [])
        # A lost connection, only after a grace period, through retries.
        state.phase = "disconnected"
        watch.observe(state, later)
        self.assertEqual(ids(later + 60), [])
        state.phase = "connecting"
        watch.observe(state, later + 90)
        state.phase = "disconnected"
        watch.observe(state, later + 120)
        self.assertEqual(ids(later + DISCONNECTED_SECONDS), ["disconnected"])
        state.phase = "playing"
        watch.observe(state, later + DISCONNECTED_SECONDS)
        self.assertIsNone(watch.disconnected_since)
        # A failed reconnect's empty placeholder map and cleared stats are no
        # movement or progress, nor is the real map coming back unchanged.
        moved, gained = watch.moved_at, watch.exp_changed_at
        real_map, real_stats = state.map, dict(state.stats)
        state.phase, state.map, state.stats = "connecting", MapState(), {}
        watch.observe(state, later + 400)
        state.phase = "disconnected"
        watch.observe(state, later + 410)
        state.phase, state.map, state.stats = "playing", real_map, real_stats
        watch.observe(state, later + 500)
        self.assertEqual((watch.moved_at, watch.exp_changed_at), (moved, gained))

    def test_each_death_keeps_its_own_record_and_time(self):
        from .alerts import death_count

        client = AtrinikClient(ClientConfig())
        client.state.map.path = "/shattered_islands/world_6_46"

        def say(text, msg_type=2):
            data = bytes((msg_type,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._handle_drawinfo(Cursor(data), data))

        clock = [time.time()]
        with patch("time.time", lambda: clock[0]):
            for _ in range(2):
                say("YOU HAVE DIED.")
                clock[0] += 3
        # Rapid repeats are not coalesced: each death ages out on its own.
        deaths = [record for record in client.decision_history if record["action"] == "death"]
        self.assertEqual([record.get("count") for record in deaths], [None, None])
        self.assertEqual(death_count(client.decision_history, 0.0), 2)
        self.assertEqual(death_count(client.decision_history, deaths[0]["time"] + 1), 1)
        # A player's chat quoting a notice is no death.
        say("[a=#charname]Mallory[/a]: YOU HAVE DIED.", c.CHAT_TYPE_CHAT)
        self.assertEqual(death_count(client.decision_history, 0.0), 2)

    def test_client_starts_the_outage_clock_when_the_connection_drops(self):
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        before = time.time()
        asyncio.run(client.close())
        self.assertIsNotNone(client.progress_watch.disconnected_since)
        self.assertGreaterEqual(client.progress_watch.disconnected_since, before)

    def test_configured_party_forms_then_rejoins_existing_open_party(self):
        client = AtrinikClient(ClientConfig(party_name="Sera"))
        commands = []

        async def execute(command):
            commands.append(command)

        client.execute_client_command = execute
        asyncio.run(client._ensure_configured_party())
        self.assertEqual(commands, ["/party form Sera"])

        text = "The party Sera already exists, pick another name."
        payload = bytes((2,)) + b"ffffff\0" + text.encode() + b"\0"
        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
        self.assertEqual(commands,
                         ["/party form Sera", "/party join Sera"])

        formed = "You have formed party: Sera"
        payload = bytes((2,)) + b"ffffff\0" + formed.encode() + b"\0"
        client._party_setup_pending = "form"
        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
        self.assertEqual(client.state.party_name, "Sera")
        self.assertEqual(client._party_setup_pending, "")

    def test_direct_global_location_question_gets_bounded_reply(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.map.name = "Northern Foothills"
        client.state.map.region_longname = "Strakewood Island"
        commands = []

        async def command(value):
            commands.append(value)

        client.command = command
        text = ("[a=#charname]Kitty[/a]: "
                "Sera, where are you?")
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"
        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, [
            "/chat Kitty, I'm in Northern Foothills in Strakewood Island "
            "right now.",
        ])

    def test_unknown_direct_chat_is_recorded_for_rule_review(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        decisions = []
        client.record_action = lambda action, detail="": (
            decisions.append((action, detail)))
        text = ("[a=#charname]Kitty[/a]: Sera, what is the airspeed "
                "velocity of an unladen swallow?")
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(decisions[0][0], "chat-unhandled")
        self.assertIn("sender=Kitty", decisions[0][1])
        self.assertIn("airspeed velocity", decisions[0][1])

    def test_live_unhandled_wellbeing_prompt_becomes_a_response(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.stats.update(hp=202, maxhp=202)
        commands = []

        async def command(value):
            commands.append(value)

        client.command = command
        text = ("[a=#charname]Kitty[/a]: "
                "How are you doing Sera? :3")
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, [
            "/chat Kitty, Doing well, thanks! Just out training.",
        ])

    def test_public_farming_invite_cannot_redirect_autoplay(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        commands = []

        async def command(value):
            commands.append(value)

        client.command = command
        text = ("[a=#charname]Kitty[/a]: Sera would you like to come join "
                "me farming the Swamp near Asteria?")
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, [
            "/chat Kitty, Thanks! I'm following a different training route "
            "right now, though.",
        ])

    def test_direct_farming_tip_is_acknowledged_and_recorded(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        commands = []
        decisions = []

        async def command(value):
            commands.append(value)

        client.command = command
        client.record_action = lambda action, detail="": (
            decisions.append((action, detail)))
        text = ("[a=#charname]Kitty[/a]: Sera it's basically the best "
                "farming spot for our level")
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, [
            "/chat Kitty, Oh nice, thanks for the tip! I'll check it out.",
        ])
        self.assertEqual(decisions[0][0], "chat-farming-tip")
        self.assertIn("best farming spot", decisions[0][1])

    def test_direct_small_talk_has_natural_bounded_responses(self):
        cases = (
            ("Thanks Sera!", "You're welcome!"),
            ("Good luck Sera!", "Thanks! You too."),
            ("Do you like frogs, Sera?", "I'm still figuring that one out!"),
            ("Bye Sera!", "See you around!"),
            ("That's cool, Sera!", "Yeah! :)"),
        )
        for prompt, response in cases:
            with self.subTest(prompt=prompt):
                client = AtrinikClient(ClientConfig(character="Sera"))
                commands = []

                async def command(value):
                    commands.append(value)

                client.command = command
                text = f"[a=#charname]Kitty[/a]: {prompt}"
                payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
                    text.encode() + b"\0"
                asyncio.run(client._handle_drawinfo(
                    Cursor(payload), payload))
                self.assertEqual(
                    commands, [f"/chat Kitty, {response}"])

    def test_recent_chat_partner_can_follow_up_without_repeating_name(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client._chat_correspondents["kitty"] = time.monotonic()
        commands = []

        async def command(value):
            commands.append(value)

        client.command = command
        text = "[a=#charname]Kitty[/a]: Thanks!"
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, ["/chat Kitty, You're welcome!"])

    def test_chat_rules_hot_reload_without_recreating_client(self):
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "chat_rules.json"
            rules_path.write_text('{"rules": []}')
            client = AtrinikClient(ClientConfig(
                character="Sera", chat_rules_path=str(rules_path)))
            commands = []
            decisions = []

            async def command(value):
                commands.append(value)

            client.command = command
            client.record_action = lambda action, detail="": (
                decisions.append((action, detail)))

            def deliver():
                text = "[a=#charname]Kitty[/a]: Sera, ribbit?"
                payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
                    text.encode() + b"\0"
                asyncio.run(client._handle_drawinfo(
                    Cursor(payload), payload))

            deliver()
            self.assertEqual(decisions[-1][0], "chat-unhandled")
            self.assertEqual(commands, [])

            rules_path.write_text(json.dumps({"rules": [{
                "id": "frog",
                "patterns": [r"\bribbit\b"],
                "responses": ["Ribbit! :)"],
            }]}))
            deliver()

            self.assertEqual(commands, ["/chat Kitty, Ribbit! :)"])
            self.assertEqual(client.chat_policy_status()["rules"], 1)

    def test_chat_reports_equipped_training_skill(self):
        client = AtrinikClient(ClientConfig(character="Sera"))
        client.state.player_tag = 7
        skill = Item(
            40, item_type=c.TYPE_SKILL, name="slash weapons",
            extra={"level": 20})
        weapon = Item(
            50, item_type=c.TYPE_WEAPON, name="steel falchion",
            required_skill_tag=40)
        client.state.place_item(skill, 7)
        client.state.place_item(weapon, 7)
        client.state.equipment[c.EQUIP_WEAPON] = 50
        client._chat_correspondents["kitty"] = time.monotonic()
        commands = []

        async def command(value):
            commands.append(value)

        client.command = command
        text = "[a=#charname]Kitty[/a]: Training what?"
        payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
            text.encode() + b"\0"

        asyncio.run(client._handle_drawinfo(Cursor(payload), payload))

        self.assertEqual(commands, [
            "/chat Kitty, I'm training Slash, currently level 20.",
        ])

    def test_chat_truthfully_reports_existing_swamp_destination(self):
        cases = (
            "Are you going to join me at the Asteria swamp?",
            "I'd like to show you how I farm at the swamp near Asteria, Sera",
        )
        for prompt in cases:
            with self.subTest(prompt=prompt):
                client = AtrinikClient(ClientConfig(character="Sera"))
                client.chat_context_provider = lambda: {
                    "destination": "/shattered_islands/world_1_50",
                }
                client._chat_correspondents["kitty"] = time.monotonic()
                commands = []

                async def command(value):
                    commands.append(value)

                client.command = command
                text = f"[a=#charname]Kitty[/a]: {prompt}"
                payload = bytes((c.CHAT_TYPE_CHAT,)) + b"orange\0" + \
                    text.encode() + b"\0"
                asyncio.run(client._handle_drawinfo(
                    Cursor(payload), payload))
                self.assertEqual(commands, [
                    "/chat Kitty, Yep—I'm already on my way to the Asteria "
                    "swamp. I'm taking a careful route through the mountains.",
                ])

    def test_action_history_is_bounded_and_map_aware(self):
        client = AtrinikClient(ClientConfig())
        client.state.map.path = "/test"
        client.state.map.world_x = 4
        client.state.map.world_y = 7
        client.action_context = "farm:test"
        for index in range(510):
            client.record_action("step", str(index))
        self.assertEqual(len(client.action_history), 500)
        self.assertEqual(client.action_history[0]["detail"], "10")
        self.assertEqual(client.action_history[-1]["task"], "farm:test")
        self.assertEqual((client.action_history[-1]["map"],
                          client.action_history[-1]["x"],
                          client.action_history[-1]["y"]),
                         ("/test", 4, 7))


    def test_combat_death_is_recorded_in_activity_history(self):
        client = AtrinikClient(ClientConfig())
        for message in ("You have been defeated in combat!",
                        "YOU HAVE DIED."):
            payload = bytes([2]) + b"ffffff\0" + message.encode() + b"\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.action_history[-1]["action"], "death")
            self.assertEqual(client.action_history[-1]["detail"], message)

    def test_decision_history_persists_without_raw_step_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            client.state.player_name = "Sera"
            client.state.player_tag = 7
            client.load_character_memory("Sera")
            client.record_action("step", "direction=1")
            client.record_action("retreat", "direction=5")
            client.record_action("retreat", "direction=5")
            client.record_action("task-start", "farm-circuit")
            self.assertEqual(
                [entry["action"] for entry in client.decision_history],
                ["retreat", "task-start"])
            self.assertEqual(client.decision_history[0]["count"], 2)
            restored = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restored.load_character_memory("Sera")
            self.assertEqual(
                [entry["action"] for entry in restored.decision_history],
                ["retreat", "task-start"])
            self.assertEqual(restored.decision_history[0]["count"], 2)

    def test_sqlite_memory_is_scoped_by_server_account_and_character(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.sqlite3")

            def client(host="server-a", account="account-a",
                       character="Sera"):
                value = AtrinikClient(ClientConfig(
                    host=host, account=account, character=character,
                    runtime_state_path=path))
                value.state.player_name = character
                value.load_character_memory(character)
                return value

            sera = client()
            sera.set_bank_balance(4321)
            self.assertEqual(client().state.bank_balance, 4321)
            self.assertFalse(client(host="server-b").state.bank_balance_known)
            self.assertFalse(client(account="account-b").state.bank_balance_known)
            self.assertFalse(client(character="Alt").state.bank_balance_known)

    def test_dialogue_vendor_quotes_survive_restart_in_character_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.sqlite3")
            config = ClientConfig(
                host="zoey", account="account-a", character="Sera",
                runtime_state_path=path)
            client = AtrinikClient(config)
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            client.record_vendor_quote(
                "asteria:merchant:drumstick", "chicken drumstick", 18)

            restored = AtrinikClient(config)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.vendor_quotes[
                    "asteria:merchant:drumstick"]["unit_cost"], 18)

            other = AtrinikClient(ClientConfig(
                host="zoey", account="account-a", character="Alt",
                runtime_state_path=path))
            other.load_character_memory("Alt")
            self.assertEqual(other.state.vendor_quotes, {})

    def test_sqlite_memory_imports_legacy_json_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            activity_path = Path(directory) / "state.activity.json"
            state_text = json.dumps({
                "Sera": {"depletion_points": 2,
                         "apartment_bed_bound": True},
            })
            activity_text = json.dumps({
                "Sera": [{"time": 1.0, "action": "retreat",
                          "detail": "safe", "task": "farm",
                          "map": "/a", "x": 1, "y": 2}],
            })
            state_path.write_text(state_text)
            activity_path.write_text(activity_text)
            client = AtrinikClient(ClientConfig(
                host="zoey", account="account-a", character="Sera",
                runtime_state_path=str(state_path)))
            client.load_character_memory("Sera")

            self.assertEqual(client.state.depletion_points, 2)
            self.assertTrue(client.state.apartment_bed_bound)
            # Records from before savebeds were tracked by location.
            self.assertEqual(client.state.savebed_map, STARTER_APARTMENT)
            self.assertEqual(client.state.savebed_xy, STARTER_APARTMENT_BED)
            self.assertEqual(client.decision_history[0]["action"], "retreat")
            self.assertTrue(state_path.with_suffix(".sqlite3").exists())
            self.assertEqual(state_path.read_text(), state_text)
            self.assertEqual(activity_path.read_text(), activity_text)

    def test_lore_attempt_counts_survive_new_tags_and_retry_after_level(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.sqlite3")

            def inventory(level, tags):
                literacy = Item(
                    1, item_type=c.TYPE_SKILL, name="literacy",
                    extra={"level": level})
                return [literacy] + [
                    Item(tag, face=81, item_type=c.TYPE_BOOK,
                         quality=100, name="paper list of creatures")
                    for tag in tags
                ]

            client = AtrinikClient(ClientConfig(
                host="zoey", account="account-a", character="Sera",
                runtime_state_path=path))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            for item in inventory(2, (101, 102)):
                client.state.place_item(item, client.state.player_tag)
            client.mark_lore_book_attempted(client.state.inventory[1])
            client.mark_lore_book_attempted(client.state.inventory[2])

            restored = AtrinikClient(ClientConfig(
                host="zoey", account="account-a", character="Sera",
                runtime_state_path=path))
            restored.state.player_name = "Sera"
            restored.state.player_tag = 7
            restored.load_character_memory("Sera")
            for item in inventory(2, (901, 902, 903)):
                restored.state.place_item(
                    item, restored.state.player_tag)
            self.assertTrue(restored.lore_book_attempted(
                restored.state.inventory[1]))
            self.assertTrue(restored.lore_book_attempted(
                restored.state.inventory[2]))
            self.assertFalse(restored.lore_book_attempted(
                restored.state.inventory[3]))

            restored.state.items.clear()
            restored.state.inventories.clear()
            for item in inventory(3, (901, 902)):
                restored.state.place_item(
                    item, restored.state.player_tag)
            self.assertFalse(restored.lore_book_attempted(
                restored.state.inventory[1]))

    def test_savebed_location_persists_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            self.assertEqual((client.state.savebed_map, client.state.savebed_xy), ("", None))
            client.set_savebed("/shattered_islands/world_6_48_1", 1, 12)
            self.assertFalse(client.state.apartment_bed_bound)
            restarted = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restarted.state.player_name = "Sera"
            restarted.load_character_memory("Sera")
            self.assertEqual(restarted.state.savebed_map, "/shattered_islands/world_6_48_1")
            self.assertEqual(restarted.state.savebed_xy, (1, 12))
            restarted.set_savebed(STARTER_APARTMENT, *STARTER_APARTMENT_BED)
            self.assertTrue(restarted.state.apartment_bed_bound)

    def test_forgotten_bank_balance_is_unknown_and_zero_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            client.set_bank_balance(4_524)
            client.forget_bank_balance()
            self.assertEqual((client.state.bank_balance, client.state.bank_balance_known),
                             (0, False))
            restarted = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restarted.state.player_name = "Sera"
            restarted.load_character_memory("Sera")
            self.assertEqual((restarted.state.bank_balance,
                              restarted.state.bank_balance_known), (0, False))

    def test_depletion_points_persist_and_count_only_in_death_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            self.assertEqual(client.state.depletion_points, 0)
            death = "You have been defeated in combat!"
            loss = "You feel clumsy!"
            for message in (death, loss, loss):
                payload = bytes([2]) + b"ffffff\0" + message.encode() + b"\0"
                asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.state.depletion_points, 2)
            restored = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restored.load_character_memory("Sera")
            self.assertEqual(restored.state.depletion_points, 2)
            client.set_last_upgrade_shop_sweep(
                1234.5, level=13, wallet=4_817, policy=2)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.last_upgrade_shop_sweep_at, 1234.5)
            self.assertEqual(
                restored.state.last_upgrade_shop_sweep_level, 13)
            self.assertEqual(
                restored.state.last_upgrade_shop_sweep_wallet, 4_817)
            self.assertEqual(
                restored.state.last_upgrade_shop_sweep_policy, 2)
            client.set_active_upgrade_shop_sweep(76, policy=5)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.active_upgrade_shop_sweep_cursor, 76)
            self.assertEqual(
                restored.state.active_upgrade_shop_sweep_policy, 5)
            client.set_last_recall_shop_check(2345.5)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.last_recall_shop_check_at, 2345.5)
            client.set_last_utility_shop_check(3456.5)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.last_utility_shop_check_at, 3456.5)
            client.set_apartment_bed_bound(True)
            restored.load_character_memory("Sera")
            self.assertTrue(restored.state.apartment_bed_bound)
            checked_at = time.time() - 60
            client.set_farm_zone_last_checked("/rare", checked_at)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.farm_zone_last_checked["/rare"], checked_at)
            client.set_bank_balance(2176)
            restored.load_character_memory("Sera")
            self.assertTrue(restored.state.bank_balance_known)
            self.assertEqual(restored.state.bank_balance, 2176)

            deposited_at = time.time() - 120
            client.set_last_bank_deposit(deposited_at)
            restored.load_character_memory("Sera")
            self.assertEqual(
                restored.state.last_bank_deposit_at, deposited_at)
            paid_until = time.time() + 6 * 3600
            client.defer_paid_restoration(paid_until)
            restored.load_character_memory("Sera")
            self.assertAlmostEqual(
                restored.state.paid_restoration_retry_until, paid_until)
            quarantine_until = time.time() + 3600
            client.quarantine_farm_zone("/dangerous/farm", quarantine_until)
            restored.load_character_memory("Sera")
            self.assertAlmostEqual(
                restored.state.farm_zone_quarantine["/dangerous/farm"],
                quarantine_until)
            client.set_bank_balance(
                9999, observed_at=time.time() -
                AtrinikClient.BANK_BALANCE_CACHE_SECONDS - 1)
            restored.load_character_memory("Sera")
            self.assertFalse(restored.state.bank_balance_known)
            self.assertEqual(restored.state.bank_balance, 0)
            restored._death_depletion_until = 0.0
            payload = bytes([2]) + b"ffffff\0You feel clumsy!\0"
            asyncio.run(restored._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(restored.state.depletion_points, 2)

    def test_prelogin_action_cannot_overwrite_character_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "memory.sqlite3")
            first = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            first.state.player_name = "Sera"
            first.load_character_memory("Sera")
            first.record_action("durable-observation", "keep me")

            restarted = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restarted.record_action("task-start", "pre-login")
            restarted.load_character_memory("Sera")

            self.assertTrue(any(
                entry.get("action") == "durable-observation"
                for entry in restarted.decision_history))

    def test_depletion_buffers_server_losses_emitted_before_death(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera",
                runtime_state_path=str(Path(directory) / "state.json")))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            for message in ("You feel less potent!", "You feel stupid!"):
                payload = bytes([2]) + b"ffffff\0" + message.encode() + b"\0"
                asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.state.depletion_points, 0)
            payload = bytes([2]) + b"ffffff\0YOU HAVE DIED.\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.state.depletion_points, 2)
            self.assertEqual(
                [entry["action"] for entry in client.action_history[-3:]],
                ["death", "depletion", "depletion"])

    def test_cursor(self):
        cur = Cursor(b"\x01\xff\x00\x03hello\0")
        self.assertEqual(cur.u8(), 1)
        self.assertEqual(cur.i8(), -1)
        self.assertEqual(cur.u16(), 3)
        self.assertEqual(cur.cstring(), "hello")


class QuestTests(unittest.TestCase):
    def test_dashboard_experience_progress_uses_server_thresholds(self):
        thresholds = experience_thresholds()
        self.assertEqual(thresholds[9:11], (250000, 500000))
        self.assertEqual(thresholds[30], 20000000)
        progress = experience_progress(9, 366608)
        self.assertEqual(progress["level_experience"], 250000)
        self.assertEqual(progress["next_experience"], 500000)
        self.assertEqual(progress["remaining_experience"], 133392)
        self.assertEqual(progress["progress_percent"], 46.6)

    def test_inventory_policy_preserves_but_does_not_lock_stackables(self):
        policy = InventoryPolicy()
        coin = Item(1, flags=c.ITEM_LOCKED, item_type=c.TYPE_MONEY,
                    quality=100, name="copper coin", quantity=50)
        gem = Item(2, item_type=c.TYPE_GEM, name="ruby")
        ring = Item(3, flags=c.ITEM_MAGICAL, item_type=c.TYPE_RING,
                    name="bronze ring (mana+5)")
        key = Item(4, item_type=21, name="Incuna Western Gate Key")
        self.assertTrue(policy.preserve(coin))
        self.assertTrue(policy.preserve(gem))
        self.assertFalse(policy.should_lock(coin))
        self.assertFalse(policy.should_lock(gem))
        self.assertTrue(policy.should_lock(ring))
        self.assertTrue(policy.should_lock(key))
        quality_89 = Item(
            5, item_type=c.TYPE_WEAPON, quality=89, condition=89,
            name="hardened steel ancus")
        quality_90 = Item(
            6, item_type=c.TYPE_WEAPON, quality=90, condition=90,
            name="hardened steel ancus")
        self.assertFalse(policy.preserve(quality_89))
        self.assertTrue(policy.preserve(quality_90))

    def test_inventory_maintenance_unlocks_an_existing_coin_stack(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"level": 1},
                    "inventory": [Item(
                        17, flags=c.ITEM_LOCKED, item_type=c.TYPE_MONEY,
                        quality=100, name="copper coin", quantity=12)],
                    "items": {}, "equipment": {},
                })()
                self.locked = []

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        changed = asyncio.run(FarmTask().maintain_inventory(client))
        self.assertTrue(changed)
        self.assertEqual(client.locked, [17])

    def test_inventory_maintenance_unlocks_ordinary_identified_loot(self):
        class FakeClient:
            def __init__(self):
                item = Item(
                    18, flags=c.ITEM_LOCKED, item_type=c.TYPE_WEAPON,
                    quality=89, condition=89,
                    name="hardened steel ancus")
                self.state = type("State", (), {
                    "stats": {"level": 15}, "inventory": [item],
                    "items": {item.tag: item}, "equipment": {},
                })()
                self.locked = []

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        changed = asyncio.run(FarmTask().maintain_inventory(client))
        self.assertTrue(changed)
        self.assertEqual(client.locked, [18])

    def test_inventory_maintenance_reads_lore_and_safe_spellbooks_once(self):
        class FakeClient:
            def __init__(self):
                lore = Item(21, item_type=c.TYPE_BOOK, quality=100,
                            name="paper bestiary")
                spellbook = Item(22, item_type=c.TYPE_SPELLBOOK,
                                 quality=100,
                                 name="spellbook of greater healing")
                self.state = type("State", (), {
                    "stats": {"level": 18},
                    "inventory": [lore, spellbook],
                    "items": {21: lore, 22: spellbook},
                    "equipment": {},
                })()
                self.applied = []
                self.locked = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                self.locked.append(tag)
                self.state.items[tag].flags |= c.ITEM_LOCKED

        client = FakeClient()
        task = FarmTask()
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.applied, [21, 22])
        self.assertEqual(client.locked, [21])
        self.assertEqual(task._lore_book_attempts, {21})
        self.assertEqual(task._spellbook_attempts, {22})

    def test_inventory_maintenance_never_reads_unsafe_spellbook(self):
        class FakeClient:
            def __init__(self):
                book = Item(23, flags=c.ITEM_CURSED,
                            item_type=c.TYPE_SPELLBOOK, quality=100,
                            name="cursed spellbook")
                self.state = type("State", (), {
                    "stats": {"level": 18}, "inventory": [book],
                    "items": {23: book}, "equipment": {},
                })()
                self.applied = []
                self.locked = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        asyncio.run(FarmTask().maintain_inventory(client))
        self.assertEqual(client.applied, [])

    def test_inventory_maintenance_skips_server_confirmed_read_lore(self):
        class FakeClient:
            def __init__(self):
                lore = Item(31, flags=c.ITEM_NO_SKILL_IDENT,
                            item_type=c.TYPE_BOOK, quality=100,
                            name="paper bestiary")
                self.state = type("State", (), {
                    "stats": {"level": 18},
                    "inventory": [lore],
                    "items": {31: lore},
                    "equipment": {},
                })()
                self.applied = []
                self.locked = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                self.locked.append(tag)
                self.state.items[tag].flags |= c.ITEM_LOCKED

        client = FakeClient()
        task = FarmTask()
        # Valuable books may still be locked for preservation, but the
        # authoritative server flag prevents reapplying already-read lore.
        while asyncio.run(task.maintain_inventory(client)):
            pass
        self.assertEqual(client.applied, [])
        self.assertEqual(client.locked, [31])

    def test_circuit_reconciles_inventory_at_fight_safe_boundary(self):
        class FakeClient:
            def __init__(self):
                item = Item(
                    19, flags=c.ITEM_LOCKED, item_type=c.TYPE_WEAPON,
                    quality=89, condition=89,
                    name="hardened steel ancus")
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats["level"] = 15
                self.state.place_item(item, 7)
                self.locked = []

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        farm = FarmTask(zone="/farm", target="rat")
        self.assertTrue(asyncio.run(
            circuit._maintain_safe_inventory(client, farm)))
        self.assertEqual(client.locked, [19])
        client.state.combat = True
        client.state.target_id = 55
        client.state.stats["target_hp"] = 50
        self.assertFalse(asyncio.run(
            circuit._maintain_safe_inventory(client, farm)))
        self.assertEqual(client.locked, [19])

    def test_task_engine_normalizes_stack_locks_without_a_task(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "phase": "playing",
                    "inventory": [Item(
                        23, flags=c.ITEM_LOCKED, item_type=c.TYPE_ARROW,
                        name="arrow", quantity=20)],
                })()
                self.locked = []

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        engine = TaskEngine(client)
        changed = asyncio.run(engine._normalize_inventory_locks())
        self.assertTrue(changed)
        self.assertEqual(client.locked, [23])
        # Packet state may lag a tick; never toggle the same lock twice.
        self.assertFalse(asyncio.run(engine._normalize_inventory_locks()))
        self.assertEqual(client.locked, [23])

    def test_task_engine_heals_critical_character_while_idle(self):
        class FakeClient:
            def __init__(self):
                heal = Item(
                    24, item_type=c.TYPE_SPELL, name="minor healing")
                heal.extra["cost"] = 3
                self.state = type("State", (), {
                    "phase": "playing",
                    "stats": {"hp": 7, "maxhp": 100, "sp": 20,
                              "food": 500},
                    "inventory": [heal],
                })()
                self.action_context = ""
                self.clears = 0
                self.fired = []

            async def clear_actions(self):
                self.clears += 1

            async def fire(self, direction, tag):
                self.fired.append((direction, tag, self.action_context))

        client = FakeClient()
        engine = TaskEngine(client)
        self.assertTrue(asyncio.run(engine._protect_idle()))
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.fired, [(0, 24, "idle-safety")])
        self.assertEqual(client.action_context, "")

    def test_idle_safety_waits_for_initial_stats_packet(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "phase": "playing",
                    "stats": {"hp": 0, "maxhp": 0, "sp": 0, "food": 0},
                    "inventory": [],
                })()
                self.action_context = ""
                self.clears = 0

            async def clear_actions(self):
                self.clears += 1

        client = FakeClient()
        engine = TaskEngine(client)
        self.assertFalse(asyncio.run(engine._protect_idle()))
        self.assertEqual(client.clears, 0)

    def test_inventory_maintenance_upgrades_boot_slot(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOOTS,
                               quality=10, condition=50, name="worn sandals")
                upgrade = Item(2, item_type=c.TYPE_BOOTS, quality=80,
                               condition=100, name="sturdy boots")
                self.state = type("State", (), {
                    "stats": {"level": 10}, "inventory": [current, upgrade],
                    "items": {1: current, 2: upgrade},
                    "equipment": {c.EQUIP_BOOTS: 1},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("ordinary boots must not be locked")

        client = FakeClient()
        self.assertTrue(asyncio.run(FarmTask().maintain_inventory(client)))
        self.assertEqual(client.applied, [2])

    def test_inventory_maintenance_uses_archetype_combat_stats(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED, face=10,
                               item_type=c.TYPE_ARMOUR, quality=80,
                               condition=80, name="soft leather padded armour")
                upgrade = Item(2, face=11, item_type=c.TYPE_ARMOUR,
                               quality=81, condition=81,
                               name="hardened leather armour")
                self.faces = {10: "leather_padded.101",
                              11: "leather_ar.101"}
                self.state = type("State", (), {
                    "stats": {"level": 10},
                    "inventory": [current, upgrade],
                    "items": {1: current, 2: upgrade},
                    "equipment": {c.EQUIP_ARMOUR: 1},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("ordinary armour must not be locked")

        client = FakeClient()
        self.assertTrue(asyncio.run(FarmTask().maintain_inventory(client)))
        self.assertEqual(client.applied, [2])

    def test_inventory_maintenance_values_light_radius_and_fuel(self):
        class FakeClient:
            def __init__(self):
                torch = Item(1, flags=c.ITEM_APPLIED, face=20,
                             item_type=c.TYPE_LIGHT_APPLY, quality=100,
                             condition=100, name="torch")
                lamp = Item(2, face=21, item_type=c.TYPE_LIGHT_APPLY,
                            quality=80, condition=80, name="tin lamp")
                self.faces = {20: "torch_unlit.101",
                              21: "lamp_unlit.101"}
                self.state = type("State", (), {
                    "stats": {"level": 10},
                    "inventory": [torch, lamp],
                    "items": {1: torch, 2: lamp},
                    "equipment": {c.EQUIP_LIGHT: 1},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("ordinary lights must not be locked")

        client = FakeClient()
        self.assertTrue(asyncio.run(FarmTask().maintain_inventory(client)))
        self.assertEqual(client.applied, [2])
        prototypes = equipment_face_catalog()
        self.assertGreater(prototypes["lamp_unlit.101"][0].base_score,
                           prototypes["torch_unlit.101"][0].base_score)

    def test_weapon_prototypes_value_damage_throughput(self):
        prototypes = equipment_face_catalog()
        shortsword = prototypes["shortsword.101"][0]
        longsword = prototypes["longsword.101"][0]
        katana = prototypes["katana.101"][0]
        self.assertGreater(katana.base_score, longsword.base_score)
        self.assertGreater(longsword.base_score, shortsword.base_score)

    def test_shop_upgrade_scores_exact_examined_dynamic_roll(self):
        skill = Item(99, item_type=c.TYPE_SKILL, name="slash weapons")
        current = Item(
            1, flags=c.ITEM_APPLIED, face=10,
            item_type=c.TYPE_WEAPON, quality=80, condition=80,
            required_skill_tag=skill.tag, name="steel longsword")
        stock = Item(
            2, flags=c.ITEM_UNPAID, face=10,
            item_type=c.TYPE_WEAPON, quality=255,
            required_skill_tag=skill.tag, name="steel longsword of flame")
        client = type("Client", (), {})()
        client.faces = {10: "longsword.101"}
        client.state = GameState(phase="playing", player_tag=7)
        for item in (skill, current):
            client.state.place_item(item, 7)
        client.state.place_item(stock, 0)
        client.state.stats["level"] = 18
        client.state.equipment[c.EQUIP_WEAPON] = current.tag

        task = BuyShopUpgradeTask()
        self.assertTrue(task._apply_examine_metadata(
            client, stock,
            "That is steel longsword of flame (wc+2) (dam+8) "
            "(2.25 sec) (Str+1) (Attacks: fire +15%) "
            "(Protections: cold +12%). Qua: 90 Con: 90."))
        detail = stock.extra["examined_gear_detail"]
        self.assertEqual(detail["dam"], 8)
        self.assertEqual(detail["attacks"], 15)
        self.assertEqual(detail["protections"], 12)
        self.assertGreater(task._upgrade_score(client, stock), 0)

    def test_shop_upgrade_does_not_score_base_physical_attack_as_bonus(self):
        current = Item(
            1, flags=c.ITEM_APPLIED | c.ITEM_MAGICAL, face=10,
            item_type=c.TYPE_WEAPON, quality=88, condition=88,
            name="shear steel falchion +2")
        stock = Item(
            2, flags=c.ITEM_UNPAID | c.ITEM_MAGICAL, face=11,
            item_type=c.TYPE_WEAPON, quality=89, condition=89,
            name="diamant steel baselard +2")
        client = type("Client", (), {})()
        client.faces = {10: "falchion.101", 11: "baselard.101"}
        client.state = GameState(phase="playing", player_tag=7)
        client.state.place_item(current, 7)
        client.state.place_item(stock, 0)
        client.state.stats["level"] = 20
        client.state.equipment[c.EQUIP_WEAPON] = current.tag

        task = BuyShopUpgradeTask()
        self.assertTrue(task._apply_examine_metadata(
            client, stock,
            "That is diamant steel baselard +2 (wc+5) (dam+4) "
            "(2.25 sec) (block+1) (absorb+2%) "
            "(Attacks: slash +100%). Qua: 89 Con: 89."))
        self.assertEqual(
            stock.extra["examined_gear_detail"]["attacks"], 0)
        self.assertLess(task._upgrade_score(client, stock), 0)

    def test_shop_upgrade_counts_unreadied_owned_bow_as_baseline(self):
        owned = Item(
            1, face=20, item_type=c.TYPE_BOW, quality=81, condition=81,
            name="spruce short bow")
        duplicate = Item(
            2, flags=c.ITEM_UNPAID, face=20, item_type=c.TYPE_BOW,
            quality=81, condition=81, name="spruce short bow")
        client = type("Client", (), {})()
        client.faces = {20: "bow_short.101"}
        client.state = GameState(phase="playing", player_tag=7)
        client.state.place_item(owned, 7)
        client.state.place_item(duplicate, 0)
        client.state.stats["level"] = 18

        self.assertLess(
            BuyShopUpgradeTask()._upgrade_score(client, duplicate), 0)

    def test_shop_upgrade_excludes_launchers_for_spell_build(self):
        stock = Item(
            2, flags=c.ITEM_UNPAID, face=20, item_type=c.TYPE_BOW,
            quality=90, condition=90, name="enchanted crossbow")
        client = type("Client", (), {})()
        client.faces = {20: "crossbow.101"}
        client.state = GameState(phase="playing", player_tag=7)
        client.state.place_item(stock, 0)
        client.state.stats["level"] = 18

        self.assertLess(
            BuyShopUpgradeTask(
                allow_launchers=False)._upgrade_score(client, stock), 0)

    def test_inventory_maintenance_never_applies_unidentified_upgrade(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED,
                               item_type=c.TYPE_BOOTS, quality=20,
                               condition=80, name="worn sandals")
                unknown = Item(2, item_type=c.TYPE_BOOTS, quality=255,
                               condition=255, name="mysterious boots")
                self.state = type("State", (), {
                    "stats": {"level": 10},
                    "inventory": [current, unknown],
                    "items": {1: current, 2: unknown},
                    "equipment": {c.EQUIP_BOOTS: 1},
                })()
                self.applied = []
                self.locked = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                self.locked.append(tag)

        client = FakeClient()
        # Protecting the unknown object is allowed; applying it never is.
        asyncio.run(FarmTask().maintain_inventory(client))
        self.assertEqual(client.applied, [])

    def test_inventory_maintenance_removes_previously_equipped_unknown_gear(self):
        class FakeClient:
            def __init__(self):
                shield = Item(4, flags=c.ITEM_APPLIED,
                              item_type=c.TYPE_SHIELD, quality=255,
                              name="mysterious shield")
                self.state = type("State", (), {
                    "stats": {"level": 10}, "inventory": [shield],
                    "items": {4: shield},
                    "equipment": {c.EQUIP_SHIELD: 4},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("unapply must happen before locking")

        client = FakeClient()
        task = FarmTask()
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.applied, [4])
        self.assertIn(4, task._unidentified_unapply_attempts)

    def test_inventory_maintenance_removes_applied_unknown_ammunition(self):
        class FakeClient:
            def __init__(self):
                arrows = Item(5, flags=c.ITEM_APPLIED,
                              item_type=c.TYPE_ARROW, quality=255,
                              name="arrows", quantity=15)
                self.state = type("State", (), {
                    "stats": {"level": 10}, "inventory": [arrows],
                    "items": {5: arrows},
                    "equipment": {c.EQUIP_AMMO: 5},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("unapply must happen before lock handling")

        client = FakeClient()
        task = FarmTask()
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.applied, [5])
        self.assertIn(5, task._unidentified_unapply_attempts)

    def test_inventory_maintenance_keeps_trained_weapon_school(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED, item_type=c.TYPE_WEAPON,
                               quality=10, condition=50, name="shortsword",
                               required_skill_tag=100)
                other = Item(2, item_type=c.TYPE_WEAPON, quality=80,
                             condition=100, name="great axe",
                             required_skill_tag=200)
                self.state = type("State", (), {
                    "stats": {"level": 10}, "inventory": [current, other],
                    "items": {1: current, 2: other},
                    "equipment": {c.EQUIP_WEAPON: 1},
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                raise AssertionError("ordinary weapons must not be locked")

        client = FakeClient()
        self.assertFalse(asyncio.run(FarmTask().maintain_inventory(client)))
        self.assertEqual(client.applied, [])

    def test_magic_inventory_maintenance_never_readies_incidental_bow(self):
        class FakeClient:
            def __init__(self):
                shield = Item(
                    1, flags=c.ITEM_APPLIED, item_type=c.TYPE_SHIELD,
                    quality=80, condition=80,
                    name="birch round shield of acid protection")
                bow = Item(
                    2, item_type=c.TYPE_BOW, quality=100, condition=100,
                    name="spruce short bow")
                arrows = Item(
                    3, item_type=c.TYPE_ARROW, quality=100, condition=100,
                    name="pine arrows", quantity=20)
                self.state = type("State", (), {
                    "stats": {"level": 18},
                    "inventory": [shield, bow, arrows],
                    "items": {1: shield, 2: bow, 3: arrows},
                    "equipment": {c.EQUIP_SHIELD: shield.tag},
                })()
                self.applied = []
                self.locked = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def lock_item(self, tag):
                self.locked.append(tag)
                self.state.items[tag].flags |= c.ITEM_LOCKED

        client = FakeClient()
        task = FarmTask(combat_spell="magic bullet")

        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertFalse(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.locked, [2])
        self.assertEqual(client.applied, [])

    def test_magic_inventory_reconciles_ranged_gear_before_lore(self):
        class FakeClient:
            def __init__(self):
                arrows = Item(
                    1, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
                    quality=100, condition=100,
                    name="pine arrows", quantity=20)
                lore = Item(
                    2, item_type=c.TYPE_BOOK, quality=100,
                    name="paper bestiary")
                self.state = type("State", (), {
                    "stats": {"level": 18},
                    "inventory": [lore, arrows],
                    "items": {1: arrows, 2: lore},
                    "equipment": {c.EQUIP_AMMO: arrows.tag},
                })()
                self.applied = []
                self.decisions = []

            async def apply(self, tag):
                self.applied.append(tag)
                self.state.items[tag].flags ^= c.ITEM_APPLIED

            def record_action(self, action, detail):
                self.decisions.append((action, detail))

        client = FakeClient()
        task = FarmTask(combat_spell="magic bullet")

        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.applied, [1])
        self.assertEqual(client.decisions[0][0], "ammunition-unready")
        self.assertNotIn(2, task._lore_book_attempts)
        self.assertTrue(asyncio.run(task.maintain_inventory(client)))
        self.assertEqual(client.applied, [1, 2])

    def test_farm_circuit_rotates_and_honors_combat_grace(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing")
                self.state.map.path = "/a"
                self.state.place_item(
                    Item(990, name="staple food", quantity=10), self.state.player_tag)
                self.clears = 0
                self.combat = []

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

        async def scenario():
            now = [1.0]
            graph = WorldGraph()
            graph.nodes["/a"] = MapNode("/a", width=10, height=10)
            graph.nodes["/b"] = MapNode("/b", width=10, height=10)
            client = FakeClient()
            task = FarmCircuitTask(
                graph, [("/a", "treant"), ("/b", "lost soul")],
                dwell_seconds=5, switch_grace=30, clock=lambda: now[0])
            await task.start(client)
            task.child.status = TaskStatus.RUNNING
            async def idle_tick(_client):
                return None
            task.child.tick = idle_tick
            await task.tick(client)
            self.assertEqual(task.child.navigation.status, TaskStatus.COMPLETE)
            client.state.target_id = 99
            client.state.map.tile(8, 8).objects[3] = MapObject(
                3, 1, 0, 0, name="treant", target_id=99)
            now[0] = 7.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            now[0] = 37.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            # Even after the old grace deadline, an off-screen but positively
            # alive selected target remains a hard transition blocker.
            client.state.map.tiles.clear()
            client.state.stats["target_hp"] = 50
            client.state.combat = True
            now[0] = 38.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            client.state.target_id = 0
            client.state.stats["target_hp"] = 0
            now[0] = 39.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 1)
            self.assertEqual(task.child.navigation.destination, "/b")
            self.assertEqual(client.clears, 1)
            self.assertEqual(client.combat, [False])
            navigated = []
            task.child.navigation.status = TaskStatus.COMPLETE
            async def navigation_tick(_client):
                navigated.append((task.child.navigation.destination,
                                  task.child.navigation.status))
            task.child.navigation.tick = navigation_tick
            client.state.stats.update({"hp": 10, "maxhp": 10, "food": 1000})
            await task.tick(client)
            self.assertEqual(navigated, [("/b", TaskStatus.RUNNING)])

        asyncio.run(scenario())

    def test_shop_upgrade_requires_identification_and_exact_budget(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED,
                               item_type=c.TYPE_BOOTS, quality=20,
                               condition=80, name="old boots")
                stock = Item(2, flags=c.ITEM_UNPAID, item_type=c.TYPE_BOOTS,
                             quality=90, condition=100, name="fine boots")
                coins = Item(3, item_type=c.TYPE_MONEY,
                             name="silver coin", quantity=20)
                self.state = GameState(phase="playing", player_tag=7)
                for item in (current, coins):
                    self.state.place_item(item, 7)
                self.state.place_item(stock, 0)
                self.state.stats["level"] = 10
                self.state.equipment[c.EQUIP_BOOTS] = current.tag
                self.examined = []
                self.moved = []
                self.decisions = []

            def record_action(self, action, detail):
                self.decisions.append((action, detail))

            async def examine(self, tag):
                self.examined.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moved.append((destination, tag, quantity))

        client = FakeClient()
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        self.assertEqual(client.examined, [2])
        client.state.messages.append(
            (0, 0, 0, "It would cost you 2 silver coins."))
        asyncio.run(task.tick(client))
        self.assertEqual(client.moved, [(7, 2, 1)])
        client.state.items[2].location = 7
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(BuyShopUpgradeTask.parse_cost(
            "It would cost you 1 gold coin, 2 silver coins and 3 copper coins."),
            10_203)

        client = FakeClient()
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        client.state.messages.append(
            (0, 0, 0, "It would cost you 6 silver coins."))
        asyncio.run(task.tick(client))
        self.assertIn((
            "shop-upgrade-reject", "fine boots cost=600 budget=500"),
            client.decisions)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.decisions[-1], (
            "shop-upgrade-scan",
            "stock=1 meaningful=1 budget-rejected=1 result=no-upgrade"))

        client = FakeClient()
        client.state.items[2].quality = 255
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.examined, [])
        self.assertEqual(client.decisions, [(
            "shop-upgrade-scan",
            "stock=1 meaningful=0 budget-rejected=0 result=no-upgrade")])

    def test_dynamic_merchant_buys_exact_recall_device_from_dialogue(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 5_000
                self.state.bank_balance_known = True
                self.state.place_item(Item(
                    9, item_type=c.TYPE_SKILL, name="wizardry spells",
                    extra={"level": 10}), 7)
                self.talks = []
                self.decisions = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

            def record_action(self, action, detail):
                self.decisions.append((action, detail))

        client = FakeClient()
        task = BuyDialogueStockTask("sage", r"word of recall")
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks, [("hello", "sage")])

        client.state.interface = InterfaceState(
            title="sage", links=[
                "[a=:buy spellbook of word of recall]Book[/a]",
                "[a=:buy wand of word of recall (lvl 12)]Wand[/a]",
            ])
        asyncio.run(task.tick(client))
        self.assertEqual(
            client.talks[-1],
            ("buy wand of word of recall (lvl 12)", "sage"))

        quote = Item(
            20, name="wand of word of recall (lvl 12)",
            extra={"message":
                   "wand of word of recall (lvl 12) for 12 silver 50 copper"})
        client.state.interface = InterfaceState(
            title="sage", objects=[quote], links=[
                "[a=:buy 1 wand of word of recall (lvl 12)]One[/a]",
            ])
        asyncio.run(task.tick(client))
        self.assertEqual(
            client.talks[-1],
            ("buy 1 wand of word of recall (lvl 12)", "sage"))
        self.assertEqual(client.state.bank_balance, 3_750)

        purchased = Item(
            21, item_type=c.TYPE_WAND, quality=80, condition=80,
            name="wand of word of recall (lvl 12)")
        client.state.place_item(purchased, 7)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertTrue(task.checked)
        self.assertTrue(task.purchased)
        self.assertEqual(client.decisions[-1][0], "dialogue-stock-buy")

    def test_dynamic_merchant_records_checked_when_recall_not_stocked(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)

            async def talk(self, message, npc):
                pass

        client = FakeClient()
        task = BuyDialogueStockTask("sage", r"word of recall")
        asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(
            title="sage", links=["[a=:buy wand of firebolt]Wand[/a]"])
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertTrue(task.checked)
        self.assertFalse(task.purchased)

    def test_dynamic_merchant_buys_distinct_utility_groups_in_one_visit(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 10_000
                self.state.bank_balance_known = True
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

        client = FakeClient()
        task = BuyDialogueStockTask(
            "sage", r"identify|remove depletion",
            preferred=("spellbook of identify",
                       "scroll of remove depletion"),
            max_purchases=2,
            distinct_patterns=(r"identify", r"remove depletion"))
        asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(title="sage", links=[
            "[a=:buy spellbook of identify]Identify book[/a]",
            "[a=:buy wand of identify (lvl 1)]Identify wand[/a]",
            "[a=:buy scroll of remove depletion]Depletion scroll[/a]",
        ])
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks[-1],
                         ("buy spellbook of identify", "sage"))
        client.state.interface = InterfaceState(
            title="sage",
            objects=[Item(40, name="spellbook of identify",
                          extra={"message":
                                 "spellbook of identify for 2 silver"})],
            links=["[a=:buy 1 spellbook of identify]One[/a]"])
        asyncio.run(task.tick(client))
        client.state.place_item(Item(
            41, item_type=c.TYPE_SPELLBOOK, quality=80,
            name="spellbook of identify"), 7)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(title="sage", links=[
            "[a=:buy wand of identify (lvl 1)]Identify wand[/a]",
            "[a=:buy scroll of remove depletion]Depletion scroll[/a]",
        ])
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks[-1],
                         ("buy scroll of remove depletion", "sage"))
        client.state.interface = InterfaceState(
            title="sage",
            objects=[Item(42, name="scroll of remove depletion",
                          extra={"message":
                                 "scroll of remove depletion for 1 silver"})],
            links=["[a=:buy 1 scroll of remove depletion]One[/a]"])
        asyncio.run(task.tick(client))
        client.state.place_item(Item(
            43, item_type=c.TYPE_SCROLL, quality=80,
            name="scroll of remove depletion"), 7)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(task.purchase_count, 2)
        self.assertEqual(client.state.bank_balance, 9_700)

    def test_dialogue_bartender_buys_bulk_food_and_detects_stack_merge(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 5_000
                self.state.bank_balance_known = True
                self.food = Item(
                    30, item_type=c.TYPE_FOOD, quality=100,
                    name="free-range chicken leg", quantity=2)
                self.state.place_item(self.food, 7)
                self.talks = []
                self.quotes = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

            def record_vendor_quote(self, key, item, unit_cost):
                self.quotes.append((key, item, unit_cost))

        client = FakeClient()
        task = BuyDialogueStockTask(
            "Kestrei", r"chicken leg", quantity=25,
            preferred=("free-range chicken leg",),
            observation_key="brynknot:kestrei:chicken")
        asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(
            title="Kestrei", links=[
                "[a=:buy free-range chicken leg]Chicken leg[/a]"])
        asyncio.run(task.tick(client))
        quote = Item(
            31, name="free-range chicken leg",
            extra={"message": "free-range chicken leg for 20 copper"})
        client.state.interface = InterfaceState(
            title="Kestrei", objects=[quote], links=[
                "[a=:buy 25 free-range chicken leg]Twenty-five[/a]"])
        asyncio.run(task.tick(client))
        self.assertEqual(
            client.talks[-1],
            ("buy 25 free-range chicken leg", "Kestrei"))
        self.assertEqual(client.state.bank_balance, 4_500)
        self.assertEqual(client.quotes, [(
            "brynknot:kestrei:chicken", "free-range chicken leg", 20)])
        client.food.quantity = 27
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertTrue(task.purchased)

    def test_shop_upgrade_accepts_retagged_inventory_pickup(self):
        class FakeClient:
            def __init__(self):
                current = Item(1, flags=c.ITEM_APPLIED, face=54,
                               item_type=c.TYPE_BOOTS, quality=20,
                               condition=80, name="old boots")
                stock = Item(2, flags=c.ITEM_UNPAID, face=55,
                             item_type=c.TYPE_BOOTS, quality=90,
                             condition=100, name="fine boots")
                existing = Item(4, face=55, item_type=c.TYPE_BOOTS,
                                quality=10, condition=10, name="fine boots")
                coins = Item(3, item_type=c.TYPE_MONEY,
                             name="silver coin", quantity=20)
                self.state = GameState(phase="playing", player_tag=7)
                for item in (current, existing, coins):
                    self.state.place_item(item, 7)
                self.state.place_item(stock, 0)
                self.state.stats["level"] = 10
                self.state.equipment[c.EQUIP_BOOTS] = current.tag
                self.examined = []
                self.moved = []

            async def examine(self, tag):
                self.examined.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moved.append((destination, tag, quantity))

        async def run():
            client = FakeClient()
            task = BuyShopUpgradeTask()
            await task.tick(client)
            client.state.messages.append(
                (0, 0, 0, "It would cost you 2 silver coins."))
            await task.tick(client)
            self.assertEqual(client.moved, [(7, 2, 1)])
            # A same-name item which existed before the purchase cannot
            # acknowledge it. Shop pickup clones the stock under a new tag.
            await task.tick(client)
            self.assertEqual(task.status, TaskStatus.RUNNING)
            client.state.remove_item(2)
            client.state.place_item(Item(20, face=55,
                                         item_type=c.TYPE_BOOTS, quality=90,
                                         condition=100, name="fine boots"), 7)
            await task.tick(client)
            self.assertEqual(task.status, TaskStatus.COMPLETE)

        asyncio.run(run())

    def test_shop_upgrade_infers_real_ground_stock_then_requires_examine(self):
        class FakeClient:
            def __init__(self):
                skill = Item(99, item_type=c.TYPE_SKILL,
                             name="slash weapons")
                current = Item(
                    1, flags=c.ITEM_APPLIED, face=1,
                    item_type=c.TYPE_WEAPON, quality=80, condition=80,
                    required_skill_tag=skill.tag, name="iron shortsword")
                stock = Item(
                    2, flags=c.ITEM_UNPAID, face=2, item_type=0,
                    quality=255, name="steel longsword")
                coins = Item(3, item_type=c.TYPE_MONEY,
                             name="silver coin", quantity=20)
                self.faces = {1: "shortsword.101", 2: "longsword.101"}
                self.state = GameState(phase="playing", player_tag=7)
                for item in (skill, current, coins):
                    self.state.place_item(item, 7)
                self.state.place_item(stock, 0)
                self.state.stats["level"] = 9
                self.state.equipment[c.EQUIP_WEAPON] = current.tag
                self.examined = []
                self.moved = []
                self.decisions = []

            def record_action(self, action, detail):
                self.decisions.append((action, detail))

            async def examine(self, tag):
                self.examined.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moved.append((destination, tag, quantity))

        client = FakeClient()
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        self.assertEqual(client.examined, [2])
        self.assertEqual(client.state.items[2].item_type, c.TYPE_WEAPON)
        client.state.messages.extend((
            (0, 0, 0, "It needs a level of 7 in slash weapons to use."),
            (0, 0, 0, "Qua: 80 Con: 80."),
            (0, 0, 0, "It would cost you 2 silver coins."),
        ))
        asyncio.run(task.tick(client))
        self.assertEqual(client.moved, [(7, 2, 1)])
        self.assertEqual(client.state.items[2].quality, 80)
        self.assertIn(("shop-upgrade-buy",
                       "steel longsword cost=200 budget=500"),
                      client.decisions)

        client = FakeClient()
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        client.state.messages.append(
            (0, 0, 0, "It would cost you 2 silver coins."))
        asyncio.run(task.tick(client))
        self.assertEqual(client.moved, [])
        self.assertIn(("shop-upgrade-skip",
                       "steel longsword reason=unidentified"),
                      client.decisions)

        client = FakeClient()
        stock = client.state.items[2]
        stock.face = 3
        stock.name = "steel axe"
        client.faces[3] = "axe.101"
        task = BuyShopUpgradeTask()
        asyncio.run(task.tick(client))
        self.assertEqual(client.examined, [])
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_upgrade_shop_sweep_expands_only_when_roads_are_safe(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        client.state.stats["level"] = 8
        early = circuit._new_upgrade_shopping(client)
        self.assertEqual(
            {path for path, _ in early.waypoints},
            {"/shattered_islands/world_0_69"})
        client.state.stats["level"] = 18
        regional = circuit._new_upgrade_shopping(client)
        self.assertIn(
            "/shattered_islands/world_7_46",
            {path for path, _ in regional.waypoints})
        self.assertIn(
            "/shattered_islands/world_6_46",
            {path for path, _ in regional.waypoints})
        self.assertIn(
            "/shattered_islands/world_5_46",
            {path for path, _ in regional.waypoints})
        self.assertIn(
            "/shattered_islands/world_5_58",
            {path for path, _ in regional.waypoints})
        self.assertTrue(regional.allow_hostile_transit)
        regional.index = next(
            index for index, (path, _) in enumerate(regional.waypoints)
            if path == "/shattered_islands/world_7_46")
        regional_child = regional._new_child()
        self.assertTrue(
            regional_child.navigation.allow_ranged_hazard_fallback)
        # Brynknot's stock floor is a separate shop-mat component. The
        # regional sweep must escape it, use the acquired ship key, and
        # explicitly mark the unavoidable crocodile/frog road as a defended
        # transit instead of silently skipping every Asteria waypoint.
        client.state.map = MapState(
            path="/shattered_islands/world_0_69",
            world_x=23, world_y=8)
        client.state.place_item(Item(
            99, name="Morg'eean's Ship Key"), client.state.player_tag)
        route = regional_child.navigation._plan(client)
        self.assertIn(
            "/shattered_islands/world_6_50",
            {edge.destination for edge in route})
        self.assertEqual(
            regional_child.navigation._route_threat_maps,
            {"/shattered_islands/world_6_50"})
        client.state.active_upgrade_shop_sweep_policy = (
            circuit.UPGRADE_SWEEP_POLICY)
        client.state.active_upgrade_shop_sweep_cursor = 76
        resumed = circuit._new_upgrade_shopping(client)
        self.assertEqual(resumed.index, 76)
        # Resume the exact persisted waypoint of the deterministic sweep; which
        # map that index lands on follows the authored shop stock in content.
        self.assertEqual(resumed.waypoints, regional.waypoints)
        self.assertEqual(resumed.waypoints[resumed.index],
                         regional.waypoints[76])
        # Centennial's nominal map currently has no authored unpaid stock
        # tiles, so it is correctly absent rather than creating empty travel.
        self.assertFalse(graph.shop_stocks[
            "/shattered_islands/world_0_52"])

    def test_authored_shop_stock_sweep_has_early_town_waypoints(self):
        graph = built_graph()
        self.assertIn((16, 6), graph.shop_stocks[
            "/shattered_islands/world_0_69"])
        sweep = ShopUpgradeSweepTask(graph, (
            "/shattered_islands/world_0_69",
            "/shattered_islands/world_5_46"))
        self.assertTrue(sweep.waypoints)
        self.assertTrue(all(graph.nodes[path].walkable(*point)
                            for path, point in sweep.waypoints))

    def test_shop_sweep_routes_unpaid_inventory_across_checkout_mat(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 20
        client.state.map = MapState(
            path="/shattered_islands/world_0_69",
            world_x=23, world_y=8)
        client.state.place_item(Item(
            90, flags=c.ITEM_UNPAID, item_type=c.TYPE_BRACERS,
            quality=88, condition=88, name="shear steel bracers"), 7)
        sweep = ShopUpgradeSweepTask(
            graph, ("/shattered_islands/world_0_69",))

        settlement = sweep._new_settlement(client)
        self.assertIsNotNone(settlement)
        self.assertEqual(
            settlement.destination,
            "/shattered_islands/world_0_69")
        self.assertNotIn(
            settlement.destination_xy,
            graph._component(
                "/shattered_islands/world_0_69", (23, 8)))
        route = settlement._plan(client)
        self.assertEqual(route[0].kind, "exit")
        self.assertEqual((route[0].x, route[0].y), (15, 9))

    def test_spell_build_shop_sweep_disallows_launchers(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 18
        circuit = FarmCircuitTask(
            graph, list(EARLY_LEGS),
            combat_spell="magic bullet")

        sweep = circuit._new_upgrade_shopping(client)
        self.assertFalse(sweep.allow_launchers)
        self.assertFalse(sweep._new_child().task.allow_launchers)

        adaptive = FarmCircuitTask(
            graph, list(EARLY_LEGS), analyzer_driven=True)
        self.assertTrue(adaptive._auto_combat_build)
        self.assertEqual(adaptive.combat_spell, "")
        between_milestones = adaptive._new_upgrade_shopping(client)
        self.assertFalse(between_milestones.allow_launchers)
        self.assertFalse(
            between_milestones._new_child().task.allow_launchers)
        adaptive_farm = adaptive._new_child().task
        self.assertFalse(adaptive_farm.allow_launchers)

    def test_ground_shop_purchase_waits_for_inventory_quantity(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(
                    Item(90, name="staple food", quantity=5), 0)
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = BuyGroundItemsTask(r"^staple food$", 5)
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(7, 90, 5)])
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.remove_item(90)
        client.state.place_item(
            Item(91, name="staple food", quantity=5), 7)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_ground_shop_purchase_accumulates_multiple_stock_stacks(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(
                    Item(90, name="staple food", quantity=5), 0)
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = BuyGroundItemsTask(r"^staple food$", 15)
        asyncio.run(task.tick(client))
        for bought, stock_tag in ((5, 91), (10, 92)):
            ground = next(item for item in client.state.ground)
            client.state.remove_item(ground.tag)
            carried = next((item for item in client.state.inventory
                            if item.name == "staple food"), None)
            if carried is None:
                client.state.place_item(
                    Item(100, name="staple food", quantity=bought), 7)
            else:
                carried.quantity = bought
            client.state.place_item(
                Item(stock_tag, name="staple food", quantity=5), 0)
            asyncio.run(task.tick(client))
        self.assertEqual(client.moves,
                         [(7, 90, 5), (7, 91, 5), (7, 92, 5)])
        client.state.inventory[0].quantity = 15
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_cost_aware_ground_purchase_checks_quote_and_returns_unpaid(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(Item(
                    90, flags=c.ITEM_UNPAID, item_type=c.TYPE_POTION,
                    quality=100, name="potion of cure illness",
                    quantity=1), 0)
                self.state.place_item(Item(
                    91, item_type=c.TYPE_MONEY, quality=100,
                    name="silver coin", quantity=13), 7)
                self.examined = []
                self.moves = []

            async def examine(self, tag):
                self.examined.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = BuyGroundItemsTask(
            r"cure illness", 1, cash_reserve=500)
        asyncio.run(task.tick(client))
        self.assertEqual(client.examined, [90])
        self.assertFalse(client.moves)
        client.state.messages.append((
            0, 0, 0, "It would cost you 22 silver coins and 20 copper coins."))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("costs 2220", task.error)
        self.assertFalse(client.moves)

        client = FakeClient()
        task = BuyGroundItemsTask(
            r"cure illness", 1, cash_reserve=500)
        asyncio.run(task.tick(client))
        item = client.state.items[90]
        client.state.remove_item(90)
        item.flags |= c.ITEM_UNPAID
        client.state.place_item(item, 7)
        client.state.messages.append((
            0, 0, 0, "You lack 8 silver coins to buy potion of cure illness."))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(client.moves, [(0, 90, 1)])

    def test_cost_aware_ground_purchase_retries_missing_quote(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(Item(
                    90, flags=c.ITEM_UNPAID, name="staple food",
                    quantity=5), 0)
                self.examined = []

            async def examine(self, tag):
                self.examined.append(tag)

        client = FakeClient()
        task = BuyGroundItemsTask(
            r"^staple food$", 5, cash_reserve=1_000)
        asyncio.run(task.tick(client))
        self.assertEqual(client.examined, [90])
        for expected in (2, 3):
            task._examined_at = time.monotonic() - 3
            asyncio.run(task.tick(client))
            self.assertEqual(len(client.examined), expected)
            self.assertEqual(task.status, TaskStatus.RUNNING)
        task._examined_at = time.monotonic() - 3
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("did not report a price", task.error)

    def test_farm_circuit_switches_early_when_named_spawn_leg_is_empty(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing")
                self.state.map.path = "/a"
                self.state.place_item(
                    Item(990, name="staple food", quantity=10), self.state.player_tag)
                self.clears = 0
                self.combat = []

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

        async def scenario():
            now = [1.0]
            graph = WorldGraph()
            graph.nodes["/a"] = MapNode("/a", width=20, height=20)
            graph.nodes["/b"] = MapNode("/b", width=20, height=20)
            graph.named_spawns["/a"] = [NamedSpawn(
                "/a", 5, 5, "Named Mob", ("ordinary mob",))]
            task = FarmCircuitTask(
                graph, [("/a", "mob"), ("/b", "other")],
                dwell_seconds=90, clock=lambda: now[0])
            client = FakeClient()
            await task.start(client)
            task.child.navigation.complete()
            task.child.status = TaskStatus.RUNNING

            async def idle_tick(_client):
                return None

            task.child.tick = idle_tick
            await task.tick(client)
            self.assertEqual(task.child.task.priority_spawns,
                             [(5, 5, "Named Mob")])
            self.assertEqual(task.child.task.patrol[0], (6, 5))
            now[0] = 17.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            # A boss/placeholder arriving during the empty confirmation
            # window cancels departure.
            client.state.map.tile(8, 8).objects[3] = MapObject(
                3, 1, 0, 0, name="ordinary mob", target_id=200)
            now[0] = 21.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            client.state.map.tiles.clear()
            now[0] = 22.0
            await task.tick(client)
            now[0] = 25.1
            await task.tick(client)
            self.assertEqual(task.leg_index, 1)
            self.assertEqual(task.child.navigation.destination, "/b")
            self.assertEqual(client.combat[-1], False)

        asyncio.run(scenario())

    def test_farm_circuit_switches_after_empty_ordinary_patrol_sweep(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing")
                self.state.map.path = "/a"
                self.state.place_item(
                    Item(990, name="staple food", quantity=10), self.state.player_tag)
                self.clears = 0
                self.combat = []

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

        async def scenario():
            now = [1.0]
            graph = WorldGraph()
            graph.nodes["/a"] = MapNode("/a", width=20, height=20)
            graph.nodes["/b"] = MapNode("/b", width=20, height=20)
            graph.named_spawns["/a"] = [NamedSpawn(
                "/a", 5, 5, "ordinary rat", ())]
            task = FarmCircuitTask(
                graph, [("/a", "rat"), ("/b", "other")],
                dwell_seconds=90, clock=lambda: now[0])
            client = FakeClient()
            await task.start(client)
            task.child.navigation.complete()
            task.child.status = TaskStatus.RUNNING

            async def idle_tick(_client):
                return None

            task.child.tick = idle_tick
            await task.tick(client)
            farm = task.child.task
            self.assertFalse(task._has_proper_named_priority(farm))
            farm._patrol_index = len(farm.patrol)
            now[0] = 17.0
            await task.tick(client)
            self.assertEqual(task.leg_index, 0)
            now[0] = 20.1
            await task.tick(client)
            self.assertEqual(task.leg_index, 1)
            self.assertEqual(task.child.navigation.destination, "/b")
            self.assertEqual(client.combat[-1], False)

        asyncio.run(scenario())

    def test_farm_circuit_requests_food_detour_at_safe_low_reserve(self):
        graph = WorldGraph()
        task = FarmCircuitTask(graph, [("/a", "treant")], clock=lambda: 10.0)
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["food"] = 150
        # Spendable money above the protected cash: a restock can buy.
        client.state.bank_balance = 5_000
        client.state.place_item(Item(92, name="staple food"), 7)
        self.assertTrue(task._needs_food_resupply(client))
        client.state.inventory_replay_complete = False
        self.assertFalse(task._inventory_ready(client))
        client.state.inventory_replay_complete = True
        # Not hungry, but one item is below the reserve she carries.
        client.state.stats["food"] = 300
        self.assertTrue(task._needs_food_resupply(client))
        client.state.stats["food"] = 150
        # Unidentified found food is no reserve: it may be cursed.
        for tag in range(93, 96):
            client.state.place_item(Item(
                tag, item_type=c.TYPE_FOOD, name=f"fruit {tag}"), 7)
        self.assertTrue(task._needs_food_resupply(client))
        for tag in range(93, 96):
            client.state.items[tag].quality = 100
        self.assertFalse(task._needs_food_resupply(client))
        detour = task._new_food_resupply(client)
        self.assertEqual(detour.navigation.destination,
                         "/shattered_islands/world_0_69")
        self.assertEqual(detour.navigation.destination_xy, (15, 7))
        self.assertIsInstance(detour.task, BuyGroundItemsTask)
        self.assertEqual(detour.task.cash_reserve, 1_000)
        self.assertEqual(detour.task.quantity,
                         FarmCircuitTask.FOOD_RESUPPLY_QUANTITY)

    def test_farm_circuit_waits_for_inventory_replay_before_maintenance(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.inventory_replay_complete = False
                self.state.stats.update({
                    "level": 19, "exp": 100, "food": 50,
                })
                self.state.map.path = "/farm"
                self.decision_history = []

        graph = WorldGraph()
        graph.nodes["/farm"] = MapNode("/farm", width=20, height=20)
        task = FarmCircuitTask(
            graph, [("/farm", "treant")], clock=lambda: 100.0)
        client = FakeClient()

        asyncio.run(task.tick(client))

        self.assertIsNone(task._resupply)
        self.assertEqual(task.legs, (("/farm", "treant"),))
        self.assertEqual(task.child.navigation.status, TaskStatus.READY)

    def test_bank_funded_purchase_persists_reserved_balance(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.bank_balance = 2_000
        client.state.bank_balance_known = True
        client.persisted = []
        client.set_bank_balance = lambda balance: (
            client.persisted.append(balance),
            setattr(client.state, "bank_balance", balance))
        BuyShopUpgradeTask.account_purchase(client, 50)
        self.assertEqual(client.state.bank_balance, 1_950)
        self.assertEqual(client.persisted, [1_950])

    def test_shop_floor_sale_waits_for_payment_feedback(self):
        class FakeClient:
            def __init__(self):
                item = Item(77, location=9, name="small axe", quantity=1)
                self.state = type("State", (), {
                    "inventory": [item], "items": {77: item},
                    "player_tag": 9, "messages": [], "message_offset": 0,
                    "message_count": GameState.message_count,
                    "messages_since": GameState.messages_since,
                })()
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = SellItemsTask("shop-floor", (77,))
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(0, 77, 1)])
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.messages.append((0, 0, 0,
                                      "You receive 4 copper coins for small axe."))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        task._last_action = 0
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_shop_floor_sale_refuses_unidentified_item(self):
        class FakeClient:
            def __init__(self):
                item = Item(79, location=10, item_type=c.TYPE_WEAPON,
                            quality=255, name="mysterious sword")
                self.state = type("State", (), {
                    "inventory": [item], "items": {79: item},
                    "player_tag": 10, "messages": [],
                })()
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = SellItemsTask("shop-floor", (79,))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("unidentified", task.error)
        self.assertEqual(client.moves, [])
        self.assertFalse(JunkPolicy(("sword",)).junk(
            client.state.items[79]))

    def test_shop_floor_sale_recovers_an_ordinary_floor_drop(self):
        class FakeClient:
            def __init__(self):
                item = Item(78, location=10, name="axe", quantity=1)
                self.state = type("State", (), {
                    "inventory": [item], "items": {78: item},
                    "player_tag": 10, "messages": [], "message_offset": 0,
                    "message_count": GameState.message_count,
                    "messages_since": GameState.messages_since,
                })()
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = SellItemsTask("shop-floor", (78,))
        asyncio.run(task.tick(client))
        client.state.items[78].location = 0
        task._last_action -= 2
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(0, 78, 1), (10, 78, 1)])
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("not sold", task.error)

    def test_bulk_unidentified_deposit_only_moves_unknown_items(self):
        class FakeClient:
            def __init__(self):
                chest = Item(90, flags=c.ITEM_CONTAINER_OPEN, name="chest")
                unknown = Item(91, location=7, item_type=c.TYPE_RING,
                               quality=255, name="ring")
                known = Item(92, location=7, item_type=c.TYPE_RING,
                             quality=80, condition=100, name="silver ring")
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(chest, 0)
                self.state.place_item(unknown, 7)
                self.state.place_item(known, 7)
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

            async def apply(self, tag):
                raise AssertionError("open chest should not be reopened")

            async def lock_item(self, tag):
                raise AssertionError("unknown item is not locked")

        client = FakeClient()
        task = DepositItemsTask("chest", unidentified_only=True)
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(90, 91, 1)])

    def test_apartment_valuable_deposit_unlocks_only_safe_rare_item(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.place_item(Item(90, flags=c.ITEM_CONTAINER_OPEN,
                                           name="chest"), 0)
                self.state.place_item(Item(
                    91, flags=c.ITEM_MAGICAL | c.ITEM_LOCKED,
                    item_type=c.TYPE_RING, quality=90, condition=100,
                    name="ring of the ghost"), 7)
                self.state.place_item(Item(
                    92, flags=c.ITEM_MAGICAL | c.ITEM_APPLIED,
                    item_type=c.TYPE_RING, quality=90, condition=100,
                    name="equipped rare ring"), 7)
                self.state.place_item(Item(
                    93, flags=c.ITEM_MAGICAL, item_type=21,
                    quality=100, condition=100, name="rare maze key"), 7)
                self.state.place_item(Item(
                    94, item_type=c.TYPE_BOOK, quality=100, condition=100,
                    name="paper file of lost artifacts"), 7)
                self.state.place_item(Item(
                    95, item_type=151, quality=100, condition=100,
                    name="compass"), 7)
                self.moves = []
                self.unlocked = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

            async def apply(self, tag):
                raise AssertionError("open chest should not be reopened")

            async def lock_item(self, tag):
                self.unlocked.append(tag)
                self.state.items[tag].flags &= ~c.ITEM_LOCKED

        client = FakeClient()
        task = DepositItemsTask(
            "chest", unidentified_only=True, valuable_only=True)
        asyncio.run(task.tick(client))
        self.assertEqual(client.unlocked, [91])
        self.assertEqual(client.moves, [])
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(90, 91, 1)])

    def test_retrieve_items_moves_only_matching_container_contents(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                chest = Item(90, flags=c.ITEM_CONTAINER_OPEN, name="chest")
                self.state.place_item(chest, 0)
                self.state.place_item(Item(91, name="compass"), chest.tag)
                self.state.place_item(Item(92, name="rare trophy"), chest.tag)
                self.moves = []

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

            async def apply(self, tag):
                raise AssertionError("open chest should not be reopened")

        client = FakeClient()
        task = RetrieveItemsTask("chest", (r"^compass$",))
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(7, 91, 1)])

    def test_retrieve_waits_for_closed_container_contents(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.chest = Item(90, name="chest")
                self.state.place_item(self.chest, 0)
                self.applied = []
                self.moves = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = RetrieveItemsTask("chest", (r"^compass$",))
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [90])
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.inventories[90] = []
        task._opened_at -= 1.0
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_retrieve_refreshes_stale_open_container(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.chest = Item(
                    90, flags=c.ITEM_CONTAINER_OPEN, name="chest")
                self.state.place_item(self.chest, 0)
                self.state.messages = []
                self.applied = []
                self.moves = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moves.append((destination, tag, quantity))

        client = FakeClient()
        task = RetrieveItemsTask("chest", (r"^compass$",))
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [90])
        client.state.messages.append((0, 0, 0, "You close chest."))
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [90, 90])
        client.state.place_item(Item(91, name="compass"), 90)
        task._opened_at -= 1.0
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(7, 91, 1)])

    def test_bank_balance_query_tracks_persistent_and_zero_balances(self):
        class FakeClient:
            def __init__(self, text):
                self.state = GameState(phase="playing")
                self.state.interface = InterfaceState(title="Tolmir", text=text)
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

        for text, expected in (("Your balance is 55 silver coins.", 5_500),
                               ("You have no money stored in your bank account.", 0)):
            client = FakeClient(text)
            task = BankBalanceTask("Tolmir")
            asyncio.run(task.tick(client))
            self.assertEqual(client.talks, [("balance", "Tolmir")])
            asyncio.run(task.tick(client))
            self.assertEqual(task.status, TaskStatus.COMPLETE)
            self.assertTrue(client.state.bank_balance_known)
            self.assertEqual(client.state.bank_balance, expected)

    def test_bank_completes_from_dialogue_interface_feedback(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "messages": [],
                    "interface": InterfaceState(
                        title="Tolmir",
                        text="You deposit 5 silver coins.\nYour new balance is 5 silver coins."),
                })()
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

        client = FakeClient()
        task = BankTask("Tolmir", "5 silver")
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks, [("deposit 5 silver", "Tolmir")])
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.state.bank_balance, 500)
        self.assertTrue(client.state.bank_balance_known)


    def test_catalog_has_all_formal_quests(self):
        catalog = load_catalog()
        self.assertEqual(len(catalog), 16)
        self.assertIn("Escaping the Deserted Island", catalog)
        self.assertIn("Lost Memories", catalog)

    def test_lost_memories_prefers_peaceful_ant_solution(self):
        priority = POLICIES["Lost Memories"].priority
        self.assertLess(priority.index("Making Friends"),
                        priority.index("To Slay A Queen"))
        self.assertLess(priority.index("Report To Angela"),
                        priority.index("To Slay A Queen"))
        self.assertLess(priority.index("Report To Arvend"),
                        priority.index("To Slay A Queen"))

    def test_safety_policy_self_casts_healing_spell(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 8, "maxhp": 32, "food": 999},
                    "inventory": [
                        Item(42, item_type=c.TYPE_SPELL,
                             name="minor healing"),
                    ],
                })()
                self.fired = []
                self.applied = []
                self.combat = []
                self.clears = 0

            async def fire(self, direction, tag=0):
                self.fired.append((direction, tag))

            async def apply(self, tag):
                self.applied.append(tag)

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

            async def clear_actions(self):
                self.clears += 1

        client = FakeClient()
        self.assertFalse(asyncio.run(SafetyPolicy().enforce(client)))
        self.assertEqual(client.fired, [(0, 42)])
        self.assertEqual(client.applied, [])

    def test_safety_warning_is_throttled_during_heal_cooldown(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 8, "maxhp": 32, "food": 999},
                    "inventory": [Item(
                        42, item_type=c.TYPE_SPELL, name="minor healing")],
                })()

            async def fire(self, direction, tag=0):
                pass

            async def apply(self, tag):
                pass

            async def clear_actions(self):
                pass

        policy = SafetyPolicy()
        client = FakeClient()
        with patch("atrinik_bot.tasks.log.warning") as warning:
            self.assertFalse(asyncio.run(policy.enforce(client)))
            self.assertFalse(asyncio.run(policy.enforce(client)))
        warning.assert_called_once()

    def test_safety_does_not_cast_healing_spell_without_mana(self):
        client = type("Client", (), {})()
        client.state = type("State", (), {
            "stats": {"hp": 8, "maxhp": 32, "sp": 1, "food": 999},
            "inventory": [Item(
                42, item_type=c.TYPE_SPELL, name="minor healing",
                extra={"cost": 3})],
        })()
        client.fired = []
        client.clears = 0

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))

        async def clear_actions():
            client.clears += 1

        client.fire = fire
        client.clear_actions = clear_actions
        policy = SafetyPolicy()
        self.assertFalse(asyncio.run(policy.enforce(client)))
        self.assertEqual(client.fired, [])
        self.assertEqual(policy._last_heal, 0)
        client.state.stats["hp"] = 20
        self.assertFalse(asyncio.run(policy.enforce(client)))

    def test_safety_food_waits_for_authoritative_update(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 32, "maxhp": 32, "food": 50},
                    "inventory": [Item(42, name="staple food")],
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

        policy = SafetyPolicy()
        client = FakeClient()
        self.assertFalse(asyncio.run(policy.enforce(client)))
        self.assertTrue(asyncio.run(policy.enforce(client)))
        self.assertEqual(client.applied, [42])
        client.state.stats["food"] = 250
        self.assertTrue(asyncio.run(policy.enforce(client)))

    def test_safety_eats_generic_server_typed_food(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 32, "maxhp": 32, "food": 50},
                    "inventory": [Item(
                        43, item_type=c.TYPE_FOOD, quality=100,
                        name="peeled rambutan")],
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        self.assertFalse(asyncio.run(SafetyPolicy().enforce(client)))
        self.assertEqual(client.applied, [43])

    def test_safety_never_eats_unidentified_food(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 32, "maxhp": 32, "food": 50},
                    "inventory": [Item(45, item_type=c.TYPE_FOOD, name="mystery stew")],
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        asyncio.run(SafetyPolicy().enforce(client))
        self.assertEqual(client.applied, [])

    def test_farm_circuit_identifies_found_food_with_a_device(self):
        from .navigation import unidentified_food
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.place_item(Item(46, item_type=c.TYPE_FOOD, name="mystery stew"), 7)
        # Not food she eats (drink, flesh), or not yet hers (unpaid): no trigger.
        client.state.place_item(Item(47, item_type=c.TYPE_FLESH, name="rat tail"), 7)
        client.state.place_item(Item(48, item_type=c.TYPE_FOOD, name="shop bread",
                                     flags=c.ITEM_UNPAID), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        self.assertEqual([item.tag for item in unidentified_food(client)], [46])
        with patch.object(InventoryCapabilityTask, "candidates", return_value=[object()]):
            self.assertTrue(circuit._needs_inventory_capability(
                client, InventoryCapabilityTask.PURPOSE_IDENTIFY))
        with patch.object(InventoryCapabilityTask, "candidates", return_value=[]):
            self.assertFalse(circuit._needs_inventory_capability(
                client, InventoryCapabilityTask.PURPOSE_IDENTIFY))

    def test_carried_food_is_identified_before_a_food_trip(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["food"] = 100
        client.state.place_item(Item(49, item_type=c.TYPE_FOOD, name="mystery stew"), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")], clock=lambda: 10.0)
        with patch.object(InventoryCapabilityTask, "candidates", return_value=[object()]):
            # Identification first: no shop or temple trip yet.
            self.assertFalse(circuit._needs_food_resupply(client))
            # A running identification is no trip that blocks feeding her.
            circuit._capability = InventoryCapabilityTask(InventoryCapabilityTask.PURPOSE_IDENTIFY)
            self.assertFalse(circuit.needs_outside_food())
            circuit._capability = None
            # The short pause after a success still waits for the device;
            # a failure backoff does not, even in its last minute.
            purpose = InventoryCapabilityTask.PURPOSE_IDENTIFY
            circuit._capability_retry_at[purpose] = 11.0
            circuit._capability_succeeded.add(purpose)
            self.assertFalse(circuit._needs_food_resupply(client))
            circuit._capability_succeeded.clear()
            circuit._capability_retry_at[purpose] = 40.0
            self.assertTrue(circuit._needs_food_resupply(client))
            circuit._capability_retry_at.clear()
        # Without a device, or once it backs off, the food trip goes ahead.
        with patch.object(InventoryCapabilityTask, "candidates", return_value=[]):
            self.assertTrue(circuit._needs_food_resupply(client))

    def test_proactive_restock_needs_money_and_room(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update({"food": 900, "weight_limit": 100})
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")], clock=lambda: 10.0)
        # Not hungry, no reserve: only with money for the whole order and room.
        self.assertFalse(circuit._needs_food_resupply(client))
        with patch.object(FarmCircuitTask, "_food_order_cost", return_value=500):
            client.state.bank_balance = 1_001
            self.assertFalse(circuit._needs_food_resupply(client))
            client.state.bank_balance = 1_500
            self.assertTrue(circuit._needs_food_resupply(client))
        client.state.bank_balance = 5_000
        client.state.place_item(Item(60, name="iron ingots", weight=85.0, quality=80), 7)
        self.assertFalse(circuit._needs_food_resupply(client))
        # Hungry, the old fallback still applies.
        client.state.stats["food"] = 100
        self.assertTrue(circuit._needs_food_resupply(client))

    def test_safety_eats_before_healing_when_starving(self):
        class FakeClient:
            def __init__(self):
                # Critical health and food 0, a safe meal carried, no heal.
                self.state = type("State", (), {
                    "stats": {"hp": 10, "maxhp": 100, "food": 0},
                    "inventory": [Item(50, item_type=c.TYPE_FOOD, quality=100,
                                       name="wholemeal bread")],
                })()
                self.applied, self.clears = [], 0

            async def apply(self, tag):
                self.applied.append(tag)

            async def clear_actions(self):
                self.clears += 1

        client = FakeClient()
        policy = SafetyPolicy()
        self.assertFalse(asyncio.run(policy.enforce(client)))
        self.assertEqual((client.applied, client.clears), ([50], 0))
        # While the stat update is pending, nothing clears it.
        self.assertTrue(asyncio.run(policy.enforce(client)))
        self.assertEqual((client.applied, client.clears), ([50], 0))

    def test_a_queued_meal_survives_clears_from_any_caller(self):
        client = AtrinikClient(ClientConfig())
        sent = []

        async def send(packet):
            sent.append(packet)

        client.send = send
        client.state.stats.update({"hp": 10, "maxhp": 100, "food": 0})
        client.state.player_tag = 7
        client.state.place_item(Item(51, item_type=c.TYPE_FOOD, quality=100,
                                     name="wholemeal bread"), 7)
        asyncio.run(SafetyPolicy().enforce(client))
        applied = len(sent)
        # Any caller's clear inside the window is skipped: the APPLY stays queued.
        asyncio.run(client.clear_actions())
        self.assertEqual(len(sent), applied)
        # After it, clears work again.
        client._queue_protected_until = 0.0
        asyncio.run(client.clear_actions())
        self.assertEqual(len(sent), applied + 1)

    def test_safety_never_eats_identified_cursed_food(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 32, "maxhp": 32, "food": 50},
                    "inventory": [
                        Item(43, flags=c.ITEM_CURSED,
                             item_type=c.TYPE_FOOD, quality=100,
                             name="staple food of poison"),
                        Item(44, item_type=c.TYPE_FOOD, quality=100,
                             name="peeled rambutan"),
                    ],
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        self.assertFalse(asyncio.run(SafetyPolicy().enforce(client)))
        self.assertEqual(client.applied, [44])

    def test_navigation_does_not_cancel_new_food_application(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.stats["food"] = 50
        client.state.inventory = [Item(42, name="staple food")]
        client.applied = []
        client.decisions = []

        async def apply(tag):
            client.applied.append(tag)

        client.apply = apply
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/bank", BankBalanceTask("Tolmir"),
            combat_approach=True)
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.applied, [42])
        self.assertEqual(client.clears, 0)
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.clears, 0)

    @staticmethod
    def _combat_client():
        class FakeClient:
            def __init__(self):
                game_map = MapState(path="/test", width=17, height=17)
                game_map.tile(11, 8).objects[3] = MapObject(
                    3, 1, 0, 0, name="kobold chief", target_id=100,
                    target_hp=100)
                game_map.tile(9, 8).objects[3] = MapObject(
                    3, 2, 0, 0, name="kobold shaman", target_id=200,
                    target_hp=100)
                self.state = type("State", (), {
                    "phase": "playing",
                    "stats": {"hp": 32, "maxhp": 32, "food": 999,
                              "level": 1},
                    "stat_observed_at": {},
                    "inventory": [], "ground": [], "map": game_map,
                    "player_tag": 7, "equipment": {}, "items": {},
                })()
                self.faces = {}
                self.targets = []
                self.combat = []
                self.combat_force = []
                self.moves = []
                self.direct_moves = []
                self.clears = 0
                self.cleared_targets = 0

            async def target(self, x, y, target_id=0):
                self.targets.append((x, y, target_id))


            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)
                self.combat_force.append(force)

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

            async def move(self, direction, run=False):
                self.direct_moves.append((direction, run))

            async def clear_actions(self):
                self.clears += 1

            async def clear_target(self):
                self.cleared_targets += 1
                self.state.target_id = 0

            async def fire(self, direction, tag=0):
                pass

            async def apply(self, tag):
                pass

        return FakeClient()

    def test_farm_lures_flying_target_off_unwalkable_ground(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100,
            target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.blocked.add((1, 0))
        task = FarmTask(zone="/test", target="lost soul")
        task.map_node = node
        self.assertFalse(task.target_tile_walkable(client, 9, 8))
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 100)])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)
        direction, running = client.direct_moves[-1]
        self.assertFalse(running)
        self.assertNotEqual(direction, 3)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertTrue(node.walkable(dx, dy))

    def test_water_lure_waits_for_authoritative_position_ack(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        soul = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100,
            target_hp=100)
        client.state.map.tile(9, 8).objects[3] = soul
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.blocked.add((1, 0))
        task = FarmTask(zone="/test", target="lost soul")
        task.map_node = node
        threats = [(1, 9, 8, soul)]
        self.assertTrue(asyncio.run(
            task.lure_off_unwalkable_tile(client, 9, 8, threats)))
        attempt = task._retreat_attempt
        self.assertIsNotNone(attempt)
        task._retreat_attempt = (*attempt[:5], time.monotonic() - 0.75)
        self.assertTrue(asyncio.run(
            task.lure_off_unwalkable_tile(client, 9, 8, threats)))
        self.assertEqual(len(client.direct_moves), 1)
        task._retreat_attempt = (*attempt[:5], time.monotonic() - 2.0)
        self.assertTrue(asyncio.run(
            task.lure_off_unwalkable_tile(client, 9, 8, threats)))
        self.assertIn((attempt[0], attempt[3], attempt[4]),
                      task._retreat_blocked)
        self.assertEqual(len(client.direct_moves), 1)
        task._last_lure_step_at -= 4.0
        self.assertTrue(asyncio.run(
            task.lure_off_unwalkable_tile(client, 9, 8, threats)))
        self.assertEqual(len(client.direct_moves), 2)

    def test_water_lure_abandons_target_without_target_movement(self):
        client = self._combat_client()
        # The server clears selection as the passive target leaves the
        # viewport; lure timeout must remain independent of selected state.
        client.state.target_id = 0
        client.state.stats["target_hp"] = 0
        task = FarmTask(zone="/test", target="lost soul")
        now = time.monotonic()
        task._engaged_target = (100, "/test", 1, 0, now - 13.0)
        task._lure_target_id = 100
        task._lure_target_world = (1, 0)
        task._lure_progress_at = now - task.LURE_STALL_SECONDS - 0.1

        self.assertTrue(asyncio.run(
            task.pursue_selected_target_last_seen(client, None)))
        self.assertEqual(client.cleared_targets, 1)
        self.assertIsNone(task._engaged_target)
        self.assertTrue(task.target_temporarily_unreachable(100))

        # A still-visible unprovoked target cannot immediately recreate the
        # engagement and block farm-circuit rotation.
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100,
            target_hp=100)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])

    def test_low_health_retreat_runs_away_from_enemy(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100)
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.combat, [False])
        self.assertIn(client.direct_moves[0][0], {6, 7, 8})
        self.assertFalse(client.direct_moves[0][1])

    def test_low_health_retreat_rejects_authored_graveyard_walls(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 23
        client.state.map.world_y = 11
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="skeleton fighter", target_id=100)
        node = MapNode(path="/test", width=24, height=24)
        node.terrain = {(x, y): 1 for x in range(24) for y in range(24)}
        node.blocked.update({(23, 10), (23, 12), (22, 10), (22, 12)})
        task = FarmTask(zone="/test")
        task.map_node = node
        task.map_bounds = (24, 24)
        with patch("atrinik_bot.tasks.random.random", return_value=0):
            self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.direct_moves, [(7, False)])
        attempt = task._retreat_attempt
        self.assertEqual(attempt[1:5], (23, 11, 22, 11))

    def test_farm_patrol_learns_rejected_live_step_and_routes_around(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 1
        client.state.map.world_y = 11
        node = MapNode(path="/test", width=12, height=14)
        node.terrain = {(x, y): 1 for x in range(12) for y in range(14)}
        task = FarmTask(zone="/test", patrol=[(7, 9)])
        task.map_node = node
        task.map_bounds = (12, 14)
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.direct_moves), 1)
        first_direction = client.direct_moves[0][0]
        dx, dy = c.DIRECTION_DELTAS[first_direction]
        rejected = ("/test", 1 + dx, 11 + dy)
        task._last_action = 0
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.direct_moves), 1)
        attempt = task._patrol_step_attempt
        task._patrol_step_attempt = (*attempt[:5], time.monotonic() - 2.0)
        task._last_action = 0
        asyncio.run(task.tick(client))
        self.assertIn(rejected, task._patrol_blocked)
        self.assertEqual(len(client.direct_moves), 2)
        self.assertNotEqual(client.direct_moves[1][0], first_direction)

    def test_cornered_pack_fights_adjacent_blocker_to_open_exit(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 22
        client.state.map.world_y = 11
        client.state.map.tile(7, 9).objects[3] = MapObject(
            3, 1, 0, 0, name="wolf", target_id=100)
        client.state.map.tile(6, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="grey bear", target_id=200)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.blocked.update({
            (21, 10), (22, 10), (23, 10), (21, 11),
            (23, 11), (22, 12), (23, 12),
        })
        task = FarmTask(zone="/test")
        task.map_node = node
        task.map_bounds = (25, 25)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.targets, [(7, 9, 100)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(task._engaged_target[0], 100)
        self.assertEqual(task._cornered_breakout_target, 100)

        # The low-health safety loop must not undo the forced breakout on its
        # next tick while the same blocker still occupies the only exit.
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.targets[-1], (7, 9, 100))
        self.assertEqual(client.combat, [True, True])

    def test_pursued_retreat_turns_to_fight_one_attacker(self):
        client = self._combat_client()
        client.state.stats.update(hp=100, maxhp=176)
        task = FarmTask(zone="/test")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        # The shaman at (9,8) stays adjacent however Sera steps (the fake map
        # never moves it); the chief at (11,8) is three tiles off.
        for _ in range(task.PURSUIT_STAND_STEPS):
            self.assertTrue(asyncio.run(task.low_health_retreat(client)))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0
        self.assertEqual(client.targets, [])
        self.assertEqual(len(client.direct_moves), task.PURSUIT_STAND_STEPS)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat[-1], True)
        self.assertEqual(task._cornered_breakout_target, 200)
        self.assertEqual(len(client.direct_moves), task.PURSUIT_STAND_STEPS)

    def test_pursued_stand_ends_at_the_health_floor(self):
        client = self._combat_client()
        client.state.stats.update(hp=100, maxhp=176)
        task = FarmTask(zone="/test")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        for _ in range(task.PURSUIT_STAND_STEPS + 1):
            asyncio.run(task.low_health_retreat(client))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0
        self.assertEqual(task._cornered_breakout_target, 200)
        # The next hit drops her to 30%: she runs again.
        client.state.stats["hp"] = 53
        moves = len(client.direct_moves)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(task._cornered_breakout_target, 0)
        self.assertEqual(client.combat[-1], False)
        self.assertEqual(len(client.direct_moves), moves + 1)
        # The engagement with the proven attacker survives the release.
        self.assertEqual(task._engaged_target[0], 200)

    def _pursued_stand_task(self, client):
        client.state.stats.update(hp=100, maxhp=176)
        task = FarmTask(zone="/test")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        return task

    def _pursue_until_stand(self, client, task):
        for _ in range(task.PURSUIT_STAND_STEPS + 1):
            asyncio.run(task.low_health_retreat(client))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0

    def test_pursued_stand_skips_authored_peaceful_actors(self):
        client = self._combat_client()
        task = self._pursued_stand_task(client)
        # A wounded peaceful deer is adjacent too; the shaman is the stand.
        client.state.map.tile(7, 8).objects[3] = MapObject(
            3, 3, 0, 0, name="deer", target_id=300, target_hp=20)
        task.map_node.peaceful_identities.add("deer")
        self._pursue_until_stand(client, task)
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(task._cornered_breakout_target, 200)

    def test_pursued_stand_skips_set_aside_actors(self):
        client = self._combat_client()
        task = self._pursued_stand_task(client)
        # A wounded adjacent kobold the server would not select.
        client.state.map.tile(7, 8).objects[3] = MapObject(
            3, 3, 0, 0, name="kobold", target_id=300, target_hp=20)
        client.state.map.ignored_targets[300] = time.monotonic() + 60
        self._pursue_until_stand(client, task)
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(task._cornered_breakout_target, 200)

    def test_pursuit_count_does_not_outlive_its_retreat(self):
        client = self._combat_client()
        client.state.stats.update(hp=100, maxhp=176)
        task = FarmTask(zone="/test")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        for _ in range(task.PURSUIT_STAND_STEPS):
            asyncio.run(task.low_health_retreat(client))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0
        # Much later, a new encounter: the first retreat is a retreat.
        task._pursuit_last_at -= task.PURSUIT_WINDOW_SECONDS + 1
        asyncio.run(task.low_health_retreat(client))
        self.assertEqual(client.targets, [])
        self.assertEqual(task._cornered_breakout_target, 0)

    def test_neutral_farm_stands_only_against_the_engaged_target(self):
        client = self._combat_client()
        client.state.stats.update(hp=100, maxhp=176)
        # Two adjacent giant slugs; combat text names only the species, so
        # both pass as attackers. The more wounded (100) is a bystander; the
        # engaged attacker is 200.
        client.state.messages = [(time.time(), 2, "ffffff", "giant slug hit you for 9.")]
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="giant slug", target_id=200, target_hp=90)
        client.state.map.tile(7, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="giant slug", target_id=100, target_hp=20)
        task = FarmTask(zone="/test", neutral_targets=True)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        task.remember_engagement(client, 9, 8, 200)
        for _ in range(task.PURSUIT_STAND_STEPS + 1):
            asyncio.run(task.low_health_retreat(client))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0
        self.assertNotIn((7, 8, 100), client.targets)
        self.assertEqual(task._cornered_breakout_target, 200)

    def test_critically_hurt_pursued_retreat_keeps_running(self):
        client = self._combat_client()
        client.state.stats.update(hp=50, maxhp=176)
        task = FarmTask(zone="/test")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        client.state.map.world_x = client.state.map.world_y = 12
        for _ in range(task.PURSUIT_STAND_STEPS + 2):
            self.assertTrue(asyncio.run(task.low_health_retreat(client)))
            task._retreat_attempt = None
            task._last_retreat_step_at = 0.0
        self.assertEqual(client.targets, [])
        self.assertEqual(len(client.direct_moves), task.PURSUIT_STAND_STEPS + 2)

    def test_single_enemy_retreat_keeps_fighting_committed_target(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="hill giant", target_id=100)
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 10, 8, 100)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.targets, [(9, 8, 100)])
        self.assertEqual(client.combat, [True])

    def test_low_health_retreat_disengages_when_healing_is_exhausted(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100)
        client.state.stats.update(hp=20, maxhp=100, sp=0)
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 4}))
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 9, 8, 100)

        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.combat, [False])
        self.assertIn("emergency-disengage",
                      [action for action, _ in client.decisions])

    def test_emergency_recall_readies_device_then_fires_after_item_ack(self):
        client = self._combat_client()
        wand = Item(
            77, item_type=c.TYPE_WAND, quality=80, condition=80,
            name="wand of word of recall (lvl 12)")
        client.state.inventory = [wand]
        client.state.items[wand.tag] = wand
        client.applied = []
        client.fired = []

        async def apply(tag):
            client.applied.append(tag)

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))

        client.apply = apply
        client.fire = fire
        task = FarmTask(zone="/test")

        self.assertTrue(asyncio.run(task.emergency_recall(
            client, ratio=0.30, nearby_count=1, hostile_contact=True)))
        self.assertEqual(client.applied, [77])
        self.assertEqual(client.fired, [])
        wand.flags |= c.ITEM_APPLIED
        self.assertTrue(asyncio.run(task.emergency_recall(
            client, ratio=0.30, nearby_count=1, hostile_contact=True)))
        self.assertEqual(client.fired, [(0, 0)])
        self.assertEqual(task._emergency_recall_origin, "/test")

    def test_emergency_recall_prefers_rod_and_rejects_unknown_device(self):
        client = self._combat_client()
        unknown = Item(
            70, item_type=c.TYPE_WAND,
            name="wand of word of recall (lvl 12)")
        spell = Item(
            71, item_type=c.TYPE_SPELL, name="word of recall",
            extra={"cost": 24})
        wand = Item(
            72, item_type=c.TYPE_WAND, quality=70, condition=70,
            name="wand of word of recall (lvl 12)")
        rod = Item(
            73, item_type=c.TYPE_ROD, quality=70, condition=70,
            name="rod of word of recall (lvl 12)")
        client.state.stats["sp"] = 30
        client.state.inventory = [unknown, spell, wand, rod]

        self.assertEqual(
            [item.tag for item in FarmTask.emergency_recall_candidates(client)],
            [73, 72, 71])

    def test_emergency_recall_preserves_charge_outside_lethal_threshold(self):
        client = self._combat_client()
        wand = Item(
            77, item_type=c.TYPE_WAND, quality=80, condition=80,
            name="wand of word of recall (lvl 12)")
        client.state.inventory = [wand]
        client.state.items[wand.tag] = wand
        task = FarmTask(zone="/test")

        self.assertFalse(asyncio.run(task.emergency_recall(
            client, ratio=0.70, nearby_count=1, hostile_contact=True)))
        self.assertEqual(client.clears, 0)
        self.assertEqual(client.combat, [])

    def test_retreat_waits_for_authoritative_position_ack(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="hill giant", target_id=100)
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 9, 8, 100)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        attempt = task._retreat_attempt
        self.assertIsNotNone(attempt)
        task._retreat_attempt = (*attempt[:5], time.monotonic() - 0.75)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(len(client.direct_moves), 1)
        task._retreat_attempt = (*attempt[:5], time.monotonic() - 2.0)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertIn((attempt[0], attempt[3], attempt[4]),
                      task._retreat_blocked)
        self.assertEqual(len(client.direct_moves), 1)
        task._last_retreat_step_at -= 2.5
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(len(client.direct_moves), 2)

    def test_recent_offscreen_attacker_is_not_replaced_by_visible_add(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="ogre", target_id=200, target_hp=100)
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "hill giant hit you for 13 damage.")]
        task = FarmTask(zone="/test", target="hill giant|ogre")
        task.remember_engagement(client, 11, 8, 100)
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 100)
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)

    def test_first_incoming_hit_commits_attacker_before_emergency_heal(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="skeleton fighter", target_id=200,
            target_hp=100)
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "skeleton fighter hit you for 10 damage.")]
        client.state.stats.update(hp=16, maxhp=32, sp=10)
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 3}))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = FarmTask(zone="/test", target="skeleton")
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 200)
        self.assertEqual(fired, [(0, 42)])
        task._last_action = 0
        asyncio.run(task.tick(client))
        # The attacker remains committed for threat tracking, but emergency
        # retreat must stop trading hits after the heal is in flight.
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)

    def test_isolated_neutral_kites_before_routine_healing(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(hp=85, maxhp=100, sp=10,
                                  action_time=1.5, target_hp=100)
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 3}))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = FarmTask(
            zone="/test", target="quickwood", neutral_targets=True)
        task.safety.flee_below = 0.75
        task.safety.heal_below = 0.92
        task.remember_engagement(client, 9, 8, target.target_id)

        asyncio.run(task.tick(client))
        self.assertEqual(fired, [])
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat, [True])
        self.assertTrue(client.direct_moves)

    def test_isolated_stationary_caster_orbits_until_swing_window(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 2, 0, 0, name="kobold shaman", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(
            hp=100, maxhp=100, action_time=1.5, target_hp=100)
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.caster_identities.add("kobold shaman")
        task = FarmTask(zone="/test", target="kobold shaman")
        task.map_node = node
        task.map_bounds = (25, 25)
        task.remember_engagement(client, 9, 8, target.target_id)
        task._caster_anchor = (
            target.target_id, "/test", 11, 10,
            time.monotonic() - 1.0)

        asyncio.run(task.tick(client))

        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [(1, False)])
        self.assertIn("caster-orbit",
                      [action for action, _ in client.decisions])
        self.assertEqual(task._kite_step_attempt[1:5], (10, 10, 10, 9))

    def test_stationary_caster_orbit_holds_for_auto_swing(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 2, 0, 0, name="kobold shaman", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(
            hp=100, maxhp=100, action_time=0.2, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.caster_identities.add("kobold shaman")
        task = FarmTask(zone="/test", target="kobold shaman")
        task.map_node = node
        task.map_bounds = (25, 25)
        task.remember_engagement(client, 9, 8, target.target_id)
        task._caster_anchor = (
            target.target_id, "/test", 11, 10,
            time.monotonic() - 1.0)

        asyncio.run(task.tick(client))

        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [])

    def test_empty_name_wasp_matches_wire_animation_alias(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {734: "wasp_giant"}
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 734, 0, c.MAP_FLAG_ANIMATION, target_id=200,
            target_hp=100, animation=734)
        task = FarmTask(
            zone="/test", target="giant wasp|wasp_giant|wasp giant")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(task._engaged_target[0], 200)

    def test_human_target_name_matches_underscored_animation_alias(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {672: "treant_evil"}
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 672, 0, c.MAP_FLAG_ANIMATION, target_id=200,
            target_hp=100, animation=672)
        task = FarmTask(zone="/test", target="evil treant")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(task._engaged_target[0], 200)

    def test_neutral_wasp_pack_counts_only_engaged_target(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {734: "wasp_giant"}
        for x, target_id in ((9, 200), (10, 300)):
            client.state.map.tile(x, 8).objects[3] = MapObject(
                3, 734, 0, c.MAP_FLAG_ANIMATION, target_id=target_id,
                target_hp=100, animation=734)
        task = FarmTask(
            zone="/test", target="giant wasp|wasp_giant|wasp giant",
            neutral_targets=True)
        task.remember_engagement(client, 9, 8, 200)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(task._engaged_target[0], 200)

    def test_reversed_wasp_alias_does_not_promote_neutral_bystander(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {734: "wasp_giant"}
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 734, 0, c.MAP_FLAG_ANIMATION, target_id=200,
            target_hp=100, animation=734)
        client.state.map.tile(10, 8).objects[3] = MapObject(
            3, 734, 0, c.MAP_FLAG_ANIMATION, name="giant wasp",
            target_id=300, target_hp=100, animation=734)
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "giant wasp hit you for 8 damage.")]
        task = FarmTask(
            zone="/test", target="giant wasp|wasp_giant|wasp giant",
            neutral_targets=True)
        task.remember_engagement(client, 9, 8, 200)
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 200)
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [])

    def test_offscreen_engaged_wasp_does_not_promote_every_visible_bystander(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {734: "wasp_giant"}
        for x, target_id in ((9, 300), (10, 400)):
            client.state.map.tile(x, 8).objects[3] = MapObject(
                3, 734, 0, c.MAP_FLAG_ANIMATION,
                name="giant wasp", target_id=target_id,
                target_hp=100, animation=734)
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "giant wasp hit you for 8 damage.")]
        client.state.combat = True
        client.state.target_id = 200
        client.state.stats["target_hp"] = 50
        client.decisions = []
        client.record_action = lambda action, detail: (
            client.decisions.append((action, detail)))
        task = FarmTask(
            zone="/test", target="giant wasp|wasp_giant|wasp giant",
            neutral_targets=True)
        task.remember_engagement(client, 12, 8, 200)
        asyncio.run(task.tick(client))
        self.assertFalse(any(
            action == "farm-pack-split"
            for action, _ in client.decisions))
        self.assertTrue(client.direct_moves)

    def test_aggressive_farm_uses_authored_detection_radius_for_adds(self):
        def run(second_x):
            client = self._combat_client()
            client.state.map.tiles.clear()
            for x, target_id in ((9, 200), (second_x, 300)):
                client.state.map.tile(x, 8).objects[3] = MapObject(
                    3, 1, 0, 0, name="giant frog",
                    target_id=target_id, target_hp=100)
            client.decisions = []
            client.record_action = lambda action, detail="": (
                client.decisions.append((action, detail)))
            task = FarmTask(
                zone="/test", target="giant frog",
                aggressive_detection_ranges={"giant frog": 3})
            task.remember_engagement(client, 9, 8, 200)
            asyncio.run(task.tick(client))
            return client

        outside = run(12)
        self.assertFalse(any(
            action == "farm-pack-split"
            for action, _ in outside.decisions))
        inside = run(11)
        self.assertTrue(any(
            action == "farm-pack-split"
            for action, _ in inside.decisions))

    def test_aggressive_farm_waits_for_isolated_detection_before_targeting(self):
        def run(target_x):
            client = self._combat_client()
            client.state.map.tiles.clear()
            client.state.map.tile(target_x, 8).objects[3] = MapObject(
                3, 1, 0, 0, name="giant frog",
                target_id=200, target_hp=100)
            task = FarmTask(
                zone="/test", target="giant frog",
                aggressive_detection_ranges={"giant frog": 3},
                patrol=[(1, 1)])
            asyncio.run(task.tick(client))
            return client, task

        outside, outside_task = run(12)
        self.assertEqual(outside.targets, [])
        self.assertIsNone(outside_task._engaged_target)
        inside, inside_task = run(11)
        self.assertEqual(inside.targets, [(11, 8, 200)])
        self.assertEqual(inside_task._engaged_target[0], 200)

    def test_requested_authored_passives_do_not_form_a_pack_before_aggro(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {672: "treant_evil"}
        for x, target_id in ((9, 200), (10, 300)):
            client.state.map.tile(x, 8).objects[3] = MapObject(
                3, 672, 0, c.MAP_FLAG_ANIMATION, target_id=target_id,
                target_hp=100, animation=672)
        task = FarmTask(zone="/test", target="evil treant")
        task.map_node = MapNode(path="/test", width=20, height=20)
        task.map_node.terrain = {
            (x, y): 1 for x in range(20) for y in range(20)}
        task.map_node.peaceful_identities = {"treant evil"}
        task.remember_engagement(client, 9, 8, 200)
        client.state.messages = [
            (time.time(), 2, "cc66ff", "evil treant hit you for 6 damage.")]
        asyncio.run(task.tick(client))
        self.assertEqual(client.combat, [True])
        self.assertEqual(task._engaged_target[0], 200)

    def test_neutral_farm_does_not_switch_to_unrequested_nearby_wildlife(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="giant wasp", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="wolf", target_id=300, target_hp=100)
        task = FarmTask(
            zone="/test", target="giant wasp", neutral_targets=True)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(11, 8, 200)])
        self.assertEqual(task._engaged_target[0], 200)

    def test_neutral_wildlife_that_attacks_becomes_pack_threat(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.animations = {734: "wasp_giant"}
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 734, 0, c.MAP_FLAG_ANIMATION, name="giant wasp",
            target_id=200, target_hp=100, animation=734)
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 2, 0, 0, name="wolf", target_id=300, target_hp=100)
        client.state.messages = [
            (time.time(), 2, "cc66ff", "wolf hit you for 11 damage.")]
        task = FarmTask(
            zone="/test", target="giant wasp|wasp_giant|wasp giant",
            neutral_targets=True)
        task.remember_engagement(client, 9, 8, 200)
        asyncio.run(task.tick(client))
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)

    def test_recent_invisible_first_attacker_without_history_takes_clear_step(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "skeleton fighter hit you for 10 damage.")]
        task = FarmTask(zone="/test", target="skeleton",
                        patrol=[(15, 15)])
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.direct_moves), 1)
        self.assertFalse(client.direct_moves[0][1])
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.moves, [])

    def test_recent_invisible_first_attacker_backtracks_safe_history(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 8
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "hill giant hit you for 12 damage.")]
        task = FarmTask(zone="/test", target="hill giant")
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task.map_node = node
        task.map_bounds = (25, 25)
        task._safe_position_history = [
            ("/test", 5, 8, time.monotonic())]
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.direct_moves), 1)
        direction, running = client.direct_moves[0]
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertLess(max(abs(8 + dx - 5), abs(8 + dy - 8)), 3)
        self.assertFalse(running)
        self.assertEqual(client.combat, [False])
        self.assertIsNotNone(task._retreat_attempt)

    def test_offscreen_escape_never_returns_to_first_hit_tile(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 23
        client.state.map.world_y = 11
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "skeleton fighter hit you for 10 damage.")]
        node = MapNode(path="/test", width=24, height=24)
        node.terrain = {(x, y): 1 for x in range(24) for y in range(24)}
        node.blocked.update({(23, 10), (23, 12)})
        task = FarmTask(zone="/test", target="skeleton")
        task.map_node = node
        task.map_bounds = (24, 24)
        with patch("atrinik_bot.tasks.random.random", return_value=0):
            asyncio.run(task.tick(client))
            first = task._retreat_attempt
            self.assertIsNotNone(first)
            client.state.map.world_x, client.state.map.world_y = first[3:5]
            task._last_action = 0
            task._last_retreat_step_at -= 3
            asyncio.run(task.tick(client))
        second = task._retreat_attempt
        self.assertIsNotNone(second)
        origin = (23, 11)
        first_distance = max(abs(first[3] - origin[0]),
                             abs(first[4] - origin[1]))
        second_distance = max(abs(second[3] - origin[0]),
                              abs(second[4] - origin[1]))
        self.assertGreater(second_distance, first_distance)
        self.assertNotEqual(second[3:5], origin)

    def test_farm_retreat_prefers_open_flank_over_farther_dead_end(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 2
        client.state.map.world_y = 2
        threat = MapObject(
            3, 1, 0, 0, name="grey bear", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 7).objects[3] = threat
        node = MapNode(path="/test", width=5, height=5)
        node.terrain = {
            (2, 2): 1, (1, 2): 1,
            (3, 2): 1, (4, 2): 1, (4, 3): 1, (4, 4): 1,
        }
        task = FarmTask(zone="/test", target="grey bear")
        task.map_node = node
        task.map_bounds = (5, 5)
        task.remember_engagement(client, 9, 7, 200)
        with patch("atrinik_bot.tasks.random.random", return_value=0):
            self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.direct_moves[-1][0], 3)

    def test_navigation_retreat_prefers_open_flank_over_farther_dead_end(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 2
        client.state.map.world_y = 2
        threat = MapObject(
            3, 1, 0, 0, name="grey bear", target_id=200,
            target_hp=100)
        client.state.map.tile(9, 7).objects[3] = threat
        node = MapNode(path="/test", width=5, height=5)
        node.terrain = {
            (2, 2): 1, (1, 2): 1,
            (3, 2): 1, (4, 2): 1, (4, 3): 1, (4, 4): 1,
        }
        graph = WorldGraph()
        graph.nodes[node.path] = node
        task = NavigateThenTask(
            graph, "/test", FarmTask(zone="/test"), combat_approach=True)
        self.assertTrue(asyncio.run(task._retreat_step(
            client, [(1, 9, 7, threat)])))
        self.assertEqual(client.direct_moves[-1][0], 3)

    def test_low_health_retreat_uses_previous_adjacent_map_seam(self):
        client = self._combat_client()
        task = FarmTask(zone="/shattered_islands/world_3_69")
        task.map_bounds = (24, 24)
        client.state.map.path = "/shattered_islands/world_3_69"
        client.state.map.world_x = 9
        client.state.map.world_y = 0
        task.remember_engagement(client, 8, 8, 100)
        task._engaged_target = None
        client.state.map.path = "/shattered_islands/world_3_68"
        client.state.map.world_y = 23
        client.state.map.tiles.clear()
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertIn(client.direct_moves[-1][0], {1, 2, 8})
        self.assertFalse(client.direct_moves[-1][1])


    def test_farm_refuses_to_pull_clustered_pack_member_with_spell(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100)
        client.state.map.tile(12, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="lost soul", target_id=200)
        client.state.stats["sp"] = 20
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="cause light wounds",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4}))
        fired = []
        async def fire(direction, tag=0):
            fired.append((direction, tag))
        client.fire = fire
        task = FarmTask(zone="/test", target="lost soul")
        asyncio.run(task.tick(client))
        self.assertEqual(fired, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)
        self.assertFalse(client.direct_moves[-1][1])
        self.assertEqual(client.moves, [])

    def test_ranged_pull_refuses_unidentified_applied_ammunition(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100, target_hp=100)
        client.state.map.tile(12, 8).objects[3] = target
        bow = Item(41, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
                   quality=100, name="short bow")
        arrows = Item(42, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
                      quality=255, name="arrows", quantity=15)
        client.state.inventory.extend((bow, arrows))
        self.assertFalse(asyncio.run(FarmTask().ranged_pull(
            client, [(3, 11, 8, target)])))

    def test_ranged_pull_sidesteps_to_exact_line_before_approaching(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        client.state.map.tile(13, 9).objects[3] = target
        client.state.inventory.extend((
            Item(41, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
                 quality=100, condition=100, name="short bow"),
            Item(42, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
                 quality=100, condition=100, name="arrows", quantity=15),
        ))
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="quickwood")
        task.map_node = node

        self.assertTrue(asyncio.run(task.ranged_pull(
            client, [(5, 13, 9, target)])))

        self.assertEqual(client.direct_moves, [(5, False)])
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.targets, [])

    def test_ranged_pull_equips_mundane_compatible_ammunition(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        client.state.map.tile(12, 8).objects[3] = target
        bow = Item(41, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
                   quality=81, name="spruce short bow")
        rare = Item(42, item_type=c.TYPE_ARROW, quality=90,
                    name="pine arrow of accuracy", quantity=2)
        mundane = Item(43, item_type=c.TYPE_ARROW, quality=80,
                       name="pine arrow", quantity=25)
        client.state.inventory.extend((bow, rare, mundane))
        applied = []

        async def apply(tag):
            applied.append(tag)

        client.apply = apply
        self.assertTrue(asyncio.run(FarmTask().ranged_pull(
            client, [(4, 12, 8, target)])))
        self.assertEqual(applied, [mundane.tag])

    def test_ranged_pull_prefers_trained_spell_over_incidental_bow(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        client.state.map.tile(12, 8).objects[3] = target
        client.state.stats["sp"] = 20
        bow = Item(41, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
                   quality=81, name="spruce short bow")
        arrow = Item(42, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
                     quality=80, name="pine arrow", quantity=25)
        spell = Item(
            43, item_type=c.TYPE_SPELL, name="magic bullet",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4})
        client.state.inventory.extend((bow, arrow, spell))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = FarmTask(combat_spell="magic bullet")
        self.assertTrue(asyncio.run(task.ranged_pull(
            client, [(4, 12, 8, target)])))
        self.assertEqual(fired, [(3, spell.tag)])

    def test_magic_build_does_not_fall_back_to_bow_when_mana_is_low(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        client.state.map.tile(12, 8).objects[3] = target
        client.state.stats["sp"] = 0
        bow = Item(41, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
                   quality=81, name="spruce short bow")
        arrow = Item(42, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
                     quality=80, name="pine arrow", quantity=25)
        spell = Item(
            43, item_type=c.TYPE_SPELL, name="magic bullet",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4})
        client.state.inventory.extend((bow, arrow, spell))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = FarmTask(combat_spell="magic bullet")

        self.assertFalse(asyncio.run(task.ranged_pull(
            client, [(4, 12, 8, target)])))
        self.assertEqual(fired, [])

    def test_neutral_ranged_pull_refuses_creature_behind_target(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100,
            target_hp=100)
        bystander = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200,
            target_hp=100)
        client.state.map.tile(12, 8).objects[3] = target
        client.state.map.tile(15, 8).objects[3] = bystander
        client.state.stats["sp"] = 20
        spell = Item(
            43, item_type=c.TYPE_SPELL, name="magic bullet",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4})
        client.state.inventory.append(spell)
        fired = []
        client.fire = lambda direction, tag=0: fired.append((direction, tag))
        task = FarmTask(
            combat_spell="magic bullet", neutral_targets=True)

        self.assertFalse(asyncio.run(task.ranged_pull(
            client, [(4, 12, 8, target)])))
        self.assertEqual(fired, [])
        self.assertEqual(client.direct_moves, [])

    def test_pursuit_holds_through_transient_missing_target_frame(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.target_id = 100
        client.state.stats["target_hp"] = 50
        task = FarmTask(zone="/test", target="quickwood")
        task._engaged_target = (
            100, "/test", 12, 8, time.monotonic() - 0.5)

        self.assertTrue(asyncio.run(
            task.pursue_selected_target_last_seen(client, None)))
        self.assertEqual(client.direct_moves, [])

    def test_nearly_dead_offscreen_pursuer_gets_bounded_auto_swing_window(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.target_id = 100
        client.state.combat = False
        client.state.stats.update({
            "hp": 32, "maxhp": 32, "target_hp": 3,
            "action_time": 0.5,
        })
        client.state.messages = [(
            time.time(), 2, "cc66ff",
            "quickwood hit you for 5 damage.")]
        task = FarmTask(zone="/test", target="quickwood")
        task._engaged_target = (
            100, "/test", 12, 8, time.monotonic() - 0.5)

        asyncio.run(task.tick(client))

        self.assertEqual(client.direct_moves, [])
        self.assertTrue(client.combat[-1])

        # The hold is only offered near the actual skill timer. With more
        # than 800 ms remaining the ordinary controlled retreat continues.
        task._last_action -= 1.0
        client.state.stats["action_time"] = 1.5
        asyncio.run(task.tick(client))
        self.assertTrue(client.direct_moves)


    def test_adjacent_blocker_triggers_split_from_sticky_target(self):
        client = self._combat_client()
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 11, 8, 100)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)

    def test_approach_routes_around_blocked_direct_step(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100, target_hp=100)
        client.state.map.tile(11, 8).objects[3] = target
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.blocked.add((1, 0))
        task = FarmTask(zone="/test")
        task.map_node = node
        self.assertTrue(asyncio.run(
            task.approach_target(client, 11, 8, target)))
        self.assertEqual(client.direct_moves, [(4, False)])

    def test_approach_marks_unacknowledged_live_step_and_reroutes(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="ettin", target_id=100, target_hp=100)
        client.state.map.tile(11, 8).objects[3] = target
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test")
        task.map_node = node
        self.assertTrue(asyncio.run(
            task.approach_target(client, 11, 8, target)))
        first_direction = client.direct_moves[-1][0]
        attempt = task._approach_attempt
        self.assertIsNotNone(attempt)
        task._approach_attempt = (*attempt[:5], time.monotonic() - 1.0,
                                  attempt[6])
        self.assertTrue(asyncio.run(
            task.approach_target(client, 11, 8, target)))
        self.assertNotEqual(client.direct_moves[-1][0], first_direction)
        self.assertIn(("/test", 1, 0), task._approach_blocked)

    def test_farm_keeps_pulled_target_until_it_dies(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(12, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold chief", target_id=100,
            target_hp=100)
        client.state.map.tile(14, 7).objects[3] = MapObject(
            3, 2, 0, 0, name="kobold shaman", target_id=200,
            target_hp=100)
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 12, 8, 100)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (12, 8, 100))

    def test_three_nearby_enemies_trigger_early_controlled_retreat(self):
        client = self._combat_client()
        client.state.stats["hp"] = 29
        client.state.map.tile(8, 10).objects[3] = MapObject(
            3, 3, 0, 0, name="kobold guard", target_id=300,
            target_hp=100)
        task = FarmTask(zone="/test")
        asyncio.run(task.tick(client))
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)
        self.assertFalse(client.direct_moves[-1][1])
        self.assertEqual(client.targets, [])

    def test_named_capable_spawn_target_outranks_nearer_filler(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 8
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100,
            target_hp=100)
        client.state.map.tile(15, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200,
            target_hp=100)
        task = FarmTask(
            zone="/test", target="evil treant|quickwood",
            priority_spawns=[(11, 8, "Fahrgorm")])
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (11, 8, 100))

    def test_named_farm_does_not_provoke_unrequested_peaceful_wildlife(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100, target_hp=100)
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="evil treant", target_id=200, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.peaceful_identities.update({"quickwood", "evil treant"})
        task = FarmTask(zone="/test", target="Fahrgorm|evil treant")
        task.map_node = node
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (11, 8, 200))

    def test_peaceful_occupant_of_named_spawn_is_cleared_for_reroll(self):
        # Live on world_14_77: Kotung's spawn rolls an unaggressive evil
        # treant 90% of the time. Ignoring it as peaceful wildlife left the
        # spawn occupied, so Kotung never appeared and Sera patrolled forever.
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 8
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100, target_hp=100)
        client.state.map.tile(15, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.peaceful_identities.update({"quickwood", "evil treant"})
        # Production priorities name every candidate of the shared spawn.
        spawns = [(11, 8, "Kotung"), (11, 8, "evil treant")]
        task = FarmTask(zone="/test", target="Kotung", priority_spawns=spawns)
        task.map_node = node
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (11, 8, 100))
        self.assertEqual(task._visible_target_count, 1)
        # A peaceful blocker is provoked, not merely selected.
        self.assertTrue(client.combat_force)
        self.assertIs(client.combat_force[-1], True)

        # Off the spawn point the same species is still unrequested wildlife,
        # even though it is one of the spawn's named candidates.
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 8
        client.state.map.tile(15, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100, target_hp=100)
        task = FarmTask(zone="/test", target="Kotung", priority_spawns=spawns)
        task.map_node = node
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])
        self.assertEqual(task._visible_target_count, 0)
        self.assertNotIn(True, client.combat_force)

    def test_committed_peaceful_target_is_finished(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.peaceful_identities.add("quickwood")
        task = FarmTask(zone="/test", target="evil treant")
        task.map_node = node
        task.remember_engagement(client, 9, 8, 100)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (9, 8, 100))

    def test_dormant_disguised_monster_on_named_spawn_is_targeted(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 8
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="tied bollard", target_id=100,
            target_hp=100)
        client.state.map.tile(15, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200,
            target_hp=100)
        task = FarmTask(
            zone="/test", target="Fahrgorm|evil treant|quickwood",
            priority_spawns=[(11, 8, "Fahrgorm")])
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets[0], (11, 8, 100))

    def test_boss_farm_splits_adjacent_add_before_resuming_target(self):
        client = self._combat_client()
        task = FarmTask(zone="/test", target="kobold chief")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)
        self.assertEqual(client.moves, [])

    def test_adjacent_melee_holds_target_without_entering_enemy_tile(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        task = FarmTask(zone="/test")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(client.moves, [])

    def test_unselectable_targets_are_not_attacked(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        task = FarmTask(zone="/test")

        async def refuse(*args, **kwargs):
            return False

        with patch("atrinik_bot.tasks.select_target", refuse):
            asyncio.run(task.tick(client))
        # Combat earlier retries may have enabled is switched off.
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(client.moves, [])

    def test_unselectable_targets_release_their_engagement(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        task = FarmTask(zone="/test")

        async def refuse(*args, **kwargs):
            return False

        with patch("atrinik_bot.tasks.select_target", refuse):
            asyncio.run(task.tick(client))
        self.assertIsNone(task._engaged_target)

    def test_late_acknowledgements_lift_a_set_aside(self):
        from . import targeting
        from .targeting import target_set_aside

        state = GameState()
        now = time.monotonic()
        state.map.ignored_targets[1343754] = now + targeting.UNRESPONSIVE_TARGET_SECONDS
        self.assertTrue(target_set_aside(state, 1343754))
        # The server's reply arrives just after the timeout.
        state.target_ack = (1343754, now + 0.1)
        self.assertFalse(target_set_aside(state, 1343754))
        self.assertNotIn(1343754, state.map.ignored_targets)

    def test_set_aside_targets_stay_threats_but_are_not_chosen(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        client.state.map.ignored_targets[200] = time.monotonic() + 60.0
        self.assertEqual(len(list(client.state.map.targets(friendly=False))), 1)
        task = FarmTask(zone="/test")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])
        self.assertNotIn(True, client.combat)

    def test_melee_kite_steps_out_until_swing_timer_is_ready(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=1.40)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="quickwood")
        task.map_node = node
        task.map_bounds = (25, 25)
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 9, 8, target)))

        direction, running = client.direct_moves[-1]
        self.assertFalse(running)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertEqual(max(abs(9 - (8 + dx)), abs(8 - (8 + dy))), 2)

    def test_melee_kite_stands_when_another_hostile_is_close(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="giant slug", target_id=100, target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        # A second slug two tiles away: kiting would walk into it.
        client.state.map.tile(6, 7).objects[3] = MapObject(
            3, 1, 0, 0, name="giant slug", target_id=101, target_hp=100)
        client.state.stats.update(target_hp=55, action_time=1.40)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="giant slug")
        task.map_node = node
        task.map_bounds = (25, 25)
        task.remember_engagement(client, 9, 8, target.target_id)
        # A kite step already sent is still awaited, not abandoned.
        task._kite_step_attempt = (
            "/test", 10, 10, 11, 10, time.monotonic(), target.target_id)
        self.assertTrue(asyncio.run(task.melee_kite(client, 9, 8, target)))
        task._kite_step_attempt = None
        self.assertFalse(asyncio.run(task.melee_kite(client, 9, 8, target)))
        self.assertEqual(client.direct_moves, [])
        # A spell finisher's line-keeping kite is not stopped by the crowd.
        self.assertTrue(asyncio.run(task.melee_kite(
            client, 9, 8, target, preserve_spell_line=Item(0, name="firebolt"))))

    def test_melee_kite_holds_adjacent_when_swing_is_imminent(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=0.20)
        task = FarmTask(zone="/test", target="quickwood")
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 9, 8, target)))
        self.assertEqual(client.direct_moves, [])

    def test_melee_kite_ticks_down_last_server_swing_timer(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=2.25)
        client.state.stat_observed_at["action_time"] = time.monotonic() - 2.10
        task = FarmTask(zone="/test", target="quickwood")
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 9, 8, target)))
        self.assertEqual(client.direct_moves, [])

    def test_melee_kite_waits_for_pursuer_then_approaches_after_timeout(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(12, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=1.20)
        task = FarmTask(zone="/test", target="quickwood")
        task.remember_engagement(client, 12, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 12, 8, target)))
        self.assertEqual(client.direct_moves, [])
        task._kite_gap_since = time.monotonic() - task.MELEE_GAP_TIMEOUT - 0.1
        self.assertFalse(asyncio.run(
            task.melee_kite(client, 12, 8, target)))

    def test_melee_kite_moves_before_pursuer_crosses_empty_tile(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(10, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=1.20)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="quickwood")
        task.map_node = node
        task.map_bounds = (25, 25)
        task.remember_engagement(client, 10, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 10, 8, target)))

        direction, running = client.direct_moves[-1]
        self.assertFalse(running)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertEqual(max(abs(10 - (8 + dx)), abs(8 - (8 + dy))), 3)

    def test_melee_kite_preserves_gap_before_first_damage(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="evil treant", target_id=100,
            target_hp=100)
        client.state.map.tile(10, 8).objects[3] = target
        client.state.stats.update(target_hp=100, action_time=1.20)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="evil treant")
        task.map_node = node
        task.remember_engagement(client, 10, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 10, 8, target)))
        self.assertTrue(client.direct_moves)

    def test_melee_kite_holds_one_empty_tile_for_imminent_swing(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(10, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=0.20)
        task = FarmTask(zone="/test", target="quickwood")
        task.remember_engagement(client, 10, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 10, 8, target)))
        self.assertEqual(client.direct_moves, [])

    def test_melee_kite_builds_two_empty_tile_latency_buffer(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=55)
        client.state.map.tile(11, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=1.20)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="quickwood")
        task.map_node = node
        task.remember_engagement(client, 11, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.melee_kite(client, 11, 8, target)))
        direction, running = client.direct_moves[-1]
        self.assertFalse(running)
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertEqual(max(abs(11 - (8 + dx)), abs(8 - (8 + dy))), 4)

    def test_melee_dodges_midcycle_without_crossing_target_tile(self):
        client = self._combat_client()
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(zone="/test")
        task._melee_target_id = target.target_id
        task._melee_cycle_anchor = time.monotonic() - 0.8
        client.state.stats["weapon_speed"] = 2.0
        self.assertTrue(asyncio.run(
            task.melee_position(client, 9, 8, target)))
        self.assertTrue(client.direct_moves)
        self.assertNotEqual(client.direct_moves[-1][0], 3)

    def test_melee_seeks_monster_back_before_predicted_swing(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(3, 2, 0, 0, name="kobold",
                           target_id=300, target_hp=100, direction=3)
        client.state.map.tile(9, 9).objects[3] = target
        task = FarmTask(zone="/test")
        task._melee_target_id = target.target_id
        task._melee_cycle_anchor = time.monotonic() - 1.5
        client.state.stats["weapon_speed"] = 2.0
        self.assertTrue(asyncio.run(
            task.melee_position(client, 9, 9, target)))
        # East-facing monster: stand west of it and face east for +5.
        self.assertEqual(client.direct_moves[-1], (5, False))

    def test_melee_records_tactical_reason_in_activity_history(self):
        client = self._combat_client()
        client.recorded = []
        client.record_action = lambda action, detail: client.recorded.append(
            (action, detail))
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(zone="/test")
        task._melee_target_id = target.target_id
        task._melee_cycle_anchor = time.monotonic() - 0.8
        client.state.stats["weapon_speed"] = 2.0
        asyncio.run(task.melee_position(client, 9, 8, target))
        self.assertEqual(client.recorded[0][0], "melee-dodge")
        self.assertIn("phase=", client.recorded[0][1])
        self.assertIn("exposure=", client.recorded[0][1])

    def test_melee_avoids_exposing_back_to_second_enemy(self):
        client = self._combat_client()
        primary = client.state.map.tile(9, 8).objects[3]
        primary.direction = 3
        client.state.map.tile(7, 8).objects[3] = MapObject(
            3, 3, 0, 0, name="kobold guard", target_id=400,
            target_hp=100, direction=3)
        task = FarmTask(zone="/test")
        task._melee_target_id = primary.target_id
        task._melee_cycle_anchor = time.monotonic() - 1.5
        task._melee_facing = 3
        client.state.stats["weapon_speed"] = 2.0
        self.assertTrue(asyncio.run(
            task.melee_position(client, 9, 8, primary)))
        # Holding east would expose Sera to the western enemy backstab. Move
        # around the primary instead, without stepping into either enemy.
        self.assertNotIn(client.direct_moves[-1][0], (3, 7))

    def test_directional_spell_waits_for_exact_alignment(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(11, 9).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=100)
        client.state.stats["sp"] = 20
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="cause light wounds",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4}))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = FarmTask(zone="/test", target="kobold",
                        combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(fired, [])
        # Preserve range and sidestep south onto the exact east-west firing
        # line instead of approaching diagonally toward melee contact.
        self.assertEqual(client.direct_moves, [(5, False)])
        self.assertEqual(client.moves, [])

        client.direct_moves.clear()
        client.state.map.tiles.clear()
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=100)
        task = FarmTask(zone="/test", target="kobold",
                        combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(fired, [(3, 42)])
        self.assertEqual(client.moves, [])

    def test_targeted_spell_uses_selected_target_not_rounded_direction(self):
        spell = Item(42, item_type=c.TYPE_SPELL, name="magic bullet",
                     extra={"flags": c.SPELL_DESC_ENEMY})
        self.assertEqual(FarmTask.spell_fire_direction(
            spell, 11, 9, 8, 8), 0)

    @staticmethod
    def _wizard_training_client(hp, x=11, y=8):
        client = QuestTests._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(x, y).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=hp)
        client.state.stats["sp"] = 20
        client.state.inventory.extend((
            Item(41, item_type=c.TYPE_SKILL, name="wizardry spells"),
            Item(42, item_type=c.TYPE_SPELL, name="cause light wounds",
                 extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 4}),
        ))
        client.fired = []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))

        client.fire = fire
        return client

    def _crowded_wizardry_farm(self, spell="cause light wounds"):
        client = self._wizard_training_client(100, x=11, y=8)
        client.state.stats["sp"] = 0
        # A pair of kobolds three tiles east, and a lone one four tiles north.
        client.state.map.tile(12, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=101, target_hp=100)
        client.state.map.tile(8, 4).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=102, target_hp=100)
        task = FarmTask(
            zone="/test", target="kobold", neutral_targets=True,
            combat_skill="wizardry spells",
            combat_spell=spell)
        return client, task

    def test_wizardry_training_pulls_only_isolated_targets(self):
        client, task = self._crowded_wizardry_farm()
        self.assertTrue(task.single_target_wizardry(client))
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 102)

    def test_knowing_firestorm_alone_keeps_single_target_wizardry(self):
        client, task = self._crowded_wizardry_farm()
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="firestorm",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 8}))
        # Still casting Cause Light Wounds: one at a time.
        self.assertTrue(task.single_target_wizardry(client))
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 102)

    def test_casting_firestorm_lifts_single_target_wizardry(self):
        client, task = self._crowded_wizardry_farm(spell="firestorm")
        # Configured but not yet learned: still one at a time.
        self.assertTrue(task.single_target_wizardry(client))
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="firestorm",
            extra={"flags": c.SPELL_DESC_DIRECTION, "cost": 8}))
        self.assertFalse(task.single_target_wizardry(client))
        asyncio.run(task.tick(client))
        self.assertEqual(task._engaged_target[0], 100)

    def test_wizardry_gear_weighs_int_pow_and_mana(self):
        def ring(name):
            return Item(1, item_type=c.TYPE_RING, name=name, quality=80,
                        condition=100)
        generic = InventoryPolicy()
        caster = InventoryPolicy(caster_stats=True)
        pow_ring = ring("copper ring (Pow+1) (item power+1)")
        mana_ring = ring("bronze ring (mana+5)")
        plus_ring = ring("silver ring +2")
        # Generic scoring is unchanged: every "+N" counts the same.
        self.assertEqual(generic.gear_score(pow_ring), 260 + 16)
        self.assertEqual(generic.gear_score(mana_ring), 260 + 40)
        # A caster prefers one point of Pow over five mana or a plain +2.
        self.assertGreater(caster.gear_score(pow_ring), caster.gear_score(mana_ring))
        self.assertGreater(caster.gear_score(pow_ring), caster.gear_score(plus_ring))
        self.assertGreater(caster.gear_score(ring("ring (Int+2)")),
                           caster.gear_score(pow_ring))
        self.assertLess(caster.gear_score(ring("ring (Int-1)")),
                        caster.gear_score(ring("plain ring")))

    def test_wizardry_training_scores_gear_as_a_caster(self):
        client = self._wizard_training_client(100)
        task = FarmTask(zone="/test", combat_skill="wizardry spells")
        asyncio.run(task.maintain_inventory(client))
        self.assertTrue(task.inventory_policy.caster_stats)
        melee = FarmTask(zone="/test", combat_skill="slash weapons")
        asyncio.run(melee.maintain_inventory(client))
        self.assertFalse(melee.inventory_policy.caster_stats)

    def test_skill_training_keeps_spell_pressure_before_finisher_window(self):
        client = self._wizard_training_client(50, x=9, y=8)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(client.moves, [])

    def test_offensive_spells_preserve_three_emergency_heals(self):
        client = self._wizard_training_client(50)
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 4}))
        spell = next(item for item in client.state.inventory
                     if item.name == "cause light wounds")
        task = FarmTask(combat_spell="cause light wounds")
        client.state.stats["sp"] = 15
        self.assertEqual(task.healing_mana_reserve(client), 12)
        self.assertFalse(task.offensive_spell_affordable(client, spell))
        client.state.stats["sp"] = 16
        self.assertTrue(task.offensive_spell_affordable(client, spell))

    def test_offensive_spell_ledger_reserves_cost_before_stats_ack(self):
        client = self._wizard_training_client(50)
        client.state.stats.update({"sp": 47, "maxsp": 47})
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 4}))
        spell = next(item for item in client.state.inventory
                     if item.name == "cause light wounds")
        spell.extra["cost"] = 6
        task = FarmTask(combat_spell="cause light wounds")

        # The server-visible SP deliberately remains stale at 47 throughout.
        # Five six-point casts leave a locally reserved 17 SP; a sixth would
        # leave 11 and must be rejected to retain three four-point heals.
        for _ in range(5):
            self.assertTrue(task.offensive_spell_affordable(client, spell))
            task.commit_offensive_spell(client, spell)
        self.assertEqual(task._spell_budget_sp, 17)
        self.assertFalse(task.offensive_spell_affordable(client, spell))
        self.assertEqual(client.state.stats["sp"], 47)

    def test_offensive_spell_ledger_reconciles_ack_and_regeneration(self):
        client = self._wizard_training_client(50)
        client.state.stats.update({"sp": 47, "maxsp": 47})
        spell = next(item for item in client.state.inventory
                     if item.name == "cause light wounds")
        spell.extra["cost"] = 6
        task = FarmTask(combat_spell="cause light wounds")

        task.commit_offensive_spell(client, spell)
        self.assertEqual(task._spell_budget_sp, 41)
        client.state.stats["sp"] = 41
        self.assertEqual(task._sync_offensive_spell_budget(client), 41)
        client.state.stats["sp"] = 43
        self.assertEqual(task._sync_offensive_spell_budget(client), 43)

    def test_neutral_wizard_finisher_waits_for_mana_instead_of_melee(self):
        client = self._wizard_training_client(15, x=9, y=8)
        client.state.stats["sp"] = 15
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="minor healing",
            extra={"cost": 4}))
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        client.state.messages = []
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.combat, [False])
        self.assertTrue(client.direct_moves)

    def test_neutral_farm_uses_spell_only_for_pull_and_finisher(self):
        client = self._wizard_training_client(50)
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        self.assertIsNone(task.primary_combat_spell(client))
        self.assertIsNotNone(task.pull_spell_item(client))

        target = client.state.map.tile(11, 8).objects[3]
        target.target_hp = 15
        self.assertTrue(task.should_training_finish(client, target))

    def test_training_configuration_does_not_disable_isolated_pull(self):
        client = self._wizard_training_client(100)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.targets, [(11, 8, 100)])

    def test_skill_training_level_cap_restores_main_school_kills(self):
        client = self._wizard_training_client(15)
        skill = next(item for item in client.state.inventory
                     if item.name == "wizardry spells")
        skill.extra["level"] = 3
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds",
            combat_skill_until_level=3)
        target = client.state.map.tile(11, 8).objects[3]
        self.assertFalse(task.training_active(client))
        self.assertIsNone(task.primary_combat_spell(client))
        self.assertFalse(task.should_training_finish(client, target))

        skill.extra["level"] = 2
        self.assertTrue(task.training_active(client))
        self.assertIsNotNone(task.primary_combat_spell(client))
        self.assertTrue(task.should_training_finish(client, target))

    def test_skill_training_adapts_finisher_window_to_melee_damage(self):
        client = self._wizard_training_client(100)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        target = client.state.map.tile(11, 8).objects[3]
        client.state.target_id = target.target_id
        self.assertFalse(task.should_training_finish(client, target))
        for hp in (86, 72, 58, 44):
            client.state.stats["target_hp"] = hp
            target.target_hp = 0
            self.assertFalse(task.should_training_finish(client, target))
        client.state.stats["target_hp"] = 30
        self.assertGreaterEqual(task.finisher_window(target.target_id), 30)
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])

    def test_neutral_wizard_handoff_precedes_two_queued_melee_hits(self):
        client = self._wizard_training_client(100)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        target = client.state.map.tile(11, 8).objects[3]
        client.state.target_id = target.target_id
        self.assertFalse(task.should_training_finish(client, target))

        target.target_hp = 0
        client.state.stats["target_hp"] = 70
        self.assertTrue(task.should_training_finish(client, target))
        self.assertGreaterEqual(task.finisher_window(target.target_id), 70)

        ordinary = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        target.target_hp = 100
        ordinary.should_training_finish(client, target)
        target.target_hp = 0
        client.state.stats["target_hp"] = 70
        self.assertFalse(ordinary.should_training_finish(client, target))
        self.assertLessEqual(ordinary.finisher_window(target.target_id), 40)

        cold_client = self._wizard_training_client(70)
        cold_task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        cold_target = cold_client.state.map.tile(11, 8).objects[3]
        self.assertTrue(cold_task.should_training_finish(
            cold_client, cold_target))

    def test_neutral_wizard_handoff_times_out_stale_full_hp(self):
        client = self._wizard_training_client(100)
        client.state.stats["weapon_speed"] = 2.0
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        target = client.state.map.tile(11, 8).objects[3]
        task._melee_target_id = target.target_id
        task._melee_cycle_anchor = time.monotonic() - 2.2

        self.assertTrue(asyncio.run(task.training_finisher(
            client, 11, 8, target)))
        self.assertEqual(client.combat, [False])

    def test_melee_cycle_anchor_starts_when_engaged_target_closes(self):
        client = self._wizard_training_client(100, x=11, y=8)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)

        task.remember_engagement(client, 11, 8, 100)
        self.assertEqual(task._melee_target_id, 0)
        task.remember_engagement(client, 9, 8, 100)
        self.assertEqual(task._melee_target_id, 100)
        self.assertGreater(task._melee_cycle_anchor, 0)

    def test_confirmed_first_melee_hit_triggers_neutral_wizard_handoff(self):
        client = self._wizard_training_client(100, x=9, y=8)
        client.state.messages = []
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        task.remember_engagement(client, 9, 8, target.target_id)
        self.assertFalse(task.observed_training_melee_hit(client, target))
        client.state.messages.append((
            time.time(), 0, 0, "You hit kobold for 27 (0) with slash."))

        self.assertTrue(task.observed_training_melee_hit(client, target))
        self.assertTrue(asyncio.run(task.training_finisher(
            client, 9, 8, target)))
        self.assertEqual(client.combat, [False])

    def test_skill_training_uses_small_window_for_tough_target(self):
        client = self._wizard_training_client(100)
        task = FarmTask(combat_skill="wizardry spells")
        target = client.state.map.tile(11, 8).objects[3]
        client.state.target_id = target.target_id
        task.should_training_finish(client, target)
        for hp in (98, 96, 94):
            target.target_hp = 0
            client.state.stats["target_hp"] = hp
            task.should_training_finish(client, target)
        self.assertLessEqual(task.finisher_window(target.target_id), 8)

    def test_skill_training_casts_only_the_finishing_blow(self):
        client = self._wizard_training_client(15)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.moves, [])

        task._last_action = 0
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])

    def test_wizard_finisher_yields_to_melee_when_player_is_hurt(self):
        client = self._wizard_training_client(15, x=9, y=8)
        client.state.stats["hp"] = 79
        client.state.stats["maxhp"] = 100
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.combat, [True])

    def test_reached_training_cap_ends_farm_before_next_neutral_pull(self):
        client = self._wizard_training_client(100)
        skill = next(item for item in client.state.inventory
                     if item.name == "wizardry spells")
        skill.extra["level"] = 10
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds",
            combat_skill_until_level=10, neutral_targets=True)

        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.fired, [])
        self.assertEqual(client.combat, [False])

    def test_wizard_finisher_has_per_target_cast_budget(self):
        client = self._wizard_training_client(15, x=9, y=8)
        client.state.stats.update({"sp": 100, "maxsp": 100})
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        for _ in range(task.WIZARD_FINISHER_CAST_LIMIT):
            task._last_finisher_at = 0
            task._finisher_pending_feedback = None
            self.assertTrue(asyncio.run(
                task.training_finisher(client, 9, 8, target)))
        task._last_finisher_at = 0
        task._finisher_pending_feedback = None
        self.assertFalse(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(
            len(client.fired), task.WIZARD_FINISHER_CAST_LIMIT)

    def test_wizard_finisher_waits_for_target_feedback_before_recast(self):
        client = self._wizard_training_client(15, x=9, y=8)
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")

        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        task._last_finisher_at = 0
        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])

        # An observed HP decrease is authoritative feedback and permits the
        # next deliberate cast without waiting for the timeout.
        target.target_hp = 8
        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [(3, 42), (3, 42)])

    def test_wizard_finisher_kites_while_feedback_is_pending(self):
        client = self._wizard_training_client(15, x=9, y=8)
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        client.state.stats["action_time"] = 1.0
        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertTrue(client.direct_moves)

    def test_in_flight_wizard_finisher_continues_below_start_health_gate(self):
        client = self._wizard_training_client(15, x=9, y=8)
        target = client.state.map.tile(9, 8).objects[3]
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")

        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        client.state.stats["hp"] = 79
        target.target_hp = 8
        task._last_finisher_at = 0
        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [(3, 42), (3, 42)])

    def test_neutral_wizard_finisher_opens_range_before_first_cast(self):
        client = self._wizard_training_client(15, x=9, y=8)
        target = client.state.map.tile(9, 8).objects[3]
        client.state.stats["action_time"] = 0.0
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        task.remember_engagement(client, 9, 8, target.target_id)

        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(client.fired, [])
        self.assertTrue(client.direct_moves)
        self.assertEqual(client.combat, [False])

    def test_neutral_wizard_finisher_refuses_bystander_behind_target(self):
        client = self._wizard_training_client(15, x=12, y=8)
        target = client.state.map.tile(12, 8).objects[3]
        client.state.map.tile(15, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="quickwood", target_id=200,
            target_hp=100)
        task = FarmTask(
            combat_skill="wizardry spells",
            combat_spell="cause light wounds", neutral_targets=True)
        task.remember_engagement(client, 12, 8, target.target_id)

        self.assertFalse(asyncio.run(
            task.training_finisher(client, 12, 8, target)))
        self.assertEqual(client.fired, [])

    def test_skill_training_uses_selected_target_protocol_hp(self):
        client = self._wizard_training_client(0)
        target = client.state.map.tile(11, 8).objects[3]
        client.state.target_id = target.target_id
        client.state.stats["target_hp"] = 15
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [(3, 42)])
        self.assertEqual(client.combat, [False])

    def test_misaligned_wizard_finisher_repositions_without_casting(self):
        client = self._wizard_training_client(15, 11, 9)
        task = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        asyncio.run(task.tick(client))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.moves, [(11, 9)])

    def test_unprobed_zero_hp_living_target_remains_attackable(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        client.state.map.tile(9, 8).objects[3].target_hp = 0
        task = FarmTask(zone="/test", target="kobold")
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])

    def test_navigation_spell_pull_does_not_round_misaligned_direction(self):
        client = self._wizard_training_client(100, 13, 9)
        client.state.map.tile(13, 10).objects[3] = MapObject(
            3, 2, 0, 0, name="kobold", target_id=200, target_hp=100)
        child = FarmTask(
            zone="/test", target="kobold",
            combat_spell="cause light wounds")
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        asyncio.run(task._defend_while_navigating(client))
        self.assertEqual(client.fired, [])

    def test_farm_random_movement_stays_inside_map_at_south_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 23
        task = FarmTask(zone="/test")
        task.map_bounds = (24, 24)
        asyncio.run(task.tick(client))
        self.assertTrue(client.moves)
        view_x, view_y = client.moves[-1]
        world_x = client.state.map.world_x + view_x - 8
        world_y = client.state.map.world_y + view_y - 8
        self.assertTrue(0 <= world_x < 24)
        self.assertTrue(0 <= world_y < 24)

    def test_farm_ignores_enemy_visible_across_map_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 23
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=100)
        task = FarmTask(zone="/test", target="kobold")
        task.map_bounds = (24, 24)
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [])

    def test_routed_farm_defense_ignores_enemy_across_map_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 8
        client.state.map.world_y = 23
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=100)
        child = FarmTask(zone="/test", target="kobold")
        child.map_bounds = (24, 24)
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])

    def test_service_navigation_ignores_enemy_across_map_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.path = "/transit"
        client.state.map.world_x = 23
        client.state.map.world_y = 13
        # This viewport object resolves to world (27, 13), outside the current
        # 25x25 map and therefore belongs to its tiled neighbor.
        client.state.map.tile(12, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="ogre", target_id=100, target_hp=100)
        graph = WorldGraph()
        graph.nodes["/transit"] = MapNode(
            "/transit", width=25, height=25,
            terrain={(x, y): 1 for x in range(25) for y in range(25)})
        task = NavigateThenTask(
            graph, "/service", BankBalanceTask("banker"),
            combat_approach=True)

        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])

    def test_routed_farm_inherits_authored_destination_bounds(self):
        graph = built_graph()
        child = FarmTask(target="kobold")
        NavigateThenTask(
            graph, "/shattered_islands/world_3_81_-1", child)
        self.assertEqual(child.map_bounds, (24, 24))

    def test_weapon_skill_finisher_swaps_matching_weapon_then_restores(self):
        client = self._combat_client()
        skill = Item(41, item_type=c.TYPE_SKILL, name="cleave weapons")
        normal = Item(
            50, flags=c.ITEM_APPLIED, item_type=c.TYPE_WEAPON,
            name="iron shortsword", required_skill_tag=99)
        finisher = Item(
            51, item_type=c.TYPE_WEAPON, name="bronze axe",
            required_skill_tag=skill.tag)
        client.state.inventory.extend((skill, normal, finisher))
        client.state.items.update({i.tag: i for i in (skill, normal, finisher)})
        client.state.equipment[c.EQUIP_WEAPON] = normal.tag
        applied = []

        async def apply(tag):
            applied.append(tag)

        client.apply = apply
        task = FarmTask(zone="/test", combat_skill="cleave weapons")
        target = MapObject(
            3, 1, 0, 0, name="kobold", target_id=100, target_hp=15)
        self.assertTrue(asyncio.run(
            task.training_finisher(client, 9, 8, target)))
        self.assertEqual(applied, [51])
        self.assertEqual(task._normal_weapon_tag, 50)

        normal.flags &= ~c.ITEM_APPLIED
        finisher.flags |= c.ITEM_APPLIED
        client.state.equipment[c.EQUIP_WEAPON] = finisher.tag
        self.assertTrue(asyncio.run(task.restore_normal_weapon(client, set())))
        self.assertEqual(applied, [51, 50])

    def test_magic_build_unreadies_launcher_inherited_across_restart(self):
        client = self._combat_client()
        bow = Item(
            60, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
            name="spruce short bow")
        arrows = Item(
            62, flags=c.ITEM_APPLIED, item_type=c.TYPE_ARROW,
            name="pine arrows", quantity=20)
        shield = Item(
            61, flags=c.ITEM_APPLIED, item_type=c.TYPE_SHIELD,
            name="birch round shield of acid protection")
        client.state.inventory.extend((bow, arrows, shield))
        applied = []

        async def apply(tag):
            applied.append(tag)
            next(item for item in client.state.inventory
                 if item.tag == tag).flags ^= c.ITEM_APPLIED

        client.apply = apply
        task = FarmTask(combat_spell="magic bullet")

        self.assertTrue(asyncio.run(task.restore_combat_loadout(client)))
        self.assertTrue(asyncio.run(task.restore_combat_loadout(client)))
        self.assertFalse(asyncio.run(task.restore_combat_loadout(client)))
        self.assertEqual(applied, [bow.tag, arrows.tag])
        self.assertEqual(task._pull_launcher_tag, 0)

    def test_durable_hybrid_build_unreadies_launcher_between_spell_milestones(self):
        client = self._combat_client()
        bow = Item(
            60, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW,
            name="spruce short bow")
        shield = Item(
            61, flags=c.ITEM_APPLIED, item_type=c.TYPE_SHIELD,
            name="birch round shield of acid protection")
        client.state.inventory.extend((bow, shield))
        applied = []

        async def apply(tag):
            applied.append(tag)
            bow.flags &= ~c.ITEM_APPLIED

        client.apply = apply
        task = FarmTask(combat_spell="", allow_launchers=False)

        self.assertTrue(asyncio.run(task.restore_combat_loadout(client)))
        self.assertFalse(asyncio.run(task.restore_combat_loadout(client)))
        self.assertEqual(applied, [bow.tag])
        self.assertTrue(shield.flags & c.ITEM_APPLIED)

    def test_vanished_engaged_target_probes_hidden_corpse_tile(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=32, food=999, level=1)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        client.faces = {}
        client.moves, client.clears, client.applied = [], 0, []
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))

        async def move_to_view(x, y):
            client.moves.append((x, y))

        async def clear_actions():
            client.clears += 1

        async def apply(tag):
            client.applied.append(tag)

        client.move_to_view = move_to_view
        client.clear_actions = clear_actions
        client.apply = apply
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 11, 8, 100)
        client.state.messages.append(
            (time.time(), 2, "ffffff", "You killed kobold."))
        task.observe_engaged_target(client, [])
        death_tile = ("/test", 13, 10)
        self.assertIn(death_tile, task._suspected_corpse_tiles)
        self.assertEqual(client.decisions, [
            ("threat-origin-retired", "confirmed kill target=100"),
            ("corpse-probe-queued", "/test (13, 10)")])

        # A kill in water leaves a corpse she cannot stand on: no probe. The
        # water tile carries an explicit terrain record without the walk bit.
        water = MapNode("/test", width=25, height=25)
        water.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        water.terrain[(13, 10)] = 2
        swamp = FarmTask(zone="/test")
        swamp.map_node = water
        swamp.remember_engagement(client, 11, 8, 101)
        client.decisions.clear()
        swamp.observe_engaged_target(client, [])
        self.assertNotIn(death_tile, swamp._suspected_corpse_tiles)
        self.assertIn(death_tile, swamp._ignored_corpse_tiles)
        self.assertIn(("corpse-probe-skip", "unstandable tile at /test (13, 10)"),
                      client.decisions)
        # A probe queued before the terrain was known is dropped, not walked.
        swamp._suspected_corpse_tiles[death_tile] = (time.monotonic() - 5.0, 0)
        swamp._ignored_corpse_tiles.clear()
        client.moves.clear()
        asyncio.run(swamp.loot_nearby(client))
        self.assertNotIn(death_tile, swamp._suspected_corpse_tiles)
        self.assertIn(death_tile, swamp._ignored_corpse_tiles)
        self.assertEqual(client.moves, [])
        # A sparse cell with no terrain record stays standable, as the server
        # treats it as passable; only an explicit record or blocker rules out.
        del water.terrain[(13, 10)]
        self.assertTrue(swamp.corpse_tile_standable("/test", 13, 10))
        water.blocked.add((13, 10))
        self.assertFalse(swamp.corpse_tile_standable("/test", 13, 10))
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        # Hidden-corpse recovery advances one acknowledged tile at a time.
        self.assertEqual(client.moves, [(9, 8)])

        # The corpse need not be present in the rendered map layer. Once the
        # character stands on its remembered tile, below-inventory data is
        # sufficient to open it.
        client.state.map.world_x, client.state.map.world_y = 13, 10
        client.state.place_item(
            Item(80, item_type=c.TYPE_CORPSE, name="kobold corpse"), 0)
        task._last_action = 0
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.applied, [80])

    def test_hidden_corpse_probe_waits_for_living_chokepoint_blocker(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        blocker = MapObject(
            3, 1, 0, 0, name="deer", target_id=200, target_hp=100)
        client.state.map.tile(9, 8).objects[3] = blocker
        node = MapNode(path="/test", width=3, height=1)
        node.terrain = {(0, 0): 1, (1, 0): 1, (2, 0): 1}
        task = FarmTask(zone="/test")
        task.map_node = node
        key = ("/test", 2, 0)
        task._suspected_corpse_tiles[key] = (time.monotonic(), 0)

        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(task._suspected_corpse_tiles[key][1], 0)
        self.assertIn("blocked-by-living-occupant", client.decisions[-1][1])

        client.state.map.tiles.clear()
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.direct_moves, [(3, False)])
        self.assertIn(key, task._suspected_corpse_tiles)

    def test_committed_pull_finishes_before_hidden_corpse_probe(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100, target_hp=100)
        client.state.map.tile(11, 8).objects[3] = target
        task = FarmTask(zone="/test", target="quickwood")
        task.remember_engagement(client, 11, 8, target.target_id)
        key = ("/test", 0, 2)
        task._suspected_corpse_tiles[key] = (time.monotonic() - 1.0, 0)

        asyncio.run(task.tick(client))
        self.assertIn(key, task._suspected_corpse_tiles)
        self.assertIsNone(task._suspected_corpse_probe)
        self.assertEqual(client.targets, [(11, 8, 100)])
        self.assertTrue(client.combat[-1])

    def test_confirmed_kill_retires_phantom_retreat_origin(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 9, 8, 100)
        task._retreat_attempt = (
            "/test", 10, 10, 9, 10, time.monotonic())
        client.state.target_id = 100
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "quickwood hit you for 7 damage."),
            (time.time(), 2, "ffffff", "You killed quickwood."),
        ]

        task.observe_engaged_target(client, [], preserve_transient=True)

        self.assertIsNone(task._engaged_target)
        self.assertIsNone(task._last_threat_origin)
        self.assertIsNone(task._retreat_attempt)
        self.assertFalse(asyncio.run(task.low_health_retreat(client)))
        self.assertEqual(client.direct_moves, [])
        self.assertIn(
            ("threat-origin-retired", "confirmed kill target=100"),
            client.decisions)

    def test_confirmed_kill_pursues_corpse_without_stale_contact_retreat(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        client.state.messages = [
            (time.time(), 2, "cc66ff",
             "quickwood hit you for 7 damage."),
            (time.time(), 2, "ffffff", "You killed quickwood."),
        ]
        task = FarmTask(zone="/test", neutral_targets=True)
        task._suspected_corpse_tiles[("/test", 13, 10)] = (
            time.monotonic() - 1.0, 0)

        asyncio.run(task.tick(client))

        self.assertEqual(client.direct_moves, [(3, False)])
        self.assertFalse(any(action == "invisible-retreat"
                             for action, _ in client.decisions))

    def test_contact_after_kill_still_triggers_invisible_retreat(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        now = time.time()
        client.state.messages = [
            (now - 0.1, 2, "ffffff", "You killed quickwood."),
            (now, 2, "cc66ff", "evil treant hit you for 7 damage."),
        ]
        task = FarmTask(zone="/test", neutral_targets=True)
        task._suspected_corpse_tiles[("/test", 13, 10)] = (
            time.monotonic() - 1.0, 0)

        asyncio.run(task.tick(client))

        self.assertTrue(client.direct_moves)
        self.assertIn(("/test", 13, 10), task._suspected_corpse_tiles)

    def test_unconfirmed_vanish_keeps_recent_retreat_origin(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 9, 8, 100)

        task.observe_engaged_target(client, [])

        self.assertIsNone(task._engaged_target)
        self.assertIsNotNone(task._last_threat_origin)
        self.assertTrue(asyncio.run(task.low_health_retreat(client)))
        self.assertTrue(client.direct_moves)

    def test_unconfirmed_distant_vanish_skips_corpse_detour(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        client.decisions = []
        client.record_action = lambda action, detail: client.decisions.append(
            (action, detail))
        task = FarmTask(zone="/test")
        task.remember_engagement(client, 14, 8, 100)
        task.observe_engaged_target(client, [])
        self.assertFalse(task._suspected_corpse_tiles)
        self.assertEqual(client.decisions, [(
            "corpse-probe-skip",
            "unconfirmed distant vanish at /test (16, 10)")])

    def test_empty_inferred_death_tile_is_abandoned(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=32, food=999, level=1)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=4, world_y=5)
        client.faces = {}
        key = ("/test", 4, 5)
        task = FarmTask(zone="/test")
        task._suspected_corpse_tiles[key] = (time.monotonic() - 1.0, 0)
        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        self.assertNotIn(key, task._suspected_corpse_tiles)
        self.assertIn(key, task._ignored_corpse_tiles)

    def test_arrived_routed_farm_delegates_emergency_retreat(self):
        client = self._combat_client()
        client.state.stats["hp"] = 10
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="lost soul", target_id=100)
        child = FarmTask(zone="/test", target="lost soul")
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.COMPLETE
        asyncio.run(task.tick(client))
        self.assertTrue(client.direct_moves)
        self.assertFalse(client.direct_moves[-1][1])
        self.assertEqual(client.combat[-1], False)


    def test_combat_approach_clears_threat_before_navigation(self):
        client = self._combat_client()
        client.state.map.tile(11, 8).objects.clear()
        child = FarmTask(zone="/test", target="kobold chief")
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        asyncio.run(task.tick(client))
        self.assertEqual(client.targets, [(9, 8, 200)])
        self.assertEqual(client.combat, [True])
        self.assertEqual(client.moves, [])

    def test_selected_offscreen_pull_stays_engaged_and_blocks_circuit(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        task = FarmTask(zone="/test", target="lost soul")
        task.remember_engagement(client, 11, 8, 100)
        client.state.target_id = 100
        task.observe_engaged_target(client, [])
        self.assertIsNotNone(task._engaged_target)
        self.assertFalse(task._suspected_corpse_tiles)
        self.assertTrue(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, task))
        client.state.target_id = 0
        task.observe_engaged_target(client, [])
        self.assertIsNone(task._engaged_target)
        self.assertIn(("/test", 13, 10), task._suspected_corpse_tiles)

    def test_committed_pull_survives_self_heal_and_viewport_gap(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        client.state.target_id = 7
        client.state.messages.append(
            (time.time(), 2, "cc66ff", "lost soul hit you for 7 damage."))
        task = FarmTask(zone="/test", target="lost soul")
        task._engaged_target = (
            100, "/test", 13, 10, time.monotonic() - 3.0)
        task.observe_engaged_target(client, [], preserve_transient=True)
        self.assertIsNotNone(task._engaged_target)
        self.assertTrue(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, task))

        client.state.messages[-1] = (
            time.time() - 7.0, 2, "cc66ff",
            "lost soul hit you for 7 damage.")
        task.observe_engaged_target(client, [], preserve_transient=True)
        self.assertIsNotNone(task._engaged_target)
        client.state.messages[-1] = (
            time.time() - 11.0, 2, "cc66ff",
            "lost soul hit you for 7 damage.")
        task.observe_engaged_target(client, [], preserve_transient=True)
        self.assertIsNotNone(task._engaged_target)
        task._engaged_target = (
            100, "/test", 13, 10, time.monotonic() - 61.0)
        task.observe_engaged_target(client, [], preserve_transient=True)
        self.assertIsNone(task._engaged_target)

    def test_selected_offscreen_pull_is_pursued_then_bounded(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.state.target_id = 100
        client.state.stats["target_hp"] = 100
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        task = FarmTask(zone="/test", target="ogre")
        task.map_node = node
        task._engaged_target = (
            100, "/test", 13, 10, time.monotonic())
        asyncio.run(task.tick(client))
        self.assertEqual(client.direct_moves, [])

        task._engaged_target = (
            100, "/test", 13, 10, time.monotonic() - 2.0)
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.direct_moves), 1)
        direction, running = client.direct_moves[0]
        self.assertIn(direction, (2, 3, 4))
        self.assertFalse(running)
        self.assertEqual(client.cleared_targets, 0)

        task._last_action = 0.0
        task._engaged_target = (
            100, "/test", 13, 10, time.monotonic() - 13.0)
        asyncio.run(task.tick(client))
        self.assertEqual(client.cleared_targets, 1)
        self.assertIsNone(task._engaged_target)

    def test_navigation_combat_records_hidden_corpse_for_farm_child(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="kobold", target_id=200, target_hp=100)
        child = FarmTask(zone="/test", target="kobold")
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        asyncio.run(task._defend_while_navigating(client))
        self.assertIsNotNone(child._engaged_target)

        client.state.map.tiles.clear()
        task._last_defense_action = 0
        asyncio.run(task._defend_while_navigating(client))
        self.assertIn(("/test", 11, 10), child._suspected_corpse_tiles)

    def test_navigation_hurt_player_heals_before_training_finisher(self):
        client = self._wizard_training_client(0, 9, 8)
        target = client.state.map.tile(9, 8).objects[3]
        client.state.target_id = target.target_id
        client.state.stats.update(hp=34, maxhp=56, target_hp=15)
        client.state.inventory.append(Item(
            43, item_type=c.TYPE_SPELL, name="minor healing"))
        child = FarmTask(
            zone="/test", target="kobold",
            combat_skill="wizardry spells",
            combat_spell="cause light wounds")
        task = NavigateThenTask(
            object(), "/test", child, combat_approach=True)
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.fired, [(0, 43)])
        self.assertEqual(client.combat, [])

    def test_combat_approach_heals_before_engaging(self):
        client = self._combat_client()
        client.state.stats["hp"] = 16
        client.state.inventory.append(Item(
            42, item_type=c.TYPE_SPELL, name="minor healing"))
        fired = []

        async def fire(direction, tag=0):
            fired.append((direction, tag))

        client.fire = fire
        task = NavigateThenTask(
            object(), "/test", FarmTask(zone="/test"),
            combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        asyncio.run(task.tick(client))
        self.assertEqual(fired, [(0, 42)])
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])
        self.assertEqual(client.clears, 1)

    def test_maintenance_navigation_replans_around_distant_pack(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        client.state.map.tile(13, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="ogre", target_id=100, target_hp=100)
        client.state.map.tile(12, 12).objects[3] = MapObject(
            3, 2, 0, 0, name="ettin", target_id=200, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/bank", BankBalanceTask("Tolmir"),
            combat_approach=True)
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertIn((17, 12), task.navigation._threat_blocked)
        self.assertIn((16, 16), task.navigation._threat_blocked)
        # Two-tile pack halos keep replanning out of the aggro edge.
        self.assertIn((15, 14), task.navigation._threat_blocked)

        # Once the pack is gone, its former squares are immediately routable;
        # collision squares learned from actual movement stalls remain
        # independent in _runtime_blocked.
        client.state.map.tiles.clear()
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(task.navigation._threat_blocked, set())

    def test_tiled_seam_uses_alternate_coordinate_when_center_is_occupied(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 2
        client.state.map.world_y = 2
        client.state.map.tile(8, 10).objects[3] = MapObject(
            3, 1, 0, 0, name="guard", target_id=100,
            target_hp=100)
        node = MapNode(path="/test", width=5, height=5)
        node.terrain = {(x, y): 1 for x in range(5) for y in range(5)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateTask(graph, "/destination")

        self.assertTrue(asyncio.run(task._click_path(client, 2, 5)))
        self.assertEqual(len(client.direct_moves), 1)
        self.assertIsNotNone(task._issued_click)
        self.assertNotEqual(task._issued_click[1:], (2, 4))

    def test_maintenance_navigation_ignores_authored_peaceful_npc(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="ranger", target_id=100, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.peaceful_identities.add("ranger")
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/bank", BankBalanceTask("Tolmir"),
            combat_approach=True)
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])
        self.assertEqual(task.navigation._runtime_blocked, set())

    def test_farm_navigation_ignores_matching_passives_until_destination(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(10, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        client.state.map.tile(11, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="evil treant", target_id=200,
            target_hp=100)
        node = MapNode(path="/transit", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        node.peaceful_identities.update({"quickwood", "evil treant"})
        client.state.map.path = "/transit"
        graph = WorldGraph()
        graph.nodes = {"/transit": node}
        task = NavigateThenTask(
            graph, "/farm",
            FarmTask(zone="/farm", target="quickwood|evil treant"),
            combat_approach=True)

        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])
        self.assertEqual(task.navigation._runtime_blocked, set())

    def test_farm_navigation_ignores_destination_passive_across_map_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.path = "/transit"
        client.state.map.tile(11, 12).objects[3] = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=100,
            target_hp=100)
        transit = MapNode(path="/transit", width=25, height=25)
        transit.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        destination = MapNode(path="/farm", width=25, height=25)
        destination.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        destination.peaceful_identities.add("quickwood")
        graph = WorldGraph()
        graph.nodes = {"/transit": transit, "/farm": destination}
        task = NavigateThenTask(
            graph, "/farm",
            FarmTask(zone="/farm", target="quickwood|evil treant"),
            combat_approach=True)

        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])

    def test_farm_navigation_ignores_global_passive_across_other_seam(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.path = "/transit"
        client.state.map.world_x = 22
        client.state.map.world_y = 6
        client.animations = {77: "quickwood"}
        client.state.map.tile(10, 4).objects[3] = MapObject(
            3, 1, 0, 0, animation=77, target_id=100,
            target_hp=100)
        transit = MapNode(path="/transit", width=24, height=24)
        transit.terrain = {
            (x, y): 1 for x in range(24) for y in range(24)}
        destination = MapNode(path="/mud-hands", width=24, height=24)
        destination.terrain = dict(transit.terrain)
        destination.peaceful_identities.add("mud hand")
        graph = WorldGraph()
        graph.nodes = {"/transit": transit, "/mud-hands": destination}
        graph.peaceful_monster_identities.add("quickwood")
        task = NavigateThenTask(
            graph, "/mud-hands",
            FarmTask(zone="/mud-hands", target="mud hand"),
            combat_approach=True)

        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [])

    def test_farm_navigation_kites_committed_transit_monster(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="hill giant", target_id=100,
            target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.stats.update(target_hp=55, action_time=1.40)
        client.state.target_id = target.target_id
        client.state.combat = True
        transit = MapNode(path="/test", width=25, height=25)
        transit.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        destination = MapNode(path="/farm", width=24, height=24)
        graph = WorldGraph()
        graph.nodes = {"/test": transit, "/farm": destination}
        farm = FarmTask(zone="/farm", target="ice golem")
        farm.remember_engagement(client, 9, 8, target.target_id)
        task = NavigateThenTask(
            graph, "/farm", farm, combat_approach=True,
            safety=farm.safety)
        task._last_pull_target = target.target_id
        # Cornered in transit: the giant is hitting her and the route has
        # made no progress, so defending is the last resort.
        client.state.messages = [(
            time.time(), 2, "cc66ff", "hill giant hit you for 9 damage.")]
        self._corner(task, client)

        self.assertTrue(asyncio.run(
            task._defend_while_navigating(client)))
        self.assertTrue(client.direct_moves)
        self.assertEqual(task._last_pull_target, target.target_id)

    def test_pack_avoidance_does_not_restart_the_cornered_clock(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        # A two-member pack five tiles off: transit avoids it and replans.
        client.state.map.tile(13, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="spider", target_id=300, target_hp=100)
        client.state.map.tile(13, 9).objects[3] = MapObject(
            3, 2, 0, 0, name="spider", target_id=301, target_hp=100)
        client.state.messages = [(
            time.time(), 2, "cc66ff", "spider hit you for 3 damage.")]
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        self._corner(task, client)
        moved_at = task._last_moved_at
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        # The replan refreshed the navigation's clock, not the movement one.
        self.assertGreater(task.navigation._last_progress, moved_at)
        self.assertEqual(task._last_moved_at, moved_at)
        # Still standing there and being hit, one closes in: she is cornered.
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="spider", target_id=300, target_hp=100)
        task._last_defense_action = 0.0
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertTrue(client.targets)

    def test_failed_first_route_under_attack_leaves_room_to_defend(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="sword spider", target_id=100, target_hp=55)
        client.state.messages = [(
            time.time(), 2, "cc66ff", "sword spider hit you for 3 damage.")]
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node, "/island": MapNode("/island", width=5, height=5)}
        task = NavigateThenTask(
            graph, "/island", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        task.status = TaskStatus.RUNNING
        # She has just moved, so the first tick holds fire and plans; the
        # plan fails (the island has no way in), but the trip does not end
        # while she is being hit ...
        m = client.state.map
        task._last_position = (m.path, m.world_x, m.world_y)
        task._last_moved_at = time.monotonic()
        asyncio.run(task.tick(client))
        self.assertEqual(task.navigation.status, TaskStatus.FAILED)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(client.targets, [])
        # ... so the next defensive tick fights back: no route is cornered.
        task._last_defense_action = 0.0
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertTrue(client.targets)
        # Once the contact ends (the first quiet tick ends the defence), the
        # failure surfaces.
        client.state.messages = []
        client.state.map.tiles.clear()
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.FAILED)

    def test_pack_avoidance_also_releases_a_held_fire_engagement(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        committed = MapObject(3, 1, 0, 0, name="spider", target_id=300, target_hp=100)
        client.state.map.tile(13, 8).objects[3] = committed
        client.state.map.tile(13, 9).objects[3] = MapObject(
            3, 2, 0, 0, name="spider", target_id=301, target_hp=100)
        client.state.target_id = committed.target_id
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node, "/farm": MapNode(path="/farm", width=24, height=24)}
        farm = FarmTask(zone="/farm", target="ice golem")
        farm.remember_engagement(client, 13, 8, committed.target_id)
        task = NavigateThenTask(graph, "/farm", farm, combat_approach=True,
                                safety=farm.safety)
        task._last_pull_target = committed.target_id
        # The pack-avoidance replan returns early; the engagement goes too.
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(task.navigation.route, [])
        self.assertIsNone(farm._engaged_target)
        self.assertEqual(task._last_pull_target, 0)
        self.assertEqual(client.cleared_targets, 1)

    def test_combat_steps_do_not_end_the_last_resort(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="sword spider", target_id=100, target_hp=55)
        client.state.messages = [(
            time.time(), 2, "cc66ff", "sword spider hit you for 3 damage.")]
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node, "/farm": MapNode(path="/farm", width=24, height=24)}
        task = NavigateThenTask(
            graph, "/farm", BuyGroundItemsTask(r"^staple food$", 5), combat_approach=True)
        self._corner(task, client)
        moved_at = task._last_moved_at
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertTrue(task._defending)
        # A defensive step moves her; she keeps defending instead of
        # treating it as renewed route progress.
        client.state.map.world_x = 11
        task._last_defense_action = 0.0
        client.targets.clear()
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(task._last_moved_at, moved_at)
        self.assertTrue(client.targets)

    @staticmethod
    def _corner(task, client):
        """She has stood on this square past the cornered threshold."""
        m = client.state.map
        task._last_position = (m.path, m.world_x, m.world_y)
        task._last_moved_at = time.monotonic() - task.TRANSIT_CORNERED_SECONDS - 1

    def test_transit_holds_fire_while_the_route_still_progresses(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        target = MapObject(
            3, 1, 0, 0, name="sword spider", target_id=100, target_hp=55)
        client.state.map.tile(9, 8).objects[3] = target
        client.state.target_id = target.target_id
        client.state.combat = True
        # Hit, but still walking: keep going instead of fighting.
        client.state.messages = [(
            time.time(), 2, "cc66ff", "sword spider hit you for 2 damage.")]
        client.decisions = []
        client.record_action = lambda action, detail="": (
            client.decisions.append((action, detail)))
        transit = MapNode(path="/test", width=25, height=25)
        transit.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": transit, "/farm": MapNode(path="/farm", width=24, height=24)}
        farm = FarmTask(zone="/farm", target="ice golem")
        farm.remember_engagement(client, 9, 8, target.target_id)
        task = NavigateThenTask(
            graph, "/farm", farm, combat_approach=True, safety=farm.safety)
        task._last_pull_target = target.target_id

        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.direct_moves, [])
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])
        self.assertEqual(task._last_pull_target, 0)
        self.assertIn(("navigation-transit-hold-fire", "target=100"), client.decisions)
        # The abandoned engagement and selection are dropped entirely.
        self.assertIsNone(farm._engaged_target)
        self.assertEqual(client.cleared_targets, 1)

        # Its squares (view (9, 8) is world (11, 10)) are marked so the route
        # steers around it; her own square stays open.
        self.assertIn((11, 10), task.navigation._threat_blocked)
        self.assertNotIn((10, 10), task.navigation._threat_blocked)
        # The click path in flight, planned without it, is cancelled once.
        self.assertEqual(client.clears, 1)
        self.assertIsNone(task.navigation._issued_click)
        task.navigation._issued_click = ("/test", 12, 12)
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.clears, 1)
        self.assertEqual(task.navigation._issued_click, ("/test", 12, 12))
        # The same creature moving rebuilds its halo, and the click with it.
        client.state.map.tiles.clear()
        client.state.map.tile(9, 9).objects[3] = target
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.clears, 2)
        self.assertIsNone(task.navigation._issued_click)
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = target

        # Also for a committed creature beyond the nearby radius.
        far = MapObject(3, 1, 0, 0, name="sword spider", target_id=101, target_hp=55)
        client.state.map.tiles.clear()
        client.state.map.tile(14, 8).objects[3] = far
        client.state.target_id = far.target_id
        farm.remember_engagement(client, 14, 8, far.target_id)
        task._last_pull_target = far.target_id
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertIsNone(farm._engaged_target)
        self.assertEqual(task._last_pull_target, 0)
        self.assertEqual(client.cleared_targets, 2)
        self.assertIn(("navigation-transit-hold-fire", "target=101"), client.decisions)
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = target

        # On the destination map the farm's own combat applies as before.
        client.state.map.path = "/farm"
        graph.nodes["/farm"].terrain = transit.terrain
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))

    def test_farm_navigation_recovers_hp_and_mana_before_advancing(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.stats.update(
            hp=32, maxhp=32, sp=5, maxsp=47, food=999)
        client.state.combat = True
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        farm = FarmTask(zone="/farm", target="ice golem")
        task = NavigateThenTask(
            graph, "/farm", farm, combat_approach=True,
            safety=farm.safety)
        task._defending = True

        self.assertTrue(asyncio.run(
            task._defend_while_navigating(client)))
        self.assertTrue(task._recovering_after_combat)
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.direct_moves, [])

        client.state.stats["sp"] = 47
        client.state.combat = False
        self.assertTrue(asyncio.run(
            task._defend_while_navigating(client)))
        self.assertFalse(task._defending)
        self.assertFalse(task._recovering_after_combat)
        self.assertEqual(client.direct_moves[-1], (0, False))

    def test_navigation_never_rests_while_starving(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        # Low mana would hold her (live: 83/102 HP, 18/26 SP), but she is
        # starving: health only drains from here.
        client.state.stats.update(hp=80, maxhp=100, sp=5, maxsp=47, food=0)
        client.state.combat = False
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        farm = FarmTask(zone="/farm", target="ice golem")
        task = NavigateThenTask(graph, "/farm", farm, combat_approach=True, safety=farm.safety)
        task._defending = True
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertFalse(task._defending)
        self.assertFalse(task._recovering_after_combat)
        self.assertEqual(client.direct_moves[-1], (0, False))
        # With safe food on her, the hold stands: safety eats it, and no
        # cleanup cancels the meal.
        client.state.inventory.append(Item(980, item_type=c.TYPE_FOOD, quality=100,
                                           name="wholemeal bread"))
        task._defending = True
        moves = len(client.direct_moves)
        asyncio.run(task._defend_while_navigating(client))
        self.assertTrue(task._defending)
        self.assertEqual(len(client.direct_moves), moves)

    def test_starving_without_food_skips_the_safety_hold(self):
        from .navigation import starving_without_food
        client = self._combat_client()
        client.state.stats.update(hp=20, maxhp=100, sp=0, maxsp=47, food=0)
        self.assertTrue(starving_without_food(client))
        bread = Item(981, item_type=c.TYPE_FOOD, quality=100, name="wholemeal bread")
        client.state.inventory.append(bread)
        self.assertFalse(starving_without_food(client))
        bread.flags = c.ITEM_CURSED
        self.assertTrue(starving_without_food(client))
        # Only food counts: coins or a key are nothing to eat.
        client.state.inventory = [
            Item(982, item_type=c.TYPE_MONEY, quality=100, name="gold coin", quantity=5),
            Item(983, quality=100, name="Incuna Tavern Barn Key")]
        self.assertTrue(starving_without_food(client))

    def test_combat_approach_retreats_from_pack(self):
        client = self._combat_client()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 3, 0, 0, name="kobold guard", target_id=300,
            target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/test", FarmTask(zone="/test"),
            combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        asyncio.run(task.tick(client))
        self.assertTrue(client.direct_moves)
        self.assertFalse(client.direct_moves[0][1])
        first_attempt = task._retreat_attempt
        self.assertIsNotNone(first_attempt)
        task._last_defense_action = 0
        task._retreat_attempt = (*first_attempt[:3], 0)
        asyncio.run(task.tick(client))
        self.assertIn(first_attempt[:3], task._retreat_blocked)
        self.assertGreaterEqual(len(client.direct_moves), 2)

    def test_resupply_navigation_splits_two_healthy_threats(self):
        client = self._combat_client()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        # Not cornered: two healthy threats are walked around, not split.
        self.assertFalse(asyncio.run(task._defend_while_navigating(client)))
        self.assertIsNone(task._retreat_attempt)
        self.assertEqual(client.targets, [])
        self.assertEqual(client.direct_moves, [])
        # Cornered (hit, no route progress): split them before fighting.
        client.state.messages = [(
            time.time(), 2, "cc66ff", "kobold chief hit you for 4 damage.")]
        self._corner(task, client)
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(task._last_pull_target, 0)
        self.assertIsNotNone(task._retreat_attempt)
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat[-1], False)

    def test_persistent_pack_at_exit_selects_an_alternate_route(self):
        client = self._combat_client()
        client.state.map.tiles.clear()
        client.state.map.tile(12, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="ogre", target_id=100, target_hp=100)
        client.state.map.tile(13, 8).objects[3] = MapObject(
            3, 2, 0, 0, name="ettin", target_id=200, target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/farm", FarmTask(zone="/farm"),
            combat_approach=True)
        task.navigation.route = [MapEdge(
            "/test", "/well", 13, 22, kind="exit", label="stone well")]

        with patch("atrinik_bot.navigation.time.monotonic",
                   return_value=100.0):
            self.assertFalse(asyncio.run(
                task._defend_while_navigating(client)))
        # Normal NavigateThenTask.tick replans immediately after the first
        # avoidance pass. Recreate that same chosen exit for the focused
        # defense helper call.
        task.navigation.route = [MapEdge(
            "/test", "/well", 13, 22, kind="exit", label="stone well")]
        with patch("atrinik_bot.navigation.time.monotonic",
                   return_value=116.0):
            self.assertFalse(asyncio.run(
                task._defend_while_navigating(client)))

        self.assertIn(("/test", "/well"),
                      task.navigation._excluded_edges)
        self.assertEqual(task.navigation.route, [])

    def test_pack_split_retreat_forbids_immediate_backtrack(self):
        client = self._combat_client()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        calls = []

        async def retreat(client, threats, **kwargs):
            calls.append(kwargs)
            return True

        task._retreat_step = retreat
        client.state.messages = [(
            time.time(), 2, "cc66ff", "kobold chief hit you for 4 damage.")]
        self._corner(task, client)
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(calls, [{"avoid_backtrack": True}])

    def test_maintenance_travel_avoids_overlevelled_wildlife(self):
        client = self._combat_client()
        client.state.stats["level"] = 8
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 3, 0, 0, name="crocodile", target_id=300,
            target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        graph.monster_levels["crocodile"] = 14
        task = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.targets, [])
        self.assertTrue(client.direct_moves)
        self.assertIn((13, 12), task.navigation._threat_blocked)

        # Merely visible wildlife outside likely aggro range must not make the
        # character oscillate while trying to increase an already-safe gap.
        client.state.map.tiles.clear()
        client.state.map.tile(0, 0).objects[3] = MapObject(
            3, 3, 0, 0, name="crocodile", target_id=301,
            target_hp=100)
        client.combat.clear()
        client.direct_moves.clear()
        distant = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        self.assertFalse(asyncio.run(
            distant._defend_while_navigating(client)))
        self.assertEqual(client.combat, [])
        self.assertEqual(client.direct_moves, [])

        # At equal distance, danger retreat continues laterally instead of
        # undoing its previous step and cycling over the same two squares.
        client.state.map.tiles.clear()
        client.state.map.world_x = 5
        client.state.map.world_y = 8
        client.state.map.tile(6, 3).objects[3] = MapObject(
            3, 3, 0, 0, name="crocodile", target_id=302,
            target_hp=100)
        client.combat.clear()
        client.direct_moves.clear()
        plateau = NavigateThenTask(
            graph, "/shop", BuyGroundItemsTask(r"^staple food$", 5),
            combat_approach=True)
        plateau._trail = [("/test", 4, 8), ("/test", 5, 8)]
        target = client.state.map.tile(6, 3).objects[3]
        self.assertTrue(asyncio.run(plateau._retreat_step(
            client, [(5, 6, 3, target)], avoid_backtrack=True)))
        self.assertNotEqual(client.direct_moves[-1][0], 7)

    def test_authored_monster_level_resolves_live_visuals(self):
        graph = built_graph()
        # Multiple maps can author harder variants with the same visual; the
        # maintenance index intentionally uses the conservative maximum.
        self.assertGreaterEqual(graph.monster_level("crocodile"), 14)
        self.assertEqual(
            graph.monster_level("crocodile.101"),
            graph.monster_level("crocodile"))
        self.assertEqual(graph.monster_level("unknown traveler"), 0)
        self.assertGreaterEqual(graph.monster_level("treant_evil"), 68)
        self.assertLessEqual(graph.monster_level(
            "treant_evil", map_path="/shattered_islands/world_3_67"), 6)

    def test_combat_retreat_prioritizes_wider_gap_over_trail(self):
        client = self._combat_client()
        client.state.map.world_x = 12
        client.state.map.world_y = 12
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 3, 0, 0, name="kobold guard", target_id=300,
            target_hp=100)
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = type("Graph", (), {"nodes": {"/test": node}})()
        task = NavigateThenTask(
            graph, "/test", FarmTask(zone="/test"),
            combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        task._trail = [("/test", 11, 12), ("/test", 12, 12)]
        asyncio.run(task.tick(client))
        # A move that widens the minimum gap outranks the known trail.
        self.assertEqual(client.direct_moves[0][0], 8)
        client.state.map.world_x = 11
        task._record_trail(client)
        self.assertEqual(task._trail, [("/test", 11, 12)])

    def test_combat_approach_uses_one_direct_step_for_early_pull(self):
        client = self._combat_client()
        client.state.map.tile(9, 8).objects.clear()
        node = MapNode(path="/test", width=25, height=25)
        node.terrain = {(x, y): 1 for x in range(25) for y in range(25)}
        graph = WorldGraph()
        graph.nodes = {"/test": node}
        task = NavigateThenTask(
            graph, "/test", FarmTask(zone="/test"),
            combat_approach=True)
        task.status = TaskStatus.RUNNING
        task.navigation.status = TaskStatus.RUNNING
        asyncio.run(task.tick(client))
        self.assertEqual(client.direct_moves, [(3, False)])
        self.assertEqual(client.moves, [])

    def test_farm_opens_corpse_and_transfers_contents(self):
        class FakeClient:
            def __init__(self, with_contents):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats.update(
                    hp=32, maxhp=32, food=999, level=1)
                corpse = Item(80, item_type=c.TYPE_CONTAINER,
                              name="kobold corpse")
                self.state.place_item(corpse, 0)
                if with_contents:
                    self.state.place_item(
                        Item(81, name="copper coin", quantity=3), 80)
                self.applied = []
                self.moved = []
                self.commands = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def move_item(self, destination, tag, quantity=0):
                self.moved.append((destination, tag, quantity))

            async def execute_client_command(self, command):
                self.commands.append(command)

        empty = FakeClient(False)
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(empty)))
        self.assertEqual(empty.applied, [80])
        task._last_action = 0
        task._corpse_take_all[80] = (0, 0)
        self.assertTrue(asyncio.run(task.loot_nearby(empty)))
        self.assertEqual(empty.commands, ["/take all"])
        task._last_action = 0
        task._corpse_take_all[80] = (1, 0)
        self.assertFalse(asyncio.run(task.loot_nearby(empty)))
        self.assertNotIn(80, task._corpse_take_all)
        task._last_action = 0
        self.assertFalse(asyncio.run(task.loot_nearby(empty)))
        self.assertEqual(empty.commands, ["/take all"])

        # A new corpse can share the tile while the old searched corpse is
        # still in below-inventory. The new tag must be processed without the
        # completed old tag recreating its removed take timer forever.
        empty.state.place_item(
            Item(82, item_type=c.TYPE_CONTAINER, name="new kobold corpse"), 0)
        task._last_action = 0
        self.assertTrue(asyncio.run(task.loot_nearby(empty)))
        self.assertEqual(empty.applied, [80, 82])
        self.assertIn(82, task._opened_corpses)

        filled = FakeClient(True)
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(filled)))
        self.assertEqual(filled.moved, [(7, 81, 3)])

    def test_farm_skips_server_confirmed_searched_empty_corpse(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=32, food=999, level=1)
        client.state.map.path = "/test"
        client.state.map.world_x = 4
        client.state.map.world_y = 5
        client.state.place_item(Item(
            80, item_type=c.TYPE_CONTAINER,
            name="decaying corpse (bounty of Sera, searched, empty)"), 0)
        client.applied = []
        client.decisions = []

        async def apply(tag):
            client.applied.append(tag)

        client.apply = apply
        client.record_action = lambda action, detail="": (
            client.decisions.append((action, detail)))
        task = FarmTask(zone="/test")
        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        task._last_action = 0
        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.applied, [])
        self.assertEqual(client.decisions,
                         [("corpse-empty-skip", "corpse=80")])
        self.assertIn(80, task._opened_corpses)
        self.assertIn(("/test", 4, 5), task._ignored_corpse_tiles)

    def test_farm_disables_combat_before_processing_ground_corpse(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats.update(
                    hp=32, maxhp=32, food=999, level=1)
                self.state.combat = True
                self.state.place_item(
                    Item(80, item_type=c.TYPE_CONTAINER,
                         name="kobold corpse"), 0)
                self.clears = 0
                self.combat = []
                self.applied = []

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.applied, [])
        self.assertFalse(task._opened_corpses)

    def test_empty_corpse_ignore_expires_and_new_tag_on_tile_is_looted(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=32, food=999, level=1)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        client.faces = {42: "corpse_goblinoid.101"}
        client.state.map.tile(9, 8).objects[3] = MapObject(3, 42, 0, 0)
        task = FarmTask(zone="/test")
        old_tile = ("/test", 11, 10)
        task._ignored_corpse_tiles.add(old_tile)
        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        self.assertIn(old_tile, task._ignored_corpse_tiles)
        client.state.map.clear(9, 8)
        self.assertFalse(asyncio.run(task.loot_nearby(client)))
        self.assertNotIn(old_tile, task._ignored_corpse_tiles)

        current = ("/test", 10, 10)
        task._ignored_corpse_tiles.add(current)
        task._opened_corpses.add(80)
        client.state.place_item(Item(81, name="new decaying corpse"), 0)
        applied = []

        async def apply(tag):
            applied.append(tag)

        client.apply = apply
        task._last_action = 0
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertNotIn(current, task._ignored_corpse_tiles)
        self.assertEqual(applied, [81])

    def test_corpse_loot_uses_server_trap_handling_and_half_health_guard(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=40, food=999, level=1)
        client.state.place_item(Item(80, name="kobold corpse"), 0)
        client.state.place_item(
            Item(90, item_type=c.TYPE_SKILL, name="find traps"), 7)
        client.state.place_item(
            Item(91, item_type=c.TYPE_SKILL, name="remove traps"), 7)
        client.fired, client.applied = [], []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))
            if tag == 90:
                client.state.messages.append((
                    0, 0, 0, "You can't detect any trap here."))

        async def apply(tag):
            client.applied.append(tag)

        client.fire, client.apply = fire, apply
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.applied, [80])
        client.state.stats["hp"] = 19
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertFalse(task._opened_corpses)

    def test_server_managed_corpse_open_does_not_wait_for_melee_timer(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(
            hp=32, maxhp=40, food=999, level=10, action_time=2.25)
        client.state.stat_observed_at["action_time"] = time.monotonic()
        client.state.place_item(Item(80, name="quickwood corpse"), 0)
        client.state.place_item(
            Item(90, item_type=c.TYPE_SKILL, name="find traps"), 7)
        client.fired, client.applied = [], []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))

        async def apply(tag):
            client.applied.append(tag)

        client.fire, client.apply = fire, apply
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.applied, [80])

    def test_server_managed_corpse_open_ignores_full_message_history(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=40, food=999, level=10)
        client.state.place_item(Item(80, name="quickwood corpse"), 0)
        client.state.place_item(
            Item(90, item_type=c.TYPE_SKILL, name="find traps"), 7)
        client.state.messages = [
            (float(index), 0, "ffffff", f"old message {index}")
            for index in range(500)
        ]
        client.fired, client.applied = [], []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))
            client.state.add_message(2, "ffffff",
                                     "You can't detect any trap here.")

        async def apply(tag):
            client.applied.append(tag)

        client.fire, client.apply = fire, apply
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.applied, [80])

    def test_corpse_loot_never_fires_manual_trap_skills(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=40, food=999, level=10)
        client.state.place_item(Item(80, name="quickwood corpse"), 0)
        client.state.place_item(
            Item(90, item_type=c.TYPE_SKILL, name="find traps"), 7)
        client.state.place_item(
            Item(91, item_type=c.TYPE_SKILL, name="remove traps"), 7)
        client.fired, client.applied = [], []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))
            message = ("You spot a Rune of Fire (lvl 12)!" if tag == 90 else
                       "You successfully remove the Rune of Fire (lvl 12)!")
            client.state.messages.append((0, 0, 0, message))

        async def apply(tag):
            client.applied.append(tag)

        client.fire, client.apply = fire, apply
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.fired, [])
        self.assertEqual(client.applied, [80])

    def test_missing_trap_feedback_does_not_block_server_managed_open(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=40, food=999, level=10)
        client.state.map.path = "/test"
        client.state.place_item(Item(80, name="quickwood corpse"), 0)
        client.state.place_item(
            Item(90, item_type=c.TYPE_SKILL, name="find traps"), 7)
        client.fired, client.applied = [], []

        async def fire(direction, tag=0):
            client.fired.append((direction, tag))

        async def apply(tag):
            client.applied.append(tag)

        client.fire, client.apply = fire, apply
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertNotIn(80, task._unsafe_corpses)
        self.assertEqual(client.fired, [])
        self.assertEqual(client.applied, [80])

    def test_isolated_neutral_finishes_before_tail_healing(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=70, maxhp=100, target_hp=20)
        target = MapObject(
            3, 1, 0, 0, name="quickwood", target_id=80, target_hp=20)
        task = FarmTask(zone="/test", target="quickwood",
                        neutral_targets=True)
        task.safety.flee_below = 0.75
        task.safety.heal_below = 0.90
        task._engaged_target = (80, "/test", 5, 5, time.monotonic())
        threats = [(1, 9, 8, target)]
        self.assertTrue(task.should_finish_before_healing(
            client, threats, 1, 0.70))
        self.assertFalse(task.should_finish_before_healing(
            client, threats, 2, 0.70))
        self.assertFalse(task.should_finish_before_healing(
            client, threats, 1, 0.60))
        target.target_hp = 40
        client.state.stats["target_hp"] = 40
        self.assertFalse(task.should_finish_before_healing(
            client, threats, 1, 0.70))

    def test_farm_steps_directly_onto_adjacent_visible_corpse(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(hp=32, maxhp=40, food=999, level=1)
        client.state.map = MapState(
            path="/test", width=17, height=17, world_x=10, world_y=10)
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 42, 0, 0)
        client.faces = {42: "corpse_goblinoid.101"}
        client.direct_moves = []
        client.clears = 0

        async def move(direction, run=False):
            client.direct_moves.append((direction, run))

        async def clear_actions():
            client.clears += 1

        client.move = move
        client.clear_actions = clear_actions
        task = FarmTask(zone="/test")
        self.assertTrue(asyncio.run(task.loot_nearby(client)))
        self.assertEqual(client.direct_moves, [(3, False)])
        self.assertEqual(client.clears, 1)

    def test_talisman_chest_route_stages_after_incuna_gate(self):
        graph = built_graph()
        action = POLICIES["Lost Memories"].parts["The Kobolds"].action
        self.assertEqual(action.kind, "key_container")
        self.assertEqual((action.place.x, action.place.y), (19, 1))
        self.assertFalse(action.target)
        task = KeyThenContainerTask(graph, action)
        client = type("Client", (), {})()
        client.state = type("State", (), {
            "phase": "playing",
            "inventory": [Item(1, name="Western Gate Key")],
            "map": MapState(
                path="/shattered_islands/world_4_85",
                world_x=7, world_y=13),
        })()
        asyncio.run(task.tick(client))
        self.assertIsInstance(task.child, NavigateTask)
        self.assertEqual(
            task.child.destination, KeyThenContainerTask.TUNNEL_STAGING_MAP)
        self.assertTrue(task.child._allow_locked_override)
        self.assertTrue(graph.route_points(
            task.child.destination, KeyThenContainerTask.TUNNEL_STAGING_XY,
            action.place.map_path, [(action.place.x, action.place.y)],
            allow_locked=False))

    def test_talisman_action_opens_chest_instead_of_farming_chief(self):
        graph = built_graph()
        action = POLICIES["Lost Memories"].parts["The Kobolds"].action
        task = KeyThenContainerTask(graph, action)
        client = type("Client", (), {})()
        client.state = type("State", (), {
            "phase": "playing",
            "inventory": [Item(1, name="Western Gate Key")],
            "map": MapState(
                path=action.place.map_path,
                world_x=action.place.x, world_y=action.place.y),
        })()
        asyncio.run(task.tick(client))
        self.assertIsInstance(task.child, NavigateThenTask)
        self.assertIsInstance(task.child.task, AcquireContainerItemTask)
        self.assertEqual(task.child.navigation.destination_xy, (19, 1))
        self.assertTrue(task.child.combat_approach)

    def test_container_acquisition_opens_chest_and_takes_talisman(self):
        graph = built_graph()
        place = POLICIES["Lost Memories"].parts["The Kobolds"].action.place
        task = AcquireContainerItemTask(graph, place, "Blue Crystal Talisman")
        task.navigation.status = TaskStatus.COMPLETE
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path=place.map_path, world_x=place.x, world_y=place.y)
        client.state.place_item(
            Item(80, item_type=c.TYPE_CONTAINER, name="chest"), 0)
        client.applied, client.moved = [], []

        async def apply(tag):
            client.applied.append(tag)

        async def move_item(destination, tag, quantity=0):
            client.moved.append((destination, tag, quantity))

        client.apply, client.move_item = apply, move_item
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [80])
        client.state.place_item(
            Item(81, name="Blue Crystal Talisman"), 80)
        task.last_action = 0
        asyncio.run(task.tick(client))
        self.assertEqual(client.moved, [(7, 81, 1)])

    def test_clearhaven_container_sweep_advances_one_bomb_at_a_time(self):
        action = POLICIES["Clearhaven Mine"].parts[
            "A Miner Supply Problem"].action
        task = AcquireContainerItemsTask(WorldGraph(), action)
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)

        asyncio.run(task.tick(client))
        self.assertIsInstance(task.child, AcquireContainerItemTask)
        self.assertEqual(task.child.navigation.destination_xy, action.patrol[0])
        self.assertEqual(task.child.required_quantity, 1)

        client.state.place_item(Item(90, name="small bomb"), 7)
        asyncio.run(task.tick(client))
        self.assertIsNone(task.child)
        self.assertEqual(task.index, 1)
        asyncio.run(task.tick(client))
        self.assertEqual(task.child.navigation.destination_xy, action.patrol[1])
        self.assertEqual(task.child.required_quantity, 2)

        client.state.items[90].quantity = action.quantity
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_lairwenn_acquisition_opens_typeless_authored_luggage(self):
        graph = built_graph()
        action = POLICIES["Lairwenn's Notes"].parts[
            "Finding the Notes"].action
        task = AcquireContainerItemTask(
            graph, action.place, action.item, action.object_name)
        task.navigation.status = TaskStatus.COMPLETE
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path=action.place.map_path,
            world_x=action.place.x, world_y=action.place.y)
        client.state.place_item(Item(80, name="big box"), 0)
        client.state.place_item(Item(81, name="iron luggage"), 0)
        client.applied = []

        async def apply(tag):
            client.applied.append(tag)

        client.apply = apply
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [81])

    def test_quest_farm_enters_map_without_pathing_onto_spawn(self):
        policy = POLICIES["Melanye's Lost Walking Stick"]
        task = CatalogQuestTask(built_graph(), policy)
        action = policy.parts["The Stick"].action
        child = task._action_task(action, "The Stick")
        self.assertIsInstance(child, NavigateThenTask)
        self.assertIsNone(child.navigation.destination_xy)
        self.assertEqual(child.task.priority_spawns,
                         [(10, 14, "evil treant")])

    def test_dialog_handoff_disarms_before_talking_to_npc(self):
        client = self._combat_client()
        task = DialogAtTask(
            built_graph(), NPC["Melanye"], "Melanye", ())
        task.navigation.complete()
        asyncio.run(task.tick(client))
        self.assertEqual(client.combat, [False])
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.cleared_targets, 1)
        self.assertFalse(task.dialog._sent_hello)

    def test_calming_tonic_is_usable_at_safe_basement_entrance(self):
        graph = built_graph()
        task = ApplyAtTask(
            graph,
            POLICIES["Lost Memories"].parts["Making Friends"].action.place,
            "Angela's calming tonic",
        )
        client = type("Client", (), {})()
        client.state = type("State", (), {})()
        client.state.map = type("Map", (), {
            "path": "/shattered_islands/world_4_85_-2",
            "world_x": 0, "world_y": 13,
        })()
        self.assertIsNone(task.navigation.destination_xy)
        self.assertTrue(task._requirements_met(client))

    def test_lost_memories_main_line_apartment_policy(self):
        policy = POLICIES["Lost Memories"]
        self.assertEqual(
            policy.parts["Speak with Sam Goodberry"].action.choices,
            (r"=:remember\]",),
        )
        action = policy.parts["Your First Apartment"].action
        self.assertEqual(action.kind, "apartment_tutorial")
        task = CatalogQuestTask(built_graph(), policy)._action_task(
            action, "Your First Apartment")
        self.assertIsInstance(task, IncunaApartmentTutorialTask)

    def test_incuna_apartment_tutorial_enters_hammock_and_applies(self):
        action = POLICIES["Lost Memories"].parts[
            "Your First Apartment"].action
        task = IncunaApartmentTutorialTask(built_graph(), action)
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path=(IncunaApartmentTutorialTask.APARTMENT_MAP + "/Sera"),
            world_x=11, world_y=12, player_x=8, player_y=8,
        )
        client.moves, client.applied = [], []

        async def move_to_view(x, y):
            client.moves.append((x, y))

        async def apply(tag):
            client.applied.append(tag)

        client.move_to_view, client.apply = move_to_view, apply
        asyncio.run(task.tick(client))
        self.assertEqual(client.moves, [(8, 7)])
        client.state.place_item(Item(90, name="hammock to reality"), 0)
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [90])
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_quest_list(self):
        data = (
            b"[book]Quest List[/book][title]Incomplete quests:[/title]\n"
            b"[title]Example[/title]\n[b]Kill Things[/b]: Go fight\n"
            b"[x=10]Status: 2/5\n[p]\n[title]Completed quests:[/title]\n"
            b"[title]Finished[/title]"
        )
        quests = parse_quest_book(data)
        self.assertEqual(quests["Example"].parts[0].current, 2)
        self.assertEqual(quests["Finished"].status, "completed")

    def test_book_quest_payload_loads_parser_lazily(self):
        data = b"[book]Quest List[/book][title]Incomplete quests:[/title]"
        expected = {"Example": QuestProgress(name="Example")}

        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                client = AtrinikClient(ClientConfig(
                    runtime_state_path=str(Path(directory) / "state.sqlite3")))
                with patch("atrinik_bot.quests.parse_quest_book",
                           return_value=expected) as parser:
                    await client._handle_book(Cursor(data), data)
                parser.assert_called_once_with(data)
                self.assertIs(client.state.quests, expected)
                self.assertTrue(client.state.quests_loaded)

        asyncio.run(scenario())

    def test_every_formal_quest_has_an_executor(self):
        catalog = load_catalog()
        self.assertEqual(
            set(POLICIES), set(catalog) - {"Escaping the Deserted Island"})
        self.assertEqual(set(RECOMMENDED_QUEST_ORDER), set(catalog))
        for name, policy in POLICIES.items():
            authored = {part.name for part in flatten_parts(catalog[name].parts)}
            self.assertFalse(
                authored - set(policy.parts),
                f"{name} lacks policies for {authored - set(policy.parts)}",
            )

    def test_policy_dialog_paths_are_safe(self):
        catalog = load_catalog()
        for name, policy in POLICIES.items():
            definition = catalog[name]
            for part_name, part in policy.parts.items():
                action = part.action
                if action.kind != "dialog" or action.choices:
                    continue
                authored = next(p for p in flatten_parts(definition.parts)
                                if p.name == part_name)
                goal = action.goal_action or (
                    "start" if action.object_arch or action.object_name
                    else "complete")
                # Empty is legitimate for a terminal hello dialog. Compiled
                # non-empty paths are validated as response-link regexes.
                choices = dialogue_choices(
                    definition, action.npc, action=goal,
                    uid=action.goal_uid or authored.uid,
                    object_arch=action.object_arch,
                    object_name=action.object_name,
                )
                self.assertTrue(all(choice.startswith("=:") for choice in choices))

    def test_ordinary_foothills_legs_patrol_authored_spawn_squares(self):
        graph = built_graph()
        circuit = FarmCircuitTask(graph, [
            ("/shattered_islands/world_7_57", "hill giant|ogre"),
            ("/shattered_islands/world_7_56", "hill giant|ogre"),
        ])
        first = circuit._new_child().task
        first_spawns = {(7, 12), (16, 9), (17, 0)}
        self.assertFalse(first_spawns & set(first.patrol))
        self.assertTrue(all(any(max(abs(x - sx), abs(y - sy)) == 1
                                for x, y in first.patrol)
                            for sx, sy in first_spawns))
        circuit.leg_index = 1
        second = circuit._new_child().task
        second_spawns = {(9, 9), (13, 17)}
        self.assertFalse(second_spawns & set(second.patrol))
        self.assertTrue(all(any(max(abs(x - sx), abs(y - sy)) == 1
                                for x, y in second.patrol)
                            for sx, sy in second_spawns))

    def test_wasp_leg_patrols_only_exact_neutral_spawn_neighborhoods(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(EARLY_LEGS))
        circuit.leg_index = 2
        wasps = circuit._new_child().task
        self.assertTrue(wasps.neutral_targets)
        expected = {(spawn.x, spawn.y) for spawn in graph.farm_priorities(
            "/shattered_islands/world_3_69", "giant wasp|wasp_giant|wasp giant")}
        self.assertFalse(expected & set(wasps.patrol))
        self.assertTrue(all(any(max(abs(x - sx), abs(y - sy)) == 1
                                for x, y in wasps.patrol)
                            for sx, sy in expected))
        self.assertTrue(all(
            min(max(abs(x - sx), abs(y - sy)) for sx, sy in expected) <= 4
            for x, y in wasps.patrol))
        self.assertNotIn((4, 2), wasps.patrol)

    def test_day_killer_bee_supplement_is_neutral_and_exactly_patrolled(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(EARLY_LEGS))
        circuit.leg_index = 3
        bees = circuit._new_child().task
        expected = {(spawn.x, spawn.y) for spawn in graph.farm_priorities(
            "/shattered_islands/world_4_68",
            "killer bee|bee_killer|bee killer")}
        self.assertEqual(expected, {(7, 9), (8, 8), (8, 9)})
        self.assertTrue(bees.neutral_targets)
        self.assertTrue(all(any(max(abs(x - sx), abs(y - sy)) == 1
                                for x, y in bees.patrol)
                            for sx, sy in expected))

    def test_alternate_tree_leg_has_six_isolated_neutral_spawns(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(EARLY_LEGS))
        circuit.leg_index = 4
        trees = circuit._new_child().task
        expected = graph.farm_priorities(
            "/shattered_islands/world_10_78",
            "evil treant|quickwood")
        self.assertEqual(len(expected), 6)
        self.assertEqual({spawn.level for spawn in expected}, {6, 7})
        self.assertEqual([spawn.level for spawn in expected],
                         [7, 7, 7, 6, 6, 6])
        self.assertTrue(trees.neutral_targets)
        self.assertEqual(len(trees.priority_spawns), 6)
        self.assertTrue(all(any(
            max(abs(x - spawn.x), abs(y - spawn.y)) == 1
            for x, y in trees.patrol) for spawn in expected))

    def test_named_spawn_patrol_uses_observation_squares(self):
        circuit = FarmCircuitTask(
            built_graph(), list(EARLY_LEGS))
        circuit.leg_index = 1
        fahrgorm = circuit._new_child().task
        self.assertTrue(fahrgorm.neutral_targets)
        self.assertNotIn((12, 11), fahrgorm.patrol)
        self.assertTrue(any(max(abs(x - 12), abs(y - 11)) == 1
                            for x, y in fahrgorm.patrol))

    def test_failed_circuit_route_retreats_instead_of_ending_task(self):
        client = self._combat_client()
        client.state.map.path = "/danger"
        client.state.map.world_x = 10
        client.state.map.world_y = 10
        client.state.map.tiles.clear()
        client.state.map.tile(9, 8).objects[3] = MapObject(
            3, 1, 0, 0, name="stone giant", target_id=100)
        client.state.map.tile(8, 9).objects[3] = MapObject(
            3, 1, 0, 0, name="ettin", target_id=200)
        client.state.messages = [(
            time.time(), 2, "cc66ff",
            "stone giant hit you for 18 damage.")]
        client.state.combat = True
        graph = WorldGraph()
        danger = MapNode(path="/danger", width=25, height=25)
        danger.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        safe = MapNode(path="/safe", width=25, height=25)
        safe.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)}
        graph.nodes.update({"/danger": danger, "/safe": safe})
        circuit = FarmCircuitTask(graph, [("/safe", "tree")])
        farm = FarmTask(zone="/safe", target="tree")
        circuit.child = NavigateThenTask(
            graph, "/safe", farm, combat_approach=True)
        circuit.child.navigation.status = TaskStatus.FAILED
        circuit.child.navigation.error = "no component route"

        recovered = asyncio.run(
            circuit._recover_failed_navigation(client, farm))

        self.assertTrue(recovered)
        self.assertEqual(circuit.child.navigation.status, TaskStatus.RUNNING)
        self.assertEqual(circuit.status, TaskStatus.READY)
        self.assertTrue(client.direct_moves)
        self.assertEqual(client.combat[-1], False)

    def test_level_eighteen_return_retries_only_after_safe_route_fails(self):
        client = self._combat_client()
        client.state.stats["level"] = 20
        client.decisions = []
        client.record_action = lambda action, detail="": (
            client.decisions.append((action, detail)))
        graph = WorldGraph()
        graph.nodes["/farm"] = MapNode("/farm", width=2, height=2)
        circuit = FarmCircuitTask(graph, [("/farm", "tree")])
        circuit.child = circuit._new_child()
        navigation = circuit.child.navigation
        navigation.status = TaskStatus.FAILED
        navigation.error = "no component route from /town to /farm []"

        self.assertTrue(circuit._retry_unavoidable_return_route(client))
        self.assertEqual(navigation.status, TaskStatus.READY)
        self.assertTrue(navigation.allow_ranged_hazard_fallback)
        self.assertEqual(client.decisions[-1][0],
                         "farm-return-threat-fallback")

        navigation.status = TaskStatus.FAILED
        navigation.error = "movement stalled"
        navigation.allow_ranged_hazard_fallback = False
        self.assertFalse(circuit._retry_unavoidable_return_route(client))

    def test_autoplay_completed_intro_enters_adaptive_farming(self):
        client = self._combat_client()
        client.state.stats.update({"level": 18, "exp": 6_000_000})
        client.state.quests_loaded = True
        client.state.quests = {
            EscapingDesertedIslandTask.QUEST: QuestProgress(
                EscapingDesertedIslandTask.QUEST, status="completed"),
            AutoplayTask.MANA_CRYSTAL_QUEST: QuestProgress(
                AutoplayTask.MANA_CRYSTAL_QUEST, status="incomplete")}
        crystal = Item(
            99, item_type=c.TYPE_POWER_CRYSTAL,
            name="Gandyld's Mana Crystal")
        client.state.inventory.append(crystal)
        client.state.items[crystal.tag] = crystal
        autoplay = AutoplayTask(built_graph(), target_level=115)
        client.farm_analyzer = StubAnalyzer({"xp": [analyzer_spot(
            "Eld Woods trees", ["/shattered_islands/world_14_79"],
            ["evil treant L13×2", "quickwood L11"])]})

        asyncio.run(autoplay.tick(client))
        self.assertIsInstance(
            autoplay.child, EscapingDesertedIslandTask)
        asyncio.run(autoplay.tick(client))
        self.assertEqual(autoplay.phase, "adaptive-farming")
        self.assertIsInstance(autoplay.child, FarmCircuitTask)
        self.assertTrue(autoplay.child.analyzer_driven)
        self.assertEqual(autoplay.child.legs, (
            ("/shattered_islands/world_14_79", "evil treant|quickwood"),))
        self.assertEqual(autoplay.child.spot["name"], "Eld Woods trees")
        self.assertEqual(autoplay.child.level_until, 115)

    def test_autoplay_waits_safely_until_the_analyzer_has_a_spot(self):
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({"level": 22, "hp": 100, "maxhp": 100})
        autoplay = AutoplayTask(built_graph(), target_level=115)
        autoplay.status = TaskStatus.RUNNING
        autoplay._intro_complete = True
        holds = []

        class Hold:
            async def enforce(self, client):
                holds.append(client.state.map.path)
                return True

        autoplay.safety = Hold()
        with patch.object(autoplay, "_needs_mana_crystal_start", return_value=False), \
                patch.object(autoplay, "_needs_mana_crystal_recovery", return_value=False):
            for _ in range(3):
                asyncio.run(autoplay.tick(client))
            self.assertIsNone(autoplay.child)
            self.assertEqual(len(holds), 3)
            waits = [record for record in client.decision_history
                     if record["action"] == "awaiting-farm-ranking"]
            self.assertEqual(len(waits), 1)
            # Every ranked spot quarantined is the same as no spot.
            spot = analyzer_spot("Trees", ["/shattered_islands/world_14_79"],
                                 ["evil treant L13"])
            client.farm_analyzer = StubAnalyzer({"xp": [spot]})
            client.state.farm_zone_quarantine = {
                "/shattered_islands/world_14_79": time.time() + 60}
            asyncio.run(autoplay.tick(client))
            self.assertIsNone(autoplay.child)
            client.state.farm_zone_quarantine = {}
            asyncio.run(autoplay.tick(client))
        self.assertEqual(autoplay.phase, "adaptive-farming")
        self.assertEqual(autoplay.child.legs[0][0], "/shattered_islands/world_14_79")

    def test_autoplay_pauses_any_phase_to_eat_at_a_temple_when_starving(self):
        from . import autoplay as autoplay_module
        from .tasks import BotTask

        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({"level": 23, "hp": 100, "maxhp": 100, "food": 500})
        sent = []

        async def send(packet):
            sent.append(packet)

        client.send = send
        graph = WorldGraph()
        autoplay = AutoplayTask(graph, target_level=115)
        autoplay.status = TaskStatus.RUNNING
        autoplay._intro_complete = True

        class Walk(BotTask):
            ticks = 0

            async def tick(self, client):
                Walk.ticks += 1

        walk = Walk("navigate-then:retrieve:chest")
        walk.status = TaskStatus.RUNNING
        autoplay.phase, autoplay.child = "mana-crystal-recovery", walk
        autoplay._mana_recovery_attempted = True
        trips = []

        def trip(graph, client):
            trips.append(client.state.stats["food"])
            return NavigateThenTask(graph, "/temple", TempleFoodTask("Saruthar"))

        with patch.object(autoplay_module, "temple_food_trip", trip):
            asyncio.run(autoplay.tick(client))
            self.assertEqual((Walk.ticks, trips), (1, []))
            # Starving with nothing to eat: the walk pauses for the temple.
            client.state.stats["food"] = 100
            asyncio.run(autoplay.tick(client))
            self.assertEqual((Walk.ticks, trips), (1, [100]))
            self.assertEqual(autoplay.phase, "temple-food")
            self.assertIsInstance(autoplay.child.task, TempleFoodTask)
            # The interrupted recovery runs again once she has eaten.
            self.assertFalse(autoplay._mana_recovery_attempted)
            hunger = [record["detail"] for record in client.decision_history
                      if record["action"] == "autoplay-hunger"]
            self.assertEqual(hunger, ["food 100; paused mana-crystal-recovery: "
                                      "navigate-then:retrieve:chest"])
            # A failed trip waits before the next one, then tries again even
            # with nothing else to schedule.
            autoplay.child.fail("no route")
            asyncio.run(autoplay.tick(client))
            self.assertIsNone(autoplay.child)
            self.assertEqual(autoplay.phase, "temple-food")
            self.assertFalse(autoplay._needs_temple_food(client))
            autoplay._temple_food_retry_at = 0.0
            self.assertTrue(autoplay._needs_temple_food(client))
            # The farm circuit feeds her itself, unless a maintenance trip
            # (here, storage) keeps its food resupply from running.
            autoplay.phase = "adaptive-farming"
            circuit = FarmCircuitTask(graph, [("/a", "rat")])
            autoplay.child = circuit
            self.assertFalse(autoplay._needs_temple_food(client))
            circuit._storage = Walk("navigate-then:storage")
            self.assertTrue(autoplay._needs_temple_food(client))
            # A hostile-route abort retracing to safety finishes first.
            circuit._expedition_return = Walk("navigate:origin")
            self.assertFalse(autoplay._needs_temple_food(client))
            circuit._expedition_return = None
            # A starter handing over Gandyld's crystal is left to finish.
            autoplay.phase, autoplay.child = "mana-crystal-starter", walk
            self.assertTrue(autoplay._needs_temple_food(client))
            client.state.quests = {AutoplayTask.MANA_CRYSTAL_QUEST: QuestProgress(
                AutoplayTask.MANA_CRYSTAL_QUEST, status="incomplete")}
            self.assertFalse(autoplay._needs_temple_food(client))
            client.state.quests = {}
            # Until the inventory replay lands, her food is unknown.
            client.state.inventory_replay_complete = False
            self.assertFalse(autoplay._needs_temple_food(client))
            client.state.inventory_replay_complete = True
        # No reachable priest: keep going, and look again later.
        autoplay.phase, autoplay.child = "mana-crystal-recovery", walk
        with patch.object(autoplay_module, "temple_food_trip", lambda graph, client: None):
            self.assertTrue(autoplay._needs_temple_food(client))
            self.assertFalse(asyncio.run(autoplay._start_temple_food(client)))
            self.assertIs(autoplay.child, walk)
            self.assertFalse(autoplay._needs_temple_food(client))

    def test_autoplay_recovers_started_mana_crystal_from_apartment(self):
        client = self._combat_client()
        client.state.stats.update({"level": 18, "exp": 6_000_000})
        client.state.quests_loaded = True
        client.state.quests = {
            EscapingDesertedIslandTask.QUEST: QuestProgress(
                EscapingDesertedIslandTask.QUEST, status="completed"),
            AutoplayTask.MANA_CRYSTAL_QUEST: QuestProgress(
                AutoplayTask.MANA_CRYSTAL_QUEST, status="incomplete")}
        autoplay = AutoplayTask(built_graph(), target_level=115)

        asyncio.run(autoplay.tick(client))
        asyncio.run(autoplay.tick(client))

        self.assertEqual(autoplay.phase, "mana-crystal-recovery")
        self.assertIsInstance(autoplay.child, NavigateThenTask)
        self.assertIsInstance(autoplay.child.task, RetrieveItemsTask)
        self.assertTrue(autoplay.child.task.require_match)
        self.assertTrue(autoplay._mana_recovery_attempted)

    def test_autoplay_backs_off_persisted_mana_recovery_failure(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 18
        client.state.quests_loaded = True
        client.state.quests["Gandyld's Mana Crystal"] = QuestProgress(
            "Gandyld's Mana Crystal", "active")
        client.decision_history = [{
            "time": time.time(), "action": "autoplay-retry",
            "detail": "mana-crystal-recovery: contents did not load",
        }]
        autoplay = AutoplayTask(WorldGraph())

        self.assertFalse(autoplay._needs_mana_crystal_recovery(client))

    def test_power_crystal_is_never_apartment_trophy(self):
        crystal = Item(
            99, flags=c.ITEM_MAGICAL, item_type=c.TYPE_POWER_CRYSTAL,
            quality=100, name="Gandyld's Mana Crystal")

        self.assertFalse(InventoryPolicy().apartment_valuable(crystal))

    def test_autoplay_collects_gandyld_opening_reward_at_level_18(self):
        client = self._combat_client()
        client.state.stats.update({"level": 18, "exp": 6_000_000})
        client.state.quests_loaded = True
        client.state.quests = {
            EscapingDesertedIslandTask.QUEST: QuestProgress(
                EscapingDesertedIslandTask.QUEST, status="completed")}
        autoplay = AutoplayTask(built_graph(), target_level=115)

        asyncio.run(autoplay.tick(client))
        asyncio.run(autoplay.tick(client))

        self.assertEqual(autoplay.phase, "mana-crystal-starter")
        self.assertIsInstance(autoplay.child, CatalogQuestTask)
        self.assertTrue(autoplay.child.stop_after_start)
        self.assertIsNotNone(autoplay.child.start_reward_pattern)

    def test_start_only_catalog_quest_stops_before_dangerous_part(self):
        client = self._combat_client()
        client.state.quests_loaded = True
        client.state.quests = {
            AutoplayTask.MANA_CRYSTAL_QUEST: QuestProgress(
                AutoplayTask.MANA_CRYSTAL_QUEST, status="incomplete")}
        task = CatalogQuestTask(
            built_graph(), POLICIES[AutoplayTask.MANA_CRYSTAL_QUEST],
            stop_after_start=True)

        asyncio.run(task.tick(client))

        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertIsNone(task.child)

    def test_start_only_quest_waits_for_required_interface_reward(self):
        client = self._combat_client()
        client.state.quests_loaded = True
        client.state.quests = {
            AutoplayTask.MANA_CRYSTAL_QUEST: QuestProgress(
                AutoplayTask.MANA_CRYSTAL_QUEST, status="incomplete")}
        task = CatalogQuestTask(
            built_graph(), POLICIES[AutoplayTask.MANA_CRYSTAL_QUEST],
            stop_after_start=True,
            start_reward_pattern=r"Gandyld's Mana Crystal")

        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.interface = InterfaceState(
            title="Gandyld", objects=[Item(
                99, item_type=c.TYPE_POWER_CRYSTAL,
                name="Gandyld's Mana Crystal")])
        asyncio.run(task.tick(client))

        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_autoplay_fresh_character_begins_resumable_intro(self):
        client = self._combat_client()
        client.state.stats.update({"level": 1, "exp": 0})
        client.state.quests_loaded = False
        client.state.quests = {}
        autoplay = AutoplayTask(built_graph(), target_level=115)

        asyncio.run(autoplay.tick(client))
        self.assertEqual(autoplay.phase, "intro-quest")
        self.assertIsInstance(
            autoplay.child, EscapingDesertedIslandTask)
        self.assertEqual(autoplay.status, TaskStatus.RUNNING)

    def test_autoplay_attributes_transit_death_to_farm_destination(self):
        client = AtrinikClient(ClientConfig())
        graph = WorldGraph()
        start = MapNode(path="/road", width=3, height=3)
        destination = MapNode(path="/farm", width=3, height=3)
        start.edges.append(MapEdge("/road", "/farm", 2, 1))
        graph.nodes.update({"/road": start, "/farm": destination})
        circuit = FarmCircuitTask(graph, [("/farm", "orc")])
        circuit.child = circuit._new_child()
        circuit.child.navigation.status = TaskStatus.RUNNING
        client.decision_history.append({
            "time": time.time(), "action": "death", "detail": "died",
            "task": circuit.name, "map": "/road", "x": 1, "y": 1,
        })

        self.assertTrue(circuit._observe_farm_death(client))
        self.assertGreater(
            client.state.farm_zone_quarantine["/farm"], time.time())

    def test_adaptive_circuit_periodically_trains_wizardry(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update({"level": 18, "hp": 178, "maxhp": 178})
        wizardry = Item(1, item_type=c.TYPE_SKILL,
                        name="wizardry spells")
        wizardry.extra["level"] = 5
        bullet = Item(2, item_type=c.TYPE_SPELL, name="magic bullet")
        client.state.place_item(wizardry, 7)
        client.state.place_item(bullet, 7)
        circuit = FarmCircuitTask(
            built_graph(), list(EARLY_LEGS), analyzer_driven=True)
        circuit.child = circuit._new_child()
        farm = circuit.child.task

        self.assertTrue(circuit._review_combat_build(client, farm))
        self.assertEqual(
            (circuit.combat_skill, circuit.combat_spell,
             circuit.combat_skill_until_level),
            ("wizardry spells", "magic bullet", 10))
        self.assertEqual(circuit.child.task.combat_skill,
                         "wizardry spells")
        self.assertIs(circuit.child.task._lore_book_attempts,
                      circuit._lore_book_attempts)

        wizardry.extra["level"] = 10
        self.assertTrue(circuit._review_combat_build(
            client, circuit.child.task))
        self.assertEqual(
            (circuit.combat_skill, circuit.combat_spell,
             circuit.combat_skill_until_level), ("", "", 0))

        client.state.stats["level"] = 25
        self.assertTrue(circuit._review_combat_build(
            client, circuit.child.task))
        self.assertEqual(circuit.combat_skill_until_level, 15)

    def test_explicit_combat_training_disables_automatic_build_review(self):
        circuit = FarmCircuitTask(
            built_graph(), list(EARLY_LEGS), analyzer_driven=True,
            combat_skill="bow archery", combat_skill_until_level=5)
        self.assertFalse(circuit._auto_combat_build)

    def test_progression_waits_for_engaged_target_then_rebuilds_circuit(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 11
        client.farm_analyzer = StubAnalyzer({"xp": [analyzer_spot(
            "Eld Woods trees", ["/shattered_islands/world_14_79"],
            ["evil treant L13×2"])]})
        circuit = FarmCircuitTask(built_graph(), list(EARLY_LEGS),
                                  analyzer_driven=True)
        circuit.child = circuit._new_child()
        farm = circuit.child.task
        farm._engaged_target = (99, "/farm", 1, 1, time.monotonic())
        self.assertFalse(circuit._upgrade_progression(client, farm))
        farm._engaged_target = None
        self.assertTrue(circuit._upgrade_progression(client, farm))
        self.assertIn("world_14_79", circuit.name)
        self.assertEqual(circuit.legs,
                         (("/shattered_islands/world_14_79", "evil treant"),))

    def test_off_map_maintenance_return_resets_farm_dwell(self):
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)
        circuit.child = circuit._new_child()
        circuit._farm_started_at = 10.0
        circuit._empty_spawn_since = 20.0
        self.assertTrue(circuit._reset_dwell_off_farm("/shop"))
        self.assertEqual(circuit._farm_started_at, 0.0)
        self.assertEqual(circuit._empty_spawn_since, 0.0)
        circuit._farm_started_at = 30.0
        self.assertFalse(circuit._reset_dwell_off_farm("/farm"))
        self.assertEqual(circuit._farm_started_at, 30.0)

    def test_server_game_clock_parses_advances_and_resyncs_sparsely(self):
        now = [100.0]
        clock = ServerGameClock(clock=lambda: now[0])
        self.assertEqual(clock.parse(
            "It is night, 5 minutes past 12 o'clock am, on the Monday."), 5)
        self.assertEqual(clock.parse(
            "It is day, 7 minutes past 12 o'clock pm, on the Monday."), 727)

        class Client:
            def __init__(self):
                self.state = GameState(phase="playing")
                self.commands = []

            async def execute_client_command(self, command):
                self.commands.append(command)

        client = Client()
        self.assertTrue(asyncio.run(clock.sync(client)))
        self.assertEqual(client.commands, ["/time"])
        client.state.messages.append((
            0, 0, 0,
            "It is night, 30 minutes past 6 o'clock pm, on the Monday."))
        now[0] += 1
        self.assertFalse(asyncio.run(clock.sync(client)))
        self.assertAlmostEqual(clock.game_minute(), 18 * 60 + 30)
        now[0] += 31.25
        self.assertAlmostEqual(clock.game_minute(), 18 * 60 + 40)
        self.assertFalse(asyncio.run(clock.sync(client)))
        now[0] += 570
        self.assertTrue(asyncio.run(clock.sync(client)))
        self.assertEqual(client.commands, ["/time", "/time"])

    def test_daytime_skips_night_only_named_farm_leg(self):
        graph = WorldGraph()
        for path in ("/souls", "/trees"):
            node = MapNode(path, width=10, height=10)
            node.terrain = {(x, y): 1 for x in range(10) for y in range(10)}
            graph.nodes[path] = node
        graph.named_spawns["/souls"] = [NamedSpawn(
            "/souls", 4, 2, "Thrakir", ("lost soul", "Thrakir"),
            19 * 60, 7 * 60)]
        circuit = FarmCircuitTask(
            graph, [("/souls", "Thrakir|lost soul"),
                    ("/trees", "Fahrgorm|evil treant")],
            clock=lambda: 10.0)
        circuit.server_clock.anchor_minute = 12 * 60
        circuit.server_clock.anchor_at = 10.0
        self.assertFalse(circuit._leg_available(0))
        self.assertTrue(circuit._leg_available(1))
        self.assertEqual(circuit._next_available_leg(0), 1)
        circuit.server_clock.anchor_minute = 20 * 60
        self.assertTrue(circuit._leg_available(0))
        self.assertEqual(circuit._next_available_leg(0), 0)

    def test_graveyard_leg_patrols_all_spawns_and_skips_near_dawn(self):
        now = [100.0]
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(EARLY_LEGS) + [
                ("/shattered_islands/world_1_58", "skeleton")],
            clock=lambda: now[0])
        graveyard_index = len(EARLY_LEGS)
        circuit.leg_index = graveyard_index
        graveyard = circuit._new_child().task
        expected = {(spawn.x, spawn.y) for spawn in graph.farm_priorities(
            "/shattered_islands/world_1_58", "skeleton")}
        self.assertEqual(len(expected), 13)
        self.assertFalse(expected & set(graveyard.patrol))
        self.assertTrue(all(any(max(abs(x - sx), abs(y - sy)) == 1
                                for x, y in graveyard.patrol)
                            for sx, sy in expected))

        circuit.server_clock.anchor_at = now[0]
        circuit.server_clock.anchor_minute = 6 * 60 + 30
        self.assertTrue(circuit._leg_available(graveyard_index))
        circuit.server_clock.anchor_minute = 6 * 60 + 53
        self.assertFalse(circuit._leg_available(graveyard_index))
        circuit.server_clock.anchor_minute = 19 * 60
        self.assertTrue(circuit._leg_available(graveyard_index))

    def test_rare_detour_check_has_persisted_thirty_minute_cooldown(self):
        circuit = FarmCircuitTask(
            built_graph(), list(LOST_SOUL_LEGS + MUD_HAND_LEGS))
        path = LOST_SOUL_LEGS[0][0]
        circuit._farm_zone_last_checked[path] = time.time()
        self.assertFalse(circuit._leg_ready(0, 18))
        self.assertEqual(circuit._next_ready_leg(0, 18), 1)
        circuit._farm_zone_last_checked[path] -= (
            circuit.OPTIONAL_RARE_DETOUR_INTERVAL + 1)
        self.assertTrue(circuit._leg_ready(0, 18))

    def test_unreachable_target_cooldown_survives_circuit_leg_change(self):
        circuit = FarmCircuitTask(
            built_graph(), list(LOST_SOUL_LEGS + MUD_HAND_LEGS))
        circuit._unreachable_targets[100] = time.monotonic() + 60
        first = circuit._new_child().task
        circuit.leg_index = 1
        second = circuit._new_child().task
        self.assertIs(first._unreachable_targets,
                      circuit._unreachable_targets)
        self.assertIs(second._unreachable_targets,
                      circuit._unreachable_targets)
        self.assertTrue(second.target_temporarily_unreachable(100))

    def test_daytime_empty_named_spawn_preserves_only_available_farm(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.faces = {}

            async def clear_actions(self):
                pass

            async def set_combat(self, enabled, force=False):
                pass

            async def move_to_view(self, x, y):
                pass

        async def run():
            client = FakeClient()
            client.state.phase = "playing"
            client.state.map = MapState(path="/trees", width=10, height=10,
                                        world_x=5, world_y=5)
            client.state.stats.update(level=6, hp=80, maxhp=80, food=500)
            graph = WorldGraph()
            for path in ("/souls", "/trees"):
                node = MapNode(path, width=10, height=10)
                node.terrain = {
                    (x, y): 1 for x in range(10) for y in range(10)
                }
                graph.nodes[path] = node
            graph.named_spawns["/souls"] = [NamedSpawn(
                "/souls", 4, 2, "Thrakir", ("lost soul", "Thrakir"),
                19 * 60, 7 * 60)]
            graph.named_spawns["/trees"] = [NamedSpawn(
                "/trees", 4, 4, "Fahrgorm", ("evil treant", "Fahrgorm"))]
            now = [100.0]
            circuit = FarmCircuitTask(
                graph, [("/souls", "Thrakir|lost soul"),
                        ("/trees", "Fahrgorm|evil treant")],
                clock=lambda: now[0])
            circuit.server_clock.anchor_minute = 12 * 60
            circuit.server_clock.anchor_at = now[0]
            circuit.leg_index = 1
            circuit.child = circuit._new_child()
            circuit.child.navigation.complete()
            circuit.child.status = TaskStatus.RUNNING
            circuit.child.task.status = TaskStatus.RUNNING
            circuit.child.task._visible_target_count = 0
            circuit.child.task._ignored_corpse_tiles.add(("/trees", 5, 5))
            circuit.status = TaskStatus.RUNNING
            circuit._farm_started_at = now[0] - 20
            original = circuit.child
            await circuit.tick(client)
            self.assertIs(circuit.child, original)
            self.assertIn(("/trees", 5, 5),
                          circuit.child.task._ignored_corpse_tiles)
            # The ordinary dwell expiry must preserve it too, not just the
            # priority-spawn early-switch branch.
            circuit.child.task.priority_spawns = []
            circuit._farm_started_at = now[0] - circuit.dwell_seconds - 1
            await circuit.tick(client)
            self.assertIs(circuit.child, original)
            self.assertIn(("/trees", 5, 5),
                          circuit.child.task._ignored_corpse_tiles)

        asyncio.run(run())

    def test_farm_circuit_detects_disease_and_uses_only_paid_known_cure(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.applied = []

            async def clear_actions(self):
                pass

            async def set_combat(self, enabled, force=False):
                pass

            async def apply(self, tag):
                self.applied.append(tag)

        async def run():
            now = [100.0]
            client = FakeClient()
            client.state.messages.append(
                (0, 0, 0, "Your feet itch.  They burn."))
            client.state.place_item(Item(
                20, item_type=c.TYPE_MONEY, quality=100,
                name="silver coin", quantity=30), 7)
            circuit = FarmCircuitTask(
                WorldGraph(), [("/farm", "rat")], clock=lambda: now[0])
            circuit._observe_disease(client)
            self.assertTrue(circuit._disease_suspected)
            client.state.stats["level"] = 8
            self.assertTrue(circuit._needs_disease_cure_purchase(client))
            self.assertFalse(circuit._needs_upgrade_shopping(client))
            purchase = circuit._new_disease_cure()
            self.assertEqual(purchase.navigation.destination,
                             "/shattered_islands/world_5_58")
            self.assertEqual(purchase.navigation.destination_xy, (9, 17))

            client.state.place_item(Item(
                21, flags=c.ITEM_UNPAID, item_type=c.TYPE_POTION,
                quality=100, name="potion of cure illness"), 7)
            self.assertFalse(circuit._needs_disease_cure_purchase(client))
            self.assertFalse(await circuit._apply_disease_cure(client))
            self.assertFalse(client.applied)
            client.state.items[21].flags &= ~c.ITEM_UNPAID
            self.assertTrue(await circuit._apply_disease_cure(client))
            self.assertEqual(client.applied, [21])
            client.state.messages.append(
                (0, 0, 0, "You are healed from disease athelete's foot."))
            circuit._observe_disease(client)
            self.assertFalse(circuit._disease_suspected)

        asyncio.run(run())

    def test_inventory_capability_prefers_free_spell_and_verifies_identify(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats["sp"] = 20
                self.fires = []
                self.applied = []

            async def fire(self, direction, tag=0):
                self.fires.append((direction, tag))

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        client.state.place_item(Item(
            20, item_type=c.TYPE_WEAPON, quality=255,
            name="unknown sword"), 7)
        client.state.place_item(Item(
            21, item_type=c.TYPE_SCROLL, quality=80,
            name="scroll of identify"), 7)
        client.state.place_item(Item(
            22, item_type=c.TYPE_SPELL, name="identify",
            extra={"cost": 5}), 7)
        task = InventoryCapabilityTask(
            InventoryCapabilityTask.PURPOSE_IDENTIFY)

        asyncio.run(task.tick(client))
        self.assertEqual(client.fires, [(0, 22)])
        self.assertFalse(client.applied)
        client.state.items[20].quality = 80
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_identify_rod_restores_previous_ranged_weapon(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats["sp"] = 0
                self.fires = []
                self.applied = []

            async def fire(self, direction, tag=0):
                self.fires.append((direction, tag))

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        client.state.place_item(Item(
            20, item_type=c.TYPE_WEAPON, quality=255,
            name="unknown sword"), 7)
        client.state.place_item(Item(
            21, flags=c.ITEM_APPLIED, item_type=c.TYPE_BOW, quality=80,
            name="oak bow"), 7)
        client.state.place_item(Item(
            22, item_type=c.TYPE_ROD, quality=80,
            name="rod of identify"), 7)
        client.state.equipment[c.EQUIP_WEAPON_RANGED] = 21
        task = InventoryCapabilityTask(
            InventoryCapabilityTask.PURPOSE_IDENTIFY)

        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [22])
        client.state.items[21].flags &= ~c.ITEM_APPLIED
        client.state.items[22].flags |= c.ITEM_APPLIED
        client.state.equipment[c.EQUIP_WEAPON_RANGED] = 22
        asyncio.run(task.tick(client))
        self.assertEqual(client.fires, [(0, 0)])
        client.state.items[20].quality = 80
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [22, 21])
        client.state.items[22].flags &= ~c.ITEM_APPLIED
        client.state.items[21].flags |= c.ITEM_APPLIED
        client.state.equipment[c.EQUIP_WEAPON_RANGED] = 21
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_remove_depletion_capability_uses_safe_potion_before_temple(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats["sp"] = 0
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def fire(self, direction, tag=0):
                raise AssertionError("no spell should be fired")

        client = FakeClient()
        client.state.place_item(Item(
            20, item_type=c.TYPE_FORCE, quality=0, name="depletion"), 7)
        client.state.place_item(Item(
            21, item_type=c.TYPE_POTION, quality=80,
            name="potion of remove depletion"), 7)
        task = InventoryCapabilityTask(
            InventoryCapabilityTask.PURPOSE_DEPLETION)

        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [21])
        client.state.remove_item(20)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_disease_capability_prefers_known_cure_over_potion(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["sp"] = 5
        client.state.place_item(Item(
            20, item_type=c.TYPE_POTION, quality=80,
            name="potion of cure illness"), 7)
        client.state.place_item(Item(
            21, item_type=c.TYPE_SPELL, name="cure disease",
            extra={"cost": 5}), 7)

        candidates = InventoryCapabilityTask.candidates(
            client, InventoryCapabilityTask.PURPOSE_DISEASE)

        self.assertEqual([item.tag for item in candidates], [21, 20])

    def test_disease_and_depletion_capabilities_accept_devices_and_scrolls(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["sp"] = 0
        client.state.place_item(Item(
            20, item_type=c.TYPE_WAND, quality=80,
            name="wand of cure disease (lvl 15)"), 7)
        client.state.place_item(Item(
            21, item_type=c.TYPE_SCROLL, quality=80,
            name="scroll of cure disease"), 7)
        client.state.place_item(Item(
            22, item_type=c.TYPE_ROD, quality=80,
            name="rod of remove depletion"), 7)
        client.state.place_item(Item(
            23, item_type=c.TYPE_SCROLL, quality=80,
            name="scroll of remove depletion"), 7)

        disease = InventoryCapabilityTask.candidates(
            client, InventoryCapabilityTask.PURPOSE_DISEASE)
        depletion = InventoryCapabilityTask.candidates(
            client, InventoryCapabilityTask.PURPOSE_DEPLETION)

        self.assertEqual([item.tag for item in disease], [20, 21])
        self.assertEqual([item.tag for item in depletion], [22, 23])

    def test_farm_circuit_routes_depletion_to_real_aris_temple(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 7
        client.state.bank_balance = 5_504
        client.state.bank_balance_known = True
        client.state.depletion_points = 8
        client.state.depletion_points_known = True
        client.state.place_item(Item(
            22, item_type=c.TYPE_FORCE, quality=0, name="depletion"), 7)
        graph = built_graph()
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        client.state.bank_balance_known = False
        self.assertTrue(circuit._needs_bank_sync(client))
        sync = circuit._new_bank_sync()
        self.assertIsInstance(sync.task, BankBalanceTask)
        self.assertEqual(sync.navigation.destination,
                         "/shattered_islands/world_0_67")
        client.state.bank_balance_known = True
        self.assertFalse(circuit._needs_bank_sync(client))
        self.assertTrue(circuit._has_depletion(client))
        self.assertEqual(circuit._depletion_service_cost(client), 980)
        client.state.depletion_points = 2
        self.assertFalse(circuit._needs_depletion_service(client))
        self.assertFalse(circuit._needs_bank_sync(client))
        client.state.depletion_points = 3
        self.assertTrue(circuit._needs_depletion_service(client))
        # A persisted paid-failure deadline (from before a restart) holds it.
        client.state.paid_restoration_retry_until = time.time() + 3600
        self.assertFalse(circuit._needs_depletion_service(client))
        client.state.paid_restoration_retry_until = 0.0
        service = circuit._new_depletion_service(client)
        self.assertEqual(service.navigation.destination,
                         "/shattered_islands/world_3_58")
        self.assertEqual(service.navigation.destination_xy, (13, 13))
        self.assertEqual(service.navigation.tolerance, 1)
        # Every way to the temple crosses pack-hazard swamp. At level 7 the
        # trip must not take the swamp fallback; from level 18 it must.
        self.assertFalse(service.navigation.allow_ranged_hazard_fallback)
        self.assertEqual(service.safety.heal_below, 0.85)
        client.state.stats["level"] = FarmCircuitTask.SWAMP_TRANSIT_LEVEL
        self.assertTrue(circuit._new_depletion_service(
            client).navigation.allow_ranged_hazard_fallback)
        client.state.stats["level"] = 7
        self.assertTrue(graph.nodes[service.navigation.destination].walkable(
            *service.navigation.destination_xy))
        self.assertIsInstance(service.task, TempleServiceTask)
        self.assertEqual(service.task.priest, "Saruthar")
        self.assertEqual(service.task.cost, 980)
        self.assertTrue(circuit._queue_depletion_service(client))
        self.assertIsInstance(circuit._restoration.task, TempleServiceTask)
        self.assertEqual(circuit._restoration.task.cost, 980)

    def test_temple_service_reports_whether_the_fee_was_taken(self):
        from .model import InterfaceState

        def timed_out(interface_text):
            client = type("Client", (), {})()
            client.state = GameState(phase="playing", player_tag=7)
            client.state.place_item(Item(
                22, item_type=c.TYPE_FORCE, quality=0, name="depletion"), 7)
            if interface_text:
                client.state.interface = InterfaceState(
                    title="Saruthar", text=interface_text)
            task = TempleServiceTask("Saruthar", "remove depletion", "depletion", 2020)
            task.status = TaskStatus.RUNNING
            task._sent_at = time.monotonic() - 9.0
            asyncio.run(task.tick(client))
            self.assertEqual(task.status, TaskStatus.FAILED)
            return task.error

        self.assertEqual(
            timed_out("Removal of depletion\n\nYou pay 20 silver coins and 20 copper coins.\n\n"
                      "Okay, I will cast remove depletion on you now."),
            "temple service took payment but did not remove depletion")
        self.assertEqual(timed_out(""),
                         "temple service did not answer the purchase of depletion")

    @staticmethod
    def _talking_client(food):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["food"] = food
        client.talks = []

        async def talk(message, npc=None):
            client.talks.append((message, npc))

        client.talk = talk
        return client

    def test_temple_food_asks_the_priest_and_checks_she_ate(self):
        from .model import InterfaceState

        client = self._talking_client(0)
        # An earlier visit's answer is still open; it must not count.
        client.state.interface = InterfaceState(
            title="Saruthar", text="Your stomach is filled again.")
        task = TempleFoodTask("Saruthar")
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks, [("food", "Saruthar")])
        self.assertIsNone(client.state.interface)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.interface = InterfaceState(
            title="Saruthar", text="Your stomach is filled again.")
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        # The raised food stat alone also proves it.
        fed = self._talking_client(0)
        task = TempleFoodTask("Saruthar")
        asyncio.run(task.tick(fed))
        fed.state.stats["food"] = 500
        asyncio.run(task.tick(fed))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        # No answer in time fails, so the circuit can back off.
        silent = self._talking_client(0)
        task = TempleFoodTask("Saruthar")
        asyncio.run(task.tick(silent))
        task._sent_at = time.monotonic() - 9.0
        asyncio.run(task.tick(silent))
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.error,
                         "Saruthar did not answer the temple food request")

    def test_world_graph_compiles_temple_priests(self):
        graph = built_graph()
        saruthar = next(priest for priest in graph.temple_priests
                        if priest.name == "Saruthar")
        self.assertEqual(saruthar.map_path, "/shattered_islands/world_3_58")
        self.assertTrue(saruthar.script.startswith("/interfaces/temples/"))
        # Priests are services, not merchants.
        self.assertNotIn(saruthar, graph.dialogue_vendors)

    def test_hungry_circuit_falls_back_to_temple_food(self):
        from .navigation import DialogueVendor

        now = [100.0]
        graph = WorldGraph()
        graph.temple_priests.append(DialogueVendor(
            "/temple", 5, 5, "Saruthar", "/interfaces/temples/elathiel.xml"))
        circuit = FarmCircuitTask(graph, [("/a", "rat")], clock=lambda: now[0])
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["food"] = 0
        planned = []

        def new_temple_food(client):
            planned.append(now[0])
            return NavigateThenTask(graph, "/temple", TempleFoodTask("Saruthar"))

        circuit._new_temple_food = new_temple_food
        self.assertTrue(circuit._needs_food_resupply(client))
        # Shop resupply backing off still leaves the temple.
        circuit._resupply_retry_at = now[0] + 1800.0
        self.assertTrue(circuit._needs_food_resupply(client))
        circuit._temple_food_retry_at = now[0] + 600.0
        self.assertFalse(circuit._needs_food_resupply(client))
        # Fed again: no detour at all.
        circuit._temple_food_retry_at = 0.0
        client.state.stats["food"] = 300
        self.assertFalse(circuit._needs_food_resupply(client))

    def test_shop_resupply_that_bought_nothing_backs_off_to_the_temple(self):
        now = [100.0]
        graph = WorldGraph()
        circuit = FarmCircuitTask(graph, [("/a", "rat")], clock=lambda: now[0])
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["food"] = 0
        done = NavigateThenTask(graph, "/shop", TempleFoodTask("x"))
        done.status = TaskStatus.COMPLETE

        circuit._resupply, circuit._resupply_kind = done, "shop"
        circuit._settle_resupply(client)
        self.assertIsNone(circuit._resupply)
        self.assertEqual(circuit._resupply_retry_at, now[0] + 1800.0)
        # Food just bought on a shop floor (unpaid until she leaves) counts.
        client.state.stats["food"] = 900
        circuit._resupply_retry_at = 0.0
        client.state.place_item(Item(971, name="staple food", quantity=5,
                                     flags=c.ITEM_UNPAID), 7)
        circuit._resupply, circuit._resupply_kind = done, "shop"
        circuit._settle_resupply(client)
        self.assertEqual(circuit._resupply_retry_at, 0.0)
        client.state.items.pop(971)
        client.state.inventories[7].remove(971)
        # A shop run that did feed her (and filled her reserve) sets no backoff.
        client.state.stats["food"] = 900
        client.state.place_item(Item(970, name="staple food", quantity=10), 7)
        circuit._resupply_retry_at = 0.0
        circuit._resupply, circuit._resupply_kind = done, "shop"
        circuit._settle_resupply(client)
        self.assertEqual(circuit._resupply_retry_at, 0.0)
        # A failed temple request backs off only the temple path.
        failed = NavigateThenTask(graph, "/temple", TempleFoodTask("x"))
        failed.status = TaskStatus.FAILED
        circuit._resupply, circuit._resupply_kind = failed, "temple"
        circuit._settle_resupply(client)
        self.assertEqual(circuit._temple_food_retry_at,
                         now[0] + FarmCircuitTask.TEMPLE_FOOD_RETRY_SECONDS)
        self.assertEqual(circuit._resupply_retry_at, 0.0)

    def test_paid_but_ineffective_temple_service_backs_off(self):
        # Live: Saruthar took the fee and said he would cast, but the
        # depletion force stayed; retrying every half hour drains the purse.
        self.assertEqual(
            FarmCircuitTask.restoration_backoff(
                "temple service took payment but did not remove depletion"),
            FarmCircuitTask.PAID_RESTORATION_RETRY_SECONDS)
        # An unanswered request was not paid for: the usual half hour.
        self.assertEqual(
            FarmCircuitTask.restoration_backoff(
                "temple service did not answer the purchase of depletion"),
            FarmCircuitTask.RESTORATION_RETRY_SECONDS)
        self.assertEqual(
            FarmCircuitTask.restoration_backoff("no component route from /a to /b"),
            FarmCircuitTask.RESTORATION_RETRY_SECONDS)
        self.assertGreater(FarmCircuitTask.PAID_RESTORATION_RETRY_SECONDS,
                           FarmCircuitTask.RESTORATION_RETRY_SECONDS)

    def test_temple_service_pays_from_bank_and_verifies_force_removal(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 5_504
                self.state.bank_balance_known = True
                self.state.depletion_points = 8
                self.state.depletion_points_known = True
                self.state.place_item(Item(
                    22, item_type=c.TYPE_FORCE, quality=0,
                    name="depletion"), 7)
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

            def set_depletion_points(self, points):
                self.state.depletion_points = points
                self.state.depletion_points_known = True

        from .model import InterfaceState

        client = FakeClient()
        # An earlier visit's paid answer is still open.
        client.state.interface = InterfaceState(
            title="Saruthar", text="You pay 9 silver coins and 80 copper coins.")
        task = TempleServiceTask(
            "Saruthar", "remove depletion", "depletion", 980)
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks,
                         [("buy remove depletion", "Saruthar")])
        self.assertEqual(client.state.bank_balance, 4_524)
        self.assertIsNone(client.state.interface)
        # Unanswered, it is not taken for paid.
        unanswered = TempleServiceTask(
            "Saruthar", "remove depletion", "depletion", 980)
        client.state.interface = InterfaceState(
            title="Saruthar", text="You pay 9 silver coins and 80 copper coins.")
        asyncio.run(unanswered.tick(client))
        unanswered._sent_at = time.monotonic() - 9.0
        asyncio.run(unanswered.tick(client))
        self.assertEqual(unanswered.error,
                         "temple service did not answer the purchase of depletion")
        client.state.remove_item(22)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.state.depletion_points, 0)

    def test_temple_service_forgets_the_bank_balance_when_its_send_is_lost(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 5_504
                self.state.bank_balance_known = True
                self.state.place_item(Item(
                    22, item_type=c.TYPE_FORCE, quality=0,
                    name="depletion"), 7)
                self.persisted = []
                self.connected = False
                self.talks = []

            def set_bank_balance(self, balance):
                self.persisted.append(balance)
                self.state.bank_balance = balance

            async def talk(self, message, npc):
                if not self.connected:
                    raise ConnectionError("not connected")
                self.talks.append((message, npc))

        client = FakeClient()
        task = TempleServiceTask(
            "Saruthar", "remove depletion", "depletion", 980)
        with self.assertRaises(ConnectionError):
            asyncio.run(task.tick(client))
        # The server may or may not have charged: neither the reserved nor the
        # refunded balance is trusted, so the bank is read again before the
        # next budgeted purchase.
        self.assertEqual(client.persisted, [4_524])
        self.assertFalse(client.state.bank_balance_known)
        # The stale reservation is not left for wallet checks to count.
        self.assertEqual(client.state.bank_balance, 0)
        self.assertEqual(BuyShopUpgradeTask.wallet_value(client), 0)

    def test_mass_identify_pays_flat_service_and_verifies_item_updates(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 3_000
                self.state.bank_balance_known = True
                self.state.place_item(Item(
                    22, item_type=c.TYPE_WEAPON, quality=255,
                    name="unknown sword"), 7)
                # Skills legitimately retain quality=255 and must not make a
                # successful type-aware identification batch time out.
                self.state.place_item(Item(
                    23, item_type=c.TYPE_SKILL, quality=255,
                    name="slash weapons"), 7)
                self.talks = []
                self.actions = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

            def record_action(self, action, detail):
                self.actions.append((action, detail))

        client = FakeClient()
        task = MassIdentifyTask("Kulgar", 1_450)
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks, [("buy identify_all", "Kulgar")])
        self.assertEqual(client.state.bank_balance, 1_550)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.items[22].quality = 88
        client.state.items[22].condition = 88
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertTrue(any(action == "mass-identify"
                            for action, _ in client.actions))

    def test_mass_identify_waits_for_inventory_sync_after_service_ack(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance = 3_000
                self.state.bank_balance_known = True
                self.state.place_item(Item(
                    22, item_type=c.TYPE_WAND, quality=255,
                    name="wand"), 7)
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

        client = FakeClient()
        task = MassIdentifyTask("Kulgar", 1_450)
        with patch("atrinik_bot.tasks.time.monotonic",
                   return_value=100.0):
            asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(
            title="Kulgar",
            text="Identification of all objects\n\n"
                 "Thank you for your business!",
        )
        with patch("atrinik_bot.tasks.time.monotonic",
                   return_value=111.0):
            asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(task._acknowledged_at, 111.0)
        with patch("atrinik_bot.tasks.time.monotonic",
                   return_value=160.0):
            asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.items[22].quality = 80
        client.state.items[22].condition = 80
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_farm_circuit_batches_unknown_gear_at_economical_smith(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.bank_balance = 3_000
        client.state.bank_balance_known = True
        for tag in range(19):
            client.state.place_item(Item(
                900 + tag, item_type=c.TYPE_WEAPON, quality=255,
                name=f"unknown weapon {tag}", weight=3.5), 7)
        client.state.stats["weight_limit"] = 100
        graph = WorldGraph()
        graph.nodes["/shattered_islands/world_8_79"] = MapNode(
            "/shattered_islands/world_8_79", width=24, height=24)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        self.assertFalse(circuit._needs_mass_identification(client))
        client.state.place_item(Item(
            999, item_type=c.TYPE_WEAPON, quality=255,
            name="unknown final weapon", weight=3.5), 7)
        self.assertTrue(circuit._needs_mass_identification(client))
        service = circuit._new_mass_identification()
        self.assertEqual(service.navigation.destination,
                         "/shattered_islands/world_8_79")
        self.assertEqual(service.navigation.destination_xy, (4, 9))
        self.assertEqual(service.navigation.tolerance, 1)
        self.assertIsInstance(service.task, MassIdentifyTask)
        self.assertEqual(service.task.smith, "Kulgar")
        self.assertEqual(service.task.cost, 1_450)
        client.state.bank_balance = 2_449
        self.assertFalse(circuit._needs_mass_identification(client))

    def test_unknown_device_is_identified_early_while_recall_is_missing(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(level=19, weight_limit=100)
        client.state.bank_balance = 3_000
        client.state.bank_balance_known = True
        client.state.place_item(Item(
            90, item_type=c.TYPE_WAND, quality=255,
            name="wand", weight=0.5), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])

        self.assertTrue(circuit._needs_mass_identification(client))
        client.state.place_item(Item(
            91, item_type=c.TYPE_WAND, quality=80, condition=80,
            name="wand of word of recall (lvl 12)"), 7)
        self.assertFalse(circuit._needs_mass_identification(client))

    def test_farm_circuit_sells_only_identified_common_junk_batches(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["weight_limit"] = 100
        for tag in range(20):
            client.state.place_item(Item(
                800 + tag, item_type=c.TYPE_BOOK, quality=80,
                name=f"paper guide {tag}"), 7)
        client.state.place_item(Item(
            850, item_type=c.TYPE_BOOK, quality=255,
            name="paper unknown chronicle"), 7)
        client.state.place_item(Item(
            851, item_type=c.TYPE_BOOK, quality=80,
            name="paper quest formula"), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        junk = circuit._identified_junk(client)
        self.assertEqual(
            {item.tag for item in junk},
            set(range(800, 820)))
        self.assertFalse(circuit._needs_junk_sale(client))
        client.state.items[800].weight = 4.0
        for tag in range(801, 820):
            client.state.items[tag].weight = 3.5
        self.assertFalse(circuit._needs_junk_sale(client))
        client.state.items[800].weight = 19.0
        self.assertTrue(circuit._needs_junk_sale(client))
        sale = circuit._new_junk_sale(client)
        self.assertEqual(sale.navigation.destination,
                         "/shattered_islands/world_0_69")
        self.assertEqual(sale.navigation.destination_xy, (15, 7))
        self.assertIsInstance(sale.task, SellJunkTask)

    def test_farm_circuit_sells_proven_inferior_nonmagical_equipment(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update(level=15, weight_limit=100)
        current = Item(
            860, flags=c.ITEM_APPLIED | c.ITEM_MAGICAL,
            item_type=c.TYPE_WEAPON, quality=88, condition=88,
            required_level=7, required_skill_tag=1001,
            name="shear steel falchion +2")
        inferior = Item(
            861, item_type=c.TYPE_WEAPON, quality=89, condition=89,
            required_level=7, required_skill_tag=1002, weight=86,
            name="diamant steel battle cleaver")
        future = Item(
            862, item_type=c.TYPE_WEAPON, quality=89, condition=89,
            required_level=20, required_skill_tag=1001,
            name="darksteel future sword")
        trophy = Item(
            863, item_type=c.TYPE_WEAPON, quality=90, condition=90,
            required_level=7, required_skill_tag=1002,
            name="diamant steel trophy cleaver")
        for item in (current, inferior, future, trophy):
            client.state.place_item(item, 7)
        client.state.equipment[c.EQUIP_WEAPON] = current.tag
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        self.assertEqual(
            {item.tag for item in circuit._surplus_equipment(client)},
            {inferior.tag})
        self.assertEqual(
            {item.tag for item in circuit._identified_junk(client)},
            {inferior.tag})
        self.assertTrue(circuit._needs_junk_sale(client))
        sale = circuit._new_junk_sale(client)
        self.assertTrue(sale.task.policy.junk(inferior))
        self.assertFalse(sale.task.policy.junk(future))
        self.assertFalse(sale.task.policy.junk(trophy))

    def test_only_proper_named_spawn_enables_early_reroll_switch(self):
        farm = FarmTask(priority_spawns=[
            (1, 1, "hill giant"), (2, 2, "ogre rock thrower")])
        self.assertFalse(FarmCircuitTask._has_proper_named_priority(farm))
        farm.priority_spawns.append((3, 3, "Fahrgorm"))
        self.assertTrue(FarmCircuitTask._has_proper_named_priority(farm))

    def test_lost_soul_circuit_leg_uses_early_retreat_margin(self):
        graph = WorldGraph()
        graph.nodes["/souls"] = MapNode("/souls", width=10, height=10)
        graph.nodes["/trees"] = MapNode("/trees", width=10, height=10)
        souls = FarmCircuitTask(
            graph, [("/souls", "Thrakir|lost soul")])._new_child().task
        trees = FarmCircuitTask(
            graph, [("/trees", "Fahrgorm|evil treant")])._new_child().task
        self.assertEqual((souls.safety.flee_below,
                          souls.safety.heal_below), (0.72, 0.88))
        self.assertEqual((trees.safety.flee_below,
                          trees.safety.heal_below), (0.55, 0.70))

    def test_hostile_route_expedition_aborts_to_its_origin_on_adds(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats.update({
                    "level": 14, "hp": 100, "maxhp": 100,
                    "sp": 40, "maxsp": 40,
                })
                self.state.map = MapState(
                    path="/safe", width=17, height=17,
                    world_x=4, world_y=5)
                self.actions = []
                self.combat = []
                self.moves = []

            async def clear_actions(self):
                self.actions.append("clear")

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)
                self.state.combat = enabled

            async def move(self, direction, run=False):
                self.moves.append((direction, run))

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

            def record_action(self, action, detail=""):
                self.actions.append((action, detail))

        graph = WorldGraph()
        for path in ("/safe", "/danger", "/outpost"):
            graph.nodes[path] = MapNode(path, width=10, height=10)
        graph.nodes["/danger"].edges.append(MapEdge(
            "/danger", "/safe", 5, 9,
            destination_x=4, destination_y=0, automatic=True))
        client = FakeClient()
        circuit = FarmCircuitTask(
            graph, [("/outpost", "ice golem")],
            clear_hostile_route=True)
        asyncio.run(circuit.start(client))
        self.assertEqual(circuit._expedition_origin, ("/safe", 4, 5))

        client.state.map = MapState(
            path="/danger", width=17, height=17,
            world_x=5, world_y=4)
        now = time.time()
        client.state.messages.extend([
            (now, 2, 0, "ogre hit you for 8 damage."),
            (now, 2, 0, "hill giant misses you!"),
        ])
        reason = circuit._hostile_route_abort_reason(client)
        self.assertIn("hill giant", reason)
        self.assertIn("ogre", reason)
        self.assertTrue(asyncio.run(
            circuit._tick_expedition_return(client)))
        self.assertEqual(circuit._expedition_return.destination, "/safe")
        self.assertEqual(circuit._expedition_return.destination_xy, (4, 5))
        self.assertIn(False, client.combat)
        self.assertTrue(any(
            isinstance(action, tuple) and action[0] == "hostile-route-abort"
            for action in client.actions))

    def test_farm_circuit_batches_and_banks_all_carried_cash(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["weight_limit"] = 100
        for item in (
                Item(1, item_type=c.TYPE_MONEY, quality=100,
                     name="gold coin", quantity=1),
                Item(2, item_type=c.TYPE_MONEY, quality=100,
                     name="silver coin", quantity=60),
                Item(3, item_type=c.TYPE_MONEY, quality=100,
                     name="copper coin", quantity=50)):
            client.state.place_item(item, 7)
        graph = built_graph()
        circuit = FarmCircuitTask(graph, [("/farm", "rat")],
                                  clock=lambda: 100.0)
        self.assertTrue(circuit._needs_bank_deposit(client))
        banking = circuit._new_bank_deposit(client)
        self.assertEqual(banking.navigation.destination,
                         "/shattered_islands/world_0_67")
        self.assertEqual(banking.navigation.destination_xy, (19, 15))
        self.assertTrue(graph.nodes[banking.navigation.destination].walkable(
            *banking.navigation.destination_xy))
        self.assertEqual(banking.task.deposit, "all")
        client.state.bank_balance = 50_000
        client.state.bank_balance_known = True
        self.assertEqual(BuyShopUpgradeTask.carried_wallet_value(client), 16_050)
        self.assertEqual(BuyShopUpgradeTask.wallet_value(client), 66_050)
        for item in list(client.state.inventory):
            client.state.remove_item(item.tag)
        self.assertFalse(circuit._needs_bank_deposit(client))

        client.state.place_item(Item(
            4, item_type=c.TYPE_MONEY, quality=100,
            name="copper coin", quantity=1), 7)
        self.assertFalse(circuit._needs_bank_deposit(client))
        client.state.items[4].quantity = circuit.BANK_DEPOSIT_MINIMUM
        self.assertFalse(circuit._needs_bank_deposit(client))
        client.state.items[4].weight = 0.071
        self.assertFalse(circuit._needs_bank_deposit(client))
        client.state.items[4].weight = 0.086
        self.assertTrue(circuit._needs_bank_deposit(client))
        client.state.last_bank_deposit_at = time.time() - 120
        self.assertFalse(circuit._needs_bank_deposit(client))
        client.state.last_bank_deposit_at = time.time() - 3601
        self.assertTrue(circuit._needs_bank_deposit(client))
        circuit._banking_retry_at = 101.0
        self.assertFalse(circuit._needs_bank_deposit(client))

    def test_farm_circuit_checkpoints_progress_only_between_fights(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.stats.update(
                    level=12, exp=1_500_000, hp=130, maxhp=130)
                self.state.map = MapState(
                    path="/farm", width=17, height=17)
                self.checkpoints = 0
                self.clears = 0
                self.combat = []
                self.decisions = []

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

            async def checkpoint_reconnect(self):
                self.checkpoints += 1
                return True

            def record_action(self, action, detail):
                self.decisions.append((action, detail))

        now = [100.0]
        client = FakeClient()
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: now[0])
        farm = FarmTask(zone="/farm")
        circuit._checkpoint_at = now[0]
        circuit._checkpoint_exp = client.state.stats["exp"]
        circuit._current_exp = circuit._checkpoint_exp + 15_000

        now[0] += circuit.PROGRESS_CHECKPOINT_INTERVAL - 1
        self.assertFalse(circuit._needs_progress_checkpoint(client, farm))
        now[0] += 1
        farm._engaged_target = (
            10, "/farm", 1, 1, time.monotonic())
        self.assertFalse(circuit._needs_progress_checkpoint(client, farm))
        farm._engaged_target = None
        self.assertTrue(circuit._needs_progress_checkpoint(client, farm))

        self.assertTrue(asyncio.run(circuit._progress_checkpoint(client)))
        self.assertEqual(client.checkpoints, 1)
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.combat, [False])
        self.assertEqual(circuit._checkpoint_exp, 1_515_000)
        self.assertEqual(client.decisions, [
            ("progress-checkpoint", "experience=1515000")])
        self.assertFalse(circuit._needs_progress_checkpoint(client, farm))

    def test_completed_junk_sale_forces_next_safe_bank_deposit(self):
        class CompletedSale:
            status = TaskStatus.COMPLETE
            error = ""

            async def tick(self, client):
                return None

        graph = WorldGraph()
        graph.nodes["/farm"] = MapNode(
            "/farm", width=10, height=10)
        circuit = FarmCircuitTask(
            graph, [("/farm", "rat")], clock=lambda: 100.0)
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map.path = "/farm"
        client.state.stats["level"] = 9
        asyncio.run(circuit.start(client))
        circuit._banking_retry_at = 3_700.0
        circuit._selling = CompletedSale()
        asyncio.run(circuit.tick(client))
        self.assertIsNone(circuit._selling)
        self.assertEqual(circuit._banking_retry_at, 0.0)
        self.assertTrue(circuit._force_bank_deposit)
        client.state.place_item(Item(
            1, item_type=c.TYPE_MONEY, quality=100,
            name="copper coin", quantity=1), 7)
        self.assertTrue(circuit._needs_bank_deposit(client))

    def test_farm_circuit_batches_unidentified_loot_to_apartment(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["weight_limit"] = 100
        for tag, name in enumerate((
                "skinned lychee", "wholemeal bread", "tournedos steak",
                "pine arrow"), 680):
            item_type = (c.TYPE_ARROW if "arrow" in name else c.TYPE_FOOD)
            client.state.place_item(Item(
                tag, item_type=item_type, quality=255, name=name), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        self.assertFalse(circuit._needs_apartment_storage(client))
        for tag in range(19):
            client.state.place_item(Item(
                700 + tag, item_type=c.TYPE_WEAPON, quality=255,
                name=f"unknown weapon {tag}"), 7)
        graph = WorldGraph()
        graph.nodes[
            "/shattered_islands/strakewood_island/apartments/apartment_cheap"
        ] = MapNode("apartment", width=6, height=6)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        self.assertFalse(circuit._needs_apartment_storage(client))
        client.state.place_item(Item(
            799, item_type=c.TYPE_WEAPON, quality=255,
            name="unknown final weapon"), 7)
        self.assertFalse(circuit._needs_apartment_storage(client))
        client.state.items[799].weight = 86.0
        self.assertTrue(circuit._needs_apartment_storage(client))
        storage = circuit._new_apartment_storage()
        self.assertEqual(storage.navigation.destination_xy, (1, 3))
        self.assertTrue(storage.task.unidentified_only)
        self.assertTrue(storage.task.valuable_only)

        rare_client = type("Client", (), {})()
        rare_client.state = GameState(phase="playing", player_tag=7)
        rare_client.state.stats["weight_limit"] = 100
        rare_client.state.place_item(Item(
            800, flags=c.ITEM_MAGICAL, item_type=c.TYPE_RING,
            quality=90, condition=100, name="ring of the ghost"), 7)
        self.assertTrue(circuit._needs_apartment_storage(rare_client))

    def test_savebed_confirmation_cannot_be_forged_by_players(self):
        state = GameState()
        position = state.message_count
        state.add_message(2, "ffffff", "[a=#charname]Mallory[/a] says: You save and your save bed location is updated.")
        state.add_message(2, "ffffff", "Mallory: you save and your save bed location is updated.")
        state.add_message(2, "ffffff", "You save and your save bed location is updated.")
        texts = [entry[3] for entry in state.messages_since(position)]
        self.assertEqual(len(texts), 2)  # the linked name is dropped
        self.assertEqual([t for t in texts if BindSavebedTask.CONFIRM.fullmatch(t)],
                         ["You save and your save bed location is updated."])

    def test_savebed_is_applied_verified_and_persisted_where_it_stands(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path="/shattered_islands/world_6_48_1", world_x=1, world_y=12)
        client.state.place_item(Item(
            800, item_type=106, name="bed to reality"), 0)
        client.applied = []
        client.persisted = None

        async def apply(tag):
            client.applied.append(tag)

        def persist(path, x, y):
            client.persisted = (path, x, y)

        client.apply = apply
        client.set_savebed = persist
        # A long session has already filled the bounded message history, so
        # the confirmation replaces an old message instead of growing it.
        for i in range(600):
            client.state.add_message(2, "ffffff", f"You hit giant frog for {i}.")
        task = BindSavebedTask()
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [800])
        client.state.add_message(
            2, "ffffff", "You save and your save bed location is updated.")
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.persisted, ("/shattered_islands/world_6_48_1", 1, 12))

    @staticmethod
    def savebed_graph(far_width=60):
        """A farm next to a public inn and a private apartment, with the
        starter apartment a long walk away through /far."""
        graph = WorldGraph()

        def lane(path, width):
            node = MapNode(path, width=width, height=1)
            node.terrain = {(x, 0): 1 for x in range(width)}
            graph.nodes[path] = node
            return node

        lane("/farm", 10)
        for path in ("/inn", "/shattered_islands/x/apartments/private"):
            lane(path, 4).edges.append(MapEdge(path, "/farm", 3, 0, 0, 0))
            graph.savebeds[path].append((1, 0))
        graph.savebeds["/inn"].append((2, 0))
        lane(STARTER_APARTMENT, 4).edges.append(
            MapEdge(STARTER_APARTMENT, "/far", 3, 0, 0, 0))
        graph.savebeds[STARTER_APARTMENT].append((1, 0))
        lane("/far", far_width).edges.append(
            MapEdge("/far", "/farm", far_width - 1, 0, 0, 0))
        return graph

    def test_savebed_walk_measures_from_real_tile_seam_arrivals(self):
        from .navigation import savebed_walk

        # West map -> east map across a tile seam: the arrival is the east
        # map's x=0 at the crossing row, not the edge default (0, 0).
        graph = WorldGraph()
        for path in ("/west", "/east", "/farm"):
            node = MapNode(path, width=10, height=10)
            node.terrain = {(x, y): 1 for x in range(10) for y in range(10)}
            graph.nodes[path] = node
        graph.nodes["/west"].edges.append(MapEdge("/west", "/east", 10, 5, kind="tile"))
        graph.nodes["/east"].edges.append(MapEdge("/east", "/farm", 9, 9, 0, 0))
        # (5, 9) -> border (9, 9) is 4 moves and the seam 1 more, arriving
        # at east (0, 9); from there the exit at (9, 9) is 9 moves.
        self.assertEqual(savebed_walk(graph, "/west", (5, 9), ["/farm"]), 14)

    def test_savebed_walk_follows_walls_and_every_circuit_leg(self):
        from .navigation import savebed_walk

        # A wall forces a detour that the straight line would miss, and the
        # second circuit map (two transitions away) is the nearer walk.
        graph = WorldGraph()
        bed = MapNode("/bed", width=10, height=10)
        bed.terrain = {(x, y): 1 for x in range(10) for y in range(10)}
        bed.blocked = {(5, y) for y in range(9)}
        graph.nodes["/bed"] = bed
        for path in ("/a", "/b", "/c"):
            node = MapNode(path, width=10, height=1)
            node.terrain = {(x, 0): 1 for x in range(10)}
            graph.nodes[path] = node
        bed.edges.append(MapEdge("/bed", "/a", 9, 0, 0, 0))
        bed.edges.append(MapEdge("/bed", "/c", 0, 1, 0, 0))
        graph.nodes["/c"].edges.append(MapEdge("/c", "/b", 1, 0, 0, 0))
        # To /a: around the wall at x=5 via row 9 is 9 + 9 moves (not 9).
        self.assertEqual(savebed_walk(graph, "/bed", (0, 0), ["/a"]), 18)
        # /b via /c is 1 + 1 moves, nearer although one transition farther.
        self.assertEqual(savebed_walk(graph, "/bed", (0, 0), ["/a", "/b"]), 2)

    def test_nearest_savebed_prefers_the_shortest_usable_walk(self):
        from .navigation import nearest_savebed, savebed_walk

        graph = self.savebed_graph()
        # Two moves to the exit on (3, 0); stepping onto it is the transfer.
        self.assertEqual(savebed_walk(graph, "/inn", (1, 0), ["/farm"]), 2)
        # The apartment's exit returns to the recorded entrance, here /far.
        returns = {STARTER_APARTMENT: ("/far", (0, 0))}
        self.assertEqual(savebed_walk(graph, STARTER_APARTMENT, (1, 0), ["/farm"],
                                      returns=returns), 61)
        # Without an observed return the apartment walk is unknown.
        self.assertIsNone(savebed_walk(graph, STARTER_APARTMENT, (1, 0), ["/farm"]))
        # The inn's other bed, one tile nearer its exit, wins; the equally
        # close private apartment belongs to someone else.
        self.assertEqual(nearest_savebed(graph, ["/farm"]), ("/inn", (2, 0), 1))
        self.assertEqual(nearest_savebed(graph, ["/farm"], excluded={"/inn"},
                                         returns=returns),
                         (STARTER_APARTMENT, (1, 0), 61))
        self.assertIsNone(nearest_savebed(graph, ["/farm"], excluded={"/inn"}))
        self.assertEqual(
            nearest_savebed(graph, ["/farm"], excluded={"/inn"},
                            owned={STARTER_APARTMENT, "/shattered_islands/x/apartments/private"},
                            returns={**returns,
                                     "/shattered_islands/x/apartments/private": ("/farm", (0, 0))}),
            ("/shattered_islands/x/apartments/private", (1, 0), 2))
        # A bed with no route to the spot is never chosen.
        graph.nodes["/inn"].edges.clear()
        self.assertEqual(nearest_savebed(graph, ["/farm"], returns=returns)[0],
                         STARTER_APARTMENT)

    def test_farm_circuit_moves_its_savebed_next_to_the_spot_before_farming(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        client.state.savebed_map, client.state.savebed_xy = STARTER_APARTMENT, (1, 0)
        client.state.apartment_returns = {STARTER_APARTMENT: ("/far", (0, 0))}
        circuit = FarmCircuitTask(self.savebed_graph(), [("/farm", "rat")])
        # The search spreads its exact routes over ticks before deciding.
        decisions = [circuit._needs_savebed(client) for _ in range(8)]
        self.assertFalse(decisions[0])
        self.assertTrue(decisions[-1])
        binding = circuit._new_savebed(client)
        self.assertEqual(binding.navigation.destination, "/inn")
        self.assertEqual(binding.navigation.destination_xy, (2, 0))
        self.assertIsInstance(binding.task, BindSavebedTask)
        client.state.savebed_map, client.state.savebed_xy = "/inn", (2, 0)
        circuit._savebed_search(client).run()
        self.assertFalse(circuit._needs_savebed(client))
        # Bound to the inn's other bed, one move farther: not worth a rebind.
        client.state.savebed_map, client.state.savebed_xy = "/inn", (1, 0)
        circuit._savebed_search(client).run()
        self.assertFalse(circuit._needs_savebed(client))
        # Never bound yet: any reachable bed beats the default spawn.
        client.state.savebed_map, client.state.savebed_xy = "", None
        circuit._savebed_search(client).run()
        self.assertTrue(circuit._needs_savebed(client))
        # A failed bed tile is skipped for another in the same room first,
        # then for the next-nearest map.
        circuit._bed_failed.add(("/inn", (2, 0)))
        self.assertEqual(circuit._savebed_search(client).run().result,
                         ("/inn", (1, 0), 2))
        circuit._bed_failed.add(("/inn", (1, 0)))
        self.assertEqual(circuit._savebed_search(client).run().result[0], STARTER_APARTMENT)

    def test_farm_circuit_moves_to_a_much_nearer_bed_on_the_same_map(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        graph = WorldGraph()
        hall = MapNode("/hall", width=40, height=1)
        hall.terrain = {(x, 0): 1 for x in range(40)}
        hall.edges.append(MapEdge("/hall", "/farm", 39, 0, 0, 0))
        farm = MapNode("/farm", width=5, height=1)
        farm.terrain = {(x, 0): 1 for x in range(5)}
        graph.nodes.update({"/hall": hall, "/farm": farm})
        graph.savebeds["/hall"] = [(0, 0), (37, 0)]
        # Bound to the far bed of a long hall: the exit-side bed saves 37.
        client.state.savebed_map, client.state.savebed_xy = "/hall", (0, 0)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        search = circuit._savebed_search(client).run()
        self.assertEqual((search.result, search.current_walk), (("/hall", (37, 0), 2), 39))
        self.assertTrue(circuit._needs_savebed(client))
        self.assertEqual(circuit._new_savebed(client).navigation.destination_xy, (37, 0))

    def test_savebed_search_routes_each_bed_component_and_skips_exhausted_maps(self):
        from . import navigation
        from .navigation import nearest_savebed

        graph = WorldGraph()
        # One map, two rooms split by a wall, each with a bed and its own
        # exit: the east room's exit is right beside the farm.
        rooms = MapNode("/rooms", width=21, height=1)
        rooms.terrain = {(x, 0): 1 for x in range(21)}
        rooms.blocked = {(10, 0)}
        rooms.edges.append(MapEdge("/rooms", "/road", 0, 0, 0, 0))
        rooms.edges.append(MapEdge("/rooms", "/farm", 20, 0, 0, 0))
        road = MapNode("/road", width=30, height=1)
        road.terrain = {(x, 0): 1 for x in range(30)}
        road.edges.append(MapEdge("/road", "/farm", 29, 0, 0, 0))
        farm = MapNode("/farm", width=5, height=1)
        farm.terrain = {(x, 0): 1 for x in range(5)}
        graph.nodes.update({"/rooms": rooms, "/road": road, "/farm": farm})
        graph.savebeds["/rooms"] = [(1, 0), (19, 0)]
        tavern = MapNode("/tavern", width=3, height=1)
        tavern.terrain = {(x, 0): 1 for x in range(3)}
        tavern.edges.append(MapEdge("/tavern", "/farm", 2, 0, 0, 0))
        aaa = MapNode("/aaa", width=2, height=1)
        aaa.terrain = {(0, 0): 1, (1, 0): 1}
        aaa.edges.append(MapEdge("/aaa", "/farm", 1, 0, 0, 0))
        graph.nodes.update({"/tavern": tavern, "/aaa": aaa})
        graph.savebeds["/tavern"] = [(0, 0)]
        graph.savebeds["/aaa"] = [(0, 0)]
        self.assertEqual(nearest_savebed(graph, ["/farm"], excluded={"/tavern", "/aaa"}),
                         ("/rooms", (19, 0), 1))
        # With room for one candidate map, an exhausted nearer map yields
        # its slot to the next one.
        with patch.object(navigation, "SAVEBED_CANDIDATES", 1):
            self.assertEqual(
                nearest_savebed(graph, ["/farm"], excluded_tiles={("/aaa", (0, 0))},
                                excluded={"/rooms"}),
                ("/tavern", (0, 0), 2))

    def test_each_bed_is_priced_through_the_exit_that_suits_it(self):
        from .navigation import nearest_savebed

        # One long room with an exit at each end, each to a different
        # circuit map. The first bed (the anchor) is nearest the west exit;
        # the east bed is beside the east exit and must be priced through it.
        graph = WorldGraph()
        hall = MapNode("/hall", width=30, height=1)
        hall.terrain = {(x, 0): 1 for x in range(30)}
        hall.edges.append(MapEdge("/hall", "/west", 0, 0, 0, 0))
        hall.edges.append(MapEdge("/hall", "/east", 29, 0, 0, 0))
        west = MapNode("/west", width=5, height=1)
        west.terrain = {(x, 0): 1 for x in range(5)}
        west.edges.append(MapEdge("/west", "/farm_a", 4, 0, 0, 0))
        graph.nodes.update({"/hall": hall, "/west": west})
        for path in ("/east", "/farm_a"):
            node = MapNode(path, width=5, height=1)
            node.terrain = {(x, 0): 1 for x in range(5)}
            graph.nodes[path] = node
        graph.savebeds["/hall"] = [(1, 0), (28, 0)]
        # Legs: /farm_a (behind the short west map, the anchor's best: 5
        # moves) and /east (next door to the east bed: 1 move).
        self.assertEqual(nearest_savebed(graph, ["/farm_a", "/east"]),
                         ("/hall", (28, 0), 1))

    def test_a_bed_cut_off_from_the_farm_room_is_not_already_there(self):
        from .navigation import NamedSpawn, savebed_walk

        # The circuit map has two rooms; the farm's spawn is in the east one
        # and the bed in the west one, joined only through /yard.
        graph = WorldGraph()
        farm = MapNode("/farm", width=11, height=1)
        farm.terrain = {(x, 0): 1 for x in range(11)}
        farm.blocked = {(5, 0)}
        farm.edges.append(MapEdge("/farm", "/yard", 0, 0, 0, 0))
        yard = MapNode("/yard", width=3, height=1)
        yard.terrain = {(x, 0): 1 for x in range(3)}
        yard.edges.append(MapEdge("/yard", "/farm", 2, 0, 10, 0))
        graph.nodes.update({"/farm": farm, "/yard": yard})
        graph.named_spawns["/farm"] = [NamedSpawn("/farm", 8, 0, "rat", ("rat",))]
        # West room bed: out to /yard (2 moves), across it (2), back in.
        self.assertEqual(savebed_walk(graph, "/farm", (2, 0), ["/farm"]), 4)
        # East room bed: already with the farm.
        self.assertEqual(savebed_walk(graph, "/farm", (9, 0), ["/farm"]), 0)

    def test_unroutable_nearer_beds_do_not_use_up_candidate_slots(self):
        from . import navigation
        from .navigation import nearest_savebed

        graph = WorldGraph()
        farm = MapNode("/farm", width=3, height=1)
        farm.terrain = {(x, 0): 1 for x in range(3)}
        # /aaa is one hop away, but its bed is walled into a closet.
        aaa = MapNode("/aaa", width=5, height=1)
        aaa.terrain = {(x, 0): 1 for x in range(5)}
        aaa.blocked = {(1, 0)}
        aaa.edges.append(MapEdge("/aaa", "/farm", 4, 0, 0, 0))
        tavern = MapNode("/tavern", width=3, height=1)
        tavern.terrain = {(x, 0): 1 for x in range(3)}
        tavern.edges.append(MapEdge("/tavern", "/farm", 2, 0, 0, 0))
        graph.nodes.update({"/farm": farm, "/aaa": aaa, "/tavern": tavern})
        graph.savebeds["/aaa"] = [(0, 0)]
        graph.savebeds["/tavern"] = [(0, 0)]
        with patch.object(navigation, "SAVEBED_CANDIDATES", 1):
            self.assertEqual(nearest_savebed(graph, ["/farm"]), ("/tavern", (0, 0), 2))

    def test_apartment_exits_record_their_runtime_return(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            town = MapState(path="/shattered_islands/world_0_68", world_x=7, world_y=12)
            client._observe_map_change(STARTER_APARTMENT, town)
            client._observe_map_change("/shattered_islands/world_0_68",
                                       MapState(path="/shattered_islands/world_0_67"))
            self.assertEqual(client.state.apartment_returns,
                             {STARTER_APARTMENT: ("/shattered_islands/world_0_68", (7, 12))})
            # A death or recall that happens to leave the apartment is not
            # its exit's return point.
            client.record_action("emergency-recall", "via scroll")
            client._observe_map_change(STARTER_APARTMENT,
                                       MapState(path="/shattered_islands/world_4_45", world_x=1, world_y=1))
            self.assertEqual(client.state.apartment_returns[STARTER_APARTMENT][0],
                             "/shattered_islands/world_0_68")
            restarted = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=path))
            restarted.state.player_name = "Sera"
            restarted.load_character_memory("Sera")
            self.assertEqual(restarted.state.apartment_returns,
                             {STARTER_APARTMENT: ("/shattered_islands/world_0_68", (7, 12))})

    def test_an_unpriced_apartment_bed_is_kept_until_its_exit_is_seen(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        client.state.savebed_map, client.state.savebed_xy = STARTER_APARTMENT, (1, 0)
        circuit = FarmCircuitTask(self.savebed_graph(), [("/farm", "rat")])
        circuit._savebed_search(client).run()
        # The apartment's return is unknown: no proven gain, no rebind.
        self.assertFalse(circuit._needs_savebed(client))
        client.state.apartment_returns = {STARTER_APARTMENT: ("/far", (0, 0))}
        circuit._savebed_search(client).run()
        self.assertTrue(circuit._needs_savebed(client))

    def test_bind_savebed_recognizes_every_savebed_name(self):
        for name, names in (("hammock to reality", ()), ("save bed", ()),
                            ("canopy bed", ("canopy bed",))):
            client = type("Client", (), {})()
            client.state = GameState(phase="playing", player_tag=7)
            client.state.place_item(Item(800, item_type=0, name=name), 0)
            client.applied = []

            async def apply(tag, client=client):
                client.applied.append(tag)

            client.apply = apply
            task = BindSavebedTask(names=names)
            asyncio.run(task.tick(client))
            self.assertEqual(client.applied, [800], name)

    def test_any_savebed_confirmation_is_persisted_where_she_stands(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AtrinikClient(ClientConfig(
                character="Sera", runtime_state_path=str(Path(directory) / "state.json")))
            client.state.player_name = "Sera"
            client.load_character_memory("Sera")
            client.state.map = MapState(path="/shattered_islands/incuna/apartments/beach_nook",
                                        world_x=3, world_y=2)
            # Another player quoting the confirmation does not move it.
            payload = bytes([2]) + b"ffffff\0Zoey says: You save and your save bed location is updated.\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual(client.state.savebed_map, "")
            payload = bytes([2]) + b"ffffff\0You save and your save bed location is updated.\0"
            asyncio.run(client._handle_drawinfo(Cursor(payload), payload))
            self.assertEqual((client.state.savebed_map, client.state.savebed_xy),
                             ("/shattered_islands/incuna/apartments/beach_nook", (3, 2)))

    def test_farm_circuit_keeps_a_savebed_that_is_nearly_as_close(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        # The starter apartment is 16 moves out; the inn's 1 saves under 20.
        client.state.savebed_map, client.state.savebed_xy = STARTER_APARTMENT, (1, 0)
        client.state.apartment_returns = {STARTER_APARTMENT: ("/far", (0, 0))}
        circuit = FarmCircuitTask(self.savebed_graph(far_width=15), [("/farm", "rat")])
        search = circuit._savebed_search(client).run()
        self.assertEqual((search.result, search.current_walk), (("/inn", (2, 0), 1), 16))
        self.assertFalse(circuit._needs_savebed(client))

    def test_real_strakewood_circuit_prefers_a_public_bed_over_the_far_apartment(self):
        from .navigation import nearest_savebed, savebed_walk

        graph = built_graph()
        spot = ["/shattered_islands/world_0_50", "/shattered_islands/world_0_51",
                "/shattered_islands/world_1_50", "/shattered_islands/world_1_51"]
        # Live on 2026-10-17, leaving the starter apartment returned Sera to
        # Strakewood's world_0_68, not to the graph's static Asteria exit.
        town = graph.nodes["/shattered_islands/world_0_68"]
        entrance = next(point for point in sorted(town.terrain) if town.walkable(*point))
        returns = {STARTER_APARTMENT: ("/shattered_islands/world_0_68", entrance)}
        bed_map, _, tiles = nearest_savebed(graph, spot, returns=returns)
        self.assertNotIn("/apartments/", bed_map)
        self.assertNotIn("/house/", bed_map)
        apartment = savebed_walk(graph, STARTER_APARTMENT, STARTER_APARTMENT_BED, spot,
                                 returns=returns)
        self.assertLessEqual(tiles + FarmCircuitTask.SAVEBED_MIN_GAIN_TILES, apartment)

    def test_dashboard_trace_prefers_active_service_over_dormant_farm(self):
        graph = WorldGraph()
        graph.nodes["/farm"] = MapNode("/farm", width=2, height=2)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        circuit.child = circuit._new_child()
        circuit._storage = NavigateThenTask(
            graph, "/farm", DepositItemsTask("chest", unidentified_only=True))
        trace = DashboardState.task_trace(circuit)
        self.assertEqual(trace[1]["class"], "NavigateThenTask")
        self.assertIn("deposit:chest", trace[1]["name"])
        self.assertNotIn("farm:rat", [entry["name"] for entry in trace])
        self.assertEqual(trace[0]["circuit"]["phase"], "maintenance")
        self.assertEqual(trace[0]["circuit"]["dwell_seconds"], 0.0)

        circuit._storage = None
        circuit._identification = NavigateThenTask(
            graph, "/farm", MassIdentifyTask("Kulgar", 1_450))
        trace = DashboardState.task_trace(circuit)
        self.assertIn("mass-identify:Kulgar", trace[1]["name"])
        self.assertNotIn("farm:rat", [entry["name"] for entry in trace])

        circuit._identification = None
        circuit.child.navigation.complete()
        circuit._current_map_path = "/farm"
        circuit._farm_started_at = time.monotonic() - 5.0
        circuit._starting_exp = 100
        circuit._current_exp = 460
        circuit._xp_started_at = time.monotonic() - 3600.0
        trace = DashboardState.task_trace(circuit)
        self.assertEqual(trace[0]["circuit"]["phase"], "farming")
        self.assertGreaterEqual(
            trace[0]["circuit"]["dwell_seconds"], 5.0)
        self.assertEqual(trace[0]["circuit"]["xp_gained"], 360)
        self.assertAlmostEqual(
            trace[0]["circuit"]["xp_per_hour"], 360.0, delta=0.2)

    def test_dashboard_trace_exposes_shop_and_navigation_progress(self):
        graph = WorldGraph()
        graph.nodes["/shop"] = MapNode("/shop", width=3, height=3)
        graph.nodes["/shop"].terrain = {
            (x, y): 1 for x in range(3) for y in range(3)}
        graph.shop_stocks["/shop"] = [(0, 0), (1, 1), (2, 2)]
        sweep = ShopUpgradeSweepTask(graph, ("/shop",), start_index=1)
        trace = DashboardState.task_trace(sweep)
        self.assertEqual(trace[0]["shop_sweep"]["waypoint"], 2)
        self.assertEqual(trace[0]["shop_sweep"]["waypoints"], 3)
        self.assertEqual(
            trace[0]["shop_sweep"]["current"]["coordinates"], [1, 1])

        navigation = NavigateTask(graph, "/shop", (2, 2))
        navigation._runtime_blocked.update({(1, 0), (1, 1)})
        navigation._temporary_blocked.add((2, 1))
        navigation._issued_goal = ("/shop", 2, 2)
        navigation._issued_click = ("/shop", 0, 1)
        navigation._route_threat_maps = {"/wolf-map"}
        navigation._threat_fallback = True
        navigation.route = [
            MapEdge("/shop", "/shop", 2, 2, kind="exit",
                    label="test stairs")]
        nav_trace = DashboardState.task_trace(navigation)[0]
        self.assertEqual(nav_trace["navigation"]["remaining_edges"], 1)
        self.assertEqual(
            nav_trace["navigation"]["runtime_blocked"],
            [[1, 0], [1, 1]])
        self.assertEqual(
            nav_trace["navigation"]["temporary_blocked"], [[2, 1]])
        self.assertEqual(
            nav_trace["navigation"]["current_edge"]["label"],
            "test stairs")
        self.assertIn("live blockers 2", nav_trace["detail"])
        self.assertIn("occupants 1", nav_trace["detail"])
        self.assertIn("route threats 1 (fallback)", nav_trace["detail"])
        self.assertEqual(
            nav_trace["navigation"]["threat_maps"], ["/wolf-map"])
        self.assertTrue(nav_trace["navigation"]["threat_fallback"])
        self.assertEqual(
            nav_trace["navigation"]["failed_tile_crossings"], [])
        self.assertIn("seam retries 0", nav_trace["detail"])
        self.assertIn("progress pending", nav_trace["detail"])
        self.assertIsNone(
            nav_trace["navigation"]["progress_age_seconds"])

        navigation._last_progress = time.monotonic() - 2.0
        progressed_trace = DashboardState.task_trace(navigation)[0]
        self.assertAlmostEqual(
            progressed_trace["navigation"]["progress_age_seconds"],
            2.0, delta=0.2)
        self.assertNotIn("progress pending", progressed_trace["detail"])

        navigation.complete()
        completed_trace = DashboardState.task_trace(navigation)[0]
        self.assertIn("complete", completed_trace["detail"])
        self.assertIn("occupants 0", completed_trace["detail"])
        self.assertEqual(
            completed_trace["navigation"]["temporary_blocked"], [])
        self.assertEqual(
            completed_trace["navigation"]["remaining_edges"], 0)
        self.assertIsNone(
            completed_trace["navigation"]["current_edge"])
        self.assertIsNone(
            completed_trace["navigation"]["progress_age_seconds"])

    def test_dashboard_trace_exposes_farm_and_circuit_debug_state(self):
        graph = WorldGraph()
        graph.nodes["/farm"] = MapNode("/farm", width=2, height=2)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")],
                                  clock=lambda: 100.0)
        circuit.server_clock.anchor_minute = 13 * 60 + 42
        circuit.server_clock.anchor_at = 100.0
        circuit.child = circuit._new_child()
        circuit.child.task._visible_target_count = 2
        circuit.child.task._engaged_target = (77, "/farm", 1, 1, 99.0)
        circuit.child.task._corpse_take_all[88] = (1, 98.0)
        trace = DashboardState.task_trace(circuit)
        self.assertIn("server 13:42", trace[0]["detail"])
        farm = next(entry for entry in trace
                    if entry["class"] == "FarmTask")
        self.assertEqual(farm["farm"]["visible_targets"], 2)
        self.assertEqual(farm["farm"]["engaged_target"], 77)
        self.assertEqual(farm["farm"]["corpse_phase_tags"], [88])
        self.assertIn("corpse phases 1", farm["detail"])

        circuit._utility_shopping = NavigateThenTask(
            graph, "/farm",
            BuyDialogueStockTask("sage", r"identify"))
        trace = DashboardState.task_trace(circuit)
        self.assertEqual(trace[0]["circuit"]["phase"], "maintenance")
        self.assertTrue(any(
            entry["class"] == "BuyDialogueStockTask" for entry in trace))

    def test_farm_circuit_empty_corpse_does_not_block_service_detour(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        corpse = Item(600, name="decaying corpse")
        client.state.place_item(corpse, 0)
        farm = FarmTask(zone="/test")
        self.assertFalse(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, farm))
        client.state.place_item(Item(601, name="copper coin"), corpse.tag)
        self.assertTrue(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, farm))
        client.state.remove_item(600)
        farm._corpse_take_all[600] = (1, 0)
        self.assertFalse(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, farm))

    def test_friendly_npc_selection_does_not_starve_maintenance(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.target_id = 500
        client.state.stats["target_hp"] = 100
        client.state.combat = False
        farm = FarmTask(zone="/test")
        self.assertFalse(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, farm))
        client.state.combat = True
        self.assertTrue(FarmCircuitTask._busy_finishing_fight_or_loot(
            client, farm))

    def test_navigation_spot_catalog_is_authored_and_unique(self):
        graph = built_graph()
        self.assertGreaterEqual(len(NAVIGATION_SPOTS), 30)
        self.assertEqual(len({spot.id for spot in NAVIGATION_SPOTS}),
                         len(NAVIGATION_SPOTS))
        self.assertFalse([spot.destination for spot in NAVIGATION_SPOTS
                          if spot.destination not in graph.nodes])
        self.assertTrue({"town", "service", "transport", "quest",
                         "dungeon", "boss"}.issubset(
                             {spot.category for spot in NAVIGATION_SPOTS}))

    def test_every_policy_destination_is_authored(self):
        graph = built_graph()
        for policy in POLICIES.values():
            destinations = [policy.start.place]
            for part in policy.parts.values():
                destinations.extend((part.action.place, part.turnin_place))
            for place in destinations:
                if place is not None:
                    self.assertIn(place.map_path, graph.nodes)

    def test_lost_memories_uses_quest_sam_not_departure_sam(self):
        dockside = (CONTENT_ROOT /
                    "maps/shattered_islands/world_4_85").read_text()
        departure = (CONTENT_ROOT /
                     "maps/shattered_islands/world_0_83").read_text()
        self.assertIn(
            "race /interfaces/quests/lost_memories/quest.xml", dockside)
        self.assertIn(
            "race /interfaces/ocean/sam_goodberry_to_strakewood.xml",
            departure)
        self.assertEqual(POLICIES["Lost Memories"].start.place.map_path,
                         "/shattered_islands/world_4_85")

    def test_dialog_accepts_equivalent_authored_responses(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.map = type("Map", (), {"path": "/test"})()
                self.state.interface = None
                self.talks = []
                self.chosen = []

            async def talk(self, text, npc=""):
                self.talks.append((text, npc))

            async def choose_interface_link(self, index):
                self.chosen.append(index)
                link = self.state.interface.links[index]
                if "[a=close:" in link:
                    self.state.interface = None

        client = FakeClient()
        task = DialogTask("Sam Goodberry", choices=(r"=:remember\]",))
        asyncio.run(task.tick(client))
        client.state.interface = InterfaceState(
            title="Sam Goodberry", text="Has your memory improved?",
            links=[
                "I feel I'm starting to remember... [=:remember]",
                "Not really... [=:remember]",
            ],
        )
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(client.chosen, [0])
        client.state.interface = InterfaceState(
            title="Sam Goodberry", text="Visit Brelend Lee.",
            links=["I'll do that. [a=close:]Continue[/a]"],
        )
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        self.assertEqual(client.chosen, [0, 0])
        self.assertEqual(task.status, TaskStatus.COMPLETE)

    def test_dialog_policy_order_selects_cross_part_response(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.map = type("Map", (), {"path": "/test"})()
                self.state.interface = InterfaceState(
                    title="Brelend Lee", text="Do you feel better?",
                    links=[
                        "[a=:headhealed]My head doesn't hurt anymore![/a]",
                        "[a=:nodifferent]No different...[/a]",
                    ],
                )
                self.chosen = []

            async def talk(self, text, npc=""):
                pass

            async def choose_interface_link(self, index):
                self.chosen.append(index)

        client = FakeClient()
        task = DialogTask("Brelend Lee", choices=(
            r"=:headhealed\]", r"=:nodifferent\]"))
        task._sent_hello = True
        asyncio.run(task.tick(client))
        self.assertEqual(client.chosen, [0])
        self.assertEqual(task.status, TaskStatus.RUNNING)

    def test_ken_confirmation_prefers_yes_over_gearup_back_edge(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.map = type("Map", (), {"path": "/test"})()
                self.state.interface = InterfaceState(
                    title="Ken Berger", text="Are you sure?",
                    links=[
                        "[a=:confirm_sword]Yes.[/a]",
                        "[a=:gearup]No. <go back>[/a]",
                    ],
                )
                self.chosen = []

            async def talk(self, text, npc=""):
                pass

            async def choose_interface_link(self, index):
                self.chosen.append(index)

        choices = POLICIES["Lost Memories"].parts["Gearing Up"].action.choices
        client = FakeClient()
        task = DialogTask("Ken Berger", choices=choices)
        task._sent_hello = True
        asyncio.run(task.tick(client))
        self.assertEqual(client.chosen, [0])
        self.assertEqual(task.status, TaskStatus.RUNNING)


class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = built_graph()

    def test_parallel_map_parsing_matches_sequential_order(self):
        import os
        from . import navigation
        paths = list(navigation.map_files())[:96]
        with patch.dict(os.environ, {"ATRINIK_GRAPH_WORKERS": "1"}):
            sequential = navigation._parse_map_files(paths)
        with patch.dict(os.environ, {"ATRINIK_GRAPH_WORKERS": "3"}):
            parallel = navigation._parse_map_files(paths)
        self.assertEqual(parallel, sequential)
        for value, expected in (("4", 4), ("0", 1), ("bogus", 1)):
            with patch.dict(os.environ, {"ATRINIK_GRAPH_WORKERS": value}):
                self.assertEqual(navigation._graph_parse_workers(), expected)

    def test_world_graph(self):
        self.assertGreaterEqual(len(self.graph.nodes), 3600)
        route = self.graph.route(
            "/shattered_islands/deserted_tutorial_island/ship_lower_deck",
            "/shattered_islands/world_-7_76",
        )
        self.assertEqual(len(route), 1)
        self.assertEqual((route[0].x, route[0].y), (1, 6))

    def test_content_attr_preserves_scalar_archetype_values(self):
        self.assertEqual(content_attr({"type": "15"}, "type"), "15")
        self.assertEqual(
            content_attr({"face": "shortsword.101"}, "face"),
            "shortsword.101")
        self.assertEqual(content_attr({"level": ["1", "10"]}, "level"),
                         "10")
        self.assertEqual(content_attr({}, "missing", "fallback"),
                         "fallback")

    def test_authored_named_spawn_points_are_extracted(self):
        fahrgorm = self.graph.farm_priorities(
            "/shattered_islands/world_3_68",
            "Fahrgorm|evil treant|quickwood")
        thrakir = self.graph.farm_priorities(
            "/shattered_islands/world_3_69", "Thrakir|lost soul")
        self.assertEqual((fahrgorm[0].x, fahrgorm[0].y,
                          fahrgorm[0].named), (12, 11, "Fahrgorm"))
        self.assertEqual(fahrgorm[0].level, 6)
        self.assertIn("evil treant", [
            name.casefold() for name in fahrgorm[0].candidates])
        self.assertEqual((thrakir[0].x, thrakir[0].y,
                          thrakir[0].named), (4, 2, "Thrakir"))
        self.assertEqual(thrakir[0].level, 7)
        self.assertIn("lost soul", [
            name.casefold() for name in thrakir[0].candidates])
        graveyard = self.graph.farm_priorities(
            "/shattered_islands/world_1_58", "skeleton")
        self.assertEqual(len(graveyard), 13)
        self.assertTrue(all(
            (spawn.start_minute, spawn.end_minute) == (19 * 60, 7 * 60)
            for spawn in graveyard))
        self.assertEqual({spawn.level for spawn in graveyard}, {6, 7})

    def test_map_override_caster_identity_is_compiled(self):
        node = self.graph.nodes[
            "/shattered_islands/strakewood_island/dark_cave/"
            "dark_cave_0101"]
        self.assertIn("fire wyvern", node.caster_identities)

    def test_auto_connected_shop_mat_routes_out_of_shop_component(self):
        source = "/shattered_islands/world_0_69"
        route = self.graph.route_points(
            source, (16, 8), "/shattered_islands/world_3_68", [])
        self.assertTrue(route)
        shop_edges = [edge for edge in route
                      if edge.source == source and edge.destination == source]
        self.assertTrue(shop_edges)
        self.assertIn("auto-connected", shop_edges[0].label)
        self.assertEqual((shop_edges[0].x, shop_edges[0].y), (15, 9))
        self.assertEqual(
            (shop_edges[0].destination_x, shop_edges[0].destination_y),
            (15, 11))

    def test_local_shop_path_does_not_cross_automatic_exit(self):
        source = "/shattered_islands/world_5_58"
        # Entering (5, 17) immediately teleports to (3, 17). The ordinary
        # path between two stock tiles inside the shop must walk around that
        # mat instead of planning an impossible post-teleport step.
        path = self.graph.local_path(source, (5, 21), (6, 18))
        self.assertTrue(path)
        self.assertNotIn((5, 17), path)
        self.assertEqual(path[-1], (6, 18))

        # From the opposite component, routing to the same stock tile must
        # deliberately use the paired shop-mat edge.
        route = self.graph.route_points(
            source, (3, 19), source, [(6, 18)])
        self.assertEqual(len(route), 1)
        self.assertTrue(route[0].automatic)
        self.assertEqual((route[0].x, route[0].y), (3, 17))
        self.assertEqual(
            (route[0].destination_x, route[0].destination_y), (5, 17))

    def test_apartment_is_destination_not_cross_world_shortcut(self):
        destination = "/shattered_islands/world_5_58"
        route = self.graph.route_points(
            "/shattered_islands/world_0_68", (4, 8),
            destination, [(9, 17)])
        self.assertTrue(route)
        self.assertFalse([
            edge for edge in route
            if "/apartments/apartment_" in edge.destination
        ])
        # Starting inside still has a usable exit. Its authored destination is
        # only a placeholder; after crossing, NavigateTask replans from the
        # actual per-player return map supplied by the server.
        inside = self.graph.route_points(
            "/shattered_islands/strakewood_island/apartments/apartment_cheap",
            (1, 2), destination, [(9, 17)])
        self.assertTrue(inside)
        self.assertEqual(
            inside[0].source,
            "/shattered_islands/strakewood_island/apartments/apartment_cheap")
        self.assertFalse([
            edge for edge in inside[1:]
            if "/apartments/apartment_" in edge.destination
        ])

    def test_scripted_brynknot_apartment_teleporter_is_routable(self):
        destination = (
            "/shattered_islands/strakewood_island/apartments/"
            "apartment_cheap"
        )
        route = self.graph.route_points(
            "/shattered_islands/world_2_69", (21, 12),
            destination, [(4, 1)],
        )
        self.assertTrue(route)
        self.assertEqual(route[-1].destination, destination)
        self.assertEqual((route[-1].x, route[-1].y), (4, 9))
        self.assertEqual(
            (route[-1].destination_x, route[-1].destination_y), (1, 2))
        self.assertIn("scripted apartment", route[-1].label)

    def test_scripted_apartment_return_is_routable(self):
        apartment = (
            "/shattered_islands/strakewood_island/apartments/"
            "apartment_cheap"
        )
        route = self.graph.route_points(
            apartment, (4, 1), "/shattered_islands/world_0_68", [(4, 8)])
        self.assertTrue(route)
        self.assertEqual(route[-1].source, apartment)
        self.assertEqual(route[-1].destination,
                         "/shattered_islands/world_0_68")
        self.assertEqual((route[-1].x, route[-1].y), (1, 1))
        self.assertEqual(
            (route[-1].destination_x, route[-1].destination_y), (4, 8))
        self.assertIn("scripted apartment return", route[-1].label)

    def test_old_outpost_storage_uses_nearby_aris_apartment_portal(self):
        apartment = (
            "/shattered_islands/strakewood_island/apartments/"
            "apartment_cheap")
        route = self.graph.route(
            "/shattered_islands/strakewood_island/old_outpost/"
            "old_outpost_0101",
            apartment)
        self.assertEqual(len(route), 4)
        self.assertEqual(route[-1].source,
                         "/shattered_islands/world_4_58")
        self.assertEqual(route[-1].destination, apartment)

    def test_coordinate_map_parser_matches_multilevel_server_names(self):
        self.assertEqual(
            _coordinate_name("/maps/underground_city_2_3_-1"),
            ("underground_city", 2, 3, -1),
        )
        self.assertEqual(_coordinate_name("/maps/world_4_83"),
                         ("world", 4, 83, 0))
        self.assertIsNone(_coordinate_name("/maps/old_outpost_0101"))

    def test_all_formal_quest_action_maps_are_graph_connected(self):
        hub = "/shattered_islands/world_4_84"
        for policy in POLICIES.values():
            actions = [policy.start]
            actions.extend(part.action for part in policy.parts.values())
            for action in actions:
                if action.place is not None:
                    self.graph.route(hub, action.place.map_path)

    def test_scripted_nyhelobo_portal_is_routable(self):
        destination = (
            "/shattered_islands/strakewood_island/brynknot/sewers/"
            "lab_nyhelobo"
        )
        route = self.graph.route("/shattered_islands/world_4_84", destination)
        self.assertEqual(route[-1].destination, destination)
        self.assertEqual((route[-1].x, route[-1].y), (23, 12))
        self.assertIn("scripted", route[-1].label)

    def test_adjusted_boundary_path_honors_exact_crossing_row(self):
        path = self.graph.local_path(
            "/shattered_islands/world_3_84", (23, 11), (24, 16))
        self.assertTrue(path)
        self.assertEqual(path[-1], (23, 16))

    def test_incuna_border_preserves_separate_street_and_church_crossings(self):
        edge = next(edge for edge in self.graph.nodes[
            "/shattered_islands/world_4_83"].edges
                    if edge.destination == "/shattered_islands/world_4_84")
        transitions = self.graph._tile_transitions(edge, (0, 7))
        arrivals = {arrival for _, arrival in transitions}
        self.assertIn((8, 0), arrivals)
        self.assertTrue(any(x >= 12 for x, _ in arrivals))

    def test_boundary_route_avoids_authored_living_arrival_blocker(self):
        source = "/shattered_islands/world_4_83"
        destination = "/shattered_islands/world_3_83"
        self.assertIn((23, 16), self.graph.nodes[destination].occupied)
        route = self.graph.route_points(
            source, (1, 16), destination, [])
        self.assertEqual(route[0].destination, destination)
        self.assertNotEqual((route[0].x, route[0].y), (-1, 16))

    def test_component_route_enters_incuna_church(self):
        route = self.graph.route_points(
            "/shattered_islands/world_4_83",
            (0, 7),
            "/shattered_islands/world_4_84",
            [(9, 8)],
        )
        self.assertEqual((route[0].x, route[0].y), (8, 24))

    def test_navigation_recovers_from_stale_component_cache(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_84",
                    "world_x": 0, "world_y": 11,
                })()

        source = "/shattered_islands/world_4_84"
        self.graph._component_cache[
            (source, False, (0, 11))] = frozenset()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_4_85_-2",
            allow_locked=True)
        route = task._plan(FakeClient())
        self.assertTrue(route)
        self.assertEqual(route[-1].destination,
                         "/shattered_islands/world_4_85_-2")

    def test_navigation_allows_server_click_path_to_finish(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_83",
                    "world_x": 11, "world_y": 22,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []
                self.clears = 0

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(self.graph, "/shattered_islands/world_4_84")
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.steps), 1)
        _, client.state.map.world_x, client.state.map.world_y = task._issued_click
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.steps), 2)

    def test_a_lesson_never_trades_one_learned_map_for_more_relaxed_hazards(self):
        def lane(path):
            node = MapNode(path, width=3, height=1)
            node.terrain = {(x, 0): 1 for x in range(3)}
            return node

        graph = WorldGraph()
        town, farm = "/shattered_islands/world_0_68", "/shattered_islands/world_1_50"
        swamp, ogre = "/shattered_islands/world_1_51", "/shattered_islands/world_1_52"
        detour = [f"/shattered_islands/world_3_{n}" for n in range(3)]
        for path in (town, farm, swamp, ogre, *detour):
            graph.nodes[path] = lane(path)
            graph.named_spawns[path] = []
        # Every way crosses over-levelled maps: the short one, the learned swamp
        # and one ogre map; the detour, three ogre maps.
        graph.nodes[town].edges = [MapEdge(town, swamp, 2, 0, 0, 0), MapEdge(town, detour[0], 0, 0, 2, 0)]
        graph.nodes[swamp].edges = [MapEdge(swamp, ogre, 2, 0, 0, 0)]
        graph.nodes[ogre].edges = [MapEdge(ogre, farm, 2, 0, 0, 0)]
        graph.nodes[detour[0]].edges = [MapEdge(detour[0], detour[1], 2, 0, 0, 0)]
        graph.nodes[detour[1]].edges = [MapEdge(detour[1], detour[2], 2, 0, 0, 0)]
        graph.nodes[detour[2]].edges = [MapEdge(detour[2], farm, 2, 0, 0, 0)]
        for path in (ogre, *detour):
            graph.map_monster_levels[path] = {"ogre": 99}

        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(path=town, width=17, height=17, world_x=1, world_y=0)
                self.record_action = lambda action, detail="": None

        client = FakeClient()
        client.state.route_hazards = {swamp: time.time() + 3600}
        route = [edge.destination for edge in NavigateTask(graph, farm)._plan(client)]
        # Two known threats (the lesson and one ogre map) beat three ogre maps.
        self.assertEqual(route, [swamp, ogre, farm])

    def test_learned_route_hazards_are_avoided_but_never_strand_her(self):
        def lane(path, width=3):
            node = MapNode(path, width=width, height=1)
            node.terrain = {(x, 0): 1 for x in range(width)}
            return node

        graph = WorldGraph()
        town, farm = "/shattered_islands/world_0_68", "/shattered_islands/world_1_50"
        swamp, detour = "/shattered_islands/world_1_51", "/shattered_islands/world_2_51"
        nodes = {path: lane(path) for path in (town, farm, swamp, detour)}
        graph.nodes.update(nodes)
        for path in nodes:
            graph.named_spawns[path] = []
        # The swamp is the short way; the detour is longer but safe.
        nodes[town].edges.append(MapEdge(town, swamp, 2, 0, 0, 0))
        nodes[swamp].edges.append(MapEdge(swamp, farm, 2, 0, 0, 0))
        nodes[town].edges.append(MapEdge(town, detour, 0, 0, 2, 0))
        nodes[detour].edges.append(MapEdge(detour, "/x", 2, 0, 0, 0))
        graph.nodes["/x"] = lane("/x")
        graph.named_spawns["/x"] = []
        graph.nodes["/x"].edges.append(MapEdge("/x", farm, 2, 0, 0, 0))

        class FakeClient:
            def __init__(self, path):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(path=path, width=17, height=17,
                                          world_x=1, world_y=0)

        client = FakeClient(town)
        route = NavigateTask(graph, farm)._plan(client)
        self.assertIn(swamp, [edge.destination for edge in route])
        client.state.route_hazards = {swamp: time.time() + 3600}
        route = NavigateTask(graph, farm)._plan(client)
        self.assertNotIn(swamp, [edge.destination for edge in route])
        # The only way still goes through it.
        nodes[town].edges = [edge for edge in nodes[town].edges if edge.destination == swamp]
        graph._world_search_snapshot = None
        route = NavigateTask(graph, farm)._plan(client)
        self.assertEqual([edge.destination for edge in route], [swamp, farm])
        # Never worse than before: when the only way also crosses an
        # over-levelled map, the planner still routes as it did without lessons.
        ogre = "/shattered_islands/world_1_52"
        graph.nodes[ogre] = lane(ogre)
        graph.named_spawns[ogre] = []
        nodes[town].edges = [MapEdge(town, swamp, 2, 0, 0, 0)]
        nodes[swamp].edges = [MapEdge(swamp, ogre, 2, 0, 0, 0)]
        graph.nodes[ogre].edges = [MapEdge(ogre, farm, 2, 0, 0, 0)]
        graph.map_monster_levels[ogre] = {"ogre": 99}
        graph._world_search_snapshot = None
        graph._component_cache.clear()
        without = NavigateTask(graph, farm)
        client.state.route_hazards = {}
        expected = [edge.destination for edge in without._plan(client)]
        client.state.route_hazards = {swamp: time.time() + 3600}
        records = []
        client.record_action = lambda action, detail="": records.append(action)
        self.assertEqual([edge.destination for edge in NavigateTask(graph, farm)._plan(client)],
                         expected)
        # The lesson never trades a known pack hazard for itself: with the
        # learned map on the safe way and a dense tunnel on the other, she
        # plans as she would without the lesson.
        tunnel = "/shattered_islands/world_4_53_-2"
        graph.nodes[tunnel] = lane(tunnel)
        graph.named_spawns[tunnel] = []
        graph.transit_max_aggro_pack = lambda path: 5 if path == tunnel else 0
        graph.map_monster_levels.pop(ogre, None)
        nodes[town].edges = [MapEdge(town, swamp, 2, 0, 0, 0), MapEdge(town, tunnel, 0, 0, 2, 0)]
        nodes[swamp].edges = [MapEdge(swamp, farm, 2, 0, 0, 0)]
        graph.nodes[tunnel].edges = [MapEdge(tunnel, farm, 2, 0, 0, 0)]
        graph._world_search_snapshot = None
        graph._component_cache.clear()
        client.state.route_hazards = {}
        expected = [edge.destination for edge in NavigateTask(graph, farm)._plan(client)]
        self.assertNotIn(tunnel, expected)
        client.state.route_hazards = {swamp: time.time() + 3600}
        self.assertEqual([edge.destination for edge in NavigateTask(graph, farm)._plan(client)],
                         expected)
        graph.transit_max_aggro_pack = lambda path: 0
        # A discarded speculative plan leaves no threat-fallback record.
        nodes[town].edges.append(MapEdge(town, "/x", 0, 0, 2, 0))
        graph.nodes["/x"].edges = [MapEdge("/x", farm, 2, 0, 0, 0)]
        graph._world_search_snapshot = None
        graph._component_cache.clear()
        client.state.route_hazards = {"/x": time.time() + 3600}
        records.clear()
        NavigateTask(graph, farm)._plan(client)
        self.assertNotIn("navigation-threat-fallback", records)
        # Expired lessons are gone; live ones count wherever they are, the
        # destination and her own map included (as costs, never walls).
        client.state.route_hazards = {swamp: time.time() - 1, farm: time.time() + 3600,
                                      town: time.time() + 3600}
        self.assertEqual(NavigateTask(graph, farm)._learned_route_hazards(client), {farm, town})

    def test_route_lessons_are_costs_that_pick_the_fewest_crossings(self):
        town, farm = "/shattered_islands/world_0_68", "/shattered_islands/world_1_50"

        def build(edges):
            graph = WorldGraph()
            for path in {farm, *(edge.source for edge in edges), *(edge.destination for edge in edges)}:
                width = 5 if path == town else 3
                node = MapNode(path, width=width, height=1)
                # Town is split by a wall at x=2; she stands in its west part.
                node.terrain = {(x, 0): 1 for x in range(width) if not (path == town and x == 2)}
                graph.nodes[path] = node
                graph.named_spawns[path] = []
            for edge in edges:
                graph.nodes[edge.source].edges.append(edge)
            return graph

        class FakeClient:
            def __init__(self, hazards):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(path=town, width=17, height=17, world_x=1, world_y=0)
                self.state.route_hazards = {path: time.time() + 3600 for path in hazards}

        a, b, c, e, f = (f"/shattered_islands/world_5_{n}" for n in range(5))
        # Two lessons on the short corridor, one on the long one: one search
        # crosses the fewest, whatever subsets would route.
        graph = build([MapEdge(town, a, 0, 0, 0, 0), MapEdge(a, b, 2, 0, 0, 0), MapEdge(b, farm, 2, 0, 0, 0),
                       MapEdge(town, c, 1, 0, 0, 0), MapEdge(c, e, 2, 0, 0, 0), MapEdge(e, f, 2, 0, 0, 0),
                       MapEdge(f, farm, 2, 0, 0, 0)])
        task = NavigateTask(graph, farm)
        self.assertEqual([edge.destination for edge in task._plan(FakeClient({a, b, c}))], [c, e, f, farm])
        self.assertTrue(task._threat_fallback)
        # Leaving the map she stands on is free, but a shortcut that re-enters
        # it (through its other, walled-off part) pays like any other lesson.
        d, p, q, r = (f"/shattered_islands/world_6_{n}" for n in range(4))
        graph = build([MapEdge(town, d, 0, 0, 0, 0), MapEdge(d, town, 2, 0, 4, 0), MapEdge(town, farm, 4, 0, 0, 0),
                       MapEdge(town, p, 1, 0, 0, 0), MapEdge(p, q, 2, 0, 0, 0), MapEdge(q, r, 2, 0, 0, 0),
                       MapEdge(r, farm, 2, 0, 0, 0)])
        task = NavigateTask(graph, farm)
        self.assertEqual([edge.destination for edge in task._plan(FakeClient(set()))], [d, town, farm])
        task = NavigateTask(graph, farm)
        self.assertEqual([edge.destination for edge in task._plan(FakeClient({town}))], [p, q, r, farm])
        self.assertFalse(task._threat_fallback)

    def test_an_unavoidable_lesson_keeps_the_others(self):
        def lane(path, width=3):
            node = MapNode(path, width=width, height=1)
            node.terrain = {(x, 0): 1 for x in range(width)}
            return node

        graph = WorldGraph()
        town, gate, hub = ("/shattered_islands/world_0_68", "/shattered_islands/world_0_69",
                           "/shattered_islands/world_0_70")
        swamp, detour, farm = ("/shattered_islands/world_1_70", "/shattered_islands/world_2_70",
                               "/shattered_islands/world_1_50")
        for path in (town, gate, hub, swamp, detour, farm):
            graph.nodes[path] = lane(path)
            graph.named_spawns[path] = []
        # The gate is the only way out of town; past the hub, the swamp is
        # the short way and the detour the safe one.
        graph.nodes[town].edges = [MapEdge(town, gate, 2, 0, 0, 0)]
        graph.nodes[gate].edges = [MapEdge(gate, hub, 2, 0, 0, 0)]
        graph.nodes[hub].edges = [MapEdge(hub, swamp, 2, 0, 0, 0), MapEdge(hub, detour, 0, 0, 2, 0)]
        graph.nodes[swamp].edges = [MapEdge(swamp, farm, 2, 0, 0, 0)]
        graph.nodes[detour].edges = [MapEdge(detour, "/x", 2, 0, 0, 0)]
        graph.nodes["/x"] = lane("/x")
        graph.named_spawns["/x"] = []
        graph.nodes["/x"].edges = [MapEdge("/x", farm, 2, 0, 0, 0)]

        class FakeClient:
            def __init__(self, path):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(path=path, width=17, height=17,
                                          world_x=1, world_y=0)

        client = FakeClient(town)
        # The unavoidable gate sorts first among six lessons; every single
        # relaxation is tried, so the five avoidable ones still hold.
        extra = [f"/shattered_islands/world_9_{n}" for n in range(4)]
        client.state.route_hazards = {path: time.time() + 3600
                                      for path in (gate, swamp, *extra)}
        self.assertLess(gate, min([swamp, *extra]))
        task = NavigateTask(graph, farm)
        route = [edge.destination for edge in task._plan(client)]
        # The gate cannot be avoided, but the swamp lesson still holds.
        self.assertIn(gate, route)
        self.assertNotIn(swamp, route)
        # Crossing a map that killed her is not threat-free for optional trips.
        self.assertTrue(task._threat_fallback)
        # Two unavoidable lessons (gate and hub) do not discard the avoidable
        # swamp: every trial missing one still holds a bottleneck.
        client.state.route_hazards = {path: time.time() + 3600 for path in (gate, hub, swamp)}
        task = NavigateTask(graph, farm)
        route = [edge.destination for edge in task._plan(client)]
        self.assertTrue({gate, hub} <= set(route))
        self.assertNotIn(swamp, route)
        client.state.route_hazards = {}
        task = NavigateTask(graph, farm)
        task._plan(client)
        self.assertFalse(task._threat_fallback)
        # A destination that killed her is reachable but not threat-free.
        client.state.route_hazards = {farm: time.time() + 3600}
        task = NavigateTask(graph, farm)
        self.assertEqual([edge.destination for edge in task._plan(client)][-1], farm)
        self.assertTrue(task._threat_fallback)

    def test_tiled_levels_are_pack_hazards_with_a_fallback_through_them(self):
        def lane(path, width=3):
            node = MapNode(path, width=width, height=1)
            node.terrain = {(x, 0): 1 for x in range(width)}
            return node

        graph = WorldGraph()
        town = "/shattered_islands/world_0_68"
        farm = "/shattered_islands/world_1_50"
        tunnel = "/shattered_islands/world_4_53_-2"
        west = "/shattered_islands/world_-2_57"
        nodes = {path: lane(path) for path in (town, farm, tunnel, west, "/interior/inn")}
        graph.nodes.update(nodes)
        for path in nodes:
            graph.named_spawns[path] = []
        graph.transit_max_aggro_pack = lambda path: 5 if path in (tunnel, west) else 0
        surface = NavigateTask(graph, farm)
        hazards = surface._known_pack_hazard_maps()
        self.assertIn(tunnel, hazards)
        self.assertIn(west, hazards)
        self.assertNotIn("/interior/inn", hazards)

        # Surface trip: the tunnel shortcut is avoided while a surface way
        # round exists ...
        nodes[town].edges.append(MapEdge(town, tunnel, 2, 0, 0, 0))
        nodes[tunnel].edges.append(MapEdge(tunnel, farm, 2, 0, 0, 0))
        nodes[town].edges.append(MapEdge(town, west, 0, 0, 2, 0))
        nodes[west].edges.append(MapEdge(west, "/interior/inn", 0, 0, 2, 0))
        nodes["/interior/inn"].edges.append(MapEdge("/interior/inn", farm, 0, 0, 0, 0))
        graph.transit_max_aggro_pack = lambda path: 5 if path == tunnel else 0

        class FakeClient:
            def __init__(self, path):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(path=path, width=17, height=17,
                                          world_x=1, world_y=0)

        route = NavigateTask(graph, farm)._plan(FakeClient(town))
        self.assertNotIn(tunnel, [edge.destination for edge in route])
        # ... but taken, avoiding the rest, when it is the only way.
        nodes[town].edges = [edge for edge in nodes[town].edges if edge.destination == tunnel]
        graph._world_search_snapshot = None
        route = NavigateTask(graph, farm)._plan(FakeClient(town))
        self.assertEqual([edge.destination for edge in route], [tunnel, farm])

        # When every way needs an over-levelled map, relaxing that hazard
        # still avoids the tunnel if a pack-free route exists, even though
        # the way through the tunnel would be shorter.
        outer = "/shattered_islands/world_2_60"
        other = "/shattered_islands/world_5_55"
        ring = ["/shattered_islands/world_3_60", "/shattered_islands/world_3_61"]
        for path in (outer, other, *ring):
            graph.nodes[path] = lane(path)
            graph.named_spawns[path] = []
        nodes[town].edges = [MapEdge(town, tunnel, 2, 0, 0, 0),
                             MapEdge(town, outer, 0, 0, 0, 0)]
        nodes[tunnel].edges = [MapEdge(tunnel, other, 2, 0, 0, 0)]
        graph.nodes[other].edges = [MapEdge(other, farm, 2, 0, 0, 0)]
        graph.nodes[outer].edges = [MapEdge(outer, ring[0], 2, 0, 0, 0)]
        graph.nodes[ring[0]].edges = [MapEdge(ring[0], ring[1], 2, 0, 0, 0)]
        graph.nodes[ring[1]].edges = [MapEdge(ring[1], farm, 2, 0, 0, 0)]
        graph._world_search_snapshot = None
        task = NavigateTask(graph, farm)
        task._known_overlevel_hazard_maps = lambda client: {outer, other}
        route = task._plan(FakeClient(town))
        self.assertEqual([edge.destination for edge in route], [outer, *ring, farm])

    def test_stalled_click_path_learns_live_blocker(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_3_81_-1",
                    "world_x": 10, "world_y": 16,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []
                self.clears = 0

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_3_81_-1", (23, 5),
            allow_locked=False)
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.steps), 1)
        task._last_progress -= task.STALL_RETRY_SECONDS + 0.1
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertIn((11, 16), task._runtime_blocked)
        self.assertEqual(len(client.steps), 2)

    def test_a_slow_walker_is_not_stalled_between_her_own_steps(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.stats = {"speed": 0.5}
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_3_81_-1",
                    "world_x": 10, "world_y": 16,
                    "width": 17, "height": 17,
                })()
                self.steps = []

            async def move_to_view(self, x, y):
                pass

            async def clear_actions(self):
                pass

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_3_81_-1", (23, 5),
            allow_locked=False)
        # At speed 0.5 one movement acknowledgement takes 1 / 0.5 + 0.5 s.
        self.assertAlmostEqual(task._stall_seconds(client), 2.5)
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.steps), 1)
        # Two seconds without a step is her normal pace, not a stall.
        task._last_progress -= 2.0
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(task._runtime_blocked, set())
        self.assertEqual(len(client.steps), 1)
        # Past one acknowledgement it is a stall again.
        task._last_progress -= 0.7
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertIn((11, 16), task._runtime_blocked)
        # Every speed gets its acknowledgement (speed 1.0: the 1.5 s floor),
        # never above the cap; an unknown speed keeps the base.
        client.state.stats = {"speed": 1.0}
        self.assertEqual(task._stall_seconds(client), 1.5)
        client.state.stats = {"speed": 0.05}
        self.assertEqual(task._stall_seconds(client), task.STALL_RETRY_MAX_SECONDS)
        client.state.stats = {}
        self.assertEqual(task._stall_seconds(client), task.STALL_RETRY_SECONDS)

    def test_navigation_waits_for_living_occupant_in_only_corridor(self):
        graph = WorldGraph()
        node = MapNode("/test/corridor", width=3, height=3)
        node.terrain = {(1, 0): 1, (1, 1): 1, (1, 2): 1}
        graph.nodes[node.path] = node

        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(
                    path=node.path, width=17, height=17,
                    world_x=1, world_y=1)
                self.state.map.tile(8, 9).objects[5] = MapObject(
                    5, 0, 0, 0, target_id=99)
                self.steps = []
                self.clears = 0
                self.actions = []

            async def move(self, direction):
                self.steps.append(direction)

            async def clear_actions(self):
                self.clears += 1

            def record_action(self, action, detail):
                self.actions.append((action, detail))

        client = FakeClient()
        task = NavigateTask(graph, node.path, (1, 2))
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(client.steps, [])
        self.assertEqual(task._temporary_blocked, {(1, 2)})
        self.assertIn(
            ("navigation-occupant-blocked", "1,2"), client.actions)

        # Once the creature moves, the transient blocker disappears and the
        # authored step proceeds without poisoning persistent blocker memory.
        client.state.map.tile(8, 9).objects.clear()
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [5])
        self.assertEqual(task._temporary_blocked, set())
        self.assertEqual(task._runtime_blocked, set())

    def test_learned_blocks_that_trap_the_character_are_reset(self):
        # Live on world_2_67: learned stall blocks ringed Sera while a visible
        # guard kept her on the occupant branch, cancelling for minutes.
        graph = WorldGraph()
        node = MapNode("/test/field", width=5, height=5)
        node.terrain = {(x, y): 1 for x in range(5) for y in range(5)}
        graph.nodes[node.path] = node

        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(
                    path=node.path, width=17, height=17, world_x=2, world_y=2)
                # A guard in view at world (0, 0), off the route.
                self.state.map.tile(6, 6).objects[5] = MapObject(
                    5, 0, 0, 0, target_id=99)
                self.steps = []
                self.clears = 0
                self.actions = []

            async def move(self, direction):
                self.steps.append(direction)

            async def clear_actions(self):
                self.clears += 1

            def record_action(self, action, detail):
                self.actions.append((action, detail))

        client = FakeClient()
        task = NavigateTask(graph, node.path, (4, 4))
        task._runtime_blocked = {
            (2 + dx, 2 + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
            if (dx, dy) != (0, 0)}
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [])
        self.assertEqual(len(task._runtime_blocked), 8)
        # Rotating approach candidates refreshes _last_progress; the reset
        # follows its own no-route timer instead.
        task._last_progress = time.monotonic()
        task._no_route_since -= 6.0  # past the five-second reset
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(task._runtime_blocked, set())
        self.assertEqual(
            [action for action, _ in client.actions].count(
                "navigation-learned-blocks-reset"), 1)
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(len(client.steps), 1)
        self.assertEqual(task.status, TaskStatus.RUNNING)

    def test_learned_blocks_stay_when_a_threat_closes_the_route(self):
        graph = WorldGraph()
        node = MapNode("/test/lane", width=5, height=1)
        node.terrain = {(x, 0): 1 for x in range(5)}
        graph.nodes[node.path] = node

        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(
                    path=node.path, width=17, height=17, world_x=0, world_y=0)
                self.steps = []
                self.actions = []

            async def move(self, direction):
                self.steps.append(direction)

            async def clear_actions(self):
                pass

            def record_action(self, action, detail):
                self.actions.append((action, detail))

        client = FakeClient()
        task = NavigateTask(graph, node.path, (4, 0))
        # Tolerance-bearing navigation, as on a combat approach: the older
        # no-route branch then waits instead of clearing anything itself.
        task.tolerance = 1
        # A stale learned block elsewhere, while a threat halo closes the lane.
        task._runtime_blocked = {(2, 0)}
        task._threat_blocked = {(1, 0)}
        asyncio.run(task.tick(client))
        task._no_route_since -= 6.0
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(task._runtime_blocked, {(2, 0)})
        self.assertNotIn("navigation-learned-blocks-reset",
                         [action for action, _ in client.actions])

    def test_locked_gate_requires_its_exact_authored_key(self):
        graph = built_graph()
        node = graph.nodes["/shattered_islands/world_2_70"]
        self.assertEqual(
            node.lock_requirements[(2, 8)], "morgeean_ship_key")
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        task = NavigateTask(graph, "/shattered_islands/world_5_46")
        client.state.place_item(Item(1, name="apartment key"), 7)
        self.assertFalse(node.walkable(
            2, 8, allow_locked=task._allow_locked(client)))
        client.state.place_item(Item(2, name="Morg'eean's Ship Key"), 7)
        self.assertTrue(node.walkable(
            2, 8, allow_locked=task._allow_locked(client)))

    def test_checker_only_gate_compiles_exact_access_requirement(self):
        graph = built_graph()
        path = ("/shattered_islands/strakewood_island/underground_city/"
                "underground_city_5_2_-1")
        node = graph.nodes[path]
        self.assertEqual(
            node.lock_requirements[(7, 3)], "underground_city_key_drow")
        self.assertFalse(node.walkable(7, 3, allow_locked=frozenset()))
        self.assertTrue(node.walkable(
            7, 3, allow_locked=frozenset({"underground_city_key_drow"})))

    def test_conditional_warning_supplies_ogre_guard_gate_key(self):
        graph = built_graph()
        node = graph.nodes["/shattered_islands/world_4_67_-1"]
        self.assertEqual(
            node.lock_requirements[(10, 15)],
            "underground_city_key_drow")
        self.assertFalse(node.walkable(
            10, 15, allow_locked=frozenset()))
        self.assertTrue(node.walkable(
            10, 15,
            allow_locked=frozenset({"underground_city_key_drow"})))

    def test_personalized_stone_giant_key_unlocks_named_checker(self):
        graph = built_graph()
        node = graph.nodes["/shattered_islands/world_5_57_-2"]
        self.assertEqual(
            node.lock_requirements[(14, 14)], "orange stone giant key")
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        task = NavigateTask(graph, "/shattered_islands/world_5_57_-2")
        self.assertFalse(node.walkable(
            14, 14, allow_locked=task._allow_locked(client)))
        client.state.place_item(Item(1, name="orange stone giant key"), 7)
        self.assertTrue(node.walkable(
            14, 14, allow_locked=task._allow_locked(client)))

    def test_graph_compiles_authored_town_npcs_as_peaceful(self):
        graph = built_graph()
        identities = graph.nodes[
            "/shattered_islands/world_0_67"].peaceful_identities
        self.assertIn("ranger", identities)
        self.assertIn("monk", identities)

    def test_graph_expands_multipart_blocking_footprints(self):
        node = self.graph.nodes["/shattered_islands/world_8_61"]
        # rock_wall6 is anchored at (17, 21), but its server-side _c
        # component blocks (18, 22). This exact omission trapped Sera after
        # a tiled-map crossing because the old graph routed northwest.
        self.assertIn((18, 22), node.blocked)
        self.assertFalse(node.walkable(18, 22))
        self.assertEqual(
            self.graph.local_path(node.path, (19, 23), (20, 22)),
            [(19, 23), (20, 22)])
        self.assertFalse(self.graph.local_path(
            node.path, (19, 23), (0, 16)))

    def test_graph_distinguishes_aggressive_type83_monsters(self):
        graph = built_graph()
        self.assertNotIn(
            "ogre rock thrower",
            graph.nodes["/shattered_islands/world_7_57"].peaceful_identities)
        self.assertIn(
            "giant wasp",
            graph.nodes["/shattered_islands/world_3_69"].peaceful_identities)
        self.assertNotIn(
            "lost soul",
            graph.nodes["/shattered_islands/world_3_69"].peaceful_identities)

    def test_early_navigation_refuses_route_without_pack_safe_option(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 9
        client.state.map = MapState(
            path="/shattered_islands/world_4_69",
            world_x=6, world_y=19)
        task = NavigateTask(graph, "/shattered_islands/world_6_56")
        with self.assertRaises(ValueError):
            task._plan(client)

    def test_depletion_trip_crosses_the_swamps_carefully(self):
        # Live failure: from world_4_45 every way south to Saruthar crosses
        # the Strakewood swamp packs, so a hazard-free plan does not exist.
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 23
        client.state.map = MapState(
            path="/shattered_islands/world_4_45", world_x=19, world_y=4)
        strict = NavigateTask(graph, "/shattered_islands/world_3_58", destination_xy=(13, 13))
        strict.tolerance = 1
        with self.assertRaisesRegex(ValueError, "no component route"):
            strict._plan(client)
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        service = circuit._new_depletion_service(client)
        route = service.navigation._plan(client)
        self.assertEqual(route[-1].destination, "/shattered_islands/world_3_58")
        self.assertFalse({edge.destination for edge in route} &
                         set(NavigateTask.KNOWN_LETHAL_TRANSIT))

    def test_navigation_never_uses_known_lethal_mixed_map_as_transit(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 9
        client.state.map = MapState(
            path="/shattered_islands/world_3_68",
            world_x=12, world_y=11)
        bear_map = "/shattered_islands/world_5_69"
        route = NavigateTask(graph, bear_map)._plan(client)
        maps = [edge.destination for edge in route]
        self.assertNotIn("/shattered_islands/world_4_69", maps)
        self.assertFalse(set(maps) & set(NavigateTask.KNOWN_LETHAL_TRANSIT))
        self.assertEqual(maps[-1], bear_map)

        # Avoidance never makes an explicit destination inaccessible.
        direct = NavigateTask(
            graph, "/shattered_islands/world_4_69")._plan(client)
        self.assertEqual(direct[-1].destination,
                         "/shattered_islands/world_4_69")
        # It also cannot imprison a character already on the hazardous map.
        client.state.map = MapState(
            path="/shattered_islands/world_4_69",
            world_x=20, world_y=12)
        escape = NavigateTask(graph, bear_map)._plan(client)
        self.assertEqual(escape[-1].destination, bear_map)

    def test_navigation_prefers_route_without_overlevel_hostiles(self):
        class FakeGraph:
            def __init__(self):
                self.nodes = {"/danger": MapNode("/danger")}
                self.map_monster_levels = {
                    "/danger": {"dire wolf": 20},
                }
                self.access_item_names = {}
                self._component_cache = {}
                self.calls = []

            def route_points(self, *args, **kwargs):
                avoided = set(kwargs.get("avoided_maps") or ())
                self.calls.append(avoided)
                middle = "/safe" if "/danger" in avoided else "/danger"
                return [
                    MapEdge("/start", middle, 0, 0),
                    MapEdge(middle, "/goal", 0, 0),
                ]

        graph = FakeGraph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 9
        client.state.map = MapState(path="/start", world_x=0, world_y=0)
        task = NavigateTask(graph, "/goal")
        route = task._plan(client)
        self.assertEqual(route[0].destination, "/safe")
        self.assertIn("/danger", graph.calls[0])
        self.assertFalse(task._threat_fallback)
        self.assertEqual(task._route_threat_maps, set())

    def test_navigation_avoids_aggressive_pack_transit_by_default(self):
        class FakeGraph:
            def __init__(self):
                self.pack = "/shattered_islands/world_1_1"
                self.nodes = {self.pack: MapNode(self.pack)}
                self.named_spawns = {self.pack: [object()]}
                self.map_monster_levels = {
                    self.pack: {"crocodile": 15}}
                self.access_item_names = {}
                self._component_cache = {}

            def transit_max_aggro_pack(self, path):
                return 3 if path == self.pack else 0

            def route_points(self, *args, **kwargs):
                avoided = set(kwargs.get("avoided_maps") or ())
                if self.pack in avoided:
                    return [
                        MapEdge("/start", "/safe", 0, 0),
                        MapEdge("/safe", "/goal", 0, 0),
                    ]
                return [
                    MapEdge("/start", self.pack, 0, 0),
                    MapEdge(self.pack, "/goal", 0, 0),
                ]

        graph = FakeGraph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 20
        client.state.map = MapState(path="/start", world_x=0, world_y=0)
        task = NavigateTask(graph, "/goal")

        route = task._plan(client)

        self.assertEqual(route[0].destination, "/safe")
        self.assertFalse(task._threat_fallback)
        self.assertEqual(task._route_threat_maps, set())

    def test_navigation_requires_explicit_clear_mode_for_pack_fallback(self):
        class FakeGraph:
            def __init__(self):
                self.pack = "/shattered_islands/world_1_1"
                self.nodes = {self.pack: MapNode(self.pack)}
                self.named_spawns = {self.pack: [object()]}
                self.map_monster_levels = {
                    self.pack: {"crocodile": 15}}
                self.access_item_names = {}
                self._component_cache = {}

            def transit_max_aggro_pack(self, path):
                return 3 if path == self.pack else 0

            def route_points(self, *args, **kwargs):
                if self.pack in set(kwargs.get("avoided_maps") or ()):
                    raise ValueError("no pack-free route")
                return [
                    MapEdge("/start", self.pack, 0, 0),
                    MapEdge(self.pack, "/goal", 0, 0),
                ]

        graph = FakeGraph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 20
        client.state.map = MapState(path="/start", world_x=0, world_y=0)
        client.decisions = []
        client.record_action = lambda action, detail="": (
            client.decisions.append((action, detail)))
        ordinary = NavigateTask(graph, "/goal")
        with self.assertRaises(ValueError):
            ordinary._plan(client)

        expedition = NavigateTask(graph, "/goal")
        expedition.allow_ranged_hazard_fallback = True
        route = expedition._plan(client)

        self.assertEqual(route[0].destination, graph.pack)
        self.assertTrue(expedition._threat_fallback)
        self.assertEqual(expedition._route_threat_maps, {graph.pack})
        self.assertEqual(
            client.decisions[-1][0], "navigation-threat-fallback")

    def test_navigation_abandons_stationary_nonattacking_transit_target(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.combat = []
                self.decisions = []

            async def clear_actions(self):
                pass

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)
                self.state.combat = enabled

            async def clear_target(self):
                self.state.target_id = 0

            def record_action(self, action, detail=""):
                self.decisions.append((action, detail))

        client = FakeClient()
        client.state.map = MapState(
            path="/road", width=17, height=17, world_x=13, world_y=14)
        client.state.stats["target_hp"] = 100
        frog = MapObject(
            30, 1, 0, 0, name="giant frog", target_id=444,
            target_hp=100)
        client.state.target_id = frog.target_id
        client.state.combat = True
        graph = WorldGraph()
        graph.nodes["/road"] = MapNode("/road", width=25, height=25)
        graph.nodes["/farm"] = MapNode("/farm", width=25, height=25)
        for node in graph.nodes.values():
            node.terrain = {
                (x, y): 1 for x in range(25) for y in range(25)}
        farm = FarmTask(zone="/farm", target="tree")
        task = NavigateThenTask(
            graph, "/farm", farm, combat_approach=True)
        farm._engaged_target = (
            frog.target_id, "/road", 16, 17, time.monotonic())
        threat = (3, 11, 11, frog)
        signature = (frog.target_id, "/road", 13, 14, 16, 17, 100)
        task._transit_stall = (*signature, time.monotonic() - 11.0)

        handled = asyncio.run(
            task._abandon_stalled_transit_target(client, threat))

        self.assertTrue(handled)
        self.assertIsNone(farm._engaged_target)
        self.assertTrue(farm.target_temporarily_unreachable(frog.target_id))
        self.assertEqual(client.combat[-1], False)
        self.assertEqual(client.decisions[-1][0],
                         "navigation-target-stalled")

    def test_navigation_retargets_rejected_tile_seam(self):
        graph = WorldGraph()
        for path in ("/start", "/goal"):
            graph.nodes[path] = MapNode(path, width=5, height=5)
            graph.nodes[path].terrain = {
                (x, y): 1 for x in range(5) for y in range(5)
            }
        graph.nodes["/start"].edges.append(MapEdge(
            "/start", "/goal", 2, 5, kind="tile",
            label="coordinate 0,1,0"))
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.map = MapState(
            path="/start", world_x=4, world_y=4)
        client.clears = 0
        client.decisions = []
        async def clear_actions():
            client.clears += 1
        client.clear_actions = clear_actions
        client.record_action = lambda action, detail="": client.decisions.append(
            (action, detail))

        task = NavigateTask(graph, "/goal")
        task.route = [MapEdge(
            "/start", "/goal", 4, 5, kind="tile",
            label="coordinate 0,1,0")]
        task._issued_goal = ("/start", 4, 5)
        task._issued_click = ("/start", 4, 5)
        task._last_progress = time.monotonic() - 3.0
        self.assertTrue(asyncio.run(
            task._retry_stalled_tile_crossing(client, task.route[0])))
        self.assertEqual((task.route[0].x, task.route[0].y), (2, 5))
        self.assertIn(("/start", "/goal", 4, 5),
                      task._failed_tile_crossings)
        self.assertEqual(client.clears, 1)
        self.assertEqual(
            client.decisions[0][0], "navigation-seam-retry")

    def test_navigation_marks_unavoidable_overlevel_fallback(self):
        class FakeGraph:
            def __init__(self):
                self.nodes = {"/danger": MapNode("/danger")}
                self.map_monster_levels = {
                    "/danger": {"dire wolf": 20},
                }
                self.access_item_names = {}
                self._component_cache = {}

            def route_points(self, *args, **kwargs):
                if "/danger" in set(kwargs.get("avoided_maps") or ()):
                    raise ValueError("no safe route")
                return [
                    MapEdge("/start", "/danger", 0, 0),
                    MapEdge("/danger", "/goal", 0, 0),
                ]

        graph = FakeGraph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 9
        client.state.map = MapState(path="/start", world_x=0, world_y=0)
        client.decisions = []
        client.record_action = lambda action, detail="": client.decisions.append(
            (action, detail))
        task = NavigateTask(graph, "/goal")
        route = task._plan(client)
        self.assertEqual(route[0].destination, "/danger")
        self.assertTrue(task._threat_fallback)
        self.assertEqual(task._route_threat_maps, {"/danger"})
        self.assertEqual(client.decisions[0][0],
                         "navigation-threat-fallback")

    def test_farm_patrol_step_never_shortcuts_across_map_boundary(self):
        node = MapNode("/test", width=25, height=25)
        node.terrain = {
            (x, y): 1 for x in range(25) for y in range(25)
        }
        task = FarmTask(patrol=[(7, 12)])
        task.map_node = node
        direction = task.patrol_step_direction(
            (19, 0), (7, 12))
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertTrue(node.walkable(19 + dx, dy))
        self.assertGreaterEqual(dy, 0)

        node.occupied.update({(18, 1), (17, 2), (7, 12)})
        direction = task.patrol_step_direction(
            (19, 0), (7, 12))
        dx, dy = c.DIRECTION_DELTAS[direction]
        self.assertNotIn((19 + dx, dy), {(18, 1), (17, 2)})

    def test_ship_key_unlocks_safe_two_transition_return(self):
        graph = built_graph()
        route = graph.route_points(
            MorgeeanShipKeyTask.MAP, (15, 14),
            MorgeeanShipKeyTask.SAFE_RETURN_MAP, [(5, 12)],
            allow_locked=frozenset({"morgeean_ship_key"}))
        self.assertEqual(len(route), 2)
        self.assertEqual(
            [edge.destination for edge in route],
            ["/shattered_islands/ships/ship_2",
             MorgeeanShipKeyTask.SAFE_RETURN_MAP])

    def test_upgrade_shop_cooldown_survives_process_restart(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        client.state.place_item(Item(
            50, item_type=c.TYPE_MONEY, quality=100,
            name="silver coin", quantity=30), 7)
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)
        client.state.last_upgrade_shop_sweep_at = time.time()
        self.assertFalse(circuit._needs_upgrade_shopping(client))
        client.state.last_upgrade_shop_sweep_at = 0.0
        self.assertTrue(circuit._needs_upgrade_shopping(client))

    def test_upgrade_shop_retries_only_for_new_tier_or_major_wealth(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 13
        client.state.bank_balance_known = True
        client.state.bank_balance = 4_817
        client.state.last_upgrade_shop_sweep_at = time.time() - 3 * 60 * 60
        client.state.last_upgrade_shop_sweep_level = 13
        client.state.last_upgrade_shop_sweep_wallet = 4_817
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)

        self.assertFalse(circuit._needs_upgrade_shopping(client))
        client.state.stats["level"] = 18
        self.assertTrue(circuit._needs_upgrade_shopping(client))
        client.state.stats["level"] = 13
        client.state.bank_balance = 9_817
        self.assertTrue(circuit._needs_upgrade_shopping(client))

    def test_upgrade_shop_policy_upgrade_rechecks_existing_level_eighteen(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 18
        client.state.bank_balance_known = True
        client.state.bank_balance = 10_686
        client.state.last_upgrade_shop_sweep_at = time.time()
        client.state.last_upgrade_shop_sweep_level = 18
        client.state.last_upgrade_shop_sweep_wallet = 10_686
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)

        self.assertTrue(circuit._needs_upgrade_shopping(client))
        client.state.last_upgrade_shop_sweep_policy = (
            circuit.UPGRADE_SWEEP_POLICY)
        self.assertFalse(circuit._needs_upgrade_shopping(client))

    def test_shop_purchase_does_not_complete_regional_policy(self):
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)
        sweep = ShopUpgradeSweepTask(WorldGraph(), [])
        sweep.purchased = True
        circuit._shopping = sweep
        self.assertTrue(circuit._shopping.purchased)
        # A purchased pass is intentionally distinguishable from a complete
        # no-upgrade scan so its caller can resume rather than persist policy.
        self.assertEqual(circuit.UPGRADE_SWEEP_POLICY, 7)

    def test_upgrade_shop_starts_only_with_discretionary_cash_above_reserve(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        client.state.bank_balance_known = True
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)
        client.state.bank_balance = 1_249
        self.assertFalse(circuit._needs_upgrade_shopping(client))
        client.state.bank_balance = 1_250
        self.assertTrue(circuit._needs_upgrade_shopping(client))

    def test_recall_shop_check_is_durable_and_readiness_aware(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 19
        client.state.bank_balance_known = True
        client.state.bank_balance = 8_000
        client.state.place_item(Item(
            10, name="Morg'eean's Ship Key"), 7)
        wizardry = Item(
            11, item_type=c.TYPE_SKILL, name="wizardry spells",
            extra={"level": 10})
        client.state.place_item(wizardry, 7)
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)

        self.assertTrue(circuit._needs_recall_shopping(client))
        client.state.last_recall_shop_check_at = time.time()
        self.assertFalse(circuit._needs_recall_shopping(client))
        client.state.last_recall_shop_check_at = 0.0
        client.state.place_item(Item(
            12, item_type=c.TYPE_WAND, quality=80, condition=80,
            name="wand of word of recall (lvl 12)"), 7)
        self.assertFalse(circuit._needs_recall_shopping(client))

        wizardry.extra["level"] = 12
        self.assertTrue(circuit._needs_recall_shopping(client))
        client.state.place_item(Item(
            13, item_type=c.TYPE_SPELLBOOK, quality=80, condition=80,
            name="spellbook of word of recall"), 7)
        self.assertFalse(circuit._needs_recall_shopping(client))

    def test_utility_shop_check_is_daily_and_capability_aware(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 19
        client.state.bank_balance_known = True
        client.state.bank_balance = 8_000
        client.state.place_item(Item(
            10, name="Morg'eean's Ship Key"), 7)
        client.state.place_item(Item(
            11, item_type=c.TYPE_SKILL, name="wizardry spells",
            extra={"level": 10}), 7)
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)

        self.assertEqual(circuit._missing_utility_capabilities(client), (
            "identify", "remove depletion", "cure disease"))
        self.assertTrue(circuit._needs_utility_shopping(client))
        client.state.last_utility_shop_check_at = time.time()
        self.assertFalse(circuit._needs_utility_shopping(client))
        client.state.last_utility_shop_check_at = 0.0
        client.state.place_item(Item(
            12, item_type=c.TYPE_SPELL, name="identify"), 7)
        client.state.place_item(Item(
            13, item_type=c.TYPE_ROD, quality=80,
            name="rod of remove depletion"), 7)
        client.state.place_item(Item(
            14, item_type=c.TYPE_SPELLBOOK, quality=80,
            name="spellbook of cure disease"), 7)
        self.assertEqual(
            circuit._missing_utility_capabilities(client), ("cure disease",))
        client.state.place_item(Item(
            15, item_type=c.TYPE_SCROLL, quality=80,
            name="scroll of cure disease"), 7)
        self.assertFalse(circuit._needs_utility_shopping(client))

    def test_utility_shop_routes_safely_and_avoids_unlearnable_cure_book(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 19
        client.state.bank_balance_known = True
        client.state.bank_balance = 8_000
        client.state.place_item(Item(
            10, name="Morg'eean's Ship Key"), 7)
        client.state.place_item(Item(
            11, item_type=c.TYPE_SKILL, name="wizardry spells",
            extra={"level": 10}), 7)
        circuit = FarmCircuitTask(
            WorldGraph(), [("/farm", "rat")], clock=lambda: 100.0)

        trip = circuit._new_utility_shopping(client)
        shopper = trip.task
        self.assertEqual(trip.navigation.destination,
                         "/shattered_islands/world_4_47")
        self.assertTrue(trip.navigation.allow_ranged_hazard_fallback)
        self.assertEqual(trip.safety.heal_below, 0.85)
        self.assertIsInstance(shopper, BuyDialogueStockTask)
        client.state.interface = InterfaceState(title="sage", links=[
            "[a=:buy spellbook of cure disease]Cure book[/a]",
            "[a=:buy wand of cure disease (lvl 15)]Cure wand[/a]",
        ])
        self.assertEqual(
            shopper._select_stock(client, shopper._destinations(client)),
            "wand of cure disease (lvl 15)")

    def test_world_graph_compiles_dialogue_vendors_and_bartender_stock(self):
        graph = built_graph()
        kestrei = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Kestrei")
        self.assertEqual(kestrei.map_path, "/shattered_islands/world_1_67")
        self.assertIn("free-range chicken leg", kestrei.stock)
        sage = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "sage")
        self.assertEqual(
            set(sage.treasure_lists), {"random_scroll", "random_wand"})
        dunowen = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Dunowen")
        self.assertIn("firestorm", dunowen.stock)
        miyoko = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Miyoko")
        self.assertIn("potion cure illness", miyoko.stock)
        feldain = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Feldain Goodwin")
        self.assertIn("magic bullet", feldain.stock)
        farmer = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Farmer Maggot")
        self.assertIn("wild white mushroom", farmer.stock)
        bazaar_farmer = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.map_path == "/shattered_islands/world_4_47" and
            vendor.name == "farmer")
        self.assertIn("red apple", bazaar_farmer.stock)
        bazaar_food = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.map_path == "/shattered_islands/world_4_47" and
            "quarter-pound cheeseburger" in vendor.stock)
        self.assertEqual(bazaar_food.name, "merchant")
        roldan = next(
            vendor for vendor in graph.dialogue_vendors
            if vendor.name == "Roldan")
        self.assertIn("wholemeal bread", roldan.stock)
        self.assertGreaterEqual(len(graph.dialogue_vendors), 25)

    def test_food_resupply_uses_nearest_safe_bulk_bartender(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 19
        client.state.map = MapState(
            path="/shattered_islands/world_0_69",
            world_x=10, world_y=10)
        client.record_action = lambda *args: None
        circuit = FarmCircuitTask(
            built_graph(), [("/farm", "rat")], clock=lambda: 100.0)

        resupply = circuit._new_food_resupply(client)
        self.assertEqual(
            resupply.navigation.destination,
            "/shattered_islands/world_1_67")
        self.assertIsInstance(resupply.task, BuyDialogueStockTask)
        self.assertEqual(resupply.task.merchant, "Kestrei")
        self.assertEqual(
            resupply.task.quantity, FarmCircuitTask.FOOD_RESUPPLY_QUANTITY)
        self.assertEqual(
            resupply.task.preferred, ("free-range chicken leg",))

        client.state.map = MapState(
            path="/shattered_islands/world_4_47", world_x=10, world_y=10)
        local = circuit._new_food_resupply(client)
        self.assertEqual(
            local.navigation.destination, "/shattered_islands/world_4_47")
        self.assertIsInstance(local.task, BuyDialogueStockTask)
        self.assertEqual(
            local.task.quantity, 36)
        self.assertEqual(local.task.preferred, ("chicken drumstick",))

        # The food catalog is derived from archetype type/nutrition rather
        # than a brittle product-name regex; custom and quest sellers count.
        mushroom_vendor = next(
            vendor for vendor in circuit.graph.dialogue_vendors
            if vendor.name == "Farmer Maggot")
        self.assertIn(
            "wild white mushroom",
            {profile.name for profile in
             circuit.graph.dialogue_food_stock(mushroom_vendor)})

    def test_food_resupply_bulk_quantity_preserves_loot_headroom(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["weight_limit"] = 20
        client.state.place_item(Item(
            91, name="kept gear", weight=14.0, quantity=1), 7)
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        profile = FoodProfile(
            "free-range chicken leg", nutrition=400, value=10,
            weight=0.4, stackable=True)

        self.assertEqual(circuit._food_resupply_quantity(client, profile), 5)

    def test_level_eight_circuit_schedules_free_ship_key_once(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        circuit = FarmCircuitTask(WorldGraph(), [("/farm", "rat")])
        # A playing-state packet precedes the complete inventory replay on
        # reconnect; do not start a cross-world key trip in that gap.
        client.state.inventory_replay_complete = False
        self.assertFalse(circuit._needs_ship_key(client))
        client.state.place_item(Item(
            9, item_type=c.TYPE_SKILL, quality=255,
            name="slash weapons"), 7)
        client.state.inventory_replay_complete = True
        self.assertTrue(circuit._needs_ship_key(client))
        client.state.place_item(
            Item(10, name="Morg'eean's Ship Key"), 7)
        self.assertFalse(circuit._needs_ship_key(client))
        self.assertTrue(any(MorgeeanShipKeyTask.KEY.search(item.name)
                            for item in client.state.inventory))

    def test_spell_purchase_waits_for_reserve_and_stops_once_known(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.bank_balance_known = True
        circuit = FarmCircuitTask(
            built_graph(), [("/farm", "rat")], clock=lambda: 100.0)
        client.state.bank_balance = 6_999
        self.assertFalse(circuit._needs_spell_purchase(client))
        client.state.bank_balance = 7_000
        self.assertTrue(circuit._needs_spell_purchase(client))
        purchase = circuit._new_spell_purchase()
        self.assertEqual(purchase.navigation.destination,
                         "/shattered_islands/world_1_69")
        self.assertEqual(purchase.navigation.destination_xy, (12, 3))
        self.assertEqual(purchase.navigation.tolerance, 1)
        self.assertTrue(any(
            circuit.graph.nodes[purchase.navigation.destination].walkable(
                *point)
            for point in purchase.navigation._approach_candidates()))
        client.state.place_item(Item(
            11, item_type=c.TYPE_SPELL, name="magic bullet"), 7)
        self.assertFalse(circuit._needs_spell_purchase(client))

    def test_buy_spell_task_uses_exact_seller_command_and_verifies_spell(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.bank_balance_known = True
                self.state.bank_balance = 7_000
                self.talks = []

            async def talk(self, message, npc):
                self.talks.append((message, npc))

            def set_bank_balance(self, balance):
                self.state.bank_balance = balance

        client = FakeClient()
        client.state.place_item(Item(
            10, item_type=c.TYPE_MONEY, name="silver coin", quantity=10), 7)
        task = BuySpellTask(
            "Feldain Goodwin", "magic bullet", cost=6_000)
        asyncio.run(task.tick(client))
        self.assertEqual(
            client.talks, [("buy magic bullet", "Feldain Goodwin")])
        self.assertEqual(task.status, TaskStatus.RUNNING)
        client.state.place_item(Item(
            12, item_type=c.TYPE_SPELL, name="magic bullet"), 7)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.state.bank_balance, 2_000)

    def test_navigation_chains_viewport_sized_clicks(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_85",
                    "world_x": 7, "world_y": 13,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []

            async def move_to_view(self, x, y):
                self.moves.append((x, y))


            async def move(self, direction):
                self.steps.append(direction)
        client = FakeClient()
        task = NavigateTask(self.graph, "/shattered_islands/world_4_84")
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [1])
        _, client.state.map.world_x, client.state.map.world_y = task._issued_click
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [1, 8])

    def test_navigation_continues_from_north_of_incuna_gangway(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_85",
                    "world_x": 11, "world_y": 4,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []

            async def move_to_view(self, x, y):
                self.moves.append((x, y))


            async def move(self, direction):
                self.steps.append(direction)
        client = FakeClient()
        task = NavigateTask(self.graph, "/shattered_islands/world_4_84")
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [8])

    def test_navigation_preserves_component_route_after_map_crossing(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_84",
                    "world_x": 13, "world_y": 0,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []
                self.clears = 0

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_4_85_-2")
        task.status = TaskStatus.RUNNING
        completed = MapEdge(
            "/shattered_islands/world_4_83",
            "/shattered_islands/world_4_84", 13, 24, kind="tile")
        retained = MapEdge(
            "/shattered_islands/world_4_84",
            "/shattered_islands/world_3_84", -1, 11, kind="tile")
        task.route = [completed, retained]
        asyncio.run(task.tick(client))
        self.assertEqual(task.route[0], retained)
        self.assertTrue(client.moves or client.steps)

    def test_navigation_bumps_door_on_destination_side_of_map_seam(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_3_84",
                    "world_x": 23, "world_y": 16,
                    "width": 17, "height": 17,
                })()
                self.steps = []
                self.clears = 0
                self.path_moves = []

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

            async def move_to_view(self, x, y):
                self.path_moves.append((x, y))

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_4_85_-2")
        task.status = TaskStatus.RUNNING
        task.route = [MapEdge(
            "/shattered_islands/world_3_84",
            "/shattered_islands/world_4_84",
            24, 16, kind="tile")]
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [3])
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.path_moves, [])

    def test_boundary_door_waits_until_aligned_with_its_row(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_3_84",
                    "world_x": 23, "world_y": 11,
                    "width": 17, "height": 17,
                })()
                self.steps = []
                self.clears = 0
                self.path_moves = []

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

            async def move_to_view(self, x, y):
                self.path_moves.append((x, y))

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_4_85_-2")
        task.status = TaskStatus.RUNNING
        task.route = [MapEdge(
            "/shattered_islands/world_3_84",
            "/shattered_islands/world_4_84",
            24, 16, kind="tile")]
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [5])
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.path_moves, [])

    def test_stalled_click_path_steps_off_door_that_closed_under_player(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_83",
                    "world_x": 1, "world_y": 15,
                    "width": 17, "height": 17,
                })()
                self.path_moves = []
                self.steps = []
                self.clears = 0

            async def move_to_view(self, x, y):
                self.path_moves.append((x, y))

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_3_83", (11, 10),
            tolerance=2)
        asyncio.run(task.tick(client))
        self.assertEqual(client.steps, [5])
        task._last_progress -= task.STALL_RETRY_SECONDS + 0.1
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.steps, [5, 5])

    def test_stalled_click_path_bumps_door_from_inside_bank(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_83",
                    "world_x": 1, "world_y": 14,
                    "width": 17, "height": 17,
                })()
                self.path_moves = []
                self.steps = []
                self.clears = 0

            async def move_to_view(self, x, y):
                self.path_moves.append((x, y))

            async def clear_actions(self):
                self.clears += 1

            async def move(self, direction):
                self.steps.append(direction)

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_3_83", (11, 10),
            tolerance=2)
        asyncio.run(task.tick(client))
        task._last_progress -= task.STALL_RETRY_SECONDS + 0.1
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        # Direction 5 is south in the server's freearr arrays and bumps the
        # closed bank door at (1, 15).
        self.assertEqual(client.clears, 1)
        self.assertEqual(client.steps, [5, 5])

    def test_john_aldman_route_uses_authored_down_stairs(self):
        route = self.graph.route(
            "/shattered_islands/world_4_83",
            "/shattered_islands/world_4_83_-1",
        )
        self.assertEqual((route[0].x, route[0].y, route[0].kind),
                         (1, 7, "exit"))
        self.assertTrue(route[0].automatic)
        self.assertNotEqual((route[0].x, route[0].y), (12, 12))
        path = self.graph.local_path(
            "/shattered_islands/world_4_83", (9, 13), (1, 7))
        self.assertTrue(path)
        self.assertEqual(path[-1], (1, 7))

    def test_john_wrong_interior_component_routes_through_church(self):
        interior = "/shattered_islands/world_4_83_-1"
        target_points = [(5, 16), (5, 17), (6, 16), (7, 17)]
        route = self.graph.route_points(
            interior, (0, 11), interior, target_points)
        self.assertEqual(
            [(edge.source, (edge.x, edge.y), edge.destination)
             for edge in route],
            [
                (interior, (0, 7), "/shattered_islands/world_4_83"),
                ("/shattered_islands/world_4_83", (8, 24),
                 "/shattered_islands/world_4_84"),
                ("/shattered_islands/world_4_84", (9, 8),
                 "/shattered_islands/world_4_84_-1"),
                ("/shattered_islands/world_4_84_-1", (10, -1), interior),
            ],
        )

    def test_john_bank_gate_requires_access_item(self):
        interior = "/shattered_islands/world_4_83_-1"
        node = self.graph.nodes[interior]
        self.assertIn((1, 11), node.locked)
        self.assertFalse(node.walkable(1, 11))
        self.assertTrue(node.walkable(1, 11, allow_locked=True))
        self.assertFalse(self.graph.local_path(
            interior, (0, 11), (5, 16)))

    def test_kobold_chief_approach_avoids_keyed_exit_door(self):
        route = self.graph.route_points(
            "/shattered_islands/world_3_81_-1", (23, 15),
            "/shattered_islands/world_3_81_-1", [(23, 5)],
            allow_locked=False)
        self.assertTrue(route)
        self.assertIn("/shattered_islands/world_3_80_-1",
                      {edge.source for edge in route})

    def test_landmark_index(self):
        matches = self.graph.find_landmarks("Sam Goodberry")
        self.assertTrue(matches)
        self.assertIn("/shattered_islands/world_-7_76",
                      {match.map_path for match in matches})

    def test_npc_navigation_targets_interaction_perimeter(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_0_83",
                    "world_x": 8, "world_y": 16,
                    "width": 17, "height": 17,
                })()
                self.moves = []
                self.steps = []

            async def move(self, direction):
                self.steps.append(direction)

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_0_83", (8, 19),
            tolerance=2)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.RUNNING)
        self.assertEqual(client.steps, [5])

    def test_quest_npcs_use_dynamic_walkable_interaction_perimeters(self):
        for key, name in (
                ("Sam Goodberry (Incuna)", "Sam Goodberry"),
                ("Brelend Lee", "Brelend Lee"),
                ("Ken Berger", "Ken Berger")):
            place = NPC[key]
            task = DialogAtTask(self.graph, place, name, ())
            navigation = task.navigation
            self.assertEqual(navigation.destination, place.map_path)
            self.assertEqual(navigation.destination_xy, (place.x, place.y))
            self.assertEqual(navigation.tolerance, 1)
            navigation._last_position = (place.map_path, 0, 0)
            candidates = navigation._approach_candidates()
            self.assertTrue(candidates)
            self.assertTrue(all(
                self.graph.nodes[place.map_path].walkable(*point)
                for point in candidates))

    def test_behind_bar_npc_expands_only_after_adjacent_route_fails(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 9
        client.state.map = MapState(
            path="/shattered_islands/world_5_48",
            world_x=7, world_y=11)
        task = DialogAtTask(self.graph, NPC["Gashir"], "Gashir", ())
        asyncio.run(task.tick(client))
        self.assertEqual(task.navigation.tolerance, 2)
        self.assertEqual(task.status, TaskStatus.RUNNING)
        asyncio.run(task.navigation.start(client))
        self.assertEqual(
            task.navigation.route[-1].destination,
            "/shattered_islands/world_6_48")

    def test_blocked_npc_approach_rotates_without_coordinate_progress(self):
        class FakeClient:
            def __init__(self):
                self.state = GameState(phase="playing", player_tag=7)
                self.state.map = MapState(
                    path="/shattered_islands/world_1_67",
                    world_x=8, world_y=19, width=17, height=17)
                self.steps = []
                self.decisions = []

            async def move(self, direction):
                self.steps.append(direction)

            async def clear_actions(self):
                pass

            def record_action(self, action, detail=""):
                self.decisions.append((action, detail))

        client = FakeClient()
        place = NPC["Melanye"]
        task = DialogAtTask(self.graph, place, "Melanye", ()).navigation
        asyncio.run(task.tick(client))
        self.assertEqual(task._approach_index, 0)
        self.assertTrue(client.steps)
        task._last_progress -= 2.0
        task._last_action = 0.0
        asyncio.run(task.tick(client))
        self.assertEqual(task._approach_index, 1)
        self.assertTrue(any(action == "navigation-approach-retry"
                            for action, _ in client.decisions))

    def test_reaching_interaction_range_cancels_pending_path(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_0_83",
                    "world_x": 8, "world_y": 17,
                    "width": 17, "height": 17,
                })()
                self.clears = 0

            async def clear_actions(self):
                self.clears += 1

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_0_83", (8, 19),
            tolerance=2)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.clears, 1)

    def test_reaching_map_only_destination_cancels_pending_path(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.inventory = []
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_3_81_-1",
                    "world_x": 14, "world_y": 23,
                    "width": 17, "height": 17,
                })()
                self.clears = 0

            async def clear_actions(self):
                self.clears += 1

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_3_81_-1")
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.clears, 1)

    def test_lost_memories_advances_the_scripted_incuna_voyage(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": LostMemoriesArrivalTask.OCEAN_MAP,
                    "world_x": 4, "world_y": 6,
                    "width": 17, "height": 17,
                })()
                self.talks = []
                self.clears = 0
                self.combat = []
                self.cleared_targets = 0

            async def talk(self, text, npc=""):
                self.talks.append((text, npc))

            async def clear_actions(self):
                self.clears += 1

            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

            async def clear_target(self):
                self.cleared_targets += 1

        client = FakeClient()
        task = LostMemoriesArrivalTask(self.graph)
        # Create the shipboard-Sam step, finish its already-adjacent
        # navigation, then open the authored voyage dialog.
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks, [("hello", "Sam Goodberry")])
        self.assertEqual(task.child.navigation.destination_xy, (4, 7))

        # Starting below deck must target the Incuna arrival map rather than
        # trying to path back to the NPC on the disconnected ocean map.
        client.state.map.path = LostMemoriesArrivalTask.LOWER_DECKS[0]
        task = LostMemoriesArrivalTask(self.graph)
        asyncio.run(task.tick(client))
        self.assertIsInstance(task.child, NavigateTask)
        self.assertEqual(task.child.destination,
                         LostMemoriesArrivalTask.INCUNA_ARRIVAL_MAP)

    def test_route_applies_stairs_after_reaching_the_exit_tile(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/incuna/ship_lower_deck",
                    "world_x": 6, "world_y": 2,
                    "width": 8, "height": 6,
                })()
                self.state.ground = [Item(4242, name="stairs going up")]
                self.applied = []
                self.moves = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

        client = FakeClient()
        task = NavigateTask(
            self.graph, LostMemoriesArrivalTask.INCUNA_ARRIVAL_MAP)
        task.status = TaskStatus.RUNNING
        task.route = [MapEdge(
            client.state.map.path, task.destination, 6, 2,
            kind="exit", label="stairs going up")]
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [4242])
        self.assertEqual(client.moves, [])

    def test_route_applies_current_square_when_exit_tag_is_missing(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {})()
                self.state.phase = "playing"
                self.state.map = type("Map", (), {
                    "path": "/shattered_islands/world_4_83",
                    "world_x": 0, "world_y": 7,
                    "width": 17, "height": 17,
                })()
                self.state.ground = []
                self.applied = []
                self.moves = []

            async def apply(self, tag):
                self.applied.append(tag)

            async def move_to_view(self, x, y):
                self.moves.append((x, y))

        client = FakeClient()
        task = NavigateTask(
            self.graph, "/shattered_islands/world_4_83_-1")
        task.status = TaskStatus.RUNNING
        task.route = [MapEdge(
            client.state.map.path, task.destination, 0, 7,
            kind="exit", label="stairs going down")]
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [0])
        self.assertEqual(client.moves, [])


FAKE_ANALYZER_MAIN = """
import json, os, sys
args = sys.argv[1:]
if os.path.exists(os.path.join(os.path.dirname(sys.argv[0]), "FAIL")):
    raise SystemExit(3)
level = int(args[0])
need = ("--melee-damage", "--weapon-class", "--weapon-speed", "--content-root",
        "--server-root", "--ranking", "--json")
missing = [flag for flag in need if flag not in args]
if missing:
    print("missing " + " ".join(missing), file=sys.stderr)
    raise SystemExit(2)
def spot(name, xp, money):
    return {"path": "/" + name, "name": name, "region": "test", "location": name,
            "map_paths": ["maps/" + name], "monsters": ["rat L%d" % level],
            "xp_hour": xp, "money_hour": money, "loot_hour": 1.0, "danger": 1.0,
            "score": 1.0, "safety_score": 1.0, "aggressive_spawns": 0,
            "max_aggro_pack": 0, "max_monster_level": level, "danger_flags": []}
rankings = {key: [spot("grind", 900.0, 5.0), spot("gold", 100.0, 50.0)]
            for key in ("balanced", "xp", "safety", "maneuverability", "loot")}
rankings["money"] = [spot("gold", 100.0, 50.0), spot("grind", 900.0, 5.0)]
print(json.dumps({"results": rankings["xp"], "rankings": rankings, "args": args}))
"""


class FarmAnalyzerTests(unittest.TestCase):
    @staticmethod
    def client(*, skill="slash weapons", level=22, dam=44, speed=0.8):
        from .farm_analyzer import OBJECTIVES  # noqa: F401  (import check)

        client = AtrinikClient(ClientConfig())
        state = client.state
        state.phase = "playing"
        weapon = Item(tag=10, name="sword", required_skill_tag=11)
        school = Item(tag=11, name=skill, item_type=c.TYPE_SKILL, extra={"level": level})
        state.items = {10: weapon, 11: school}
        state.equipment = {c.EQUIP_WEAPON: 10}
        state.stats.update({"dam": dam, "wc": 31, "weapon_speed": 2.25, "level": 22,
                            "maxhp": 188, "speed": speed, "ac": 37})
        names = list(c.ATTACK_NAMES)
        state.protections = {names.index("slash"): 49, names.index("weapon_magic"): 7}
        return client

    def analyzer(self, *, fail=False, clock=None):
        from .farm_analyzer import FarmAnalyzer

        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        package = directory / "farming_analyzer"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text(FAKE_ANALYZER_MAIN)
        if fail:
            (package / "FAIL").write_text("")
        analyzer = FarmAnalyzer(directory, directory / "content", directory / "server",
                                clock=clock or time.time)
        return analyzer

    def test_profile_reads_the_killing_school_and_player_doll(self):
        from .farm_analyzer import combat_profile

        profile = combat_profile(self.client())
        self.assertEqual((profile.skill, profile.skill_level, profile.attack),
                         ("slash weapons", 22, "slash"))
        args = profile.arguments()
        self.assertEqual(args[0], "22")
        for flag, value in (("--melee-damage", "44"), ("--weapon-class", "31"),
                            ("--weapon-speed", "2.25"), ("--melee-attack", "slash=100"),
                            ("--max-health", "188"), ("--player-ac", "37")):
            self.assertEqual(args[args.index(flag) + 1], value, flag)
        protections = [args[i + 1] for i, arg in enumerate(args) if arg == "--player-protection"]
        self.assertEqual(sorted(protections), ["slash=49", "weaponmagic=7"])
        self.assertIsNone(combat_profile(self.client(skill="wizardry spells")))
        unarmed = self.client()
        unarmed.state.equipment = {}
        self.assertIsNone(combat_profile(unarmed))

    def test_only_material_profile_changes_call_for_a_new_ranking(self):
        from .farm_analyzer import combat_profile

        base = combat_profile(self.client())
        self.assertFalse(base.materially_differs(combat_profile(self.client(dam=46))))
        self.assertTrue(base.materially_differs(combat_profile(self.client(dam=50))))
        self.assertTrue(base.materially_differs(combat_profile(self.client(level=23))))
        self.assertFalse(base.materially_differs(combat_profile(self.client(speed=0.82))))
        self.assertTrue(base.materially_differs(combat_profile(self.client(speed=0.9))))
        self.assertTrue(base.materially_differs(None))

    def test_background_run_ranks_every_objective_without_blocking(self):
        async def scenario():
            now = [1000.0]
            analyzer = self.analyzer(clock=lambda: now[0])
            client = self.client()
            self.assertTrue(analyzer.poll(client))
            self.assertTrue(analyzer.running)
            self.assertEqual(analyzer.status, "running")
            self.assertFalse(analyzer.poll(client))  # one process at a time
            await analyzer._task
            self.assertEqual(analyzer.status, "ready", analyzer.error)
            self.assertEqual(analyzer.best("xp")["name"], "grind")
            self.assertEqual(analyzer.best("money")["name"], "gold")
            snapshot = analyzer.snapshot("money")
            self.assertEqual(snapshot["objective"], "money")
            self.assertEqual([spot["name"] for spot in snapshot["ranking"]], ["gold", "grind"])
            self.assertEqual(snapshot["profile"]["skill_level"], 22)
            # An unchanged profile is not re-ranked until the result ages out.
            now[0] += analyzer.MIN_INTERVAL_SECONDS + 1
            self.assertFalse(analyzer.poll(client))
            # A level-up re-ranks once the minimum interval has passed.
            self.assertTrue(analyzer.poll(self.client(level=23)))
            await analyzer._task
            self.assertEqual(analyzer.profile.skill_level, 23)
            self.assertEqual(analyzer.runs, 2)
            # Without a supported weapon school, the last ranking stays.
            self.assertFalse(analyzer.poll(self.client(skill="wizardry spells")))
            self.assertEqual(analyzer.status, "ready")
            await analyzer.close()

        asyncio.run(scenario())

    def test_failed_run_keeps_playing_and_backs_off(self):
        async def scenario():
            now = [1000.0]
            analyzer = self.analyzer(fail=True, clock=lambda: now[0])
            self.assertTrue(analyzer.poll(self.client()))
            await analyzer._task
            self.assertEqual(analyzer.status, "failed")
            self.assertIn("status 3", analyzer.error)
            self.assertIsNone(analyzer.best("xp"))
            now[0] += analyzer.MIN_INTERVAL_SECONDS + 1
            self.assertFalse(analyzer.poll(self.client()))
            now[0] += analyzer.RETRY_SECONDS
            self.assertTrue(analyzer.poll(self.client()))
            await analyzer.close()

        asyncio.run(scenario())

    def test_failed_refresh_retries_after_backoff_not_six_hours(self):
        async def scenario():
            now = [1000.0]
            analyzer = self.analyzer(clock=lambda: now[0])
            client = self.client()
            self.assertTrue(analyzer.poll(client))
            await analyzer._task
            self.assertEqual(analyzer.status, "ready")
            reported_at = analyzer.reported_at
            # The six-hour refresh of an unchanged profile fails...
            now[0] += analyzer.REFRESH_SECONDS + 1
            (analyzer.analyzer_root / "farming_analyzer" / "FAIL").write_text("")
            self.assertTrue(analyzer.poll(client))
            await analyzer._task
            self.assertEqual(analyzer.status, "failed")
            self.assertEqual(analyzer.reported_at, reported_at)
            self.assertEqual(analyzer.best("xp")["name"], "grind")  # report kept
            # ...and is retried after the failure backoff, not six hours later.
            (analyzer.analyzer_root / "farming_analyzer" / "FAIL").unlink()
            now[0] += analyzer.RETRY_SECONDS - 1
            self.assertFalse(analyzer.poll(client))
            now[0] += 2
            self.assertTrue(analyzer.poll(client))
            await analyzer._task
            self.assertEqual(analyzer.status, "ready")
            self.assertGreater(analyzer.reported_at, reported_at)
            await analyzer.close()

        asyncio.run(scenario())

    def test_command_is_isolated_and_low_priority(self):
        from .farm_analyzer import combat_profile

        analyzer = self.analyzer()
        command = analyzer.command(combat_profile(self.client()))
        if command[0].endswith("nice"):
            self.assertEqual(command[1:3], ["-n", "10"])
            command = command[3:]
        self.assertEqual(command[1:3], ["-E", "-s"])
        self.assertEqual(command[command.index("--ranking") + 1], "all")
        self.assertIn("--json", command)


class AnalyzerFarmSelectionTests(unittest.TestCase):
    TREES = "/shattered_islands/world_14_79"
    POCKET = "/shattered_islands/world_14_78"
    UPPER = "/shattered_islands/world_14_77"

    def client(self, rankings):
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({"level": 22, "exp": 1000, "hp": 100, "maxhp": 100})
        client.farm_analyzer = StubAnalyzer(rankings)
        return client

    def test_spot_legs_follow_analyzer_maps_and_monsters(self):
        from .navigation import analyzer_spot_legs

        graph = built_graph()
        spot = analyzer_spot("Circuit", [self.TREES, self.POCKET],
                             ["evil treant L13×3", "quickwood L11×2", "evil treant L14"])
        self.assertEqual(analyzer_spot_legs(spot, graph), (
            (self.TREES, "evil treant|quickwood"),
            (self.POCKET, "evil treant|quickwood")))
        unknown = analyzer_spot("Nowhere", [self.TREES, "/no/such/map"], ["rat L1"])
        self.assertEqual(analyzer_spot_legs(unknown, graph), ())

    def test_selection_follows_the_objective_and_skips_live_failures(self):
        from .navigation import analyzer_farm_selection

        graph = built_graph()
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        pocket = analyzer_spot("Pocket", [self.POCKET], ["evil treant L13"])
        gold = analyzer_spot("Gold", [self.UPPER], ["Kotung L17"])
        client = self.client({"xp": [trees, pocket], "money": [gold, trees]})
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Trees")
        client.state.farm_objective = "money"
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Gold")
        # Depleted at the temple threshold: the safest ranking applies until
        # the temple removes it, then the chosen objective again.
        client.state.farm_objective = "xp"
        client.farm_analyzer.rankings["safety"] = [pocket]
        client.state.depletion_points_known = True
        client.state.depletion_points = FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD
        # A stale counter alone (cured elsewhere) is not depletion.
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Trees")
        client.state.place_item(Item(
            501, item_type=c.TYPE_FORCE, quality=0, name="depletion"), client.state.player_tag)
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Pocket")
        client.state.depletion_points = FarmCircuitTask.DEPLETION_SERVICE_THRESHOLD - 1
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Trees")
        client.state.depletion_points = 0
        client.state.remove_item(501)
        client.state.farm_objective = "money"
        client.state.farm_objective = "xp"
        client.state.farm_zone_quarantine = {self.TREES: time.time() + 60}
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Pocket")
        client.state.farm_zone_quarantine = {self.TREES: time.time() - 1}
        self.assertEqual(analyzer_farm_selection(client, graph)[1]["name"], "Trees")
        client.farm_analyzer = None
        self.assertIsNone(analyzer_farm_selection(client, graph))

    def circuit(self, client, spot):
        from .navigation import analyzer_spot_legs

        circuit = FarmCircuitTask(built_graph(), list(analyzer_spot_legs(
            spot, built_graph())), analyzer_driven=True, spot=spot)
        circuit.child = circuit._new_child()
        return circuit

    def test_failures_at_an_analyzer_spot_teach_instead_of_excluding_it(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        pocket = analyzer_spot("Pocket", [self.POCKET], ["evil treant L13"])
        client = self.client({"xp": [trees, pocket]})
        circuit = self.circuit(client, trees)
        farm = circuit.child.task
        base = (farm.safety.flee_below, farm.safety.heal_below)
        # Two treants hitting at once, then a death on the spot.
        client.state.add_message(2, "ff0000", "evil treant hit you for 30 (-30) impact damage.")
        client.state.add_message(2, "ff0000", "quickwood hit you for 20 (-20) impact damage.")
        client.decision_history.append({
            "time": time.time() + 1, "action": "death", "detail": "",
            "task": "", "map": self.TREES})
        self.assertFalse(circuit._observe_farm_death(client))
        self.assertNotIn(self.TREES, client.state.farm_zone_quarantine)
        record = client.state.farm_adaptations[trees["path"]]
        self.assertEqual(record["level"], 1)
        self.assertEqual(record["causes"], {"pack": 1})
        self.assertEqual(record["reviews"][-1]["peak_attackers"], 2)
        self.assertIn("area spells for packs", record["reviews"][-1]["remedies"])
        rebuilt = circuit.child.task
        self.assertIsNot(rebuilt, farm)
        self.assertEqual((rebuilt.safety.flee_below, rebuilt.safety.heal_below),
                         (round(base[0] + 0.03, 6), round(base[1] + 0.02, 6)))
        self.assertIn("farm-failure-review",
                      [entry["action"] for entry in client.decision_history])
        # The analyzer's pick is unchanged: the spot is kept.
        self.assertFalse(circuit._upgrade_progression(client, rebuilt))
        self.assertEqual(circuit.spot["name"], "Trees")
        # An emergency recall there teaches again, and the rebuilt task keeps
        # the recall in flight instead of firing another.
        rebuilt._emergency_recall_cast_at = 123.0
        rebuilt._emergency_recall_origin = self.TREES
        client.decision_history.append({
            "time": time.time() + 2, "action": "emergency-recall", "detail": "",
            "task": "", "map": self.TREES})
        self.assertFalse(circuit._observe_farm_death(client))
        self.assertEqual(client.state.farm_adaptations[trees["path"]]["level"], 2)
        self.assertIsNot(circuit.child.task, rebuilt)
        self.assertEqual((circuit.child.task._emergency_recall_cast_at,
                          circuit.child.task._emergency_recall_origin),
                         (123.0, self.TREES))
        # Once the pick is reached, a failure elsewhere does not count.
        circuit.child.navigation.complete()
        client.decision_history.append({
            "time": time.time() + 3, "action": "death", "detail": "",
            "task": "", "map": "/elsewhere"})
        self.assertFalse(circuit._observe_farm_death(client))
        self.assertEqual(client.state.farm_adaptations[trees["path"]]["level"], 2)

    def test_route_death_in_combat_teaches_a_route_hazard(self):
        from .navigation import FarmCircuitTask

        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp = "/shattered_islands/world_1_50"
        # Killed by a crocodile pack while still on the way to the spot.
        before = time.time()
        client.decision_history.append({"time": before - 1, "action": "step",
                                        "detail": "", "task": "", "map": swamp})
        client.state.add_message(2, "ff0000", "crocodile hit you for 25 (-25) pierce damage.")
        client.decision_history.append({
            "time": time.time() + 1, "action": "death", "detail": "",
            "task": "", "map": swamp})
        circuit._observe_farm_death(client)
        until = client.state.route_hazards[swamp]
        self.assertAlmostEqual(until - before, FarmCircuitTask.ROUTE_HAZARD_SECONDS, delta=10)
        self.assertIn("route-hazard-learned",
                      [entry["action"] for entry in client.decision_history])

    def test_hits_taken_on_an_earlier_map_teach_no_route_hazard(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp, meadow = "/shattered_islands/world_1_50", "/shattered_islands/world_1_49"
        now = time.time()
        # The crossing step is recorded on the swamp before the server moves
        # her, so the crocodile's hit can land after it but still on the swamp.
        client.decision_history.append({"time": now - 0.5, "action": "step",
                                        "detail": "", "task": "", "map": swamp})
        client.state.add_message(2, "ff0000", "crocodile hit you for 25 (-25) pierce damage.")
        # Then on the meadow, she died there without a fight.
        client.decision_history.append({"time": time.time() + 0.5, "action": "step",
                                        "detail": "", "task": "", "map": meadow})
        client.decision_history.append({"time": time.time() + 1, "action": "death",
                                        "detail": "", "task": "", "map": meadow})
        circuit._observe_farm_death(client)
        self.assertEqual(client.state.route_hazards, {})

    def test_an_immediate_route_death_is_blamed_on_the_map_she_arrived_on(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp = "/shattered_islands/world_1_50"
        now = time.time()
        # She arrived and was killed before sending another order.
        client.map_arrivals = [(now - 5, swamp)]
        client.state.messages.append((now - 2, 2, "ff0000",
                                      "crocodile hit you for 99 (-99) pierce damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": swamp})
        circuit._observe_farm_death(client)
        self.assertIn(swamp, client.state.route_hazards)

    def test_a_hit_after_respawning_is_not_the_death_maps(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp, bed = "/shattered_islands/world_1_50", "/shattered_islands/world_0_68"
        now = time.time()
        client.map_arrivals = [(now - 30, swamp), (now + 0.5, bed)]
        # A fightless death, then a hit right after respawning by the bed.
        client.state.messages.append((now + 1, 2, "ff0000",
                                      "rat hit you for 3 (-3) impact damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": swamp})
        circuit._observe_farm_death(client)
        self.assertEqual(client.state.route_hazards, {})

    def test_old_hits_on_the_map_teach_no_route_hazard(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp = "/shattered_islands/world_1_50"
        now = time.time()
        # A scratch five minutes ago, then a death without a fight.
        client.map_arrivals = [(now - 600, swamp)]
        client.state.messages.append((now - 300, 2, "ff0000",
                                      "crocodile hit you for 5 (-5) pierce damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": swamp})
        circuit._observe_farm_death(client)
        self.assertEqual(client.state.route_hazards, {})

    def test_starving_on_the_way_teaches_no_route_hazard(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        client.state.add_message(2, "ffffff", "Sera starved to death.")
        client.decision_history.append({
            "time": time.time() + 1, "action": "death", "detail": "",
            "task": "", "map": "/shattered_islands/world_6_46"})
        circuit._observe_farm_death(client)
        self.assertEqual(client.state.route_hazards, {})

    def test_player_chat_never_teaches_a_route_hazard(self):
        from . import self_improvement

        # Anyone can type a hit line or a death notice; only the server's own
        # lines count, so chat cannot mark a map hazardous.
        messages = [
            (10.0, c.CHAT_TYPE_CHAT, "ffffff", "crocodile hit you for 25 damage"),
            (11.0, c.CHAT_TYPE_PRIVATE, "ffffff", "crocodile hit you for 25 damage"),
            (12.0, 2, "ffffff", "[a=#charname]Mallory[/a] hit you for 25 damage"),
            (13.0, c.CHAT_TYPE_LOCAL, "ffffff", "Sera starved to death."),
        ]
        self.assertEqual(self_improvement.hits_between(messages, 0.0, 20.0), {})
        review = self_improvement.review_failure(
            messages, [], {"time": 14.0, "action": "death", "map": "/a"}, ["/a"])
        self.assertEqual((review.damage_taken, review.attackers), (0, {}))
        self.assertNotEqual(review.cause, "starvation")
        # The server's own line still counts.
        messages.append((13.5, 2, "ffffff", "crocodile hit you for 25 damage"))
        self.assertEqual(self_improvement.hits_between(messages, 0.0, 20.0), {"crocodile": 25})
        # And a chat line quoting a death notice is no death.
        client = AtrinikClient(ClientConfig(character="Sera"))
        for msg_type, text in ((c.CHAT_TYPE_CHAT, "[a=#charname]Mallory[/a]: you have died"),
                               (c.CHAT_TYPE_PRIVATE, "you have died, Sera")):
            data = bytes((msg_type,)) + b"ffffff\0" + text.encode() + b"\0"
            asyncio.run(client._handle_drawinfo(Cursor(data), data))
        self.assertNotIn("death", [entry["action"] for entry in client.action_history])

    def test_only_this_life_explains_a_route_death(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        swamp, meadow = "/shattered_islands/world_1_50", "/shattered_islands/world_1_49"
        now = time.time()
        # She starved, respawned, was bitten by a wolf on the meadow, then
        # killed by a crocodile on the swamp within the same minute.
        client.state.messages.append((now - 40, 2, "ffffff", "Sera starved to death."))
        client.decision_history.append({"time": now - 39, "action": "death", "detail": "",
                                        "task": "", "map": meadow})
        client.state.messages.append((now - 30, 2, "ff0000", "wolf hit you for 9 (-9) bite damage."))
        client.map_arrivals = [(now - 38, meadow), (now - 20, swamp)]
        client.state.messages.append((now - 5, 2, "ff0000",
                                      "crocodile hit you for 40 (-40) pierce damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": swamp})
        circuit._observe_farm_death(client)
        # The earlier starvation does not excuse the swamp, and the lesson
        # names only what hit her there.
        self.assertIn(swamp, client.state.route_hazards)
        learned = [entry["detail"] for entry in client.decision_history
                   if entry["action"] == "route-hazard-learned"]
        self.assertEqual(learned, [f"{swamp} for 6 h: 40 damage from crocodile"])

    def test_a_death_within_a_second_of_the_last_still_splits_lives(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        now = time.time()
        # Out of heals in the previous life, which ended under a second ago.
        client.decision_history.append({"time": now - 0.8, "action": "emergency-disengage",
                                        "detail": "hp=20/134 sp=1 next-heal=4 target=0",
                                        "task": "", "map": self.TREES})
        client.decision_history.append({"time": now - 0.5, "action": "death", "detail": "",
                                        "task": "", "map": self.TREES})
        # This life: one treant out-damages her at the spot.
        client.state.messages.append((now - 0.2, 2, "ff0000",
                                      "evil treant hit you for 90 (-90) impact damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": self.TREES})
        circuit._observe_farm_death(client)
        reviews = [entry["detail"] for entry in client.decision_history
                   if entry["action"] == "farm-failure-review"]
        self.assertNotIn("resources", reviews[-1])

    def test_the_previous_lifes_decisions_do_not_explain_this_death(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        now = time.time()
        # Out of heals in the previous life, which ended 40 s ago.
        client.decision_history.append({"time": now - 45, "action": "emergency-disengage",
                                        "detail": "hp=20/134 sp=1 next-heal=4 target=0",
                                        "task": "", "map": self.TREES})
        client.decision_history.append({"time": now - 40, "action": "death", "detail": "",
                                        "task": "", "map": self.TREES})
        # This life: one treant out-damages her at the spot.
        client.state.messages.append((now - 5, 2, "ff0000",
                                      "evil treant hit you for 90 (-90) impact damage."))
        client.decision_history.append({"time": now, "action": "death", "detail": "",
                                        "task": "", "map": self.TREES})
        circuit._observe_farm_death(client)
        reviews = [entry["detail"] for entry in client.decision_history
                   if entry["action"] == "farm-failure-review"]
        self.assertIn("out-damaged", reviews[-1])
        self.assertNotIn("resources", reviews[-1])

    def test_any_detour_beats_a_learned_map(self):
        def lane(path):
            node = MapNode(path, width=3, height=1)
            node.terrain = {(x, 0): 1 for x in range(3)}
            return node

        graph = WorldGraph()
        town, swamp, farm = ("/shattered_islands/world_0_68", "/shattered_islands/world_1_50",
                             "/shattered_islands/world_1_49")
        detour = [f"/shattered_islands/detour_{n}" for n in range(1010)]
        for path in (town, swamp, farm, *detour):
            graph.nodes[path] = lane(path)
            graph.named_spawns[path] = []
        graph.nodes[town].edges = [MapEdge(town, swamp, 2, 0, 0, 0), MapEdge(town, detour[0], 1, 0, 0, 0)]
        graph.nodes[swamp].edges = [MapEdge(swamp, farm, 2, 0, 0, 0)]
        for here, there in zip(detour, [*detour[1:], farm]):
            graph.nodes[here].edges = [MapEdge(here, there, 2, 0, 0, 0)]
        # More than a thousand maps around one lesson is still the way.
        route = graph.route_points(town, (1, 0), farm, [], penalized_maps={swamp})
        self.assertNotIn(swamp, [edge.destination for edge in route])
        self.assertEqual(len(route), 1011)
        self.assertEqual([edge.destination for edge in graph.route_points(town, (1, 0), farm, [])],
                         [swamp, farm])

    def test_depletion_prefers_safety_spots_then_falls_back_to_the_choice(self):
        from .navigation import analyzer_farm_selection

        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        pocket = analyzer_spot("Pocket", [self.POCKET], ["evil treant L13"])
        client = self.client({"xp": [trees], "safety": [pocket]})
        client.state.player_tag = 7
        client.state.place_item(Item(
            501, item_type=c.TYPE_FORCE, quality=0, name="depletion"), 7)
        client.state.depletion_points_known = True
        client.state.depletion_points = 32
        circuit = self.circuit(client, trees)
        self.assertTrue(circuit._upgrade_progression(client, circuit.child.task))
        self.assertEqual(circuit.spot["name"], "Pocket")
        selected = [entry["detail"] for entry in client.decision_history
                    if entry["action"] == "farm-spot-selected"]
        self.assertIn("(safety)", selected[-1])
        # With no usable safety spot, the chosen objective's spot applies: the
        # circuit keeps it, removing depletion before any farming, instead of
        # ending and leaving autoplay to hold where nothing can cure her.
        client.farm_analyzer.rankings["safety"] = []
        circuit = self.circuit(client, trees)
        self.assertFalse(circuit._upgrade_progression(client, circuit.child.task))
        self.assertNotEqual(circuit.status, TaskStatus.COMPLETE)
        self.assertEqual(circuit.spot["name"], "Trees")
        # Autoplay starting depleted still gets a circuit to cure her from.
        self.assertEqual(analyzer_farm_selection(client, built_graph())[1]["name"],
                         "Trees")
        # A safety circuit whose spot became unusable returns to the choice.
        circuit = self.circuit(client, pocket)
        self.assertTrue(circuit._upgrade_progression(client, circuit.child.task))
        self.assertEqual(circuit.spot["name"], "Trees")
        selected = [entry["detail"] for entry in client.decision_history
                    if entry["action"] == "farm-spot-selected"]
        self.assertIn("(xp)", selected[-1])

    def test_repeated_failures_at_maximum_adaptation_pause_the_spot_briefly(self):
        from . import self_improvement

        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        pocket = analyzer_spot("Pocket", [self.POCKET], ["evil treant L13"])
        client = self.client({"xp": [trees, pocket]})
        now = time.time()
        client.state.farm_adaptations = {trees["path"]: {
            "level": self_improvement.MAX_LEVEL, "failures": [now - 600, now - 300]}}
        circuit = self.circuit(client, trees)
        client.decision_history.append({
            "time": now + 1, "action": "death", "detail": "", "task": "", "map": self.TREES})
        self.assertTrue(circuit._observe_farm_death(client))
        until = client.state.farm_zone_quarantine[self.TREES]
        self.assertAlmostEqual(until - now, self_improvement.PAUSE_SECONDS, delta=10)
        self.assertTrue(circuit._upgrade_progression(client, circuit.child.task))
        self.assertEqual(circuit.spot["name"], "Pocket")

    def test_no_xp_for_ten_minutes_excludes_any_analyzer_pick(self):
        now = [1000.0]
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = FarmCircuitTask(built_graph(), [(self.TREES, "evil treant")],
                                  analyzer_driven=True, spot=trees, clock=lambda: now[0])
        circuit.child = circuit._new_child()
        client.state.map.path = self.TREES
        farm = circuit.child.task
        def farm_for(seconds):
            stalled = False
            for _ in range(int(seconds // 30)):
                now[0] += 30
                stalled = circuit._observe_stalled_farm(client, farm) or stalled
            return stalled

        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        self.assertFalse(farm_for(circuit.FARM_NO_PROGRESS_SECONDS - 60))
        circuit._current_exp += 10  # XP resets the watchdog
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        self.assertTrue(farm_for(circuit.FARM_NO_PROGRESS_SECONDS + 60))
        self.assertIn(self.TREES, client.state.farm_zone_quarantine)
        explicit = FarmCircuitTask(built_graph(), [(self.TREES, "evil treant")])
        self.assertFalse(explicit.analyzer_driven)

    def test_failures_from_before_the_circuit_are_not_new(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        # A persisted recall from an earlier run is replayed into the ledger.
        client.decision_history.append({
            "time": time.time() - 3600, "action": "emergency-recall", "detail": "",
            "task": "", "map": self.TREES})
        circuit = self.circuit(client, trees)
        self.assertFalse(circuit._observe_farm_death(client))
        self.assertNotIn(self.TREES, client.state.farm_zone_quarantine)

    def test_time_away_from_the_farm_restarts_the_no_xp_clock(self):
        now = [1000.0]
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = FarmCircuitTask(built_graph(), [(self.TREES, "evil treant")],
                                  analyzer_driven=True, spot=trees, clock=lambda: now[0])
        circuit.child = circuit._new_child()
        client.state.map.path = self.TREES
        farm = circuit.child.task
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        # Nine farmed minutes, then an eleven-minute maintenance trip in which
        # the watchdog is never consulted.
        for _ in range(9):
            now[0] += 60
            self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] += 11 * 60
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        self.assertNotIn(self.TREES, client.state.farm_zone_quarantine)
        # A full ten farmed minutes without XP after returning still counts.
        for _ in range(10):
            now[0] += 60
            stalled = circuit._observe_stalled_farm(client, farm)
        self.assertTrue(stalled)

    def test_unreachable_pick_is_excluded_instead_of_failing_autoplay(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        pocket = analyzer_spot("Pocket", [self.POCKET], ["evil treant L13"])
        client = self.client({"xp": [trees, pocket]})
        circuit = self.circuit(client, trees)
        circuit.child.navigation.status = TaskStatus.FAILED
        circuit.child.navigation.error = "no component route"
        self.assertTrue(circuit._exclude_unreachable_leg(client, circuit.child.task))
        self.assertIn(self.TREES, client.state.farm_zone_quarantine)
        self.assertEqual(circuit.spot["name"], "Pocket")

    def test_excluding_the_last_usable_spot_stops_farming_it(self):
        trees = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [trees]})
        circuit = self.circuit(client, trees)
        farm = circuit.child.task
        circuit.status = TaskStatus.RUNNING
        client.state.farm_zone_quarantine = {self.TREES: time.time() + 60}
        self.assertTrue(circuit._upgrade_progression(client, farm))
        self.assertEqual(circuit.status, TaskStatus.COMPLETE)
        self.assertIn("farm-spots-exhausted",
                      [record["action"] for record in client.decision_history])
        # Without a quarantined current spot, an empty ranking changes nothing.
        client = self.client({"xp": []})
        circuit = self.circuit(client, trees)
        circuit.status = TaskStatus.RUNNING
        self.assertFalse(circuit._upgrade_progression(client, circuit.child.task))
        self.assertEqual(circuit.status, TaskStatus.RUNNING)

    def test_aggressive_picks_get_cautious_margins_and_isolated_pulls(self):
        passive = analyzer_spot("Trees", [self.TREES], ["evil treant L13"])
        client = self.client({"xp": [passive]})
        circuit = self.circuit(client, passive)
        self.assertEqual((circuit.child.task.safety.flee_below,
                          circuit.child.task.safety.heal_below),
                         FarmCircuitTask.PASSIVE_SAFETY)


class SelfImprovementTests(unittest.TestCase):
    SPOT = "/shattered_islands/world_1_50"

    def review(self, lines, decisions=(), *, failure_map=None, action="death"):
        from .self_improvement import review_failure

        now = 1000.0
        messages = [(now - 30 + index, 2, "ff0000", text) for index, text in enumerate(lines)]
        failure = {"time": now, "action": action, "map": failure_map or self.SPOT}
        return review_failure(messages, list(decisions), failure, [self.SPOT])

    def test_review_classifies_what_went_wrong(self):
        pack = self.review(["giant frog hit you for 20 (-20) slash damage.",
                            "crocodile hit you for 25 (-25) pierce damage.",
                            "giant frog hit you for 18 (-18) slash damage."])
        self.assertEqual((pack.cause, pack.peak_attackers), ("pack", 2))
        self.assertEqual(pack.attackers, {"giant frog": 2, "crocodile": 1})
        self.assertEqual(pack.damage_taken, 63)
        starved = self.review(
            ["crocodile hit you for 25 (-25) pierce damage."],
            [{"time": 990.0, "action": "emergency-disengage",
              "detail": "hp=40/188 sp=3 next-heal=8 target=7"}])
        self.assertEqual(starved.cause, "resources")
        burned = self.review(["fire wyvern hit you for 60 (-60) fire damage.",
                              "fire wyvern hit you for 20 (-20) slash damage."])
        self.assertEqual(burned.cause, "damage-type:fire")
        self.assertEqual(burned.damage_by_type, {"fire": 60, "slash": 20})
        untyped = self.review(["giant frog hit you for 20 damage.",
                               "crocodile hit you for 25 damage."])
        self.assertEqual((untyped.cause, untyped.damage_taken), ("pack", 45))
        beaten = self.review(["ogre hit you for 50 (-50) impact damage."])
        self.assertEqual(beaten.cause, "out-damaged")
        self.assertIn("ogre", beaten.detail)
        route = self.review(["ogre hit you for 50 (-50) impact damage."],
                            failure_map="/shattered_islands/world_2_50")
        self.assertEqual(route.cause, "route")
        # Live: starving off the spot was filed as a route death.
        hungry = self.review(["The spell heals you for 12 hp!", "Sera starved to death.",
                              "YOU HAVE DIED."],
                             failure_map="/shattered_islands/world_6_46")
        self.assertEqual((hungry.cause, hungry.detail), ("starvation", "ran out of food"))
        from .self_improvement import remedies, review_failure
        self.assertEqual(remedies(hungry), ("keep food stocked",))
        # A one-shot iterable is scanned for both hits and starvation.
        once = review_failure(iter([(990.0, 2, "", "Sera starved to death.")]), [],
                              {"time": 1000.0, "action": "death",
                               "map": "/shattered_islands/world_6_46"}, [self.SPOT])
        self.assertEqual(once.cause, "starvation")
        # Messages outside the review window are ignored.
        from .self_improvement import review_failure
        old = review_failure([(100.0, 2, "", "giant frog hit you for 1 (-1) slash damage.")],
                             [], {"time": 1000.0, "action": "death", "map": self.SPOT},
                             [self.SPOT])
        self.assertEqual(old.damage_taken, 0)

    def test_learning_raises_the_level_and_pauses_only_as_a_last_resort(self):
        from . import self_improvement as si

        review = self.review(["ogre hit you for 50 (-50) impact damage."])
        record, pause = si.learn({}, review, now=1000.0)
        self.assertEqual((record["level"], pause), (1, False))
        # Failures on the way to the maximum level never count toward a pause.
        for _ in range(si.MAX_LEVEL - 1):
            record, pause = si.learn(record, review, now=1000.0)
            self.assertFalse(pause)
        self.assertEqual((record["level"], record["failures"]), (si.MAX_LEVEL, []))
        for _ in range(si.PAUSE_FAILURES - 1):
            record, pause = si.learn(record, review, now=1000.0)
            self.assertFalse(pause)
        record, pause = si.learn(record, review, now=1000.0)
        self.assertEqual(record["level"], si.MAX_LEVEL)
        self.assertTrue(pause)
        self.assertLessEqual(len(record["reviews"]), si.KEEP_REVIEWS)
        # Old failures age out of the pause window.
        record["failures"] = [1000.0 - si.PAUSE_WINDOW_SECONDS - 1] * 2
        _, pause = si.learn(record, review, now=1000.0)
        self.assertFalse(pause)

    def test_margins_tighten_per_level_up_to_their_caps(self):
        from . import self_improvement as si

        self.assertEqual(si.margins(0, 0.80, 0.95), (0.80, 0.95))
        self.assertEqual(tuple(round(v, 6) for v in si.margins(2, 0.80, 0.95)),
                         (0.86, 0.98))
        self.assertEqual(si.margins(99, 0.80, 0.95), (si.FLEE_CAP, si.HEAL_CAP))

    def test_goals_aggregate_what_to_improve_across_spots(self):
        from .self_improvement import improvement_goals

        goals = improvement_goals({
            "a": {"causes": {"pack": 2, "damage-type:fire": 1}},
            "b": {"causes": {"out-damaged": 3, "resources": 1, "damage-type:fire": 2}}})
        self.assertEqual(goals, {"area_spells": True, "protection": {"fire": 3},
                                 "offence": 3, "healing": 1})


class BuildInfoTests(unittest.TestCase):
    COMMIT = "4a29be882631e570df3028c6e6ead2893b3d4723"

    def test_release_build_shows_tag_and_short_commit(self):
        from .build_info import describe_build

        build = describe_build({"tag": "v1.17.0", "distance": 0,
                                "commit": self.COMMIT, "dirty": False})
        self.assertEqual(build["label"], "v1.17.0 · 4a29be8")
        self.assertEqual(build["version"], "v1.17.0")
        self.assertEqual(build["release_url"],
                         "https://github.com/atrinik/playtester/releases/tag/v1.17.0")
        self.assertEqual(build["commit_url"],
                         f"https://github.com/atrinik/playtester/commit/{self.COMMIT}")

    def test_unreleased_and_unknown_builds_are_labelled_honestly(self):
        from .build_info import describe_build

        ahead = describe_build({"tag": "v1.17.0", "distance": 3,
                                "commit": self.COMMIT, "dirty": True})
        self.assertEqual(ahead["label"], "v1.17.0+3 · 4a29be8 (modified)")
        untagged = describe_build({"tag": "", "distance": 0,
                                   "commit": self.COMMIT, "dirty": False})
        self.assertEqual(untagged["label"], "unreleased · 4a29be8")
        self.assertEqual(untagged["release_url"], "")
        for raw in (None, {}, {"tag": "<script>", "commit": "not-a-sha",
                               "distance": True, "dirty": "yes"}):
            build = describe_build(raw)
            self.assertEqual(build["label"], "unknown build", raw)
            self.assertEqual((build["tag"], build["commit"], build["distance"],
                              build["dirty"], build["commit_url"]),
                             ("", "", 0, False, ""), raw)

    def test_installed_package_records_its_source_commit(self):
        from .build_info import BUILD_INFO_PATH, build_info

        if not BUILD_INFO_PATH.exists():
            self.skipTest("running from sources, not the CMake-built package")
        self.assertRegex(build_info()["commit"], r"^[0-9a-f]{40}$")

    def test_cmake_script_records_tag_distance_and_local_changes(self):
        import shutil
        import subprocess

        cmake, git = shutil.which("cmake"), shutil.which("git")
        if cmake is None or git is None:
            self.skipTest("cmake and git are required")
        # Sources sit at the repository root; the CMake-built copy is below build/.
        script = next((
            parent / "scripts" / "build_info.cmake"
            for parent in Path(__file__).resolve().parents
            if (parent / "scripts" / "build_info.cmake").exists()), None)
        if script is None:
            self.skipTest("build_info.cmake is not in an enclosing checkout")
        from .build_info import describe_build

        with tempfile.TemporaryDirectory() as tmp:
            repo, output = Path(tmp) / "repo", Path(tmp) / "out.json"
            repo.mkdir()

            def run(*args):
                return subprocess.run(
                    [git, "-C", str(repo), "-c", "user.name=t", "-c",
                     "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
                     "-c", "tag.gpgsign=false", *args],
                    check=True, capture_output=True, text=True).stdout.strip()

            def record(source=repo):
                subprocess.run([cmake, f"-DSOURCE_DIR={source}", f"-DOUTPUT={output}",
                                "-P", str(script)], check=True, capture_output=True)
                return describe_build(json.loads(output.read_text()))

            run("init", "-q")
            (repo / "file").write_text("one\n")
            run("add", "file")
            run("commit", "-q", "-m", "one")
            head = run("rev-parse", "HEAD")
            # No release tag yet: still the commit, and local edits still show.
            (repo / "file").write_text("edited\n")
            build = record()
            self.assertEqual((build["tag"], build["commit"], build["dirty"]),
                             ("", head, True))
            self.assertEqual(build["label"], f"unreleased · {head[:7]} (modified)")
            run("checkout", "-q", "--", "file")
            self.assertFalse(record()["dirty"])
            # A copy without its own .git inside another worktree is unknown.
            nested = repo / "vendored"
            nested.mkdir()
            self.assertEqual(record(nested)["label"], "unknown build")
            nested.rmdir()
            run("tag", "v1.2.3")
            head = run("rev-parse", "HEAD")
            self.assertEqual(record()["label"], f"v1.2.3 · {head[:7]}")
            (repo / "file").write_text("two\n")
            run("commit", "-q", "-am", "two")
            head = run("rev-parse", "HEAD")
            build = record()
            self.assertEqual((build["tag"], build["distance"], build["commit"]),
                             ("v1.2.3", 1, head))
            (repo / "file").write_text("three\n")
            self.assertTrue(record()["dirty"])
            # Without Git metadata the build still succeeds, reporting unknown.
            shutil.rmtree(repo / ".git")
            self.assertEqual(record()["label"], "unknown build")


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AtrinikClient(ClientConfig())
        self.engine = TaskEngine(self.client)
        self.server = WebControlServer(
            self.client, self.engine, WorldGraph(), "127.0.0.1", 0)
        self.serve_task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.server is not None:
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(self.server.server)
        self.port = self.server.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.server.close()
        self.serve_task.cancel()
        with suppress(asyncio.CancelledError):
            await self.serve_task

    async def request(self, method: str, path: str, data=None, headers=None):
        body = b"" if data is None else json.dumps(data).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        fields = {"Host": "localhost", **(headers or {})}
        header_text = "".join(
            f"{name}: {value}\r\n" for name, value in fields.items())
        writer.write(
            f"{method} {path} HTTP/1.1\r\n{header_text}"
            f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, payload = raw.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), payload

    async def test_dashboard_farms_a_ranked_spot_with_its_analyzer_safety(self):
        spot = analyzer_spot("Trees", ["/shattered_islands/world_14_79"],
                             ["evil treant L13"], aggressive=2)
        self.client.farm_analyzer = StubAnalyzer({"xp": [spot]})
        self.server.graph = built_graph()
        status, payload = await self.request("POST", "/api/task", {
            "type": "farm_circuit", "analyzer_spot": spot["path"],
            "legs": [{"zone": "/tampered", "target": "x"}]})
        self.assertEqual(status, 200, payload)
        circuit = self.engine.task
        self.assertIsInstance(circuit, FarmCircuitTask)
        self.assertIs(circuit.spot, spot)
        self.assertFalse(circuit.analyzer_driven)
        self.assertEqual(circuit.legs, (("/shattered_islands/world_14_79", "evil treant"),))
        child = circuit._new_child()
        self.assertEqual((child.task.safety.flee_below, child.task.safety.heal_below),
                         FarmCircuitTask.AGGRESSIVE_SAFETY)
        status, _ = await self.request("POST", "/api/task", {
            "type": "farm_circuit", "analyzer_spot": "gone", "legs": []})
        self.assertEqual(status, 400)

    async def test_dashboard_serves_farming_analysis_and_persists_the_objective(self):
        self.client.state.farm_adaptations = {
            "/old": {"level": 1, "updated_at": 1.0, "causes": {"out-damaged": 1}},
            "/trees": {"level": 2, "updated_at": 2.0,
                       "causes": {"pack": 2, "damage-type:fire": 1},
                       "reviews": [{"cause": "pack"}] * 5}}
        status, payload = await self.request("GET", "/api/state?include=farming_analysis")
        self.assertEqual(status, 200)
        analysis = json.loads(payload)["farming_analysis"]
        self.assertEqual(analysis["status"], "unavailable")
        self.assertIsNone(analysis["savebed"])
        learning = analysis["self_improvement"]
        self.assertEqual([spot["spot"] for spot in learning["spots"]], ["/trees", "/old"])
        self.assertEqual(learning["spots"][0]["level"], 2)
        self.assertEqual(len(learning["spots"][0]["reviews"]), 3)
        self.assertEqual(learning["goals"], {"area_spells": True, "protection": {"fire": 1},
                                             "offence": 1, "healing": 0})
        self.assertEqual(analysis["objective"], "xp")
        self.assertEqual(analysis["selected_objective"], "xp")
        self.assertIsNone(analysis["objective_override"])
        self.assertIn("money", analysis["objectives"])
        # Depleted: she farms for safety, and the toggle keeps the choice.
        self.client.state.place_item(Item(
            501, item_type=c.TYPE_FORCE, quality=0, name="depletion"),
            self.client.state.player_tag)
        self.client.state.depletion_points_known = True
        self.client.state.depletion_points = 32
        status, payload = await self.request("GET", "/api/state?include=farming_analysis")
        analysis = json.loads(payload)["farming_analysis"]
        self.assertEqual((analysis["objective"], analysis["selected_objective"]),
                         ("safety", "xp"))
        self.assertEqual(analysis["objective_override"],
                         {"reason": "depletion", "points": 32})
        # No usable safety spot: she farms the choice, and the page says so.
        self.client.farm_analyzer = StubAnalyzer({"xp": [analyzer_spot(
            "Trees", ["/shattered_islands/world_14_79"], ["evil treant L13"])]})
        self.server.dashboard.graph = built_graph()
        status, payload = await self.request("GET", "/api/state?include=farming_analysis")
        analysis = json.loads(payload)["farming_analysis"]
        self.assertEqual(analysis["objective"], "xp")
        self.assertIsNone(analysis["objective_override"])
        self.client.farm_analyzer = None
        self.client.state.depletion_points = 0
        status, payload = await self.request(
            "POST", "/api/action", {"type": "farm_objective", "objective": "money"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.client.state.farm_objective, "money")
        status, _ = await self.request(
            "POST", "/api/action", {"type": "farm_objective", "objective": "fame"})
        self.assertEqual(status, 400)
        self.assertEqual(self.client.state.farm_objective, "money")
        status, page = await self.request("GET", "/")
        self.assertIn(b'id="tab-farming"', page)
        self.assertIn(b'id="fa-deaths"', page)
        self.client.post_mortems = [{"time": 1.0, "cause": "pack"}, {"time": 2.0, "cause": "starvation"}]
        status, payload = await self.request("GET", "/api/state?include=post_mortems")
        self.assertEqual([death["time"] for death in json.loads(payload)["post_mortems"]],
                         [2.0, 1.0])
        self.assertIn(b'id="fa-trends"', page)
        self.client.trends.count("deaths")
        status, payload = await self.request("GET", "/api/state?include=trends")
        self.assertEqual(json.loads(payload)["trends"]["points"][-1]["deaths_per_hour"], 1)
        # Live polls that do not ask for trends never build them.
        built = []
        self.client.trends.summary = lambda: built.append(1) or {"points": []}
        status, payload = await self.request("GET", "/api/state?include=phase")
        self.assertNotIn("trends", json.loads(payload))
        self.assertEqual(built, [])
        self.assertIn(b'id="alerts"', page)
        now = time.time()
        self.client.decision_history.extend([
            {"action": "death", "time": now - 30}, {"action": "death", "time": now - 10}])
        status, payload = await self.request("GET", "/api/state?include=alerts")
        self.assertIn("deaths", [alert["id"] for alert in json.loads(payload)["alerts"]])

    async def test_dashboard_reports_the_running_build(self):
        from .build_info import build_info

        status, payload = await self.request("GET", "/api/state?include=build")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"build": build_info()})
        status, page = await self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="build"', page)

    async def test_dashboard_rejects_remote_binding_and_cross_site_control(self):
        with self.assertRaisesRegex(ValueError, "only listen on loopback"):
            WebControlServer(
                self.client, self.engine, WorldGraph(), "0.0.0.0", 8765)
        status, _ = await self.request(
            "GET", "/api/state", headers={"Host": "attacker.example"})
        self.assertEqual(status, 400)
        status, _ = await self.request(
            "POST", "/api/task", {"type": "stop"},
            headers={"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        status, _ = await self.request(
            "POST", "/api/task", {"type": "stop"},
            headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 200)

    async def test_dashboard_serves_static_face_images_and_resolves_animations(self):
        from .face_images import FaceImages
        png_a = b"\x89PNG\r\n\x1a\nface-a"
        png_b = b"\x89PNG\r\n\x1a\nface-b"
        with tempfile.TemporaryDirectory() as directory:
            lib = Path(directory) / "lib"
            lib.mkdir()
            (lib / "atrinik.0").write_bytes(
                b"IMAGE 0 %d grass.101\n" % len(png_a) + png_a +
                b"IMAGE 1 %d hero.111\n" % len(png_b) + png_b)
            (lib / "animations").write_text(
                "anim hero\nfacings 9\ntrans.101\nhero.111\nmina\n")
            self.server.face_images = FaceImages(Path(directory))
            status, payload = await self.request("GET", "/api/face/grass.101")
            self.assertEqual((status, payload), (200, png_a))
            # Animated objects render statically as their first visible frame.
            status, payload = await self.request("GET", "/api/face/hero")
            self.assertEqual((status, payload), (200, png_b))
            for name in ("missing.101", "..%2Fsecret", "../lib/atrinik.0", ""):
                status, _ = await self.request("GET", f"/api/face/{name}")
                self.assertEqual(status, 404, name)

    async def test_dashboard_state_include_limits_the_snapshot(self):
        status, payload = await self.request("GET", "/api/state?include=phase,stats")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(payload)), {"phase", "stats"})
        status, payload = await self.request("GET", "/api/state")
        self.assertIn("decisions", json.loads(payload))

    async def test_dashboard_can_resume_autoplay_and_items_carry_faces(self):
        from .autoplay import AutoplayTask
        status, payload = await self.request(
            "POST", "/api/task", {"type": "autoplay", "until_level": 50})
        self.assertEqual(status, 200)
        self.assertIsInstance(self.engine.task, AutoplayTask)
        self.assertEqual(self.engine.task.target_level, 50)
        self.client.state.player_tag = 7
        self.client.faces[12] = "apple.101"
        self.client.state.place_item(Item(601, name="apple", face=12), 7)
        status, payload = await self.request("GET", "/api/state?include=inventory")
        apple = next(i for i in json.loads(payload)["inventory"] if i["tag"] == 601)
        self.assertEqual(apple["face"], "apple.101")
        relic = Item(602, name="relic", face=12, flags=(
            c.ITEM_MAGICAL | c.ITEM_DAMNED | c.ITEM_UNPAID | c.ITEM_TRAPPED))
        self.client.state.place_item(relic, 7)
        status, payload = await self.request("GET", "/api/state?include=inventory")
        torch = Item(603, name="torch", face=12, animation=4)
        self.client.animations[4] = "torch_lit"
        self.client.state.place_item(torch, 7)
        status, payload = await self.request("GET", "/api/state?include=inventory")
        torch = next(i for i in json.loads(payload)["inventory"] if i["tag"] == 603)
        # Animated items draw their animation, like the graphical client.
        self.assertEqual(torch["face"], "torch_lit")
        relic = next(i for i in json.loads(payload)["inventory"] if i["tag"] == 602)
        self.assertEqual(
            {k: relic[k] for k in ("magical", "cursed", "damned", "unpaid", "trapped", "locked")},
            {"magical": True, "cursed": True, "damned": True, "unpaid": True,
             "trapped": True, "locked": False})

    async def test_dashboard_pickup_moves_only_an_underfoot_item(self):
        self.client.state.player_tag = 7
        item = Item(501, name="staple food", quantity=5)
        self.client.state.place_item(item, 0)
        moves = []

        async def move_item(destination, tag, quantity=0):
            moves.append((destination, tag, quantity))

        self.client.move_item = move_item
        status, _ = await self.request("POST", "/api/action", {
            "type": "pickup", "tag": 501,
        })
        self.assertEqual(status, 200)
        self.assertEqual(moves, [(7, 501, 5)])
        status, payload = await self.request("POST", "/api/action", {
            "type": "pickup", "tag": 999,
        })
        self.assertEqual(status, 400)
        self.assertIn(b"no longer underfoot", payload)

    async def test_dashboard_exposes_overall_and_skill_xp_progress(self):
        self.client.state.player_tag = 7
        self.client.state.stats.update(level=9, exp=382746)
        skill = Item(503, item_type=c.TYPE_SKILL, name="slash weapons")
        skill.extra.update(level=9, experience=366608)
        self.client.state.place_item(skill, 7)
        status, payload = await self.request("GET", "/api/state")
        snapshot = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["level_progress"]["remaining_experience"],
                         117254)
        slash = next(value for value in snapshot["skills"]
                     if value["name"] == "slash weapons")
        self.assertEqual(slash["experience"], 366608)
        self.assertEqual(slash["next_experience"], 500000)
        self.assertEqual(slash["remaining_experience"], 133392)
        self.assertEqual(slash["progress_percent"], 46.6)

    async def test_dashboard_exposes_named_live_protections(self):
        self.client.state.protections.update({
            c.ATTACK_FIRE: 12, c.ATTACK_ACID: 7,
        })
        status, payload = await self.request("GET", "/api/state")
        snapshot = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["protections"]["fire"], 12)
        self.assertEqual(snapshot["protections"]["acid"], 7)
        self.assertEqual(snapshot["protections"]["impact"], 0)

    async def test_dashboard_exposes_ground_shop_inference_fields(self):
        item = Item(502, flags=c.ITEM_UNPAID, face=12, item_type=0,
                    quality=255, name="steel longsword")
        item.extra.update(inferred_item_skill=16,
                          inferred_skill_name="slash weapons")
        self.client.faces[12] = "longsword.101"
        self.client.state.place_item(item, 0)
        status, payload = await self.request("GET", "/api/state")
        ground = json.loads(payload)["ground"][0]
        self.assertEqual(status, 200)
        self.assertEqual(ground["face"], "longsword.101")
        self.assertEqual(ground["type"], 0)
        self.assertFalse(ground["identified"])
        self.assertTrue(ground["unpaid"])
        self.assertEqual(ground["inferred_skill_name"], "slash weapons")

    async def test_dashboard_creates_container_retrieve_task(self):
        status, _ = await self.request("POST", "/api/task", {
            "type": "retrieve", "container": "chest",
            "patterns": [r"^compass$"],
        })
        self.assertEqual(status, 200)
        self.assertIsInstance(self.engine.task, RetrieveItemsTask)

    async def test_dashboard_applies_only_an_underfoot_fixture(self):
        bed = Item(601, name="bed to reality")
        self.client.state.place_item(bed, 0)
        applied = []

        async def apply(tag):
            applied.append(tag)

        self.client.apply = apply
        status, _ = await self.request("POST", "/api/action", {
            "type": "apply_ground", "tag": 601,
        })
        self.assertEqual(status, 200)
        self.assertEqual(applied, [601])
        status, payload = await self.request("POST", "/api/action", {
            "type": "apply_ground", "tag": 999,
        })
        self.assertEqual(status, 400)
        self.assertIn(b"no longer underfoot", payload)

    async def test_dashboard_and_task_replacement_without_login(self):
        status, page = await self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Atrinik Observer", page)
        self.assertIn(b'data-tab="activity"', page)
        self.assertIn(b"/app.js?v=19", page)
        status, script = await self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function renderGround", script)
        # Post-mortem traces hold the last sample through the death, and one
        # sample draws a flat trace.
        self.assertIn(b"${x(pm.time)},${y(pick, v[v.length - 1])}", script)
        self.assertIn(b'if (!v.length) return "";', script)
        self.assertIn(b"Raw orders", page)
        self.assertNotIn(b'||"<p class="', script)
        status, payload = await self.request("GET", "/api/state")
        snapshot = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(snapshot["quests"]), 16)
        self.assertEqual(snapshot["farm_spots"], [])
        spot = analyzer_spot("Trees", ["/shattered_islands/world_14_79"],
                             ["evil treant L13"], aggressive=2)
        self.client.farm_analyzer = StubAnalyzer({"xp": [spot]})
        spots = DashboardState(self.client, self.engine, built_graph(),
                               load_catalog()).snapshot()["farm_spots"]
        self.assertEqual(spots, [{
            "id": spot["path"], "name": "#1 Trees",
            "circuit": [{"zone": "/shattered_islands/world_14_79",
                         "target": "evil treant"}]}])
        self.assertEqual(len(snapshot["navigation_spots"]),
                         len(NAVIGATION_SPOTS))
        self.assertIn("quest_journal", snapshot)
        self.assertIn("actions", snapshot)
        self.assertIn("decisions", snapshot)
        self.assertIn("current_action", snapshot)
        self.assertTrue(snapshot["inventory_ready"])
        self.assertEqual(snapshot["economy"], {
            "carried": 0, "banked": 0, "bank_known": False, "total": 0,
        })
        self.assertEqual(snapshot["conditions"]["depletion_threshold"], 3)
        self.assertIn("suggested_level", snapshot["quests"][0])

        self.client.state.target_name = "Sera"
        self.client.state.target_id = 0
        self.client.state.stats["target_hp"] = 3
        status, payload = await self.request("GET", "/api/state")
        snapshot = json.loads(payload)
        self.assertEqual(snapshot["target"], {
            "id": 0, "name": "Sera", "combat": False, "hp": 0,
        })

        self.client.state.phase = "playing"
        self.client.state.target_id = 42

        async def no_action(*args, **kwargs):
            return None

        async def clear_target():
            self.client.state.target_id = 0

        self.client.clear_actions = no_action
        self.client.set_combat = no_action
        self.client.clear_target = clear_target
        status, payload = await self.request("POST", "/api/task", {
            "type": "farm", "target": "kobold", "quantity": 2,
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.engine.task.name, "farm:kobold")
        self.assertEqual(self.client.state.target_id, 0)
        self.server.graph.nodes["/a"] = MapNode("/a", width=10, height=10)
        self.server.graph.nodes["/b"] = MapNode("/b", width=10, height=10)
        status, payload = await self.request("POST", "/api/task", {
            "type": "farm_circuit",
            "legs": [{"zone": "/a", "target": "treant"},
                     {"zone": "/b", "target": "lost soul"}],
            "until_level": 10,
            "combat_skill": "wizardry spells",
            "combat_spell": "cause light wounds",
            "combat_skill_until_level": 3,
        })
        self.assertEqual(status, 200)
        self.assertIsInstance(self.engine.task, FarmCircuitTask)
        self.assertEqual(self.engine.task.level_until, 10)
        self.assertEqual(self.engine.task.combat_skill_until_level, 3)
        skill = Item(801, item_type=c.TYPE_SKILL, name="slash weapons",
                     extra={"level": 9})
        weapon = Item(802, flags=c.ITEM_APPLIED,
                      item_type=c.TYPE_WEAPON,
                      required_skill_tag=skill.tag, name="shortsword")
        self.client.state.place_item(skill, self.client.state.player_tag)
        self.client.state.place_item(weapon, self.client.state.player_tag)
        self.client.state.equipment[c.EQUIP_WEAPON] = weapon.tag
        self.client.state.stats["level"] = 10
        self.engine.task._progression_level = (
            self.engine.task._combat_progression_level(self.client))
        status, payload = await self.request("GET", "/api/state")
        trace = json.loads(payload)["task"]["trace"][0]
        self.assertEqual(status, 200)
        self.assertEqual(trace["circuit"]["combat_level"], 9)
        self.assertIn("combat L9", trace["detail"])
        status, _ = await self.request("POST", "/api/task", {"type": "stop"})
        self.assertEqual(status, 200)
        self.assertIsNone(self.engine.task)


if __name__ == "__main__":
    unittest.main()
