"""Leitura ao vivo do console.log durante a partida.

Requer a launch option -condebug no CS2. O jogo passa a escrever tudo o que
aparece no console em

    ...\\Steam\\steamapps\\common\\Counter-Strike Global Offensive\\game\\csgo\\console.log

Voce digita `status` no console do jogo; nos lemos o arquivo. Nenhuma leitura
de memoria, nenhuma injecao, nenhum hook: apenas um arquivo de texto que o
proprio jogo escreve. Isso e seguro em relacao ao VAC.

O que sai daqui e perfil de risco de conta, nao deteccao de cheat.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .steam import to_steam64

# "# 3 1 "nick" STEAM_1:0:12345678 12:34 45 0 active 786000"
_STATUS_RE = re.compile(
    r'^#\s*\d+\s+\d+\s+"(?P<name>.*)"\s+(?P<sid>STEAM_[0-5]:[01]:\d+|\[U:1:\d+\])',
)
# CS2 tambem imprime linhas no formato: "  3   nick   [U:1:123]  ..."
_LOOSE_RE = re.compile(r'"(?P<name>[^"]{1,64})"\s+(?P<sid>STEAM_[0-5]:[01]:\d+|\[U:1:\d+\])')

DEFAULT_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log",
    r"D:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log",
    r"E:\SteamLibrary\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log",
]


def find_console_log(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    env = os.environ.get("CSRADAR_CONSOLE_LOG")
    if env and Path(env).exists():
        return Path(env)
    for candidate in DEFAULT_PATHS:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def parse_status_lines(text: str) -> dict:
    """Extrai {steam64: nick} de um trecho de console.log."""
    found = {}
    for line in text.splitlines():
        m = _STATUS_RE.match(line.strip()) or _LOOSE_RE.search(line)
        if not m:
            continue
        sid = to_steam64(m.group("sid"))
        if sid:
            found[sid] = m.group("name").strip()
    return found


def tail(path: Path, from_start: bool = False, poll: float = 1.0):
    """Gerador de linhas novas, tolerante a truncamento do arquivo."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        if not from_start:
            fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        while True:
            line = fh.readline()
            if line:
                pos = fh.tell()
                yield line
                continue
            try:
                if path.stat().st_size < pos:   # o jogo reiniciou e truncou
                    fh.seek(0)
                    pos = 0
                    continue
            except OSError:
                pass
            time.sleep(poll)


def watch(path: Path, on_players, poll: float = 1.0, from_start: bool = False) -> None:
    """Chama on_players({steam64: nick}) sempre que novos jogadores aparecerem.

    Bloqueia ate Ctrl+C.
    """
    seen: set = set()
    buffer: dict = {}
    last_flush = time.time()
    for line in tail(path, from_start=from_start, poll=poll):
        found = parse_status_lines(line)
        for sid, nick in found.items():
            if sid not in seen:
                buffer[sid] = nick
        # agrupa os 10 jogadores de um mesmo `status` em uma unica chamada
        if buffer and (len(buffer) >= 10 or time.time() - last_flush > 2.0):
            seen.update(buffer)
            on_players(dict(buffer))
            buffer.clear()
            last_flush = time.time()
