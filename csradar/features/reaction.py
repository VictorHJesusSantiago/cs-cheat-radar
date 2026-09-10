"""Tempo de reacao a partir do momento em que o alvo entra no campo de visao.

O que denuncia nao e a media baixa (jogador bom tambem reage rapido), e sim
a distribuicao apertada: humano tem cauda longa, software nao.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import angular_error, angular_speed, distance, mean, median, ramp, stdev


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    index = index if index is not None else demo.index_ticks()
    rcfg = cfg.reaction
    my_ticks = index.get(steamid, {})
    my_team = demo.team_of(steamid)

    # primeiro disparo de cada engajamento, por kill
    reactions = []
    impossible = 0
    evidence: list = []

    shots_by_tick = sorted(s.tick for s in demo.shots if s.steamid == steamid)

    for kill in demo.kills:
        if kill.attacker != steamid or kill.victim == steamid:
            continue
        victim_ticks = index.get(kill.victim, {})
        if not victim_ticks:
            continue

        exposure = _first_exposure(my_ticks, victim_ticks, kill.tick, rcfg)
        if exposure is None:
            continue

        fire_tick = _first_shot_after(shots_by_tick, exposure, kill.tick)
        if fire_tick is None:
            fire_tick = kill.tick
        delta_ms = cfg.ticks_to_ms(fire_tick - exposure)
        if delta_ms < 0 or delta_ms > 2000:
            continue
        reactions.append(delta_ms)
        if delta_ms <= rcfg.impossible_ms:
            impossible += 1
            if len(evidence) < 8:
                evidence.append(
                    Evidence(
                        kind="reaction",
                        round_num=kill.round_num,
                        tick=exposure,
                        target=demo.name_of(kill.victim),
                        detail=f"disparo {delta_ms:.0f} ms apos o alvo aparecer no FOV",
                    )
                )

    n = len(reactions)
    if n < rcfg.min_samples:
        return {
            "reaction": SignalResult(
                name="reaction",
                value=0.0,
                samples=n,
                confident=False,
                raw={"amostras": n},
                evidence=evidence,
            )
        }

    med = median(reactions)
    sd = stdev(reactions)
    fast_rate = sum(1 for r in reactions if r <= rcfg.fast_ms) / n
    impossible_rate = impossible / n

    # consistencia só pesa se a mediana ja for rapida
    consistency = ramp(sd, rcfg.low_stdev_ms * 2.0, rcfg.low_stdev_ms * 0.4)
    if med > rcfg.fast_ms * 1.5:
        consistency *= 0.3

    value = max(
        ramp(impossible_rate, 0.05, 0.40),
        0.85 * ramp(fast_rate, 0.25, 0.80),
        0.75 * consistency,
    )

    return {
        "reaction": SignalResult(
            name="reaction",
            value=value,
            samples=n,
            confident=n >= rcfg.min_samples,
            raw={
                "amostras": n,
                "mediana_ms": round(med, 1),
                "media_ms": round(mean(reactions), 1),
                "desvio_ms": round(sd, 1),
                "abaixo_de_%dms" % int(rcfg.fast_ms): round(fast_rate, 3),
                "impossiveis": impossible,
            },
            evidence=evidence,
        )
    }


def _first_exposure(my_ticks: dict, victim_ticks: dict, kill_tick: int, rcfg) -> int | None:
    """Inicio do engajamento: primeiro tick do bloco continuo que termina na kill.

    Varre para TRAS a partir da kill. Usar a ultima entrada no FOV seria um
    erro grosseiro: durante o proprio flick a mira sai e volta ao FOV varias
    vezes, o que faria qualquer jogador parecer ter reagido em 30 ms.

    Buracos curtos sao tolerados para o bloco nao se fragmentar; a margem
    sobre o FOV e pequena, porque a fragmentacao causada pelo proprio giro do
    jogador ja e descartada pelo teste de mira parada mais abaixo.
    """
    wide = rcfg.fov_deg * 1.05
    exposure = None
    gap = 0
    for t in range(kill_tick, kill_tick - 256, -1):
        me = my_ticks.get(t)
        him = victim_ticks.get(t)
        ok = (
            me is not None
            and him is not None
            and me.is_alive
            and him.is_alive
            and distance(me, him) <= rcfg.max_range
            and angular_error(me, him) <= wide
        )
        if ok:
            exposure = t
            gap = 0
        else:
            gap += 1
            if gap > 4:
                break
    if exposure is None:
        return None
    # o bloco so conta como "alvo apareceu" se antes dele o alvo estava fora
    before = my_ticks.get(exposure - 6), victim_ticks.get(exposure - 6)
    if all(x is not None for x in before):
        if angular_error(before[0], before[1]) <= wide:
            return None  # ja estava a vista ha muito tempo: nao e reacao
    if not _was_steady(my_ticks, exposure, rcfg):
        return None      # foi o proprio jogador que girou ate o alvo
    return exposure


def _was_steady(my_ticks: dict, exposure: int, rcfg) -> bool:
    """A mira estava parada nos ticks anteriores a entrada do alvo?"""
    frames = [my_ticks.get(t) for t in range(exposure - rcfg.steady_ticks, exposure + 1)]
    frames = [f for f in frames if f is not None]
    if len(frames) < 3:
        return False
    peak = max(angular_speed(frames[i - 1], frames[i]) for i in range(1, len(frames)))
    return peak <= rcfg.steady_deg_per_tick


def _first_shot_after(shots_sorted: list, start: int, limit: int) -> int | None:
    for t in shots_sorted:
        if t < start:
            continue
        if t > limit:
            return None
        return t
    return None
