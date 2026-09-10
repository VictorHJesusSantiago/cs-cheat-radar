"""Detectores para entrada 2D de cursor (osu! e qualquer jogo de mira plana).

Aqui nao existe inimigo, alvo nem angulo de visao. Existe uma trajetoria de
cursor amostrada e o estado das teclas. Entao os detectores perguntam uma
coisa so: essa entrada foi produzida por uma mao?

Tres assinaturas, das mais dificeis de imitar:

- tremor: uma mao humana nunca desenha uma curva limpa. Mesmo o jogador mais
  firme deixa energia de alta frequencia - micro-correcoes constantes. Aim
  assist e replay-bot interpolam, e a curva fica lisa demais. Medimos o
  "jerk" (variacao da aceleracao) normalizado pela velocidade: humano alto,
  interpolacao baixa.
- teleporte de cursor: salto grande entre quadros consecutivos seguido de
  parada. Mao nao teleporta; software que reposiciona o cursor, sim.
- regularidade da tecla: a duracao dos pressionamentos de um humano varia
  bastante. Relax (que aperta sozinho) produz duracoes com dispersao
  minuscula, mesmo quando a media parece plausivel.

Nenhum deles olha para acerto de nota, porque sem o beatmap nao da para saber
o que era certo - e chutar isso produziria acusacao baseada em nada.
"""

from __future__ import annotations

import math

from ..models import Evidence, SignalResult
from .geometry import mean, median, ramp, stdev

_KEYS = (1, 2, 4, 8)   # M1, M2, K1, K2


def _series(demo, steamid: int) -> list:
    seq = demo.ticks_by_player.get(steamid) or []
    return [t for t in seq if t is not None]


def _speeds(seq: list) -> list:
    out = []
    for i in range(1, len(seq)):
        dt = max(1, seq[i].tick - seq[i - 1].tick)
        dist = math.dist((seq[i - 1].x, seq[i - 1].y), (seq[i].x, seq[i].y))
        out.append(dist / dt)
    return out


def analyze_tremor(demo, steamid: int, cfg, index=None) -> dict:
    """Energia de alta frequencia da trajetoria."""
    ccfg = cfg.cursor
    seq = _series(demo, steamid)
    if len(seq) < ccfg.min_samples:
        return {"tremor": _empty("tremor", len(seq))}

    # jerk discreto: terceira diferenca da posicao
    jerks = []
    speeds = []
    for i in range(3, len(seq)):
        p0, p1, p2, p3 = seq[i - 3], seq[i - 2], seq[i - 1], seq[i]
        jx = (p3.x - 3 * p2.x + 3 * p1.x - p0.x)
        jy = (p3.y - 3 * p2.y + 3 * p1.y - p0.y)
        speed = math.dist((p2.x, p2.y), (p3.x, p3.y))
        # so medimos onde ha movimento: cursor parado tem jerk zero e isso
        # nao diz nada sobre a mao de ninguem
        if speed < ccfg.min_speed:
            continue
        jerks.append(math.hypot(jx, jy) / max(speed, 1e-6))
        speeds.append(speed)

    if len(jerks) < ccfg.min_samples // 4:
        return {"tremor": _empty("tremor", len(jerks))}

    smoothness = median(jerks)
    value = ramp(smoothness, ccfg.human_jerk, ccfg.smooth_jerk)

    evidence = []
    if value >= 0.5:
        evidence.append(Evidence(
            kind="tremor", round_num=1, tick=seq[0].tick,
            detail=(f"jerk mediano {smoothness:.3f} (humano fica perto de "
                    f"{ccfg.human_jerk:.2f}); trajetoria lisa demais em "
                    f"{len(jerks)} amostras de movimento"),
        ))

    return {"tremor": SignalResult(
        name="tremor", value=value, samples=len(jerks),
        confident=len(jerks) >= ccfg.min_samples // 2,
        raw={
            "amostras_em_movimento": len(jerks),
            "jerk_mediano": round(smoothness, 4),
            "velocidade_mediana": round(median(speeds), 3),
        },
        evidence=evidence,
    )}


