"""Lista de observacao e vigia de pasta.

Duas funcionalidades que so fazem sentido depois que o programa roda ha algum
tempo:

- watchlist: voce marca um SteamID e passa a ser avisado toda vez que ele
  reaparece numa partida sua, com o historico de score. E assim que um
  suspeito vira caso: nao por uma partida, mas por reincidencia.
- daemon: fica de olho na pasta de replays e analisa sozinho o que aparecer.
  Sem isso, a etapa manual "lembrar de rodar o analisador" e onde o processo
  morre na pratica.
"""

from __future__ import annotations

import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    steamid    INTEGER PRIMARY KEY,
    label      TEXT,
    reason     TEXT,
    added_at   INTEGER NOT NULL,
    last_seen  INTEGER,
    times_seen INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_files (
    path        TEXT PRIMARY KEY,
    size        INTEGER,
    analyzed_at INTEGER NOT NULL,
    match_id    INTEGER
);
"""


def ensure_schema(store) -> None:
    store.conn.executescript(SCHEMA)
    store.conn.commit()


# --------------------------------------------------------------- watchlist


def add(store, steamid: int, label: str = "", reason: str = "") -> bool:
    """Devolve True se entrou agora, False se ja estava na lista."""
    ensure_schema(store)
    existing = store.conn.execute(
        "SELECT steamid FROM watchlist WHERE steamid = ?", (int(steamid),)
    ).fetchone()
    store.conn.execute(
        "INSERT INTO watchlist (steamid, label, reason, added_at) "
        "VALUES (?,?,?,?) ON CONFLICT(steamid) DO UPDATE SET "
        "label=excluded.label, reason=excluded.reason",
        (int(steamid), label, reason, int(time.time())),
    )
    store.conn.commit()
    return existing is None


def remove(store, steamid: int) -> bool:
    ensure_schema(store)
    cur = store.conn.execute("DELETE FROM watchlist WHERE steamid = ?",
                             (int(steamid),))
    store.conn.commit()
    return cur.rowcount > 0


def entries(store) -> list:
    """A lista, enriquecida com o que o banco sabe de cada um."""
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT w.steamid, w.label, w.reason, w.added_at, w.times_seen,
               (SELECT MAX(o.name) FROM observations o
                 WHERE o.steamid = w.steamid)              AS nome,
               (SELECT COUNT(*) FROM observations o
                 WHERE o.steamid = w.steamid)              AS partidas,
               (SELECT AVG(o.score) FROM observations o
                 WHERE o.steamid = w.steamid)              AS score_medio,
               (SELECT MAX(o.score) FROM observations o
                 WHERE o.steamid = w.steamid)              AS score_max,
               (SELECT MAX(b.vac_banned + b.game_bans) FROM ban_checks b
                 WHERE b.steamid = w.steamid)              AS bans
        FROM watchlist w
        ORDER BY score_medio DESC NULLS LAST, w.added_at ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def check_report(store, report) -> list:
    """Cruza um relatorio com a watchlist e devolve os encontros."""
    ensure_schema(store)
    watched = {int(r["steamid"]): r for r in entries(store)}
    if not watched:
        return []
    hits = []
    now = int(time.time())
    for player in report.players:
        entry = watched.get(int(player.steamid))
        if entry is None:
            continue
        store.conn.execute(
            "UPDATE watchlist SET last_seen = ?, times_seen = times_seen + 1 "
            "WHERE steamid = ?", (now, int(player.steamid)),
        )
        hits.append({
            "steamid": player.steamid,
            "nome": player.name,
            "rotulo": entry.get("label") or "",
            "motivo": entry.get("reason") or "",
            "score_agora": player.score,
            "score_medio": entry.get("score_medio"),
            "partidas_anteriores": entry.get("partidas") or 0,
            "banido": bool(entry.get("bans")),
        })
    store.conn.commit()
    return hits


def auto_add_from_report(store, report, cfg, reason: str = "") -> list:
    """Coloca na lista quem passou do limiar de revisao."""
    added = []
    for player in report.sorted_players():
        if player.score < cfg.scoring.review_threshold:
            continue
        if "amostra_pequena" in player.flags:
            continue
        if add(store, player.steamid, label=player.name,
               reason=reason or f"score {player.score:.0f} em {report.source}"):
            added.append(player)
    return added


# ------------------------------------------------------------------ daemon


ANALYZABLE = (".dem", ".log", ".json", ".jsonl", ".ndjson", ".csv")


def pending_files(store, folder, extensions=ANALYZABLE) -> list:
    """Arquivos da pasta que ainda nao foram analisados.

    A chave e (caminho, tamanho): uma demo ainda sendo baixada tem tamanho
    diferente da final, entao ela sera reanalisada quando terminar em vez de
    ficar marcada como pronta pela metade.
    """
    ensure_schema(store)
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"pasta nao encontrada: {root}")

    known = {
        row["path"]: row["size"]
        for row in store.conn.execute(
            "SELECT path, size FROM seen_files").fetchall()
    }
    out = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if known.get(str(path)) == size:
            continue
        out.append(path)
    return out


def mark_analyzed(store, path, match_id=None) -> None:
    ensure_schema(store)
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    store.conn.execute(
        "INSERT INTO seen_files (path, size, analyzed_at, match_id) "
        "VALUES (?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
        "size=excluded.size, analyzed_at=excluded.analyzed_at, "
        "match_id=excluded.match_id",
        (str(p), size, int(time.time()), match_id),
    )
    store.conn.commit()


def is_stable(path, wait: float = 1.5) -> bool:
    """O arquivo parou de crescer? Evita analisar download pela metade."""
    p = Path(path)
    try:
        first = p.stat().st_size
    except OSError:
        return False
    time.sleep(wait)
    try:
        return p.stat().st_size == first and first > 0
    except OSError:
        return False
