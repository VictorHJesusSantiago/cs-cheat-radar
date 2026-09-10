"""Conversores: saida de ferramenta externa -> formato de ingestao.

Escrever um parser binario por jogo nao escala. O que escala e converter a
saida das ferramentas que ja existem e sao mantidas por quem entende do
formato. Aqui ficam tres caminhos:

  pubg    telemetria oficial da PUBG API (JSON publico, cobre todos os
          jogadores da partida)
  r6      saida do r6-dissect para replays .rec do Rainbow Six Siege
  map     mapeador generico guiado por um arquivo de regras - serve para
          QUALQUER ferramenta que produza JSON, sem precisar de codigo novo

Todo conversor devolve um DemoData com as capacidades que o dado realmente
sustenta. Nenhum deles inventa angulo de visao onde nao ha.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models import DemoData, Kill, PlayerInfo, PlayerTick, Round, Shot
from .base import (
    CAP_ANGLES, CAP_KILLS, CAP_POSITIONS, CAP_ROUNDS, CAP_SHOTS, CAP_TEAMS,
)
from .ingest import IngestError, _bool, _int, _num, _team


class ConversionError(ValueError):
    pass


def _load_json(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"arquivo nao encontrado: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{p.name} nao e JSON valido: {exc}") from exc


def _stable_id(text: str) -> int:
    """ID numerico estavel para jogos sem SteamID.

    O resto do programa indexa jogador por inteiro. Para PUBG (accountId em
    texto) ou Siege (UUID), derivamos um inteiro deterministico do proprio
    identificador: mesmo jogador, mesmo numero, entre partidas e execucoes.
    Fica fora da faixa de SteamID64 para nao se confundir com um.
    """
    import hashlib

    digest = hashlib.sha1(str(text).encode("utf-8")).digest()
    return int.from_bytes(digest[:7], "big") | (1 << 55)


# ------------------------------------------------------------------- PUBG


PUBG_KILL_TYPES = ("LogPlayerKillV2", "LogPlayerKill")


def from_pubg_telemetry(path, tickrate: float = 10.0) -> DemoData:
    """Telemetria oficial da PUBG API.

    O que existe: kills, dano, disparos e posicao periodica de cada jogador.
    O que NAO existe: direcao da mira. Por isso o resultado declara apenas
    kills/posicoes/times - os detectores de mira nao vao rodar, e o relatorio
    dira isso.

    A amostragem de LogPlayerPosition e da ordem de 10 segundos, o que torna
    as posicoes uteis para contexto e inuteis para qualquer coisa continua.
    """
    raw = _load_json(path)
    if not isinstance(raw, list):
        raise ConversionError(
            "a telemetria da PUBG e uma lista de eventos; recebi "
            f"{type(raw).__name__}"
        )

    demo = DemoData(source=Path(path).name, map_name="desconhecido",
                    game="pubg", tickrate=tickrate)
    demo.capabilities = set()

    base_time = None
    teams: dict = {}

    def tick_of(stamp) -> int:
        nonlocal base_time
        moment = _pubg_time(stamp)
        if moment is None:
            return 0
        if base_time is None:
            base_time = moment
        return int((moment - base_time) * tickrate)

    def note_player(char) -> int | None:
        if not isinstance(char, dict):
            return None
        account = char.get("accountId") or char.get("name")
        if not account:
            return None
        sid = _stable_id(account)
        team_id = _int(char.get("teamId"), 0)
        info = demo.players.get(sid)
        if info is None:
            demo.players[sid] = PlayerInfo(sid, str(char.get("name", account)),
                                           team_id)
            demo.ticks_by_player.setdefault(sid, [])
        elif team_id:
            info.team = team_id
        if team_id:
            teams[sid] = team_id
        return sid

    for event in raw:
        if not isinstance(event, dict):
            continue
        etype = event.get("_T", "")
        stamp = event.get("_D")

        if etype == "LogMatchStart":
            demo.map_name = str(
                (event.get("mapName") or event.get("map") or "desconhecido")
            )
            for entry in event.get("characters") or []:
                note_player(entry.get("character") if isinstance(entry, dict)
                            else entry)
            demo.rounds.append(Round(number=1, start_tick=0, end_tick=10**9))
            demo.capabilities.add(CAP_ROUNDS)
            continue

        if etype in PUBG_KILL_TYPES:
            victim = note_player(event.get("victim"))
            killer = note_player(
                (event.get("killer")
                 or (event.get("finisher") if isinstance(event.get("finisher"), dict)
                     else None))
            )
            if victim is None or killer is None or killer == victim:
                continue
            demo.kills.append(Kill(
                tick=tick_of(stamp), attacker=killer, victim=victim,
                weapon=str(_pubg_weapon(event)),
                headshot=str(event.get("damageReason", "")).lower() == "headshot",
                round_num=1,
            ))
            demo.capabilities.add(CAP_KILLS)
            continue

        if etype == "LogPlayerPosition":
            sid = note_player(event.get("character"))
            loc = (event.get("character") or {}).get("location") or {}
            if sid is None or not loc:
                continue
            demo.ticks_by_player.setdefault(sid, []).append(PlayerTick(
                tick=tick_of(stamp), steamid=sid,
                x=_num(loc.get("x")), y=_num(loc.get("y")), z=_num(loc.get("z")),
                pitch=0.0, yaw=0.0, team=teams.get(sid, 0),
                health=_int((event.get("character") or {}).get("health"), 100),
                round_num=1,
            ))
            demo.capabilities.add(CAP_POSITIONS)
            continue

        if etype == "LogPlayerAttack":
            sid = note_player(event.get("attacker"))
            if sid is None:
                continue
            demo.shots.append(Shot(tick=tick_of(stamp), steamid=sid,
                                   weapon=str(_pubg_weapon(event)), round_num=1))
            demo.capabilities.add(CAP_SHOTS)

    if len({t for t in teams.values() if t}) >= 2:
        demo.capabilities.add(CAP_TEAMS)
    if not demo.kills and not demo.ticks_by_player:
        raise ConversionError("nenhum evento reconhecido na telemetria")

    _finish(demo)
    return demo


def _pubg_time(stamp) -> float | None:
    if not stamp:
        return None
    text = str(stamp).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _pubg_weapon(event: dict) -> str:
    weapon = event.get("damageCauserName") or event.get("weapon")
    if isinstance(weapon, dict):
        return weapon.get("itemId") or ""
    return weapon or ""


# --------------------------------------------------------------- r6-dissect


def from_r6_dissect(path, tickrate: float = 1.0) -> DemoData:
    """Saida JSON do r6-dissect (replays .rec do Rainbow Six Siege).

    Traz rounds, jogadores, times e kills com o instante em segundos de
    round. Nao traz posicao nem mira: o resultado e nivel de eventos.
    """
    raw = _load_json(path)
    rounds_raw = raw if isinstance(raw, list) else [raw]

    demo = DemoData(source=Path(path).name, game="r6siege", tickrate=tickrate)
    demo.capabilities = {CAP_KILLS, CAP_ROUNDS}
    offset = 0
    seen_teams = set()

    for index, rnd in enumerate(rounds_raw, start=1):
        if not isinstance(rnd, dict):
            continue
        header = rnd.get("header") or rnd
        demo.map_name = str(
            (header.get("map") or {}).get("name")
            if isinstance(header.get("map"), dict)
            else header.get("map") or demo.map_name
        )
        number = _int(header.get("roundNumber"), index)
        start = offset
        length = 0

        for player in rnd.get("players") or []:
            sid = _r6_player_id(player)
            if sid is None:
                continue
            team = _int(player.get("teamIndex"), 0) + 2
            seen_teams.add(team)
            info = demo.players.get(sid)
            if info is None:
                demo.players[sid] = PlayerInfo(
                    sid, str(player.get("username") or sid), team)
                demo.ticks_by_player.setdefault(sid, [])
            else:
                info.team = team

        by_name = {info.name: sid for sid, info in demo.players.items()}
        for event in rnd.get("matchFeedback") or []:
            if not isinstance(event, dict):
                continue
            # o r6-dissect ja escreveu "type" como texto e como objeto
            # {"name": "Kill"}; aceitamos as duas formas
            kind = event.get("type")
            if isinstance(kind, dict):
                kind = kind.get("name", "")
            if str(kind).strip().lower() != "kill":
                continue
            attacker = by_name.get(str(event.get("username", "")))
            victim = by_name.get(str(event.get("target", "")))
            if attacker is None or victim is None or attacker == victim:
                continue
            seconds = _r6_seconds(event.get("timeInSeconds"), event.get("time"))
            length = max(length, seconds)
            demo.kills.append(Kill(
                tick=int((start + seconds) * tickrate),
                attacker=attacker, victim=victim,
                weapon=str(event.get("weapon", "")),
                headshot=_bool(event.get("headshot"), False),
                round_num=number,
            ))

        demo.rounds.append(Round(number=number,
                                 start_tick=int(start * tickrate),
                                 end_tick=int((start + max(length, 1)) * tickrate)))
        offset = start + max(length, 1) + 1

    if len(seen_teams) >= 2:
        demo.capabilities.add(CAP_TEAMS)
    if not demo.kills:
        raise ConversionError(
            "nenhuma kill reconhecida - confira se o JSON veio do r6-dissect"
        )
    _finish(demo)
    return demo


def _r6_player_id(player: dict):
    if not isinstance(player, dict):
        return None
    ident = (player.get("profileID") or player.get("id")
             or player.get("username"))
    return _stable_id(ident) if ident else None


def _r6_seconds(value, fallback) -> float:
    if value is not None:
        return _num(value)
    # o r6-dissect tambem escreve "2:31" como texto de relogio de round
    if isinstance(fallback, str) and ":" in fallback:
        minutes, _, secs = fallback.partition(":")
        return _num(minutes) * 60 + _num(secs)
    return 0.0


# ------------------------------------------------------- mapeador generico


MAP_SPEC_EXAMPLE = {
    "meta": {"game": "meu_jogo", "tickrate": 60},
    "ticks": {
        "path": "frames[].players[]",
        "fields": {
            "tick": "$parent.frame_number",
            "steamid": "player_id",
            "name": "display_name",
            "team": "team_index",
            "x": "position.x", "y": "position.y", "z": "position.z",
            "pitch": "rotation.pitch", "yaw": "rotation.yaw",
        },
    },
    "kills": {
        "path": "events.kills[]",
        "fields": {"tick": "frame", "attacker": "killer_id",
                   "victim": "victim_id", "headshot": "is_headshot"},
    },
}


def from_mapping(data_path, spec_path, tickrate: float = 64.0) -> DemoData:
    """Converte QUALQUER JSON guiado por um arquivo de regras.

    Existe para o caso, muito comum, de a ferramenta do seu jogo produzir um
    JSON perfeitamente utilizavel com nomes de campo diferentes dos nossos.
    Em vez de escrever um conversor novo, voce escreve um mapa de dez linhas.

    Sintaxe de caminho: `a.b.c` desce em objetos, `a[]` itera uma lista, e
    `$parent.campo` alcanca o objeto que continha a lista - necessario porque
    o numero do frame quase sempre esta um nivel acima do jogador.
    """
    raw = _load_json(data_path)
    spec = _load_json(spec_path)
    if not isinstance(spec, dict):
        raise ConversionError("o arquivo de mapeamento precisa ser um objeto")

    demo = DemoData(source=Path(data_path).name, tickrate=tickrate)
    demo.capabilities = set()
    meta = spec.get("meta") or {}
    demo.game = str(meta.get("game", "desconhecido"))
    demo.map_name = str(meta.get("map", "desconhecido"))
    if meta.get("tickrate"):
        demo.tickrate = _num(meta["tickrate"], tickrate)

    from .ingest import _add_kill, _add_round, _add_shot, _add_tick

    handlers = (("ticks", _add_tick), ("kills", _add_kill),
                ("shots", _add_shot), ("rounds", _add_round))
    for key, adder in handlers:
        rule = spec.get(key)
        if not rule:
            continue
        path = rule.get("path", "")
        fields = rule.get("fields") or {}
        if not fields:
            raise ConversionError(f"a regra '{key}' nao declara 'fields'")
        for node, parent in _walk(raw, path):
            record = {}
            for target, source in fields.items():
                value = _resolve(node, parent, source)
                if value is not None:
                    record[target] = value
            if record:
                adder(demo, record)

    if not demo.ticks_by_player and not demo.kills:
        raise ConversionError(
            "o mapeamento nao produziu nenhum tick nem kill; confira os "
            "caminhos em 'path' e 'fields'"
        )
    _finish(demo)
    return demo


def _walk(node, path: str):
    """Percorre `path` e devolve (no, pai) para cada folha alcancada."""
    if not path:
        yield node, None
        return
    parts = [p for p in path.split(".") if p]
    stack = [(node, None)]
    for part in parts:
        is_list = part.endswith("[]")
        key = part[:-2] if is_list else part
        nxt = []
        for current, _parent in stack:
            if not isinstance(current, dict):
                continue
            value = current.get(key) if key else current
            if value is None:
                continue
            if is_list:
                if isinstance(value, list):
                    nxt.extend((item, current) for item in value)
            else:
                nxt.append((value, current))
        stack = nxt
    yield from stack


def _resolve(node, parent, source):
    if not isinstance(source, str):
        return source
    if source.startswith("$parent."):
        return _dig(parent, source[len("$parent."):])
    if source.startswith("$const:"):
        return source[len("$const:"):]
    return _dig(node, source)


def _dig(node, path: str):
    current = node
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


# ------------------------------------------------------------------ saida


def _finish(demo: DemoData) -> None:
    for seq in demo.ticks_by_player.values():
        seq.sort(key=lambda t: t.tick)
    demo.kills.sort(key=lambda k: k.tick)
    demo.shots.sort(key=lambda s: s.tick)
    demo.rounds.sort(key=lambda r: r.start_tick)
    if len({p.team for p in demo.players.values()} - {0}) < 2:
        demo.capabilities.discard(CAP_TEAMS)


def write_ingest(demo: DemoData, out_path) -> Path:
    """Grava um DemoData no formato de ingestao (JSONL)."""
    out = Path(out_path)
    lines = [json.dumps({
        "type": "meta", "game": demo.game, "map": demo.map_name,
        "tickrate": demo.tickrate,
    })]
    for rnd in demo.rounds:
        lines.append(json.dumps({
            "type": "round", "number": rnd.number,
            "start_tick": rnd.start_tick, "end_tick": rnd.end_tick,
        }))
    for seq in demo.ticks_by_player.values():
        for t in seq:
            lines.append(json.dumps({
                "type": "tick", "tick": t.tick, "steamid": t.steamid,
                "name": demo.name_of(t.steamid), "team": t.team,
                "x": t.x, "y": t.y, "z": t.z,
                "pitch": t.pitch, "yaw": t.yaw,
                "is_alive": t.is_alive, "health": t.health,
                "round": t.round_num,
            }))
    for s in demo.shots:
        lines.append(json.dumps({
            "type": "shot", "tick": s.tick, "steamid": s.steamid,
            "weapon": s.weapon, "round": s.round_num,
        }))
    for k in demo.kills:
        lines.append(json.dumps({
            "type": "kill", "tick": k.tick, "attacker": k.attacker,
            "victim": k.victim, "weapon": k.weapon,
            "headshot": k.headshot, "round": k.round_num,
        }))
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


CONVERTERS = {
    "pubg": from_pubg_telemetry,
    "r6": from_r6_dissect,
}
