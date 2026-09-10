"""Formato de ingestao normalizado: a porta de entrada para qualquer jogo.

Escrever um parser binario para cada jogo do mundo nao e viavel, e para a
maioria deles nem existe replay legivel. O que e viavel e definir UM formato
de entrada e deixar que qualquer conversor externo (rrrocket para Rocket
League, r6-dissect para Siege, clarity para Dota, um parser proprio de CS:GO)
despeje os dados aqui.

O que voce alimenta define o que sai: sem pitch/yaw por tick, os sinais de
mira simplesmente nao rodam, e o relatorio diz isso.

Tres formatos aceitos:

1. JSON  - um objeto com as chaves meta/ticks/kills/shots/rounds
2. JSONL - um registro por linha, cada um com "type": tick|kill|shot|round|meta
3. CSV   - somente ticks, cabecalho com os nomes das colunas

Nomes de campo aceitam variantes (x/X/pos_x, yaw/view_yaw, steamid/player_id).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ..models import DemoData, Kill, PlayerInfo, PlayerTick, Round, Shot
from .base import (
    CAP_ANGLES, CAP_KILL_FLAGS, CAP_KILLS, CAP_POSITIONS, CAP_ROUNDS,
    CAP_SHOTS, CAP_TEAMS,
)

ALIASES = {
    "tick": ("tick", "frame", "t", "time_tick"),
    "steamid": ("steamid", "steam_id", "player_id", "playerid", "id", "uid"),
    "name": ("name", "player_name", "nick", "nickname"),
    "x": ("x", "X", "pos_x", "position_x"),
    "y": ("y", "Y", "pos_y", "position_y"),
    "z": ("z", "Z", "pos_z", "position_z"),
    "pitch": ("pitch", "view_pitch", "rot_pitch", "angle_pitch"),
    "yaw": ("yaw", "view_yaw", "rot_yaw", "angle_yaw"),
    "is_alive": ("is_alive", "alive", "living"),
    "team": ("team", "team_num", "team_number", "side"),
    "health": ("health", "hp"),
    "round": ("round", "round_num", "round_number", "total_rounds_played"),
    "attacker": ("attacker", "attacker_steamid", "killer", "attacker_id"),
    "victim": ("victim", "user_steamid", "victim_steamid", "killed", "victim_id"),
    "weapon": ("weapon", "weapon_name", "gun"),
    "headshot": ("headshot", "is_headshot", "hs"),
    "start_tick": ("start_tick", "start", "begin"),
    "end_tick": ("end_tick", "end", "finish"),
    "number": ("number", "round", "round_num", "n"),
}


class IngestError(ValueError):
    pass


def _get(record: dict, field: str, default=None):
    for key in ALIASES.get(field, (field,)):
        if key in record and record[key] is not None:
            return record[key]
    return default


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=0) -> int:
    """Inteiro exato.

    Nao passe por float: um SteamID de 64 bits nao cabe na mantissa de um
    double, e int(float(76561197960265729)) devolve ...728. Isso funde
    jogadores diferentes num so, silenciosamente.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(float(text))   # aceita "12.0" vindo de CSV/planilha
    except (TypeError, ValueError):
        return default


