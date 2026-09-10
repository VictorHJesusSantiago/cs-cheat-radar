"""Fontes de dados ao vivo - todas oficiais, nenhuma toca no processo do jogo.

Quatro caminhos, do mais rico ao mais pobre:

  srcds_udp    servidor dedicado seu, com `logaddress_add ip:porta`. Eventos
               completos em tempo real. Melhor fonte que existe sem cheat.
  srcds_file   o mesmo, lendo o arquivo de log em vez da rede.
  gsi          Game State Integration do CS2. Recurso oficial da Valve: o jogo
               faz POST de JSON para o seu localhost. Traz placar, round e
               estado do jogador - NAO traz posicao nem angulo de visao, e o
               bloco `allplayers` so vem quando voce esta assistindo/observando.
  console_log  `-condebug` + comando `status`. So a lista de quem esta na
               partida, para triagem de conta.

O que nenhuma delas da: posicao e mira dos outros jogadores enquanto a partida
acontece. Isso so existe na memoria do cliente, e ler de la e escrever um
wallhack. Por isso a analise de mira continua sendo pos-partida.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# --------------------------------------------------------------------- GSI

GSI_CONFIG_TEMPLATE = """"cs-cheat-radar"
{{
    "uri"          "http://127.0.0.1:{port}"
    "timeout"      "5.0"
    "buffer"       "0.1"
    "throttle"     "0.1"
    "heartbeat"    "10.0"
    "auth"
    {{
        "token"    "{token}"
    }}
    "data"
    {{
        "provider"            "1"
        "map"                 "1"
        "round"               "1"
        "player_id"           "1"
        "player_state"        "1"
        "player_weapons"      "1"
        "player_match_stats"  "1"
        "allplayers_id"       "1"
        "allplayers_state"    "1"
        "allplayers_match_stats" "1"
        "allgrenades"         "1"
    }}
}}
"""

GSI_FILENAME = "gamestate_integration_csradar.cfg"


def install_gsi_config(cfg_dir, port: int = 3000, token: str = "csradar") -> Path:
    """Escreve o arquivo de configuracao do GSI na pasta cfg do CS2."""
    path = Path(cfg_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"pasta cfg nao encontrada: {path}\n"
            r"Normalmente e ...\Counter-Strike Global Offensive\game\csgo\cfg"
        )
    target = path / GSI_FILENAME
    target.write_text(GSI_CONFIG_TEMPLATE.format(port=port, token=token),
                      encoding="utf-8")
    return target


class _GSIHandler(BaseHTTPRequestHandler):
    server_version = "csradar-gsi"

    def do_POST(self):  # noqa: N802 (nome exigido pela stdlib)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        token = self.server.expected_token
        if token:
            got = (payload.get("auth") or {}).get("token")
            if got != token:
                return
        self.server.sink(payload)

    def log_message(self, *args):
        pass  # o servidor do GSI recebe dezenas de POSTs por segundo


class GSIServer:
    """Servidor HTTP local que recebe o estado do jogo."""

    def __init__(self, port: int = 3000, token: str = "csradar", sink=None):
        self.port = port
        self.token = token
        self.sink = sink or (lambda payload: None)
        self._httpd = None
        self._thread = None

    def start(self) -> None:
        httpd = HTTPServer(("127.0.0.1", self.port), _GSIHandler)
        httpd.expected_token = self.token
        httpd.sink = self.sink
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def gsi_roster(payload: dict) -> dict:
    """Extrai {steam64: nick} de um payload do GSI.

    `allplayers` so aparece quando voce esta observando a partida; num jogo
    normal vem apenas o seu proprio bloco `player`. Nao ha jeito oficial de
    contornar isso, e tentar contornar seria exatamente o que nao vamos fazer.
    """
    out = {}
    for sid, info in (payload.get("allplayers") or {}).items():
        try:
            out[int(sid)] = str((info or {}).get("name", ""))
        except (TypeError, ValueError):
            continue
    me = payload.get("player") or {}
    if me.get("steamid"):
        try:
            out.setdefault(int(me["steamid"]), str(me.get("name", "")))
        except (TypeError, ValueError):
            pass
    return out


# ------------------------------------------------------------ log do servidor


class UdpLogSource:
    """Recebe log de servidor Source via `logaddress_add ip:porta`.

    Esta e a unica fonte que entrega eventos de todos os jogadores em tempo
    real sem tocar em nada - e por isso exige um servidor que voce controle.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 27500):
        self.host = host
        self.port = port
        self._sock = None
        self._thread = None
        self._running = False
        self.lines: queue.Queue = queue.Queue()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.5)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            # o srcds prefixa 4 bytes 0xFF e um byte de tipo
            text = data.lstrip(b"\xff").decode("utf-8", errors="replace")
            for line in text.splitlines():
                line = line.strip("\x00 \r\n")
                if line:
                    self.lines.put(line)

    def stop(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
            self._sock = None

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.lines.get_nowait())
            except queue.Empty:
                return out


