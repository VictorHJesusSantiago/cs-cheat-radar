"""Assinaturas de entrada nao-humana na propria trajetoria da mira.

Diferente dos outros detectores, este nao olha para alvo nenhum. Ele olha so
para a serie temporal de pitch/yaw do jogador e pergunta se aquilo poderia ter
sido produzido por uma mao num mouse.

Duas assinaturas:

- teleporte: deslocamento por tick que nenhum mouse sustenta. Um flick humano
  rapido chega a dezenas de graus num tick a 64 Hz, entao o limiar e alto de
  proposito e o que importa e a REPETICAO, nao um caso isolado.
- ida e volta ("silent aim"): a mira salta e RETORNA ao angulo de origem em um
  ou dois ticks. Aqui o tamanho do salto quase nao importa - o que denuncia e
  a precisao do retorno. Um humano que gira 40 graus e volta nao para no mesmo
  angulo de antes com meio grau de erro; um cheat que aponta so no instante do
  disparo e devolve a mira, sim.

O segundo e o sinal forte. O primeiro sozinho pega lag e teleporte de servidor
tambem, e por isso pesa menos.

Nota sobre a escala: cada episodio de silent aim produz DOIS saltos contados,
a ida e a volta, e so a ida "retorna". Por isso a taxa de retorno satura perto
de 0,5 mesmo num caso escancarado, e os limiares refletem isso.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import angular_speed, ramp, _spherical_delta


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    mcfg = cfg.movement
    seq = [t for t in (demo.ticks_by_player.get(steamid) or []) if t.is_alive]
    if len(seq) < 32:
        return {"movement": _empty(len(seq))}

    teleports = 0
    jumps = 0
    returns = 0
    considered = 0
    evidence: list = []

    for i in range(1, len(seq)):
        prev, cur = seq[i - 1], seq[i]
        if cur.tick - prev.tick != 1:      # buraco na amostragem: nao da para medir
            continue
        considered += 1
        jump = angular_speed(prev, cur)
        if jump >= mcfg.teleport_deg:
            teleports += 1
        if jump < mcfg.snap_back_deg:
            continue
        jumps += 1

        # a mira voltou para onde estava, poucos ticks depois?
        back = _returned(seq, i, prev, mcfg)
        if back is None:
            continue
        returns += 1
        if len(evidence) < 8:
            evidence.append(Evidence(
                kind="silent_aim",
                round_num=cur.round_num,
                tick=cur.tick,
                detail=(
                    f"mira saltou {jump:.0f} deg e voltou a {back:.1f} deg do "
                    f"angulo original em {mcfg.return_window_ticks} ticks"
                ),
            ))

    if considered < mcfg.min_ticks:
        return {"movement": _empty(considered)}

    teleport_rate = teleports / considered
    return_rate = returns / jumps if jumps else 0.0

    value = max(
        0.5 * ramp(teleport_rate, 0.0005, 0.006),
        ramp(return_rate, 0.05, 0.30) if jumps >= 8 else 0.0,
    )

    return {"movement": SignalResult(
        name="movement",
        value=value,
        samples=considered,
        confident=considered >= mcfg.min_ticks and jumps >= 8,
        raw={
            "ticks_analisados": considered,
            "teleportes": teleports,
            "saltos": jumps,
            "saltos_com_retorno": returns,
            "taxa_de_retorno": round(return_rate, 3),
        },
        evidence=evidence,
    )}


def _returned(seq: list, i: int, origin, mcfg) -> float | None:
    """Distancia angular ao angulo de origem, se a mira voltou a ele."""
    for j in range(i + 1, min(i + 1 + mcfg.return_window_ticks, len(seq))):
        delta = _spherical_delta(origin.pitch, origin.yaw,
                                 seq[j].pitch, seq[j].yaw)
        if delta <= mcfg.return_tolerance_deg:
            return delta
    return None


def _empty(samples: int) -> SignalResult:
    return SignalResult("movement", 0.0, samples=samples, confident=False,
                        raw={"ticks_analisados": samples})
