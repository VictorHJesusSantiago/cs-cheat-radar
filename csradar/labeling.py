"""Rotulo retroativo: quem voce viu hoje e a Valve baniu depois.

Este e o unico ponto do projeto que produz verdade. Todo o resto sao
heuristicas com limiar chutado; e este job, rodando por meses, que diz se os
limiares prestam.

Cuidado metodologico que vale repetir: rotulo negativo aqui significa "ainda
nao banido", nao "limpo". Nem todo cheater e pego, e quando e, demora. Isso e
um problema de classe rara com rotulo ruidoso e positivo atrasado - precisao
importa muito mais que revocacao, e acuracia nao significa nada.
"""

from __future__ import annotations

import time

from .steam import SteamClient


def run_recheck(store, client: SteamClient, min_age_days: int = 30,
                cooldown_days: int = 7, limit: int = 500, verbose=None) -> dict:
    """Reconsulta os bans dos jogadores vistos ha pelo menos min_age_days."""
    pending = store.steamids_pending_recheck(min_age_days, cooldown_days)[:limit]
    if not pending:
        return {"consultados": 0, "novos_banidos": [], "ja_banidos": 0}

    newly_banned = []
    already = 0
    known = _known_banned(store)

    for i in range(0, len(pending), 100):
        chunk = pending[i : i + 100]
        for sid, info in client.bans(chunk).items():
            store.save_ban_check(sid, info)
            banned = bool(info.get("VACBanned")) or int(
                info.get("NumberOfGameBans", 0) or 0
            ) > 0
            if not banned:
                continue
            if sid in known:
                already += 1
                continue
            newly_banned.append({
                "steamid": sid,
                "dias_desde_ban": info.get("DaysSinceLastBan"),
                "vac": bool(info.get("VACBanned")),
                "game_bans": int(info.get("NumberOfGameBans", 0) or 0),
            })
        if verbose:
            verbose(f"  consultados {min(i + 100, len(pending))}/{len(pending)}")

    return {
        "consultados": len(pending),
        "novos_banidos": newly_banned,
        "ja_banidos": already,
        "em": int(time.time()),
    }


def _known_banned(store) -> set:
    rows = store.conn.execute(
        "SELECT DISTINCT steamid FROM ban_checks "
        "WHERE vac_banned = 1 OR game_bans > 0"
    ).fetchall()
    return {int(r["steamid"]) for r in rows}


def evaluate_threshold(store, threshold: float) -> dict:
    """Precisao/revocacao do score contra o rotulo de ban acumulado.

    So faz sentido depois de algumas centenas de partidas E de os rechecks
    terem tido tempo de rodar. Antes disso os numeros nao querem dizer nada.
    """
    rows = store.labeled_dataset()
    if not rows:
        return {"erro": "nenhuma observacao no banco"}

    by_player: dict = {}
    for r in rows:
        sid = r["steamid"]
        cur = by_player.setdefault(sid, {"score": 0.0, "label": 0, "n": 0})
        cur["score"] = max(cur["score"], r["score"])
        cur["label"] = max(cur["label"], r["label"])
        cur["n"] += 1

    tp = fp = fn = tn = 0
    for v in by_player.values():
        flagged = v["score"] >= threshold
        if flagged and v["label"]:
            tp += 1
        elif flagged:
            fp += 1
        elif v["label"]:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    positives = tp + fn
    return {
        "limiar": threshold,
        "jogadores": len(by_player),
        "banidos_conhecidos": positives,
        "prevalencia": round(positives / len(by_player), 4) if by_player else 0.0,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precisao": round(precision, 3),
        "revocacao": round(recall, 3),
        "aviso": (
            "amostra pequena demais para concluir qualquer coisa"
            if positives < 10
            else "rotulo negativo = 'ainda nao banido', nao 'limpo'"
        ),
    }


def sweep_thresholds(store, steps=(30, 40, 50, 55, 60, 70, 80)) -> list:
    return [evaluate_threshold(store, t) for t in steps]
