"""Controle de recuo em rajada sustentada.

Segurar o gatilho num rifle sobe a mira num padrao conhecido, e o jogador
compensa puxando para baixo. Isso e uma habilidade real e muito treinavel, e
um bom jogador compensa bem - por isso o sinal aqui NAO e "compensou bem".

O que separa e a forma do erro ao longo da rajada. O humano corrige em ciclo:
erra para cima, puxa demais, corrige, erra de novo - a curva do erro sobe e
desce. A compensacao por software zera o erro e o mantem zerado, porque ela
nao esta reagindo ao que ve, esta subtraindo um vetor conhecido.

Medimos as duas coisas juntas: erro medio muito baixo E dispersao do erro
muito baixa ao longo dos disparos. Uma sozinha nao vale nada.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import angular_error, mean, ramp, stdev


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    index = index if index is not None else demo.index_ticks()
    rcfg = cfg.recoil
    my_ticks = index.get(steamid, {})

    shots = sorted(s.tick for s in demo.shots if s.steamid == steamid)
    if not shots:
        return {"recoil": _empty(0)}

    bursts = _group_bursts(shots, rcfg.max_gap_ticks, rcfg.min_shots)
    if not bursts:
        return {"recoil": _empty(0)}

    analyzed = 0
    perfect = 0
    errors_seen = []
    evidence: list = []

    for burst in bursts:
        victim = _victim_during(demo, steamid, burst[0], burst[-1])
        if victim is None:
            continue
        victim_ticks = index.get(victim, {})
        series = []
        for tick in burst:
            me = my_ticks.get(tick)
            him = victim_ticks.get(tick)
            if me is None or him is None or not him.is_alive:
                continue
            series.append(angular_error(me, him))
        if len(series) < rcfg.min_shots:
            continue

        analyzed += 1
        avg = mean(series)
        spread = stdev(series)
        errors_seen.append(avg)

        if avg <= rcfg.perfect_error_deg and spread <= rcfg.flat_spread_deg:
            perfect += 1
            if len(evidence) < 6:
                evidence.append(Evidence(
                    kind="recoil",
                    round_num=_round_of(demo, burst[0]),
                    tick=burst[0],
                    target=demo.name_of(victim),
                    detail=(
                        f"{len(series)} tiros com erro medio {avg:.2f} deg e "
                        f"dispersao {spread:.2f} deg - a curva de correcao "
                        f"humana nao e plana assim"
                    ),
                ))

    if analyzed == 0:
        return {"recoil": _empty(0)}

    rate = perfect / analyzed
    return {"recoil": SignalResult(
        name="recoil",
        value=ramp(rate, 0.15, 0.65),
        samples=analyzed,
        confident=analyzed >= cfg.recoil.min_bursts,
        raw={
            "rajadas_analisadas": analyzed,
            "rajadas_perfeitas": perfect,
            "taxa": round(rate, 3),
            "erro_medio_deg": round(mean(errors_seen), 2) if errors_seen else None,
        },
        evidence=evidence,
    )}


def _group_bursts(shots: list, max_gap: int, min_shots: int) -> list:
    bursts = []
    current = [shots[0]]
    for tick in shots[1:]:
        if tick - current[-1] <= max_gap:
            current.append(tick)
        else:
            if len(current) >= min_shots:
                bursts.append(current)
            current = [tick]
    if len(current) >= min_shots:
        bursts.append(current)
    return bursts


def _victim_during(demo, steamid: int, start: int, end: int):
    """Quem esta levando a rajada: a vitima morta durante ou logo apos ela."""
    window = end + 64
    for kill in demo.kills:
        if kill.attacker != steamid or kill.victim == steamid:
            continue
        if start <= kill.tick <= window:
            return kill.victim
    return None


def _round_of(demo, tick: int) -> int:
    for rnd in demo.rounds:
        if rnd.start_tick <= tick <= rnd.end_tick:
            return rnd.number
    return 0


def _empty(samples: int) -> SignalResult:
    return SignalResult("recoil", 0.0, samples=samples, confident=False,
                        raw={"rajadas_analisadas": samples})
