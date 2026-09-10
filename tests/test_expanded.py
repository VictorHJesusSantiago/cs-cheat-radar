"""Testes dos detectores novos, conversores, ML, watchlist e HTML.

Offline: fixtures em pasta temporaria, nenhuma chamada de rede.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csradar import htmlreport, ml, watchlist as wl
from csradar.config import Config
from csradar.demo.synthetic import make_synthetic_demo
from csradar.features import context as context_mod
from csradar.features import movement as movement_mod
from csradar.features import recoil as recoil_mod
from csradar.games.converters import (
    ConversionError, from_mapping, from_pubg_telemetry, from_r6_dissect,
    write_ingest, _stable_id,
)
from csradar.games import load_ingest
from csradar.games.registry import GAMES, SUPPORT_RANK, identify, stats
from csradar.games.base import Support
from csradar.models import MatchReport, PlayerReport, SignalResult
from csradar.platforms import FaceitClient, PlatformError
from csradar.scoring import analyze_demo, compute_score
from csradar.storage import Store

STEAM64 = 76561197960265728
CHEATERS = {3: "aimbot", 7: "wallhack", 5: "silent"}


class TestExpandedRegistry(unittest.TestCase):
    def test_catalog_is_large_and_consistent(self):
        counts = stats()
        self.assertGreaterEqual(counts["total"], 100)
        self.assertEqual(sum(counts[str(s)] for s in SUPPORT_RANK),
                         counts["total"])

    def test_every_claim_above_events_has_an_adapter(self):
        """Nao prometer nivel de suporte sem caminho real para o dado."""
        for g in GAMES.values():
            if g.support in (Support.FULL, Support.POSITIONAL):
                self.assertTrue(g.adapter, f"{g.key} promete {g.support} sem adaptador")
                if g.adapter == "ingest":
                    self.assertTrue(
                        g.converter,
                        f"{g.key} depende de ingestao mas nao nomeia o conversor")

    def test_kernel_anticheat_titles_stay_account_only(self):
        for key in ("valorant", "fortnite", "apex", "cod_bo6", "lol",
                    "overwatch2", "tarkov", "roblox"):
            self.assertEqual(GAMES[key].support, Support.ACCOUNT,
                             f"{key} nao pode prometer mais que conta")

    def test_pubg_is_events_not_full(self):
        """A telemetria da PUBG nao tem direcao de mira - nao pode ser FULL."""
        self.assertEqual(GAMES["pubg"].support, Support.EVENTS)

    def test_name_hints_resolve_specific_before_generic(self):
        self.assertEqual(identify("Counter-Strike 2").key, "cs2")
        self.assertEqual(identify("Counter-Strike: Source").key, "css")
        self.assertEqual(identify("Rainbow Six Siege X").key, "siege_x")
        self.assertEqual(identify("Rainbow Six Siege").key, "r6siege")

    def test_every_game_key_matches_its_dict_key(self):
        for key, game in GAMES.items():
            self.assertEqual(key, game.key)


class TestNewDetectors(unittest.TestCase):
    cfg = Config()
    _cache: dict = {}

    def _report(self, seed: int):
        if seed not in self._cache:
            demo = make_synthetic_demo(rounds=24, seed=seed, profiles=CHEATERS)
            self._cache[seed] = (demo, analyze_demo(demo, self.cfg))
        return self._cache[seed]

    def _player(self, report, prefix: str):
        return next(p for p in report.players if p.name.startswith(prefix))

    def test_all_three_cheaters_rank_above_every_honest_player(self):
        for seed in (7, 11, 23, 42):
            _demo, report = self._report(seed)
            ranked = report.sorted_players()
            top3 = {p.name for p in ranked[:3]}
            self.assertEqual(top3, {"aimbot_3", "wallhack_7", "silent_5"},
                             f"seed {seed}: os tres primeiros foram {top3}")

    def test_honest_players_stay_below_threshold(self):
        for seed in (7, 11, 23, 42):
            _demo, report = self._report(seed)
            for p in report.players:
                if p.name.startswith("honest"):
                    self.assertLess(p.score, self.cfg.scoring.review_threshold,
                                    f"seed {seed}: falso positivo em {p.name}")

    def test_recoil_flags_flat_compensation_only(self):
        _demo, report = self._report(7)
        self.assertGreater(self._player(report, "aimbot").signals["recoil"].value, 0.6)
        for p in report.players:
            if p.name.startswith("honest"):
                self.assertLess(p.signals["recoil"].value, 0.4)

    def test_movement_flags_silent_aim_only(self):
        _demo, report = self._report(7)
        silent = self._player(report, "silent").signals["movement"]
        self.assertGreater(silent.value, 0.5)
        for p in report.players:
            if p.name.startswith("honest"):
                self.assertLess(p.signals["movement"].value, 0.3)

    def test_context_flags_kills_through_things(self):
        _demo, report = self._report(7)
        wh = self._player(report, "wallhack").signals["context"]
        self.assertGreater(wh.value, 0.5)
        self.assertGreater(wh.raw["fumaca"] + wh.raw["parede"], 0)

    def test_context_needs_kill_flags_capability(self):
        demo, _ = self._report(7)
        demo2 = make_synthetic_demo(rounds=6, seed=7)
        demo2.capabilities.discard("kill_flags")
        report = analyze_demo(demo2, self.cfg)
        self.assertNotIn("context", report.players[0].signals)
        self.assertIn("context", [s["sinal"] for s in report.skipped_signals])

    def test_movement_needs_only_angles(self):
        demo = make_synthetic_demo(rounds=6, seed=7)
        demo.capabilities = {"angles"}
        report = analyze_demo(demo, self.cfg)
        self.assertIn("movement", report.players[0].signals)

    def test_recoil_ignores_players_without_shots(self):
        demo = make_synthetic_demo(rounds=6, seed=7)
        demo.shots = []
        sid = demo.steamids()[0]
        result = recoil_mod.analyze(demo, sid, self.cfg)["recoil"]
        self.assertEqual(result.value, 0.0)
        self.assertFalse(result.confident)

    def test_detectors_survive_empty_input(self):
        demo = make_synthetic_demo(rounds=2, seed=1)
        ghost = 999
        for mod in (recoil_mod, movement_mod, context_mod):
            out = mod.analyze(demo, ghost, self.cfg)
            self.assertEqual(len(out), 1)
            self.assertEqual(list(out.values())[0].value, 0.0)


class TestConverters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _pubg_events(self):
        events = [{
            "_T": "LogMatchStart", "_D": "2024-01-15T20:00:00.000Z",
            "mapName": "Erangel_Main",
            "characters": [
                {"character": {"accountId": "account.a", "name": "Ace", "teamId": 1}},
                {"character": {"accountId": "account.b", "name": "Bob", "teamId": 2}},
            ],
        }]
        for i in range(6):
            events.append({
                "_T": "LogPlayerKillV2", "_D": f"2024-01-15T20:0{i}:00.000Z",
                "killer": {"accountId": "account.a", "name": "Ace", "teamId": 1},
                "victim": {"accountId": f"account.v{i}", "name": f"V{i}",
                           "teamId": 2},
                "damageReason": "HeadShot", "damageCauserName": "WeapAK47_C",
            })
        events.append({
            "_T": "LogPlayerPosition", "_D": "2024-01-15T20:00:10.000Z",
            "character": {"accountId": "account.a", "name": "Ace", "teamId": 1,
                          "health": 100,
                          "location": {"x": 1000.0, "y": 2000.0, "z": 50.0}},
        })
        return events

    def test_pubg_reads_kills_and_map(self):
        demo = from_pubg_telemetry(self._write("t.json", self._pubg_events()))
        self.assertEqual(demo.map_name, "Erangel_Main")
        self.assertEqual(len(demo.kills), 6)
        self.assertEqual(demo.game, "pubg")

    def test_pubg_never_claims_aim_data(self):
        """O ponto principal: telemetria de PUBG nao tem mira."""
        demo = from_pubg_telemetry(self._write("t.json", self._pubg_events()))
        self.assertNotIn("angles", demo.capabilities)
        report = analyze_demo(demo, Config())
        skipped = {s["sinal"] for s in report.skipped_signals}
        self.assertTrue({"snap", "reaction", "tracking"} <= skipped)

    def test_pubg_rejects_wrong_shape(self):
        with self.assertRaises(ConversionError):
            from_pubg_telemetry(self._write("t.json", {"nao": "e lista"}))

    def test_stable_id_is_deterministic_and_outside_steam_range(self):
        a, b = _stable_id("account.a"), _stable_id("account.a")
        self.assertEqual(a, b)
        self.assertNotEqual(a, _stable_id("account.b"))
        self.assertLess(a, STEAM64)

    def _r6_rounds(self):
        return [{
            "header": {"roundNumber": n, "map": {"name": "Oregon"}},
            "players": [
                {"profileID": "uuid-a", "username": "Ace", "teamIndex": 0},
                {"profileID": "uuid-b", "username": "Bob", "teamIndex": 1},
            ],
            "matchFeedback": [{"type": "Kill", "username": "Ace",
                               "target": "Bob", "timeInSeconds": 31.0,
                               "headshot": True, "weapon": "R4-C"}],
        } for n in (1, 2)]

    def test_r6_reads_rounds_and_kills(self):
        demo = from_r6_dissect(self._write("r.json", self._r6_rounds()))
        self.assertEqual(len(demo.kills), 2)
        self.assertEqual(len(demo.rounds), 2)
        self.assertEqual(demo.map_name, "Oregon")
        self.assertIn("teams", demo.capabilities)
        self.assertNotIn("angles", demo.capabilities)

    def test_r6_accepts_type_as_object(self):
        rounds = self._r6_rounds()
        for rnd in rounds:
            rnd["matchFeedback"][0]["type"] = {"name": "Kill"}
        demo = from_r6_dissect(self._write("r.json", rounds))
        self.assertEqual(len(demo.kills), 2)

    def test_r6_rejects_unrelated_json(self):
        with self.assertRaises(ConversionError):
            from_r6_dissect(self._write("r.json", [{"header": {}}]))

    def _mapping_pair(self):
        data = {
            "frames": [
                {"frame_number": i, "players": [
                    {"player_id": STEAM64 + j, "display_name": f"p{j}",
                     "team_index": 2 + j % 2,
                     "position": {"x": j * 100.0, "y": i * 1.0, "z": 0.0},
                     "rotation": {"pitch": 0.0, "yaw": i * 2.0}}
                    for j in range(2)]}
                for i in range(5)
            ],
            "events": {"kills": [{"frame": 4, "killer_id": STEAM64,
                                  "victim_id": STEAM64 + 1,
                                  "is_headshot": True}]},
        }
        spec = {
            "meta": {"game": "meu_jogo", "tickrate": 60},
            "ticks": {"path": "frames[].players[]", "fields": {
                "tick": "$parent.frame_number", "steamid": "player_id",
                "name": "display_name", "team": "team_index",
                "x": "position.x", "y": "position.y", "z": "position.z",
                "pitch": "rotation.pitch", "yaw": "rotation.yaw"}},
            "kills": {"path": "events.kills[]", "fields": {
                "tick": "frame", "attacker": "killer_id",
                "victim": "victim_id", "headshot": "is_headshot"}},
        }
        return self._write("d.json", data), self._write("s.json", spec)

    def test_mapping_reaches_parent_fields(self):
        data_path, spec_path = self._mapping_pair()
        demo = from_mapping(data_path, spec_path)
        self.assertEqual(len(demo.players), 2)
        ticks = demo.ticks_by_player[STEAM64]
        self.assertEqual([t.tick for t in ticks], [0, 1, 2, 3, 4])
        self.assertEqual(demo.game, "meu_jogo")
        self.assertIn("angles", demo.capabilities)

    def test_mapping_without_angles_drops_aim_capability(self):
        data_path, spec_path = self._mapping_pair()
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        del spec["ticks"]["fields"]["pitch"]
        del spec["ticks"]["fields"]["yaw"]
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        demo = from_mapping(data_path, spec_path)
        self.assertNotIn("angles", demo.capabilities)
        self.assertIn("positions", demo.capabilities)

    def test_mapping_reports_useless_spec(self):
        data_path, spec_path = self._mapping_pair()
        spec_path.write_text(json.dumps(
            {"ticks": {"path": "nao.existe[]", "fields": {"tick": "x"}}}),
            encoding="utf-8")
        with self.assertRaises(ConversionError):
            from_mapping(data_path, spec_path)

    def test_write_ingest_round_trips(self):
        data_path, spec_path = self._mapping_pair()
        demo = from_mapping(data_path, spec_path)
        out = write_ingest(demo, self.root / "out.jsonl")
        back = load_ingest(out)
        self.assertEqual(set(back.players), set(demo.players))
        self.assertEqual(len(back.kills), len(demo.kills))
        self.assertEqual(back.capabilities, demo.capabilities)


class TestTrainer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _populate(self, matches=25, players=200, cheaters=20, seed=3):
        rng = random.Random(seed)
        cfg = Config()
        marked = set(range(cheaters))
        for m in range(matches):
            reports = []
            for pid in range(players):
                cheat = pid in marked
                signals = {}
                for f in ml.FEATURES:
                    base = (0.65 if f in ("snap", "recoil", "jitter") else 0.15) \
                        if cheat else 0.08
                    signals[f] = SignalResult(
                        f, max(0.0, min(1.0, rng.gauss(base, 0.18))),
                        confident=True)
                p = PlayerReport(steamid=STEAM64 + pid, name=f"p{pid}",
                                 team=2 + pid % 2, kills=15, deaths=12,
                                 headshots=6, shots=100, signals=signals)
                p.score = compute_score(signals, cfg)
                reports.append(p)
            self.store.save_report(MatchReport(source=f"m{m}.dem", map_name="x",
                                               tickrate=64, players=reports))
        for pid in marked:
            self.store.save_ban_check(STEAM64 + pid,
                                      {"VACBanned": True, "NumberOfGameBans": 1})

    def test_refuses_to_train_on_thin_data(self):
        with self.assertRaises(ml.NotEnoughData):
            ml.train(self.store)

    def test_recovers_the_planted_signals(self):
        self._populate()
        model = ml.train(self.store, epochs=250)
        top = sorted(model.weights.items(), key=lambda kv: -kv[1])[:3]
        self.assertEqual({name for name, _ in top},
                         {"snap", "recoil", "jitter"})
        self.assertGreater(model.metrics["auc"], 0.9)

    def test_split_is_by_player_not_by_observation(self):
        self._populate(matches=5)
        rows, _ = ml.build_dataset(self.store)
        train_rows, test_rows = ml.split_by_player(rows)
        train_ids = {r["steamid"] for r in train_rows}
        test_ids = {r["steamid"] for r in test_rows}
        self.assertEqual(train_ids & test_ids, set(),
                         "jogador nos dois lados = vazamento")

    def test_model_round_trips(self):
        self._populate()
        model = ml.train(self.store, epochs=100)
        path = model.save(Path(self.tmp.name) / "modelo.json")
        back = ml.Model.load(path)
        self.assertEqual(back.weights, model.weights)
        probe = {f: {"value": 0.9} for f in ml.FEATURES}
        self.assertEqual(back.score(probe), model.score(probe))

    def test_scoring_weights_drop_negative_coefficients(self):
        model = ml.Model(weights={"snap": 2.0, "burst": -1.0, "recoil": 1.0})
        weights = model.to_scoring_weights()
        self.assertNotIn("burst", weights)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=3)


class TestWatchlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        demo = make_synthetic_demo(rounds=8, seed=3, profiles=CHEATERS)
        self.report = analyze_demo(demo, Config())
        self.store.save_report(self.report)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_is_idempotent(self):
        sid = self.report.players[0].steamid
        self.assertTrue(wl.add(self.store, sid, label="x"))
        self.assertFalse(wl.add(self.store, sid, label="y"))
        self.assertEqual(len(wl.entries(self.store)), 1)

    def test_remove(self):
        sid = self.report.players[0].steamid
        wl.add(self.store, sid)
        self.assertTrue(wl.remove(self.store, sid))
        self.assertFalse(wl.remove(self.store, sid))

    def test_check_report_finds_and_counts(self):
        sid = self.report.players[0].steamid
        wl.add(self.store, sid, label="suspeito")
        hits = wl.check_report(self.store, self.report)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["steamid"], sid)
        wl.check_report(self.store, self.report)
        self.assertEqual(wl.entries(self.store)[0]["times_seen"], 2)

    def test_empty_watchlist_matches_nothing(self):
        self.assertEqual(wl.check_report(self.store, self.report), [])

    def test_pending_files_skips_analyzed_and_reacts_to_growth(self):
        folder = Path(self.tmp.name) / "replays"
        folder.mkdir()
        target = folder / "a.log"
        target.write_text("x", encoding="utf-8")
        self.assertEqual([p.name for p in wl.pending_files(self.store, folder)],
                         ["a.log"])
        wl.mark_analyzed(self.store, target)
        self.assertEqual(wl.pending_files(self.store, folder), [])
        target.write_text("x muito maior agora", encoding="utf-8")
        self.assertEqual([p.name for p in wl.pending_files(self.store, folder)],
                         ["a.log"])

    def test_pending_files_ignores_unknown_extensions(self):
        folder = Path(self.tmp.name) / "r2"
        folder.mkdir()
        (folder / "a.mp4").write_text("x", encoding="utf-8")
        (folder / "b.jsonl").write_text("x", encoding="utf-8")
        self.assertEqual([p.name for p in wl.pending_files(self.store, folder)],
                         ["b.jsonl"])


class TestHtmlReport(unittest.TestCase):
    def setUp(self):
        demo = make_synthetic_demo(rounds=10, seed=7, profiles=CHEATERS)
        self.cfg = Config()
        self.report = analyze_demo(demo, self.cfg)

    def test_renders_self_contained_page(self):
        page = htmlreport.render(self.report, self.cfg)
        self.assertIn("<!doctype html>", page)
        self.assertNotIn("http://", page.replace("http://127.0.0.1", ""))
        self.assertNotIn("cdn", page.lower())
        for p in self.report.players:
            self.assertIn(p.name, page)

    def test_escapes_player_names(self):
        self.report.players[0].name = '<script>alert(1)</script>'
        page = htmlreport.render(self.report, self.cfg)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_warns_when_source_is_limited(self):
        self.report.skipped_signals = [{"sinal": "snap", "faltou": ["angles"]}]
        page = htmlreport.render(self.report, self.cfg)
        self.assertIn("Fonte limitada", page)

    def test_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = htmlreport.write(self.report, self.cfg, Path(tmp) / "r.html")
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 1000)


class TestPlatformClients(unittest.TestCase):
    def test_faceit_requires_key(self):
        with self.assertRaises(PlatformError):
            FaceitClient("")

    def test_faceit_builds_profile_from_payloads(self):
        """Sem rede: injetamos as respostas que a API daria."""
        client = FaceitClient("chave-de-teste")
        client.player_by_steam = lambda sid, game="cs2": {
            "player_id": "abc", "nickname": "Fulano", "country": "br",
            "games": {"cs2": {"faceit_elo": 2100, "skill_level": 9}},
        }
        client.bans = lambda pid: [{"reason": "cheating", "type": "banned",
                                    "starts_at": "2024-02-01"}]
        profile = client.profile(STEAM64 + 1)
        self.assertTrue(profile["encontrado"])
        self.assertEqual(profile["nickname"], "Fulano")
        self.assertGreaterEqual(profile["risco"], 60)
        self.assertEqual(len(profile["bans"]), 1)

    def test_faceit_handles_player_without_account(self):
        client = FaceitClient("chave-de-teste")
        client.player_by_steam = lambda sid, game="cs2": None
        profile = client.profile(STEAM64 + 1)
        self.assertFalse(profile["encontrado"])
        self.assertEqual(profile["risco"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
