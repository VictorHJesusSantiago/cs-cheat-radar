"""Testes da expansao do catalogo: jogos, scanners e plataformas.

Nenhum teste aqui toca a rede. Os clientes de plataforma sao exercitados com
`_get` (ou `_fetch`, no caso do XML da Steam) trocado por uma funcao que
devolve resposta de mentira - e o unico jeito de testar o tratamento de
resposta sem depender do servico estar de pe.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csradar import catalog, platforms
from csradar.games.base import Support
from csradar.games.registry import (
    BY_APPID, GAMES, _NAME_HINTS, by_name, identify, stats,
)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class CatalogoDeJogos(unittest.TestCase):
    def test_chaves_unicas_e_catalogo_grande(self):
        self.assertGreaterEqual(len(GAMES), 300)
        for key, game in GAMES.items():
            self.assertEqual(key, game.key)
            self.assertTrue(game.name)

    def test_hints_apontam_para_jogos_existentes_e_sem_repeticao(self):
        vistos = set()
        for hint, key in _NAME_HINTS:
            self.assertIn(key, GAMES, f"hint '{hint}' aponta para nada")
            self.assertNotIn(hint, vistos, f"hint '{hint}' duplicado")
            vistos.add(hint)

    def test_todo_jogo_real_e_reconhecido_pelo_proprio_nome(self):
        # as entradas genericas ("Servidor dedicado Source") nao tem nome de
        # produto para casar, e isso e proposital
        for game in GAMES.values():
            if "generic" in game.key:
                continue
            self.assertIsNotNone(by_name(game.name),
                                 f"{game.name} nao casa com nenhum hint")

    def test_especifico_vence_generico(self):
        casos = {
            "Quake III Arena": "quake3",
            "Quake II": "quake2",
            "Counter-Strike: Condition Zero": "czero",
            "Counter-Strike 2": "cs2",
            "Day of Defeat: Source": "dod_source",
            "Day of Defeat": "dod",
            "Battlefield 2042": "bf2042",
            "Battlefield 4": "bf4",
            "SMITE 2": "smite2",
            "SMITE": "smite",
            "Diablo IV": "diablo4",
            "Diablo II: Resurrected": "diablo2r",
            "Sons of the Forest": "sonsoftheforest",
            "The Forest": "theforest",
            "Killing Floor 2": "kf2",
            "Killing Floor": "kf1",
        }
        for nome, esperado in casos.items():
            self.assertEqual(by_name(nome).key, esperado, nome)

    def test_appid_nao_conflita(self):
        # o indice guarda o primeiro declarado; o que nao pode e um appid
        # apontar para jogo de outra familia
        self.assertEqual(BY_APPID[730].key, "cs2")
        self.assertEqual(identify("", 232090).key, "kf2")

    def test_nivel_alto_exige_adaptador_ou_conversor(self):
        for game in GAMES.values():
            if game.support in (Support.FULL, Support.POSITIONAL):
                self.assertTrue(
                    game.adapter or game.converter,
                    f"{game.key} promete {game.support} sem dizer como")

    def test_conta_nao_promete_dado_que_nao_tem(self):
        for game in GAMES.values():
            if game.support is Support.ACCOUNT:
                self.assertEqual(game.caps, set(), game.key)

    def test_distribuicao_bate_com_o_total(self):
        d = stats()
        self.assertEqual(
            d["total"],
            d["completo"] + d["posicional"] + d["eventos"] + d["conta"]
            + d["nenhum"])


class ScannersNovos(unittest.TestCase):
    def test_todos_os_scanners_toleram_maquina_vazia(self):
        vazio = tmpdir()
        self.assertEqual(catalog.scan_heroic(vazio / "nao_existe"), [])
        self.assertEqual(catalog.scan_oculus(vazio, vazio / "x"), [])
        self.assertEqual(catalog.scan_wargaming(vazio / "p.xml"), [])
        self.assertEqual(catalog.scan_minecraft(vazio / ".minecraft"), [])
        self.assertEqual(catalog.scan_roblox(vazio / "Versions"), [])
        self.assertEqual(catalog.scan_steam_shortcuts(vazio), [])
        self.assertEqual(catalog.scan_flatpak([vazio / "app"]), [])
        self.assertEqual(catalog.scan_snap(vazio / "snap"), [])

    def test_heroic_le_gog_e_epic(self):
        root = tmpdir()
        epic = root / "legendaryConfig" / "legendary"
        epic.mkdir(parents=True)
        (epic / "installed.json").write_text(json.dumps({
            "Fortnite": {"title": "Fortnite", "install_path": r"D:\FN",
                         "install_size": 1024},
        }), encoding="utf-8")
        gog = root / "gog_store"
        gog.mkdir()
        (gog / "installed.json").write_text(json.dumps({
            "installed": [{"appName": "1207658691", "title": "GWENT",
                           "install_path": r"D:\GWENT"}],
        }), encoding="utf-8")

        nomes = {g.name: g for g in catalog.scan_heroic(root)}
        self.assertIn("Fortnite", nomes)
        self.assertEqual(nomes["Fortnite"].launcher, "epic")
        self.assertEqual(nomes["GWENT"].launcher, "gog")
        self.assertEqual(nomes["GWENT"].profile.key, "gwent")

    def test_oculus_usa_manifesto_e_depois_a_pasta(self):
        manifests = tmpdir()
        (manifests / "beat.json").write_text(json.dumps({
            "displayName": "Beat Saber", "appId": "123",
            "libraryPath": r"D:\Oculus",
        }), encoding="utf-8")
        (manifests / "beat_assets.json").write_text("{}", encoding="utf-8")
        software = tmpdir()
        (software / "vankrupt-games-pavlov").mkdir()

        jogos = catalog.scan_oculus(manifests, software)
        nomes = {g.name for g in jogos}
        self.assertIn("Beat Saber", nomes)
        self.assertIn("Vankrupt Games Pavlov", nomes)
        self.assertTrue(all(g.launcher == "oculus" for g in jogos))
        beat = next(g for g in jogos if g.name == "Beat Saber")
        self.assertEqual(beat.profile.key, "beatsaber")

    def test_wargaming_le_preferences(self):
        p = tmpdir() / "preferences.xml"
        p.write_text(
            "<protocol><application><games_manager><games>"
            "<game><path>D:/Games/World_of_Tanks_EU</path></game>"
            "<game><path>D:/Games/World_of_Warships_EU</path></game>"
            "</games></games_manager></application></protocol>",
            encoding="utf-8")
        jogos = catalog.scan_wargaming(p)
        self.assertEqual([g.name for g in jogos],
                         ["World of Tanks EU", "World of Warships EU"])
        self.assertEqual(jogos[0].profile.key, "wot")

    def test_minecraft_e_roblox_sao_presenca_de_pasta(self):
        mc = tmpdir() / ".minecraft"
        mc.mkdir()
        jogos = catalog.scan_minecraft(mc)
        self.assertEqual(jogos[0].profile.key, "minecraft")

        versions = tmpdir() / "Versions"
        (versions / "version-abc").mkdir(parents=True)
        self.assertEqual(catalog.scan_roblox(versions), [])
        (versions / "version-abc" / "RobloxPlayerBeta.exe").write_bytes(b"")
        self.assertEqual(catalog.scan_roblox(versions)[0].profile.key, "roblox")

    def test_vdf_binario_le_atalho_nao_steam(self):
        def s(key, value):
            return b"\x01" + key.encode() + b"\x00" + value.encode() + b"\x00"

        def i(key, value):
            return b"\x02" + key.encode() + b"\x00" + value.to_bytes(4, "little")

        entrada = (b"\x000\x00" + i("appid", 7) + s("AppName", "FiveM")
                   + s("StartDir", r"C:\FiveM") + b"\x08")
        blob = b"\x00shortcuts\x00" + entrada + b"\x08\x08"

        parsed = catalog.parse_binary_vdf(blob)
        self.assertEqual(parsed["shortcuts"]["0"]["AppName"], "FiveM")
        self.assertEqual(parsed["shortcuts"]["0"]["appid"], 7)
        # arquivo truncado nao pode explodir: vira dicionario vazio
        self.assertEqual(catalog.parse_binary_vdf(blob[:15]), {})

        steam = tmpdir()
        cfg = steam / "userdata" / "1" / "config"
        cfg.mkdir(parents=True)
        (cfg / "shortcuts.vdf").write_bytes(blob)
        jogos = catalog.scan_steam_shortcuts(steam)
        self.assertEqual(jogos[0].name, "FiveM")
        self.assertEqual(jogos[0].launcher, "steam:atalho")
        self.assertEqual(jogos[0].profile.key, "fivem")

    def test_scan_all_conhece_os_novos_scanners(self):
        for nome in ("heroic", "oculus", "wargaming", "minecraft", "roblox",
                     "steam_shortcuts", "flatpak", "snap"):
            self.assertIn(nome, catalog.SCANNERS)
        # um scanner que explode nao derruba os outros
        catalog.SCANNERS["quebrado"] = lambda o: (_ for _ in ()).throw(
            RuntimeError("boom"))
        try:
            catalog.scan_all(only=["quebrado", "minecraft"],
                             dot_minecraft="/nao/existe")
            self.assertIn("quebrado", catalog.scan_all.last_errors)
        finally:
            del catalog.SCANNERS["quebrado"]


class FakeResposta:
    """Substitui `_get` por uma tabela de respostas prontas."""

    def __init__(self, payload):
        self.payload = payload
        self.chamadas = []

    def __call__(self, path, **params):
        self.chamadas.append((path, params))
        return self.payload


class ClientesDePlataforma(unittest.TestCase):
    def test_registro_e_indices_sao_coerentes(self):
        self.assertEqual(sorted(platforms.PLATFORMS),
                         sorted(set(platforms.BY_STEAMID)
                                | set(platforms.BY_NICKNAME)))
        for nome in platforms.KEYLESS:
            self.assertIn(nome, platforms.PLATFORMS)
        for _, attr, _ in platforms.KEYED:
            self.assertTrue(attr.endswith("_key"))

    def test_plataforma_com_chave_falha_explicito_sem_chave(self):
        for cls in (platforms.RiotClient, platforms.BungieClient,
                    platforms.WargamingClient, platforms.BallchasingClient,
                    platforms.OsuClient, platforms.OpenXblClient):
            with self.assertRaises(platforms.PlatformError):
                cls("")

    def test_lichess_marca_violacao_de_tos(self):
        client = platforms.LichessClient()
        client._get = FakeResposta({"username": "x", "tosViolation": True})
        out = client.profile("x")
        self.assertTrue(out["banido"])
        self.assertEqual(out["risco"], 90.0)
        self.assertTrue(out["motivos"])

        client._get = FakeResposta({"username": "y"})
        self.assertEqual(client.profile("y")["risco"], 0.0)

    def test_chesscom_le_status_de_fair_play(self):
        client = platforms.ChessComClient()
        client._get = FakeResposta({"username": "z",
                                    "status": "closed:fair_play_violations"})
        out = client.profile("Z")
        self.assertTrue(out["banido"])
        self.assertEqual(out["risco"], 90.0)

        client._get = FakeResposta({"username": "z", "status": "premium"})
        self.assertFalse(client.profile("z")["banido"])

    def test_steam_community_le_vac_do_xml(self):
        xml = (b'<?xml version="1.0" encoding="UTF-8"?><profile>'
               b"<steamID64>1</steamID64><steamID>fulano</steamID>"
               b"<privacyState>public</privacyState><vacBanned>1</vacBanned>"
               b"<isLimitedAccount>0</isLimitedAccount>"
               b"<tradeBanState>None</tradeBanState>"
               b"<memberSince>June 26, 2011</memberSince></profile>")
        client = platforms.SteamCommunityClient()
        client._fetch = lambda path, **params: xml
        out = client.profile(1)
        self.assertTrue(out["vac_banned"])
        self.assertEqual(out["risco"], 45.0)
        self.assertEqual(out["nome"], "fulano")

        limpo = xml.replace(b"<vacBanned>1", b"<vacBanned>0").replace(
            b"<privacyState>public", b"<privacyState>private")
        client._fetch = lambda path, **params: limpo
        out = client.profile(1)
        self.assertFalse(out["vac_banned"])
        self.assertTrue(out["perfil_privado"])
        self.assertEqual(out["risco"], 15.0)

        # pagina de erro em HTML nao pode virar perfil
        client._fetch = lambda path, **params: b"<html><body>404</body></html>"
        with self.assertRaises(platforms.PlatformError):
            client.profile(1)

    def test_opendota_converte_steamid64_para_account_id(self):
        client = platforms.OpenDotaClient()
        fake = FakeResposta({"profile": {"account_id": 1, "personaname": "a"}})
        client._get = fake
        out = client.profile(76561197960265729)
        self.assertEqual(out["account_id"], 1)
        self.assertEqual(fake.chamadas[0][0], "/players/1")

    def test_riot_exige_riot_id_completo(self):
        client = platforms.RiotClient("chave")
        with self.assertRaises(platforms.PlatformError):
            client.account("SoNome")

    def test_gametools_marca_headshot_alto(self):
        client = platforms.GametoolsClient()
        client._get = FakeResposta({"headShots": "62%", "accuracy": "30%"})
        out = client.profile("jogador")
        self.assertGreater(out["risco"], 0)
        client._get = FakeResposta({"headShots": "18%"})
        self.assertEqual(client.profile("jogador")["risco"], 0.0)

    def test_osu_trata_conta_ausente_como_sinal(self):
        client = platforms.OsuClient("chave")
        client._get = FakeResposta([])
        out = client.profile("alguem")
        self.assertFalse(out["encontrado"])
        self.assertGreater(out["risco"], 0)

    def test_tracker_exige_chave(self):
        with self.assertRaises(platforms.PlatformError):
            platforms.TrackerClient("")

    def test_tracker_le_headshots_e_precisao(self):
        payload = {"data": {
            "platformInfo": {"platformUserHandle": "Fulano"},
            "segments": [
                {"type": "overview",
                 "metadata": {"name": "Lifetime"},
                 "stats": {"headshots": {"value": 4200},
                           "accuracy": {"value": 0.31}}},
            ],
        }}
        client = platforms.TrackerClient("chave")
        client._get = FakeResposta(payload)
        out = client.profile(76561197960265728)
        self.assertTrue(out["encontrado"])
        self.assertEqual(out["jogador"], "Fulano")
        self.assertGreater(out["risco"], 0)
        self.assertTrue(out["motivos"])

    def test_tracker_usa_slug_csgo_e_nao_encontra_vira_perfil_vazio(self):
        client = platforms.TrackerClient("chave")
        calls = []

        def falso(path, **params):
            calls.append(path)
            raise platforms.PlatformError("nao encontrado")

        client._get = falso
        out = client.profile(76561197960265728)
        self.assertFalse(out["encontrado"])
        self.assertIn("/csgo/standard/profile/steam/", calls[0])

    def test_runescape_sem_chave_le_nivel_total(self):
        client = platforms.RunescapeClient()
        corpo = ("1623886,1466,27957906\n"
                 "1603320,76,1343681\n")
        client._fetch = lambda path, **params: corpo.encode("utf-8")
        out = client.profile("Zezima")
        self.assertTrue(out["encontrado"])
        self.assertEqual(out["nivel_total"], 1466)
        self.assertEqual(out["risco"], 0.0)

    def test_runescape_conta_zerada_e_sinal_fraco(self):
        client = platforms.RunescapeClient()
        client._fetch = lambda path, **params: b"0,0,0\n"
        out = client.profile("fulano")
        self.assertTrue(out["encontrado"])
        self.assertEqual(out["nivel_total"], 0)
        self.assertGreater(out["risco"], 0)

    def test_runescape_sem_perfil_nao_cai(self):
        client = platforms.RunescapeClient()
        client._fetch = lambda path, **params: b""
        out = client.profile("ninguem")
        self.assertFalse(out["encontrado"])
        self.assertEqual(out["risco"], 0.0)

    def test_cross_check_registra_erro_sem_derrubar_o_resto(self):
        original = platforms.SteamCommunityClient.profile

        def falha(self, sid):
            raise platforms.PlatformError("fora do ar")

        platforms.SteamCommunityClient.profile = falha
        try:
            out = platforms.cross_check(76561197960265728, None)
        finally:
            platforms.SteamCommunityClient.profile = original

        fontes = {item["fonte"]: item for item in out}
        self.assertIn("erro", fontes["steamcommunity"])
        self.assertEqual(fontes["steamcommunity"]["risco"], 0.0)

    def test_merge_cross_pega_o_maior_e_nao_soma(self):
        from csradar.cli import _merge_cross

        perfil = {"risco": 30.0, "motivos": ["conta nova"]}
        _merge_cross(perfil, [
            {"fonte": "steamcommunity", "risco": 70.0,
             "motivos": ["VAC ban no perfil publico"]},
            {"fonte": "faceit", "risco": 60.0, "motivos": ["1 ban(s)"]},
        ])
        self.assertEqual(perfil["risco"], 70.0)
        self.assertIn("[steamcommunity] VAC ban no perfil publico",
                      perfil["motivos"])
        self.assertEqual(len(perfil["externo"]), 2)


class ConfiguracaoDeChaves(unittest.TestCase):
    def test_toda_plataforma_com_chave_tem_campo_na_config(self):
        from csradar.config import PlatformsConfig

        cfg = PlatformsConfig()
        for label, attr, where in platforms.KEYED:
            self.assertTrue(hasattr(cfg, attr), f"{label} sem campo {attr}")
            self.assertTrue(where)
        status = platforms.key_status(cfg)
        self.assertEqual(len(status), len(platforms.KEYED))
        self.assertTrue(all(valor == "" for _, valor, _ in status))


if __name__ == "__main__":
    unittest.main()
