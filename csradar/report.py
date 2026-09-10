"""Formatacao do relatorio para o terminal."""

from __future__ import annotations

SIGNAL_LABELS = {
    "snap": "flick nao-humano",
    "jitter": "mira sem tremor",
    "reaction": "tempo de reacao",
    "tracking": "segue inimigo sem ver",
    "prefire": "pre-aim",
    "burst": "ritmo de kills (fraco)",
    "recoil": "controle de recuo",
    "movement": "movimento da mira",
    "context": "contexto das kills",
    "tremor": "tremor do cursor",
    "cursor_jump": "salto de cursor",
    "key_timing": "regularidade da tecla",
}

TEAM_LABELS = {2: "T", 3: "CT"}


def bar(value: float, width: int = 12) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def render_match(report, cfg, verbose: bool = False, me: int | None = None) -> str:
    lines = []
    lines.append(f"Fonte: {report.source}   jogo: {report.game}   "
                 f"mapa: {report.map_name}   tick: {report.tickrate:g}")
    if report.skipped_signals:
        faltando = sorted({c for s in report.skipped_signals for c in s["faltou"]})
        omitidos = ", ".join(SIGNAL_LABELS.get(s["sinal"], s["sinal"])
                             for s in report.skipped_signals)
        lines.append("")
        lines.append(f"ATENCAO: esta fonte nao fornece {', '.join(faltando)}.")
        lines.append(f"Sinais que NAO rodaram: {omitidos}.")
        lines.append("O que sobrou e triagem fraca - nao confunda score baixo")
        lines.append("com jogador limpo.")
    lines.append("")
    lines.append(f"{'jogador':<22}{'time':<5}{'K/D':>8}{'HS%':>7}{'score':>8}  sinais")
    lines.append("-" * 78)

    for p in report.sorted_players():
        team = TEAM_LABELS.get(p.team, str(p.team or "?"))
        mark = "*" if me and p.steamid == me else " "
        top = _top_signals(p)
        lines.append(
            f"{mark}{_clip(p.name, 21):<21}{team:<5}"
            f"{p.kills:>4}/{p.deaths:<3}{p.hs_pct:>6.0f}%{p.score:>8.1f}  {top}"
        )

    review = [p for p in report.sorted_players() if "REVISAR" in p.flags]
    lines.append("")
    if review:
        lines.append(f"Para assistir na demo ({len(review)} jogador(es) acima de "
                     f"{cfg.scoring.review_threshold:g}):")
        for p in review:
            lines.append("")
            lines.append(f"  {p.name}  ({p.steamid})  score {p.score:.1f}")
            for name, sig in sorted(
                p.signals.items(), key=lambda kv: kv[1].value, reverse=True
            ):
                if sig.value < 0.2:
                    continue
                flag = "" if sig.confident else "  (amostra pequena)"
                lines.append(
                    f"    {SIGNAL_LABELS.get(name, name):<24} "
                    f"[{bar(sig.value)}] {sig.value:.2f}{flag}"
                )
            for ev in p.top_evidence(5):
                lines.append(
                    f"      round {ev.round_num:>2}  tick {ev.tick:<8} "
                    f"{ev.kind:<11} {ev.detail}"
                )
                if ev.target:
                    lines.append(f"{'':>24}alvo: {ev.target}")
    else:
        lines.append("Nenhum jogador acima do limiar de revisao.")

    if verbose:
        lines.append("")
        lines.append("Detalhe numerico por jogador:")
        for p in report.sorted_players():
            lines.append(f"  {p.name} ({p.steamid})")
            for name, sig in p.signals.items():
                lines.append(f"    {name:<10} {sig.value:.3f}  {sig.raw}")

    lines.append("")
    lines.append("Isto e uma fila de priorizacao, nao um veredito. Assista aos")
    lines.append("momentos listados antes de reportar qualquer pessoa.")
    return "\n".join(lines)


def _top_signals(p, limit: int = 2) -> str:
    strong = sorted(p.signals.items(), key=lambda kv: kv[1].value, reverse=True)
    parts = [
        f"{SIGNAL_LABELS.get(n, n)} {s.value:.2f}"
        for n, s in strong[:limit]
        if s.value >= 0.3
    ]
    return ", ".join(parts) if parts else "-"


def _clip(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "~"


def render_risk(profiles: list) -> str:
    lines = [f"{'jogador':<24}{'risco':>7}  {'conta':>8}  motivos", "-" * 78]
    for pr in sorted(profiles, key=lambda x: x["risco"], reverse=True):
        age = f"{pr['idade_conta_dias']}d" if pr["idade_conta_dias"] is not None else "?"
        lines.append(
            f"{_clip(pr['nome'] or str(pr['steamid']), 23):<24}{pr['risco']:>7.0f}"
            f"  {age:>8}  {', '.join(pr['motivos']) or '-'}"
        )
        lines.append(f"{'':<24}{pr['perfil']}")
    lines.append("")
    lines.append("Risco de conta nao e deteccao de cheat. E so triagem.")
    return "\n".join(lines)