def _bool(value, default=True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes", "sim")
    return bool(value)


def _team(value) -> int:
    if value is None:
        return 0
    text = str(value).strip().upper()
    if text in ("CT", "COUNTER-TERRORIST", "BLUE", "3", "ATTACK"):
        return 3
    if text in ("T", "TERRORIST", "RED", "2", "DEFENSE"):
        return 2
    return _int(value, 0)


# --------------------------------------------------------------- construcao


def _add_tick(demo: DemoData, rec: dict) -> None:
    sid = _int(_get(rec, "steamid"))
    if not sid:
        return
    team = _team(_get(rec, "team"))
    pt = PlayerTick(
        tick=_int(_get(rec, "tick")),
        steamid=sid,
        x=_num(_get(rec, "x")), y=_num(_get(rec, "y")), z=_num(_get(rec, "z")),
        pitch=_num(_get(rec, "pitch")), yaw=_num(_get(rec, "yaw")),
        is_alive=_bool(_get(rec, "is_alive")),
        team=team,
        health=_int(_get(rec, "health"), 100),
        round_num=_int(_get(rec, "round")),
    )
    demo.ticks_by_player.setdefault(sid, []).append(pt)
    info = demo.players.get(sid)
    if info is None:
        demo.players[sid] = PlayerInfo(sid, str(_get(rec, "name", sid)), team)
    elif team:
        info.team = team

    if _get(rec, "pitch") is not None or _get(rec, "yaw") is not None:
        demo.capabilities.add(CAP_ANGLES)
    if _get(rec, "x") is not None:
        demo.capabilities.add(CAP_POSITIONS)
    if team:
        demo.capabilities.add(CAP_TEAMS)


def _add_kill(demo: DemoData, rec: dict) -> None:
    att, vic = _int(_get(rec, "attacker")), _int(_get(rec, "victim"))
    if not att or not vic:
        return
    flags = ("through_smoke", "penetrated", "noscope")
    demo.kills.append(Kill(
        tick=_int(_get(rec, "tick")),
        attacker=att, victim=vic,
        weapon=str(_get(rec, "weapon", "")),
        headshot=_bool(_get(rec, "headshot"), False),
        through_smoke=_bool(rec.get("through_smoke"), False),
        penetrated=_int(rec.get("penetrated"), 0),
        noscope=_bool(rec.get("noscope"), False),
        round_num=_int(_get(rec, "round")),
    ))
    demo.capabilities.add(CAP_KILLS)
    if any(rec.get(f) is not None for f in flags):
        demo.capabilities.add(CAP_KILL_FLAGS)


def _add_shot(demo: DemoData, rec: dict) -> None:
    sid = _int(_get(rec, "steamid")) or _int(_get(rec, "attacker"))
    if not sid:
        return
    demo.shots.append(Shot(
        tick=_int(_get(rec, "tick")), steamid=sid,
        weapon=str(_get(rec, "weapon", "")),
        round_num=_int(_get(rec, "round")),
    ))
    demo.capabilities.add(CAP_SHOTS)


def _add_round(demo: DemoData, rec: dict) -> None:
    demo.rounds.append(Round(
        number=_int(_get(rec, "number"), len(demo.rounds) + 1),
        start_tick=_int(_get(rec, "start_tick")),
        end_tick=_int(_get(rec, "end_tick"), 10**9),
    ))
    demo.capabilities.add(CAP_ROUNDS)


def _apply_meta(demo: DemoData, meta: dict) -> None:
    demo.map_name = str(meta.get("map") or meta.get("map_name") or demo.map_name)
    if meta.get("tickrate"):
        demo.tickrate = _num(meta["tickrate"], demo.tickrate)
    if meta.get("game"):
        demo.game = str(meta["game"])
    for sid, name in (meta.get("players") or {}).items():
        s = _int(sid)
        if s and s not in demo.players:
            demo.players[s] = PlayerInfo(s, str(name), 0)


_DISPATCH = {
    "tick": _add_tick, "ticks": _add_tick,
    "kill": _add_kill, "kills": _add_kill, "death": _add_kill,
    "shot": _add_shot, "shots": _add_shot, "weapon_fire": _add_shot,
    "round": _add_round, "rounds": _add_round,
}


# ------------------------------------------------------------------- leitura


def load_ingest(path, tickrate: float = 64.0, game: str = "") -> DemoData:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"arquivo nao encontrado: {p}")
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return _load_csv(p, tickrate, game)
    if suffix in (".jsonl", ".ndjson"):
        return _load_jsonl(p, tickrate, game)
    if suffix == ".json":
        return _load_json(p, tickrate, game)
    raise IngestError(
        f"extensao {suffix or '(nenhuma)'} nao reconhecida. "
        "Use .json, .jsonl ou .csv - veja `csradar ingest --help`."
    )


