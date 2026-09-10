"""Testes do catalogo multi-jogo, dos adaptadores e do modulo de tempo real.

Tudo roda offline: fixtures em pasta temporaria, sockets de mentira, nenhum
acesso a rede e nenhuma dependencia do que esta instalado nesta maquina.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csradar import catalog
from csradar.config import Config
from csradar.features import burst as burst_mod
from csradar.games import adapter_for, load_any, load_ingest
from csradar.games.base import Support
from csradar.games.ingest import IngestError, _int
from csradar.games.registry import GAMES, identify
from csradar.games.srcds_log import parse_log_text
from csradar.realtime import LiveEngine, RconClient, gsi_roster, install_gsi_config
from csradar.realtime.sources import FileLogSource
from csradar.scoring import analyze_demo, applicable_detectors, compute_score
from csradar.steam import to_steam64

STEAM64 = 76561197960265728
NL = chr(10)


def kill_line(sec: int, att: str, att_id: str, att_team: str,
              vic: str, vic_id: str, vic_team: str, hs: bool = False) -> str:
    flag = " (headshot)" if hs else ""
    return (f'L 01/15/2024 - 20:{sec // 60:02d}:{sec % 60:02d}: '
            f'"{att}<1><{att_id}><{att_team}>" [0 0 0] killed '
            f'"{vic}<2><{vic_id}><{vic_team}>" [500 0 0] with "ak47"{flag}')


def round_line(sec: int) -> str:
    return (f'L 01/15/2024 - 20:{sec // 60:02d}:{sec % 60:02d}: '
            f'World triggered "Round_Start"')


class TestVdfParser(unittest.TestCase):
    def test_flat(self):
        got = catalog.parse_vdf('"AppState" { "appid" "730" "name" "CS2" }')
        self.assertEqual(got["AppState"]["appid"], "730")

    def test_nested_and_comments(self):
        text = '''
        // comentario
        "libraryfolders"
        {
            "0"
            {
                "path"  "D:\\\\SteamLibrary"
                "apps"  { "730" "123" }
            }
        }
        '''
        got = catalog.parse_vdf(text)
        folder = got["libraryfolders"]["0"]
        self.assertEqual(folder["path"], "D:\\SteamLibrary")
        self.assertEqual(folder["apps"]["730"], "123")

    def test_empty_input(self):
        self.assertEqual(catalog.parse_vdf(""), {})


class TestCatalogScanners(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.steam = self.root / "Steam"
        apps = self.steam / "steamapps"
        apps.mkdir(parents=True)
        (apps / "libraryfolders.vdf").write_text(
            '"libraryfolders" { "0" { "path" "%s" } }'
            % str(self.steam).replace("\\", "\\\\"),
            encoding="utf-8",
        )
        (apps / "appmanifest_730.acf").write_text(
            '"AppState" { "appid" "730" "name" "Counter-Strike 2" '
            '"installdir" "cs2" "SizeOnDisk" "1073741824" }', encoding="utf-8")
        (apps / "appmanifest_1.acf").write_text(
            '"AppState" { "appid" "1" "name" "Jogo Sem Suporte" '
            '"installdir" "x" "SizeOnDisk" "10" }', encoding="utf-8")

        self.epic = self.root / "epic"
        self.epic.mkdir()
        (self.epic / "a.item").write_text(json.dumps({
            "DisplayName": "Fortnite", "InstallLocation": "C:/FN",
            "AppName": "Fortnite", "InstallSize": 100,
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_steam_reads_manifests(self):
        games = catalog.scan_steam(self.steam)
        names = {g.name for g in games}
        self.assertIn("Counter-Strike 2", names)
        cs2 = next(g for g in games if g.app_id == "730")
        self.assertEqual(cs2.launcher, "steam")
        self.assertEqual(cs2.support, Support.FULL)
        self.assertEqual(cs2.size_bytes, 1073741824)

    def test_unknown_game_has_no_profile(self):
        games = catalog.scan_steam(self.steam)
        unknown = next(g for g in games if g.app_id == "1")
        self.assertIsNone(unknown.profile)
        self.assertEqual(unknown.support, Support.NONE)

    def test_scan_epic(self):
        games = catalog.scan_epic(self.epic)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].launcher, "epic")
        self.assertEqual(games[0].support, Support.ACCOUNT)

    def test_missing_paths_are_not_an_error(self):
        self.assertEqual(catalog.scan_steam(self.root / "nao_existe"), [])
        self.assertEqual(catalog.scan_epic(self.root / "nao_existe"), [])

    def test_dedupe_keeps_richest_and_both_launchers(self):
        from csradar.games.base import InstalledGame

        poor = InstalledGame(name="Rocket League", launcher="steam")
        rich = InstalledGame(name="Rocket League", launcher="steam",
                             install_dir="C:/RL", size_bytes=10,
                             profile=identify("Rocket League"))
        other = InstalledGame(name="Rocket League", launcher="epic")
        out = catalog.dedupe([poor, rich, other])
        self.assertEqual(len(out), 2)
        steam = next(g for g in out if g.launcher == "steam")
        self.assertEqual(steam.install_dir, "C:/RL")


class TestRegistry(unittest.TestCase):
    def test_identify_by_appid_and_name(self):
        self.assertEqual(identify(appid=730).key, "cs2")
        self.assertEqual(identify("Counter-Strike 2").key, "cs2")
        self.assertEqual(identify("VALORANT").key, "valorant")
        self.assertIsNone(identify("Planilha Contabil 2019"))

    def test_no_shooter_claims_full_support_without_adapter(self):
        for g in GAMES.values():
            if g.support in (Support.FULL, Support.POSITIONAL):
                self.assertTrue(
                    g.adapter,
                    f"{g.key} promete suporte {g.support} sem adaptador",
                )

    def test_kernel_anticheat_games_are_account_only(self):
        for key in ("valorant", "fortnite", "apex", "cod_mw"):
            self.assertEqual(GAMES[key].support, Support.ACCOUNT)

    def test_adapter_chosen_by_extension(self):
        self.assertEqual(adapter_for("x.dem"), "cs2_demo")
        self.assertEqual(adapter_for("x.log"), "srcds_log")
        self.assertEqual(adapter_for("x.jsonl"), "ingest")
        self.assertIsNone(adapter_for("x.mp4"))


class TestSrcdsLog(unittest.TestCase):
    def test_parses_players_rounds_and_kills(self):
        text = "\n".join([
            round_line(0),
            kill_line(5, "A", "STEAM_1:0:1", "CT", "B", "STEAM_1:1:2",
                      "TERRORIST", hs=True),
            round_line(60),
            kill_line(65, "B", "STEAM_1:1:2", "TERRORIST", "A",
                      "STEAM_1:0:1", "CT"),
        ])
        demo = parse_log_text(text)
        self.assertEqual(len(demo.kills), 2)
        self.assertEqual(len(demo.rounds), 2)
        self.assertEqual(len(demo.players), 2)
        a = to_steam64("STEAM_1:0:1")
        self.assertEqual(demo.team_of(a), 3)
        self.assertTrue(demo.kills[0].headshot)
        self.assertEqual(demo.kills[0].round_num, 1)
        self.assertEqual(demo.kills[1].round_num, 2)

    def test_capabilities_exclude_aim_data(self):
        demo = parse_log_text(kill_line(1, "A", "STEAM_1:0:1", "CT", "B",
                                        "STEAM_1:1:2", "TERRORIST"))
        self.assertNotIn("angles", demo.capabilities)
        self.assertIn("kills", demo.capabilities)

    def test_junk_lines_ignored(self):
        demo = parse_log_text("nada\nL 01/15/2024 - 20:00:00: outra coisa\n")
        self.assertEqual(demo.kills, [])

    def test_tick_spacing_follows_timestamps(self):
        text = "\n".join([
            kill_line(0, "A", "STEAM_1:0:1", "CT", "B", "STEAM_1:1:2", "T"),
            kill_line(2, "A", "STEAM_1:0:1", "CT", "C", "STEAM_1:1:3", "T"),
        ])
        demo = parse_log_text(text, tickrate=64.0)
        self.assertEqual(demo.kills[1].tick - demo.kills[0].tick, 128)


class TestCapabilityAwareScoring(unittest.TestCase):
    """As asserçoes aqui falam de FAMILIAS, nao de listas fixas.

    Versoes anteriores fixavam o conjunto exato de sinais e quebravam toda vez
    que um detector novo entrava - ruido puro para quem mantem o projeto. O
    que importa e a regra: sinal so roda quando a fonte sustenta o dado dele.
    """

    AIM_3D = {"snap", "jitter", "reaction", "tracking", "prefire", "recoil",
              "movement"}
    CURSOR_2D = {"tremor", "cursor_jump", "key_timing"}
    FULL_CS2 = {"angles", "positions", "shots", "kills", "rounds", "teams",
                "kill_flags"}
    OSU = {"cursor", "keys", "positions"}

    def _split(self, caps):
        run, missing = applicable_detectors(caps)
        return ({n for n, _fn, _p in run}, {sig for sig, _ in missing})

    def test_events_only_source_runs_only_weak_signals(self):
        ran, skipped = self._split({"kills", "rounds", "teams"})
        self.assertEqual(ran, {"burst"})
        self.assertTrue(self.AIM_3D <= skipped)
        self.assertTrue(self.CURSOR_2D <= skipped)

    def test_full_cs2_source_runs_every_3d_signal(self):
        ran, skipped = self._split(self.FULL_CS2)
        self.assertIn("snap", ran)
        self.assertIn("context", ran)
        self.assertIn("burst", ran)
        # nenhum sinal 3D pode ficar de fora numa fonte completa
        self.assertEqual(self.AIM_3D & skipped, set())
        # os 2D ficam de fora, e isso e correto: nao ha cursor numa demo
        self.assertTrue(self.CURSOR_2D <= skipped)

    def test_cursor_source_runs_only_the_2d_family(self):
        ran, skipped = self._split(self.OSU)
        self.assertEqual(ran, {"cursor_tremor", "cursor_jump", "cursor_keys"})
        self.assertTrue(self.AIM_3D <= skipped)

    def test_kill_flags_are_optional_even_on_full_sources(self):
        """CS2 sem as colunas de flag ainda roda todo o resto."""
        ran, skipped = self._split(self.FULL_CS2 - {"kill_flags"})
        self.assertNotIn("context", ran)
        self.assertIn("context", skipped)
        self.assertEqual(self.AIM_3D & skipped, set())

    def test_every_declared_signal_belongs_to_some_detector(self):
        """Peso na configuracao sem detector que o produza e peso morto."""
        from csradar.scoring import DETECTORS

        produced = {sig for _n, _fn, _needs, sigs in DETECTORS for sig in sigs}
        for name in Config().scoring.weights:
            self.assertIn(name, produced,
                          f"peso '{name}' nao corresponde a nenhum detector")

    def test_report_lists_what_was_skipped(self):
        demo = parse_log_text(NL.join(
            kill_line(i, "A", "STEAM_1:0:1", "CT", f"B{i}",
                      f"STEAM_1:1:{i}", "TERRORIST") for i in range(1, 8)
        ))
        report = analyze_demo(demo, Config())
        self.assertTrue(report.skipped_signals)
        faltou = {c for s in report.skipped_signals for c in s["faltou"]}
        self.assertIn("angles", faltou)

    def test_weak_only_score_is_capped(self):
        from csradar.models import SignalResult

        cfg = Config()
        weak = {"burst": SignalResult("burst", 1.0, confident=True)}
        self.assertEqual(compute_score(weak, cfg), cfg.scoring.weak_only_cap)
        strong = dict(weak)
        strong["snap"] = SignalResult("snap", 1.0, confident=True)
        self.assertGreater(compute_score(strong, cfg), cfg.scoring.weak_only_cap)


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_int_keeps_64bit_precision(self):
        """int(float(steamid)) perde o ultimo digito e funde jogadores."""
        for sid in (STEAM64 + 1, STEAM64 + 2, 76561198123456789):
            self.assertEqual(_int(sid), sid)
            self.assertEqual(_int(str(sid)), sid)

    def test_jsonl_roundtrip(self):
        rows = [
            {"type": "meta", "game": "rocket_league", "map": "DFH", "tickrate": 30},
            {"type": "round", "number": 1, "start_tick": 0, "end_tick": 100},
            {"type": "tick", "tick": 1, "steamid": STEAM64 + 1, "name": "a",
             "team": 2, "x": 1, "y": 2, "z": 3, "pitch": 0, "yaw": 90},
            {"type": "tick", "tick": 1, "steamid": STEAM64 + 2, "name": "b",
             "team": 3, "x": 4, "y": 5, "z": 6, "pitch": 1, "yaw": 180},
            {"type": "shot", "tick": 2, "steamid": STEAM64 + 1},
            {"type": "kill", "tick": 3, "attacker": STEAM64 + 1,
             "victim": STEAM64 + 2, "headshot": True},
        ]
        path = self._write("a.jsonl", "\n".join(json.dumps(r) for r in rows))
        demo = load_ingest(path)
        self.assertEqual(demo.map_name, "DFH")
        self.assertEqual(demo.tickrate, 30)
        self.assertEqual(len(demo.players), 2)
        self.assertEqual(demo.kills[0].attacker, STEAM64 + 1)
        self.assertTrue({"angles", "positions", "shots", "kills", "teams"}
                        <= demo.capabilities)

    def test_json_object_form(self):
        payload = {
            "meta": {"map": "x"},
            "ticks": [{"tick": 1, "steamid": STEAM64 + 1, "team": 2,
                       "x": 0, "y": 0, "z": 0, "pitch": 0, "yaw": 0}],
            "kills": [{"tick": 2, "attacker": STEAM64 + 1,
                       "victim": STEAM64 + 2}],
        }
        demo = load_ingest(self._write("a.json", json.dumps(payload)))
        self.assertEqual(len(demo.kills), 1)

    def test_csv_ticks_only(self):
        text = ("tick,steamid,name,team,x,y,z,pitch,yaw\n"
                f"1,{STEAM64 + 1},a,2,0,0,0,0,10\n"
                f"1,{STEAM64 + 2},b,3,100,0,0,0,20\n")
        demo = load_ingest(self._write("a.csv", text))
        self.assertEqual(len(demo.players), 2)
        self.assertIn("angles", demo.capabilities)
        self.assertNotIn("kills", demo.capabilities)

    def test_field_aliases(self):
        rows = [{"type": "tick", "frame": 1, "player_id": STEAM64 + 1,
                 "side": "CT", "pos_x": 1, "pos_y": 2, "pos_z": 3,
                 "view_pitch": 0, "view_yaw": 45}]
        demo = load_ingest(self._write("a.jsonl",
                                       "\n".join(json.dumps(r) for r in rows)))
        tick = demo.ticks_by_player[STEAM64 + 1][0]
        self.assertEqual((tick.tick, tick.yaw, tick.team), (1, 45.0, 3))

    def test_unknown_type_is_a_clear_error(self):
        path = self._write("a.jsonl", json.dumps({"type": "banana"}))
        with self.assertRaises(IngestError) as ctx:
            load_ingest(path)
        self.assertIn("banana", str(ctx.exception))

    def test_empty_file_rejected(self):
        with self.assertRaises(IngestError):
            load_ingest(self._write("a.jsonl", ""))

    def test_bad_extension_rejected(self):
        with self.assertRaises(IngestError):
            load_ingest(self._write("a.txt2", "{}"))

    def test_load_any_dispatches(self):
        text = "\n".join(
            kill_line(i, "A", "STEAM_1:0:1", "CT", f"B{i}", f"STEAM_1:1:{i}", "T")
            for i in range(1, 4)
        )
        demo = load_any(self._write("s.log", text))
        self.assertEqual(len(demo.kills), 3)
        with self.assertRaises(ValueError):
            load_any(self._write("s.mp4", "x"))


class TestGsi(unittest.TestCase):
    def test_roster_from_allplayers(self):
        payload = {
            "allplayers": {
                str(STEAM64 + 1): {"name": "um"},
                str(STEAM64 + 2): {"name": "dois"},
            },
            "player": {"steamid": str(STEAM64 + 3), "name": "eu"},
        }
        roster = gsi_roster(payload)
        self.assertEqual(len(roster), 3)
        self.assertEqual(roster[STEAM64 + 1], "um")

    def test_roster_without_allplayers_has_only_me(self):
        """Num jogo normal o GSI so entrega o proprio jogador."""
        roster = gsi_roster({"player": {"steamid": str(STEAM64 + 3), "name": "eu"}})
        self.assertEqual(list(roster), [STEAM64 + 3])

    def test_config_file_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = install_gsi_config(tmp, port=4321, token="abc")
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("http://127.0.0.1:4321", text)
            self.assertIn('"token"    "abc"', text)

    def test_missing_cfg_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            install_gsi_config(Path(tempfile.gettempdir()) / "nao_existe_csradar")


class TestLiveEngine(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.alerts = []
        self.engine = LiveEngine(self.cfg, on_alert=self.alerts.append)

    def test_roster_grows_from_log_lines(self):
        self.engine.feed_log_line(
            kill_line(1, "A", "STEAM_1:0:1", "CT", "B", "STEAM_1:1:2", "TERRORIST"))
        self.assertEqual(len(self.engine.roster), 2)
        self.assertEqual(len(self.engine.demo.kills), 1)
        kinds = [a.kind for a in self.alerts]
        self.assertEqual(kinds.count("entrou"), 2)

    def test_player_announced_once(self):
        for sec in (1, 2, 3):
            self.engine.feed_log_line(
                kill_line(sec, "A", "STEAM_1:0:1", "CT", f"B{sec}",
                          f"STEAM_1:1:{sec}", "TERRORIST"))
        entered = [a for a in self.alerts if a.kind == "entrou" and a.name == "A"]
        self.assertEqual(len(entered), 1)

    def test_rounds_tracked(self):
        self.engine.feed_log_line(round_line(0))
        self.engine.feed_log_line(round_line(60))
        self.assertEqual(len(self.engine.demo.rounds), 2)
        self.assertEqual(self.engine.demo.rounds[0].end_tick,
                         self.engine.demo.rounds[1].start_tick - 1)

    def test_console_status_feeds_roster(self):
        self.engine.feed_console_line(
            '#  2 1 "Fulano" STEAM_1:0:9 05:11 34 0 active 786432')
        self.assertIn(to_steam64("STEAM_1:0:9"), self.engine.roster)

    def test_gsi_payload_feeds_roster_and_map(self):
        self.engine.feed_gsi({
            "map": {"name": "de_dust2"},
            "allplayers": {str(STEAM64 + 5): {"name": "x"}},
        })
        self.assertEqual(self.engine.demo.map_name, "de_dust2")
        self.assertIn(STEAM64 + 5, self.engine.roster)

    def test_risk_lookup_failure_does_not_crash(self):
        def boom(_sid):
            raise RuntimeError("sem rede")

        engine = LiveEngine(self.cfg, on_alert=self.alerts.append, risk_lookup=boom)
        engine.feed_console_line('#  2 1 "F" STEAM_1:0:9 05:11 34 0 active 1')
        self.assertTrue(any(a.kind == "risco_erro" for a in self.alerts))

    def test_snapshot_is_a_valid_report(self):
        self.engine.feed_log_line(round_line(0))
        self.engine.feed_log_line(
            kill_line(5, "A", "STEAM_1:0:1", "CT", "B", "STEAM_1:1:2", "T"))
        report = self.engine.snapshot()
        self.assertEqual(len(report.players), 2)
        self.assertTrue(report.skipped_signals)


class TestFileLogSource(unittest.TestCase):
    def test_tail_reads_only_new_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("antiga\n", encoding="utf-8")
            src = FileLogSource(path)
            src.start()
            self.assertEqual(src.drain(), [])
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("nova 1\nnova 2\n")
            self.assertEqual(src.drain(), ["nova 1", "nova 2"])
            src.stop()

    def test_from_start_reads_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.log"
            path.write_text("a\nb\n", encoding="utf-8")
            src = FileLogSource(path, from_start=True)
            src.start()
            self.assertEqual(src.drain(), ["a", "b"])
            src.stop()


class TestRconProtocol(unittest.TestCase):
    """Servidor RCON de mentira, para exercitar o enquadramento dos pacotes."""

    def setUp(self):
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.close()
        except OSError:
            pass

    def _serve(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        with conn:
            while True:
                head = conn.recv(4)
                if not head:
                    return
                (length,) = struct.unpack("<i", head)
                body = b""
                while len(body) < length:
                    chunk = conn.recv(length - len(body))
                    if not chunk:
                        return
                    body += chunk
                pid, ptype = struct.unpack("<ii", body[:8])
                text = body[8:-2].decode("utf-8")
                self.received.append((ptype, text))
                if ptype == RconClient.AUTH:
                    ok = text == "senha_certa"
                    self._reply(conn, pid, RconClient.RESPONSE_VALUE, "")
                    self._reply(conn, pid if ok else -1,
                                RconClient.AUTH_RESPONSE, "")
                else:
                    self._reply(conn, pid, RconClient.RESPONSE_VALUE,
                                '# 2 1 "Fulano" STEAM_1:0:7 05:11 34 0 active')

    @staticmethod
    def _reply(conn, pid, ptype, body):
        payload = struct.pack("<ii", pid, ptype) + body.encode("utf-8") + b"\x00\x00"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    def test_auth_and_command(self):
        with RconClient("127.0.0.1", self.port, "senha_certa", timeout=3) as rcon:
            out = rcon.command("status")
        self.assertIn("Fulano", out)
        self.assertEqual(self.received[0][0], RconClient.AUTH)
        self.assertEqual(self.received[1][1], "status")

    def test_wrong_password_rejected(self):
        from csradar.realtime import RconError

        client = RconClient("127.0.0.1", self.port, "errada", timeout=3)
        with self.assertRaises(RconError):
            client.connect()
        client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
