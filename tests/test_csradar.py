"""Testes do cs-cheat-radar. Rodam sem demoparser2 e sem rede.

    python -m pytest tests -q        (ou)      python tests/test_csradar.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csradar.config import Config
from csradar.demo.synthetic import make_synthetic_demo
from csradar.features.geometry import (
    angle_diff, angular_error, angular_speed, desired_angles, ramp, stdev,
)
from csradar.labeling import evaluate_threshold
from csradar.live import parse_status_lines
from csradar.models import PlayerTick
from csradar.scoring import analyze_demo, compute_score
from csradar.steam import to_steam3, to_steam64
from csradar.storage import Store


def tick(x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0, sid=1, t=0):
    return PlayerTick(tick=t, steamid=sid, x=x, y=y, z=z, pitch=pitch, yaw=yaw)


class TestGeometry(unittest.TestCase):
    def test_angle_diff_wraps(self):
        self.assertAlmostEqual(angle_diff(350, 10), -20.0)
        self.assertAlmostEqual(angle_diff(10, 350), 20.0)
        self.assertAlmostEqual(angle_diff(180, 0), 180.0)

    def test_aim_straight_at_target_has_zero_error(self):
        me = tick(0, 0, 0, pitch=0, yaw=0)
        him = tick(500, 0, 0, sid=2)
        self.assertLess(angular_error(me, him), 0.01)

    def test_error_grows_with_misalignment(self):
        him = tick(500, 0, 0, sid=2)
        for yaw in (5, 30, 90):
            me = tick(0, 0, 0, yaw=yaw)
            self.assertAlmostEqual(angular_error(me, him), yaw, delta=0.5)

    def test_pitch_sign_is_source_convention(self):
        """Pitch positivo olha para baixo."""
        me = tick(0, 0, 500)
        below = tick(0, 0, 0, sid=2)
        pitch, _ = desired_angles(me, below)
        self.assertGreater(pitch, 0)

    def test_angular_speed_symmetric(self):
        a, b = tick(yaw=0), tick(yaw=40)
        self.assertAlmostEqual(angular_speed(a, b), 40.0, delta=0.5)
        self.assertAlmostEqual(angular_speed(b, a), 40.0, delta=0.5)

    def test_spherical_not_axis_sum(self):
        """Perto do polo, somar eixos superestimaria o erro."""
        me = tick(0, 0, 0, pitch=85, yaw=0)
        other = tick(0, 0, 0, pitch=85, yaw=90)
        self.assertLess(angular_speed(me, other), 20.0)

    def test_ramp_and_stdev(self):
        self.assertEqual(ramp(5, 0, 10), 0.5)
        self.assertEqual(ramp(-3, 0, 10), 0.0)
        self.assertEqual(ramp(99, 0, 10), 1.0)
        self.assertAlmostEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9]), 2.138, places=2)


class TestSteamIds(unittest.TestCase):
    def test_steam2_conversion(self):
        self.assertEqual(to_steam64("STEAM_1:1:12345"), 76561197960265728 + 24690 + 1)

    def test_steam3_conversion(self):
        self.assertEqual(to_steam64("[U:1:24691]"), 76561197960265728 + 24691)

    def test_steam64_passthrough_and_roundtrip(self):
        sid = 76561198000000000
        self.assertEqual(to_steam64(str(sid)), sid)
        self.assertEqual(to_steam64(to_steam3(sid)), sid)

    def test_garbage_returns_none(self):
        self.assertIsNone(to_steam64("nao e um id"))


class TestConsoleLog(unittest.TestCase):
    SAMPLE = '''
# userid name uniqueid connected ping loss state rate
#  2 1 "Fulano" STEAM_1:0:12345 05:11 34 0 active 786432
#  3 1 "cara com espaco" STEAM_1:1:99999 02:00 51 0 active 786432
linha irrelevante do console
'''

    def test_parses_status_block(self):
        found = parse_status_lines(self.SAMPLE)
        self.assertEqual(len(found), 2)
        self.assertIn(to_steam64("STEAM_1:0:12345"), found)
        self.assertEqual(found[to_steam64("STEAM_1:1:99999")], "cara com espaco")

    def test_ignores_noise(self):
        self.assertEqual(parse_status_lines("nada aqui\noutra linha"), {})


class TestDetectors(unittest.TestCase):
    """O simulador sabe quem cheatou; a demo real nunca sabe."""

    cfg = Config()
    _cache: dict = {}

    def _run(self, seed: int, rounds: int = 24):
        key = (seed, rounds)
        if key not in self._cache:
            demo = make_synthetic_demo(rounds=rounds, seed=seed)
            self._cache[key] = analyze_demo(demo, self.cfg)
        return self._cache[key]

    def test_cheaters_rank_above_every_honest_player(self):
        for seed in (7, 11, 23, 42, 101):
            report = self._run(seed)
            ranked = report.sorted_players()
            top2 = {p.name for p in ranked[:2]}
            self.assertEqual(
                top2, {"aimbot_3", "wallhack_7"},
                f"seed {seed}: os dois primeiros foram {top2}",
            )

    def test_honest_players_stay_below_review_threshold(self):
        for seed in (7, 11, 23, 42, 101):
            report = self._run(seed)
            for p in report.players:
                if p.name.startswith("honest"):
                    self.assertLess(
                        p.score, self.cfg.scoring.review_threshold,
                        f"seed {seed}: falso positivo em {p.name} ({p.score})",
                    )

    def test_aimbot_triggers_aim_signals_specifically(self):
        report = self._run(23)
        aim = next(p for p in report.players if p.name == "aimbot_3")
        self.assertGreater(aim.signals["snap"].value, 0.5)
        self.assertGreater(aim.signals["jitter"].value, 0.5)

    def test_wallhack_triggers_information_signals_specifically(self):
        report = self._run(7)
        wh = next(p for p in report.players if p.name == "wallhack_7")
        self.assertGreater(
            max(wh.signals["tracking"].value, wh.signals["prefire"].value), 0.5
        )

    def test_all_honest_match_produces_no_review_queue(self):
        demo = make_synthetic_demo(rounds=20, seed=5, profiles={})
        report = analyze_demo(demo, self.cfg)
        flagged = [p.name for p in report.players if "REVISAR" in p.flags]
        self.assertEqual(flagged, [])

    def test_evidence_points_to_a_real_tick(self):
        report = self._run(7)
        wh = next(p for p in report.players if p.name == "wallhack_7")
        lo, hi = 0, 10**9
        for ev in wh.top_evidence():
            self.assertGreaterEqual(ev.tick, lo)
            self.assertLess(ev.tick, hi)
            self.assertTrue(ev.detail)


class TestScoring(unittest.TestCase):
    def test_unconfident_signals_are_discounted(self):
        from csradar.models import SignalResult

        cfg = Config()
        confident = {"snap": SignalResult("snap", 1.0, confident=True)}
        shaky = {"snap": SignalResult("snap", 1.0, confident=False)}
        # com um unico sinal a media ponderada normaliza para 100 nos dois
        # casos; o desconto aparece quando ha sinais concorrentes
        confident["tracking"] = SignalResult("tracking", 0.0, confident=True)
        shaky["tracking"] = SignalResult("tracking", 0.0, confident=True)
        self.assertGreater(compute_score(confident, cfg), compute_score(shaky, cfg))

    def test_empty_signals_score_zero(self):
        self.assertEqual(compute_score({}, Config()), 0.0)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        demo = make_synthetic_demo(rounds=8, seed=3)
        self.report = analyze_demo(demo, Config())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_save_is_idempotent(self):
        a = self.store.save_report(self.report)
        b = self.store.save_report(self.report)
        self.assertEqual(a, b)
        self.assertEqual(self.store.stats()["observacoes"], 10)

    def test_top_suspects_ordered(self):
        self.store.save_report(self.report)
        rows = self.store.top_suspects(limit=10)
        self.assertEqual(rows, sorted(rows, key=lambda r: -r["score_medio"]))

    def test_ban_check_creates_label(self):
        self.store.save_report(self.report)
        sid = self.store.top_suspects(limit=1)[0]["steamid"]
        self.store.save_ban_check(sid, {"VACBanned": True, "NumberOfGameBans": 1,
                                        "DaysSinceLastBan": 12})
        labeled = {r["steamid"]: r["label"] for r in self.store.labeled_dataset()}
        self.assertEqual(labeled[sid], 1)
        self.assertEqual(sum(labeled.values()), 1)

    def test_recheck_queue_respects_age(self):
        self.store.save_report(self.report)
        self.assertEqual(self.store.steamids_pending_recheck(min_age_days=30), [])
        self.assertEqual(len(self.store.steamids_pending_recheck(min_age_days=0)), 10)

    def test_evaluate_threshold_counts(self):
        self.store.save_report(self.report)
        sid = self.store.top_suspects(limit=1)[0]["steamid"]
        self.store.save_ban_check(sid, {"VACBanned": True, "NumberOfGameBans": 0})
        res = evaluate_threshold(self.store, 0.0)
        self.assertEqual(res["jogadores"], 10)
        self.assertEqual(res["banidos_conhecidos"], 1)
        self.assertEqual(res["fn"], 0)


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config()
            cfg.scoring.review_threshold = 71.0
            cfg.steam.api_key = "abc"
            cfg.save(path)
            os.environ.pop("STEAM_API_KEY", None)
            back = Config.load(path)
            self.assertEqual(back.scoring.review_threshold, 71.0)
            self.assertEqual(back.steam.api_key, "abc")

    def test_tick_conversions(self):
        cfg = Config()
        self.assertAlmostEqual(cfg.ticks_to_ms(64), 1000.0)
        self.assertAlmostEqual(cfg.ms_to_ticks(500), 32.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
