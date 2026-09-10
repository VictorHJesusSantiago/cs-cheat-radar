"""O que cada jogo permite medir.

A honestidade do projeto inteiro depende desta camada. Os detectores de mira
precisam de angulo de visao por tick; a maioria esmagadora dos jogos nao expoe
isso de forma alguma sem tocar no processo - e tocar no processo esta fora de
questao. Entao cada fonte declara o que consegue entregar, e o motor de score
roda apenas os sinais que aquela fonte sustenta.

O resultado pratico: para CS2 sai analise de mira; para um log de servidor
Source sai apenas triagem fraca por ritmo de kills; para Valorant nao sai nada
alem de risco de conta. Isso e uma limitacao real, nao um detalhe de
implementacao, e o relatorio precisa dizer isso na cara do usuario.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# --------------------------------------------------------------- capacidades

CAP_ANGLES = "angles"        # pitch/yaw por tick, de todos os jogadores
CAP_POSITIONS = "positions"  # x/y/z por tick, de todos os jogadores
CAP_SHOTS = "shots"          # evento de disparo com tick
CAP_KILLS = "kills"          # evento de morte com tick e autor
CAP_ROUNDS = "rounds"        # limites de round
CAP_TEAMS = "teams"          # times, para saber quem e inimigo
CAP_KILL_FLAGS = "kill_flags"  # fumaca/parede/noscope por kill (so CS2 hoje)
CAP_CURSOR = "cursor"        # posicao 2D de cursor por amostra (osu! e afins)
CAP_KEYS = "keys"            # estado das teclas por amostra

ALL_CAPS = (CAP_ANGLES, CAP_POSITIONS, CAP_SHOTS, CAP_KILLS, CAP_ROUNDS,
            CAP_TEAMS, CAP_KILL_FLAGS, CAP_CURSOR, CAP_KEYS)


class Support(Enum):
    """Quanto do programa funciona para um jogo."""

    FULL = "completo"          # replay com angulo por tick: todos os sinais
    POSITIONAL = "posicional"  # posicoes por tick, sem angulo de visao
    EVENTS = "eventos"         # so eventos (kills/rounds): triagem fraca
    ACCOUNT = "conta"          # nenhum replay legivel: so risco de conta
    NONE = "nenhum"

    def __str__(self) -> str:
        return self.value


SUPPORT_CAPS = {
    Support.FULL: {CAP_ANGLES, CAP_POSITIONS, CAP_SHOTS, CAP_KILLS,
                   CAP_ROUNDS, CAP_TEAMS},   # kill_flags e opcional mesmo aqui
    Support.POSITIONAL: {CAP_POSITIONS, CAP_KILLS, CAP_ROUNDS, CAP_TEAMS},
    Support.EVENTS: {CAP_KILLS, CAP_ROUNDS, CAP_TEAMS},
    Support.ACCOUNT: set(),
    Support.NONE: set(),
}


@dataclass
class GameProfile:
    """Uma entrada do catalogo de jogos."""

    key: str
    name: str
    support: Support
    # como chegar no artefato analisavel
    artifacts: tuple = ()          # descricao humana de onde ficam replays/logs
    patterns: tuple = ()           # globs relativos a pasta de instalacao
    adapter: str = ""              # nome do adaptador que le esse artefato
    converter: str = ""            # ferramenta externa que produz a ingestao
    steam_appid: int | None = None
    launchers: tuple = ()          # ("steam", "epic", "riot", ...)
    live: tuple = ()               # fontes de tempo real disponiveis
    note: str = ""

    @property
    def caps(self) -> set:
        return set(SUPPORT_CAPS[self.support])

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "nome": self.name,
            "suporte": str(self.support),
            "adaptador": self.adapter,
            "conversor": self.converter,
            "artefatos": list(self.artifacts),
            "steam_appid": self.steam_appid,
            "launchers": list(self.launchers),
            "tempo_real": list(self.live),
            "nota": self.note,
        }


@dataclass
class InstalledGame:
    """Um jogo encontrado no disco por um scanner de launcher."""

    name: str
    launcher: str
    install_dir: str = ""
    app_id: str = ""
    size_bytes: int = 0
    profile: GameProfile | None = None

    @property
    def support(self) -> Support:
        return self.profile.support if self.profile else Support.NONE

    def as_dict(self) -> dict:
        return {
            "nome": self.name,
            "launcher": self.launcher,
            "pasta": self.install_dir,
            "app_id": self.app_id,
            "tamanho_gb": round(self.size_bytes / 1024**3, 1) if self.size_bytes else None,
            "suporte": str(self.support),
            "jogo_conhecido": self.profile.key if self.profile else None,
        }


@dataclass
class AdapterInfo:
    """Metadados de um adaptador de leitura."""

    name: str
    description: str
    caps: set = field(default_factory=set)
    extensions: tuple = ()
    requires: tuple = ()   # dependencias externas

    def supports_file(self, path) -> bool:
        return str(path).lower().endswith(self.extensions)
