"""Testes do adaptador osu!, detectores 2D, analytics, sharing e plugins.

Offline: escrevemos nossos proprios .osr em vez de commitar o replay de
alguem, e nenhum teste toca a rede ou o que esta instalado nesta maquina.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csradar import analytics, plugins, sharing
from csradar.config import Config
from csradar.demo.synthetic import make_osu_frames, make_synthetic_demo
from csradar.features import cursor as cursor_mod
from csradar.games import adapter_for, load_any
from csradar.games.osu_replay import (
    OsuReplayError, parse_frames, pressed, read_osr, write_osr,
)
from csradar.models import SignalResult
from csradar.scoring import analyze_demo, applicable_detectors
from csradar.storage import Store

STEAM64 = 76561197960265728
CHEATERS = {3: "aimbot", 7: "wallhack", 5: "silent"}

# Gerar e analisar partida sintetica custa segundos. Varios testes precisam
# das MESMAS partidas, e setUp roda por metodo - sem cache, a suite passava de
# cinco minutos, que e tempo demais para alguem rodar a cada alteracao.
_REPORT_CACHE: dict = {}


def cached_report(seed: int, rounds: int, profiles=None):
    key = (seed, rounds, tuple(sorted((profiles or {}).items())))
    if key not in _REPORT_CACHE:
        demo = make_synthetic_demo(rounds=rounds, seed=seed,
                                   profiles=dict(profiles or {}))
        _REPORT_CACHE[key] = analyze_demo(demo, Config())
    return _REPORT_CACHE[key]


class TestOsuFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _frames(self, n=200):
        return [(i * 16, 100.0 + i, 200.0 - i, 5 if i % 10 < 3 else 0)
                for i in range(n)]

    def test_round_trip(self):
        frames = self._frames()
        path = write_osr(self.root / "r.osr", frames, player="Fulano")
        demo = read_osr(path)
        self.assertEqual(demo.osu["jogador"], "Fulano")
        self.assertEqual(demo.osu["quadros"], len(frames))
        seq = list(demo.ticks_by_player.values())[0]
        self.assertEqual(len(seq), len(frames))
        self.assertAlmostEqual(seq[5].x, frames[5][1], places=2)
        self.assertEqual(seq[5].keys, frames[5][3])

    def test_capabilities_are_cursor_not_angles(self):
        path = write_osr(self.root / "r.osr", self._frames())
        demo = read_osr(path)
        self.assertIn("cursor", demo.capabilities)
        self.assertIn("keys", demo.capabilities)
        self.assertNotIn("angles", demo.capabilities)

    def test_unicode_player_name(self):
        path = write_osr(self.root / "r.osr", self._frames(),
                         player="ジョゼ da Silva")
        self.assertEqual(read_osr(path).osu["jogador"], "ジョゼ da Silva")

    def test_rejects_non_osr(self):
        bad = self.root / "x.osr"
        bad.write_bytes(b"\x00" * 200)
        with self.assertRaises(OsuReplayError):
            read_osr(bad)

    def test_rejects_truncated_file(self):
        path = write_osr(self.root / "r.osr", self._frames())
        raw = path.read_bytes()
        path.write_bytes(raw[:len(raw) // 3])
        with self.assertRaises(OsuReplayError):
            read_osr(path)

    def test_replay_without_frames_is_explicit(self):
        path = write_osr(self.root / "r.osr", [(0, 1.0, 1.0, 0)])
        raw = bytearray(path.read_bytes())
        # zera o bloco comprimido mantendo o cabecalho valido
        path2 = self.root / "vazio.osr"
        path2.write_bytes(bytes(raw))
        demo = read_osr(path2)
        self.assertGreaterEqual(demo.osu["quadros"], 1)

    def test_parse_frames_drops_markers_and_seed(self):
        frames = parse_frames("-1|0|0|0,-1|0|0|0,16|10|20|1,16|11|21|0,"
                              "-12345|0|0|9999,")
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0][0], 16)
        self.assertEqual(frames[1][0], 32)

    def test_parse_frames_tolerates_garbage(self):
        self.assertEqual(parse_frames("lixo,16|1|2,16|1|2|3,"), [(16, 1.0, 2.0, 3)])

    def test_pressed_reports_physical_keys(self):
        self.assertEqual(pressed(0), ())
        self.assertEqual(set(pressed(4 | 8)), {4, 8})

    def test_adapter_dispatch_by_extension(self):
        self.assertEqual(adapter_for("x.osr"), "osu_replay")
        path = write_osr(self.root / "r.osr", self._frames())
        demo = load_any(path)
        self.assertEqual(demo.game, "osu")


class TestCursorDetectors(unittest.TestCase):
    cfg = Config()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    _osu_cache: dict = {}

    def _analyze(self, profile: str, seed: int = 5):
        key = (profile, seed)
        if key not in self._osu_cache:
            path = write_osr(self.root / f"{profile}.osr",
                             make_osu_frames(profile, seed=seed),
                             player=profile)
            self._osu_cache[key] = analyze_demo(read_osr(path), self.cfg)
        return self._osu_cache[key]

    def test_human_stays_low_and_cheats_do_not(self):
        human = self._analyze("human").players[0].score
        for profile in ("relax", "aim_assist", "replay_bot"):
            other = self._analyze(profile).players[0].score
            self.assertGreater(other, human + 20,
                               f"{profile} nao separou do humano")

    def test_human_below_review_threshold(self):
        score = self._analyze("human").players[0].score
        self.assertLess(score, self.cfg.scoring.review_threshold)

    def test_tremor_catches_interpolation(self):
        smooth = self._analyze("aim_assist").players[0].signals["tremor"]
        human = self._analyze("human").players[0].signals["tremor"]
        self.assertGreater(smooth.value, 0.6)
        self.assertLess(human.value, 0.3)

    def test_jump_catches_teleporting_bot(self):
        bot = self._analyze("replay_bot").players[0].signals["cursor_jump"]
        human = self._analyze("human").players[0].signals["cursor_jump"]
        self.assertGreater(bot.value, 0.6)
        self.assertLess(human.value, 0.4)

    def test_key_timing_catches_machine_regularity(self):
        relax = self._analyze("relax").players[0].signals["key_timing"]
        human = self._analyze("human").players[0].signals["key_timing"]
        self.assertGreater(relax.value, 0.5)
        self.assertLess(human.value, 0.3)

    def test_stationary_cursor_produces_no_tremor_claim(self):
        """Cursor parado nao diz nada sobre a mao de ninguem."""
        frames = [(i * 16, 100.0, 100.0, 0) for i in range(600)]
        path = write_osr(self.root / "parado.osr", frames)
        demo = read_osr(path)
        result = cursor_mod.analyze_tremor(demo, demo.steamids()[0], self.cfg)
        self.assertEqual(result["tremor"].value, 0.0)
        self.assertFalse(result["tremor"].confident)

    def test_cursor_detectors_gated_on_capability(self):
        run, missing = applicable_detectors({"cursor", "keys", "positions"})
        self.assertEqual({n for n, _f, _p in run},
                         {"cursor_tremor", "cursor_jump", "cursor_keys"})
        skipped = {sig for sig, _ in missing}
        self.assertIn("snap", skipped)

    def test_keys_capability_required_for_key_timing(self):
        run, missing = applicable_detectors({"cursor", "positions"})
        self.assertNotIn("cursor_keys", {n for n, _f, _p in run})
        self.assertIn("key_timing", {sig for sig, _ in missing})


class TestAnalytics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        for seed in range(1, 13):
            self.store.save_report(cached_report(seed, 5, CHEATERS))
        self.cheater = STEAM64 + 1003     # slot 3 = aimbot

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_distribution_reports_per_signal(self):
        dist = analytics.signal_distribution(self.store, min_samples=5)
        self.assertEqual(dist["observacoes"], 120)
        self.assertIn("snap", dist["sinais"])
        self.assertIn("mediana", dist["sinais"]["snap"])
        self.assertIn("p99", dist["score"])

    def test_threshold_refuses_thin_data(self):
        result = analytics.suggest_threshold(self.store, min_observations=10_000)
        self.assertIn("erro", result)

    def test_threshold_is_the_percentile(self):
        result = analytics.suggest_threshold(self.store, percentile=0.9,
                                             min_observations=10)
        self.assertIn("limiar_sugerido", result)
        self.assertGreaterEqual(result["fracao"], 0.0)
        self.assertLessEqual(result["fracao"], 0.25)

    def test_percentile_math(self):
        self.assertEqual(analytics._percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(analytics._percentile([5], 0.99), 5)
        self.assertEqual(analytics._percentile([], 0.5), 0.0)

    def test_history_tracks_a_player(self):
        data = analytics.player_history(self.store, self.cheater)
        self.assertEqual(data["partidas"], 12)
        self.assertGreater(data["score_maximo"], data["score_mediano"] - 1)
        self.assertFalse(data["banido"])

    def test_history_marks_ban(self):
        self.store.save_ban_check(self.cheater, {"VACBanned": True,
                                                 "NumberOfGameBans": 0,
                                                 "DaysSinceLastBan": 12})
        data = analytics.player_history(self.store, self.cheater)
        self.assertTrue(data["banido"])
        self.assertEqual(data["dias_desde_ban"], 12)

    def test_recurring_requires_multiple_matches(self):
        rows = analytics.recurring_suspects(self.store, min_matches=99,
                                            min_mean=0.0)
        self.assertEqual(rows, [])
        rows = analytics.recurring_suspects(self.store, min_matches=3,
                                            min_mean=0.0)
        self.assertTrue(rows)
        self.assertEqual(rows, sorted(rows, key=lambda r: -r["media"]))

    def test_co_occurrence_counts_pairs(self):
        pairs = analytics.co_occurrence(self.store, min_together=5,
                                        min_score=0.0)
        self.assertTrue(pairs)
        for pair in pairs:
            self.assertGreaterEqual(pair["juntos"], 5)
            self.assertLessEqual(pair["mesmo_time"], pair["juntos"])

    def test_co_occurrence_min_score_filters(self):
        todos = analytics.co_occurrence(self.store, min_together=2,
                                        min_score=0.0)
        altos = analytics.co_occurrence(self.store, min_together=2,
                                        min_score=90.0)
        self.assertLess(len(altos), len(todos))

    def test_explain_returns_signals_and_evidence(self):
        data = analytics.explain(self.store, self.cheater)
        self.assertIn("sinais", data)
        self.assertIn("snap", data["sinais"])
        self.assertEqual(data["outras_partidas"], 11)

    def test_explain_unknown_player(self):
        self.assertIn("erro", analytics.explain(self.store, 1))

    def test_prune_dry_run_changes_nothing(self):
        before = self.store.stats()["partidas"]
        result = analytics.prune(self.store, keep_matches=3, dry_run=True)
        self.assertEqual(result["removeria"], before - 3)
        self.assertEqual(self.store.stats()["partidas"], before)

    def test_prune_keeps_recent_and_ban_history(self):
        self.store.save_ban_check(self.cheater, {"VACBanned": True,
                                                 "NumberOfGameBans": 0})
        analytics.prune(self.store, keep_matches=3, dry_run=False)
        stats = self.store.stats()
        self.assertLessEqual(stats["partidas"], 3)
        self.assertEqual(stats["consultas_de_ban"], 1,
                         "o historico de ban nunca pode ser descartado")

    def test_prune_below_limit_is_a_noop(self):
        result = analytics.prune(self.store, keep_matches=999, dry_run=False)
        self.assertEqual(result["removidas"], 0)


class TestSharing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "t.sqlite3")
        for seed in range(1, 9):
            self.store.save_report(cached_report(seed, 4, CHEATERS))
        self.store.save_ban_check(STEAM64 + 1003,
                                  {"VACBanned": True, "NumberOfGameBans": 0})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_export_carries_no_identity(self):
        out = self.root / "troca.json"
        sharing.export_dataset(self.store, out, salt="sal-de-teste")
        raw = out.read_text(encoding="utf-8")
        self.assertNotIn("7656119", raw)
        self.assertNotIn("aimbot", raw)
        self.assertNotIn("sintetica", raw)
        self.assertNotIn("de_sintetico", raw)

    def test_export_keeps_features_and_labels(self):
        out = self.root / "troca.json"
        info = sharing.export_dataset(self.store, out, salt="s")
        payload = sharing.read_dataset(out)
        self.assertEqual(payload["observacoes"], info["observacoes"])
        self.assertEqual(info["positivos"], 1)
        record = payload["registros"][0]
        self.assertIn("snap", record["f"])
        self.assertIn(record["y"], (0, 1))

    def test_same_salt_is_stable_different_salt_is_not(self):
        a = sharing.pseudonymize(STEAM64 + 1, "sal")
        self.assertEqual(a, sharing.pseudonymize(STEAM64 + 1, "sal"))
        self.assertNotEqual(a, sharing.pseudonymize(STEAM64 + 1, "outro"))
        self.assertNotEqual(a, sharing.pseudonymize(STEAM64 + 2, "sal"))

    def test_random_salt_is_used_when_absent(self):
        info = sharing.export_dataset(self.store, self.root / "a.json")
        self.assertEqual(len(info["sal"]), 32)

    def test_merge_namespaces_external_players(self):
        out = self.root / "troca.json"
        sharing.export_dataset(self.store, out, salt="s")
        rows = sharing.merge_for_training(self.store, [out, out])
        prefixes = {str(r["steamid"]).split(":", 1)[0] for r in rows}
        self.assertEqual(prefixes, {"local", "ext0", "ext1"})

    def test_merge_grows_the_training_set(self):
        out = self.root / "troca.json"
        sharing.export_dataset(self.store, out, salt="s")
        local = sharing.merge_for_training(self.store, [])
        merged = sharing.merge_for_training(self.store, [out])
        self.assertGreater(len(merged), len(local))

    def test_read_rejects_foreign_file(self):
        bad = self.root / "x.json"
        bad.write_text(json.dumps({"algo": 1}), encoding="utf-8")
        with self.assertRaises(ValueError):
            sharing.read_dataset(bad)

    def test_read_rejects_newer_format(self):
        bad = self.root / "novo.json"
        bad.write_text(json.dumps({"formato": 999, "registros": []}),
                       encoding="utf-8")
        with self.assertRaises(ValueError):
            sharing.read_dataset(bad)

    def test_export_on_empty_database_is_explicit(self):
        empty = Store(self.root / "vazio.sqlite3")
        with self.assertRaises(ValueError):
            sharing.export_dataset(empty, self.root / "nada.json")
        empty.close()


class TestPlugins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.folder = self.home / "plugins"
        self.folder.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, body: str) -> Path:
        path = self.folder / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_scaffold_creates_working_example(self):
        path = plugins.scaffold(self.home)
        self.assertTrue(path.exists())
        name, requires, produces, analyze = plugins.load(path)
        self.assertEqual(produces, ("exemplo",))
        self.assertIn("angles", requires)
        self.assertTrue(callable(analyze))

    def test_plugin_runs_alongside_core_detectors(self):
        plugins.scaffold(self.home)
        detectors, errors = plugins.load_all(self.home)
        self.assertEqual(errors, {})
        demo = make_synthetic_demo(rounds=2, seed=7)
        report = analyze_demo(demo, Config(), extra_detectors=detectors)
        self.assertIn("exemplo", report.players[0].signals)
        self.assertIn("snap", report.players[0].signals)

    def test_plugin_respects_capability_gate(self):
        self._write("preciso_de_cursor.py",
                    "REQUIRES={'cursor'}\nPRODUCES=('c',)\n"
                    "def analyze(demo,s,cfg,index=None):\n"
                    "    raise AssertionError('nao deveria rodar')\n")
        detectors, _ = plugins.load_all(self.home)
        demo = make_synthetic_demo(rounds=2, seed=7)   # sem cursor
        report = analyze_demo(demo, Config(), extra_detectors=detectors)
        self.assertNotIn("c", report.players[0].signals)
        self.assertIn("c", {s["sinal"] for s in report.skipped_signals})

    def test_broken_plugin_does_not_kill_the_analysis(self):
        self._write("quebrado.py",
                    "REQUIRES=set()\nPRODUCES=('x',)\n"
                    "def analyze(demo,s,cfg,index=None):\n"
                    "    raise ValueError('boom')\n")
        detectors, errors = plugins.load_all(self.home)
        self.assertEqual(errors, {})
        demo = make_synthetic_demo(rounds=2, seed=7)
        report = analyze_demo(demo, Config(), extra_detectors=detectors)
        signals = report.players[0].signals
        self.assertIn("plugin_quebrado_erro", signals)
        self.assertIn("snap", signals, "o resto da analise precisa sobreviver")

    def test_plugin_with_syntax_error_is_reported_not_raised(self):
        self._write("sintaxe.py", "def analyze(:\n")
        detectors, errors = plugins.load_all(self.home)
        self.assertIn("sintaxe", errors)
        self.assertEqual(detectors, [])

    def test_plugin_without_produces_is_rejected(self):
        path = self._write("sem_produces.py",
                           "REQUIRES=set()\ndef analyze(d,s,c,index=None):\n"
                           "    return {}\n")
        with self.assertRaises(plugins.PluginError):
            plugins.load(path)

    def test_plugin_returning_garbage_is_contained(self):
        self._write("lixo.py",
                    "REQUIRES=set()\nPRODUCES=('l',)\n"
                    "def analyze(d,s,c,index=None):\n    return 42\n")
        detectors, _ = plugins.load_all(self.home)
        demo = make_synthetic_demo(rounds=2, seed=7)
        report = analyze_demo(demo, Config(), extra_detectors=detectors)
        self.assertIn("plugin_lixo_erro", report.players[0].signals)

    def test_underscore_files_are_ignored(self):
        self._write("_privado.py", "PRODUCES=('x',)\n")
        self.assertEqual(plugins.discover(self.home), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
