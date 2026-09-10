"""Contexto das kills: fumaca, parede, sem mira.

Nao mede mira nenhuma - mede em que circunstancia as mortes aconteceram. Um
jogador que enxerga atraves da fumaca e das paredes acumula kills em situacoes
que quem depende dos olhos raramente consegue.

Precisa das flags por kill, que hoje so o adaptador de CS2 entrega. Onde elas
nao existem, o detector nao roda - e isso aparece no relatorio.

Cada taxa isolada tem explicacao inocente: fumaca se atravessa de proposito
com spray combinado, parede se fura em angulos conhecidos, noscope acontece em
duelo apertado de AWP. O sinal so cresce quando as taxas sao altas em mais de
uma categoria ao mesmo tempo, que e o padrao dificil de justificar.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import ramp


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    ccfg = cfg.context
    kills = [k for k in demo.kills
             if k.attacker == steamid and k.victim != steamid]
    total = len(kills)
    if total < ccfg.min_kills:
        return {"context": SignalResult(
            "context", 0.0, samples=total, confident=False,
            raw={"kills": total})}

    smoke = sum(1 for k in kills if k.through_smoke)
    wall = sum(1 for k in kills if k.penetrated)
    noscope = sum(1 for k in kills if k.noscope)

    smoke_rate = smoke / total
    wall_rate = wall / total
    noscope_rate = noscope / total

    parts = [
        ramp(smoke_rate, ccfg.smoke_rate * 0.5, ccfg.smoke_rate * 2.0),
        ramp(wall_rate, ccfg.wallbang_rate * 0.5, ccfg.wallbang_rate * 2.0),
        ramp(noscope_rate, ccfg.noscope_rate * 0.5, ccfg.noscope_rate * 2.0),
    ]
    parts.sort(reverse=True)
    # a maior categoria domina; a segunda soma metade. Duas categorias altas
    # ao mesmo tempo e o que realmente chama atencao.
    value = min(1.0, parts[0] * 0.7 + parts[1] * 0.3)

    evidence: list = []
    for kill in kills:
        if len(evidence) >= 6:
            break
        why = []
        if kill.through_smoke:
            why.append("atraves de fumaca")
        if kill.penetrated:
            why.append(f"atraves de parede ({kill.penetrated})")
        if kill.noscope:
            why.append("sem mira")
        if why:
            evidence.append(Evidence(
                kind="contexto", round_num=kill.round_num, tick=kill.tick,
                target=demo.name_of(kill.victim),
                detail=", ".join(why) + f" com {kill.weapon or 'arma'}",
            ))

    return {"context": SignalResult(
        name="context",
        value=value,
        samples=total,
        confident=total >= ccfg.min_kills * 2,
        raw={
            "kills": total,
            "fumaca": smoke, "taxa_fumaca": round(smoke_rate, 3),
            "parede": wall, "taxa_parede": round(wall_rate, 3),
            "noscope": noscope, "taxa_noscope": round(noscope_rate, 3),
        },
        evidence=evidence,
    )}
