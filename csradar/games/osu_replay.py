"""Leitor e escritor de replays .osr do osu!.

Segundo adaptador NATIVO do projeto, e o unico alem do CS2 que nao depende de
ferramenta externa: o formato .osr e publico e a compressao usada e LZMA
"alone", que vem na biblioteca padrao do Python. Zero dependencia nova.

Por que osu! vale o esforco: o replay guarda a posicao do cursor a ~60 Hz e o
estado das teclas quadro a quadro. E o dado de entrada mais cru que qualquer
jogo distribui publicamente - melhor, em resolucao de intencao, do que uma
demo de CS2. E cheat ali (relax, aim assist, timewarp) tem assinatura forte
nesse dado.

O que NAO da para fazer sem o beatmap: julgar se um clique foi no tempo certo.
Entao nada aqui olha para acerto ou erro de nota - so para a forma do
movimento e a regularidade da entrada.

Formato (little endian):
    byte    modo de jogo
    int32   versao do cliente
    string  md5 do beatmap
    string  nome do jogador
    string  md5 do replay
    int16   300s, 100s, 50s, gekis, katus, misses
    int32   score
    int16   combo maximo
    byte    perfect
    int32   mods
    string  grafico da barra de vida
    int64   timestamp (ticks do Windows)
    int32   tamanho dos dados comprimidos
    bytes   frames em LZMA alone
    int64   id do score online (versao >= 20140721)

String usa o esquema do .NET: 0x00 = vazia, 0x0b = seguida de ULEB128 com o
tamanho em bytes UTF-8.
"""

from __future__ import annotations

import lzma
import struct
from pathlib import Path

from ..models import DemoData, PlayerInfo, PlayerTick
from .base import CAP_CURSOR, CAP_KEYS, CAP_POSITIONS

# ticks de 100 ns entre 01/01/0001 e 01/01/1970
_EPOCH_TICKS = 621355968000000000
_TICKS_PER_SECOND = 10_000_000

MODES = {0: "osu!", 1: "taiko", 2: "catch", 3: "mania"}

# bitmask das teclas nos frames
KEY_M1 = 1
KEY_M2 = 2
KEY_K1 = 4
KEY_K2 = 8
KEY_SMOKE = 16
# K1/K2 tambem acendem M1/M2; para contar pressionamentos usamos so os bits
# que representam uma tecla fisica distinta
PHYSICAL_KEYS = (KEY_M1, KEY_M2, KEY_K1, KEY_K2)


class OsuReplayError(ValueError):
    pass


