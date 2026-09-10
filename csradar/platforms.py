"""Fontes de rotulo e risco fora da Steam.

O `recheck` da Steam so enxerga VAC e game bans. Boa parte do CS competitivo
acontece na FACEIT, que bane por conta propria e expoe isso publicamente; e
servidores de sobrevivencia (Rust, ARK, DayZ) usam Battlemetrics, que agrega
bans de milhares de servidores.

Cada cliente aqui e pequeno de proposito e usa apenas urllib - o projeto nao
deve exigir `requests` para tres chamadas HTTP. Todos falham de forma explicita
quando a chave nao existe, em vez de silenciosamente devolver vazio.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "cs-cheat-radar/3.0"


class PlatformError(RuntimeError):
    pass


class _Client:
    """Base HTTP com throttle e erros legiveis."""

    base = ""
    name = "plataforma"

    def __init__(self, api_key: str = "", timeout: float = 15.0,
                 min_interval: float = 0.35):
        self.api_key = api_key
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0

    def _headers(self) -> dict:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _fetch(self, path: str, **params) -> bytes:
        """Uma requisicao GET, com throttle e erro legivel. Devolve bytes
        porque nem toda plataforma responde JSON."""
        delta = time.time() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.time()

        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PlatformError(
                    f"{self.name}: chave rejeitada ({exc.code})") from exc
            if exc.code == 404:
                raise PlatformError(f"{self.name}: nao encontrado") from exc
            if exc.code == 429:
                raise PlatformError(
                    f"{self.name}: limite de requisicoes atingido") from exc
            raise PlatformError(f"{self.name}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise PlatformError(f"{self.name}: falha de rede ({exc.reason})") from exc

    def _get(self, path: str, **params) -> dict:
        raw = self._fetch(path, **params)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PlatformError(f"{self.name}: resposta nao e JSON") from exc


# ------------------------------------------------------------------ FACEIT


class FaceitClient(_Client):
    """FACEIT Data API v4.

    Chave gratuita em developers.faceit.com. O que interessa aqui: dado um
    SteamID, achar o jogador e ver se a conta esta banida na plataforma - um
    rotulo independente do VAC, e em geral mais rapido que ele.
    """

    base = "https://open.faceit.com/data/v4"
    name = "FACEIT"

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave da FACEIT ausente. Pegue em developers.faceit.com e "
                "rode: csradar config --faceit-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def player_by_steam(self, steam64: int, game: str = "cs2") -> dict | None:
        try:
            return self._get("/players", game=game, game_player_id=str(steam64))
        except PlatformError as exc:
            if "nao encontrado" in str(exc):
                return None
            raise

    def bans(self, player_id: str) -> list:
        try:
            data = self._get(f"/players/{player_id}/bans")
        except PlatformError as exc:
            if "nao encontrado" in str(exc):
                return []
            raise
        return data.get("items") or []

    def profile(self, steam64: int, game: str = "cs2") -> dict:
        """Perfil resumido, no mesmo formato do risco da Steam."""
        player = self.player_by_steam(steam64, game)
        if not player:
            return {"steamid": steam64, "encontrado": False,
                    "motivos": ["sem conta FACEIT vinculada"], "risco": 0.0}

        bans = self.bans(player.get("player_id", ""))
        games = player.get("games") or {}
        cs = games.get(game) or {}
        elo = cs.get("faceit_elo")
        level = cs.get("skill_level")

        reasons = []
        risk = 0.0
        if bans:
            risk += 60
            reasons.append(f"{len(bans)} ban(s) na FACEIT")
        if player.get("memberships") and "free" in player["memberships"]:
            pass
        created = player.get("activated_at")
        return {
            "steamid": steam64,
            "encontrado": True,
            "nickname": player.get("nickname", ""),
            "player_id": player.get("player_id", ""),
            "pais": player.get("country", ""),
            "elo": elo,
            "nivel": level,
            "criado_em": created,
            "bans": [
                {"motivo": b.get("reason", ""), "tipo": b.get("type", ""),
                 "em": b.get("starts_at", "")}
                for b in bans
            ],
            "risco": min(100.0, risk),
            "motivos": reasons,
        }


# ----------------------------------------------------------- Battlemetrics


class BattlemetricsClient(_Client):
    """Battlemetrics: bans agregados de servidores de comunidade.

    Util para Rust, ARK, DayZ e afins, onde nao existe VAC valendo grande
    coisa e a punicao real e o ban de servidor. A API tem parte publica; a
    busca de bans exige token.
    """

    base = "https://api.battlemetrics.com"
    name = "Battlemetrics"

    def player_bans(self, steam64: int) -> list:
        if not self.api_key:
            raise PlatformError(
                "Battlemetrics exige token. Gere em battlemetrics.com "
                "(Developers) e rode: csradar config --battlemetrics-key TOKEN"
            )
        data = self._get(
            "/bans",
            **{"filter[search]": str(steam64), "page[size]": 50},
        )
        out = []
        for item in data.get("data") or []:
            attrs = item.get("attributes") or {}
            out.append({
                "motivo": attrs.get("reason", ""),
                "nota": attrs.get("note", ""),
                "em": attrs.get("timestamp", ""),
                "expira": attrs.get("expires"),
            })
        return out

    def risk(self, steam64: int) -> dict:
        bans = self.player_bans(steam64)
        risk = min(100.0, 25.0 * len(bans))
        return {
            "steamid": steam64,
            "bans": bans,
            "risco": risk,
            "motivos": ([f"{len(bans)} ban(s) em servidores de comunidade"]
                        if bans else []),
        }


# -------------------------------------------------------------------- PUBG


class PubgClient(_Client):
    """PUBG Developer API: partidas e telemetria.

    A telemetria e um JSON publico (a URL vem dentro da resposta da partida e
    nao exige chave para baixar). E a fonte que alimenta o conversor
    `csradar convert --from pubg`.
    """

    base = "https://api.pubg.com"
    name = "PUBG"

    def __init__(self, api_key: str, shard: str = "steam", timeout: float = 20.0):
        if not api_key:
            raise PlatformError(
                "chave da PUBG ausente. Pegue em developer.pubg.com e rode: "
                "csradar config --pubg-key SUACHAVE"
            )
        super().__init__(api_key, timeout, min_interval=6.5)  # 10 req/min
        self.shard = shard

    def _headers(self) -> dict:
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.api+json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def player_matches(self, player_name: str) -> list:
        data = self._get(f"/shards/{self.shard}/players",
                         **{"filter[playerNames]": player_name})
        items = data.get("data") or []
        if not items:
            return []
        rel = (items[0].get("relationships") or {}).get("matches") or {}
        return [m.get("id") for m in (rel.get("data") or []) if m.get("id")]

    def telemetry_url(self, match_id: str) -> str | None:
        data = self._get(f"/shards/{self.shard}/matches/{match_id}")
        for item in data.get("included") or []:
            if item.get("type") == "asset":
                url = (item.get("attributes") or {}).get("URL")
                if url:
                    return url
        return None

    def download_telemetry(self, url: str, out_path) -> str:
        from pathlib import Path

        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT,
                          "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip

                    raw = gzip.decompress(raw)
        except urllib.error.URLError as exc:
            raise PlatformError(f"PUBG: falha ao baixar telemetria ({exc})") from exc
        out = Path(out_path)
        out.write_bytes(raw)
        return str(out)


# ------------------------------------------------------- Steam Community


class SteamCommunityClient(_Client):
    """Perfil publico da Steam em XML - sem chave de API nenhuma.

    Existe porque a Web API da Valve exige chave e nem todo mundo tem uma. O
    `?xml=1` do perfil devolve `vacBanned`, visibilidade e data de criacao, que
    e a maior parte do que o `risk` calcula. Nao substitui a Web API (nao tem
    game bans nem horas), mas tira o programa do zero para quem so quer olhar
    uma conta.
    """

    base = "https://steamcommunity.com"
    name = "Steam Community"

    def profile(self, steam64: int) -> dict:
        import xml.etree.ElementTree as ET

        raw = self._fetch(f"/profiles/{int(steam64)}/", xml=1)
        try:
            root = ET.fromstring(raw.decode("utf-8", errors="replace"))
        except ET.ParseError as exc:
            raise PlatformError(
                f"{self.name}: resposta nao e XML de perfil") from exc
        if root.tag != "profile":
            raise PlatformError(f"{self.name}: perfil inexistente ou privado")

        def field(tag: str, default: str = "") -> str:
            node = root.find(tag)
            return (node.text or default) if node is not None else default

        vac = field("vacBanned") == "1"
        limitada = field("isLimitedAccount") == "1"
        privado = field("privacyState", "public") != "public"

        reasons = []
        risk = 0.0
        if vac:
            risk += 45
            reasons.append("VAC ban no perfil publico")
        if privado:
            risk += 15
            reasons.append("perfil privado")
        if limitada:
            risk += 10
            reasons.append("conta limitada (sem compra de 5 dolares)")

        return {
            "steamid": int(steam64),
            "nome": field("steamID"),
            "vac_banned": vac,
            "conta_limitada": limitada,
            "perfil_privado": privado,
            "trade_ban": field("tradeBanState"),
            "membro_desde": field("memberSince"),
            "risco": min(100.0, risk),
            "motivos": reasons,
        }


# ----------------------------------------------------------------- OpenDota


class OpenDotaClient(_Client):
    """OpenDota: partidas publicas de Dota 2, sem chave.

    O ID de conta do Dota e o SteamID de 32 bits; a conversao fica aqui para
    quem chama poder continuar pensando em SteamID64 o tempo todo.
    """

    base = "https://api.opendota.com/api"
    name = "OpenDota"

    def profile(self, steam64: int) -> dict:
        account = int(steam64) - 76561197960265728
        data = self._get(f"/players/{account}")
        profile = data.get("profile") or {}
        reasons = []
        risk = 0.0
        if profile.get("cheese") is None and not profile.get("account_id"):
            reasons.append("perfil privado ou inexistente no OpenDota")
        return {
            "steamid": steam64,
            "account_id": account,
            "nickname": profile.get("personaname", ""),
            "rank_tier": data.get("rank_tier"),
            "mmr_estimate": (data.get("mmr_estimate") or {}).get("estimate"),
            "risco": risk,
            "motivos": reasons,
        }

    def recent_matches(self, steam64: int, limit: int = 20) -> list:
        account = int(steam64) - 76561197960265728
        data = self._get(f"/players/{account}/matches", limit=limit)
        return data if isinstance(data, list) else []


# -------------------------------------------------------------------- Riot


class RiotClient(_Client):
    """Riot Games API: identidade de conta em VALORANT, LoL e TFT.

    A Riot nao publica bans. O que da para fazer com a chave publica e
    resolver um Riot ID em PUUID e ver o historico de ranked - idade de conta
    e progressao servem de sinal fraco, e o programa diz isso.
    """

    name = "Riot"

    def __init__(self, api_key: str, region: str = "americas",
                 timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave da Riot ausente. Pegue em developer.riotgames.com e "
                "rode: csradar config --riot-key SUACHAVE"
            )
        super().__init__(api_key, timeout, min_interval=1.3)  # 20 req / 1s
        self.region = region
        self.base = f"https://{region}.api.riotgames.com"

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "X-Riot-Token": self.api_key}

    def account(self, riot_id: str) -> dict:
        if "#" not in riot_id:
            raise PlatformError("informe o Riot ID completo, no formato Nome#TAG")
        game_name, tag = riot_id.split("#", 1)
        return self._get(
            f"/riot/account/v1/accounts/by-riot-id/"
            f"{urllib.parse.quote(game_name)}/{urllib.parse.quote(tag)}")

    def profile(self, riot_id: str) -> dict:
        data = self.account(riot_id)
        return {
            "riot_id": riot_id,
            "puuid": data.get("puuid", ""),
            "nome": data.get("gameName", ""),
            "tag": data.get("tagLine", ""),
            "risco": 0.0,
            "motivos": ["a Riot nao expoe bans; use isto so para confirmar "
                        "que a conta existe"],
        }


# ------------------------------------------------------------------ Bungie


class BungieClient(_Client):
    """Bungie API: Destiny 2. Chave gratuita em bungie.net/developer."""

    base = "https://www.bungie.net/Platform"
    name = "Bungie"

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave da Bungie ausente. Pegue em bungie.net/developer e "
                "rode: csradar config --bungie-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "X-API-Key": self.api_key}

    def profile(self, steam64: int) -> dict:
        data = self._get(f"/Destiny2/3/Profile/{steam64}/LinkedProfiles/")
        response = data.get("Response") or {}
        profiles = response.get("profiles") or []
        banned = [p for p in profiles if p.get("isOverridden")
                  or p.get("isCrossSavePrimary") is None]
        reasons = []
        if not profiles:
            reasons.append("nenhum perfil Destiny 2 vinculado a esta Steam")
        return {
            "steamid": steam64,
            "perfis": [{"nome": p.get("displayName", ""),
                        "plataforma": p.get("membershipType"),
                        "ultimo_acesso": p.get("dateLastPlayed", "")}
                       for p in profiles],
            "risco": 0.0,
            "motivos": reasons or ["a Bungie nao publica bans na API"],
        }


# --------------------------------------------------------------- Wargaming


class WargamingClient(_Client):
    """Wargaming: World of Tanks e World of Warships.

    A chave e um `application_id` e vai na query, nao em cabecalho. Nao ha
    endpoint de ban: conta banida some da busca, o que e sinal, nao prova.
    """

    name = "Wargaming"

    REGIONS = {"eu": "eu", "na": "com", "asia": "asia"}

    def __init__(self, api_key: str, region: str = "eu",
                 game: str = "wot", timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "application_id da Wargaming ausente. Pegue em "
                "developers.wargaming.net e rode: "
                "csradar config --wargaming-key SEUID"
            )
        super().__init__(api_key, timeout)
        tld = self.REGIONS.get(region, "eu")
        host = {"wot": "worldoftanks", "wows": "worldofwarships"}.get(
            game, "worldoftanks")
        self.game = game
        self.base = f"https://api.{host}.{tld}"

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def profile(self, nickname: str) -> dict:
        path = f"/{self.game}/account/list/"
        data = self._get(path, application_id=self.api_key, search=nickname)
        items = data.get("data") or []
        if not items:
            return {"nickname": nickname, "encontrado": False, "risco": 0.0,
                    "motivos": ["conta nao encontrada (pode ter sido banida "
                                "ou nunca ter existido)"]}
        return {"nickname": nickname, "encontrado": True,
                "account_id": items[0].get("account_id"),
                "risco": 0.0, "motivos": []}


# ------------------------------------------------------------- Ballchasing


class BallchasingClient(_Client):
    """ballchasing.com: replays de Rocket League em massa.

    E a fonte que alimenta o conversor de Rocket League: os replays sao
    publicos e ja vem com posicao e rotacao por quadro.
    """

    base = "https://ballchasing.com/api"
    name = "ballchasing"

    def __init__(self, api_key: str, timeout: float = 20.0):
        if not api_key:
            raise PlatformError(
                "chave do ballchasing ausente. Pegue em ballchasing.com/upload "
                "e rode: csradar config --ballchasing-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def _headers(self) -> dict:
        # aqui e o token cru, sem "Bearer"
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "Authorization": self.api_key}

    def replays(self, steam64: int, count: int = 20) -> list:
        data = self._get("/replays", **{"player-id": f"steam:{steam64}",
                                        "count": count})
        return data.get("list") or []

    def profile(self, steam64: int) -> dict:
        items = self.replays(steam64)
        return {
            "steamid": steam64,
            "replays": [{"id": r.get("id"), "mapa": r.get("map_name", ""),
                         "em": r.get("date", "")} for r in items],
            "risco": 0.0,
            "motivos": [] if items else ["nenhum replay publico"],
        }


# ---------------------------------------------------------------- GameTools


class GametoolsClient(_Client):
    """gametools.network: estatisticas de Battlefield, sem chave.

    Cobre BF3 ate BF2042 e expoe o que os servidores comunitarios veem. Nao e
    fonte de ban oficial - a EA nao tem uma - mas mostra proporcao de tiro na
    cabeca e precisao, que e o mais perto de sinal que existe nesses jogos.
    """

    base = "https://api.gametools.network"
    name = "gametools"

    def profile(self, name: str, game: str = "bf2042",
                platform: str = "pc") -> dict:
        data = self._get(f"/{game}/stats/", name=name, platform=platform,
                         lang="en-us")
        headshots = data.get("headShots") or data.get("headshots")
        accuracy = data.get("accuracy")
        reasons = []
        risk = 0.0
        try:
            hs = float(str(headshots).strip("%")) if headshots else 0.0
        except ValueError:
            hs = 0.0
        if hs >= 40:
            risk += 35
            reasons.append(f"proporcao de tiro na cabeca alta ({hs:g}%)")
        return {
            "jogador": name,
            "jogo": game,
            "headshots": headshots,
            "precisao": accuracy,
            "kd": data.get("killDeath"),
            "risco": min(100.0, risk),
            "motivos": reasons,
        }


# --------------------------------------------------------------------- osu!


class OsuClient(_Client):
    """osu! API v1: perfil publico por chave simples.

    A osu! remove do ranking quem e pego (`restricted`), e uma conta
    restringida simplesmente desaparece da API - a ausencia e o rotulo.
    """

    base = "https://osu.ppy.sh/api"
    name = "osu!"

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave da osu! ausente. Pegue em osu.ppy.sh/p/api e rode: "
                "csradar config --osu-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def profile(self, user: str) -> dict:
        data = self._get("/get_user", k=self.api_key, u=user)
        items = data if isinstance(data, list) else [data]
        if not items or not items[0]:
            return {"usuario": user, "encontrado": False, "risco": 40.0,
                    "motivos": ["conta ausente da API - pode estar restrita"]}
        info = items[0]
        return {
            "usuario": info.get("username", user),
            "encontrado": True,
            "user_id": info.get("user_id"),
            "pp": info.get("pp_raw"),
            "rank": info.get("pp_rank"),
            "precisao": info.get("accuracy"),
            "risco": 0.0,
            "motivos": [],
        }


# --------------------------------------------------------------- Xbox (XBL)


class OpenXblClient(_Client):
    """OpenXBL: ponte publica para a rede Xbox.

    Cobre Halo, Forza, Sea of Thieves e o resto do catalogo Microsoft. A
    Microsoft aplica suspensoes de conta e elas aparecem como perfil
    inacessivel.
    """

    base = "https://xbl.io/api/v2"
    name = "OpenXBL"

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave do OpenXBL ausente. Pegue em xbl.io e rode: "
                "csradar config --openxbl-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "X-Authorization": self.api_key}

    def profile(self, gamertag: str) -> dict:
        data = self._get(f"/search/{urllib.parse.quote(gamertag)}")
        people = data.get("people") or []
        if not people:
            return {"gamertag": gamertag, "encontrado": False, "risco": 30.0,
                    "motivos": ["gamertag nao encontrada - conta suspensa ou "
                                "renomeada"]}
        person = people[0]
        return {
            "gamertag": person.get("gamertag", gamertag),
            "encontrado": True,
            "xuid": person.get("xuid", ""),
            "gamerscore": person.get("gamerScore"),
            "risco": 0.0,
            "motivos": [],
        }


# ------------------------------------------------------------------ Lichess


class LichessClient(_Client):
    """Lichess: o unico da lista que publica o rotulo de trapaca direto.

    O campo `tosViolation` no perfil publico e exatamente o que o `recheck`
    faz com o VAC: rotulo retroativo, dado pela plataforma, sem chave.
    """

    base = "https://lichess.org/api"
    name = "Lichess"

    def profile(self, user: str) -> dict:
        data = self._get(f"/user/{urllib.parse.quote(user)}")
        violation = bool(data.get("tosViolation"))
        return {
            "usuario": data.get("username", user),
            "banido": violation,
            "fechada": bool(data.get("disabled")),
            "criado_em": data.get("createdAt"),
            "risco": 90.0 if violation else 0.0,
            "motivos": (["marcado como violacao de TOS (trapaca) pelo Lichess"]
                        if violation else []),
        }


# --------------------------------------------------------------- Chess.com


class ChessComClient(_Client):
    """Chess.com: `status` do perfil publico diz quando a conta foi fechada
    por violacao de fair play. Tambem sem chave."""

    base = "https://api.chess.com/pub"
    name = "Chess.com"

    def profile(self, user: str) -> dict:
        data = self._get(f"/player/{urllib.parse.quote(user.lower())}")
        status = str(data.get("status", ""))
        cheating = "fair_play" in status or "abuse" in status
        return {
            "usuario": data.get("username", user),
            "status": status,
            "banido": cheating,
            "risco": 90.0 if cheating else 0.0,
            "motivos": ([f"conta fechada pela plataforma ({status})"]
                        if cheating else []),
        }


# ---------------------------------------------------------- Tracker Network


class TrackerClient(_Client):
    """Tracker Network (tracker.gg): estatisticas multi-jogo com chave.

    A `public-api.tracker.gg/v2` cobre CS2/CSGO, Apex, The Division 2 e
    Splitgate com um unico formato. A chave vai no cabecalho `TRN-Api-Key`.
    Nao publica ban (cada jogo tem o seu), mas expoe proporcao de tiro na
    cabeca e precisao, o sinal mais perto de mira que existe nessas fontes.
    """

    base = "https://public-api.tracker.gg/v2"
    name = "tracker.gg"

    TITLES = {
        "cs2": "csgo",
        "csgo": "csgo",
        "apex": "apex",
        "division2": "division-2",
        "splitgate": "splitgate",
    }

    def __init__(self, api_key: str, timeout: float = 15.0):
        if not api_key:
            raise PlatformError(
                "chave da tracker.gg ausente. Crie um app em "
                "tracker.gg/developers e rode: "
                "csradar config --tracker-key SUACHAVE"
            )
        super().__init__(api_key, timeout)

    def _headers(self) -> dict:
        return {"User-Agent": USER_AGENT, "Accept": "application/json",
                "TRN-Api-Key": self.api_key}

    def profile(self, steam64: int, game: str = "cs2") -> dict:
        title = self.TITLES.get(game, "csgo")
        path = f"/{title}/standard/profile/steam/{int(steam64)}"
        try:
            data = self._get(path)
        except PlatformError as exc:
            if "nao encontrado" in str(exc):
                return {"steamid": int(steam64), "jogo": game,
                        "encontrado": False, "risco": 0.0,
                        "motivos": ["sem perfil no tracker.gg"]}
            raise
        data = data.get("data") or {}
        player = (data.get("platformInfo") or {}).get("platformUserHandle", "")
        headshots = acc = 0.0
        for seg in (data.get("segments") or []):
            seg_meta = (seg.get("metadata") or {}).get("name", "")
            if str(seg.get("type")) != "overview" or not seg_meta == "Lifetime":
                continue
            stats = seg.get("stats") or {}
            hs = stats.get("headshots") or {}
            if isinstance(hs, dict):
                headshots = float(hs.get("value") or 0.0)
            hp = stats.get("header:Accuracy") or {}
            ac = stats.get("accuracy") or {}
            acc_val = ac.get("value") if isinstance(ac, dict) else None
            if acc_val is None and isinstance(hp, dict):
                acc_val = hp.get("value")
            acc = float(acc_val) if acc_val is not None else 0.0
            break
        reasons = []
        risk = 0.0
        if headshots >= 3000:
            risk += 25
            reasons.append(f"volume alto de tiro na cabeca ({headshots:g})")
        return {
            "steamid": int(steam64),
            "jogo": game,
            "encontrado": True,
            "jogador": player,
            "headshots": headshots,
            "precisao": acc,
            "risco": risk,
            "motivos": reasons,
        }


# -------------------------------------------------- RuneScape (hiscore)


class RunescapeClient(_Client):
    """RuneScape / Old School RuneScape: hiscores publicos por nome.

    A `index_lite.ws` e uma API livre e sem chave que responde o perfil de
    quem existe (nivel e XP totais por habilidade). Nao publica ban - o que
    damos e presenca/atividade de conta: uma conta que sumiu do hiscore pode
    ser trocada ou zerada, sinal fraco (mesma logica da Wargaming).
    """

    base = "https://secure.runescape.com"
    name = "RuneScape"

    PATHS = {"osrs": "/m=hiscore_oldschool/index_lite.ws",
             "rs3": "/m=hiscore/index_lite.ws"}

    def profile(self, user: str, mode: str = "osrs") -> dict:
        path = self.PATHS.get(mode, self.PATHS["osrs"])
        try:
            raw = self._fetch(path, player=urllib.parse.quote(user))
        except PlatformError:
            return {"usuario": user, "modo": mode, "encontrado": False,
                    "risco": 0.0, "motivos": ["sem perfil nos hiscores"]}
        text = raw.decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        total = lines[0].split(",") if lines else []
        level = int(total[1]) if len(total) > 1 else 0
        xp = int(total[2]) if len(total) > 2 else 0
        reasons = []
        risk = 0.0
        if not lines:
            return {"usuario": user, "modo": mode, "encontrado": False,
                    "risco": 0.0,
                    "motivos": ["sem perfil nos hiscores"]}
        if level <= 0:
            reasons.append("conta zerada ou recriada (sumiu do hiscore)")
            risk = 20.0
        return {
            "usuario": user,
            "modo": mode,
            "encontrado": True,
            "nivel_total": level,
            "xp_total": xp,
            "risco": risk,
            "motivos": reasons,
        }


PLATFORMS = {
    "faceit": FaceitClient,
    "battlemetrics": BattlemetricsClient,
    "pubg": PubgClient,
    "steamcommunity": SteamCommunityClient,
    "opendota": OpenDotaClient,
    "riot": RiotClient,
    "bungie": BungieClient,
    "wargaming": WargamingClient,
    "ballchasing": BallchasingClient,
    "gametools": GametoolsClient,
    "osu": OsuClient,
    "openxbl": OpenXblClient,
    "lichess": LichessClient,
    "chesscom": ChessComClient,
    "tracker": TrackerClient,
    "runescape": RunescapeClient,
}

# quem consulta por SteamID64 e quem consulta por nome/apelido. O `risk`
# agregado so chama os primeiros; os outros precisam do identificador daquela
# plataforma, que ninguem consegue adivinhar a partir de um SteamID.
BY_STEAMID = ("faceit", "battlemetrics", "steamcommunity", "opendota",
              "bungie", "ballchasing", "tracker")
BY_NICKNAME = ("pubg", "riot", "wargaming", "gametools", "osu", "openxbl",
               "lichess", "chesscom", "runescape")

# plataformas que respondem sem chave nenhuma
KEYLESS = ("steamcommunity", "opendota", "gametools", "lichess", "chesscom",
           "runescape")


# label, atributo em PlatformsConfig, onde se consegue a chave
KEYED = (
    ("FACEIT", "faceit_key", "developers.faceit.com"),
    ("Battlemetrics", "battlemetrics_key", "battlemetrics.com"),
    ("PUBG", "pubg_key", "developer.pubg.com"),
    ("Riot", "riot_key", "developer.riotgames.com"),
    ("Bungie", "bungie_key", "bungie.net/developer"),
    ("Wargaming", "wargaming_key", "developers.wargaming.net"),
    ("ballchasing", "ballchasing_key", "ballchasing.com/upload"),
    ("osu!", "osu_key", "osu.ppy.sh/p/api"),
    ("OpenXBL", "openxbl_key", "xbl.io"),
    ("tracker.gg", "tracker_key", "tracker.gg/developers"),
)


def key_status(platforms_cfg) -> list:
    """(label, valor, onde) para cada plataforma que exige chave."""
    return [(label, getattr(platforms_cfg, attr, ""), where)
            for label, attr, where in KEYED]


def cross_check(steam64: int, platforms_cfg=None, timeout: float = 15.0,
                include_keyed: bool = True) -> list:
    """Consulta, para um SteamID, toda plataforma que aceita SteamID.

    As sem chave entram sempre; as com chave, so quando a chave existe. Uma
    plataforma fora do ar nao pode derrubar as outras, entao o erro vira um
    campo do resultado em vez de excecao - quem le o relatorio precisa saber
    a diferenca entre "consultei e nao achei nada" e "nao consegui consultar".
    """
    cfg = platforms_cfg
    sid = int(steam64)
    jobs = [
        ("steamcommunity",
         lambda: SteamCommunityClient(timeout=timeout).profile(sid)),
    ]
    if include_keyed and cfg is not None:
        if getattr(cfg, "faceit_key", ""):
            jobs.append(("faceit", lambda: FaceitClient(
                cfg.faceit_key, timeout).profile(sid)))
        if getattr(cfg, "battlemetrics_key", ""):
            jobs.append(("battlemetrics", lambda: BattlemetricsClient(
                cfg.battlemetrics_key, timeout).risk(sid)))
        if getattr(cfg, "tracker_key", ""):
            jobs.append(("tracker", lambda: TrackerClient(
                cfg.tracker_key, timeout).profile(sid)))

    out = []
    for name, call in jobs:
        try:
            data = call()
        except PlatformError as exc:
            out.append({"fonte": name, "erro": str(exc), "risco": 0.0,
                        "motivos": []})
            continue
        out.append({"fonte": name,
                    "risco": float(data.get("risco") or 0.0),
                    "motivos": list(data.get("motivos") or []),
                    "detalhe": data})
    return out
