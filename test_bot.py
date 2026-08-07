import asyncio
import json
import time
import tempfile
import unittest
from unittest.mock import patch
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

from .client import logical_map_path, map_object_visual_name
from .protocol import Cursor, Packet, ProtocolError
from .transport import normalize_certificate_sha256
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
                    TempleServiceTask,
                    equipment_face_catalog)
from .model import GameState, Item, MapObject, MapState
from .model import QuestProgress
from .model import InterfaceState
from .quest_tasks import DialogTask, EscapingDesertedIslandTask
from .web_server import (DashboardState, WebControlServer,
                         experience_progress, experience_thresholds)
from .farm_spots import FARM_SPOTS
from .navigation_spots import NAVIGATION_SPOTS
from . import constants as c
from .autoplay import AutoplayTask


@lru_cache(maxsize=1)
def built_graph():
    return WorldGraph().build()


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
                raise asyncio.TimeoutError

            def close(self):
                self.closed = True

            async def wait_closed(self):
                pass

        async def run():
            client = AtrinikClient(ClientConfig(connect_timeout=0.01))
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
        self.assertEqual(client.faces[treant_id], "bollard_tied_e_2.101")
        obj = MapObject(5, treant_id, 0, c.MAP_FLAG_ANIMATION,
                        animation=treant_id)
        self.assertEqual(map_object_visual_name(client, obj), "treant_evil")
        static = MapObject(5, treant_id, 0, 0)
        self.assertEqual(map_object_visual_name(client, static),
                         "bollard_tied_e_2.101")

    def test_packet(self):
        self.assertEqual(c.SOCKET_VERSION, 1072)
        self.assertEqual(
            Packet(c.S_VERSION).add("I", c.SOCKET_VERSION).encode(),
            b"\x00\x05\x03\x00\x00\x04\x30",
        )

    def test_item_decoder_retains_authoritative_read_flag(self):
        client = AtrinikClient(ClientConfig())
        item = Item(17)
        payload = c.ITEM_NO_SKILL_IDENT.to_bytes(4, "big")
        client._decode_item_fields(Cursor(payload), c.UPD_FLAGS, item)
        self.assertEqual(item.flags, c.ITEM_NO_SKILL_IDENT)

    def test_version_setup_sends_join_password(self):
        client = AtrinikClient(ClientConfig(join_password="test-secret"))
        sent = []

        async def send(packet):
            sent.append(packet.encode())

        client.send = send
        raw = c.SOCKET_VERSION.to_bytes(4, "big")
        asyncio.run(client._handle_version(Cursor(raw), raw))
        self.assertEqual(sent, [
            b"\x00\x15\x02\x00\x00\x01\x11\x11\x02\x00"
            b"\x03test-secret\x00",
        ])

    def test_setup_requires_join_password_acceptance(self):
        client = AtrinikClient(ClientConfig(
            account="a", password="b", join_password="test-secret"))
        sent = []

        async def send(packet):
            sent.append(packet.encode())

        client.send = send
        accepted = bytes((c.SETUP_JOIN_PASSWORD, 1))
        asyncio.run(client._handle_setup(Cursor(accepted), accepted))
        self.assertEqual(client.state.phase, "account")
        self.assertEqual(sent, [b"\x00\x06\x07\x01a\x00b\x00"])

        rejected = AtrinikClient(ClientConfig(
            account="a", password="b", join_password="wrong"))
        payload = bytes((c.SETUP_JOIN_PASSWORD, 0))
        with self.assertRaisesRegex(ProtocolError, "rejected"):
            asyncio.run(rejected._handle_setup(Cursor(payload), payload))

    def test_join_password_rejects_plain_tcp_and_invalid_values(self):
        for password in ("bad\0tail", "x" * 1024):
            client = AtrinikClient(ClientConfig(
                account="a", password="b", join_password=password,
                transport="quic"))
            with self.assertRaises(ValueError):
                asyncio.run(client.connect())
        client = AtrinikClient(ClientConfig(
            account="a", password="b", join_password="test-secret",
            transport="tcp"))
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
                    30, item_type=c.TYPE_FOOD,
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
        self.assertEqual(resumed.waypoints[resumed.index][0],
                         "/shattered_islands/world_6_46")
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
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS),
            combat_spell="magic bullet")

        sweep = circuit._new_upgrade_shopping(client)
        self.assertFalse(sweep.allow_launchers)
        self.assertFalse(sweep._new_child().task.allow_launchers)

        adaptive = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
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
        client.state.place_item(Item(92, name="staple food"), 7)
        self.assertTrue(task._needs_food_resupply(client))
        client.state.inventory_replay_complete = False
        self.assertFalse(task._inventory_ready(client))
        client.state.inventory_replay_complete = True
        client.state.stats["food"] = 300
        self.assertFalse(task._needs_food_resupply(client))
        client.state.stats["food"] = 150
        for tag in range(93, 96):
            client.state.place_item(Item(
                tag, item_type=c.TYPE_FOOD, name=f"fruit {tag}"), 7)
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
                    "player_tag": 9, "messages": [],
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
                    "player_tag": 10, "messages": [],
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
                        43, item_type=c.TYPE_FOOD,
                        name="peeled rambutan")],
                })()
                self.applied = []

            async def apply(self, tag):
                self.applied.append(tag)

        client = FakeClient()
        self.assertFalse(asyncio.run(SafetyPolicy().enforce(client)))
        self.assertEqual(client.applied, [43])

    def test_safety_never_eats_identified_cursed_food(self):
        class FakeClient:
            def __init__(self):
                self.state = type("State", (), {
                    "stats": {"hp": 32, "maxhp": 32, "food": 50},
                    "inventory": [
                        Item(43, flags=c.ITEM_CURSED,
                             item_type=c.TYPE_FOOD,
                             name="staple food of poison"),
                        Item(44, item_type=c.TYPE_FOOD,
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
                self.moves = []
                self.direct_moves = []
                self.clears = 0
                self.cleared_targets = 0

            async def target(self, x, y, target_id=0):
                self.targets.append((x, y, target_id))


            async def set_combat(self, enabled, force=False):
                self.combat.append(enabled)

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

        self.assertTrue(asyncio.run(
            task._defend_while_navigating(client)))
        self.assertTrue(client.direct_moves)
        self.assertEqual(task._last_pull_target, target.target_id)

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
        self.assertTrue(asyncio.run(task._defend_while_navigating(client)))
        self.assertEqual(task._last_pull_target, 0)
        self.assertIsNotNone(task._retreat_attempt)
        self.assertEqual(client.targets, [])
        self.assertEqual(client.combat, [False])

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

    def test_farm_spot_catalog_covers_progression(self):
        graph = built_graph()
        self.assertGreaterEqual(len(FARM_SPOTS), 35)
        self.assertEqual(len({spot.id for spot in FARM_SPOTS}), len(FARM_SPOTS))
        self.assertFalse([spot.zone for spot in FARM_SPOTS
                          if spot.zone not in graph.nodes])
        self.assertFalse([level for level in range(1, 116)
                          if not any(spot.min_level <= level <= spot.max_level
                                     for spot in FARM_SPOTS)])
        self.assertTrue(all(spot.target for spot in FARM_SPOTS
                            if spot.category == "boss"))
        combined = next(spot for spot in FARM_SPOTS
                        if spot.id == "fahrgorm_thrakir")
        self.assertEqual([zone for zone, _ in combined.circuit], [
            "/shattered_islands/world_3_69",
            "/shattered_islands/world_3_68",
            "/shattered_islands/world_3_69",
            "/shattered_islands/world_4_68",
            "/shattered_islands/world_10_78",
        ])
        self.assertEqual(combined.circuit[1][1], "Fahrgorm|evil treant")
        self.assertEqual(combined.circuit[2][1], "giant wasp|wasp_giant|wasp giant")
        self.assertEqual(
            combined.circuit[3][1], "killer bee|bee_killer|bee killer")
        self.assertEqual(
            combined.circuit[4][1], "evil treant|quickwood")
        foothills = next(spot for spot in FARM_SPOTS
                         if spot.id == "giant_foothills")
        self.assertEqual((foothills.min_level, foothills.max_level), (14, 18))
        self.assertEqual([zone for zone, _ in foothills.circuit], [
            "/shattered_islands/world_7_57",
            "/shattered_islands/world_7_56",
        ])
        self.assertIn("Ring of the Ghost — 1/30", combined.notable_drops)
        self.assertIn("19:00–07:00", next(
            spot.notes for spot in FARM_SPOTS if spot.id == "thrakir"))
        late = {spot.id: spot for spot in FARM_SPOTS if spot.id in {
            "eld_passive_trees", "eld_deer", "eld_giant_slugs",
            "eld_red_ants"}}
        self.assertEqual(set(late), {
            "eld_passive_trees", "eld_deer", "eld_giant_slugs",
            "eld_red_ants"})
        self.assertEqual(
            [(late[key].min_level, late[key].max_level, late[key].target)
             for key in ("eld_passive_trees", "eld_deer",
                         "eld_giant_slugs", "eld_red_ants")],
            [(11, 17, "Kotung|evil treant|quickwood"),
             (18, 22, "deer"), (22, 27, "giant slug"),
             (25, 30, "red ant")])
        self.assertEqual(len(late["eld_passive_trees"].circuit), 3)
        self.assertIn("Crossbow Accuracy — 1/40",
                      late["eld_passive_trees"].notable_drops)
        self.assertIn("crocodile", late["eld_deer"].notes)
        combined_data = combined.as_dict(graph)
        self.assertEqual(combined_data["monster_levels"], [5, 6, 7])
        self.assertEqual(combined_data["monster_level_min"], 5)
        self.assertEqual(combined_data["monster_level_max"], 7)
        self.assertIn("treasure_difficulty", combined_data)
        dark_data = next(
            spot for spot in FARM_SPOTS if spot.id == "dark_cave").as_dict(
                graph)
        self.assertEqual(dark_data["monster_levels"], [10, 11, 12])
        ogre_map = "/shattered_islands/world_6_56"
        self.assertEqual(max(graph.map_monster_levels[ogre_map].values()), 8)
        self.assertEqual(
            graph.map_roaming_monster_levels[ogre_map]["stone giant"], 11)
        self.assertEqual(
            graph.map_roaming_monster_levels[ogre_map]["ettin"], 9)
        self.assertIn("acid", late["eld_giant_slugs"].notes)

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
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
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
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
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

    def test_bear_pocket_is_excluded_from_automatic_progression(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
        expected = graph.farm_priorities(
            "/shattered_islands/world_5_69", "grey bear")
        self.assertEqual(len(expected), 4)
        self.assertEqual([spawn.level for spawn in expected], [11, 10, 10, 10])
        self.assertNotIn(("/shattered_islands/world_5_69", "grey bear"),
                         circuit._progression_legs(9))
        self.assertNotIn(("/shattered_islands/world_5_69", "grey bear"),
                         circuit._progression_legs(10))
        self.assertIn("/shattered_islands/world_4_69",
                      NavigateTask.KNOWN_LETHAL_TRANSIT)

    def test_alternate_tree_leg_has_six_isolated_neutral_spawns(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
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
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
        circuit.leg_index = 1
        fahrgorm = circuit._new_child().task
        self.assertTrue(fahrgorm.neutral_targets)
        self.assertNotIn((12, 11), fahrgorm.patrol)
        self.assertTrue(any(max(abs(x - 12), abs(y - 11)) == 1
                            for x, y in fahrgorm.patrol))

    def test_early_circuit_excludes_live_failed_ogre_pockets(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
        self.assertEqual(circuit._progression_legs(8),
                         FarmCircuitTask.EARLY_BASE_LEGS)
        self.assertEqual(circuit._progression_legs(9),
                         FarmCircuitTask.EARLY_BASE_LEGS)
        self.assertEqual(circuit._progression_legs(10),
                         FarmCircuitTask.QUICKWOOD_11_LEGS)
        for level in (11, 12):
            self.assertEqual(
                circuit._progression_legs(level),
                FarmCircuitTask.MID_TREE_LEGS)
        self.assertEqual(circuit._progression_legs(13),
                         FarmCircuitTask.TREE_13_LEGS)
        unsafe_ogre_maps = {
            "/shattered_islands/world_6_56",
            "/shattered_islands/world_5_57",
            "/shattered_islands/world_6_57",
        }
        for level in (9, 10, 11, 12, 13):
            self.assertFalse(any(
                path in unsafe_ogre_maps
                for path, _ in circuit._progression_legs(level)))
        quickwoods = circuit._progression_legs(10)[0]
        self.assertEqual(len(graph.farm_priorities(*quickwoods)), 5)
        self.assertEqual({spawn.level for spawn in
                          graph.farm_priorities(*quickwoods)}, {11, 12})
        mid_trees = circuit._progression_legs(11)[0]
        self.assertEqual(len(graph.farm_priorities(*mid_trees)), 7)
        self.assertTrue(graph.nodes[mid_trees[0]].peaceful_identities &
                        {"evil treant", "quickwood"})
        circuit.legs = circuit._progression_legs(11)
        circuit.leg_index = 0
        tree_farm = circuit._new_child().task
        self.assertTrue(tree_farm.neutral_targets)
        self.assertEqual(tree_farm.safety.flee_below, 0.75)

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

        asyncio.run(autoplay.tick(client))
        self.assertIsInstance(
            autoplay.child, EscapingDesertedIslandTask)
        asyncio.run(autoplay.tick(client))
        self.assertEqual(autoplay.phase, "adaptive-farming")
        self.assertIsInstance(autoplay.child, FarmCircuitTask)
        self.assertTrue(autoplay.child._adaptive_early_progression)
        self.assertEqual(autoplay.child.level_until, 115)

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

    def test_late_progression_uses_only_live_safe_xp_pockets(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
        expected = {
            13: FarmCircuitTask.TREE_13_LEGS,
            14: FarmCircuitTask.TREE_13_LEGS,
            15: FarmCircuitTask.TREE_15_LEGS,
            17: FarmCircuitTask.TREE_15_LEGS,
            18: FarmCircuitTask.TREE_18_LEGS,
            19: FarmCircuitTask.TREE_19_LEGS,
            21: FarmCircuitTask.TREE_19_LEGS,
            22: FarmCircuitTask.TREE_22_LEGS,
            25: FarmCircuitTask.TREE_22_LEGS,
            28: FarmCircuitTask.TREE_22_LEGS,
            30: FarmCircuitTask.UNDERGROUND_CITY_31_LEGS,
        }
        for level, legs in expected.items():
            self.assertEqual(circuit._progression_legs(level), legs)
        self.assertEqual(
            [path for path, _ in FarmCircuitTask.TREE_13_LEGS],
            ["/shattered_islands/world_14_78",
             "/shattered_islands/world_14_79"])
        self.assertEqual(
            [path for path, _ in FarmCircuitTask.TREE_15_LEGS],
            ["/shattered_islands/world_14_77",
             "/shattered_islands/world_14_78",
             "/shattered_islands/world_14_79"])
        self.assertEqual(
            FarmCircuitTask.TREE_18_LEGS,
            (("/shattered_islands/world_14_77", "Kotung|evil treant"),
             ("/shattered_islands/world_14_78", "evil treant")))
        self.assertEqual(
            FarmCircuitTask.TREE_19_LEGS,
            (("/shattered_islands/world_14_77", "Kotung|evil treant"),))
        self.assertEqual(
            [path for path, _ in FarmCircuitTask.ANALYZER_SLASH_18_LEGS],
            ["/shattered_islands/world_4_51"])
        self.assertEqual(
            [path for path, _ in FarmCircuitTask.DEER_TIER_LEGS],
            ["/shattered_islands/world_11_75",
             "/shattered_islands/world_14_77",
             "/shattered_islands/world_14_78",
             "/shattered_islands/world_14_79"])
        self.assertEqual(
            [path for path, _ in FarmCircuitTask.DEER_READINESS_LEGS],
            ["/shattered_islands/world_11_75",
             "/shattered_islands/world_14_77"])
        expected_counts = {
            FarmCircuitTask.TREE_13_LEGS[0]: 8,
            FarmCircuitTask.TREE_15_LEGS[0]: 7,
            FarmCircuitTask.DEER_LEGS[0]: 9,
            FarmCircuitTask.SLUG_LEGS[0]: 9,
            FarmCircuitTask.RED_ANT_LEGS[0]: 7,
        }
        for (path, target), count in expected_counts.items():
            priorities = graph.farm_priorities(path, target)
            self.assertEqual(len(priorities), count)
            peaceful = graph.nodes[path].peaceful_identities
            self.assertTrue(all(
                graph._semantic_name(spawn.named) in peaceful or
                any(graph._semantic_name(name) in peaceful
                    for name in spawn.candidates)
                for spawn in priorities))
            circuit.legs = ((path, target),)
            circuit.leg_index = 0
            farm = circuit._new_child().task
            self.assertTrue(farm.neutral_targets)
            self.assertGreaterEqual(farm.safety.flee_below, 0.75)
            if path == FarmCircuitTask.DEER_LEGS[0][0]:
                self.assertEqual(farm.safety.flee_below, 0.82)
                self.assertEqual(farm.safety.heal_below, 0.95)

    def test_improved_level_twenty_loadout_unlocks_guarded_deer_retrial(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update({
            "level": 20, "maxhp": 194, "ac": 35, "wc": 28, "dam": 38,
        })
        client.state.protections.update({
            c.ATTACK_IMPACT: 43, c.ATTACK_SLASH: 49,
            c.ATTACK_CLEAVE: 54, c.ATTACK_PIERCE: 48,
        })
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))

        self.assertTrue(circuit._deer_readiness(client))
        self.assertEqual(
            circuit._safe_progression_legs(client, 19),
            circuit.DEER_READINESS_LEGS)
        client.state.stats["ac"] = 34
        self.assertFalse(circuit._deer_readiness(client))
        self.assertEqual(
            circuit._safe_progression_legs(client, 19),
            circuit.TREE_19_LEGS)
        client.state.stats["ac"] = 35
        client.state.farm_zone_quarantine[
            circuit.DEER_LEGS[0][0]] = time.time() + 3600
        self.assertEqual(
            circuit._safe_progression_legs(client, 19),
            circuit.TREE_19_LEGS)
        client.state.stats.update({
            "level": 21, "maxhp": 202, "ac": 36, "wc": 29, "dam": 40,
        })
        self.assertTrue(circuit._strakewood_readiness(client))
        self.assertEqual(
            circuit._safe_progression_legs(client, 20),
            circuit.STRAKEWOOD_20_LEGS)
        trial_path = circuit.STRAKEWOOD_20_LEGS[0][0]
        client.state.farm_zone_quarantine[trial_path] = time.time() + 3600
        self.assertEqual(
            circuit._safe_progression_legs(client, 20),
            circuit.STRAKEWOOD_20_LEGS)

    def test_guarded_deer_emergency_is_persistently_quarantined(self):
        graph = built_graph()
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.farm_zone_quarantine = {}
        client.decision_history = [{
            "action": "emergency-disengage",
            "map": FarmCircuitTask.DEER_LEGS[0][0],
        }]
        client.quarantines = []
        client.quarantine_farm_zone = lambda path, until: (
            client.state.farm_zone_quarantine.__setitem__(path, until),
            client.quarantines.append((path, until)))
        client.record_action = lambda action, detail="": None
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.DEER_READINESS_LEGS))

        self.assertTrue(circuit._observe_farm_death(client))
        self.assertEqual(client.quarantines[0][0],
                         FarmCircuitTask.DEER_LEGS[0][0])

        # A capped decision ledger keeps the same length as new records evict
        # old ones. Timestamp tracking must still observe the new emergency.
        now = time.time()
        client.state.farm_zone_quarantine = {}
        client.quarantines.clear()
        client.decision_history = [
            {"action": "status", "map": "/safe", "time": now + index}
            for index in range(2000)
        ]
        capped = FarmCircuitTask(
            graph, list(FarmCircuitTask.DEER_READINESS_LEGS))
        capped._decision_history_index = len(client.decision_history)
        capped._decision_history_time = client.decision_history[-1]["time"]
        client.decision_history.pop(0)
        client.decision_history.append({
            "action": "emergency-disengage",
            "map": FarmCircuitTask.DEER_LEGS[0][0],
            "time": now + 2001,
        })
        self.assertTrue(capped._observe_farm_death(client))

        # A fresh process must also honor a recent persisted emergency instead
        # of immediately replaying the failed guarded trial.
        client.state.farm_zone_quarantine = {}
        restarted = FarmCircuitTask(
            graph, list(FarmCircuitTask.DEER_READINESS_LEGS))
        restarted._decision_history_index = len(client.decision_history)
        restarted._decision_history_time = client.decision_history[-1]["time"]
        self.assertTrue(restarted._observe_farm_death(client))

        # The good aggressive-density farm learns to split and retry rather
        # than treating one imperfect pull as evidence to abandon the map.
        client.state.farm_zone_quarantine = {}
        client.decision_history = [{
            "action": "farm-pack-split",
            "map": FarmCircuitTask.STRAKEWOOD_20_LEGS[0][0],
        }]
        strakewood = FarmCircuitTask(
            graph, list(FarmCircuitTask.STRAKEWOOD_20_LEGS))
        self.assertFalse(strakewood._observe_farm_death(client))
        strake_farm = strakewood._new_child().task
        self.assertEqual(strake_farm.safety.flee_below, 0.82)
        self.assertEqual(strake_farm.safety.heal_below, 0.95)
        self.assertEqual(
            strake_farm.aggressive_detection_ranges["giant frog"], 3)
        priorities = graph.farm_priorities(
            *FarmCircuitTask.STRAKEWOOD_20_LEGS[0])
        self.assertEqual(len(strake_farm.patrol), len(priorities))
        for goal in strake_farm.patrol:
            self.assertEqual(sum(
                max(abs(goal[0] - spawn.x),
                    abs(goal[1] - spawn.y)) <= spawn.aggro_radius
                for spawn in priorities), 1)
            self.assertNotIn(goal, strake_farm.pull_avoidance[goal])

        client.decision_history.append({
            "action": "death",
            "map": FarmCircuitTask.STRAKEWOOD_20_LEGS[0][0],
        })
        self.assertFalse(strakewood._observe_farm_death(client))
        self.assertFalse(client.state.farm_zone_quarantine)

        for path, target in FarmCircuitTask.ANALYZER_SLASH_18_LEGS:
            priorities = graph.farm_priorities(path, target)
            self.assertTrue(priorities)
            self.assertFalse(FarmCircuitTask(
                graph, [(path, target)])._new_child().task.neutral_targets)
            self.assertTrue(all(
                spawn.level <= 16 for spawn in priorities))
            farm = FarmCircuitTask(
                graph, [(path, target)])._new_child().task
            self.assertEqual(farm.safety.flee_below, 0.75)
            self.assertEqual(farm.safety.heal_below, 0.92)
        self.assertEqual(
            graph.farm_max_aggro_pack(
                FarmCircuitTask.ANALYZER_SLASH_18_LEGS[0][0]), 1)
        self.assertEqual(
            graph.farm_max_aggro_pack(
                FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS[0][0]), 1)
        self.assertEqual(
            graph.transit_max_aggro_pack(
                FarmCircuitTask.ANALYZER_SLASH_18_LEGS[0][0]), 3)
        self.assertEqual(
            graph.transit_max_aggro_pack(
                FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS[0][0]), 6)
        self.assertEqual(
            graph.farm_max_aggro_pack(
                FarmCircuitTask.TREE_18_LEGS[1][0]), 1)
        self.assertEqual(
            graph.transit_max_aggro_pack(
                "/shattered_islands/world_6_50"), 3)
        self.assertEqual(
            graph.transit_max_aggro_pack(
                "/shattered_islands/world_6_51"), 1)
        fort_sether = FarmCircuitTask.FORT_SETHER_19_LEGS[0]
        self.assertGreater(graph.farm_max_aggro_pack(fort_sether[0]), 1)
        self.assertEqual(
            [spawn.named for spawn in graph.farm_priorities(*fort_sether)],
            ["spider", "brown bat", "spider", "sword spider", "brown bat"])
        brownrott = next(
            spawn for spawn in graph.named_spawns[fort_sether[0]]
            if spawn.named == "Brownrott")
        self.assertTrue(brownrott.peaceful)
        fort_farm = FarmCircuitTask(
            graph, [fort_sether])._new_child().task
        self.assertFalse(fort_farm.neutral_targets)
        self.assertEqual(fort_farm.safety.flee_below, 0.80)
        self.assertEqual(fort_farm.safety.heal_below, 0.95)
        route_client = AtrinikClient(ClientConfig())
        route_client.state.map = MapState(
            path=FarmCircuitTask.TREE_18_LEGS[0][0],
            world_x=7, world_y=18)
        fort_route = NavigateTask(graph, fort_sether[0])
        fort_route.allow_ranged_hazard_fallback = True
        self.assertTrue(fort_route._plan(route_client))
        self.assertTrue(fort_route._threat_fallback)
        kotung = graph.farm_priorities(*FarmCircuitTask.TREE_15_LEGS[0])
        self.assertEqual(kotung[0].named, "Kotung")
        level_22 = graph.farm_priorities(*FarmCircuitTask.TREE_22_LEGS[0])
        self.assertEqual(
            [(spawn.named, spawn.level) for spawn in level_22],
            [("Kotung", 17), ("evil treant", 16)])

        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 18
        client.state.map = MapState(
            path=FarmCircuitTask.TREE_15_LEGS[0][0],
            world_x=10, world_y=10)
        route = NavigateTask(graph, FarmCircuitTask.DEER_LEGS[0][0])
        with self.assertRaises(ValueError):
            route._plan(client)
        route.allow_ranged_hazard_fallback = True
        edges = route._plan(client)
        self.assertTrue(route._threat_fallback)
        self.assertTrue(route._route_threat_maps)
        for path in [client.state.map.path] + [edge.destination
                                               for edge in edges]:
            node = graph.nodes[path]
            aggressive_levels = [
                level for name, level in
                graph.map_monster_levels.get(path, {}).items()
                if name not in node.peaceful_identities]
            self.assertLessEqual(max(aggressive_levels, default=0), 18)

    def test_autoplay_has_source_audited_reachable_post_30_ladder(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS))
        expected = {
            30: FarmCircuitTask.UNDERGROUND_CITY_31_LEGS,
            36: FarmCircuitTask.HEMLOCK_37_LEGS,
            44: FarmCircuitTask.UNDERGROUND_CITY_46_LEGS,
            52: FarmCircuitTask.UNDERGROUND_CITY_55_LEGS,
            57: FarmCircuitTask.ZECHNA_60_LEGS,
            64: FarmCircuitTask.UNDERGROUND_CITY_68_LEGS,
            76: FarmCircuitTask.UNDERGROUND_CITY_80_LEGS,
            82: FarmCircuitTask.UNDERGROUND_CITY_83_LEGS,
            85: FarmCircuitTask.ZECHNA_88_LEGS,
            92: FarmCircuitTask.ZECHNA_95_LEGS,
            98: FarmCircuitTask.UNDERGROUND_CITY_100_LEGS,
            102: FarmCircuitTask.ZECHNA_99_LEGS,
            115: FarmCircuitTask.ZECHNA_99_LEGS,
        }
        hub = "/shattered_islands/world_0_69"
        previous_threshold = 29
        for threshold, legs in expected.items():
            self.assertGreater(threshold, previous_threshold)
            previous_threshold = threshold
            self.assertEqual(circuit._progression_legs(threshold), legs)
            for path, _ in legs:
                self.assertIn(path, graph.nodes)
                self.assertTrue(graph.route(hub, path))
                levels = list(graph.map_monster_levels[path].values())
                self.assertTrue(levels)
                # Each transition is based on exact authored residents, not
                # the map header's treasure difficulty alone.
                self.assertLessEqual(min(levels), threshold + 6)
                circuit.legs = legs
                circuit.leg_index = 0
                farm = circuit._new_child().task
                self.assertGreaterEqual(farm.safety.flee_below, 0.80)
                self.assertGreaterEqual(farm.safety.heal_below, 0.95)
        endgame_path, endgame_target = FarmCircuitTask.ZECHNA_99_LEGS[0]
        endgame_levels = [
            spawn.level for spawn in
            graph.farm_priorities(endgame_path, endgame_target)
        ]
        self.assertTrue(endgame_levels)
        self.assertGreaterEqual(min(endgame_levels), 82)
        self.assertLessEqual(max(endgame_levels), 99)

    def test_autoplay_demotes_around_persistently_quarantined_farm(self):
        client = AtrinikClient(ClientConfig())
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
        dangerous = FarmCircuitTask.TREE_22_LEGS[0][0]
        client.state.farm_zone_quarantine[dangerous] = time.time() + 3600

        self.assertEqual(
            circuit._safe_progression_legs(client, 28),
            FarmCircuitTask.TREE_13_LEGS)
        client.state.farm_zone_quarantine[dangerous] = time.time() - 1
        self.assertEqual(
            circuit._safe_progression_legs(client, 28),
            FarmCircuitTask.TREE_22_LEGS)

        analyzer_path = FarmCircuitTask.ANALYZER_SLASH_18_LEGS[0][0]
        client.state.farm_zone_quarantine[analyzer_path] = time.time() + 3600
        self.assertEqual(
            circuit._safe_progression_legs(client, 18),
            FarmCircuitTask.TREE_18_LEGS)

        fort_path = FarmCircuitTask.FORT_SETHER_19_LEGS[0][0]
        client.state.farm_zone_quarantine[fort_path] = time.time() + 3600
        self.assertEqual(
            circuit._safe_progression_legs(client, 19),
            FarmCircuitTask.TREE_19_LEGS)

    def test_autoplay_rejects_unaudited_multi_aggro_progression(self):
        client = AtrinikClient(ClientConfig())
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))

        self.assertGreater(
            circuit.graph.farm_max_aggro_pack(
                FarmCircuitTask.UNDERGROUND_CITY_31_LEGS[0][0]), 1)
        self.assertEqual(
            circuit._safe_progression_legs(client, 30),
            FarmCircuitTask.TREE_22_LEGS)

    def test_autoplay_quarantines_current_farm_after_combat_death(self):
        client = AtrinikClient(ClientConfig())
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
        circuit.legs = FarmCircuitTask.UNDERGROUND_CITY_31_LEGS
        path = FarmCircuitTask.UNDERGROUND_CITY_31_LEGS[0][0]
        client.decision_history.append({
            "time": time.time(), "action": "death", "detail": "died",
            "task": circuit.name, "map": path, "x": 4, "y": 5,
        })

        self.assertTrue(circuit._observe_farm_death(client))
        self.assertGreater(
            client.state.farm_zone_quarantine[path], time.time())
        self.assertEqual(
            circuit._safe_progression_legs(client, 30),
            FarmCircuitTask.TREE_22_LEGS)
        self.assertEqual(
            client.decision_history[-1]["action"],
            "farm-zone-quarantine")

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

    def test_autoplay_demotes_high_tier_after_ten_minutes_without_xp(self):
        now = [0.0]
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({
            "level": 30, "exp": 1_000, "hp": 100, "maxhp": 100,
        })
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS),
            clock=lambda: now[0])
        circuit.legs = FarmCircuitTask.UNDERGROUND_CITY_31_LEGS
        circuit.leg_index = 0
        circuit.child = circuit._new_child()
        circuit.child.navigation.complete()
        path = circuit.child.navigation.destination
        client.state.map = MapState(path=path, width=17, height=17)
        circuit._progression_level = 30
        circuit._current_exp = 1_000
        farm = circuit.child.task

        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 599.0
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 601.0
        self.assertTrue(circuit._observe_stalled_farm(client, farm))
        self.assertEqual(
            circuit._safe_progression_legs(client, 30),
            FarmCircuitTask.TREE_22_LEGS)

    def test_analyzer_trial_demotes_after_ten_minutes_without_xp(self):
        now = [0.0]
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({
            "level": 19, "exp": 1_000, "hp": 186, "maxhp": 186,
        })
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS),
            clock=lambda: now[0])
        circuit.legs = FarmCircuitTask.ANALYZER_SLASH_18_LEGS
        circuit.child = circuit._new_child()
        circuit.child.navigation.complete()
        client.state.map = MapState(
            path=circuit.child.navigation.destination,
            width=17, height=17)
        circuit._progression_level = 18
        circuit._current_exp = 1_000
        farm = circuit.child.task

        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 601.0
        self.assertTrue(circuit._observe_stalled_farm(client, farm))
        self.assertEqual(
            circuit._safe_progression_legs(client, 18),
            FarmCircuitTask.TREE_18_LEGS)

    def test_fort_sether_trial_demotes_after_ten_minutes_without_xp(self):
        now = [0.0]
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        client.state.stats.update({
            "level": 19, "exp": 1_000, "hp": 186, "maxhp": 186,
        })
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS),
            clock=lambda: now[0])
        circuit.legs = FarmCircuitTask.FORT_SETHER_19_LEGS
        circuit.child = circuit._new_child()
        circuit.child.navigation.complete()
        client.state.map = MapState(
            path=circuit.child.navigation.destination,
            width=17, height=17)
        circuit._progression_level = 19
        circuit._current_exp = 1_000
        farm = circuit.child.task

        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 601.0
        self.assertTrue(circuit._observe_stalled_farm(client, farm))
        self.assertEqual(
            circuit._safe_progression_legs(client, 19),
            FarmCircuitTask.TREE_19_LEGS)

    def test_high_tier_xp_resets_no_progress_watchdog(self):
        now = [0.0]
        client = AtrinikClient(ClientConfig())
        client.state.phase = "playing"
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS),
            clock=lambda: now[0])
        circuit.legs = FarmCircuitTask.UNDERGROUND_CITY_31_LEGS
        circuit.child = circuit._new_child()
        circuit.child.navigation.complete()
        client.state.map = MapState(
            path=circuit.child.navigation.destination,
            width=17, height=17)
        circuit._progression_level = 30
        circuit._current_exp = 1_000
        farm = circuit.child.task

        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 500.0
        circuit._current_exp = 1_001
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        now[0] = 1_099.0
        self.assertFalse(circuit._observe_stalled_farm(client, farm))
        self.assertFalse(client.state.farm_zone_quarantine)

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
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
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

    def test_wizardry_catchup_uses_directly_pullable_lower_level_farms(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats.update({"level": 18, "hp": 178, "maxhp": 178})
        wizardry = Item(1, item_type=c.TYPE_SKILL,
                        name="wizardry spells")
        wizardry.extra["level"] = 6
        bullet = Item(2, item_type=c.TYPE_SPELL, name="magic bullet")
        client.state.place_item(wizardry, 7)
        client.state.place_item(bullet, 7)
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
        circuit.combat_skill = "wizardry spells"
        circuit.combat_spell = "magic bullet"
        circuit.combat_skill_until_level = 10

        low = circuit._catchup_progression_legs(client)
        self.assertEqual(low, FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS)

        wizardry.extra["level"] = 8
        mid = circuit._catchup_progression_legs(client)
        self.assertEqual(mid, FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS)

        client.state.protections[c.ATTACK_FIRE] = 10
        protected = circuit._catchup_progression_legs(client)
        self.assertEqual(
            protected, FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS)

        bracers = Item(3, item_type=c.TYPE_BRACERS,
                       name="bracers of the fire wyvern")
        client.state.place_item(bracers, 7)
        self.assertNotIn(FarmCircuitTask.DARK_CAVE_WYVERN_LEGS[0],
                         circuit._catchup_progression_legs(client))

    def test_analyzer_selected_wizard_farms_are_separately_pullable(self):
        graph = built_graph()
        source = "/shattered_islands/world_14_78"
        for leg in FarmCircuitTask.WIZARD_MUD_HAND_CATCHUP_LEGS:
            path, target = leg
            priorities = graph.farm_priorities(path, target)
            self.assertTrue(priorities)
            self.assertTrue(graph.route_points(source, (1, 21), path, []))
            self.assertEqual(graph.farm_max_aggro_pack(path), 1)
            self.assertGreater(graph.transit_max_aggro_pack(path), 1)
            self.assertFalse(any(
                any(graph._semantic_name(identity) in
                    graph.nodes[path].peaceful_identities
                    for identity in (spawn.named,) + spawn.candidates)
                for spawn in priorities))
            farm = FarmCircuitTask(graph, [leg])._new_child().task
            self.assertFalse(farm.neutral_targets)

    def test_explicit_combat_training_disables_automatic_build_review(self):
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS),
            combat_skill="bow archery", combat_skill_until_level=5)
        self.assertFalse(circuit._auto_combat_build)

    def test_progression_waits_for_engaged_target_then_rebuilds_circuit(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 11
        circuit = FarmCircuitTask(built_graph(), list(
            FarmCircuitTask.EARLY_SAFE_LEGS))
        circuit.child = circuit._new_child()
        farm = circuit.child.task
        farm._engaged_target = (99, "/farm", 1, 1, time.monotonic())
        self.assertFalse(circuit._upgrade_progression(client, farm))
        farm._engaged_target = None
        self.assertTrue(circuit._upgrade_progression(client, farm))
        self.assertIn("world_14_79", circuit.name)
        self.assertNotIn("world_6_56", circuit.name)

    def test_progression_uses_equipped_weapon_skill_not_overall_level(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 10
        skill = Item(90, item_type=c.TYPE_SKILL, name="slash weapons",
                     extra={"level": 9, "experience": 490_000})
        weapon = Item(91, flags=c.ITEM_APPLIED,
                      item_type=c.TYPE_WEAPON,
                      required_skill_tag=skill.tag, name="iron shortsword")
        client.state.place_item(skill, 7)
        client.state.place_item(weapon, 7)
        client.state.equipment[c.EQUIP_WEAPON] = weapon.tag
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.EARLY_SAFE_LEGS))
        farm = circuit._new_child().task
        self.assertEqual(circuit._combat_progression_level(client), 9)
        self.assertFalse(circuit._upgrade_progression(client, farm))
        skill.extra["level"] = 10
        self.assertTrue(circuit._upgrade_progression(client, farm))
        self.assertEqual(circuit.legs, FarmCircuitTask.QUICKWOOD_11_LEGS)
        skill.extra["level"] = 11
        self.assertTrue(circuit._upgrade_progression(client, farm))
        self.assertEqual(circuit.legs, FarmCircuitTask.MID_TREE_LEGS)
        self.assertIn("world_14_79", circuit.name)
        self.assertNotIn("world_6_56", circuit.name)

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
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS) + [
                ("/shattered_islands/world_1_58", "skeleton")],
            clock=lambda: now[0])
        graveyard_index = len(FarmCircuitTask.EARLY_SAFE_LEGS)
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

    def test_adaptive_circuit_defers_hard_legs_to_safe_levels(self):
        graph = built_graph()
        circuit = FarmCircuitTask(
            graph, list(FarmCircuitTask.EARLY_SAFE_LEGS),
            clock=lambda: 10.0)
        circuit.server_clock.anchor_minute = 20 * 60
        circuit.server_clock.anchor_at = 10.0
        self.assertTrue(circuit._leg_available(0))
        self.assertFalse(circuit._leg_ready(0, 8))
        self.assertEqual(circuit._next_ready_leg(0, 8), 1)
        self.assertFalse(circuit._leg_ready(3, 8))
        self.assertFalse(circuit._leg_ready(3, 9))
        self.assertFalse(circuit._leg_ready(3, 10))
        circuit.server_clock.anchor_minute = 12 * 60
        self.assertTrue(circuit._leg_ready(2, 8))
        self.assertTrue(circuit._leg_ready(3, 8))
        self.assertTrue(circuit._leg_ready(3, 9))
        self.assertFalse(circuit._leg_ready(3, 10))
        self.assertEqual(circuit._next_ready_leg(2, 8), 2)
        circuit.server_clock.anchor_minute = 20 * 60
        self.assertFalse(circuit._leg_ready(0, 10))
        self.assertTrue(circuit._leg_ready(0, 11))
        self.assertEqual(circuit._next_ready_leg(0, 11), 0)

    def test_rare_detour_check_has_persisted_thirty_minute_cooldown(self):
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.WIZARD_LOW_CATCHUP_LEGS))
        path = FarmCircuitTask.WIZARD_LOST_SOUL_LEGS[0][0]
        circuit._farm_zone_last_checked[path] = time.time()
        self.assertFalse(circuit._leg_ready(0, 18))
        self.assertEqual(circuit._next_ready_leg(0, 18), 1)
        circuit._farm_zone_last_checked[path] -= (
            circuit.OPTIONAL_RARE_DETOUR_INTERVAL + 1)
        self.assertTrue(circuit._leg_ready(0, 18))

    def test_unreachable_target_cooldown_survives_circuit_leg_change(self):
        circuit = FarmCircuitTask(
            built_graph(), list(FarmCircuitTask.WIZARD_LOW_CATCHUP_LEGS))
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
        service = circuit._new_depletion_service(client)
        self.assertEqual(service.navigation.destination,
                         "/shattered_islands/world_3_58")
        self.assertEqual(service.navigation.destination_xy, (13, 13))
        self.assertEqual(service.navigation.tolerance, 1)
        self.assertTrue(graph.nodes[service.navigation.destination].walkable(
            *service.navigation.destination_xy))
        self.assertIsInstance(service.task, TempleServiceTask)
        self.assertEqual(service.task.priest, "Saruthar")
        self.assertEqual(service.task.cost, 980)
        self.assertTrue(circuit._queue_depletion_service(client))
        self.assertIsInstance(circuit._restoration.task, TempleServiceTask)
        self.assertEqual(circuit._restoration.task.cost, 980)

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

        client = FakeClient()
        task = TempleServiceTask(
            "Saruthar", "remove depletion", "depletion", 980)
        asyncio.run(task.tick(client))
        self.assertEqual(client.talks,
                         [("buy remove depletion", "Saruthar")])
        self.assertEqual(client.state.bank_balance, 4_524)
        client.state.remove_item(22)
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertEqual(client.state.depletion_points, 0)

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

    def test_old_outpost_trial_uses_elemental_pack_margin(self):
        zone = ("/shattered_islands/strakewood_island/old_outpost/"
                "old_outpost_0101")
        graph = WorldGraph()
        graph.nodes[zone] = MapNode(zone, width=24, height=24)
        child = FarmCircuitTask(
            graph, [(zone, "ice golem")])._new_child()
        farm = child.task
        self.assertEqual((farm.safety.flee_below,
                          farm.safety.heal_below), (0.80, 0.98))
        self.assertIs(child.safety, farm.safety)
        self.assertFalse(child.navigation.allow_ranged_hazard_fallback)
        expedition = FarmCircuitTask(
            graph, [(zone, "ice golem")],
            clear_hostile_route=True)._new_child()
        self.assertTrue(
            expedition.navigation.allow_ranged_hazard_fallback)

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

    def test_apartment_savebed_is_applied_verified_and_persisted(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.place_item(Item(
            800, item_type=106, name="bed to reality"), 0)
        client.applied = []
        client.persisted = False

        async def apply(tag):
            client.applied.append(tag)

        def persist(bound=True):
            client.persisted = bound
            client.state.apartment_bed_bound = bound

        client.apply = apply
        client.set_apartment_bed_bound = persist
        task = BindSavebedTask()
        asyncio.run(task.tick(client))
        self.assertEqual(client.applied, [800])
        client.state.add_message(
            2, "ffffff", "You save and your save bed location is updated.")
        asyncio.run(task.tick(client))
        self.assertEqual(task.status, TaskStatus.COMPLETE)
        self.assertTrue(client.persisted)

    def test_farm_circuit_schedules_one_time_apartment_savebed(self):
        client = type("Client", (), {})()
        client.state = GameState(phase="playing", player_tag=7)
        client.state.stats["level"] = 8
        graph = built_graph()
        circuit = FarmCircuitTask(graph, [("/farm", "rat")])
        self.assertTrue(circuit._needs_apartment_bed(client))
        binding = circuit._new_apartment_bed()
        self.assertEqual(binding.navigation.destination_xy, (4, 1))
        self.assertIsInstance(binding.task, BindSavebedTask)
        client.state.apartment_bed_bound = True
        self.assertFalse(circuit._needs_apartment_bed(client))

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
        # Entering (5, 20) immediately teleports to (3, 20). The ordinary
        # path between two stock tiles inside the shop must walk around that
        # mat instead of planning an impossible post-teleport step.
        path = self.graph.local_path(source, (5, 21), (6, 18))
        self.assertTrue(path)
        self.assertNotIn((5, 20), path)
        self.assertEqual(path[-1], (6, 18))

        # From the opposite component, routing to the same stock tile must
        # deliberately use the paired shop-mat edge.
        route = self.graph.route_points(
            source, (3, 19), source, [(6, 18)])
        self.assertEqual(len(route), 1)
        self.assertTrue(route[0].automatic)
        self.assertEqual((route[0].x, route[0].y), (3, 20))
        self.assertEqual(
            (route[0].destination_x, route[0].destination_y), (5, 20))

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
        self.assertIn(b"Atrinik Operator", page)
        self.assertIn(b"Bot activity", page)
        self.assertIn(b"/app.js?v=8", page)
        status, script = await self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"function renderGround", script)
        self.assertIn(b"Persistent decisions", page)
        self.assertNotIn(b'||"<p class="', script)
        status, payload = await self.request("GET", "/api/state")
        snapshot = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(len(snapshot["quests"]), 16)
        self.assertEqual(len(snapshot["farm_spots"]), len(FARM_SPOTS))
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
