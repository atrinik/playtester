"""Command-line runner for observation and autonomous task modes."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

from .client import AtrinikClient, ClientConfig
from .tasks import (BankTask, DepositItemsTask, FarmTask, JunkPolicy,
                    SellJunkTask, TaskEngine)
from .navigation import NavigateTask, NavigateThenTask, WorldGraph
from .quest_tasks import EscapingDesertedIslandTask
from .catalog_quest_tasks import (AllFormalQuestsTask, CatalogQuestTask,
                                  POLICIES)
from .web_server import WebControlServer
from .autoplay import AutoplayTask


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atrinik-playtester")
    p.add_argument("--host", default="host.docker.internal")
    p.add_argument("--port", type=int, default=1728)
    p.add_argument("--transport", choices=("auto", "tcp", "quic"),
                   default="auto")
    p.add_argument("--quic-port", type=int, default=1730)
    p.add_argument(
        "--certificate-sha256",
        default=os.getenv("ATRINIK_BOT_CERTIFICATE_SHA256", ""),
        help="pinned SHA-256 fingerprint required for QUIC",
    )
    p.add_argument("--account", default=os.getenv("ATRINIK_BOT_ACCOUNT", ""))
    p.add_argument("--password", default=os.getenv("ATRINIK_BOT_PASSWORD", ""))
    p.add_argument("--character", default=os.getenv("ATRINIK_BOT_CHARACTER", ""))
    p.add_argument(
        "--join-password",
        default=os.getenv("ATRINIK_BOT_JOIN_PASSWORD", ""),
        help="private server password (prefer ATRINIK_BOT_JOIN_PASSWORD)",
    )
    p.add_argument("--party-name", default=os.getenv("ATRINIK_BOT_PARTY", ""),
                   help="form or rejoin this open party after every login")
    p.add_argument(
        "--chat-rules",
        default=os.getenv("ATRINIK_BOT_CHAT_RULES", ""),
        help="hot-reloadable JSON conversational rules",
    )
    p.add_argument(
        "--runtime-state",
        default=os.getenv("ATRINIK_BOT_RUNTIME_STATE", ""),
        help="SQLite memory path (prefer ATRINIK_BOT_RUNTIME_STATE)",
    )
    p.add_argument(
        "--runtime-content",
        default=os.getenv("ATRINIK_RUNTIME_CONTENT", ""),
        help="collected Atrinik runtime content containing lib/bmaps",
    )
    p.add_argument("--register", action="store_true")
    p.add_argument("--create-character", action="store_true")
    p.add_argument("--archetype", default="half_elf_male")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="task", required=True)
    sub.add_parser("observe")
    web = sub.add_parser("web", help="run the persistent browser dashboard")
    web.add_argument("--listen", default="127.0.0.1")
    web.add_argument("--web-port", type=int, default=8765)
    autoplay = sub.add_parser(
        "autoplay",
        help="bootstrap and continuously progress without dashboard input")
    autoplay.add_argument("--listen", default="127.0.0.1")
    autoplay.add_argument("--web-port", type=int, default=8765)
    autoplay.add_argument("--until-level", type=int, default=115)
    navigate = sub.add_parser("navigate")
    navigate.add_argument("destination")
    navigate.add_argument("--x", type=int)
    navigate.add_argument("--y", type=int)
    quest = sub.add_parser("quest")
    quest.add_argument("--name", default="Escaping the Deserted Island")
    quest.add_argument("--all", action="store_true",
                       help="finish all 16 formal quests in recommended order")
    farm = sub.add_parser("farm")
    farm.add_argument("--zone", default="")
    farm.add_argument("--target", default="")
    farm.add_argument("--item", default="")
    farm.add_argument("--quantity", type=int, default=1)
    farm.add_argument("--until-level", type=int, default=0)
    farm.add_argument("--combat-skill", default="")
    farm.add_argument("--combat-spell", default="")
    farm.add_argument("--combat-skill-until-level", type=int, default=0)
    bank = sub.add_parser("bank")
    bank.add_argument("--banker", required=True)
    bank.add_argument("--amount", default="all")
    sell = sub.add_parser("sell")
    sell.add_argument("--merchant", default="shop-floor",
                      help="legacy task label; stand on a shop floor to sell")
    sell.add_argument("--junk", action="append", required=True)
    deposit = sub.add_parser("deposit")
    deposit.add_argument("--container", required=True)
    deposit.add_argument("--item", action="append", required=True)
    return p


async def main_async(args: argparse.Namespace) -> None:
    config = ClientConfig(
        host=args.host, port=args.port, account=args.account,
        password=args.password, character=args.character,
        join_password=args.join_password,
        party_name=args.party_name,
        transport=args.transport, quic_port=args.quic_port,
        certificate_sha256=args.certificate_sha256,
        chat_rules_path=args.chat_rules,
        runtime_state_path=args.runtime_state,
        runtime_content_path=args.runtime_content,
        register=args.register, create_character=args.create_character,
        character_archetype=args.archetype,
    )
    client = AtrinikClient(config)
    engine = TaskEngine(client)
    client.chat_context_provider = engine.chat_context
    web_server = None
    if args.task == "navigate":
        if (args.x is None) != (args.y is None):
            raise SystemExit("--x and --y must be supplied together")
        xy = None if args.x is None else (args.x, args.y)
        engine.set_task(NavigateTask(WorldGraph().build(), args.destination, xy))
    elif args.task == "quest":
        graph = WorldGraph().build()
        if args.all:
            engine.set_task(AllFormalQuestsTask(graph))
        elif args.name == "Escaping the Deserted Island":
            engine.set_task(EscapingDesertedIslandTask(graph))
        elif args.name in POLICIES:
            engine.set_task(CatalogQuestTask(graph, POLICIES[args.name]))
        else:
            choices = "\n  ".join(
                ("Escaping the Deserted Island", *sorted(POLICIES)))
            raise SystemExit(f"unknown formal quest {args.name!r}; choose:\n  {choices}")
    elif args.task == "farm":
        farm_task = FarmTask(
            zone=args.zone, target=args.target, item=args.item,
            quantity=args.quantity, level_until=args.until_level,
            combat_skill=args.combat_skill, combat_spell=args.combat_spell,
            combat_skill_until_level=args.combat_skill_until_level,
        )
        if args.zone.startswith("/"):
            engine.set_task(NavigateThenTask(
                WorldGraph().build(), args.zone, farm_task,
                combat_approach=True))
        else:
            engine.set_task(farm_task)
    elif args.task == "bank":
        engine.set_task(BankTask(args.banker, args.amount))
    elif args.task == "sell":
        engine.set_task(SellJunkTask(args.merchant, JunkPolicy(tuple(args.junk))))
    elif args.task == "deposit":
        engine.set_task(DepositItemsTask(args.container, tuple(args.item)))
    elif args.task in ("web", "autoplay"):
        graph = WorldGraph().build()
        if args.task == "autoplay":
            engine.set_task(AutoplayTask(
                graph, target_level=args.until_level))
        web_server = WebControlServer(
            client, engine, graph, args.listen, args.web_port)

    async def report(event):
        if event.kind in ("playing", "characters", "map", "quests", "message"):
            value = event.data
            if event.kind == "quests":
                value = {
                    "quests": {name: {
                        "status": quest.status,
                        "parts": [
                            {"name": part.name, "status": part.status,
                             "current": part.current, "required": part.required}
                            for part in quest.parts
                        ],
                    }
                    for name, quest in value.items()
                    },
                    "inventory": [
                        {"tag": item.tag, "name": item.name,
                         "quantity": item.quantity, "location": item.location}
                        for item in client.state.inventory
                    ],
                }
            print(json.dumps({"event": event.kind, "data": value}, default=str))

    if args.task not in ("web", "autoplay"):
        client.add_handler(report)
    tasks = [asyncio.create_task(client.run())]
    if args.task != "observe":
        tasks.append(asyncio.create_task(engine.run()))
    if web_server is not None:
        tasks.append(asyncio.create_task(web_server.serve()))
    try:
        await asyncio.gather(*tasks)
    finally:
        if web_server is not None:
            await web_server.close()
        await client.close()


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
