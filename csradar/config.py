"""Parametros de deteccao. Tudo ajustavel via JSON (~/.csradar/config.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

TICKRATE = 64.0


def default_home() -> Path:
    return Path(os.environ.get("CSRADAR_HOME", Path.home() / ".csradar"))


@dataclass
class SnapConfig:
    """Deteccao de flick nao-humano."""

    # janela analisada antes do primeiro disparo do duelo
    lookback_ticks: int = 32
    # e depois dele: sem esta parte nao da para julgar se a mira ficou estavel,
    # que e metade do criterio de snap
    lookahead_ticks: int = 20
    # erro angular (graus) acima do qual consideramos "fora do alvo"
    off_target_deg: float = 12.0
    # erro angular abaixo do qual consideramos "travado no alvo"
    on_target_deg: float = 2.5
    # quantos ticks no maximo a transicao off->on pode levar para virar snap
    max_transition_ticks: int = 3
    # velocidade angular (graus/tick) considerada extrema
    extreme_angvel: float = 45.0
    # jitter residual (desvio padrao do erro, graus) abaixo do qual e suspeito
    low_jitter_deg: float = 0.35
    min_jitter_samples: int = 4
    # erro abaixo do qual consideramos a mira "assentada" para medir tremor
    jitter_window_deg: float = 4.0
    # apos travar, um snap real fica parado; humano oscila mais que isto
    post_lock_max_stdev: float = 1.0
    post_lock_min_ticks: int = 4


@dataclass
class ReactionConfig:
    """Tempo entre alvo entrar no campo de visao e o primeiro disparo."""

    fov_deg: float = 55.0
    max_range: float = 3000.0
    # reacoes abaixo disso sao praticamente impossiveis sem informacao previa
    impossible_ms: float = 90.0
    fast_ms: float = 140.0
    # consistencia: desvio padrao baixo demais denuncia mais que a media baixa
    low_stdev_ms: float = 25.0
    min_samples: int = 5
    # so medimos reacao quando o jogador estava SEGURANDO um angulo: se ele
    # proprio girou a mira ate o alvo, a "entrada no FOV" e obra dele e o
    # tempo medido nao significa nada
    steady_deg_per_tick: float = 3.0
    steady_ticks: int = 5


@dataclass
class TrackingConfig:
    """Proxy de wallhack: mira acompanhando inimigo sem interacao."""

    lock_deg: float = 4.0
    min_distance: float = 900.0
    min_lock_ticks: int = 24
    ignore_window_ticks: int = 96
    # o alvo precisa ter se DESLOCADO angularmente durante o episodio, senao
    # e apenas crosshair parado que o inimigo atravessou
    min_target_travel_deg: float = 8.0


@dataclass
class RecoilConfig:
    """Controle de recuo durante rajada sustentada."""

    # uma rajada e uma sequencia de disparos com no maximo este intervalo
    max_gap_ticks: int = 12
    min_shots: int = 5
    # erro angular medio, em graus, abaixo do qual a compensacao e boa demais
    perfect_error_deg: float = 1.2
    # dispersao do erro ao longo da rajada; humano sobe e corrige
    flat_spread_deg: float = 0.8
    min_bursts: int = 4


@dataclass
class MovementConfig:
    """Assinaturas de entrada nao-humana na propria movimentacao da mira."""

    # deslocamento por tick que nenhum mouse humano produz de forma sustentada
    teleport_deg: float = 90.0
    # salto minimo para investigar retorno. E baixo de proposito: o que
    # denuncia silent aim nao e o tamanho do salto, e voltar ao angulo exato
    snap_back_deg: float = 25.0
    # "silent aim": vai longe e volta em 1-2 ticks
    return_window_ticks: int = 3
    return_tolerance_deg: float = 2.0
    min_ticks: int = 2000


@dataclass
class ContextConfig:
    """Contexto das kills: fumaca, parede, sem mira."""

    min_kills: int = 8
    # taxas acima destas sao raras mesmo em jogador muito bom
    smoke_rate: float = 0.12
    wallbang_rate: float = 0.18
    noscope_rate: float = 0.20


@dataclass
class CursorConfig:
    """Entrada 2D de cursor (osu! e afins)."""

    min_samples: int = 400
    min_presses: int = 20
    # so mede tremor onde ha movimento; cursor parado nao diz nada
    min_speed: float = 0.5
    # jerk normalizado: mao humana fica alto, interpolacao fica baixo
    human_jerk: float = 0.55
    smooth_jerk: float = 0.12
    # salto de cursor entre quadros consecutivos, em pixels por tick
    jump_speed: float = 60.0
    stop_speed: float = 1.0
    # dispersao relativa da duracao das teclas
    human_key_spread: float = 0.35
    robot_key_spread: float = 0.06


@dataclass
class BurstConfig:
    """Ritmo de kills: unico sinal disponivel em fontes so-de-eventos."""

    window_ms: float = 1200.0
    min_burst_kills: int = 3
    min_kills: int = 6
    min_gap_samples: int = 8
    fast_gap_ms: float = 900.0


@dataclass
class ScoringConfig:
    """Pesos do score final (0-100) e limiares de triagem."""

    weights: dict = field(
        default_factory=lambda: {
            "snap": 0.28,
            "jitter": 0.17,
            "reaction": 0.20,
            "tracking": 0.25,
            "prefire": 0.10,
            # so entra quando a fonte nao sustenta os sinais de mira; sozinho
            # e triagem fraca e o peso reflete isso
            "burst": 0.12,
            "recoil": 0.18,
            "movement": 0.22,
            "context": 0.14,
            # entrada 2D: so aparecem em fontes de cursor (osu!)
            "tremor": 0.30,
            "cursor_jump": 0.22,
            "key_timing": 0.26,
        }
    )
    review_threshold: float = 55.0
    min_kills_for_score: int = 6
    # teto do score quando a fonte so sustenta sinais fracos (ritmo de kills).
    # Sem isto um log de servidor produziria score 100, que leria como certeza
    # quando na verdade e a evidencia mais fraca que o programa tem.
    weak_only_cap: float = 60.0


@dataclass
class SteamConfig:
    api_key: str = ""
    recheck_days: tuple = (7, 30, 60, 90)
    request_timeout: float = 15.0


@dataclass
class PlatformsConfig:
    """Chaves das outras plataformas de rotulo.

    Cada uma cobre um buraco da Steam: a FACEIT bane por conta propria e mais
    rapido que o VAC; o Battlemetrics agrega bans de servidor de comunidade,
    que e a unica punicao real em varios jogos de sobrevivencia; SteamRep,
    Leetify, OpenDota, gametools, Lichess e Chess.com respondem sem chave
    nenhuma e por isso nao aparecem aqui.
    """

    faceit_key: str = ""
    battlemetrics_key: str = ""
    pubg_key: str = ""
    pubg_shard: str = "steam"
    riot_key: str = ""
    riot_region: str = "americas"
    bungie_key: str = ""
    wargaming_key: str = ""
    wargaming_region: str = "eu"
    ballchasing_key: str = ""
    osu_key: str = ""
    openxbl_key: str = ""
    tracker_key: str = ""


@dataclass
class Config:
    snap: SnapConfig = field(default_factory=SnapConfig)
    reaction: ReactionConfig = field(default_factory=ReactionConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    burst: BurstConfig = field(default_factory=BurstConfig)
    recoil: RecoilConfig = field(default_factory=RecoilConfig)
    movement: MovementConfig = field(default_factory=MovementConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    cursor: CursorConfig = field(default_factory=CursorConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    steam: SteamConfig = field(default_factory=SteamConfig)
    platforms: PlatformsConfig = field(default_factory=PlatformsConfig)
    tickrate: float = TICKRATE

    @classmethod
    def path(cls) -> Path:
        return default_home() / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        p = path or cls.path()
        cfg = cls.from_dict(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else cls()
        env_key = os.environ.get("STEAM_API_KEY")
        if env_key:
            cfg.steam.api_key = env_key
        for env_name, attr in (("FACEIT_API_KEY", "faceit_key"),
                               ("BATTLEMETRICS_API_KEY", "battlemetrics_key"),
                               ("PUBG_API_KEY", "pubg_key"),
                               ("RIOT_API_KEY", "riot_key"),
                               ("BUNGIE_API_KEY", "bungie_key"),
                               ("WARGAMING_APP_ID", "wargaming_key"),
                               ("BALLCHASING_API_KEY", "ballchasing_key"),
                               ("OSU_API_KEY", "osu_key"),
                               ("OPENXBL_API_KEY", "openxbl_key"),
                               ("TRACKER_API_KEY", "tracker_key")):
            value = os.environ.get(env_name)
            if value:
                setattr(cfg.platforms, attr, value)
        return cfg

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for key, value in raw.items():
            if key not in known:
                continue
            current = getattr(cfg, key)
            if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
                for k, v in value.items():
                    if k in current.__dataclass_fields__:
                        setattr(current, k, v)
            else:
                setattr(cfg, key, value)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        p = path or self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return p

    def ticks_to_ms(self, ticks: float) -> float:
        return ticks / self.tickrate * 1000.0

    def ms_to_ticks(self, ms: float) -> float:
        return ms / 1000.0 * self.tickrate
