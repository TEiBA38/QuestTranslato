from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from io import BytesIO
import re
from typing import Dict, List, Optional, Tuple


def escape_text(text: str) -> str:
    for match, seq in ((r"%", r"%%"), (r'"', r"\"")):
        text = text.replace(match, seq)
    return text


def filter_text(text: str) -> bool:
    if not text:
        return False
    if text.startswith("{") and text.endswith("}"):
        return False
    if text.startswith("[") and text.endswith("]"):
        return False
    return True


def safe_name(name: str) -> str:
    return re.compile(r"\W+").sub("", name.lower().replace(" ", "_"))


class FileVersion(IntEnum):
    INITIAL = 0
    QUESTS = 1
    SETS = 2
    LORE = 3
    UNCOMPLETED_DISABLED = 4
    LORE_AUDIO = 5
    BAGS = 6
    LOCK = 7
    BAG_LIMITS = 8
    TEAMS = 9
    TEAM_SETTINGS = 10
    DEATHS = 11
    REMOVED_QUESTS = 12
    REPEATABLE_QUESTS = 13
    TRIGGER_QUESTS = 14
    OPTION_LINKS = 15
    NO_ITEM_IDS = 16
    NO_ITEM_IDS_FIX = 17
    PARENT_COUNT = 18
    REPUTATION = 19
    REPUTATION_KILL = 20
    REPUTATION_BARS = 21
    CUSTOM_PRECISION_TYPES = 22
    COMMAND_REWARDS = 23


class Bits(IntEnum):
    BYTE = 8
    SHORT = 16
    INT = 32
    BOOLEAN = 1
    NBT_LENGTH = 15
    NAME_LENGTH = 5
    QUESTS = 10
    TASKS = 4
    REWARDS = 3
    QUEST_SETS = 5
    ITEM_PROGRESS = 30
    QUEST_NAME_LENGTH = 5
    QUEST_DESCRIPTION_LENGTH = 16
    QUEST_POS_X = 9
    QUEST_POS_Y = 8
    TASK_TYPE = 4
    TASK_ITEM_COUNT = 6
    TASK_REQUIREMENT = 32
    ITEM_PRECISION = 30
    GROUP_ITEMS = 6
    GROUP_COUNT = 10
    TIER_COUNT = 7
    WEIGHT = 19
    COLOR = 4
    PASS_CODE = 7
    LIMIT = 10
    BAG_TIER = 3
    DEATHS = 12
    TASK_LOCATION_COUNT = 3
    WORLD_COORDINATE = 32
    LOCATION_VISIBILITY = 2
    HOURS = 32
    REPEAT_TYPE = 2
    TRIGGER_TYPE = 2
    TASK_MOB_COUNT = 3
    KILL_COUNT = 16
    MOB_ID_LENGTH = 10
    REPUTATION = 8
    REPUTATION_VALUE = 32
    REPUTATION_REWARD = 3
    REPUTATION_SETTING = 3
    REPUTATION_MARKER = 5


BAG_TIER_COUNT = 5


def bit_count(bit: Bits, version: int) -> int:
    if bit == Bits.QUESTS and version < FileVersion.SETS:
        return 7
    if bit == Bits.TASK_TYPE and version < FileVersion.REPUTATION_KILL:
        return 3
    if bit == Bits.ITEM_PRECISION and version < FileVersion.CUSTOM_PRECISION_TYPES:
        return 2
    return int(bit)


def bit_max(bit: Bits, version: int) -> int:
    count = bit_count(bit, version)
    if count == 32:
        return 2**31 - 1
    return (1 << count) - 1