class FileLogSource:
    """Tail de um arquivo de log (servidor Source ou console.log do cliente)."""

    def __init__(self, path, from_start: bool = False, poll: float = 0.5):
        self.path = Path(path)
        self.from_start = from_start
        self.poll = poll
        self._fh = None
        self._pos = 0

    def start(self) -> None:
        self._fh = open(self.path, "r", encoding="utf-8", errors="replace")
        if not self.from_start:
            self._fh.seek(0, os.SEEK_END)
        self._pos = self._fh.tell()

    def stop(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def drain(self) -> list:
        if not self._fh:
            return []
        out = []
        while True:
            line = self._fh.readline()
            if not line:
                break
            self._pos = self._fh.tell()
            out.append(line.rstrip("\r\n"))
        try:
            if self.path.stat().st_size < self._pos:   # arquivo truncado
                self._fh.seek(0)
                self._pos = 0
        except OSError:
            pass
        return out


# -------------------------------------------------------------------- RCON


class RconError(RuntimeError):
    pass


class RconClient:
    """Source RCON. Serve para pedir `status` no SEU servidor e ter o roster.

    Protocolo simples: pacote com id, tipo e corpo terminado em dois nulos.
    """

    AUTH = 3
    AUTH_RESPONSE = 2
    EXECCOMMAND = 2
    RESPONSE_VALUE = 0

    def __init__(self, host: str, port: int = 27015, password: str = "",
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._id = 0

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        self._send(self.AUTH, self.password)
        # a Valve responde primeiro um RESPONSE_VALUE vazio e depois o auth
        for _ in range(2):
            pid, ptype, _body = self._recv()
            if ptype == self.AUTH_RESPONSE:
                if pid == -1:
                    raise RconError("senha de RCON recusada")
                return
        raise RconError("resposta de autenticacao inesperada")

    def command(self, text: str) -> str:
        if not self._sock:
            raise RconError("nao conectado")
        self._send(self.EXECCOMMAND, text)
        chunks = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                _pid, ptype, body = self._recv()
            except (socket.timeout, RconError):
                break
            if ptype == self.RESPONSE_VALUE:
                chunks.append(body)
                if len(body) < 3000:      # respostas longas vem fatiadas
                    break
        return "".join(chunks)

    def close(self) -> None:
        if self._sock:
            self._sock.close()
            self._sock = None

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ----------------------------------------------------------- protocolo

    def _send(self, ptype: int, body: str) -> None:
        self._id += 1
        payload = struct.pack("<ii", self._id, ptype) + body.encode("utf-8") + b"\x00\x00"
        self._sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv(self) -> tuple:
        raw = self._read_exactly(4)
        (length,) = struct.unpack("<i", raw)
        if length < 10 or length > 8192:
            raise RconError(f"tamanho de pacote invalido: {length}")
        payload = self._read_exactly(length)
        pid, ptype = struct.unpack("<ii", payload[:8])
        body = payload[8:-2].decode("utf-8", errors="replace")
        return pid, ptype, body

    def _read_exactly(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RconError("conexao fechada pelo servidor")
            buf += chunk
        return buf
