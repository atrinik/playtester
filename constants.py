"""Classic wire constants, with command IDs supplied by the protocol package."""

import os
from pathlib import Path
import sys

# Development and coordinated integration tests may use the generated bindings
# from the checked-out Classic protocol source. Released playtester installs
# continue to resolve the immutable wheel selected by dependencies.lock.json.
_protocol_source = os.environ.get("ATRINIK_PLAYTESTER_PROTOCOL_SOURCE")
if _protocol_source:
    _protocol_source_path = Path(_protocol_source).expanduser().resolve()
    if not (_protocol_source_path / "atrinik_protocol" / "__init__.py").is_file():
        raise RuntimeError(
            "ATRINIK_PLAYTESTER_PROTOCOL_SOURCE must contain atrinik_protocol"
        )
    sys.path.insert(0, str(_protocol_source_path))

import atrinik_protocol as _protocol

if _protocol_source:
    _protocol_origin = Path(_protocol.__file__).resolve()
    if _protocol_origin != (_protocol_source_path / "atrinik_protocol" / "__init__.py").resolve():
        raise RuntimeError(
            "ATRINIK_PLAYTESTER_PROTOCOL_SOURCE was not imported; remove the "
            "already-loaded incompatible protocol module")

ClientToServerCommand = _protocol.ClientToServerCommand
PROTOCOL_VERSION = _protocol.PROTOCOL_VERSION
ServerToClientCommand = _protocol.ServerToClientCommand
SOCKET_VERSION = PROTOCOL_VERSION

# TARGET's depth-aware framing was introduced by this protocol revision.
REQUIRED_PROTOCOL_VERSION = 1085
if SOCKET_VERSION != REQUIRED_PROTOCOL_VERSION:
    raise RuntimeError(
        f"playtester requires Classic protocol {REQUIRED_PROTOCOL_VERSION}; "
        "install the coordinated protocol wheel or set "
        "ATRINIK_PLAYTESTER_PROTOCOL_SOURCE to its generated/python directory"
    )

try:
    TalkMode = _protocol.TalkMode
    TALK_NPC_TARGET_MESSAGE_MAX = _protocol.TALK_NPC_TARGET_MESSAGE_MAX
except AttributeError as error:
    raise RuntimeError(
        "playtester requires the Classic targeted TALK protocol surface; install "
        "the coordinated protocol wheel or set "
        "ATRINIK_PLAYTESTER_PROTOCOL_SOURCE to its generated/python directory"
    ) from error

# Client -> server packet types.
S_CONTROL = int(ClientToServerCommand.CONTROL)
S_ASK_FACE = int(ClientToServerCommand.ASK_FACE)
S_SETUP = int(ClientToServerCommand.SETUP)
S_VERSION = int(ClientToServerCommand.VERSION)
S_CLEAR = int(ClientToServerCommand.CLEAR)
S_REQUEST_UPDATE = int(ClientToServerCommand.REQUEST_UPDATE)
S_KEEPALIVE = int(ClientToServerCommand.KEEPALIVE)
S_ACCOUNT = int(ClientToServerCommand.ACCOUNT)
S_ITEM_EXAMINE = int(ClientToServerCommand.ITEM_EXAMINE)
S_ITEM_APPLY = int(ClientToServerCommand.ITEM_APPLY)
S_ITEM_MOVE = int(ClientToServerCommand.ITEM_MOVE)
S_ASK_RESOURCE = int(ClientToServerCommand.ASK_RESOURCE)
S_PLAYER_CMD = int(ClientToServerCommand.PLAYER_CMD)
S_ITEM_LOCK = int(ClientToServerCommand.ITEM_LOCK)
S_ITEM_MARK = int(ClientToServerCommand.ITEM_MARK)
S_FIRE = int(ClientToServerCommand.FIRE)
S_QUICKSLOT = int(ClientToServerCommand.QUICKSLOT)
S_QUESTLIST = int(ClientToServerCommand.QUESTLIST)
S_MOVE_PATH = int(ClientToServerCommand.MOVE_PATH)
S_COMBAT = int(ClientToServerCommand.COMBAT)
S_TALK = int(ClientToServerCommand.TALK)
S_MOVE = int(ClientToServerCommand.MOVE)
S_TARGET = int(ClientToServerCommand.TARGET)
S_ACCESS_AUTH = int(ClientToServerCommand.ACCESS_AUTH)