def decode_hqm_string(raw: bytes) -> str:
    # HQM 本体は charset 未指定の String.getBytes/new String を使う。
    # Python 側では実用上 UTF-8 を優先し、古い日本語環境向けに CP932 も試す。
    for encoding in ("utf-8", "cp932", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("iso-8859-1", errors="replace")


def encode_hqm_string(text: str, max_len: int) -> Tuple[bytes, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_len:
        return encoded, False

    # byte 上限で切ると日本語の途中 byte を壊すため、1 文字ずつ積む。
    out = bytearray()
    for char in text:
        chunk = char.encode("utf-8")
        if len(out) + len(chunk) > max_len:
            break
        out.extend(chunk)
    return bytes(out), True


@dataclass
class HQMString:
    text: Optional[str]
    raw: bytes = b""
    changed: bool = False
    truncated: bool = False

    @classmethod
    def from_raw(cls, raw: Optional[bytes]) -> "HQMString":
        if raw is None:
            return cls(None, b"")
        return cls(decode_hqm_string(raw), raw)

    def set(self, value: Optional[str]) -> None:
        if value != self.text:
            self.text = value
            self.changed = True

    def to_raw(self, max_len: int) -> bytes:
        if self.text is None:
            return b""
        if not self.changed and len(self.raw) <= max_len:
            return self.raw
        raw, truncated = encode_hqm_string(self.text, max_len)
        self.truncated = truncated
        return raw


@dataclass
class HQMTextEntry:
    key: str
    value: str
    max_bytes: int
    kind: str
    ref: HQMString


class HQMBitReader:
    def __init__(self, data: bytes):
        self.stream = BytesIO(data)
        self.byte_buffer = 0
        self.bit_count_buffer = 0
        self.version = 0

    def read_data(self, bits: int | Bits) -> int:
        count = bit_count(bits, self.version) if isinstance(bits, Bits) else bits
        data = 0
        read_bits = 0

        while True:
            bits_left = count - read_bits
            if self.bit_count_buffer >= bits_left:
                data |= (self.byte_buffer & ((1 << bits_left) - 1)) << read_bits
                self.byte_buffer >>= bits_left
                self.bit_count_buffer -= bits_left
                return data

            data |= self.byte_buffer << read_bits
            read_bits += self.bit_count_buffer
            next_byte = self.stream.read(1)
            self.byte_buffer = next_byte[0] if next_byte else 0
            self.bit_count_buffer = 8

    def read_bool(self) -> bool:
        return self.read_data(Bits.BOOLEAN) != 0

    def read_version(self) -> int:
        self.version = self.read_data(Bits.BYTE)
        return self.version

    def read_string(self, bits: Bits) -> HQMString:
        length = self.read_data(bits)
        if length == 0:
            return HQMString.from_raw(None)
        raw = bytes(self.read_data(Bits.BYTE) for _ in range(length))
        return HQMString.from_raw(raw)

    def read_nbt(self) -> Optional[bytes]:
        if not self.read_bool():
            return None
        length = self.read_data(Bits.NBT_LENGTH)
        return bytes(self.read_data(Bits.BYTE) for _ in range(length))


class HQMBitWriter:
    def __init__(self, version: int):
        self.stream = BytesIO()
        self.byte_buffer = 0
        self.bit_count_buffer = 0
        self.version = version

    def write_data(self, data: int, bits: int | Bits) -> None:
        count = bit_count(bits, self.version) if isinstance(bits, Bits) else bits
        if count == 0:
            return
        data &= (1 << count) - 1

        while True:
            if self.bit_count_buffer + count >= 8:
                bits_to_add = 8 - self.bit_count_buffer
                add_data = data & ((1 << bits_to_add) - 1)
                data >>= bits_to_add
                self.byte_buffer |= add_data << self.bit_count_buffer
                self.stream.write(bytes([self.byte_buffer]))
                self.byte_buffer = 0
                count -= bits_to_add
                self.bit_count_buffer = 0
                if count == 0:
                    return
            else:
                self.byte_buffer |= data << self.bit_count_buffer
                self.bit_count_buffer += count
                return

    def write_bool(self, value: bool) -> None:
        self.write_data(1 if value else 0, Bits.BOOLEAN)

    def write_string(self, value: HQMString, bits: Bits) -> None:
        max_len = bit_max(bits, self.version)
        raw = value.to_raw(max_len)
        if not raw:
            self.write_data(0, bits)
            return
        self.write_data(len(raw), bits)
        for byte in raw:
            self.write_data(byte, Bits.BYTE)

    def write_nbt(self, raw: Optional[bytes]) -> None:
        self.write_bool(raw is not None)
        if raw is None:
            return
        self.write_data(len(raw), Bits.NBT_LENGTH)
        for byte in raw:
            self.write_data(byte, Bits.BYTE)

    def finish(self) -> bytes:
        if self.bit_count_buffer > 0:
            self.stream.write(bytes([self.byte_buffer]))
            self.byte_buffer = 0
            self.bit_count_buffer = 0
        return self.stream.getvalue()


@dataclass
class ItemStackData:
    item: HQMString
    size: Optional[int]
    damage: int
    nbt: Optional[bytes]

    @classmethod
    def read(cls, reader: HQMBitReader, use_size: bool) -> "ItemStackData":
        item = reader.read_string(Bits.SHORT)
        size = reader.read_data(Bits.SHORT) if use_size else None
        damage = reader.read_data(Bits.SHORT)
        nbt = reader.read_nbt()
        return cls(item, size, damage, nbt)

    def write(self, writer: HQMBitWriter, use_size: bool) -> None:
        writer.write_string(self.item, Bits.SHORT)
        if use_size:
            writer.write_data(self.size or 1, Bits.SHORT)
        writer.write_data(self.damage, Bits.SHORT)
        writer.write_nbt(self.nbt)


@dataclass
class ReputationMarkerData:
    name: HQMString
    value: int


@dataclass
class ReputationData:
    id: int
    name: HQMString
    neutral: HQMString
    markers: List[ReputationMarkerData] = field(default_factory=list)


@dataclass
class QuestSetData:
    name: HQMString
    description: HQMString
    reputation_bars: List[int] = field(default_factory=list)


@dataclass
class RepeatInfoData:
    repeat_type: int = 0
    hours_total: Optional[int] = None


@dataclass
class TaskPayloadData:
    kind: int
    data: dict = field(default_factory=dict)


@dataclass
class TaskData:
    task_type: int
    name: HQMString
    description: HQMString
    payload: TaskPayloadData


@dataclass
class QuestData:
    id: int
    exists: bool
    name: Optional[HQMString] = None
    description: Optional[HQMString] = None
    x: int = 0
    y: int = 0
    big_icon: bool = False
    set_id: int = 0
    icon: Optional[ItemStackData] = None
    requirements: List[int] = field(default_factory=list)
    option_links: List[int] = field(default_factory=list)
    repeat: RepeatInfoData = field(default_factory=RepeatInfoData)
    trigger_type: int = 0
    trigger_tasks: Optional[int] = None
    parent_requirement_count: Optional[int] = None
    tasks: List[TaskData] = field(default_factory=list)
    rewards: Optional[List[ItemStackData]] = None
    reward_choices: Optional[List[ItemStackData]] = None
    command_rewards: List[HQMString] = field(default_factory=list)
    reputation_rewards: List[Tuple[int, int]] = field(default_factory=list)


@dataclass
class TierData:
    name: HQMString
    color: int
    weights: List[int]


@dataclass
class GroupData:
    id: int
    name: HQMString
    tier_id: int
    items: List[ItemStackData]
    limit: int = 0


@dataclass
class HQMFile:
    version: int
    pass_code: HQMString
    main_description: HQMString
    quest_sets: List[QuestSetData]
    reputations: List[ReputationData]
    quests: List[QuestData]
    tiers: List[TierData]
    groups: List[GroupData]

    @classmethod
    def parse(cls, data: bytes) -> "HQMFile":
        reader = HQMBitReader(data)
        version = reader.read_version()
        if version < FileVersion.NO_ITEM_IDS:
            raise ValueError("HQM files older than NO_ITEM_IDS are not supported yet.")

        pass_code = reader.read_string(Bits.PASS_CODE) if version >= FileVersion.LOCK else HQMString.from_raw(None)
        main_description = (
            reader.read_string(Bits.QUEST_DESCRIPTION_LENGTH)
            if version >= FileVersion.LORE
            else HQMString.from_raw(b"No description")
        )

        quest_sets = cls._read_sets(reader, version)
        reputations = cls._read_reputations(reader, version)
        quests = cls._read_quests(reader, version)
        tiers, groups = cls._read_bags(reader, version)
        return cls(version, pass_code, main_description, quest_sets, reputations, quests, tiers, groups)

    def to_bytes(self) -> bytes:
        writer = HQMBitWriter(self.version)
        writer.write_data(self.version, Bits.BYTE)
        if self.version >= FileVersion.LOCK:
            writer.write_string(self.pass_code, Bits.PASS_CODE)
        if self.version >= FileVersion.LORE:
            writer.write_string(self.main_description, Bits.QUEST_DESCRIPTION_LENGTH)

        self._write_sets(writer)
        self._write_reputations(writer)
        self._write_quests(writer)
        self._write_bags(writer)
        return writer.finish()

    def extract_lang_dict(self, modpack_name: str) -> Dict[str, str]:
        lang_dict: Dict[str, str] = {}
        for entry in self.text_entries(modpack_name):
            if filter_text(entry.value):
                lang_dict[entry.key] = escape_text(entry.value)
        return lang_dict

    def apply_lang_dict(self, modpack_name: str, lang_dict: Dict[str, str]) -> List[str]:
        warnings: List[str] = []
        for entry in self.text_entries(modpack_name):
            if entry.key not in lang_dict:
                continue
            entry.ref.set(str(lang_dict[entry.key]))
            raw, truncated = encode_hqm_string(entry.ref.text or "", entry.max_bytes)
            if truncated:
                warnings.append(f"{entry.key}: {len(raw)}/{entry.max_bytes} bytes after truncation")
        return warnings

    def text_entries(self, modpack_name: str) -> List[HQMTextEntry]:
        prefix = f"{safe_name(modpack_name)}.hqm"
        entries: List[HQMTextEntry] = []

        def add(key: str, ref: Optional[HQMString], bits: Bits, kind: str) -> None:
            if ref is None or ref.text is None:
                return
            entries.append(HQMTextEntry(f"{prefix}.{key}", ref.text, bit_max(bits, self.version), kind, ref))

        add("main.description", self.main_description, Bits.QUEST_DESCRIPTION_LENGTH, "main_description")
        for set_id, quest_set in enumerate(self.quest_sets):
            add(f"sets.{set_id}.name", quest_set.name, Bits.QUEST_NAME_LENGTH, "set_name")
            add(f"sets.{set_id}.desc", quest_set.description, Bits.QUEST_DESCRIPTION_LENGTH, "set_description")

        for reputation in self.reputations:
            add(f"reputations.{reputation.id}.name", reputation.name, Bits.QUEST_NAME_LENGTH, "reputation_name")
            add(f"reputations.{reputation.id}.neutral", reputation.neutral, Bits.QUEST_NAME_LENGTH, "reputation_neutral")
            for marker_id, marker in enumerate(reputation.markers):
                add(
                    f"reputations.{reputation.id}.markers.{marker_id}.name",
                    marker.name,
                    Bits.QUEST_NAME_LENGTH,
                    "reputation_marker",
                )

        for quest in self.quests:
            if not quest.exists:
                continue
            add(f"quests.{quest.id}.name", quest.name, Bits.QUEST_NAME_LENGTH, "quest_name")
            add(f"quests.{quest.id}.desc", quest.description, Bits.QUEST_DESCRIPTION_LENGTH, "quest_description")
            for task_id, task in enumerate(quest.tasks):
                add(f"quests.{quest.id}.tasks.{task_id}.name", task.name, Bits.QUEST_NAME_LENGTH, "task_name")
                add(f"quests.{quest.id}.tasks.{task_id}.desc", task.description, Bits.QUEST_DESCRIPTION_LENGTH, "task_description")
                if task.task_type == 2:
                    for loc_id, location in enumerate(task.payload.data["locations"]):
                        add(
                            f"quests.{quest.id}.tasks.{task_id}.locations.{loc_id}.name",
                            location["name"],
                            Bits.NAME_LENGTH,
                            "location_name",
                        )
                elif task.task_type == 5:
                    for mob_id, mob in enumerate(task.payload.data["mobs"]):
                        add(
                            f"quests.{quest.id}.tasks.{task_id}.mobs.{mob_id}.name",
                            mob["name"],
                            Bits.NAME_LENGTH,
                            "mob_name",
                        )

        for tier_id, tier in enumerate(self.tiers):
            add(f"tiers.{tier_id}.name", tier.name, Bits.QUEST_NAME_LENGTH, "tier_name")
        for group in self.groups:
            add(f"groups.{group.id}.name", group.name, Bits.QUEST_NAME_LENGTH, "group_name")
        return entries

    @staticmethod
    def _read_sets(reader: HQMBitReader, version: int) -> List[QuestSetData]:
        if version < FileVersion.SETS:
            return [QuestSetData(HQMString.from_raw(b"Automatically generated"), HQMString.from_raw(b"This set was automatically generated. All your quests were put in this one."))]
        sets = []
        for _ in range(reader.read_data(Bits.QUEST_SETS)):
            name = reader.read_string(Bits.QUEST_NAME_LENGTH)
            description = reader.read_string(Bits.QUEST_DESCRIPTION_LENGTH)
            bars = []
            if version >= FileVersion.REPUTATION_BARS:
                bars = [reader.read_data(Bits.INT) for _ in range(reader.read_data(Bits.BYTE))]
            sets.append(QuestSetData(name, description, bars))
        return sets

    @staticmethod
    def _write_set(writer: HQMBitWriter, quest_set: QuestSetData) -> None:
        writer.write_string(quest_set.name, Bits.QUEST_NAME_LENGTH)
        writer.write_string(quest_set.description, Bits.QUEST_DESCRIPTION_LENGTH)
        if writer.version >= FileVersion.REPUTATION_BARS:
            writer.write_data(len(quest_set.reputation_bars), Bits.BYTE)
            for bar in quest_set.reputation_bars:
                writer.write_data(bar, Bits.INT)

    def _write_sets(self, writer: HQMBitWriter) -> None:
        writer.write_data(len(self.quest_sets), Bits.QUEST_SETS)
        for quest_set in self.quest_sets:
            self._write_set(writer, quest_set)

    @staticmethod
    def _read_reputations(reader: HQMBitReader, version: int) -> List[ReputationData]:
        if version < FileVersion.REPUTATION:
            return []
        reputations = []
        for _ in range(reader.read_data(Bits.REPUTATION)):
            reputation_id = reader.read_data(Bits.REPUTATION)
            name = reader.read_string(Bits.QUEST_NAME_LENGTH)
            neutral = reader.read_string(Bits.QUEST_NAME_LENGTH)
            markers = [
                ReputationMarkerData(reader.read_string(Bits.QUEST_NAME_LENGTH), reader.read_data(Bits.REPUTATION_VALUE))
                for _ in range(reader.read_data(Bits.REPUTATION_MARKER))
            ]
            reputations.append(ReputationData(reputation_id, name, neutral, markers))
        return reputations

    def _write_reputations(self, writer: HQMBitWriter) -> None:
        if writer.version < FileVersion.REPUTATION:
            return
        writer.write_data(len(self.reputations), Bits.REPUTATION)
        for reputation in self.reputations:
            writer.write_data(reputation.id, Bits.REPUTATION)
            writer.write_string(reputation.name, Bits.QUEST_NAME_LENGTH)
            writer.write_string(reputation.neutral, Bits.QUEST_NAME_LENGTH)
            writer.write_data(len(reputation.markers), Bits.REPUTATION_MARKER)
            for marker in reputation.markers:
                writer.write_string(marker.name, Bits.QUEST_NAME_LENGTH)
                writer.write_data(marker.value, Bits.REPUTATION_VALUE)

    @classmethod
    def _read_quests(cls, reader: HQMBitReader, version: int) -> List[QuestData]:
        quests = []
        for quest_id in range(reader.read_data(Bits.QUESTS)):
            exists = reader.read_bool()
            quest = QuestData(quest_id, exists)
            if not exists:
                quests.append(quest)
                continue

            quest.name = reader.read_string(Bits.QUEST_NAME_LENGTH)
            quest.description = reader.read_string(Bits.QUEST_DESCRIPTION_LENGTH)
            quest.x = reader.read_data(Bits.QUEST_POS_X)
            quest.y = reader.read_data(Bits.QUEST_POS_Y)
            quest.big_icon = reader.read_bool()
            quest.set_id = reader.read_data(Bits.QUEST_SETS) if version >= FileVersion.SETS else 0
            if version >= FileVersion.SETS and reader.read_bool():
                quest.icon = ItemStackData.read(reader, False)

            if reader.read_bool():
                quest.requirements = [reader.read_data(Bits.QUESTS) for _ in range(reader.read_data(Bits.QUESTS))]
            if version >= FileVersion.OPTION_LINKS and reader.read_bool():
                quest.option_links = [reader.read_data(Bits.QUESTS) for _ in range(reader.read_data(Bits.QUESTS))]

            quest.repeat = cls._read_repeat(reader, version)
            if version >= FileVersion.TRIGGER_QUESTS:
                quest.trigger_type = reader.read_data(Bits.TRIGGER_TYPE)
                if quest.trigger_type == 2:
                    quest.trigger_tasks = reader.read_data(Bits.TASKS)
            if version >= FileVersion.PARENT_COUNT and reader.read_bool():
                quest.parent_requirement_count = reader.read_data(Bits.QUESTS)

            quest.tasks = [cls._read_task(reader, version) for _ in range(reader.read_data(Bits.TASKS))]
            quest.rewards = cls._read_rewards(reader)
            quest.reward_choices = cls._read_rewards(reader)
            if version >= FileVersion.COMMAND_REWARDS:
                quest.command_rewards = cls._read_command_rewards(reader)
            if version >= FileVersion.REPUTATION:
                quest.reputation_rewards = [
                    (reader.read_data(Bits.REPUTATION), reader.read_data(Bits.REPUTATION_VALUE))
                    for _ in range(reader.read_data(Bits.REPUTATION_REWARD))
                ]
            quests.append(quest)
        return quests

    def _write_quests(self, writer: HQMBitWriter) -> None:
        writer.write_data(len(self.quests), Bits.QUESTS)
        for quest in self.quests:
            writer.write_bool(quest.exists)
            if not quest.exists:
                continue
            writer.write_string(quest.name or HQMString.from_raw(None), Bits.QUEST_NAME_LENGTH)
            writer.write_string(quest.description or HQMString.from_raw(None), Bits.QUEST_DESCRIPTION_LENGTH)
            writer.write_data(quest.x, Bits.QUEST_POS_X)
            writer.write_data(quest.y, Bits.QUEST_POS_Y)
            writer.write_bool(quest.big_icon)
            if writer.version >= FileVersion.SETS:
                writer.write_data(quest.set_id, Bits.QUEST_SETS)
                writer.write_bool(quest.icon is not None)
                if quest.icon is not None:
                    quest.icon.write(writer, False)

            writer.write_bool(bool(quest.requirements))
            if quest.requirements:
                writer.write_data(len(quest.requirements), Bits.QUESTS)
                for requirement in quest.requirements:
                    writer.write_data(requirement, Bits.QUESTS)

            if writer.version >= FileVersion.OPTION_LINKS:
                writer.write_bool(bool(quest.option_links))
                if quest.option_links:
                    writer.write_data(len(quest.option_links), Bits.QUESTS)
                    for option_link in quest.option_links:
                        writer.write_data(option_link, Bits.QUESTS)

            self._write_repeat(writer, quest.repeat)
            if writer.version >= FileVersion.TRIGGER_QUESTS:
                writer.write_data(quest.trigger_type, Bits.TRIGGER_TYPE)
                if quest.trigger_type == 2:
                    writer.write_data(quest.trigger_tasks or 0, Bits.TASKS)
            if writer.version >= FileVersion.PARENT_COUNT:
                writer.write_bool(quest.parent_requirement_count is not None)
                if quest.parent_requirement_count is not None:
                    writer.write_data(quest.parent_requirement_count, Bits.QUESTS)

            writer.write_data(len(quest.tasks), Bits.TASKS)
            for task in quest.tasks:
                self._write_task(writer, task)
            self._write_rewards(writer, quest.rewards)
            self._write_rewards(writer, quest.reward_choices)
            if writer.version >= FileVersion.COMMAND_REWARDS:
                self._write_command_rewards(writer, quest.command_rewards)
            if writer.version >= FileVersion.REPUTATION:
                writer.write_data(len(quest.reputation_rewards), Bits.REPUTATION_REWARD)
                for reputation_id, value in quest.reputation_rewards:
                    writer.write_data(reputation_id, Bits.REPUTATION)
                    writer.write_data(value, Bits.REPUTATION_VALUE)

    @staticmethod
    def _read_repeat(reader: HQMBitReader, version: int) -> RepeatInfoData:
        if version < FileVersion.REPEATABLE_QUESTS:
            return RepeatInfoData()
        repeat_type = reader.read_data(Bits.REPEAT_TYPE)
        hours_total = reader.read_data(Bits.HOURS) if repeat_type in (2, 3) else None
        return RepeatInfoData(repeat_type, hours_total)

    @staticmethod
    def _write_repeat(writer: HQMBitWriter, repeat: RepeatInfoData) -> None:
        if writer.version < FileVersion.REPEATABLE_QUESTS:
            return
        writer.write_data(repeat.repeat_type, Bits.REPEAT_TYPE)
        if repeat.repeat_type in (2, 3):
            writer.write_data(repeat.hours_total or 0, Bits.HOURS)

    @classmethod
    def _read_task(cls, reader: HQMBitReader, version: int) -> TaskData:
        task_type = reader.read_data(Bits.TASK_TYPE)
        name = reader.read_string(Bits.QUEST_NAME_LENGTH)
        description = reader.read_string(Bits.QUEST_DESCRIPTION_LENGTH)
        payload = cls._read_task_payload(reader, version, task_type)
        return TaskData(task_type, name, description, payload)

    @classmethod
    def _write_task(cls, writer: HQMBitWriter, task: TaskData) -> None:
        writer.write_data(task.task_type, Bits.TASK_TYPE)
        writer.write_string(task.name, Bits.QUEST_NAME_LENGTH)
        writer.write_string(task.description, Bits.QUEST_DESCRIPTION_LENGTH)
        cls._write_task_payload(writer, task.payload)

    @classmethod
    def _read_task_payload(cls, reader: HQMBitReader, version: int, task_type: int) -> TaskPayloadData:
        if task_type in (0, 1, 3, 4):
            items = []
            for _ in range(reader.read_data(Bits.TASK_ITEM_COUNT)):
                is_item = reader.read_bool()
                if is_item:
                    item = reader.read_string(Bits.SHORT)
                    damage = reader.read_data(Bits.SHORT)
                    nbt = reader.read_nbt()
                    required = reader.read_data(Bits.TASK_REQUIREMENT)
                    precision = (
                        reader.read_string(Bits.ITEM_PRECISION)
                        if version >= FileVersion.CUSTOM_PRECISION_TYPES
                        else HQMString.from_raw(str(reader.read_data(Bits.ITEM_PRECISION)).encode("ascii"))
                    )
                    items.append({"is_item": True, "item": item, "damage": damage, "nbt": nbt, "required": required, "precision": precision})
                else:
                    items.append({"is_item": False, "nbt": reader.read_nbt()})
            return TaskPayloadData(task_type, {"items": items})

        if task_type == 2:
            locations = []
            for _ in range(reader.read_data(Bits.TASK_LOCATION_COUNT)):
                icon = None
                if reader.read_bool():
                    icon = ItemStackData(reader.read_string(Bits.SHORT), None, reader.read_data(Bits.SHORT), reader.read_nbt())
                locations.append(
                    {
                        "icon": icon,
                        "name": reader.read_string(Bits.NAME_LENGTH),
                        "x": reader.read_data(Bits.WORLD_COORDINATE),
                        "y": reader.read_data(Bits.WORLD_COORDINATE),
                        "z": reader.read_data(Bits.WORLD_COORDINATE),
                        "radius": reader.read_data(Bits.WORLD_COORDINATE),
                        "visible": reader.read_data(Bits.LOCATION_VISIBILITY),
                        "dimension": reader.read_data(Bits.WORLD_COORDINATE),
                    }
                )
            return TaskPayloadData(task_type, {"locations": locations})

        if task_type == 5:
            mobs = []
            for _ in range(reader.read_data(Bits.TASK_MOB_COUNT)):
                icon = None
                if reader.read_bool():
                    icon = ItemStackData(reader.read_string(Bits.SHORT), None, reader.read_data(Bits.SHORT), reader.read_nbt())
                mobs.append(
                    {
                        "icon": icon,
                        "name": reader.read_string(Bits.NAME_LENGTH),
                        "mob": reader.read_string(Bits.MOB_ID_LENGTH),
                        "count": reader.read_data(Bits.KILL_COUNT),
                        "exact": reader.read_bool(),
                    }
                )
            return TaskPayloadData(task_type, {"mobs": mobs})

        if task_type == 6:
            return TaskPayloadData(task_type, {"deaths": reader.read_data(Bits.DEATHS)})

        if task_type in (7, 8):
            settings = []
            for _ in range(reader.read_data(Bits.REPUTATION_SETTING)):
                reputation = reader.read_data(Bits.REPUTATION)
                lower = reader.read_data(Bits.REPUTATION_MARKER) if reader.read_bool() else None
                upper = reader.read_data(Bits.REPUTATION_MARKER) if reader.read_bool() else None
                settings.append({"reputation": reputation, "lower": lower, "upper": upper, "inverted": reader.read_bool()})
            data = {"settings": settings}
            if task_type == 8:
                data["kills"] = reader.read_data(Bits.DEATHS)
            return TaskPayloadData(task_type, data)

        raise ValueError(f"Unsupported HQM task type: {task_type}")

    @classmethod
    def _write_task_payload(cls, writer: HQMBitWriter, payload: TaskPayloadData) -> None:
        task_type = payload.kind
        data = payload.data
        if task_type in (0, 1, 3, 4):
            writer.write_data(len(data["items"]), Bits.TASK_ITEM_COUNT)
            for item in data["items"]:
                writer.write_bool(item["is_item"])
                if item["is_item"]:
                    writer.write_string(item["item"], Bits.SHORT)
                    writer.write_data(item["damage"], Bits.SHORT)
                    writer.write_nbt(item["nbt"])
                    writer.write_data(item["required"], Bits.TASK_REQUIREMENT)
                    if writer.version >= FileVersion.CUSTOM_PRECISION_TYPES:
                        writer.write_string(item["precision"], Bits.ITEM_PRECISION)
                    else:
                        writer.write_data(int(item["precision"].text or "0"), Bits.ITEM_PRECISION)
                else:
                    writer.write_nbt(item["nbt"])
            return

        if task_type == 2:
            writer.write_data(len(data["locations"]), Bits.TASK_LOCATION_COUNT)
            for location in data["locations"]:
                writer.write_bool(location["icon"] is not None)
                if location["icon"] is not None:
                    writer.write_string(location["icon"].item, Bits.SHORT)
                    writer.write_data(location["icon"].damage, Bits.SHORT)
                    writer.write_nbt(location["icon"].nbt)
                writer.write_string(location["name"], Bits.NAME_LENGTH)
                writer.write_data(location["x"], Bits.WORLD_COORDINATE)
                writer.write_data(location["y"], Bits.WORLD_COORDINATE)
                writer.write_data(location["z"], Bits.WORLD_COORDINATE)
                writer.write_data(location["radius"], Bits.WORLD_COORDINATE)
                writer.write_data(location["visible"], Bits.LOCATION_VISIBILITY)
                writer.write_data(location["dimension"], Bits.WORLD_COORDINATE)
            return

        if task_type == 5:
            writer.write_data(len(data["mobs"]), Bits.TASK_MOB_COUNT)
            for mob in data["mobs"]:
                writer.write_bool(mob["icon"] is not None)
                if mob["icon"] is not None:
                    writer.write_string(mob["icon"].item, Bits.SHORT)
                    writer.write_data(mob["icon"].damage, Bits.SHORT)
                    writer.write_nbt(mob["icon"].nbt)
                writer.write_string(mob["name"], Bits.NAME_LENGTH)
                writer.write_string(mob["mob"], Bits.MOB_ID_LENGTH)
                writer.write_data(mob["count"], Bits.KILL_COUNT)
                writer.write_bool(mob["exact"])
            return

        if task_type == 6:
            writer.write_data(data["deaths"], Bits.DEATHS)
            return

        if task_type in (7, 8):
            writer.write_data(len(data["settings"]), Bits.REPUTATION_SETTING)
            for setting in data["settings"]:
                writer.write_data(setting["reputation"], Bits.REPUTATION)
                writer.write_bool(setting["lower"] is not None)
                if setting["lower"] is not None:
                    writer.write_data(setting["lower"], Bits.REPUTATION_MARKER)
                writer.write_bool(setting["upper"] is not None)
                if setting["upper"] is not None:
                    writer.write_data(setting["upper"], Bits.REPUTATION_MARKER)
                writer.write_bool(setting["inverted"])
            if task_type == 8:
                writer.write_data(data["kills"], Bits.DEATHS)
            return

        raise ValueError(f"Unsupported HQM task type: {task_type}")

    @staticmethod
    def _read_rewards(reader: HQMBitReader) -> Optional[List[ItemStackData]]:
        if not reader.read_bool():
            return None
        return [ItemStackData.read(reader, True) for _ in range(reader.read_data(Bits.REWARDS))]

    @staticmethod
    def _write_rewards(writer: HQMBitWriter, rewards: Optional[List[ItemStackData]]) -> None:
        writer.write_bool(rewards is not None)
        if rewards is None:
            return
        writer.write_data(len(rewards), Bits.REWARDS)
        for reward in rewards:
            reward.write(writer, True)

    @staticmethod
    def _read_command_rewards(reader: HQMBitReader) -> List[HQMString]:
        if not reader.read_bool():
            return []
        return [reader.read_string(Bits.QUEST_DESCRIPTION_LENGTH) for _ in range(reader.read_data(Bits.REWARDS))]

    @staticmethod
    def _write_command_rewards(writer: HQMBitWriter, commands: List[HQMString]) -> None:
        writer.write_bool(bool(commands))
        if not commands:
            return
        writer.write_data(len(commands), Bits.REWARDS)
        for command in commands:
            writer.write_string(command, Bits.QUEST_DESCRIPTION_LENGTH)

    @classmethod
    def _read_bags(cls, reader: HQMBitReader, version: int) -> Tuple[List[TierData], List[GroupData]]:
        if version < FileVersion.BAGS:
            return [], []
        tiers = []
        for _ in range(reader.read_data(Bits.TIER_COUNT)):
            tiers.append(
                TierData(
                    reader.read_string(Bits.QUEST_NAME_LENGTH),
                    reader.read_data(Bits.COLOR),
                    [reader.read_data(Bits.WEIGHT) for _ in range(BAG_TIER_COUNT)],
                )
            )

        groups = []
        for i in range(reader.read_data(Bits.GROUP_COUNT)):
            group_id = reader.read_data(Bits.GROUP_COUNT) if version >= FileVersion.BAG_LIMITS else i
            name = reader.read_string(Bits.QUEST_NAME_LENGTH)
            tier_id = reader.read_data(Bits.TIER_COUNT)
            items = [ItemStackData.read(reader, True) for _ in range(reader.read_data(Bits.GROUP_ITEMS))]
            limit = reader.read_data(Bits.LIMIT) if version >= FileVersion.BAG_LIMITS and reader.read_bool() else 0
            groups.append(GroupData(group_id, name, tier_id, items, limit))
        return tiers, groups

    def _write_bags(self, writer: HQMBitWriter) -> None:
        if writer.version < FileVersion.BAGS:
            return
        writer.write_data(len(self.tiers), Bits.TIER_COUNT)
        for tier in self.tiers:
            writer.write_string(tier.name, Bits.QUEST_NAME_LENGTH)
            writer.write_data(tier.color, Bits.COLOR)
            for weight in tier.weights:
                writer.write_data(weight, Bits.WEIGHT)

        writer.write_data(len(self.groups), Bits.GROUP_COUNT)
        for index, group in enumerate(self.groups):
            if writer.version >= FileVersion.BAG_LIMITS:
                writer.write_data(group.id, Bits.GROUP_COUNT)
            writer.write_string(group.name, Bits.QUEST_NAME_LENGTH)
            writer.write_data(group.tier_id, Bits.TIER_COUNT)
            writer.write_data(len(group.items), Bits.GROUP_ITEMS)
            for item in group.items:
                item.write(writer, True)
            if writer.version >= FileVersion.BAG_LIMITS:
                writer.write_bool(group.limit > 0)
                if group.limit > 0:
                    writer.write_data(group.limit, Bits.LIMIT)


class HQMQuestConverter:
    def read(self, quest_file) -> HQMFile:
        return HQMFile.parse(quest_file.getvalue())

    def extract(self, modpack_name: str, quest_file) -> Tuple[HQMFile, Dict[str, str]]:
        hqm_file = self.read(quest_file)
        return hqm_file, hqm_file.extract_lang_dict(modpack_name)

    def apply(self, modpack_name: str, quest_file, lang_dict: Dict[str, str]) -> Tuple[bytes, List[str], Dict[str, str]]:
        hqm_file = self.read(quest_file)
        source_lang = hqm_file.extract_lang_dict(modpack_name)
        warnings = hqm_file.apply_lang_dict(modpack_name, lang_dict)
        return hqm_file.to_bytes(), warnings, source_lang
