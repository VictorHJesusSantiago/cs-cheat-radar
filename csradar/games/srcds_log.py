"""Log de servidor dedicado Source (CS2, CS:GO, CS:S, TF2, L4D2, GMod).

Um servidor que voce controla escreve tudo em texto puro, com uma linha por
evento. Isso e uma fonte legitima e nao exige parser binario nenhum:

    L 01/15/2024 - 20:11:33: "Nick<12><STEAM_1:0:123><CT>" [100 200 30] killed
    "Outro<13><STEAM_1:1:99><TERRORIST>" [400 500 30] with "ak47" (headshot)

Limite serio e incontornavel: o carimbo de tempo tem resolucao de UM SEGUNDO.
Nao da para medir tempo de reacao nem flick com isso. Sobra o ritmo grosso -
tres jogadores mortos dentro do mesmo segundo, repetidamente - e mesmo isso e
triagem fraca, nao evidencia.

As posicoes entre colchetes so aparecem com `mp_logdetail 3` (ou equivalente).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..models import DemoData, Kill, PlayerInfo, PlayerTick, Round
from ..steam import to_steam64
from .base import CAP_KILLS, CAP_ROUNDS, CAP_TEAMS

TIMESTAMP = r"L (?P<date>\d{2}/\d{2}/\d{4}) - (?P<time>\d{2}:\d{2}:\d{2})"
PLAYER = r'"(?P<{p}name>.*?)<\d+><(?P<{p}id>[^>]*)><(?P<{p}team>[^>]*)>"'

KILL_RE = re.compile(
    TIMESTAMP + r":\s*" + PLAYER.format(p="a")
    + r"(?:\s*\[(?P<apos>-?\d+ -?\d+ -?\d+)\])?"
    + r"\s+killed\s+" + PLAYER.format(p="v")
    + r"(?:\s*\[(?P<vpos>-?\d+ -?\d+ -?\d+)\])?"
    + r'\s+with\s+"(?P<weapon>[^"]*)"(?P<flags>.*)$'
)
ROUND_RE = re.compile(TIMESTAMP + r':\s*World triggered "Round_Start"')
TEAM_MAP = {"CT": 3, "TERRORIST": 2, "T": 2, "RED": 2, "BLUE": 3}


def _parse_time(date: str, time: str) -> float:
    return datetime.strptime(f"{date} {time}", "%m/%d/%Y %H:%M:%S").timestamp()


def _parse_pos(text) -> tuple:
    if not text:
        return (0.0, 0.0, 0.0)
    parts = text.split()
    if len(parts) != 3:
        return (0.0, 0.0, 0.0)
    return tuple(float(p) for p in parts)


def load_log(path, tickrate: float = 64.0) -> DemoData:
    """Le um log de servidor Source e devolve DemoData somente de eventos."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"log nao encontrado: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    return parse_log_text(text, source=p.name, tickrate=tickrate)


def parse_log_text(text: str, source: str = "srcds.log",
                   tickrate: float = 64.0) -> DemoData:
    demo = DemoData(source=source, map_name="desconhecido", tickrate=tickrate)
    demo.capabilities = {CAP_KILLS, CAP_ROUNDS, CAP_TEAMS}

    events = []       # (epoch, kind, payload)
    for line in text.splitlines():
        line = line.strip()
        m = KILL_RE.search(line)
        if m:
            events.append((_parse_time(m.group("date"), m.group("time")), "kill", m))
            continue
        m = ROUND_RE.search(line)
        if m:
            events.append((_parse_time(m.group("date"), m.group("time")), "round", m))

    if not events:
        return demo

    base = min(e[0] for e in events)

    def tick_of(epoch: float) -> int:
        return int((epoch - base) * tickrate)

    round_starts = []
    for epoch, kind, m in events:
        tick = tick_of(epoch)
        if kind == "round":
            round_starts.append(tick)
            continue

        att = _ensure_player(demo, m.group("aname"), m.group("aid"), m.group("ateam"))
        vic = _ensure_player(demo, m.group("vname"), m.group("vid"), m.group("vteam"))
        if not att or not vic:
            continue
        flags = m.group("flags") or ""
        kill = Kill(
            tick=tick,
            attacker=att,
            victim=vic,
            weapon=m.group("weapon") or "",
            headshot="headshot" in flags.lower(),
            round_num=0,
        )
        demo.kills.append(kill)
        # posicao da kill vira um unico PlayerTick, util para distancia
        _add_position(demo, att, tick, m.group("apos"))
        _add_position(demo, vic, tick, m.group("vpos"))

    for i, start in enumerate(sorted(round_starts)):
        end = sorted(round_starts)[i + 1] - 1 if i + 1 < len(round_starts) else 10**9
        demo.rounds.append(Round(number=i + 1, start_tick=start, end_tick=end))

    lookup = _round_lookup(demo.rounds)
    for kill in demo.kills:
        kill.round_num = lookup(kill.tick)
    demo.kills.sort(key=lambda k: k.tick)
    for seq in demo.ticks_by_player.values():
        seq.sort(key=lambda t: t.tick)
    return demo


def _ensure_player(demo: DemoData, name, raw_id, team) -> int | None:
    sid = to_steam64(raw_id or "")
    if not sid:
        return None
    tnum = TEAM_MAP.get((team or "").strip().upper(), 0)
    info = demo.players.get(sid)
    if info is None:
        demo.players[sid] = PlayerInfo(steamid=sid, name=(name or "").strip(),
                                       team=tnum)
        demo.ticks_by_player.setdefault(sid, [])
    elif tnum:
        info.team = tnum
        if name:
            info.name = name.strip()
    return sid


def _add_position(demo: DemoData, sid: int, tick: int, raw) -> None:
    if not raw:
        return
    x, y, z = _parse_pos(raw)
    demo.ticks_by_player.setdefault(sid, []).append(
        PlayerTick(tick=tick, steamid=sid, x=x, y=y, z=z, pitch=0.0, yaw=0.0,
                   team=demo.players[sid].team)
    )


def _round_lookup(rounds: list):
    if not rounds:
        return lambda tick: 0
    bounds = [(r.start_tick, r.number) for r in rounds]

    def lookup(tick: int) -> int:
        num = 0
        for start, n in bounds:
            if tick >= start:
                num = n
            else:
                break
        return num

    return lookup
