"""Ma Sói – engine trò chơi (16 vai) có máy quản trò đọc lời dẫn.

Không phụ thuộc FastAPI để dễ test. Toàn bộ trạng thái của một phòng nằm trong
`Room`; mọi thay đổi chạy trên một event loop asyncio duy nhất nên không cần lock.
Mỗi khi trạng thái đổi, engine gọi `broadcast(room)` để lớp WebSocket gửi cho
từng người một "góc nhìn" riêng (view_for).

Máy quản trò (narrator) là một thiết bị không chơi: nhận danh sách câu thoại
`speech` và đọc to bằng giọng tiếng Việt, kể cả bản tổng kết diễn biến cuối ván.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import secrets
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

log = logging.getLogger("masoi")

MIN_PLAYERS = 4  # để test cho tiện; chơi thật nên đặt 10
MAX_PLAYERS = 30


def rv(role) -> str:
    """Mã vai dạng chuỗi – vai có sẵn (Role) hoặc vai tự tạo ("c_…")."""
    return role.value if isinstance(role, Enum) else str(role)


class Role(str, Enum):
    # Phe Dân Làng
    VILLAGER = "villager"
    BODYGUARD = "bodyguard"
    SEER = "seer"
    WITCH = "witch"
    HUNTER = "hunter"
    DISEASED = "diseased"        # bị sói cắn chết -> đêm sau bầy sói không cắn được
    MOON_MAIDEN = "moon_maiden"  # mỗi đêm: soi vai 1 người HOẶC che chắn 1 người khỏi kỹ năng sói
    SPELLCASTER = "spellcaster"  # mỗi đêm làm 1 người bị câm trong buổi thảo luận hôm sau
    # Phe Ma Sói
    WEREWOLF = "werewolf"
    MUTANT_WOLF = "mutant_wolf"  # đêm 1 là dân, từ đêm 2 hoá sói
    WOLF_CUB = "wolf_cub"        # chết -> đêm sau bầy được cắn 2 người
    SNOW_WOLF = "snow_wolf"      # phong ấn năng lực 1 người trong đêm kế tiếp
    # Phe thứ ba & biến đổi
    CURSED = "cursed"            # bị sói cắn -> hoá sói thay vì chết
    TRAITOR = "traitor"          # trông như dân, thắng cùng sói; sói chết hết thì hoá sói
    CLONER = "cloner"            # đêm 1 chọn mục tiêu; mục tiêu chết -> nhận vai & phe
    CREATOR = "creator"          # xem 2 lá dư, chọn 1 làm vai của mình


ROLE_VI = {
    Role.VILLAGER: "Dân Làng", Role.BODYGUARD: "Bảo Vệ", Role.SEER: "Tiên Tri",
    Role.WITCH: "Phù Thủy", Role.HUNTER: "Thợ Săn", Role.DISEASED: "Người Bệnh",
    Role.MOON_MAIDEN: "Nguyệt Nữ", Role.SPELLCASTER: "Kẻ Phù Phép",
    Role.WEREWOLF: "Ma Sói", Role.MUTANT_WOLF: "Sói Biến Chủng", Role.WOLF_CUB: "Sói Con",
    Role.SNOW_WOLF: "Sói Băng", Role.CURSED: "Kẻ Bị Nguyền", Role.TRAITOR: "Kẻ Phản Bội",
    Role.CLONER: "Kẻ Nhân Bản", Role.CREATOR: "Kẻ Sáng Tạo",
}
WOLF_CARDS = {Role.WEREWOLF, Role.MUTANT_WOLF, Role.WOLF_CUB, Role.SNOW_WOLF}
SINGLE_ROLES = set(Role) - {Role.WEREWOLF, Role.VILLAGER}  # mỗi vai đặc biệt tối đa 1
CREATOR_EXTRA = 2  # số lá dư khi có Kẻ Sáng Tạo


class Phase(str, Enum):
    LOBBY = "lobby"
    NIGHTFALL = "nightfall"
    NIGHT_CREATOR = "night_creator"
    NIGHT_CLONER = "night_cloner"
    NIGHT_GUARD = "night_guard"
    NIGHT_WOLVES = "night_wolves"
    NIGHT_WITCH = "night_witch"
    NIGHT_SEER = "night_seer"
    NIGHT_MOON = "night_moon"
    NIGHT_SNOW = "night_snow"
    NIGHT_SPELL = "night_spell"
    NIGHT_CUSTOM = "night_custom"   # lượt của một vai tự tạo
    DAWN = "dawn"
    DAY_DISCUSS = "day_discuss"
    DAY_VOTE = "day_vote"
    VERDICT = "verdict"
    HUNTER_SHOT = "hunter_shot"
    ENDED = "ended"


# Bị Sói Băng phong ấn thì mất lượt ở các bước này
FREEZABLE = {Phase.NIGHT_GUARD, Phase.NIGHT_WITCH, Phase.NIGHT_SEER,
             Phase.NIGHT_MOON, Phase.NIGHT_SPELL, Phase.NIGHT_CUSTOM}

# ---------- vai tự tạo: ghép từ các "khối năng lực" mà engine hiểu được ----------
CUSTOM_ACTIONS = {
    "none": ("", ""),
    "protect": ("Bảo vệ", "chọn người cần bảo vệ đêm nay"),
    "check_team": ("Soi", "chọn người muốn soi phe"),
    "check_role": ("Soi vai", "chọn người muốn xem vai"),
    "kill": ("Ra tay", "chọn người muốn giết"),
}
CUSTOM_MAX = 12


def clean_custom_role(rid: str, d: dict) -> dict:
    """Làm sạch định nghĩa vai tự tạo nhận từ chủ phòng."""
    if not re.fullmatch(r"c_[a-z0-9]{3,12}", str(rid)):
        raise GameError("Mã vai không hợp lệ")
    name = " ".join(str(d.get("name") or "").split())[:20]
    if not name:
        raise GameError("Vai tự tạo cần có tên")
    action = d.get("action") if d.get("action") in CUSTOM_ACTIONS else "none"
    team = "wolves" if d.get("team") == "wolves" else "village"
    out = {
        "name": name,
        "icon": "".join(list(str(d.get("icon") or "⭐"))[:3]) or "⭐",
        "desc": " ".join(str(d.get("desc") or "").split())[:160],
        "team": team,
        "action": action,
        "uses": max(0, min(3, int(d.get("uses") or 0))),        # 0 = mỗi đêm
        "start_night": max(1, min(3, int(d.get("start_night") or 1))),
        "no_repeat": bool(d.get("no_repeat")),
        "tough": bool(d.get("tough")),        # chịu được 1 lần sói cắn
        "revenge": bool(d.get("revenge")),    # chết trong đêm được kéo theo 1 người
        "seen_as": d.get("seen_as") if d.get("seen_as") in ("wolf", "village") else "",
        "call": " ".join(str(d.get("call") or "").split())[:200],
    }
    if not out["call"] and action != "none":
        out["call"] = f"{name} hãy thức dậy và {CUSTOM_ACTIONS[action][1]}."
    return out

# Câu gọi vai của quản trò
CALLS = {
    Phase.NIGHT_CREATOR: (Role.CREATOR, "Kẻ Sáng Tạo hãy thức dậy. Hai lá bài dư đang hiện trên điện thoại của bạn, hãy chọn một lá làm vai của mình."),
    Phase.NIGHT_CLONER: (Role.CLONER, "Kẻ Nhân Bản hãy thức dậy và chọn người bạn muốn nhân bản."),
    Phase.NIGHT_GUARD: (Role.BODYGUARD, "Bảo Vệ hãy thức dậy và chọn người cần bảo vệ đêm nay."),
    Phase.NIGHT_WOLVES: (Role.WEREWOLF, "Ma Sói hãy thức dậy, nhìn nhau và chọn người để cắn."),
    Phase.NIGHT_WITCH: (Role.WITCH, "Phù Thủy hãy thức dậy. Bạn có muốn dùng bình cứu hay bình độc không?"),
    Phase.NIGHT_SEER: (Role.SEER, "Tiên Tri hãy thức dậy và chọn người bạn muốn soi."),
    Phase.NIGHT_MOON: (Role.MOON_MAIDEN, "Nguyệt Nữ hãy thức dậy. Hãy soi một người, hoặc che chắn một người."),
    Phase.NIGHT_SNOW: (Role.SNOW_WOLF, "Sói Băng hãy thức dậy và chọn người để phong ấn."),
    Phase.NIGHT_SPELL: (Role.SPELLCASTER, "Kẻ Phù Phép hãy thức dậy và chọn người sẽ bị câm vào ngày mai."),
}


# Lời quản trò mặc định. Chủ phòng sửa được từng câu; {…} là chỗ máy điền tên, số…
SCRIPT_DEFAULTS: dict[str, str] = {
    "nightfall": "Đêm thứ {dem}. Màn đêm buông xuống, tất cả mọi người hãy nhắm mắt lại.",
    **{f"call_{role.value}": text for _, (role, text) in CALLS.items()},
    "sleep": "{vai} hãy nhắm mắt lại và đi ngủ.",
    "dawn_deaths": "Trời đã sáng, mọi người hãy mở mắt. Đêm qua, {nguoi_chet} đã chết.",
    "dawn_peace": "Trời đã sáng, mọi người hãy mở mắt. Đêm qua là một đêm bình yên, không ai chết.",
    "reveal": "{ten} là {vai}.",
    "silenced": "{ten} đã bị phù phép, hôm nay không được nói.",
    "hunter_call": "{ten} là Thợ Săn. Thợ Săn hãy chọn một người để kéo theo.",
    "hunter_shot": "Thợ Săn đã bắn {ten}.",
    "hunter_skip": "Thợ Săn đã hạ súng, không bắn ai.",
    "discuss": "Bây giờ là thời gian thảo luận. Mọi người có {thoi_gian}. Ai muốn bỏ phiếu ngay thì bấm nút Vote ngay.",
    "ready": "Đa số đã sẵn sàng.",
    "host_skip": "Quản trò cho bỏ phiếu ngay.",
    "vote": "Bắt đầu bỏ phiếu. Mọi người hãy chọn người muốn treo cổ trên điện thoại.",
    "lynch": "Với {so_phieu} phiếu, {ten} đã bị treo cổ.",
    "no_lynch": "Số phiếu không đủ. Hôm nay không ai bị treo cổ.",
    "end": "Ván chơi kết thúc. {phe_thang} chiến thắng! Sau đây là diễn biến của ván chơi.",
    "thanks": "Cảm ơn mọi người đã chơi.",
    "vote_detail": "{ten} có {so_phieu} phiếu từ {nguoi_bo_phieu}.",
    "cancel": "Quản trò đã huỷ ván chơi. Mọi người quay lại phòng chờ.",
    "ranking": "Bảng xếp hạng sau {so_van} ván: {xep_hang}.",
}
SCRIPT_MAX = 300
BOT_NAMES = ["Na", "Bi", "Tôm", "Mít", "Su", "Bống", "Cốm", "Kem", "Xoài", "Đậu", "Gạo", "Mận", "Bơ", "Nho",
             "Sóc", "Thỏ", "Cam", "Táo", "Lê", "Ổi", "Dừa", "Chanh", "Bưởi", "Sung", "Mơ", "Hồng", "Nấm", "Tiêu",
             "Gừng", "Quế"]
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def suggest_roles(n: int) -> dict[str, int]:
    """Chia vai mặc định: ~1/4 là sói; càng đông càng thêm vai đặc biệt."""
    r = {role: 0 for role in Role}
    wolves = max(1, n // 4)
    r[Role.WOLF_CUB] = int(n >= 12)
    r[Role.SNOW_WOLF] = int(n >= 16)
    r[Role.MUTANT_WOLF] = int(n >= 20)
    r[Role.WEREWOLF] = wolves - r[Role.WOLF_CUB] - r[Role.SNOW_WOLF] - r[Role.MUTANT_WOLF]
    r[Role.SEER] = r[Role.BODYGUARD] = r[Role.WITCH] = 1
    r[Role.HUNTER] = int(n >= 8)
    r[Role.SPELLCASTER] = int(n >= 12)
    r[Role.DISEASED] = r[Role.CURSED] = int(n >= 14)
    r[Role.MOON_MAIDEN] = int(n >= 16)
    r[Role.TRAITOR] = int(n >= 18)
    r[Role.CLONER] = int(n >= 20)
    r[Role.CREATOR] = int(n >= 22)
    out = {role.value: c for role, c in r.items()}
    _fill_villagers(out, n)
    return out


def _fill_villagers(r: dict[str, int], n: int) -> None:
    extra = CREATOR_EXTRA if r.get(Role.CREATOR.value) else 0
    others = sum(c for k, c in r.items() if k != Role.VILLAGER.value)
    r[Role.VILLAGER.value] = max(0, n + extra - others)


@dataclass
class Player:
    id: str
    name: str
    token: str
    is_host: bool = False
    narrator: bool = False     # máy quản trò: không nhận vai, không chơi
    bot: bool = False          # máy chơi do server điều khiển
    role: Optional[Role] = None
    alive: bool = True
    connected: bool = False
    private_log: list[str] = field(default_factory=list)
    death: Optional[str] = None  # "night" (chết trong đêm) hoặc "day" (bị treo cổ)


@dataclass
class Settings:
    roles: dict[str, int] = field(default_factory=dict)
    creator_seconds: int = 30
    wolves_seconds: int = 20
    role_seconds: int = 5        # mọi vai còn lại
    hunter_seconds: int = 15
    discuss_seconds: int = 180
    vote_seconds: int = 60
    reveal_role_on_death: bool = True
    fast_turns: bool = True
    host_voice: bool = False     # chủ phòng (đang chơi) cũng đọc lời quản trò trên máy mình
    show_votes: bool = False     # công khai ai bỏ phiếu cho ai (mặc định: phiếu kín, chỉ thấy số phiếu)
    scripts: dict[str, str] = field(default_factory=dict)  # lời quản trò đã sửa
    custom_roles: dict[str, dict] = field(default_factory=dict)  # vai tự tạo: mã -> định nghĩa      # chọn xong là kết thúc lượt, không chờ hết giờ


class GameError(Exception):
    """Lỗi hợp lệ hoá – thông điệp được gửi thẳng cho người chơi."""


Broadcast = Callable[["Room"], Awaitable[None]]


class Room:
    def __init__(self, code: str, broadcast: Broadcast):
        self.code = code
        self._broadcast = broadcast
        self.players: dict[str, Player] = {}
        self.settings = Settings()
        self.roles_custom = False
        self.created_at = time.time()
        # Thời gian để quản trò đọc câu gọi trước khi đếm giờ của vai (test có thể chỉnh nhỏ)
        self.call_seconds = 5.0
        self.announce_seconds = 6.0
        self.jitter = (1.0, 3.0)  # giây ngẫu nhiên thêm vào mỗi lượt để khó đoán
        self._speech_id = 0
        # Bảng xếp hạng của phòng – giữ qua các ván, theo tên người chơi
        self.scores: dict[str, dict] = {}
        self.games_played = 0
        self.task: asyncio.Task | None = None
        self._reset_game()

    # ------------------------------------------------------------ phòng chờ
    def _reset_game(self) -> None:
        self.phase = Phase.LOBBY
        self.day_no = 0
        self.deadline: float | None = None
        self.log: list[dict] = []
        self.speech: list[dict] = []
        self.history: list[dict] = []
        self._period = "Chia bài"
        self.winner: str | None = None
        self.actors: set[str] = set()
        self._step_done = asyncio.Event()
        self.last_guarded: str | None = None
        self.witch_heal_left = True
        self.witch_poison_left = True
        self.day_votes: dict[str, str] = {}
        self.hunter_shooter: str | None = None
        self.hunter_target: str | None = None
        self.creator_options: list[Role] = []
        self.creator_choice: Role | None = None
        self.clone_target: str | None = None
        self.custom_used: dict[str, int] = {}      # số lần đã dùng năng lực (vai tự tạo)
        self.custom_last: dict[str, str] = {}      # mục tiêu đêm trước (luật không chọn trùng)
        self.tough_used: set[str] = set()
        self.sick_next = False      # Người Bệnh vừa bị cắn chết
        self.cub_bonus = False      # Sói Con vừa chết
        self.frozen_next: str | None = None
        self.silenced: str | None = None
        self.ready_vote: set[str] = set()
        self.next_ready: set[str] = set()
        self.last_result: list[dict] = []
        self._day_log_start = 0  # ai đã bấm "Sẵn sàng chơi tiếp" khi hết ván
        self._step_id = 0
        self.bot_mem: dict[str, dict] = {}      # trí nhớ riêng của từng máy chơi
        self.public_claims: dict[str, bool] = {}  # Tiên Tri máy đã công khai: id -> là sói?
        self._reset_night()

    def _reset_night(self) -> None:
        self.moon_result: str | None = None
        self.moon_shield: str | None = None
        self.frozen: str | None = None
        self.guard_target: str | None = None
        self.bites = 1
        self.wolf_votes: dict[str, list[str]] = {}
        self.wolf_targets: list[str] = []
        self.seer_result: str | None = None
        self.witch_heal: str | None = None
        self.witch_poison: str | None = None
        self.spell_target: str | None = None
        self.custom_key: str | None = None          # vai tự tạo đang được gọi (reset mỗi đêm)
        self.custom_protect: dict[str, str] = {}
        self.custom_kills: dict[str, str] = {}
        self.custom_results: dict[str, str] = {}

    def seated(self) -> list[Player]:
        """Người chơi thật (không tính máy quản trò)."""
        return [p for p in self.players.values() if not p.narrator]

    @property
    def narrator(self) -> Player | None:
        return next((p for p in self.players.values() if p.narrator), None)

    def add_player(self, name: str, is_host: bool = False, narrator: bool = False) -> Player:
        if self.phase != Phase.LOBBY:
            raise GameError("Ván đã bắt đầu, không thể vào thêm")
        if not narrator and len(self.seated()) >= MAX_PLAYERS:
            raise GameError(f"Phòng đã đủ {MAX_PLAYERS} người")
        name = " ".join(name.split())[:20] or ("Quản trò" if narrator else "")
        if not name:
            raise GameError("Hãy nhập tên")
        if any(p.name.lower() == name.lower() for p in self.players.values()):
            raise GameError("Tên này đã có người dùng trong phòng")
        p = Player(id=secrets.token_hex(4), name=name, token=secrets.token_urlsafe(16),
                   is_host=is_host, narrator=narrator)
        self.players[p.id] = p
        self._auto_roles()
        return p

    def remove_player(self, pid: str) -> None:
        if self.phase not in (Phase.LOBBY, Phase.ENDED):
            raise GameError("Không thể rời khi ván đang diễn ra")
        p = self.players.pop(pid, None)
        if p and p.is_host:
            heir = next((x for x in self.players.values() if not x.bot), None)
            if heir:
                heir.is_host = True
        self._auto_roles()

    # ------------------------------------------------------------ máy chơi
    def humans(self) -> list[Player]:
        return [p for p in self.seated() if not p.bot]

    def add_bot(self) -> Player:
        if self.phase != Phase.LOBBY:
            raise GameError("Chỉ thêm máy chơi được ở phòng chờ")
        if len(self.seated()) >= MAX_PLAYERS:
            raise GameError(f"Phòng đã đủ {MAX_PLAYERS} người")
        used = {p.name for p in self.players.values()}
        name = next((f"🤖 {n}" for n in BOT_NAMES if f"🤖 {n}" not in used),
                    f"🤖 Máy {len(self.players) + 1}")
        p = Player(id=secrets.token_hex(4), name=name, token=secrets.token_urlsafe(16),
                   bot=True, connected=True)
        self.players[p.id] = p
        self._auto_roles()
        return p

    def remove_bot(self) -> None:
        bots = [p for p in self.seated() if p.bot]
        if not bots:
            raise GameError("Không còn máy chơi nào")
        self.remove_player(bots[-1].id)

    def fill_bots(self, total: int) -> None:
        total = max(0, min(MAX_PLAYERS, int(total)))
        while len(self.seated()) < total:
            self.add_bot()
        while len(self.seated()) > total and any(p.bot for p in self.seated()):
            self.remove_bot()

    def _auto_roles(self) -> None:
        """Tự động: chia theo số người. Tuỳ chỉnh: giữ vai Host chọn, Dân Làng tự lấp chỗ trống."""
        n = len(self.seated())
        if not self.roles_custom:
            self.settings.roles = suggest_roles(n)
        else:
            _fill_villagers(self.settings.roles, n)

    def update_scripts(self, data) -> None:
        """Sửa lời quản trò – được phép cả khi đang chơi. Câu để trống = dùng lại mặc định."""
        if not isinstance(data, dict):
            raise GameError("Lời quản trò không hợp lệ")
        for key, text in data.items():
            if key not in SCRIPT_DEFAULTS:
                continue
            text = " ".join(str(text or "").split())[:SCRIPT_MAX]
            if text and text != SCRIPT_DEFAULTS[key]:
                self.settings.scripts[key] = text
            else:
                self.settings.scripts.pop(key, None)

    def update_settings(self, data: dict) -> None:
        if "scripts" in data:
            self.update_scripts(data["scripts"])
        if "show_votes" in data:  # bật/tắt được cả khi đang chơi
            self.settings.show_votes = bool(data["show_votes"])
        data = {k: v for k, v in data.items() if k not in ("scripts", "show_votes")}
        if not data:
            return
        if self.phase != Phase.LOBBY:
            raise GameError("Chỉ đổi cấu hình được ở phòng chờ")
        try:
            if isinstance(data.get("custom_roles"), dict):  # thêm / sửa / xoá (giá trị null) vai tự tạo
                lib = dict(self.settings.custom_roles)
                for rid, d in data["custom_roles"].items():
                    if d is None:
                        lib.pop(rid, None)
                        self.settings.roles.pop(rid, None)
                    else:
                        lib[rid] = clean_custom_role(rid, d)
                if len(lib) > CUSTOM_MAX:
                    raise GameError(f"Tối đa {CUSTOM_MAX} vai tự tạo")
                self.settings.custom_roles = lib
            if data.get("auto_roles"):
                self.roles_custom = False
            elif isinstance(data.get("roles"), dict):
                roles = {
                    r.value: max(0, min(1 if r in SINGLE_ROLES else MAX_PLAYERS,
                                        int(data["roles"].get(r.value, 0))))
                    for r in Role
                }
                for rid in self.settings.custom_roles:
                    roles[rid] = max(0, min(5, int(data["roles"].get(rid, 0))))
                self.settings.roles = roles
                self.roles_custom = True
            self._auto_roles()
            limits = {"creator_seconds": (10, 90), "wolves_seconds": (10, 90),
                      "role_seconds": (5, 60), "hunter_seconds": (5, 60),
                      "discuss_seconds": (30, 900), "vote_seconds": (15, 300)}
            for key, (lo, hi) in limits.items():
                if key in data:
                    setattr(self.settings, key, max(lo, min(hi, int(data[key]))))
            for key in ("reveal_role_on_death", "fast_turns", "host_voice"):
                if key in data:
                    setattr(self.settings, key, bool(data[key]))
        except (TypeError, ValueError):
            raise GameError("Cấu hình không hợp lệ")

    def validate_roles(self) -> None:
        n, r = len(self.seated()), self.settings.roles
        if n < MIN_PLAYERS:
            raise GameError(f"Cần ít nhất {MIN_PLAYERS} người chơi")
        need = n + (CREATOR_EXTRA if r.get(Role.CREATOR.value) else 0)
        total = sum(r.values())
        if total != need:
            raise GameError(f"Đang có {total} lá cho {need} chỗ – hãy bớt vai đặc biệt")
        wolves = sum(r.get(w.value, 0) for w in WOLF_CARDS) + sum(
            r.get(k, 0) for k, d in self.settings.custom_roles.items() if d["team"] == "wolves")
        if wolves < 1:
            raise GameError("Cần ít nhất 1 lá phe Sói")
        if wolves >= n - wolves:
            raise GameError("Sói quá đông – phải ít hơn phe Dân")

    async def start(self) -> None:
        self.validate_roles()
        players = self.seated()
        cards = [self._as_role(k) for k, c in self.settings.roles.items() for _ in range(c)]
        has_creator = Role.CREATOR in cards
        if has_creator:
            cards.remove(Role.CREATOR)
        for _ in range(200):  # đảm bảo sau khi rút 2 lá dư vẫn còn sói trong ván
            random.shuffle(cards)
            extra = cards[:CREATOR_EXTRA] if has_creator else []
            deal = cards[len(extra):]
            if any(c in WOLF_CARDS for c in deal):
                break
        random.shuffle(players)
        self._reset_game()
        self.creator_options = extra
        if has_creator:
            deal = [Role.CREATOR] + deal
        for p, role in zip(players, deal):
            p.role, p.alive, p.private_log, p.death = role, True, [], None
        self._hist("Bài đã chia: " + ", ".join(f"{p.name} là {self._rn(p.role)}" for p in players)
                   + (f". Hai lá dư là {self._rn(extra[0])} và {self._rn(extra[1])}" if extra else "") + ".")
        self.task = asyncio.create_task(self._run())

    def reset_to_lobby(self) -> None:
        if self.phase != Phase.ENDED:
            raise GameError("Ván chưa kết thúc")
        self._reset_game()
        for p in self.players.values():
            p.role, p.alive, p.private_log = None, True, []
        self._auto_roles()

    def toggle_next_ready(self, pid: str) -> None:
        p = self.players.get(pid)
        if self.phase not in (Phase.LOBBY, Phase.ENDED) or not p or p.narrator or p.bot:
            raise GameError("Chỉ bấm được ở phòng chờ hoặc khi ván đã kết thúc")
        self.next_ready ^= {pid}

    def cancel_game(self) -> None:
        """Quản trò huỷ ván đang chơi: mọi người về phòng chờ, giữ nguyên người chơi và cấu hình."""
        if self.phase == Phase.LOBBY:
            raise GameError("Chưa có ván nào đang chơi")
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        self._reset_game()
        for p in self.players.values():
            p.role, p.alive, p.private_log = None, True, []
        self._auto_roles()
        self._event("⛔ Quản trò đã huỷ ván.")
        self._say(self._t("cancel"))

    async def new_game(self) -> None:
        """Chơi tiếp ngay với cùng người chơi và cấu hình – mọi điện thoại tự vào ván mới."""
        if self.phase != Phase.ENDED:
            raise GameError("Ván chưa kết thúc")
        self.reset_to_lobby()
        await self.start()  # nếu cấu hình vai không còn hợp lệ: ở lại phòng chờ và báo lỗi

    # ------------------------------------------------------------ tiện ích
    def is_wolf(self, p: Player) -> bool:
        """Sói Biến Chủng chỉ thành sói từ đêm thứ 2. Vai tự tạo phe Sói thức dậy cùng bầy."""
        d = self._cdef(p.role)
        if d:
            return d["team"] == "wolves"
        if p.role == Role.MUTANT_WOLF:
            return self.day_no >= 2
        return p.role in WOLF_CARDS

    def alive(self, role: Role | None = None) -> list[Player]:
        return [p for p in self.seated()
                if p.alive and (role is None or p.role == role)]

    def wolves(self) -> list[Player]:
        return [p for p in self.alive() if self.is_wolf(p)]

    @staticmethod
    def _names_list(names: list[str]) -> str:
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " và " + names[-1]

    def _names(self, ids) -> str:
        names = [self._name(i) for i in ids]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " và " + names[-1]

    def _pack_names(self, exclude: str) -> str:
        return ", ".join(w.name for w in self.wolves() if w.id != exclude) or "không còn ai"

    def _holders(self, role: Role) -> str:
        return ", ".join(p.name for p in self.seated() if p.role == role)

    def _in_game(self, role: Role) -> bool:
        return self.settings.roles.get(role.value, 0) > 0

    def _cdef(self, role) -> dict | None:
        return self.settings.custom_roles.get(rv(role)) if role is not None else None

    def _rn(self, role) -> str:
        """Tên tiếng Việt của vai (có sẵn hoặc tự tạo)."""
        if role in ROLE_VI:
            return ROLE_VI[role]
        d = self._cdef(role)
        return d["name"] if d else str(role)

    def _as_role(self, key: str):
        try:
            return Role(key)
        except ValueError:
            if key in self.settings.custom_roles:
                return key
            raise GameError("Vai không tồn tại")

    def _looks_wolf(self, p: Player) -> bool:
        """Kết quả khi bị soi phe (vai tự tạo có thể 'giả dạng')."""
        d = self._cdef(p.role)
        if d and d["seen_as"]:
            return d["seen_as"] == "wolf"
        return self.is_wolf(p)

    def _name(self, pid: str | None) -> str:
        return self.players[pid].name if pid in self.players else "?"

    def _event(self, text: str) -> None:
        self.log.append({"t": time.time(), "text": text})

    def _t(self, key: str, **values) -> str:
        """Lấy câu thoại (bản đã sửa hoặc mặc định) và điền tên, số vào chỗ {…}."""
        text = self.settings.scripts.get(key) or SCRIPT_DEFAULTS[key]
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), text)

    def _say(self, text: str) -> None:
        """Câu để máy quản trò đọc to."""
        self._speech_id += 1
        self.speech.append({"id": self._speech_id, "text": text})

    def _hist(self, text: str) -> None:
        """Diễn biến bí mật – chỉ công bố khi ván kết thúc."""
        self.history.append({"period": self._period, "text": text})

    def _note(self, pid: str, text: str) -> None:
        self.players[pid].private_log.append(text)

    def _join_pack(self, p: Player, reason: str) -> None:
        self._note(p.id, f"{reason} Đồng đội sói: {self._pack_names(p.id)}")
        for w in self.wolves():
            if w.id != p.id:
                self._note(w.id, f"🐺 {p.name} vừa gia nhập bầy sói.")

    def _kill(self, pid: str, cause: str) -> list[tuple[str, str]]:
        p = self.players[pid]
        if not p.alive:
            return []
        p.alive = False
        p.death = "day" if cause == "lynch" else "night"
        if self.settings.reveal_role_on_death:
            self._event(f"🪦 {p.name} là {self._rn(p.role)}")
        if p.role == Role.WOLF_CUB:
            self.cub_bonus = True
        for c in self.alive(Role.CLONER):  # Kẻ Nhân Bản nhận lá bài của mục tiêu
            if self.clone_target == pid:
                c.role = p.role
                self._note(c.id, f"🧬 {p.name} đã chết. Bạn nhận lá {self._rn(p.role)}.")
                self._hist(f"Kẻ Nhân Bản {c.name} nhận lá {self._rn(p.role)} của {p.name}.")
                if self.is_wolf(c):
                    self._join_pack(c, "🐺 Bạn đã thuộc phe Sói.")
        return [(pid, cause)]

    def _need_alive(self, target) -> None:
        if (not isinstance(target, str) or target not in self.players
                or self.players[target].narrator or not self.players[target].alive):
            raise GameError("Mục tiêu không hợp lệ")

    def _check_win(self) -> bool:
        if self.winner:
            return True
        alive = self.alive()
        wolves = [p for p in alive if self.is_wolf(p)]
        if not wolves:
            traitor = next((p for p in alive if p.role == Role.TRAITOR), None)
            if traitor:  # Kẻ Phản Bội trở thành con sói cuối cùng
                traitor.role = Role.WEREWOLF
                self._note(traitor.id, "🗡️ Toàn bộ sói đã chết. Bạn lộ nanh vuốt – giờ bạn là Ma Sói duy nhất!")
                self._hist(f"Sói đã chết hết, Kẻ Phản Bội {traitor.name} hoá thành Ma Sói.")
                wolves = [traitor]
            elif any(p.role == Role.MUTANT_WOLF for p in alive):
                return False  # Sói Biến Chủng sắp hoá sói
            else:
                self.winner = "village"
                return True
        if len(wolves) >= len(alive) - len(wolves):
            self.winner = "wolves"
        return self.winner is not None

    # ------------------------------------------------------------ vòng lặp ván
    async def _run(self) -> None:
        try:
            while True:
                self.day_no += 1
                await self._night()
                if await self._dawn():
                    return
                if await self._day():
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # không để lỗi làm treo phòng mà không ai biết
            log.exception("Game loop crashed in room %s", self.code)
            self._event("⚠️ Lỗi máy chủ – ván chơi đã dừng.")
            self._say("Xin lỗi, máy chủ gặp lỗi. Ván chơi đã dừng.")
            self.phase, self.deadline = Phase.ENDED, None
            await self._broadcast(self)

    def _intros(self) -> None:
        tips = {
            Role.MUTANT_WOLF: "🌀 Đêm đầu bạn là Dân Làng. Từ đêm thứ 2 bạn hoá sói và thức dậy cùng bầy.",
            Role.TRAITOR: "🗡️ Bạn trông như Dân Làng nhưng thắng cùng phe Sói. Khi sói chết hết, bạn hoá sói.",
            Role.CLONER: "🧬 Đêm đầu hãy chọn một người. Khi họ chết, bạn nhận lá bài và phe của họ.",
            Role.CURSED: "🌗 Nếu bị sói cắn, bạn không chết mà hoá thành sói.",
            Role.WOLF_CUB: "🐾 Nếu bạn chết, đêm sau bầy được cắn 2 người.",
            Role.DISEASED: "🤒 Nếu sói cắn chết bạn, đêm sau bầy sói không cắn được ai.",
            Role.HUNTER: "🏹 Nếu chết trong đêm, bạn được kéo một người chết theo.",
        }
        for p in self.seated():
            p.private_log.insert(0, f"Vai của bạn: {self._rn(p.role)}")
            if p.role in tips:
                self._note(p.id, tips[p.role])
            cd = self._cdef(p.role)
            if cd and cd["desc"]:
                self._note(p.id, f"{cd['icon']} {cd['desc']}")
            if self.is_wolf(p):
                self._note(p.id, f"🐺 Đồng đội sói: {self._pack_names(p.id)}")

    async def _step(self, phase: Phase, seconds: float, actors: set[str],
                    early: bool = False, wait: float | None = None,
                    min_seconds: float = 0) -> None:
        """Mở một bước.

        seconds: thời gian hiển thị trên đồng hồ. early: kết thúc khi mọi người làm xong.
        wait: thời gian chờ thật (để lượt của vai đã chết trông giống lượt bình thường).
        min_seconds: chờ ít nhất chừng này (để quản trò kịp đọc xong câu gọi).
        """
        self.phase = phase
        self.actors = set(actors)
        self._step_done = asyncio.Event()
        self._step_id += 1
        start = time.time()
        self.deadline = start + seconds
        await self._broadcast(self)
        self._wake_bots(seconds)
        try:
            if early:
                await asyncio.wait_for(self._step_done.wait(), timeout=seconds)
            else:
                await asyncio.sleep(seconds if wait is None else wait)
        except asyncio.TimeoutError:
            pass
        rest = start + min_seconds - time.time()
        if rest > 0:
            await asyncio.sleep(rest)
        self.actors = set()

    async def _call(self, phase: Phase, seconds: float, actors: set[str]) -> None:
        """Quản trò gọi một vai dậy, chờ, rồi cho vai đó đi ngủ."""
        role, _ = CALLS[phase]
        self._say(self._t(f"call_{role.value}"))
        lead, full = self.call_seconds, self.call_seconds + seconds
        if not self.settings.fast_turns:
            await self._step(phase, full, actors)  # luôn chờ đủ giờ
        elif actors:
            # chọn xong là đi tiếp, cộng một chút ngẫu nhiên cho khó đoán
            await self._step(phase, full, actors, early=True,
                             min_seconds=lead + random.uniform(*self.jitter))
        else:
            # vai đã chết / lá dư: vẫn gọi, chờ một khoảng như người thật đang chọn
            await self._step(phase, full, set(),
                             wait=lead + random.uniform(min(self.jitter[1], seconds), seconds))
        self._say(self._t("sleep", vai=self._rn(role)))

    async def _call_custom(self, key: str, d: dict, seconds: float, actors: set[str]) -> None:
        """Gọi một vai tự tạo – cùng nhịp với vai có sẵn (gọi dậy, chờ, đi ngủ)."""
        self._say(self._t(f"call_{key}") if f"call_{key}" in self.settings.scripts else d["call"])
        lead, full = self.call_seconds, self.call_seconds + seconds
        if not self.settings.fast_turns:
            await self._step(Phase.NIGHT_CUSTOM, full, actors)
        elif actors:
            await self._step(Phase.NIGHT_CUSTOM, full, actors, early=True,
                             min_seconds=lead + random.uniform(*self.jitter))
        else:
            await self._step(Phase.NIGHT_CUSTOM, full, set(),
                             wait=lead + random.uniform(min(self.jitter[1], seconds), seconds))
        self._say(self._t("sleep", vai=d["name"]))

    def _finish_step(self) -> None:
        self.actors = set()
        self._step_done.set()

    def _done(self, pid: str) -> None:
        self.actors.discard(pid)
        if not self.actors:
            self._step_done.set()

    async def _announce(self, phase: Phase, seconds: float | None = None) -> None:
        self.phase, self.deadline, self.actors = phase, None, set()
        await self._broadcast(self)
        await asyncio.sleep(self.announce_seconds if seconds is None else seconds)

    async def _night(self) -> None:
        self._reset_night()
        self._period = f"Đêm {self.day_no}"
        if self.day_no == 2:
            for m in self.alive(Role.MUTANT_WOLF):
                self._join_pack(m, "🌀 Bạn đã hoá thành sói!")
                self._hist(f"Sói Biến Chủng {m.name} hoá sói.")
        self.frozen, self.frozen_next = self.frozen_next, None
        if self.frozen and not self.players[self.frozen].alive:
            self.frozen = None
        if self.frozen:
            self._note(self.frozen, f"❄️ Đêm {self.day_no}: bạn bị Sói Băng phong ấn, không dùng được năng lực.")
            self._hist(f"{self._name(self.frozen)} bị phong ấn suốt đêm nay.")

        self._event(f"🌙 Đêm {self.day_no}")
        self._say(self._t("nightfall", dem=self.day_no))
        await self._announce(Phase.NIGHTFALL)

        st = self.settings
        ids = lambda role: {p.id for p in self.alive(role)}
        # Gọi MỌI vai có trong cấu hình, kể cả đã chết hay là lá dư – luôn chờ đủ giờ.
        if self.day_no == 1:
            if self._in_game(Role.CREATOR):
                creator = next((p for p in self.seated() if p.role == Role.CREATOR), None)
                await self._call(Phase.NIGHT_CREATOR, st.creator_seconds,
                                 {creator.id} if creator else set())
                if creator:
                    choice = self.creator_choice or random.choice(self.creator_options)
                    other = [r for r in self.creator_options if r != choice] or [choice]
                    creator.role = choice
                    self._note(creator.id, f"✨ Bạn là Kẻ Sáng Tạo và đã chọn vai {self._rn(choice)}.")
                    self._hist(f"Kẻ Sáng Tạo {creator.name} chọn lá {self._rn(choice)}, bỏ lá {self._rn(other[0])}"
                               + ("" if self.creator_choice else " (hết giờ, máy chọn ngẫu nhiên)") + ".")
            self._intros()
            if self._in_game(Role.CLONER):
                await self._call(Phase.NIGHT_CLONER, st.role_seconds, ids(Role.CLONER))
        if self._in_game(Role.BODYGUARD):
            await self._call(Phase.NIGHT_GUARD, st.role_seconds, ids(Role.BODYGUARD))

        self.bites = 0 if self.sick_next else (2 if self.cub_bonus else 1)
        self.sick_next = False
        if self.bites:
            self.cub_bonus = False
        pack = self.wolves()
        await self._call(Phase.NIGHT_WOLVES, st.wolves_seconds, {w.id for w in pack})
        votes = Counter(t for ts in self.wolf_votes.values() for t in ts)
        for _ in range(self.bites):  # theo số đông, hoà thì bốc thăm
            if not votes:
                break
            top = max(votes.values())
            pick = random.choice([t for t, c in votes.items() if c == top])
            self.wolf_targets.append(pick)
            del votes[pick]
        who = ", ".join(w.name for w in pack) or "không còn con nào"
        if self.bites == 0:
            self._hist(f"Bầy sói ({who}) nhiễm bệnh nên không cắn được ai.")
        elif self.wolf_targets:
            self._hist(f"Bầy sói ({who}) chọn cắn {self._names(self.wolf_targets)}.")
        else:
            self._hist(f"Bầy sói ({who}) không chọn ai.")

        if self._in_game(Role.WITCH):
            await self._call(Phase.NIGHT_WITCH, st.role_seconds, ids(Role.WITCH))
        if self._in_game(Role.SEER):
            await self._call(Phase.NIGHT_SEER, st.role_seconds, ids(Role.SEER))
        for phase, role in ((Phase.NIGHT_MOON, Role.MOON_MAIDEN), (Phase.NIGHT_SNOW, Role.SNOW_WOLF),
                            (Phase.NIGHT_SPELL, Role.SPELLCASTER)):
            if self._in_game(role):
                await self._call(phase, st.role_seconds, ids(role))
        for key, d in self.settings.custom_roles.items():
            if d["action"] == "none" or self.settings.roles.get(key, 0) <= 0 or self.day_no < d["start_night"]:
                continue
            holders = {p.id for p in self.alive()
                       if p.role == key and (not d["uses"] or self.custom_used.get(p.id, 0) < d["uses"])}
            self.custom_key = key
            await self._call_custom(key, d, st.role_seconds, holders)
        self.custom_key = None

    async def _dawn(self) -> bool:
        protected = {x: by for x, by in ((self.guard_target, "Bảo Vệ"), (self.moon_shield, "Nguyệt Nữ")) if x}
        for t, by in self.custom_protect.items():
            protected.setdefault(t, by)
        deaths: dict[str, str] = {}
        for t in self.wolf_targets:
            if t in protected:
                self._hist(f"{self._name(t)} được {protected[t]} che chở nên thoát chết.")
                if self.witch_heal == t:  # cứu người đã được bảo vệ -> hoàn bình
                    self.witch_heal_left = True
                    self._witch_note(f"🧪 {self._name(t)} đã được bảo vệ sẵn – bình cứu được hoàn lại.")
                continue
            if self.witch_heal == t:
                self._hist(f"{self._name(t)} được Phù Thủy cứu sống.")
                continue
            v = self.players[t]
            vd = self._cdef(v.role)
            if vd and vd["tough"] and t not in self.tough_used:  # chịu được 1 lần cắn
                self.tough_used.add(t)
                self._note(t, f"💪 Đêm {self.day_no}: sói cắn bạn nhưng bạn vẫn trụ được. Lần sau thì không.")
                self._hist(f"{v.name} ({vd['name']}) bị sói cắn nhưng trụ được.")
                continue
            if v.role == Role.CURSED:
                v.role = Role.WEREWOLF
                self._join_pack(v, "🌗 Bạn bị sói cắn và đã hoá thành Ma Sói!")
                self._hist(f"Kẻ Bị Nguyền {v.name} bị cắn và hoá thành Ma Sói.")
            else:
                deaths[t] = "wolf"
        if self.witch_poison:
            if self.witch_poison in deaths:  # đằng nào cũng chết -> hoàn bình
                self.witch_poison_left = True
                self._witch_note(f"☠️ {self._name(self.witch_poison)} đã chết vì sói – bình độc được hoàn lại.")
            else:
                deaths[self.witch_poison] = "poison"
        for t, by in self.custom_kills.items():
            if t not in deaths and self.players[t].alive:
                deaths[t] = "kill"
                self._hist(f"{self._name(t)} bị {by} ra tay giết.")
        self.last_guarded = self.guard_target

        order = list(deaths)
        random.shuffle(order)  # không lộ ai chết vì sói, ai chết vì độc
        self._period = f"Ngày {self.day_no}"
        reveal = self.settings.reveal_role_on_death
        if order:
            names = self._names(order)
            self._event(f"☀️ Trời sáng. Đêm qua {names} đã chết.")
            roles = " ".join(self._t("reveal", ten=self._name(d), vai=self._rn(self.players[d].role))
                             for d in order) if reveal else ""
            self._say(f"{self._t('dawn_deaths', nguoi_chet=names)} {roles}".strip())
            for d in order:
                self._hist(f"{self._name(d)} ({self._rn(self.players[d].role)}) chết vì "
                           + ({"wolf": "bị sói cắn.", "poison": "bình độc của Phù Thủy."}.get(deaths[d], "bị ra tay trong đêm.")))
        else:
            self._event("☀️ Trời sáng. Đêm qua bình yên, không ai chết.")
            self._say(self._t("dawn_peace"))
        killed = []
        for pid in order:
            role_before = self.players[pid].role
            killed += self._kill(pid, deaths[pid])
            if role_before == Role.DISEASED and deaths[pid] == "wolf" and pid != self.frozen:
                self.sick_next = True
                self._hist("Bầy sói đã cắn phải Người Bệnh – đêm sau không cắn được.")
        if self.spell_target and self.players[self.spell_target].alive:
            self.silenced = self.spell_target
            self._event(f"🤐 {self._name(self.silenced)} bị phù phép – không được nói trong buổi thảo luận hôm nay.")
            self._say(self._t("silenced", ten=self._name(self.silenced)))
        await self._announce(Phase.DAWN)

        for pid, _ in killed:  # Thợ Săn (và vai tự tạo "kéo theo") chết trong đêm được bắn
            cd = self._cdef(self.players[pid].role)
            if cd and cd["revenge"]:
                if pid == self.frozen:
                    self._hist(f"{self._name(pid)} bị phong ấn nên không kéo theo được ai.")
                else:
                    await self._hunter(pid, cd["name"])
            elif self.players[pid].role == Role.HUNTER:
                if pid == self.frozen:
                    self._hist(f"Thợ Săn {self._name(pid)} bị phong ấn nên không bắn được.")
                else:
                    await self._hunter(pid)
        if self._check_win():
            await self._end()
            return True
        return False

    def _witch_note(self, text: str) -> None:
        for w in self.seated():
            if w.role == Role.WITCH:
                self._note(w.id, text)

    async def _day(self) -> bool:
        mins = self.settings.discuss_seconds
        length = f"{mins // 60} phút" + (f" {mins % 60} giây" if mins % 60 else "") if mins >= 60 else f"{mins} giây"
        self._say(self._t("discuss", thoi_gian=length))
        self.ready_vote = set()
        self._day_log_start = len(self.log)
        if not any(not p.bot for p in self.alive()):
            mins = min(mins, 12)  # chỉ còn máy chơi: thảo luận ngắn
        for b in self.alive():
            if b.bot and random.random() < .6:
                lo = min(4, mins * .15)
                asyncio.create_task(self._bot_chat(b.id, self._step_id + 1, random.uniform(lo, max(lo + .1, mins * .5))))
        await self._step(Phase.DAY_DISCUSS, mins, set(), early=True)
        self.day_votes = {}
        self._say(self._t("vote"))
        await self._step(Phase.DAY_VOTE, self.settings.vote_seconds,
                         {p.id for p in self.alive()}, early=True)
        target = self._vote_result()
        if self.settings.show_votes and self.day_votes:
            by: dict[str, list[str]] = {}
            for voter, t in self.day_votes.items():
                by.setdefault(t, []).append(self._name(voter))
            for t, names in sorted(by.items(), key=lambda kv: -len(kv[1])):
                who = "Phiếu trắng" if t == "skip" else self._name(t)
                self._event(f"📋 {who} ({len(names)}): {', '.join(names)}")
                self._say(self._t("vote_detail", ten=who, so_phieu=len(names), nguoi_bo_phieu=self._names_list(names)))
        if target:
            n = sum(1 for t in self.day_votes.values() if t == target)
            role = self._rn(self.players[target].role)
            self._event(f"⚖️ Dân làng đã treo cổ {self._name(target)}.")
            self._say(self._t("lynch", so_phieu=n, ten=self._name(target))
                      + (" " + self._t("reveal", ten=self._name(target), vai=role)
                         if self.settings.reveal_role_on_death else ""))
            self._hist(f"Dân làng treo cổ {self._name(target)} ({role}) với {n} phiếu.")
            self._kill(target, "lynch")
        else:
            self._event("⚖️ Phiếu không đủ – hôm nay không ai bị treo cổ.")
            self._say(self._t("no_lynch"))
            self._hist("Không ai bị treo cổ.")
        self.silenced = None
        await self._announce(Phase.VERDICT)
        if self._check_win():
            await self._end()
            return True
        return False

    def _vote_result(self) -> str | None:
        c = Counter(self.day_votes.values())
        skips = c.pop("skip", 0)
        if not c:
            return None
        top = max(c.values())
        leaders = [t for t, n in c.items() if n == top]
        if len(leaders) > 1 or skips >= top:  # hoà, hoặc phiếu trắng thắng
            return None
        return leaders[0]

    async def _hunter(self, pid: str, label: str | None = None) -> None:
        self.hunter_shooter, self.hunter_target = pid, None
        self._event(f"🏹 {self._name(pid)} là {label or 'Thợ Săn'} và được kéo một người chết theo!")
        self._say(self._t("hunter_call", ten=self._name(pid)) if not label
                  else f"{self._name(pid)} là {label}. Hãy chọn một người để kéo theo.")
        await self._step(Phase.HUNTER_SHOT, self.settings.hunter_seconds, {pid}, early=True)
        t = self.hunter_target
        if t and self.players[t].alive:
            role = self._rn(self.players[t].role)
            self._event(f"💥 Thợ Săn đã bắn {self._name(t)}.")
            self._say(self._t("hunter_shot", ten=self._name(t))
                      + (" " + self._t("reveal", ten=self._name(t), vai=role)
                         if self.settings.reveal_role_on_death else ""))
            self._hist(f"{label or 'Thợ Săn'} {self._name(pid)} bắn chết {self._name(t)} ({role}).")
            self._kill(t, "hunter")
        else:
            self._event("Thợ Săn đã hạ súng, không bắn ai.")
            self._say(self._t("hunter_skip"))
            self._hist(f"{label or 'Thợ Săn'} {self._name(pid)} không kéo theo ai.")
        self.hunter_shooter = None
        await self._announce(Phase.VERDICT)

    async def _end(self) -> None:
        self.phase, self.deadline, self.actors = Phase.ENDED, None, set()
        won = "Phe Dân Làng" if self.winner == "village" else "Phe Ma Sói"
        self._event(("🏆 " if self.winner == "village" else "🐺 ") + f"{won} chiến thắng!")
        self._say(self._t("end", phe_thang=won))
        self._score_game()
        top = self.leaderboard()[:3]
        if top:
            self._say(self._t("ranking", so_van=self.games_played, xep_hang=", ".join(
                f"hạng {r['rank']} là {r['name']} với {r['points']} điểm" for r in top)))
        period = None
        for h in self.history:
            if h["period"] != period:
                period = h["period"]
                self._say(period + ".")
            self._say(h["text"])
        self._say(self._t("thanks"))
        await self._broadcast(self)

    # ------------------------------------------------------------ tính điểm
    POINTS_PER_WIN = 1

    def _team(self, p: Player) -> str:
        """Phe lúc kết thúc ván (đã tính Kẻ Bị Nguyền hoá sói, Kẻ Nhân Bản nhận vai…)."""
        d = self._cdef(p.role)
        if d:
            return d["team"]
        return "wolves" if (p.role in WOLF_CARDS or p.role == Role.TRAITOR) else "village"

    def _score_game(self) -> None:
        """Phe thắng được điểm, kể cả người đã chết trong đêm. Bị treo cổ ban ngày: không tính điểm."""
        self.games_played += 1
        self.last_result = []
        for p in self.seated():
            won = self._team(p) == self.winner
            lynched = p.death == "day"
            gain = self.POINTS_PER_WIN if (won and not lynched) else 0
            if won and lynched:
                reason = "Phe thắng nhưng bị treo cổ – không tính điểm"
            elif won and p.alive:
                reason = "Phe thắng, sống sót"
            elif won:
                reason = "Phe thắng, chết trong đêm vẫn được tính"
            else:
                reason = "Phe thua"
            st = self.scores.setdefault(p.name, {"name": p.name, "bot": p.bot, "points": 0,
                                                 "games": 0, "wins": 0, "lynched": 0})
            st["games"] += 1
            st["points"] += gain
            st["wins"] += int(won)
            st["lynched"] += int(lynched)
            self.last_result.append({"id": p.id, "name": p.name, "role": rv(p.role),
                                     "team": self._team(p), "won": won, "lynched": lynched,
                                     "alive": p.alive, "gain": gain, "reason": reason})

    def leaderboard(self) -> list[dict]:
        rows = sorted(self.scores.values(),
                      key=lambda r: (-r["points"], -r["wins"], r["lynched"], r["games"], r["name"]))
        out, prev, rank = [], None, 0
        for k, r in enumerate(rows):
            key = (r["points"], r["wins"], r["lynched"])
            if key != prev:
                rank, prev = k + 1, key
            out.append({**r, "rank": rank})
        return out

    def reset_scores(self) -> None:
        if self.phase not in (Phase.LOBBY, Phase.ENDED):
            raise GameError("Chỉ xoá bảng điểm khi không có ván đang chơi")
        self.scores, self.games_played = {}, 0

    # ------------------------------------------------------------ hành động
    async def handle_action(self, pid: str, action: str, target=None) -> None:
        p = self.players.get(pid)
        if p is None:
            raise GameError("Người chơi không tồn tại")
        ph = self.phase

        if action == "skip_discussion":
            if not p.is_host or ph != Phase.DAY_DISCUSS:
                raise GameError("Chỉ chủ phòng mới bỏ qua được thảo luận")
            self._say(self._t("host_skip"))
            self._finish_step()
            return
        if action == "ready_vote":
            if ph != Phase.DAY_DISCUSS or p.narrator or not p.alive:
                raise GameError("Chỉ người còn sống mới bấm được lúc thảo luận")
            self.ready_vote ^= {pid}  # bấm lần nữa để huỷ
            voters = [x for x in self.alive() if not x.bot]  # chỉ người thật quyết định
            if len(self.ready_vote) * 2 > len(voters):
                self._event(f"🗳️ Đa số ({len(self.ready_vote)}/{len(voters)}) muốn bỏ phiếu ngay.")
                self._say(self._t("ready"))
                self._finish_step()
            await self._broadcast(self)
            return
        if pid not in self.actors:
            raise GameError("Hết lượt của bạn rồi" if ph.value.startswith("night") else "Chưa đến lượt của bạn")
        if ph in FREEZABLE and pid == self.frozen:
            if action != "frozen_ack":
                raise GameError("Bạn đang bị Sói Băng phong ấn")
            self._done(pid)
            await self._broadcast(self)
            return

        if ph == Phase.NIGHT_CREATOR and action == "creator_pick":
            if target not in [rv(r) for r in self.creator_options]:
                raise GameError("Hãy chọn một trong hai lá")
            self.creator_choice = self._as_role(target)
            self._done(pid)

        elif ph == Phase.NIGHT_CLONER and action == "clone":
            self._need_alive(target)
            if target == pid:
                raise GameError("Hãy chọn người khác")
            self.clone_target = target
            self._note(pid, f"🧬 Bạn đã chọn nhân bản {self._name(target)}.")
            self._hist(f"Kẻ Nhân Bản {p.name} chọn nhân bản {self._name(target)}.")
            self._done(pid)

        elif ph == Phase.NIGHT_GUARD and action == "guard":
            self._need_alive(target)
            if target == self.last_guarded:
                raise GameError("Không được bảo vệ một người hai đêm liền")
            self.guard_target = target
            self._note(pid, f"🛡️ Đêm {self.day_no}: bạn bảo vệ {self._name(target)}")
            self._hist(f"Bảo Vệ {p.name} bảo vệ {self._name(target)}.")
            self._done(pid)

        elif ph == Phase.NIGHT_WOLVES and action == "sick_ack":
            self._done(pid)
        elif ph == Phase.NIGHT_WOLVES and action == "wolf_vote":
            picks = target if isinstance(target, list) else [target]
            if len(set(map(str, picks))) != self.bites:
                raise GameError(f"Đêm nay bầy được cắn {self.bites} người")
            for t in picks:
                self._need_alive(t)
                if self.is_wolf(self.players[t]):
                    raise GameError("Không thể cắn đồng đội")
            self.wolf_votes[pid] = sorted(picks)  # đổi được đến khi cả bầy thống nhất
            if not p.bot:  # máy sói đi theo lựa chọn của sói người
                for w in self.wolves():
                    if w.bot and self.wolf_votes.get(w.id) != sorted(picks):
                        asyncio.create_task(self._bot_turn(w.id, self._step_id, random.uniform(.8, 2)))
            pack = {w.id for w in self.wolves()}
            if pack <= self.wolf_votes.keys() and len({tuple(v) for v in self.wolf_votes.values()}) == 1:
                self._finish_step()

        elif ph == Phase.NIGHT_WITCH and action == "witch_heal":
            if not self.witch_heal_left or target not in self.wolf_targets:
                raise GameError("Không thể dùng bình cứu cho người này")
            self.witch_heal, self.witch_heal_left = target, False
            self._note(pid, f"🧪 Đêm {self.day_no}: bạn cứu {self._name(target)}")
            self._hist(f"Phù Thủy {p.name} dùng bình cứu cho {self._name(target)}.")
        elif ph == Phase.NIGHT_WITCH and action == "witch_poison":
            if not self.witch_poison_left:
                raise GameError("Bạn đã dùng bình độc")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự đầu độc")
            self.witch_poison, self.witch_poison_left = target, False
            self._note(pid, f"☠️ Đêm {self.day_no}: bạn đầu độc {self._name(target)}")
            self._hist(f"Phù Thủy {p.name} đầu độc {self._name(target)}.")
        elif ph == Phase.NIGHT_WITCH and action == "witch_done":
            self._done(pid)

        elif ph == Phase.NIGHT_SEER and action == "seer_check":
            if self.seer_result:
                raise GameError("Bạn đã soi rồi")
            self._need_alive(target)
            if target == pid:
                raise GameError("Không thể tự soi mình")
            wolf = self._looks_wolf(self.players[target])  # Kẻ Phản Bội, Kẻ Bị Nguyền chưa hoá: phe Dân
            self.seer_result = f"{self._name(target)} thuộc {'PHE SÓI 🐺' if wolf else 'phe Dân Làng'}"
            self._note(pid, f"🔮 Đêm {self.day_no}: {self.seer_result}")
            self._hist(f"Tiên Tri {p.name} soi {self._name(target)}: {'phe Sói' if wolf else 'phe Dân'}.")
        elif ph == Phase.NIGHT_SEER and action == "seer_ack":
            self._done(pid)

        elif ph == Phase.NIGHT_MOON and action in ("moon_check", "moon_shield"):
            if self.moon_result or self.moon_shield:
                raise GameError("Đêm nay bạn đã dùng năng lực")
            self._need_alive(target)
            if action == "moon_check":
                if target == pid:
                    raise GameError("Không thể tự soi mình")
                role = self._rn(self.players[target].role)
                self.moon_result = f"{self._name(target)} là {role}"
                self._note(pid, f"🌙 Đêm {self.day_no}: {self.moon_result}")
                self._hist(f"Nguyệt Nữ {p.name} soi {self._name(target)}: {role}.")
            else:
                self.moon_shield = target
                self._note(pid, f"🌙 Đêm {self.day_no}: bạn che chắn {self._name(target)}")
                self._hist(f"Nguyệt Nữ {p.name} che chắn {self._name(target)}.")
                self._done(pid)
        elif ph == Phase.NIGHT_MOON and action == "moon_ack":
            self._done(pid)

        elif ph == Phase.NIGHT_SNOW and action == "freeze":
            self._need_alive(target)
            if self.is_wolf(self.players[target]):
                raise GameError("Không thể phong ấn đồng đội")
            if target == self.moon_shield:  # lá chắn mặt trăng chặn được phong ấn
                self._hist(f"Sói Băng {p.name} định phong ấn {self._name(target)} nhưng bị lá chắn mặt trăng chặn.")
            else:
                self.frozen_next = target
                self._hist(f"Sói Băng {p.name} phong ấn {self._name(target)} cho đêm sau.")
            self._note(pid, f"❄️ Đêm {self.day_no}: bạn phong ấn {self._name(target)} (có hiệu lực đêm sau)")
            self._done(pid)

        elif ph == Phase.NIGHT_CUSTOM and action == "custom_act":
            d = self._cdef(self.custom_key)
            if not d or p.role != self.custom_key:
                raise GameError("Không phải lượt của bạn")
            if pid in self.custom_results:
                raise GameError("Bạn đã dùng năng lực đêm nay")
            self._need_alive(target)
            act = d["action"]
            if act != "protect" and target == pid:
                raise GameError("Hãy chọn người khác")
            if d["no_repeat"] and self.custom_last.get(pid) == target:
                raise GameError("Không được chọn một người hai đêm liền")
            self.custom_last[pid] = target
            self.custom_used[pid] = self.custom_used.get(pid, 0) + 1
            tname = self._name(target)
            if act == "protect":
                self.custom_protect[target] = d["name"]
                self.custom_results[pid] = ""
                self._note(pid, f"{d['icon']} Đêm {self.day_no}: bạn bảo vệ {tname}")
                self._hist(f"{d['name']} {p.name} bảo vệ {tname}.")
                self._done(pid)
            elif act == "kill":
                self.custom_kills[target] = d["name"]
                self.custom_results[pid] = ""
                self._note(pid, f"{d['icon']} Đêm {self.day_no}: bạn ra tay với {tname}")
                self._hist(f"{d['name']} {p.name} ra tay với {tname}.")
                self._done(pid)
            else:
                if act == "check_team":
                    res = f"{tname} thuộc {'PHE SÓI 🐺' if self._looks_wolf(self.players[target]) else 'phe Dân Làng'}"
                else:
                    res = f"{tname} là {self._rn(self.players[target].role)}"
                self.custom_results[pid] = res
                self._note(pid, f"{d['icon']} Đêm {self.day_no}: {res}")
                self._hist(f"{d['name']} {p.name} soi: {res}.")
        elif ph == Phase.NIGHT_CUSTOM and action == "custom_ack":
            self._done(pid)

        elif ph == Phase.NIGHT_SPELL and action == "silence":
            self._need_alive(target)
            if target == pid:
                raise GameError("Hãy chọn người khác")
            self.spell_target = target
            self._note(pid, f"🤐 Đêm {self.day_no}: bạn phù phép {self._name(target)}")
            self._hist(f"Kẻ Phù Phép {p.name} làm {self._name(target)} bị câm.")
            self._done(pid)

        elif ph == Phase.DAY_VOTE and action == "vote":
            if target != "skip":
                self._need_alive(target)
                if target == pid:
                    raise GameError("Không thể bỏ phiếu cho chính mình")
            self.day_votes[pid] = target
            if self.actors <= self.day_votes.keys():
                self._finish_step()

        elif ph == Phase.HUNTER_SHOT and action in ("hunter_shoot", "hunter_skip"):
            if action == "hunter_shoot":
                self._need_alive(target)
                if target == pid:
                    raise GameError("Hãy chọn người khác")
                self.hunter_target = target
            self._finish_step()
        else:
            raise GameError("Hành động không hợp lệ lúc này")
        await self._broadcast(self)

    # ------------------------------------------------------------ máy chơi: suy nghĩ
    def _mem(self, pid: str) -> dict:
        return self.bot_mem.setdefault(pid, {"checked": {}})

    def _wake_bots(self, seconds: float) -> None:
        """Mỗi khi mở một bước, máy chơi có lượt sẽ 'suy nghĩ' một lúc rồi hành động."""
        bots = [pid for pid in self.actors if self.players[pid].bot]
        if not bots:
            return
        lead = self.call_seconds if self.phase.value.startswith("night") else 0
        room = max(1.0, (seconds - lead) * .6)
        for pid in bots:
            delay = lead + random.uniform(min(1.5, room), min(room, 7))
            asyncio.create_task(self._bot_turn(pid, self._step_id, delay))

    async def _bot_turn(self, pid: str, step_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        for _ in range(4):  # vài thao tác liên tiếp: soi rồi bấm "đã ghi nhớ", cứu rồi "xong"…
            if self._step_id != step_id or pid not in self.actors:
                return
            p = self.players[pid]
            a = self._action_for(p)
            if not a:
                return
            act, target = self._bot_choose(p, a)
            try:
                await self.handle_action(pid, act, target)
            except GameError:
                return
            if act == "seer_check":
                self._mem(pid)["checked"][target] = self.is_wolf(self.players[target])
            if act in ("wolf_vote", "vote"):
                return
            await asyncio.sleep(random.uniform(.8, 1.6))

    def _bot_choose(self, p: Player, a: dict):
        kind, targets = a["kind"], a.get("targets") or []
        mem = self._mem(p.id)
        known = [t for t, w in mem["checked"].items() if w and self.players[t].alive]
        if a.get("info") or a.get("result"):
            return a["ack"], None
        if kind == "creator":
            return "creator_pick", random.choice(a["options"])
        if kind == "moon":
            if random.random() < .5:
                return "moon_shield", random.choice(targets)
            return "moon_check", random.choice([t for t in targets if t != p.id] or targets)
        if kind == "witch":
            if a["heal_left"] and a["victims"] and not a["healed"] and random.random() < .5:
                return "witch_heal", a["victims"][0]
            if (a["poison_left"] and not a["poisoned"] and self.day_no >= 2 and targets
                    and not mem.get("poison_tried") and random.random() < .3):
                mem["poison_tried"] = True
                pool = [t for t in targets if t in known] or targets
                return "witch_poison", random.choice(pool)
            return "witch_done", None
        if kind == "wolf_vote":
            team = a["team_votes"]
            human = [v for w, v in team.items() if not self.players[w].bot]
            pick = human[0] if human else (next(iter(team.values())) if team else
                                           random.sample(targets, min(a["pick"], len(targets))))
            return "wolf_vote", pick
        if kind == "seer":
            fresh = [t for t in targets if t not in mem["checked"]]
            return "seer_check", random.choice(fresh or targets)
        if kind == "vote":
            return "vote", self._bot_vote(p, targets, known)
        if kind == "hunter":
            pool = [t for t in targets if t in known or self.public_claims.get(t)] or targets
            return "hunter_shoot", random.choice(pool)
        if kind == "custom":
            if a["action"] in ("check_team", "check_role"):
                fresh = [t for t in targets if t not in mem["checked"]]
                t = random.choice(fresh or targets)
                if a["action"] == "check_team":
                    mem["checked"][t] = self._looks_wolf(self.players[t])
                return "custom_act", t
            if a["action"] == "kill" and not self.is_wolf(p):
                return "custom_act", random.choice([t for t in targets if t in known] or targets)
            return "custom_act", random.choice(targets)
        return a["act"], random.choice(targets)

    def _bot_vote(self, p: Player, targets: list[str], known: list[str]):
        tally = Counter(t for t in self.day_votes.values() if t != "skip")
        leader = next((t for t, _ in tally.most_common() if t in targets), None)
        if self.is_wolf(p):
            prey = [t for t in targets if not self.is_wolf(self.players[t])]
            if leader in prey and random.random() < .6:
                return leader
            claimed = [t for t in prey if self.public_claims.get(t) is False]
            return random.choice([t for t in prey if t not in claimed] or prey or targets)
        if known:
            return random.choice(known)
        claimed = [t for t in targets if self.public_claims.get(t)]
        if claimed and random.random() < .7:
            return claimed[0]
        if leader and random.random() < .5:
            return leader
        return "skip" if random.random() < .1 else random.choice(targets)

    async def _bot_chat(self, pid: str, step_id: int, delay: float) -> None:
        """Máy chơi lên tiếng trong buổi thảo luận (quản trò đọc to)."""
        await asyncio.sleep(delay)
        p = self.players[pid]
        if self._step_id != step_id or self.phase != Phase.DAY_DISCUSS or not p.alive:
            return
        others = [x for x in self.alive() if x.id != pid]
        if not others:
            return
        mem = self._mem(pid)
        wolves = [t for t, w in mem["checked"].items() if w and self.players[t].alive]
        clears = [t for t, w in mem["checked"].items() if not w and self.players[t].alive]
        claimed = [t for t, w in self.public_claims.items() if w and self.players[t].alive and t != pid]
        if p.role == Role.SEER and wolves:
            t = wolves[0]
            self.public_claims[t] = True
            line = f"Tôi là Tiên Tri. Tôi đã soi {self._name(t)}, đó là Sói! Treo cổ {self._name(t)} đi."
        elif p.role == Role.SEER and clears and random.random() < .5:
            t = clears[0]
            self.public_claims.setdefault(t, False)
            line = f"Tôi là Tiên Tri. {self._name(t)} là người tốt, đừng treo nhầm."
        elif self.is_wolf(p):
            prey = [x for x in others if not self.is_wolf(x)]
            t = random.choice(prey or others)
            line = random.choice([f"Tôi thấy {t.name} rất đáng ngờ.", f"{t.name} im lặng quá, tôi nghi lắm.",
                                  f"Hôm nay tôi sẽ bỏ phiếu cho {t.name}."])
        elif claimed:
            line = f"Tôi tin Tiên Tri, treo {self._name(claimed[0])} đi."
        else:
            t = random.choice(others)
            line = random.choice([f"Tôi chưa chắc lắm, nhưng {t.name} hơi lạ.", "Tôi chưa có manh mối, mọi người nói trước đi.",
                                  f"Tôi nghi {t.name}."])
        self._event(f"💬 {p.name}: {line}")
        self._say(f"{p.name.replace('🤖', '').strip()} nói: {line}")
        await self._broadcast(self)

    # ------------------------------------------------------------ góc nhìn riêng
    def view_for(self, pid: str) -> dict:
        me = self.players[pid]
        started = self.phase != Phase.LOBBY
        ended = self.phase == Phase.ENDED

        def visible_role(o: Player) -> str | None:
            if not started or o.role is None:
                return None
            if (ended or o.id == pid or me.narrator
                    or (not o.alive and self.settings.reveal_role_on_death)
                    or (self.is_wolf(me) and self.is_wolf(o))):
                return rv(o.role)
            return None

        narr = self.narrator
        return {
            "type": "state",
            "code": self.code,
            "phase": self.phase.value,
            "day": self.day_no,
            "deadline": self.deadline,
            "server_now": time.time(),
            "me": {"id": me.id, "name": me.name, "is_host": me.is_host, "alive": me.alive,
                   "narrator": me.narrator, "role": rv(me.role) if me.role else None,
                   "wolf": self.is_wolf(me) if me.role else False},
            "narrator": {"name": narr.name, "connected": narr.connected} if narr else None,
            "players": [{"id": o.id, "name": o.name, "alive": o.alive, "bot": o.bot,
                         "connected": o.connected, "is_host": o.is_host,
                         "role": visible_role(o)} for o in self.seated()],
            # chỉ chủ phòng / máy quản trò biết cấu hình vai
            "settings": {**asdict(self.settings), "roles": self.settings.roles if me.is_host else {},
                         "scripts": self.settings.scripts if me.is_host else {},
                         "custom_roles": self.settings.custom_roles if me.is_host else {}},
            "roles_custom": self.roles_custom,
            "limits": {"min": MIN_PLAYERS, "max": MAX_PLAYERS, "creator_extra": CREATOR_EXTRA},
            # người chơi chỉ thấy: ai chết, ai bị treo, ai bị câm… (không thấy lời bàn / số người bấm Vote ngay)
            "log": (self.log if me.narrator else
                    [e for e in self.log if not e["text"].startswith(("💬", "🗳️"))])[-40:],
            "chat": [e for e in self.log[self._day_log_start:] if e["text"].startswith("💬")][-6:]
                    if self.phase == Phase.DAY_DISCUSS else [],
            "private": me.private_log[-20:],
            "action": self._action_for(me),
            # phiếu kín: người chơi chỉ thấy số phiếu, không biết ai bầu ai
            "votes": ({} if self.phase not in (Phase.DAY_VOTE, Phase.VERDICT)
                      else self.day_votes if (self.settings.show_votes or me.narrator)
                      else {f"#{k}": t for k, t in enumerate(self.day_votes.values())}),
            "hunter": self.hunter_shooter,
            "silenced": self.silenced,
            "next_ready": {"count": len(self.next_ready), "me": pid in self.next_ready,
                           "ids": sorted(self.next_ready),
                           "total": len(self.humans())},
            "ready": {"count": len(self.ready_vote),
                      "need": len([x for x in self.alive() if not x.bot]) // 2 + 1,
                      "me": pid in self.ready_vote},
            "speech": self.speech[-150:] if (me.narrator or (me.is_host and self.settings.host_voice)) else [],
            "script_defaults": SCRIPT_DEFAULTS if me.is_host else None,
            "history": self.history if ended else [],
            "scores": self.leaderboard(),
            "games_played": self.games_played,
            "last_result": self.last_result if ended else [],
            "winner": self.winner,
            "role_defs": {k: {x: d[x] for x in ("name", "icon", "desc", "team")}
                          for k, d in self.settings.custom_roles.items()},
            "custom_step": ({x: self._cdef(self.custom_key)[x] for x in ("name", "icon")}
                            if self.phase == Phase.NIGHT_CUSTOM and self.custom_key else None),
        }

    def _action_for(self, me: Player) -> dict | None:
        """Việc người này cần làm ngay bây giờ.

        act: hành động gửi lên khi xác nhận; pick: số người cần chọn;
        result/ack: kết quả soi và hành động để đóng lượt; info: chỉ thông báo.
        """
        if me.id not in self.actors:
            return None
        ph = self.phase
        alive = [p.id for p in self.alive()]
        others = [x for x in alive if x != me.id]
        if ph in FREEZABLE and me.id == self.frozen:
            return {"kind": "frozen", "ack": "frozen_ack",
                    "info": "Sói Băng đã phong ấn bạn. Đêm nay bạn không dùng được năng lực."}
        if ph == Phase.NIGHT_CREATOR:
            return {"kind": "creator", "act": "creator_pick",
                    "options": [rv(r) for r in self.creator_options]}
        if ph == Phase.NIGHT_CLONER:
            return {"kind": "cloner", "act": "clone", "pick": 1, "targets": others}
        if ph == Phase.NIGHT_GUARD:
            return {"kind": "guard", "act": "guard", "pick": 1, "last": self.last_guarded,
                    "targets": [x for x in alive if x != self.last_guarded]}
        if ph == Phase.NIGHT_WOLVES:
            if self.bites == 0:
                return {"kind": "wolf_sick", "ack": "sick_ack",
                        "info": "Đêm qua bầy đã cắn phải Người Bệnh. Cả bầy nhiễm bệnh, đêm nay không cắn được ai."}
            return {"kind": "wolf_vote", "act": "wolf_vote", "pick": self.bites,
                    "chosen": self.wolf_votes.get(me.id) or [],
                    "team_votes": self.wolf_votes,
                    "targets": [p.id for p in self.alive() if not self.is_wolf(p)]}
        if ph == Phase.NIGHT_WITCH:
            return {"kind": "witch", "act": "witch_poison", "pick": 1,
                    "victims": self.wolf_targets if (self.witch_heal_left or self.witch_heal) else [],
                    "heal_left": self.witch_heal_left, "healed": self.witch_heal,
                    "poison_left": self.witch_poison_left, "poisoned": self.witch_poison,
                    "targets": others}
        if ph == Phase.NIGHT_SEER:
            return {"kind": "seer", "act": "seer_check", "pick": 1,
                    "result": self.seer_result, "ack": "seer_ack", "targets": others}
        if ph == Phase.NIGHT_MOON:
            return {"kind": "moon", "pick": 1, "targets": alive,
                    "result": self.moon_result, "ack": "moon_ack"}
        if ph == Phase.NIGHT_SNOW:
            return {"kind": "snow", "act": "freeze", "pick": 1,
                    "targets": [p.id for p in self.alive() if not self.is_wolf(p)]}
        if ph == Phase.NIGHT_SPELL:
            return {"kind": "spell", "act": "silence", "pick": 1, "targets": others}
        if ph == Phase.NIGHT_CUSTOM:
            d = self._cdef(self.custom_key)
            act = d["action"]
            if act == "protect":
                targets = [x for x in alive if not (d["no_repeat"] and self.custom_last.get(me.id) == x)]
            elif act == "kill" and d["team"] == "wolves":
                targets = [p.id for p in self.alive() if not self.is_wolf(p)]
            else:
                targets = [x for x in others if not (d["no_repeat"] and self.custom_last.get(me.id) == x)]
            left = (f" Còn {d['uses'] - self.custom_used.get(me.id, 0)} lần dùng." if d["uses"] else "")
            return {"kind": "custom", "act": "custom_act", "pick": 1, "action": act,
                    "title": f"{d['icon']} {d['name']}", "verb": CUSTOM_ACTIONS[act][0],
                    "desc": (d["desc"] or CUSTOM_ACTIONS[act][1].capitalize() + ".") + left,
                    "danger": act == "kill", "targets": targets,
                    "result": self.custom_results.get(me.id) or None, "ack": "custom_ack"}
        if ph == Phase.DAY_VOTE:
            return {"kind": "vote", "act": "vote", "pick": 1,
                    "chosen": self.day_votes.get(me.id), "targets": others}
        if ph == Phase.HUNTER_SHOT:
            return {"kind": "hunter", "act": "hunter_shoot", "pick": 1, "targets": others}
        return None
