"""Steam Web API: score de risco de conta e rotulo retroativo de ban.

Isto NAO detecta cheat. Detecta perfil de risco (conta nova, privada, poucas
horas, bans anteriores) e, semanas depois, confirma quem a Valve baniu - que e
a unica fonte de rotulo confiavel que voce tem.

Requer uma chave gratuita em https://steamcommunity.com/dev/apikey e a
biblioteca `requests` (pip install requests).
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

BASE = "https://api.steampowered.com"
CS2_APPID = 730
STEAM64_BASE = 76561197960265728

_STEAM_ID_RE = re.compile(r"STEAM_[0-5]:([01]):(\d+)", re.IGNORECASE)
_STEAM3_RE = re.compile(r"\[?U:1:(\d+)\]?", re.IGNORECASE)
_STEAM64_RE = re.compile(r"\b(7656119\d{10})\b")


class SteamError(RuntimeError):
    pass


# --------------------------------------------------------------- conversao de id


def to_steam64(value) -> int | None:
    """Aceita STEAM_1:0:123, [U:1:246], 7656119..., ou o proprio inteiro."""
    if isinstance(value, int):
        return value if value > STEAM64_BASE else None
    text = str(value).strip()
    m = _STEAM64_RE.search(text)
    if m:
        return int(m.group(1))
    m = _STEAM_ID_RE.search(text)
    if m:
        return STEAM64_BASE + int(m.group(2)) * 2 + int(m.group(1))
    m = _STEAM3_RE.search(text)
    if m:
        return STEAM64_BASE + int(m.group(1))
    if text.isdigit():
        n = int(text)
        return n if n > STEAM64_BASE else STEAM64_BASE + n
    return None


def to_steam3(steam64: int) -> str:
    return f"[U:1:{steam64 - STEAM64_BASE}]"


def profile_url(steam64: int) -> str:
    return f"https://steamcommunity.com/profiles/{steam64}"


# ------------------------------------------------------------------- cliente


class SteamClient:
    def __init__(self, api_key: str, timeout: float = 15.0, min_interval: float = 1.1):
        if not api_key:
            raise SteamError(
                "chave da Steam Web API ausente. Pegue em "
                "https://steamcommunity.com/dev/apikey e exporte STEAM_API_KEY, "
                "ou rode: csradar config --steam-key SUACHAVE"
            )
        self.api_key = api_key
        self.timeout = timeout
        self.min_interval = min_interval
        self._last_call = 0.0

    def _get(self, path: str, **params) -> dict:
        self._throttle()
        params["key"] = self.api_key
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "cs-cheat-radar/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise SteamError("chave da Steam rejeitada (403)") from exc
            if exc.code == 429:
                raise SteamError("limite de requisicoes da Steam atingido (429)") from exc
            raise SteamError(f"Steam respondeu {exc.code} em {path}") from exc
        except urllib.error.URLError as exc:
            raise SteamError(f"falha de rede ao chamar a Steam: {exc.reason}") from exc

    def _throttle(self) -> None:
        delta = time.time() - self._last_call
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last_call = time.time()

    # ------------------------------------------------------------ endpoints

    def bans(self, steamids) -> dict:
        """steamid -> dict de bans. Aceita ate 100 ids por chamada."""
        out = {}
        ids = [int(s) for s in steamids]
        for chunk in _chunks(ids, 100):
            data = self._get(
                "ISteamUser/GetPlayerBans/v1/",
                steamids=",".join(str(i) for i in chunk),
            )
            for entry in data.get("players", []):
                sid = int(entry.get("SteamId", 0))
                if sid:
                    out[sid] = entry
        return out

    def summaries(self, steamids) -> dict:
        out = {}
        ids = [int(s) for s in steamids]
        for chunk in _chunks(ids, 100):
            data = self._get(
                "ISteamUser/GetPlayerSummaries/v2/",
                steamids=",".join(str(i) for i in chunk),
            )
            for entry in data.get("response", {}).get("players", []):
                sid = int(entry.get("steamid", 0))
                if sid:
                    out[sid] = entry
        return out

    def cs2_hours(self, steamid: int) -> float | None:
        """Horas em CS2. None se o perfil de jogos for privado."""
        try:
            data = self._get(
                "IPlayerService/GetOwnedGames/v1/",
                steamid=steamid,
                include_played_free_games=1,
                appids_filter=CS2_APPID,
                format="json",
            )
        except SteamError:
            return None
        games = data.get("response", {}).get("games") or []
        for g in games:
            if int(g.get("appid", 0)) == CS2_APPID:
                return round(float(g.get("playtime_forever", 0)) / 60.0, 1)
        return None

    def friends_banned(self, steamid: int) -> tuple:
        """(amigos_banidos, total_amigos). (0, 0) se a lista for privada."""
        try:
            data = self._get(
                "ISteamUser/GetFriendList/v1/", steamid=steamid, relationship="friend"
            )
        except SteamError:
            return 0, 0
        friends = data.get("friendslist", {}).get("friends") or []
        ids = [int(f["steamid"]) for f in friends if f.get("steamid")]
        if not ids:
            return 0, 0
        banned = 0
        for sid, info in self.bans(ids[:300]).items():
            if info.get("VACBanned") or int(info.get("NumberOfGameBans", 0) or 0) > 0:
                banned += 1
        return banned, len(ids)


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


# --------------------------------------------------------------- score de risco


def risk_profile(client: SteamClient, steamid: int, deep: bool = False) -> dict:
    """Perfil de risco de UMA conta. Nao e acusacao de cheat."""
    sid = int(steamid)
    summary = client.summaries([sid]).get(sid, {})
    ban = client.bans([sid]).get(sid, {})

    created = summary.get("timecreated")
    age_days = int((time.time() - created) / 86400) if created else None
    visibility = int(summary.get("communityvisibilitystate", 1) or 1)
    private = visibility != 3

    hours = client.cs2_hours(sid) if deep else None
    friends_banned = friends_total = 0
    if deep:
        friends_banned, friends_total = client.friends_banned(sid)

    reasons = []
    score = 0.0
    if ban.get("VACBanned"):
        score += 45
        reasons.append(f"VAC ban ha {ban.get('DaysSinceLastBan', '?')} dias")
    game_bans = int(ban.get("NumberOfGameBans", 0) or 0)
    if game_bans:
        score += 35
        reasons.append(f"{game_bans} game ban(s)")
    if age_days is not None and age_days < 60:
        score += 25
        reasons.append(f"conta com {age_days} dias")
    elif age_days is not None and age_days < 365:
        score += 10
        reasons.append(f"conta com {age_days} dias")
    if private:
        score += 15
        reasons.append("perfil privado")
    if hours is not None and hours < 50:
        score += 20
        reasons.append(f"{hours} h em CS2")
    if friends_total and friends_banned / friends_total > 0.15:
        score += 20
        reasons.append(f"{friends_banned}/{friends_total} amigos banidos")

    return {
        "steamid": sid,
        "nome": summary.get("personaname", ""),
        "perfil": profile_url(sid),
        "idade_conta_dias": age_days,
        "perfil_privado": private,
        "horas_cs2": hours,
        "vac_banned": bool(ban.get("VACBanned")),
        "game_bans": game_bans,
        "dias_desde_ban": ban.get("DaysSinceLastBan"),
        "amigos_banidos": friends_banned,
        "amigos_total": friends_total,
        "risco": min(100.0, round(score, 1)),
        "motivos": reasons,
    }