def analyze_teleport(demo, steamid: int, cfg, index=None) -> dict:
    """Saltos de cursor incompativeis com movimento continuo."""
    ccfg = cfg.cursor
    seq = _series(demo, steamid)
    if len(seq) < ccfg.min_samples:
        return {"cursor_jump": _empty("cursor_jump", len(seq))}

    speeds = _speeds(seq)
    if not speeds:
        return {"cursor_jump": _empty("cursor_jump", 0)}

    jumps = 0
    followed_by_stop = 0
    evidence = []
    for i, speed in enumerate(speeds):
        if speed < ccfg.jump_speed:
            continue
        jumps += 1
        nxt = speeds[i + 1] if i + 1 < len(speeds) else None
        if nxt is not None and nxt < ccfg.stop_speed:
            followed_by_stop += 1
            if len(evidence) < 6:
                evidence.append(Evidence(
                    kind="cursor_jump", round_num=1, tick=seq[i + 1].tick,
                    detail=(f"salto de {speed:.0f} px/tick seguido de parada "
                            f"({nxt:.2f} px/tick)"),
                ))

    rate = jumps / len(speeds)
    stop_rate = followed_by_stop / jumps if jumps else 0.0
    value = max(0.4 * ramp(rate, 0.001, 0.02),
                ramp(stop_rate, 0.15, 0.6) if jumps >= 5 else 0.0)

    return {"cursor_jump": SignalResult(
        name="cursor_jump", value=value, samples=len(speeds),
        confident=len(speeds) >= ccfg.min_samples and jumps >= 5,
        raw={"amostras": len(speeds), "saltos": jumps,
             "saltos_com_parada": followed_by_stop,
             "taxa_de_parada": round(stop_rate, 3)},
        evidence=evidence,
    )}


def analyze_keys(demo, steamid: int, cfg, index=None) -> dict:
    """Regularidade da duracao dos pressionamentos."""
    ccfg = cfg.cursor
    seq = _series(demo, steamid)
    if len(seq) < ccfg.min_samples:
        return {"key_timing": _empty("key_timing", len(seq))}

    durations = []
    gaps = []
    for key in _KEYS:
        press_start = None
        last_release = None
        for tick in seq:
            down = bool(tick.keys & key)
            if down and press_start is None:
                press_start = tick.tick
                if last_release is not None:
                    gaps.append(tick.tick - last_release)
            elif not down and press_start is not None:
                durations.append(tick.tick - press_start)
                last_release = tick.tick
                press_start = None

    if len(durations) < ccfg.min_presses:
        return {"key_timing": _empty("key_timing", len(durations))}

    ms = [cfg.ticks_to_ms(d) for d in durations if d > 0]
    if len(ms) < ccfg.min_presses:
        return {"key_timing": _empty("key_timing", len(ms))}

    spread = stdev(ms)
    average = mean(ms)
    relative = spread / average if average else 1.0
    value = ramp(relative, ccfg.human_key_spread, ccfg.robot_key_spread)

    evidence = []
    if value >= 0.5:
        evidence.append(Evidence(
            kind="key_timing", round_num=1, tick=seq[0].tick,
            detail=(f"{len(ms)} pressionamentos com duracao media "
                    f"{average:.0f} ms e dispersao relativa {relative:.3f} - "
                    f"mao humana fica acima de {ccfg.human_key_spread:.2f}"),
        ))

    return {"key_timing": SignalResult(
        name="key_timing", value=value, samples=len(ms),
        confident=len(ms) >= ccfg.min_presses * 2,
        raw={
            "pressionamentos": len(ms),
            "duracao_media_ms": round(average, 1),
            "dispersao_ms": round(spread, 1),
            "dispersao_relativa": round(relative, 3),
            "intervalo_mediano_ms": (round(cfg.ticks_to_ms(median(gaps)), 1)
                                     if gaps else None),
        },
        evidence=evidence,
    )}


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    """Roda os tres e devolve tudo junto."""
    out = {}
    out.update(analyze_tremor(demo, steamid, cfg, index))
    out.update(analyze_teleport(demo, steamid, cfg, index))
    out.update(analyze_keys(demo, steamid, cfg, index))
    return out


def _empty(name: str, samples: int) -> SignalResult:
    return SignalResult(name, 0.0, samples=samples, confident=False,
                        raw={"amostras": samples})
