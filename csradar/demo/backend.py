"""Adaptador do demoparser2 para o modelo normalizado.

Nada aqui toca no processo do jogo: le apenas o arquivo .dem no disco.
Os nomes de coluna do demoparser2 mudam entre versoes, entao toda leitura
passa por _pick(), que aceita varios candidatos e falha de forma explicita.
"""

from __future__ import annotations

from pathlib import Path

from ..games.base import CAP_KILL_FLAGS
from ..models import DemoData, Kill, PlayerInfo, PlayerTick, Round, Shot

TICK_FIELDS = [
    "X", "Y", "Z", "pitch", "yaw", "health", "team_num", "is_alive",
]


class ParserUnavailable(RuntimeError):
    """demoparser2 nao instalado ou incompativel com este Python."""


def _import_parser():
    try:
        from demoparser2 import DemoParser  # type: ignore
    except Exception as exc:  # pragma: no cover - depende do ambiente
        raise ParserUnavailable(
            "demoparser2 nao esta disponivel neste interpretador.\n"
            "Instale com:  pip install demoparser2\n"
            "Se nao houver wheel para a sua versao de Python, use um venv com "
            "Python 3.11 ou 3.12:\n"
            "  py -3.12 -m venv .venv\n"
            "  .venv\\Scripts\\pip install -e .[all]\n"
            f"Erro original: {exc}"
        ) from exc
    return DemoParser


def _columns(df) -> list:
    return [str(c) for c in df.columns]


def _values(df, column) -> list:
    """Coluna -> lista Python, seja o DataFrame polars (atual) ou pandas.

    O demoparser2 migrou de pandas para polars: to_list existe em polars,
    tolist em pandas/numpy.
    """
    col = df[column]
    for attr in ("to_list", "tolist"):
        fn = getattr(col, attr, None)
        if callable(fn):
            return fn()
    return list(col)


def _pick(df, *candidates, required: bool = True):
    cols = set(_columns(df))
    for c in candidates:
        if c in cols:
            return c
    if required:
        raise KeyError(
            f"nenhuma das colunas {candidates} existe na demo; "
            f"disponiveis: {sorted(cols)[:40]}"
        )
    return None


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "yes")
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def load_demo(path, tickrate: float = 64.0, max_ticks: int | None = None) -> DemoData:
    """Le um .dem de CS2 e devolve DemoData normalizado."""
    DemoParser = _import_parser()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"demo nao encontrada: {p}")

    parser = DemoParser(str(p))
    demo = DemoData(source=p.name, tickrate=tickrate)
    demo.map_name = _read_map(parser)

    rounds = _read_rounds(parser)
    demo.rounds = rounds
    round_of = _round_lookup(rounds)

    _read_ticks(parser, demo, round_of, max_ticks)
    _read_kills(parser, demo, round_of)
    _read_shots(parser, demo, round_of)
    return demo


def _read_map(parser) -> str:
    fn = getattr(parser, "parse_header", None)
    if fn is None:
        return "desconhecido"
    try:
        header = fn()
    except Exception:
        return "desconhecido"
    if isinstance(header, dict):
        for key in ("map_name", "map"):
            if header.get(key):
                return str(header[key])
    return "desconhecido"


def _read_rounds(parser) -> list:
    try:
        df = parser.parse_event("round_start")
        starts = sorted(int(t) for t in _values(df, _pick(df, "tick"))) if len(df) else []
    except Exception:
        starts = []
    if not starts:
        return []
    rounds = []
    for i, start in enumerate(starts):
        end = starts[i + 1] - 1 if i + 1 < len(starts) else 10**9
        rounds.append(Round(number=i + 1, start_tick=start, end_tick=end))
    return rounds


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


