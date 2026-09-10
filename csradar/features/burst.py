"""Ritmo de kills - o unico sinal que sobrevive quando so ha eventos.

Serve para fontes sem angulo de visao: log de servidor dedicado, replays de
jogos que so expoem eventos, ingestao parcial. Mede duas coisas:

- multikill comprimido: varios inimigos mortos dentro de uma janela curta
  demais para haver reposicionamento de mira entre eles;
- regularidade: intervalos entre kills consecutivas com dispersao baixa
  demais, do mesmo jeito que o tempo de reacao denuncia pela consistencia.

Isto e triagem FRACA e precisa ser tratado como tal. Um ace legitimo com AWP
em jogadores empilhados produz exatamente este padrao, e log de servidor
Source tem resolucao de um segundo, o que torna a medida grosseira. Nunca
reporte alguem so por causa deste sinal.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import median, ramp, stdev


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    bcfg = cfg.burst
    kills = [
        k for k in demo.kills
        if k.attacker == steamid and k.victim != steamid
    ]
    kills.sort(key=lambda k: k.tick)

    if len(kills) < bcfg.min_kills:
        return {"burst": SignalResult(
            name="burst", value=0.0, samples=len(kills), confident=False,
            raw={"kills": len(kills)},
        )}

    by_round: dict = {}
    for k in kills:
        by_round.setdefault(k.round_num, []).append(k)

    window = cfg.ms_to_ticks(bcfg.window_ms)
    bursts = 0
    biggest = 0
    gaps = []
    evidence: list = []

    for rnd, seq in sorted(by_round.items()):
        for i in range(1, len(seq)):
            gaps.append(seq[i].tick - seq[i - 1].tick)
        # maior grupo dentro da janela
        start = 0
        for i in range(len(seq)):
            while seq[i].tick - seq[start].tick > window:
                start += 1
            size = i - start + 1
            if size >= bcfg.min_burst_kills:
                biggest = max(biggest, size)
                if i == len(seq) - 1 or seq[i + 1].tick - seq[start].tick > window:
                    bursts += 1
                    if len(evidence) < 6:
                        evidence.append(Evidence(
                            kind="burst",
                            round_num=rnd,
                            tick=seq[start].tick,
                            detail=(
                                f"{size} kills em "
                                f"{cfg.ticks_to_ms(seq[i].tick - seq[start].tick):.0f} ms"
                            ),
                        ))

    rounds = max(1, len(by_round))
    burst_rate = bursts / rounds

    gap_ms = [cfg.ticks_to_ms(g) for g in gaps if g >= 0]
    regularity = 0.0
    if len(gap_ms) >= bcfg.min_gap_samples:
        med = median(gap_ms)
        sd = stdev(gap_ms)
        if med <= bcfg.fast_gap_ms:
            # dispersao relativa baixa = ritmo de maquina
            rel = sd / med if med else 1.0
            regularity = ramp(rel, 0.55, 0.15)

    value = max(ramp(burst_rate, 0.08, 0.45), 0.7 * regularity)

    return {"burst": SignalResult(
        name="burst",
        value=value,
        samples=len(kills),
        confident=len(kills) >= bcfg.min_kills * 2 and len(by_round) >= 5,
        raw={
            "kills": len(kills),
            "rajadas": bursts,
            "maior_rajada": biggest,
            "rajadas_por_round": round(burst_rate, 3),
            "intervalo_mediano_ms": round(median(gap_ms), 1) if gap_ms else None,
            "dispersao_ms": round(stdev(gap_ms), 1) if len(gap_ms) > 1 else None,
        },
        evidence=evidence,
    )}
