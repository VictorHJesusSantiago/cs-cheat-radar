"""Modelo normalizado de uma demo, independente do parser usado."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlayerTick:
    """Estado de um jogador em um tick."""

    tick: int
    steamid: int
    x: float
    y: float
    z: float
    pitch: float
    yaw: float
    is_alive: bool = True
    team: int = 0
    health: int = 100
    round_num: int = 0
    # bitmask de teclas pressionadas neste tick. Só jogos que expõem entrada
    # preenchem (hoje: osu!). Zero significa "nao sei", nao "nada apertado".
    keys: int = 0


@dataclass
class Kill:
    tick: int
    attacker: int
    victim: int
    weapon: str = ""
    headshot: bool = False
    round_num: int = 0
    noscope: bool = False
    through_smoke: bool = False
    penetrated: int = 0


@dataclass
class Shot:
    tick: int
    steamid: int
    weapon: str = ""
    round_num: int = 0


@dataclass
class Round:
    number: int
    start_tick: int
    end_tick: int


@dataclass
class PlayerInfo:
    steamid: int
    name: str = ""
    team: int = 0


@dataclass
class DemoData:
    """Tudo o que os detectores precisam, ja normalizado."""

    source: str = ""
    map_name: str = ""
    game: str = "cs2"
    tickrate: float = 64.0
    # o que esta fonte consegue entregar; ver games/base.py. O motor de score
    # roda apenas os sinais sustentados por estas capacidades.
    capabilities: set = field(
        default_factory=lambda: {"angles", "positions", "shots", "kills",
                                 "rounds", "teams"}
    )
    players: dict = field(default_factory=dict)          # steamid -> PlayerInfo
    ticks_by_player: dict = field(default_factory=dict)  # steamid -> [PlayerTick] ordenado
    kills: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    rounds: list = field(default_factory=list)

    def steamids(self) -> list:
        return sorted(self.ticks_by_player.keys())

    def name_of(self, steamid: int) -> str:
        info = self.players.get(steamid)
        return info.name if info else str(steamid)

    def team_of(self, steamid: int) -> int:
        info = self.players.get(steamid)
        if info and info.team:
            return info.team
        seq = self.ticks_by_player.get(steamid) or []
        return seq[-1].team if seq else 0

    def index_ticks(self) -> dict:
        """steamid -> {tick: PlayerTick} para lookup O(1)."""
        return {sid: {t.tick: t for t in seq} for sid, seq in self.ticks_by_player.items()}

    def tick_range(self) -> tuple:
        lo, hi = None, None
        for seq in self.ticks_by_player.values():
            if not seq:
                continue
            lo = seq[0].tick if lo is None else min(lo, seq[0].tick)
            hi = seq[-1].tick if hi is None else max(hi, seq[-1].tick)
        return (lo or 0, hi or 0)


@dataclass
class Evidence:
    """Um momento concreto para o usuario conferir na demo."""

    kind: str
    round_num: int
    tick: int
    detail: str = ""
    target: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "round": self.round_num,
            "tick": self.tick,
            "detail": self.detail,
            "target": self.target,
        }


@dataclass
class SignalResult:
    """Saida de um detector para um jogador."""

    name: str
    value: float          # 0..1, ja normalizado
    raw: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    samples: int = 0
    confident: bool = True


@dataclass
class PlayerReport:
    steamid: int
    name: str
    team: int
    kills: int = 0
    deaths: int = 0
    headshots: int = 0
    shots: int = 0
    score: float = 0.0
    signals: dict = field(default_factory=dict)  # name -> SignalResult
    flags: list = field(default_factory=list)

    @property
    def hs_pct(self) -> float:
        return 100.0 * self.headshots / self.kills if self.kills else 0.0

    def top_evidence(self, limit: int = 6) -> list:
        ev = []
        for sig in self.signals.values():
            ev.extend(sig.evidence)
        ev.sort(key=lambda e: (e.round_num, e.tick))
        return ev[:limit]

    def as_dict(self) -> dict:
        return {
            "steamid": self.steamid,
            "name": self.name,
            "team": self.team,
            "kills": self.kills,
            "deaths": self.deaths,
            "headshots": self.headshots,
            "hs_pct": round(self.hs_pct, 1),
            "shots": self.shots,
            "score": round(self.score, 1),
            "flags": self.flags,
            "signals": {
                k: {
                    "value": round(v.value, 3),
                    "samples": v.samples,
                    "confident": v.confident,
                    "raw": v.raw,
                }
                for k, v in self.signals.items()
            },
            "evidence": [e.as_dict() for e in self.top_evidence()],
        }


@dataclass
class MatchReport:
    source: str
    map_name: str
    tickrate: float
    players: list = field(default_factory=list)  # [PlayerReport]
    game: str = "cs2"
    capabilities: set = field(default_factory=set)
    skipped_signals: list = field(default_factory=list)

    def sorted_players(self) -> list:
        return sorted(self.players, key=lambda p: p.score, reverse=True)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "game": self.game,
            "map": self.map_name,
            "tickrate": self.tickrate,
            "capabilities": sorted(self.capabilities),
            "sinais_indisponiveis": self.skipped_signals,
            "players": [p.as_dict() for p in self.sorted_players()],
        }