def _read_ticks(parser, demo: DemoData, round_of, max_ticks: int | None) -> None:
    df = parser.parse_ticks(TICK_FIELDS)
    if df is None or not len(df):
        raise ValueError("a demo nao produziu nenhum tick (arquivo corrompido?)")

    c_tick = _pick(df, "tick")
    c_sid = _pick(df, "steamid", "steam_id")
    c_name = _pick(df, "name", "player_name", required=False)
    c_x, c_y, c_z = _pick(df, "X", "x"), _pick(df, "Y", "y"), _pick(df, "Z", "z")
    c_pitch = _pick(df, "pitch", "eye_pitch")
    c_yaw = _pick(df, "yaw", "eye_yaw")
    c_alive = _pick(df, "is_alive", required=False)
    c_team = _pick(df, "team_num", "team_number", required=False)
    c_hp = _pick(df, "health", required=False)

    wanted = {c_tick, c_sid, c_name, c_x, c_y, c_z, c_pitch, c_yaw,
              c_alive, c_team, c_hp}
    data = {c: _values(df, c) for c in wanted if c is not None}

    n = len(data[c_tick])
    cutoff = int(data[c_tick][0]) + max_ticks if max_ticks else None

    for i in range(n):
        tick = _to_int(data[c_tick][i], -1)
        if tick < 0:
            continue
        if cutoff is not None and tick > cutoff:
            break
        sid = _to_int(data[c_sid][i], 0)
        if not sid:
            continue
        x, y, z = data[c_x][i], data[c_y][i], data[c_z][i]
        pitch, yaw = data[c_pitch][i], data[c_yaw][i]
        if x is None or y is None or z is None or pitch is None or yaw is None:
            continue
        try:
            pt = PlayerTick(
                tick=tick,
                steamid=sid,
                x=float(x), y=float(y), z=float(z),
                pitch=float(pitch), yaw=float(yaw),
                is_alive=_to_bool(data[c_alive][i]) if c_alive else True,
                team=_to_int(data[c_team][i]) if c_team else 0,
                health=_to_int(data[c_hp][i], 100) if c_hp else 100,
                round_num=round_of(tick),
            )
        except (TypeError, ValueError):
            continue
        demo.ticks_by_player.setdefault(sid, []).append(pt)
        if sid not in demo.players:
            name = str(data[c_name][i]) if c_name else str(sid)
            demo.players[sid] = PlayerInfo(steamid=sid, name=name, team=pt.team)
        elif pt.team:
            demo.players[sid].team = pt.team

    for seq in demo.ticks_by_player.values():
        seq.sort(key=lambda t: t.tick)


def _read_kills(parser, demo: DemoData, round_of) -> None:
    try:
        df = parser.parse_event("player_death")
    except Exception:
        return
    if df is None or not len(df):
        return
    c_tick = _pick(df, "tick")
    c_att = _pick(df, "attacker_steamid", "attacker_steam_id", required=False)
    c_vic = _pick(df, "user_steamid", "victim_steamid", "user_steam_id", required=False)
    if not c_att or not c_vic:
        return
    c_hs = _pick(df, "headshot", required=False)
    c_wep = _pick(df, "weapon", required=False)
    c_pen = _pick(df, "penetrated", required=False)
    c_smoke = _pick(df, "thrusmoke", "through_smoke", required=False)
    c_nos = _pick(df, "noscope", required=False)
    if any((c_pen, c_smoke, c_nos)):
        demo.capabilities.add(CAP_KILL_FLAGS)

    wanted = {c_tick, c_att, c_vic, c_hs, c_wep, c_pen, c_smoke, c_nos}
    cols = {c: _values(df, c) for c in wanted if c}

    for i in range(len(cols[c_tick])):
        att, vic = _to_int(cols[c_att][i]), _to_int(cols[c_vic][i])
        if not att or not vic:
            continue
        tick = _to_int(cols[c_tick][i])
        demo.kills.append(
            Kill(
                tick=tick,
                attacker=att,
                victim=vic,
                weapon=str(cols[c_wep][i]) if c_wep else "",
                headshot=_to_bool(cols[c_hs][i], False) if c_hs else False,
                penetrated=_to_int(cols[c_pen][i]) if c_pen else 0,
                through_smoke=_to_bool(cols[c_smoke][i], False) if c_smoke else False,
                noscope=_to_bool(cols[c_nos][i], False) if c_nos else False,
                round_num=round_of(tick),
            )
        )
    demo.kills.sort(key=lambda k: k.tick)


def _read_shots(parser, demo: DemoData, round_of) -> None:
    try:
        df = parser.parse_event("weapon_fire")
    except Exception:
        return
    if df is None or not len(df):
        return
    c_tick = _pick(df, "tick")
    c_sid = _pick(df, "user_steamid", "user_steam_id", "steamid", required=False)
    if not c_sid:
        return
    c_wep = _pick(df, "weapon", required=False)
    cols = {c: _values(df, c) for c in {c_tick, c_sid, c_wep} if c}
    for i in range(len(cols[c_tick])):
        sid = _to_int(cols[c_sid][i])
        if not sid:
            continue
        tick = _to_int(cols[c_tick][i])
        demo.shots.append(
            Shot(tick=tick, steamid=sid,
                 weapon=str(cols[c_wep][i]) if c_wep else "",
                 round_num=round_of(tick))
        )
    demo.shots.sort(key=lambda s: s.tick)
