"""Analises que so existem depois de muitas partidas no banco.

Tres coisas que uma partida isolada nao consegue responder:

- **calibracao**: meus limiares fazem sentido para a MINHA populacao? Os
  valores padrao sao palpite; a distribuicao real dos seus sinais, no seu elo,
  no seu servidor, e a unica referencia honesta. Daqui sai um limiar derivado
  de percentil em vez de chute.
- **reincidencia**: um score alto numa partida e ruido. O mesmo jogador alto
  em cinco partidas e outra coisa. Agrega por jogador com peso por amostra.
- **grupos**: jogadores que aparecem sempre juntos. Nao prova nada sozinho -
  amigos jogam juntos - mas um grupo fixo em que varios pontuam alto e o
  padrao de quem sobe conta em bando.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict


def _percentile(values: list, p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * p
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return ordered[int(index)]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


# ------------------------------------------------------------- calibracao


def signal_distribution(store, min_samples: int = 30) -> dict:
    """Percentis de cada sinal na SUA base.

    Um sinal cuja mediana ja e alta na sua populacao nao esta detectando
    trapaca - esta detectando o seu jogo, o seu tickrate ou um vies do
    detector. Isso aparece aqui e em nenhum outro lugar.
    """
    buckets: dict = defaultdict(list)
    scores = []
    for row in store.labeled_dataset():
        scores.append(float(row.get("score") or 0.0))
        for name, entry in (row.get("signals") or {}).items():
            if isinstance(entry, dict):
                buckets[name].append(float(entry.get("value") or 0.0))

    out = {"observacoes": len(scores), "sinais": {}}
    if scores:
        out["score"] = {
            "mediana": round(_percentile(scores, 0.5), 1),
            "p90": round(_percentile(scores, 0.90), 1),
            "p99": round(_percentile(scores, 0.99), 1),
            "maximo": round(max(scores), 1),
        }
    for name, values in sorted(buckets.items()):
        if len(values) < min_samples:
            out["sinais"][name] = {"amostras": len(values),
                                   "aviso": "amostra insuficiente"}
            continue
        nonzero = [v for v in values if v > 0.01]
        out["sinais"][name] = {
            "amostras": len(values),
            "fracao_nao_zero": round(len(nonzero) / len(values), 3),
            "mediana": round(_percentile(values, 0.5), 3),
            "p90": round(_percentile(values, 0.90), 3),
            "p99": round(_percentile(values, 0.99), 3),
            "maximo": round(max(values), 3),
        }
    return out


def suggest_threshold(store, percentile: float = 0.99,
                      min_observations: int = 200) -> dict:
    """Limiar de revisao derivado da sua propria distribuicao.

    A logica: voce tem tempo para assistir a uma fracao pequena das partidas.
    Se quer revisar o 1% mais alto, o limiar e o percentil 99 do seu historico
    - nao um numero redondo que alguem escolheu.
    """
    scores = [float(r.get("score") or 0.0) for r in store.labeled_dataset()]
    if len(scores) < min_observations:
        return {
            "erro": (f"so {len(scores)} observacoes; abaixo de "
                     f"{min_observations} o percentil e instavel demais para "
                     f"virar limiar"),
            "observacoes": len(scores),
        }
    valor = _percentile(scores, percentile)
    acima = sum(1 for s in scores if s >= valor)
    return {
        "percentil": percentile,
        "limiar_sugerido": round(valor, 1),
        "observacoes": len(scores),
        "acima_do_limiar": acima,
        "fracao": round(acima / len(scores), 4),
        "aviso": ("isto calibra QUANTOS voce vai revisar, nao quantos estao "
                  "trapaceando; use `csradar eval` com bans para saber se o "
                  "limiar acerta"),
    }


# ------------------------------------------------------------ reincidencia


def player_history(store, steamid: int) -> dict:
    """Trajetoria de um jogador ao longo das partidas."""
    rows = store.observations_for(int(steamid))
    if not rows:
        return {"steamid": int(steamid), "partidas": 0}

    entradas = []
    for row in rows:
        signals = json.loads(row["signals"]) if row.get("signals") else {}
        entradas.append({
            "fonte": row.get("source", ""),
            "mapa": row.get("map_name", ""),
            "quando": row.get("analyzed_at"),
            "score": row.get("score"),
            "kills": row.get("kills"),
            "deaths": row.get("deaths"),
            "sinais": {k: v.get("value") for k, v in signals.items()
                       if isinstance(v, dict)},
        })

    scores = [e["score"] for e in entradas if e["score"] is not None]
    ban = store.conn.execute(
        "SELECT MAX(vac_banned) v, MAX(game_bans) g, MIN(days_since_ban) d "
        "FROM ban_checks WHERE steamid = ?", (int(steamid),)
    ).fetchone()

    return {
        "steamid": int(steamid),
        "nome": rows[0].get("name", ""),
        "partidas": len(entradas),
        "score_medio": round(sum(scores) / len(scores), 1) if scores else 0.0,
        "score_maximo": round(max(scores), 1) if scores else 0.0,
        "score_mediano": round(_percentile(scores, 0.5), 1) if scores else 0.0,
        "banido": bool(ban and ((ban["v"] or 0) or (ban["g"] or 0))),
        "dias_desde_ban": ban["d"] if ban else None,
        "entradas": entradas,
    }


def recurring_suspects(store, min_matches: int = 3,
                       min_mean: float = 35.0) -> list:
    """Quem pontua alto de forma CONSISTENTE.

    Ordena por media, mas exige um minimo de partidas: uma pontuacao alta
    isolada e ruido, e a lista de suspeitos nao deveria ser dominada por quem
    voce viu uma vez.
    """
    rows = store.conn.execute(
        """
        SELECT o.steamid,
               MAX(o.name)  AS nome,
               COUNT(*)     AS partidas,
               AVG(o.score) AS media,
               MAX(o.score) AS maximo,
               MIN(o.score) AS minimo,
               SUM(o.kills) AS kills
        FROM observations o
        GROUP BY o.steamid
        HAVING COUNT(*) >= ? AND AVG(o.score) >= ?
        ORDER BY media DESC, partidas DESC
        """,
        (min_matches, min_mean),
    ).fetchall()

    out = []
    for row in rows:
        data = dict(row)
        # consistencia: media alta com minimo alto vale mais que media alta
        # puxada por um pico
        spread = (data["maximo"] or 0) - (data["minimo"] or 0)
        data["consistencia"] = round(
            max(0.0, 1.0 - spread / max(data["maximo"] or 1.0, 1.0)), 3)
        data["media"] = round(data["media"] or 0.0, 1)
        data["maximo"] = round(data["maximo"] or 0.0, 1)
        data["minimo"] = round(data["minimo"] or 0.0, 1)
        out.append(data)
    return out


# ------------------------------------------------------------------ grupos


def co_occurrence(store, min_together: int = 3, min_score: float = 0.0) -> list:
    """Pares de jogadores que aparecem juntos com frequencia.

    Amigos jogam juntos - isso sozinho nao significa nada. O que interessa e
    o cruzamento: um par que sempre aparece junto E em que os dois pontuam
    alto. Por isso `min_score` filtra por media antes de contar o par.
    """
    rows = store.conn.execute(
        "SELECT match_id, steamid, name, score, team FROM observations"
    ).fetchall()

    by_match: dict = defaultdict(list)
    names: dict = {}
    scores: dict = defaultdict(list)
    for row in rows:
        by_match[row["match_id"]].append((row["steamid"], row["team"]))
        names[row["steamid"]] = row["name"]
        scores[row["steamid"]].append(float(row["score"] or 0.0))

    means = {sid: sum(v) / len(v) for sid, v in scores.items() if v}
    pairs: dict = defaultdict(int)
    same_team: dict = defaultdict(int)

    for players in by_match.values():
        players.sort()
        for i in range(len(players)):
            for j in range(i + 1, len(players)):
                a, team_a = players[i]
                b, team_b = players[j]
                pairs[(a, b)] += 1
                if team_a and team_a == team_b:
                    same_team[(a, b)] += 1

    out = []
    for (a, b), count in pairs.items():
        if count < min_together:
            continue
        mean_a, mean_b = means.get(a, 0.0), means.get(b, 0.0)
        if min(mean_a, mean_b) < min_score:
            continue
        out.append({
            "a": a, "nome_a": names.get(a, ""), "media_a": round(mean_a, 1),
            "b": b, "nome_b": names.get(b, ""), "media_b": round(mean_b, 1),
            "juntos": count,
            "mesmo_time": same_team.get((a, b), 0),
            "media_do_par": round((mean_a + mean_b) / 2, 1),
        })
    out.sort(key=lambda d: (-d["media_do_par"], -d["juntos"]))
    return out


def explain(store, steamid: int, source: str = "") -> dict:
    """Tudo o que o banco sabe sobre um jogador numa partida especifica."""
    rows = store.observations_for(int(steamid))
    if not rows:
        return {"erro": f"nenhuma observacao para {steamid}"}
    row = None
    if source:
        row = next((r for r in rows if source.lower() in
                    str(r.get("source", "")).lower()), None)
        if row is None:
            return {"erro": f"{steamid} nao aparece em nenhuma fonte "
                            f"contendo '{source}'"}
    else:
        row = max(rows, key=lambda r: r.get("score") or 0)

    signals = json.loads(row["signals"]) if row.get("signals") else {}
    evidence = json.loads(row["evidence"]) if row.get("evidence") else []
    return {
        "steamid": int(steamid),
        "nome": row.get("name", ""),
        "fonte": row.get("source", ""),
        "mapa": row.get("map_name", ""),
        "score": row.get("score"),
        "kills": row.get("kills"),
        "deaths": row.get("deaths"),
        "headshots": row.get("headshots"),
        "sinais": signals,
        "evidencias": evidence,
        "outras_partidas": len(rows) - 1,
    }


# --------------------------------------------------------------- manutencao


def prune(store, keep_matches: int = 500, dry_run: bool = True) -> dict:
    """Descarta as partidas mais antigas, preservando o que vira rotulo.

    O banco cresce sem limite e um projeto mantido por uma pessoa nao deveria
    precisar administrar isso. Regra: joga fora observacao antiga, mas NUNCA
    joga fora `ban_checks` nem quem esta na watchlist - e exatamente esse
    historico que vale mais com o tempo.
    """
    total = store.conn.execute(
        "SELECT COUNT(*) n FROM matches").fetchone()["n"]
    if total <= keep_matches:
        return {"partidas": total, "removidas": 0,
                "motivo": "abaixo do limite"}

    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM matches ORDER BY analyzed_at DESC LIMIT -1 OFFSET ?",
        (keep_matches,)).fetchall()]
    if not ids:
        return {"partidas": total, "removidas": 0}

    protegidos = set()
    try:
        protegidos = {r["steamid"] for r in store.conn.execute(
            "SELECT steamid FROM watchlist").fetchall()}
    except Exception:      # noqa: BLE001 - watchlist pode nao existir ainda
        protegidos = set()

    obs = store.conn.execute(
        f"SELECT COUNT(*) n FROM observations WHERE match_id IN "
        f"({','.join('?' * len(ids))})", ids).fetchone()["n"]

    if dry_run:
        return {"partidas": total, "removeria": len(ids),
                "observacoes_afetadas": obs,
                "jogadores_protegidos": len(protegidos),
                "aviso": "nada foi removido; use --confirm"}

    placeholders = ",".join("?" * len(ids))
    if protegidos:
        keep = ",".join("?" * len(protegidos))
        store.conn.execute(
            f"DELETE FROM observations WHERE match_id IN ({placeholders}) "
            f"AND steamid NOT IN ({keep})", (*ids, *protegidos))
    else:
        store.conn.execute(
            f"DELETE FROM observations WHERE match_id IN ({placeholders})", ids)
    store.conn.execute(
        f"DELETE FROM matches WHERE id IN ({placeholders}) AND id NOT IN "
        f"(SELECT DISTINCT match_id FROM observations)", ids)
    store.conn.commit()
    store.conn.execute("VACUUM")
    return {"partidas": total, "removidas": len(ids),
            "observacoes_removidas": obs,
            "jogadores_protegidos": len(protegidos)}
