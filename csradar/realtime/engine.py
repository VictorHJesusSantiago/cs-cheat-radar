"""Motor de tempo real: junta as fontes ao vivo e emite alertas.

O que este motor consegue fazer enquanto a partida acontece:

  - manter o roster (quem esta jogando, aliados e inimigos) e disparar a
    triagem de conta de cada jogador assim que ele aparece;
  - consumir eventos de kill em tempo real, quando existe log de servidor, e
    rodar o sinal de ritmo sobre uma janela deslizante;
  - acumular tudo num DemoData vivo, que ao fim da partida vira relatorio.

O que ele NAO consegue, e nao vai conseguir: analisar mira ao vivo. Angulo de
visao dos outros jogadores nao esta em nenhuma fonte oficial. A analise de
mira continua sendo pos-partida, em cima da demo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..features import burst as burst_mod
from ..games.srcds_log import KILL_RE, ROUND_RE, _ensure_player, _parse_time
from ..live import parse_status_lines
from ..models import DemoData, Kill, Round
from ..scoring import analyze_demo
from .sources import gsi_roster


@dataclass
class Alert:
    kind: str
    steamid: int
    name: str
    detail: str
    at: float = field(default_factory=time.time)

    def line(self) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(self.at))
        return f"[{stamp}] {self.kind:<12} {self.name or self.steamid}: {self.detail}"


class LiveEngine:
    """Estado vivo de uma partida em andamento."""

    def __init__(self, cfg, on_alert=None, risk_lookup=None):
        self.cfg = cfg
        self.on_alert = on_alert or (lambda alert: None)
        self.risk_lookup = risk_lookup       # callable(steamid) -> dict | None
        self.demo = DemoData(source="ao_vivo", map_name="desconhecido",
                             tickrate=cfg.tickrate)
        self.demo.capabilities = {"kills", "rounds", "teams"}
        self.roster: dict = {}
        self._risk_done: set = set()
        self._base_epoch = None
        self._round_starts: list = []
        self._last_burst_check = 0.0
        self._burst_reported: set = set()

    # ------------------------------------------------------------- entrada

    def feed_log_line(self, line: str) -> None:
        """Uma linha de log de servidor Source (UDP ou arquivo)."""
        m = ROUND_RE.search(line)
        if m:
            self._add_round(self._tick(m))
            return
        m = KILL_RE.search(line)
        if not m:
            self._maybe_status(line)
            return

        tick = self._tick(m)
        att = _ensure_player(self.demo, m.group("aname"), m.group("aid"),
                             m.group("ateam"))
        vic = _ensure_player(self.demo, m.group("vname"), m.group("vid"),
                             m.group("vteam"))
        if not att or not vic:
            return
        for sid in (att, vic):
            self._note_player(sid, self.demo.name_of(sid))
        flags = m.group("flags") or ""
        self.demo.kills.append(Kill(
            tick=tick, attacker=att, victim=vic,
            weapon=m.group("weapon") or "",
            headshot="headshot" in flags.lower(),
            round_num=len(self._round_starts),
        ))
        self._check_burst(att)

    def feed_console_line(self, line: str) -> None:
        """Uma linha do console.log do cliente (saida do comando `status`)."""
        for sid, nick in parse_status_lines(line).items():
            self._note_player(sid, nick)

    def feed_gsi(self, payload: dict) -> None:
        """Um POST do Game State Integration."""
        game_map = (payload.get("map") or {}).get("name")
        if game_map:
            self.demo.map_name = game_map
        for sid, nick in gsi_roster(payload).items():
            self._note_player(sid, nick)

    def feed_status_text(self, text: str) -> None:
        """Saida bruta de um `status` (via RCON, por exemplo)."""
        for sid, nick in parse_status_lines(text).items():
            self._note_player(sid, nick)

    # -------------------------------------------------------------- estado

    def _tick(self, match) -> int:
        epoch = _parse_time(match.group("date"), match.group("time"))
        if self._base_epoch is None:
            self._base_epoch = epoch
        return int((epoch - self._base_epoch) * self.cfg.tickrate)

    def _add_round(self, tick: int) -> None:
        self._round_starts.append(tick)
        n = len(self._round_starts)
        if self.demo.rounds:
            self.demo.rounds[-1].end_tick = tick - 1
        self.demo.rounds.append(Round(number=n, start_tick=tick, end_tick=10**9))

    def _maybe_status(self, line: str) -> None:
        found = parse_status_lines(line)
        for sid, nick in found.items():
            self._note_player(sid, nick)

    def _note_player(self, steamid: int, name: str = "") -> None:
        if steamid in self.roster:
            if name and not self.roster[steamid]:
                self.roster[steamid] = name
            return
        self.roster[steamid] = name or ""
        self._emit(Alert("entrou", steamid, name, "novo jogador na partida"))
        self._run_risk(steamid, name)

    def _run_risk(self, steamid: int, name: str) -> None:
        if self.risk_lookup is None or steamid in self._risk_done:
            return
        self._risk_done.add(steamid)
        try:
            profile = self.risk_lookup(steamid)
        except Exception as exc:
            self._emit(Alert("risco_erro", steamid, name, f"consulta falhou: {exc}"))
            return
        if not profile:
            return
        if profile.get("risco", 0) >= 40:
            self._emit(Alert(
                "risco_conta", steamid, profile.get("nome") or name,
                f"risco {profile['risco']:.0f} - {', '.join(profile['motivos'])}",
            ))

    def _check_burst(self, steamid: int) -> None:
        """Roda o sinal de ritmo so no autor da ultima kill, e com parcimonia."""
        now = time.time()
        if now - self._last_burst_check < 1.0:
            return
        self._last_burst_check = now
        result = burst_mod.analyze(self.demo, steamid, self.cfg)["burst"]
        if result.value < 0.6 or not result.evidence:
            return
        latest = result.evidence[-1]
        key = (steamid, latest.tick)
        if key in self._burst_reported:
            return
        self._burst_reported.add(key)
        self._emit(Alert(
            "ritmo", steamid, self.demo.name_of(steamid),
            f"{latest.detail} (sinal fraco - confirme na demo depois)",
        ))

    def _emit(self, alert: Alert) -> None:
        self.on_alert(alert)

    # -------------------------------------------------------------- saida

    def snapshot(self):
        """Relatorio parcial com o que ja foi coletado."""
        return analyze_demo(self.demo, self.cfg)

    def summary(self) -> dict:
        return {
            "jogadores": len(self.roster),
            "kills": len(self.demo.kills),
            "rounds": len(self.demo.rounds),
            "mapa": self.demo.map_name,
        }


def run_loop(engine: LiveEngine, sources: list, poll: float = 0.5,
             stop_after: float | None = None) -> None:
    """Consome as fontes ate Ctrl+C.

    `sources` sao objetos com drain() -> lista de linhas (UdpLogSource,
    FileLogSource). O GSI empurra sozinho, por callback.
    """
    started = time.time()
    while True:
        got = False
        for src in sources:
            for line in src.drain():
                got = True
                engine.feed_log_line(line)
        if stop_after is not None and time.time() - started >= stop_after:
            return
        if not got:
            time.sleep(poll)
