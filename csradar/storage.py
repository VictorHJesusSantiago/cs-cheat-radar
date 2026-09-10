"""Persistencia em SQLite.

O banco existe por um motivo so: sem historico nao ha rotulo. As features de
hoje so viram dataset quando cruzadas com os bans que a Valve aplicar daqui a
30, 60 ou 90 dias.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import default_home

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL UNIQUE,
    map_name    TEXT,
    tickrate    REAL,
    analyzed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id   INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
    steamid    INTEGER NOT NULL,
    name       TEXT,
    team       INTEGER,
    kills      INTEGER,
    deaths     INTEGER,
    headshots  INTEGER,
    shots      INTEGER,
    score      REAL,
    signals    TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    UNIQUE(match_id, steamid)
);

CREATE TABLE IF NOT EXISTS players (
    steamid       INTEGER PRIMARY KEY,
    last_name     TEXT,
    first_seen    INTEGER,
    last_seen     INTEGER,
    matches_seen  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ban_checks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    steamid        INTEGER NOT NULL,
    checked_at     INTEGER NOT NULL,
    vac_banned     INTEGER,
    game_bans      INTEGER,
    days_since_ban INTEGER,
    community_ban  INTEGER,
    economy_ban    TEXT,
    raw            TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_steamid ON observations(steamid);
CREATE INDEX IF NOT EXISTS idx_ban_steamid ON ban_checks(steamid);
"""


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_home() / "csradar.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ escrita

    def save_report(self, report, replace: bool = True) -> int:
        now = int(time.time())
        cur = self.conn.cursor()
        row = cur.execute(
            "SELECT id FROM matches WHERE source = ?", (report.source,)
        ).fetchone()
        if row:
            match_id = row["id"]
            if not replace:
                return match_id
            cur.execute("DELETE FROM observations WHERE match_id = ?", (match_id,))
            cur.execute(
                "UPDATE matches SET map_name=?, tickrate=?, analyzed_at=? WHERE id=?",
                (report.map_name, report.tickrate, now, match_id),
            )
        else:
            cur.execute(
                "INSERT INTO matches (source, map_name, tickrate, analyzed_at) "
                "VALUES (?, ?, ?, ?)",
                (report.source, report.map_name, report.tickrate, now),
            )
            match_id = cur.lastrowid

        for p in report.players:
            data = p.as_dict()
            cur.execute(
                "INSERT OR REPLACE INTO observations "
                "(match_id, steamid, name, team, kills, deaths, headshots, shots, "
                " score, signals, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match_id, p.steamid, p.name, p.team, p.kills, p.deaths,
                    p.headshots, p.shots, p.score,
                    json.dumps(data["signals"], ensure_ascii=False),
                    json.dumps(data["evidence"], ensure_ascii=False),
                ),
            )
            cur.execute(
                "INSERT INTO players (steamid, last_name, first_seen, last_seen, "
                "matches_seen) VALUES (?,?,?,?,1) "
                "ON CONFLICT(steamid) DO UPDATE SET last_name=excluded.last_name, "
                "last_seen=excluded.last_seen, matches_seen=matches_seen+1",
                (p.steamid, p.name, now, now),
            )
        self.conn.commit()
        return match_id

    def save_ban_check(self, steamid: int, info: dict) -> None:
        self.conn.execute(
            "INSERT INTO ban_checks (steamid, checked_at, vac_banned, game_bans, "
            "days_since_ban, community_ban, economy_ban, raw) VALUES (?,?,?,?,?,?,?,?)",
            (
                steamid,
                int(time.time()),
                int(bool(info.get("VACBanned"))),
                int(info.get("NumberOfGameBans", 0) or 0),
                int(info.get("DaysSinceLastBan", 0) or 0),
                int(bool(info.get("CommunityBanned"))),
                str(info.get("EconomyBan", "")),
                json.dumps(info, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ leitura

    def top_suspects(self, limit: int = 20, min_score: float = 0.0) -> list:
        rows = self.conn.execute(
            """
            SELECT o.steamid,
                   MAX(o.name)      AS name,
                   COUNT(*)         AS partidas,
                   AVG(o.score)     AS score_medio,
                   MAX(o.score)     AS score_max,
                   SUM(o.kills)     AS kills
            FROM observations o
            GROUP BY o.steamid
            HAVING AVG(o.score) >= ?
            ORDER BY score_medio DESC, score_max DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def observations_for(self, steamid: int) -> list:
        rows = self.conn.execute(
            "SELECT o.*, m.source, m.map_name, m.analyzed_at FROM observations o "
            "JOIN matches m ON m.id = o.match_id WHERE o.steamid = ? "
            "ORDER BY m.analyzed_at DESC",
            (steamid,),
        ).fetchall()
        return [dict(r) for r in rows]

    def steamids_pending_recheck(self, min_age_days: int, cooldown_days: int = 7) -> list:
        """Jogadores vistos ha tempo suficiente e ainda nao reconsultados agora."""
        now = int(time.time())
        cutoff = now - min_age_days * 86400
        cooldown = now - cooldown_days * 86400
        rows = self.conn.execute(
            """
            SELECT p.steamid FROM players p
            WHERE p.first_seen <= ?
              AND NOT EXISTS (
                    SELECT 1 FROM ban_checks b
                    WHERE b.steamid = p.steamid AND b.checked_at >= ?
              )
            ORDER BY p.first_seen ASC
            """,
            (cutoff, cooldown),
        ).fetchall()
        return [r["steamid"] for r in rows]

    def labeled_dataset(self) -> list:
        """Features + rotulo retroativo, prontas para treinar.

        Rotulo positivo = o jogador levou VAC/game ban DEPOIS de ter sido visto.
        Rotulo negativo e "ainda nao banido", nao "limpo": nem todo cheater e
        pego, e o ban demora. Trate como ruido no rotulo, nao como verdade.
        """
        rows = self.conn.execute(
            """
            SELECT o.steamid, o.name, o.score, o.kills, o.signals,
                   m.source, m.analyzed_at,
                   (SELECT MAX(b.vac_banned + b.game_bans) FROM ban_checks b
                     WHERE b.steamid = o.steamid) AS bans
            FROM observations o JOIN matches m ON m.id = o.match_id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["signals"] = json.loads(d["signals"])
            d["label"] = 1 if (d.pop("bans") or 0) > 0 else 0
            out.append(d)
        return out

    def stats(self) -> dict:
        c = self.conn.execute
        return {
            "partidas": c("SELECT COUNT(*) n FROM matches").fetchone()["n"],
            "observacoes": c("SELECT COUNT(*) n FROM observations").fetchone()["n"],
            "jogadores": c("SELECT COUNT(*) n FROM players").fetchone()["n"],
            "consultas_de_ban": c("SELECT COUNT(*) n FROM ban_checks").fetchone()["n"],
            "banidos": c(
                "SELECT COUNT(DISTINCT steamid) n FROM ban_checks "
                "WHERE vac_banned = 1 OR game_bans > 0"
            ).fetchone()["n"],
            "banco": str(self.path),
        }