# Server -> client packet types.
C_MAP = int(ServerToClientCommand.MAP)
C_DRAWINFO = int(ServerToClientCommand.DRAWINFO)
C_FILE_UPDATE = int(ServerToClientCommand.FILE_UPDATE)
C_ITEM = int(ServerToClientCommand.ITEM)
C_SOUND = int(ServerToClientCommand.SOUND)
C_TARGET = int(ServerToClientCommand.TARGET)
C_ITEM_UPDATE = int(ServerToClientCommand.ITEM_UPDATE)
C_ITEM_DELETE = int(ServerToClientCommand.ITEM_DELETE)
C_STATS = int(ServerToClientCommand.STATS)
C_IMAGE = int(ServerToClientCommand.IMAGE)
C_ANIM = int(ServerToClientCommand.ANIM)
C_PLAYER = int(ServerToClientCommand.PLAYER)
C_MAPSTATS = int(ServerToClientCommand.MAPSTATS)
C_RESOURCE = int(ServerToClientCommand.RESOURCE)
C_VERSION = int(ServerToClientCommand.VERSION)
C_SETUP = int(ServerToClientCommand.SETUP)
C_CONTROL = int(ServerToClientCommand.CONTROL)
C_PAINTING = int(ServerToClientCommand.PAINTING)
C_CHARACTERS = int(ServerToClientCommand.CHARACTERS)
C_BOOK = int(ServerToClientCommand.BOOK)
C_PARTY = int(ServerToClientCommand.PARTY)
C_QUICKSLOT = int(ServerToClientCommand.QUICKSLOT)
C_COMPRESSED = int(ServerToClientCommand.COMPRESSED)
C_REGION_MAP = int(ServerToClientCommand.REGION_MAP)
C_SOUND_AMBIENT = int(ServerToClientCommand.SOUND_AMBIENT)
C_INTERFACE = int(ServerToClientCommand.INTERFACE)
C_NOTIFICATION = int(ServerToClientCommand.NOTIFICATION)
C_KEEPALIVE = int(ServerToClientCommand.KEEPALIVE)
C_ACCESS_RESULT = int(ServerToClientCommand.ACCESS_RESULT)
C_ACCESS_POLICY = int(ServerToClientCommand.ACCESS_POLICY)

# DRAWINFO chat channels shared with common/toolkit/socket.h.
CHAT_TYPE_GAME = 2
CHAT_TYPE_CHAT = 3
CHAT_TYPE_LOCAL = 4
CHAT_TYPE_PRIVATE = 5
CHAT_TYPE_PARTY = 7
# Channels other players write to: never authoritative game state.
PLAYER_CHAT_TYPES = frozenset({
    CHAT_TYPE_CHAT, CHAT_TYPE_LOCAL, CHAT_TYPE_PRIVATE, CHAT_TYPE_PARTY})

CMD_PARTY_LIST = 1
CMD_PARTY_WHO = 2
CMD_PARTY_JOIN = 3
CMD_PARTY_PASSWORD = 4
CMD_PARTY_LEAVE = 5
CMD_PARTY_UPDATE = 6
CMD_PARTY_REMOVE_MEMBER = 7

SETUP_SOUND = 0
SETUP_MAPSIZE = 1
SETUP_DATA_URL = 2
# Reserved legacy setup subtype. Classic protocol 1085 does not negotiate a
# join password and the server rejects this subtype.
SETUP_JOIN_PASSWORD = 3
SETUP_ASSET_TRANSPORT = 4
SETUP_CONNECTION_MODE = 5

ACCOUNT_LOGIN = 1
ACCOUNT_REGISTER = 2
ACCOUNT_LOGIN_CHAR = 3
ACCOUNT_NEW_CHAR = 4
ACCOUNT_PASSWORD = 5

TARGET_SELF = 0
TARGET_ENEMY = 1
TARGET_NEUTRAL = 2
TARGET_FRIEND = 3
TARGET_MAPXY = 1
TARGET_CLEAR = 2
# MAP2 supports the viewer level plus six levels above and below it.
TARGET_MAX_RELATIVE_DEPTH = 6

TALK_NPC = int(TalkMode.NPC)
TALK_INV = int(TalkMode.INV)
TALK_BELOW = int(TalkMode.BELOW)
TALK_CONTAINER = int(TalkMode.CONTAINER)
TALK_NPC_NAME = int(TalkMode.NPC_NAME)
TALK_CLOSE = int(TalkMode.CLOSE)
TALK_NPC_TARGET = int(TalkMode.NPC_TARGET)