def _new_demo(p: Path, tickrate: float, game: str) -> DemoData:
    demo = DemoData(source=p.name, tickrate=tickrate)
    demo.game = game or "desconhecido"
    demo.capabilities = set()
    return demo


def _load_json(p: Path, tickrate: float, game: str) -> DemoData:
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise IngestError("o JSON precisa ser um objeto com as chaves "
                          "meta/ticks/kills/shots/rounds")
    demo = _new_demo(p, tickrate, game)
    _apply_meta(demo, raw.get("meta") or {})
    for key, fn in (("ticks", _add_tick), ("kills", _add_kill),
                    ("shots", _add_shot), ("rounds", _add_round)):
        for rec in raw.get(key) or []:
            fn(demo, rec)
    return _finish(demo)


def _load_jsonl(p: Path, tickrate: float, game: str) -> DemoData:
    demo = _new_demo(p, tickrate, game)
    with open(p, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise IngestError(f"linha {lineno} nao e JSON valido: {exc}") from exc
            kind = str(rec.get("type") or rec.get("kind") or "tick").lower()
            if kind == "meta":
                _apply_meta(demo, rec)
                continue
            fn = _DISPATCH.get(kind)
            if fn is None:
                raise IngestError(
                    f"linha {lineno}: type '{kind}' desconhecido "
                    f"(use tick, kill, shot, round ou meta)"
                )
            fn(demo, rec)
    return _finish(demo)


def _load_csv(p: Path, tickrate: float, game: str) -> DemoData:
    demo = _new_demo(p, tickrate, game)
    with open(p, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise IngestError("CSV sem cabecalho")
        for rec in reader:
            _add_tick(demo, rec)
    return _finish(demo)


def _finish(demo: DemoData) -> DemoData:
    for seq in demo.ticks_by_player.values():
        seq.sort(key=lambda t: t.tick)
    demo.kills.sort(key=lambda k: k.tick)
    demo.shots.sort(key=lambda s: s.tick)
    demo.rounds.sort(key=lambda r: r.start_tick)
    if not demo.ticks_by_player and not demo.kills:
        raise IngestError("nenhum tick nem kill encontrado no arquivo")
    # times sao necessarios para saber quem e inimigo de quem
    if len({p.team for p in demo.players.values()} - {0}) < 2:
        demo.capabilities.discard(CAP_TEAMS)
    return demo


SPEC = """Formato de ingestao do cs-cheat-radar
=====================================

JSONL (um registro por linha):

  {"type":"meta","game":"rocket_league","map":"DFH Stadium","tickrate":30}
  {"type":"round","number":1,"start_tick":0,"end_tick":1799}
  {"type":"tick","tick":100,"steamid":7656119...,"name":"nick","team":2,
   "x":10.5,"y":-3.0,"z":17.0,"pitch":-2.1,"yaw":143.7,"is_alive":true}
  {"type":"shot","tick":118,"steamid":7656119...,"weapon":"ak47"}
  {"type":"kill","tick":121,"attacker":7656119...,"victim":7656119...,
   "headshot":true,"weapon":"ak47","round":1}

JSON (mesmos registros agrupados):

  {"meta":{...},"ticks":[...],"kills":[...],"shots":[...],"rounds":[...]}

CSV (somente ticks; cabecalho obrigatorio):

  tick,steamid,name,team,x,y,z,pitch,yaw,is_alive

O que voce fornece determina quais sinais rodam:

  pitch/yaw por tick ....... snap, jitter, reacao, tracking, prefire
  so posicoes por tick ..... tracking parcial, sem sinais de mira
  so kills ................. apenas ritmo de kills (triagem fraca)

Convencao de angulo: a mesma da engine Source - yaw 0 em +X, crescendo no
sentido anti-horario; pitch POSITIVO olhando para baixo. Se o seu jogo usa
outra convencao, converta antes de alimentar.
"""
