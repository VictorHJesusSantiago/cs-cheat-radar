"""Despacho: dado um arquivo, descobrir quem sabe ler."""

from __future__ import annotations

from pathlib import Path

from .base import (
    CAP_ANGLES, CAP_KILLS, CAP_POSITIONS, CAP_ROUNDS, CAP_SHOTS, CAP_TEAMS,
    AdapterInfo, GameProfile, InstalledGame, Support,
)
from .ingest import SPEC as INGEST_SPEC, IngestError, load_ingest
from .registry import GAMES, by_key, identify, supported
from .osu_replay import OsuReplayError, read_osr
from .srcds_log import load_log

ADAPTERS = {
    "cs2_demo": AdapterInfo(
        name="cs2_demo",
        description="demos .dem de CS2 via demoparser2",
        caps={CAP_ANGLES, CAP_POSITIONS, CAP_SHOTS, CAP_KILLS, CAP_ROUNDS,
              CAP_TEAMS},
        extensions=(".dem",),
        requires=("demoparser2",),
    ),
    "srcds_log": AdapterInfo(
        name="srcds_log",
        description="log de servidor dedicado Source (texto)",
        caps={CAP_KILLS, CAP_ROUNDS, CAP_TEAMS},
        extensions=(".log", ".txt"),
    ),
    "osu_replay": AdapterInfo(
        name="osu_replay",
        description="replays .osr do osu! (nativo, sem dependencia externa)",
        caps={"cursor", "keys", "positions"},
        extensions=(".osr",),
    ),
    "ingest": AdapterInfo(
        name="ingest",
        description="formato normalizado JSON/JSONL/CSV (qualquer jogo)",
        caps=set(),  # depende do que o arquivo trouxer
        extensions=(".json", ".jsonl", ".ndjson", ".csv"),
    ),
}


def adapter_for(path) -> str | None:
    """Escolhe o adaptador pela extensao do arquivo."""
    name = str(path).lower()
    for key in ("cs2_demo", "osu_replay", "srcds_log", "ingest"):
        if name.endswith(ADAPTERS[key].extensions):
            return key
    return None


def load_any(path, tickrate: float = 64.0, game: str = "", adapter: str = ""):
    """Le qualquer fonte suportada e devolve DemoData com capacidades corretas.

    O adaptador pode ser forcado; por padrao vem da extensao.
    """
    p = Path(path)
    kind = adapter or adapter_for(p)
    if kind is None:
        raise ValueError(
            f"nao sei ler {p.name}. Extensoes suportadas: "
            f".dem (CS2), .osr (osu!), .log/.txt (servidor Source), "
            f".json/.jsonl/.csv "
            f"(formato de ingestao). Veja `csradar games`."
        )

    if kind == "cs2_demo":
        from ..demo.backend import load_demo

        demo = load_demo(p, tickrate=tickrate)
        demo.game = game or "cs2"
        return demo

    if kind == "osu_replay":
        demo = read_osr(p, sample_rate=tickrate if tickrate != 64.0 else 60.0)
        demo.game = game or "osu"
        return demo

    if kind == "srcds_log":
        demo = load_log(p, tickrate=tickrate)
        demo.game = game or "srcds"
        return demo

    return load_ingest(p, tickrate=tickrate, game=game)


__all__ = [
    "ADAPTERS", "GAMES", "INGEST_SPEC", "IngestError", "Support",
    "AdapterInfo", "GameProfile", "InstalledGame",
    "adapter_for", "by_key", "identify", "load_any", "load_ingest",
    "load_log", "read_osr", "OsuReplayError", "supported",
]
