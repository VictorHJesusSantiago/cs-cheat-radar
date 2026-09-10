"""Combina os sinais em um score 0-100 por jogador.

O score e uma FILA DE PRIORIZACAO para revisao manual, nunca um veredito.

Cada detector declara de que dados precisa. Uma fonte que nao fornece angulo
de visao simplesmente nao roda os detectores de mira - eles nao sao "zerados",
sao omitidos, e o relatorio lista o que ficou de fora. Zerar seria pior que
inutil: faria um log de servidor parecer inocentar todo mundo.
"""

from __future__ import annotations

from .features import burst as burst_mod
from .features import context as context_mod
from .features import cursor as cursor_mod
from .features import movement as movement_mod
from .features import reaction as reaction_mod
from .features import recoil as recoil_mod
from .features import snap as snap_mod
from .features import tracking as tracking_mod
from .games.base import (
    CAP_ANGLES, CAP_CURSOR, CAP_KEYS, CAP_KILL_FLAGS, CAP_KILLS,
    CAP_POSITIONS, CAP_SHOTS, CAP_TEAMS,
)
from .models import MatchReport, PlayerReport

UNCONFIDENT_WEIGHT = 0.35

# Sinais que sozinhos nao sustentam uma acusacao. Quando sao os unicos
# disponiveis, o score e limitado por scoring.weak_only_cap.
WEAK_SIGNALS = {"burst"}

# detector -> (funcao, capacidades exigidas, sinais que produz)
DETECTORS = (
    ("snap", snap_mod.analyze,
     {CAP_ANGLES, CAP_POSITIONS, CAP_KILLS}, ("snap", "jitter")),
    ("reaction", reaction_mod.analyze,
     {CAP_ANGLES, CAP_POSITIONS, CAP_KILLS, CAP_SHOTS}, ("reaction",)),
    ("tracking", tracking_mod.analyze,
     {CAP_ANGLES, CAP_POSITIONS, CAP_TEAMS}, ("tracking", "prefire")),
    ("recoil", recoil_mod.analyze,
     {CAP_ANGLES, CAP_POSITIONS, CAP_SHOTS, CAP_KILLS}, ("recoil",)),
    ("movement", movement_mod.analyze,
     {CAP_ANGLES}, ("movement",)),
    ("context", context_mod.analyze,
     {CAP_KILLS, CAP_KILL_FLAGS}, ("context",)),
    ("burst", burst_mod.analyze,
     {CAP_KILLS}, ("burst",)),
    # familia 2D: nao ha alvo nem inimigo, so a forma da propria entrada
    ("cursor_tremor", cursor_mod.analyze_tremor,
     {CAP_CURSOR}, ("tremor",)),
    ("cursor_jump", cursor_mod.analyze_teleport,
     {CAP_CURSOR}, ("cursor_jump",)),
    ("cursor_keys", cursor_mod.analyze_keys,
     {CAP_CURSOR, CAP_KEYS}, ("key_timing",)),
)


def applicable_detectors(caps: set, extra=()) -> tuple:
    """(rodam, faltando) - faltando e uma lista de (sinal, capacidade ausente).

    `extra` recebe detectores de plugin, no mesmo formato de DETECTORS. Eles
    passam exatamente pela mesma checagem de capacidade: um plugin nao pode
    rodar sobre dado que nao existe so porque foi o usuario que escreveu.
    """
    run, missing = [], []
    for name, fn, needs, produces in tuple(DETECTORS) + tuple(extra):
        absent = needs - set(caps)
        if absent:
            for sig in produces:
                missing.append((sig, sorted(absent)))
        else:
            run.append((name, fn, produces))
    return run, missing


def analyze_player(demo, steamid: int, cfg, index=None, detectors=None) -> PlayerReport:
    index = index if index is not None else demo.index_ticks()
    if detectors is None:
        detectors, _ = applicable_detectors(demo.capabilities)

    signals = {}
    for _name, fn, _produces in detectors:
        signals.update(fn(demo, steamid, cfg, index))

    kills = sum(1 for k in demo.kills if k.attacker == steamid and k.victim != steamid)
    deaths = sum(1 for k in demo.kills if k.victim == steamid)
    hs = sum(
        1 for k in demo.kills
        if k.attacker == steamid and k.victim != steamid and k.headshot
    )
    shots = sum(1 for s in demo.shots if s.steamid == steamid)

    report = PlayerReport(
        steamid=steamid,
        name=demo.name_of(steamid),
        team=demo.team_of(steamid),
        kills=kills,
        deaths=deaths,
        headshots=hs,
        shots=shots,
        signals=signals,
    )
    report.score = compute_score(signals, cfg)
    report.flags = build_flags(report, cfg)
    return report


def compute_score(signals: dict, cfg) -> float:
    weights = cfg.scoring.weights
    total_w = 0.0
    acc = 0.0
    for name, weight in weights.items():
        sig = signals.get(name)
        if sig is None:
            continue
        w = weight if sig.confident else weight * UNCONFIDENT_WEIGHT
        acc += w * sig.value
        total_w += w
    if total_w <= 0:
        return 0.0
    base = acc / total_w

    # dois sinais independentes fortes valem mais que um: pequeno bonus de
    # corroboracao, limitado, para nao virar somatorio de ruido.
    strong = sum(1 for s in signals.values() if s.confident and s.value >= 0.55)
    if strong >= 2:
        base = min(1.0, base * (1.0 + 0.08 * (strong - 1)))

    score = 100.0 * base
    if signals and set(signals) <= WEAK_SIGNALS:
        score = min(score, cfg.scoring.weak_only_cap)
    return round(score, 1)


def build_flags(report: PlayerReport, cfg) -> list:
    flags = []
    if report.kills < cfg.scoring.min_kills_for_score:
        flags.append("amostra_pequena")
    only_weak = set(report.signals) <= {"burst"}
    if only_weak and report.signals:
        flags.append("sinal_fraco_apenas")
    for name, sig in report.signals.items():
        if sig.confident and sig.value >= 0.6:
            flags.append(f"forte:{name}")
        elif sig.confident and sig.value >= 0.35:
            flags.append(f"moderado:{name}")
    if report.kills >= 10 and report.hs_pct >= 80:
        flags.append("hs_pct_alto")  # sozinho nao significa nada
    if (report.score >= cfg.scoring.review_threshold
            and "amostra_pequena" not in flags):
        flags.append("REVISAR")
    return flags


def analyze_demo(demo, cfg, only=None, extra_detectors=()) -> MatchReport:
    detectors, missing = applicable_detectors(demo.capabilities,
                                              extra_detectors)
    index = demo.index_ticks()
    targets = only if only else demo.steamids()
    players = [
        analyze_player(demo, sid, cfg, index, detectors) for sid in targets
    ]
    return MatchReport(
        source=demo.source,
        map_name=demo.map_name,
        tickrate=demo.tickrate,
        players=players,
        game=getattr(demo, "game", "cs2"),
        capabilities=set(demo.capabilities),
        skipped_signals=[
            {"sinal": sig, "faltou": faltou} for sig, faltou in missing
        ],
    )