MAP_SAME = 0
MAP_NEW = 1
MAP_CONNECTED = 2
MAP_PARTIAL = 3
MAP2_MAX_DEPTH = 6
MAP2_SUB_LAYERS = 7
MAP2_REAL_LAYERS = 49
# Protocol 1085 retired the timed-light descriptor; this marker bit is
# reserved and rejected in every MAP2 header.
MAP2_CONTINUATION_TIMED_LIGHT = 0x8000
MAP2_ENVELOPE_MAX = 65_534
MAP2_LEVEL_PAYLOAD_MAX = MAP2_ENVELOPE_MAX - 7 - 5
MAP2_MASK_SUPPORT_HEIGHT = 0x01
MAP2_MASK_CLEAR = 0x02
MAP2_MASK_LIGHT_LEVEL = 0x04
MAP2_MASK_LIGHT_LEVEL_MORE = 0x08
MAP2_MASK_HARD_CLEAR = 0x10
MAP2_MASK_FOW = 0x20
# Compatibility names used by the access-admission branch and policy-only map
# callers. All describe the single MAP2 grammar above.
MAP_MAX_DEPTH = MAP2_MAX_DEPTH
MAP_MAX_CONTINUATIONS = 4096
MAP_TIMED_LIGHT = MAP2_CONTINUATION_TIMED_LIGHT
MAP_MASK_SUPPORT_HEIGHT = MAP2_MASK_SUPPORT_HEIGHT
MAP_MASK_HARD_CLEAR = MAP2_MASK_HARD_CLEAR
MAP_MASK_FOW = MAP2_MASK_FOW
MAP_MASK_CLEAR = MAP2_MASK_CLEAR
MAP_MASK_DARKNESS = MAP2_MASK_LIGHT_LEVEL
MAP_MASK_DARKNESS_MORE = MAP2_MASK_LIGHT_LEVEL_MORE
MAP_LAYER_CLEAR = 255
MAP_FLAG_MULTI = 0x01
MAP_FLAG_NAME = 0x02
MAP_FLAG_ANIMATION = 0x04
MAP_FLAG_HEIGHT = 0x08
MAP_FLAG_INFRAVISION = 0x10
MAP_FLAG_ALIGN = 0x20
MAP_FLAG_DOUBLE = 0x40
MAP_FLAG_MORE = 0x80
MAP_FLAG2_ALPHA = 0x01
MAP_FLAG2_ROTATE = 0x02
MAP_FLAG2_ZOOM = 0x04
MAP_FLAG2_TARGET = 0x08
MAP_FLAG2_PROBE = 0x10
MAP_FLAG2_PRIORITY = 0x20
MAP_FLAG2_SECONDPASS = 0x40
MAP_FLAG2_GLOW = 0x80
MAP_FLAG2_ROOF = 0x100
MAP_FLAG2_DOOR = 0x200
MAP_FLAG2_EXIT = 0x400
# Protocol 1085 client lighting: a per-object light emitter, 5 bytes after
# the GLOW payload (i8 radius, u8 flags, u8 red, green, blue).
MAP_FLAG2_EMITTER = 0x800
MAP2_EMITTER_FLICKER = 0x01
MAP2_EMITTER_RESERVED = 0xFE
MAP_EXT_ANIM = 0x01
MAP_EXT_LIGHT_RGB = 0x02
# Protocol 1085 retired the light keyframe (0x04) and added coarse local
# light levels and occluder faces.
MAP_EXT_LOCAL_LIGHT = 0x08
MAP_EXT_OCCLUDER = 0x10
MAP2_LOCAL_LIGHT_LEVEL_MASK = 0x1F
MAP2_LOCAL_LIGHT_RESERVED = 0x40
MAP2_OCCLUDER_RESERVED = 0xC0F0
MAP2_OCCLUDER_VERTICAL_RESERVED = 0xF0
MAP_EXT_RGB16 = MAP_EXT_LIGHT_RGB
ANIM_MOVING = 0x01

