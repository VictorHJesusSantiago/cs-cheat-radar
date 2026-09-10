"""Proxy de wallhack: mira acompanhando inimigo sem interacao, e pre-aim.

Sem nav mesh nao ha teste exato de linha de visao, entao o substituto e
deliberadamente conservador. Um episodio so conta quando as quatro condicoes
valem ao mesmo tempo:

- a mira fica dentro de lock_deg do inimigo por min_lock_ticks seguidos;
- a distancia e grande (linha de visao direta e menos provavel);
- o jogador nao atirou nem matou/morreu perto no tempo (duelo legitimo quase
  sempre termina em disparo);
- o angulo ate o alvo MUDOU durante o episodio, ou seja, a mira teve de
  seguir. Sem isso, contariamos crosshair parado que o inimigo atravessou -
  que e exatamente o que um jogador bom faz de proposito.

O pre-aim mede outra coisa: meio segundo e um segundo antes da kill, a mira
ja estava colada no alvo.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import angular_error, desired_angles, distance, ramp
from .geometry import _spherical_delta  # noqa: F401  (uso interno deliberado)


class _Run:
    __slots__ = ("start", "last", "travel", "prev_angles", "counted")

    def __init__(self, tick: int, angles: tuple):
        self.start = tick
        self.last = tick
        self.travel = 0.0
        self.prev_angles = angles
        self.counted = False

    def extend(self, tick: int, angles: tuple) -> None:
        self.travel += _spherical_delta(*self.prev_angles, *angles)
        self.prev_angles = angles
        self.last = tick

    @property
    def span(self) -> int:
        return self.last - self.start + 1


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    index = index if index is not None else demo.index_ticks()
    tcfg = cfg.tracking
    my_seq = demo.ticks_by_player.get(steamid) or []
    my_team = demo.team_of(steamid)

    enemies = [
        sid
        for sid in demo.steamids()
        if sid != steamid and demo.team_of(sid) != my_team and demo.team_of(sid) != 0
    ]
    if not enemies or not my_seq:
        return {"tracking": _empty_tracking(len(my_seq)),
                "prefire": _empty_prefire()}

    action_ticks = sorted(
        [s.tick for s in demo.shots if s.steamid == steamid]
        + [k.tick for k in demo.kills if k.attacker == steamid or k.victim == steamid]
    )

    episodes = 0
    locked_ticks = 0
    alive_ticks = 0
    evidence: list = []
    runs: dict = {}

    for pt in my_seq:
        if not pt.is_alive:
            runs.clear()
            continue
        alive_ticks += 1
        for sid in enemies:
            et = index.get(sid, {}).get(pt.tick)
            if (
                et is None
                or not et.is_alive
                or distance(pt, et) < tcfg.min_distance
                or angular_error(pt, et) > tcfg.lock_deg
            ):
                runs.pop(sid, None)
                continue

            angles = desired_angles(pt, et)
            run = runs.get(sid)
            if run is None or pt.tick - run.last > 2:
                runs[sid] = _Run(pt.tick, angles)
                continue

            run.extend(pt.tick, angles)
            if run.counted:
                locked_ticks += 1
                continue
            if run.span < tcfg.min_lock_ticks:
                continue
            if run.travel < tcfg.min_target_travel_deg:
                continue
            if _near_action(action_ticks, run.start, tcfg.ignore_window_ticks):
                runs.pop(sid, None)
                continue

            run.counted = True
            episodes += 1
            locked_ticks += run.span
            if len(evidence) < 8:
                evidence.append(
                    Evidence(
                        kind="wall_track",
                        round_num=pt.round_num,
                        tick=run.start,
                        target=demo.name_of(sid),
                        detail=(
                            f"mira seguiu {run.travel:.0f} deg de deslocamento do alvo "
                            f"por {cfg.ticks_to_ms(run.span):.0f} ms a "
                            f"{distance(pt, et):.0f} unidades, sem disparo"
                        ),
                    )
                )

    rounds = max(1, len(demo.rounds))
    per_round = episodes / rounds
    lock_ratio = locked_ticks / alive_ticks if alive_ticks else 0.0
    value = max(ramp(per_round, 0.10, 0.90), 0.8 * ramp(lock_ratio, 0.003, 0.04))

    return {
        "tracking": SignalResult(
            name="tracking",
            value=value,
            samples=alive_ticks,
            confident=alive_ticks >= 2000,
            raw={
                "episodios": episodes,
                "episodios_por_round": round(per_round, 2),
                "ticks_travados": locked_ticks,
                "fracao_do_tempo_vivo": round(lock_ratio, 4),
            },
            evidence=evidence,
        ),
        "prefire": _prefire(demo, steamid, cfg, index),
    }


def _prefire(demo, steamid: int, cfg, index) -> SignalResult:
    """A mira ja estava no alvo bem antes do duelo comecar.

    Ancorar isso na entrada do alvo no campo de visao nao funciona: quem usa
    wallhack mantem a mira no inimigo o tempo todo, entao nunca existe uma
    "entrada" para medir. Olhamos para tras a partir da kill em dois pontos
    fixos (0,5 s e 1 s antes) e perguntamos se a mira ja estava colada.
    """
    tight = cfg.tracking.lock_deg
    offsets = (int(cfg.ms_to_ticks(500)), int(cfg.ms_to_ticks(1000)))
    my_ticks = index.get(steamid, {})
    hits = 0
    total = 0
    evidence: list = []

    for kill in demo.kills:
        if kill.attacker != steamid or kill.victim == steamid:
            continue
        victim_ticks = index.get(kill.victim, {})
        frames = []
        for off in offsets:
            me = my_ticks.get(kill.tick - off)
            him = victim_ticks.get(kill.tick - off)
            if me is None or him is None or not me.is_alive or not him.is_alive:
                frames = []
                break
            frames.append((off, me, him))
        if not frames:
            continue
        total += 1
        errors = [(off, angular_error(me, him)) for off, me, him in frames]
        if all(err <= tight for _, err in errors):
            hits += 1
            if len(evidence) < 6:
                shown = ", ".join(
                    f"{cfg.ticks_to_ms(off):.0f} ms antes: {err:.1f} deg"
                    for off, err in errors
                )
                evidence.append(
                    Evidence(
                        kind="prefire",
                        round_num=kill.round_num,
                        tick=kill.tick - offsets[-1],
                        target=demo.name_of(kill.victim),
                        detail=f"mira ja no alvo ({shown})",
                    )
                )

    rate = hits / total if total else 0.0
    return SignalResult(
        name="prefire",
        value=ramp(rate, 0.15, 0.65),
        samples=total,
        confident=total >= 5,
        raw={"kills_avaliadas": total, "pre_aim": hits, "taxa": round(rate, 3)},
        evidence=evidence,
    )


def _near_action(action_ticks: list, tick: int, window: int) -> bool:
    for t in action_ticks:
        if abs(t - tick) <= window:
            return True
        if t > tick + window:
            return False
    return False


def _empty_tracking(samples: int) -> SignalResult:
    return SignalResult("tracking", 0.0, samples=samples, confident=False,
                        raw={"episodios": 0})


def _empty_prefire() -> SignalResult:
    return SignalResult("prefire", 0.0, samples=0, confident=False,
                        raw={"engajamentos": 0})