# ------------------------------------------------------------------ leitura


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise OsuReplayError(
                f"arquivo truncado: faltam bytes na posicao {self.pos}")

    def byte(self) -> int:
        self._need(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        self._need(size)
        value = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def short(self) -> int:
        return self.unpack("<H")

    def integer(self) -> int:
        return self.unpack("<i")

    def long(self) -> int:
        return self.unpack("<q")

    def uleb128(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise OsuReplayError("ULEB128 absurdamente longo")

    def string(self) -> str:
        marker = self.byte()
        if marker == 0x00:
            return ""
        if marker != 0x0B:
            raise OsuReplayError(
                f"marcador de string invalido: 0x{marker:02x} "
                f"(o arquivo provavelmente nao e um .osr)")
        length = self.uleb128()
        self._need(length)
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode("utf-8", errors="replace")

    def blob(self, length: int) -> bytes:
        self._need(length)
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw


def parse_frames(text: str) -> list:
    """Converte 'w|x|y|z,w|x|y|z,...' em [(ms_absoluto, x, y, keys)].

    Os dois primeiros quadros costumam vir com w = -1 (marcadores) e o ultimo
    com w = -12345 carregando a semente do RNG. Nenhum deles e movimento, e
    incluir isso arruinaria qualquer medida de velocidade.
    """
    frames = []
    clock = 0
    for chunk in text.split(","):
        if not chunk:
            continue
        parts = chunk.split("|")
        if len(parts) != 4:
            continue
        try:
            delta = int(float(parts[0]))
            x = float(parts[1])
            y = float(parts[2])
            keys = int(float(parts[3]))
        except ValueError:
            continue
        if delta == -12345:      # semente do RNG, nao e um quadro
            continue
        if delta < 0:            # marcadores do inicio
            continue
        clock += delta
        frames.append((clock, x, y, keys))
    return frames


def read_osr(path, sample_rate: float = 60.0) -> DemoData:
    """Le um .osr e devolve DemoData com cursor e teclas por amostra.

    `sample_rate` so define a escala de tick usada no relatorio; os tempos
    reais vem em milissegundos do proprio replay e sao convertidos.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"replay nao encontrado: {p}")

    r = _Reader(p.read_bytes())
    mode = r.byte()
    version = r.integer()
    beatmap_md5 = r.string()
    player = r.string()
    r.string()                                  # md5 do replay
    counts = [r.short() for _ in range(6)]
    score = r.integer()
    max_combo = r.short()
    perfect = r.byte()
    mods = r.integer()
    r.string()                                  # grafico da barra de vida
    timestamp = r.long()
    length = r.integer()

    if length < 0:
        raise OsuReplayError("tamanho de bloco comprimido negativo")
    compressed = r.blob(length) if length else b""

    frames = []
    if compressed:
        try:
            raw = lzma.decompress(compressed, format=lzma.FORMAT_ALONE)
        except lzma.LZMAError as exc:
            raise OsuReplayError(
                f"nao consegui descomprimir os quadros: {exc}") from exc
        frames = parse_frames(raw.decode("utf-8", errors="replace"))

    if not frames:
        raise OsuReplayError(
            "o replay nao tem quadros de movimento. Replays baixados do site "
            "as vezes vem sem os dados de entrada."
        )

    demo = DemoData(source=p.name, map_name=beatmap_md5 or "desconhecido",
                    game="osu", tickrate=sample_rate)
    demo.capabilities = {CAP_CURSOR, CAP_KEYS, CAP_POSITIONS}

    sid = _player_id(player)
    demo.players[sid] = PlayerInfo(sid, player or "desconhecido", 0)
    ticks = []
    for ms, x, y, keys in frames:
        ticks.append(PlayerTick(
            tick=int(ms * sample_rate / 1000.0), steamid=sid,
            x=x, y=y, z=0.0, pitch=0.0, yaw=0.0,
            keys=keys, round_num=1,
        ))
    demo.ticks_by_player[sid] = ticks

    demo.osu = {
        "modo": MODES.get(mode, str(mode)),
        "versao": version,
        "jogador": player,
        "beatmap_md5": beatmap_md5,
        "score": score,
        "combo_maximo": max_combo,
        "perfect": bool(perfect),
        "mods": mods,
        "acertos": {"300": counts[0], "100": counts[1], "50": counts[2],
                    "geki": counts[3], "katu": counts[4], "miss": counts[5]},
        "quando": _unix_time(timestamp),
        "quadros": len(frames),
        "duracao_ms": frames[-1][0] if frames else 0,
    }
    return demo


def _player_id(name: str) -> int:
    """ID numerico estavel a partir do nome (osu! nao usa SteamID)."""
    import hashlib

    digest = hashlib.sha1((name or "?").encode("utf-8")).digest()
    return int.from_bytes(digest[:7], "big") | (1 << 54)


def _unix_time(ticks: int) -> int:
    if ticks <= _EPOCH_TICKS:
        return 0
    return int((ticks - _EPOCH_TICKS) / _TICKS_PER_SECOND)


# ------------------------------------------------------------------ escrita


def _write_string(text: str) -> bytes:
    if not text:
        return b"\x00"
    raw = text.encode("utf-8")
    out = bytearray(b"\x0b")
    length = len(raw)
    while True:
        byte = length & 0x7F
        length >>= 7
        out.append(byte | (0x80 if length else 0))
        if not length:
            break
    return bytes(out) + raw


def write_osr(path, frames, player: str = "jogador", mode: int = 0,
              beatmap_md5: str = "0" * 32, mods: int = 0,
              version: int = 20240101) -> Path:
    """Escreve um .osr valido.

    Existe para os testes: sem um escritor, testar o leitor exigiria commitar
    um replay real de alguem no repositorio, o que nao e aceitavel nem util.
    """
    parts = []
    previous = 0
    for ms, x, y, keys in frames:
        parts.append(f"{int(ms - previous)}|{x:g}|{y:g}|{int(keys)}")
        previous = ms
    payload = (",".join(parts) + ",").encode("utf-8")
    compressed = lzma.compress(payload, format=lzma.FORMAT_ALONE)

    out = bytearray()
    out.append(mode & 0xFF)
    out += struct.pack("<i", version)
    out += _write_string(beatmap_md5)
    out += _write_string(player)
    out += _write_string("0" * 32)
    out += struct.pack("<6H", 0, 0, 0, 0, 0, 0)
    out += struct.pack("<i", 0)
    out += struct.pack("<H", 0)
    out.append(0)
    out += struct.pack("<i", mods)
    out += _write_string("")
    out += struct.pack("<q", _EPOCH_TICKS)
    out += struct.pack("<i", len(compressed))
    out += compressed
    out += struct.pack("<q", 0)

    target = Path(path)
    target.write_bytes(bytes(out))
    return target


def pressed(keys: int) -> tuple:
    """Quais teclas fisicas estao pressionadas neste quadro."""
    return tuple(k for k in PHYSICAL_KEYS if keys & k)