IF_TEXT = 0
IF_LINK = 1
IF_ICON = 2
IF_TITLE = 3
IF_INPUT = 4
IF_INPUT_PREPEND = 5
IF_ALLOW_TAB = 6
IF_INPUT_CLEANUP_DISABLE = 7
IF_INPUT_ALLOW_EMPTY = 8
IF_SCROLL_BOTTOM = 9
IF_AUTOCOMPLETE = 10
IF_RESTORE = 11
IF_APPEND_TEXT = 12
IF_ANIM = 13
IF_OBJECT = 14

UPD_LOCATION = 1
UPD_FLAGS = 2
UPD_WEIGHT = 4
UPD_FACE = 8
UPD_NAME = 16
UPD_ANIM = 32
UPD_ANIMSPEED = 64
UPD_NROF = 128
UPD_DIRECTION = 256
UPD_TYPE = 512
UPD_EXTRA = 1024
UPD_GLOW = 2048

ITEM_APPLIED = 1
ITEM_UNPAID = 2
ITEM_MAGICAL = 4
ITEM_CURSED = 8
ITEM_DAMNED = 16
ITEM_CONTAINER_OPEN = 32
ITEM_LOCKED = 64
ITEM_TRAPPED = 128
ITEM_WEAPON_2H = 256
ITEM_NO_SKILL_IDENT = 512

TYPE_PLAYER = 1
TYPE_ROD = 3
TYPE_POTION = 5
TYPE_FOOD = 6
TYPE_BOOK = 8
TYPE_ARROW = 13
TYPE_BOW = 14
TYPE_WEAPON = 15
TYPE_ARMOUR = 16
TYPE_SPELL = 29
TYPE_SHIELD = 33
TYPE_HELMET = 34
TYPE_PANTS = 35
TYPE_MONEY = 36
TYPE_PAINTING = 37
TYPE_AMULET = 39
TYPE_TRINKET = 45
TYPE_DRINK = 54
TYPE_PEARL = 59
TYPE_SKILL = 43
TYPE_GEM = 60
TYPE_EXIT = 66
TYPE_RING = 70
TYPE_FLESH = 72
TYPE_INORGANIC = 73
TYPE_LIGHT_APPLY = 74
TYPE_MONSTER = 80
TYPE_LIGHT_REFILL = 82
TYPE_SPELLBOOK = 85
TYPE_CLOAK = 87
TYPE_BOOTS = 99
TYPE_GLOVES = 100
TYPE_BRACERS = 104
TYPE_WAND = 109
TYPE_SCROLL = 111
TYPE_GIRDLE = 113
TYPE_FORCE = 114
TYPE_JEWEL = 116
TYPE_NUGGET = 117
TYPE_CONTAINER = 122
TYPE_POWER_CRYSTAL = 156
TYPE_CORPSE = 157
TYPE_POISONING = 105

# Spell descriptor flags sent in a spell inventory item's extra metadata.
SPELL_DESC_DIRECTION = 0x02
SPELL_DESC_SELF = 0x04
SPELL_DESC_FRIENDLY = 0x08
SPELL_DESC_ENEMY = 0x10

# Equipment indexes from common/toolkit/socket.h.
EQUIP_AMMO = 0
EQUIP_AMULET = 1
EQUIP_WEAPON = 2
EQUIP_SHIELD = 3
EQUIP_GAUNTLETS = 4
EQUIP_RING_RIGHT = 5
EQUIP_HELM = 6
EQUIP_ARMOUR = 7
EQUIP_BELT = 8
EQUIP_PANTS = 9
EQUIP_BOOTS = 10
EQUIP_CLOAK = 11
EQUIP_BRACERS = 12
EQUIP_WEAPON_RANGED = 13
EQUIP_LIGHT = 14
EQUIP_RING_LEFT = 15
EQUIP_SKILL_ITEM = 16

STAT_HP = 1
STAT_MAXHP = 2
STAT_SP = 3
STAT_MAXSP = 4
STAT_STR = 5
STAT_DEX = 6
STAT_CON = 7
STAT_INT = 8
STAT_POW = 9
STAT_EXP = 11
STAT_LEVEL = 12
STAT_WC = 13
STAT_AC = 14
STAT_DAM = 15
STAT_ARMOUR = 16
STAT_SPEED = 17
STAT_FOOD = 18
STAT_WEAPON_SPEED = 19
STAT_RANGE = 20
STAT_TITLE = 21
STAT_FLAGS = 25
STAT_WEIGHT_LIMIT = 26
STAT_REGEN_HP = 28
STAT_REGEN_SP = 29
STAT_TARGET_HP = 31
STAT_GENDER = 35
STAT_ACTION_TIME = 36
STAT_RANGED_DAM = 37
STAT_RANGED_WC = 38
STAT_RANGED_WS = 39
STAT_PATH_ATTUNED = 40
STAT_PATH_REPELLED = 41
STAT_PATH_DENIED = 42
STAT_EQUIP_START = 100
STAT_EQUIP_END = 116
STAT_PROT_START = 130
STAT_PROT_END = 149

