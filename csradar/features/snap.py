"""Snap angular e jitter residual.

Dois sinais que compartilham a mesma passada de dados:

- snap: a mira sai de fora do alvo (>off_target_deg) e chega travada
  (<on_target_deg) em poucos ticks, sem o overshoot-e-corrige caracteristico
  de um flick humano.
- jitter: uma vez travado, o erro angular residual de um humano oscila. Erro
  quase constante ao longo de varios ticks e assinatura de correcao por
  software.
"""

from __future__ import annotations

from ..models import Evidence, SignalResult
from .geometry import angular_error, angular_speed, mean, ramp, stdev


def _window(seq_by_tick: dict, anchor: int, back: int, ahead: int = 0) -> list:
    out = []
    for t in range(anchor - back, anchor + ahead + 1):
        pt = seq_by_tick.get(t)
        if pt is not None:
            out.append(pt)
    return out


def analyze(demo, steamid: int, cfg, index=None) -> dict:
    """Retorna {'snap': SignalResult, 'jitter': SignalResult}."""
    index = index if index is not None else demo.index_ticks()
    scfg = cfg.snap
    shooter_ticks = index.get(steamid, {})
    my_shots = sorted(s.tick for s in demo.shots if s.steamid == steamid)

    snaps = 0
    considered = 0
    extreme = 0
    jitter_locked = 0
    jitter_flat = 0
    peak_speeds = []
    snap_ev: list = []
    jitter_ev: list = []

    for kill in demo.kills:
        if kill.attacker != steamid or kill.victim == steamid:
            continue
        victim_ticks = index.get(kill.victim, {})
        if not victim_ticks:
            continue

        # A ancora e o PRIMEIRO disparo do duelo, nao a kill. Numa rajada de
        # rifle a morte acontece dezenas de ticks depois do flick, e olhar so
        # para tras da kill perderia justamente o movimento que interessa.
        anchor = _engagement_start(my_shots, kill.tick, cfg)
        frames = _window(shooter_ticks, anchor, scfg.lookback_ticks,
                         scfg.lookahead_ticks)
        if len(frames) < 4:
            continue

        errors = []
        for f in frames:
            vt = victim_ticks.get(f.tick)
            if vt is None or not vt.is_alive:
                errors.append(None)
                continue
            errors.append(angular_error(f, vt))
        if sum(1 for e in errors if e is not None) < 4:
            continue

        considered += 1

        speeds = [
            angular_speed(frames[i - 1], frames[i]) for i in range(1, len(frames))
        ]
        peak = max(speeds) if speeds else 0.0
        peak_speeds.append(peak)
        if peak >= scfg.extreme_angvel:
            extreme += 1

        # --- snap: transicao rapida off -> on sem overshoot ---
        hit = _find_snap(errors, scfg)
        if hit is not None:
            i_off, i_on = hit
            snaps += 1
            if len(snap_ev) < 8:
                snap_ev.append(
                    Evidence(
                        kind="snap",
                        round_num=kill.round_num,
                        tick=frames[i_on].tick,
                        target=demo.name_of(kill.victim),
                        detail=(
                            f"erro {errors[i_off]:.1f} deg -> {errors[i_on]:.1f} deg "
                            f"em {i_on - i_off} tick(s), pico {peak:.0f} deg/tick"
                        ),
                    )
                )

        # --- jitter residual apos travar ---
        locked = [e for e in errors if e is not None and e <= scfg.jitter_window_deg]
        if len(locked) >= scfg.min_jitter_samples:
            jitter_locked += 1
            sd = stdev(locked)
            if sd <= scfg.low_jitter_deg:
                jitter_flat += 1
                if len(jitter_ev) < 6:
                    jitter_ev.append(
                        Evidence(
                            kind="flat_aim",
                            round_num=kill.round_num,
                            tick=kill.tick,
                            target=demo.name_of(kill.victim),
                            detail=(
                                f"erro medio {mean(locked):.2f} deg com desvio "
                                f"{sd:.3f} deg em {len(locked)} ticks"
                            ),
                        )
                    )

    snap_rate = snaps / considered if considered else 0.0
    extreme_rate = extreme / considered if considered else 0.0
    snap_value = max(ramp(snap_rate, 0.05, 0.45), 0.7 * ramp(extreme_rate, 0.10, 0.60))

    flat_rate = jitter_flat / jitter_locked if jitter_locked else 0.0
    jitter_value = ramp(flat_rate, 0.15, 0.70)

    return {
        "snap": SignalResult(
            name="snap",
            value=snap_value,
            samples=considered,
            confident=considered >= 5,
            raw={
                "kills_analisadas": considered,
                "snaps": snaps,
                "snap_rate": round(snap_rate, 3),
                "flicks_extremos": extreme,
                "pico_medio_deg_por_tick": round(mean(peak_speeds), 1),
            },
            evidence=snap_ev,
        ),
        "jitter": SignalResult(
            name="jitter",
            value=jitter_value,
            samples=jitter_locked,
            confident=jitter_locked >= 4,
            raw={
                "janelas_travadas": jitter_locked,
                "janelas_sem_tremor": jitter_flat,
                "flat_rate": round(flat_rate, 3),
            },
            evidence=jitter_ev,
        ),
    }


def _engagement_start(shots: list, kill_tick: int, cfg) -> int:
    """Primeiro disparo da sequencia que termina nesta kill."""
    window = int(cfg.ms_to_ticks(2500))
    candidates = [t for t in shots if kill_tick - window <= t <= kill_tick]
    if not candidates:
        return kill_tick
    first = candidates[-1]
    gap = int(cfg.ms_to_ticks(250))
    for tick in reversed(candidates):
        if first - tick <= gap:
            first = tick
        else:
            break
    return first


def _find_snap(errors: list, scfg) -> tuple | None:
    """Acha (i_off, i_on): saida de fora do alvo para travado em poucos ticks.

    Um flick humano rapido tambem pode cruzar essa distancia em um tick a 64 Hz,
    entao a transicao sozinha nao basta. Exigimos as tres coisas juntas:

    - transicao off -> on em poucos ticks;
    - sem overshoot (o humano passa do alvo e volta);
    - e a mira fica ESTAVEL depois de travar (o humano continua corrigindo).
    """
    last_off = None
    for i, err in enumerate(errors):
        if err is None:
            continue
        if err >= scfg.off_target_deg:
            last_off = i
            continue
        if err <= scfg.on_target_deg and last_off is not None:
            gap = i - last_off
            if 0 < gap <= scfg.max_transition_ticks:
                if not _has_overshoot(errors, i, scfg) and _stable_after(errors, i, scfg):
                    return last_off, i
            last_off = None
    return None


def _stable_after(errors: list, i_on: int, scfg) -> bool:
    tail = [e for e in errors[i_on:] if e is not None]
    if len(tail) < scfg.post_lock_min_ticks:
        return False
    return stdev(tail) <= scfg.post_lock_max_stdev


def _has_overshoot(errors: list, i_on: int, scfg) -> bool:
    """Depois de travar, um humano tipicamente escapa do alvo e volta."""
    for err in errors[i_on + 1 : i_on + 1 + scfg.lookback_ticks]:
        if err is None:
            continue
        if err > scfg.on_target_deg * 2.5:
            return True
    return False