# Protection indices follow server/src/include/attack.h.  Keep the wire
# positions explicit so autonomous danger gates and dashboard telemetry do
# not rely on equipment-name guesses.
ATTACK_IMPACT = 0
ATTACK_SLASH = 1
ATTACK_CLEAVE = 2
ATTACK_PIERCE = 3
ATTACK_WEAPON_MAGIC = 4
ATTACK_FIRE = 5
ATTACK_COLD = 6
ATTACK_ELECTRICITY = 7
ATTACK_POISON = 8
ATTACK_ACID = 9
ATTACK_MAGIC = 10
ATTACK_LIFESTEAL = 11
ATTACK_BLIND = 12
ATTACK_PARALYZE = 13
ATTACK_FORCE = 14
ATTACK_GODPOWER = 15
ATTACK_CHAOS = 16
ATTACK_DRAIN = 17
ATTACK_SLOW = 18
ATTACK_CONFUSION = 19
ATTACK_NAMES = (
    "impact", "slash", "cleave", "pierce", "weapon_magic", "fire",
    "cold", "electricity", "poison", "acid", "magic", "lifesteal",
    "blind", "paralyze", "force", "godpower", "chaos", "drain",
    "slow", "confusion",
)

STAT_NAMES = {
    STAT_HP: "hp", STAT_MAXHP: "maxhp", STAT_SP: "sp", STAT_MAXSP: "maxsp",
    STAT_STR: "str", STAT_DEX: "dex", STAT_CON: "con", STAT_INT: "int",
    STAT_POW: "pow", STAT_EXP: "exp", STAT_LEVEL: "level", STAT_WC: "wc",
    STAT_AC: "ac", STAT_DAM: "dam", STAT_ARMOUR: "armour",
    STAT_SPEED: "speed", STAT_FOOD: "food", STAT_WEAPON_SPEED: "weapon_speed",
    STAT_FLAGS: "flags", STAT_WEIGHT_LIMIT: "weight_limit",
    STAT_REGEN_HP: "regen_hp", STAT_REGEN_SP: "regen_sp",
    STAT_TARGET_HP: "target_hp", STAT_GENDER: "gender",
    STAT_ACTION_TIME: "action_time", STAT_RANGED_DAM: "ranged_dam",
    STAT_RANGED_WC: "ranged_wc", STAT_RANGED_WS: "ranged_ws",
    STAT_PATH_ATTUNED: "path_attuned", STAT_PATH_REPELLED: "path_repelled",
    STAT_PATH_DENIED: "path_denied",
}

STAT_FORMATS = {
    STAT_HP: "i", STAT_MAXHP: "i", STAT_SP: "h", STAT_MAXSP: "h",
    STAT_STR: "B", STAT_DEX: "B", STAT_CON: "B", STAT_INT: "B", STAT_POW: "B",
    STAT_EXP: "Q", STAT_LEVEL: "B", STAT_WC: "H", STAT_AC: "H",
    STAT_DAM: "H", STAT_SPEED: "d", STAT_FOOD: "H", STAT_WEAPON_SPEED: "d",
    STAT_FLAGS: "H", STAT_WEIGHT_LIMIT: "I", STAT_REGEN_HP: "H",
    STAT_REGEN_SP: "H", STAT_TARGET_HP: "B", STAT_GENDER: "B",
    STAT_ACTION_TIME: "f", STAT_RANGED_DAM: "H", STAT_RANGED_WC: "H",
    STAT_RANGED_WS: "f", STAT_PATH_ATTUNED: "I", STAT_PATH_REPELLED: "I",
    STAT_PATH_DENIED: "I",
}

# Atrinik direction numbering and deltas.
DIRECTION_DELTAS = {
    1: (0, -1), 2: (1, -1), 3: (1, 0), 4: (1, 1),
    5: (0, 1), 6: (-1, 1), 7: (-1, 0), 8: (-1, -1),
}
