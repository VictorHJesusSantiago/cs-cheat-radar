"""Gerador de partidas sinteticas.

Serve para dois propositos concretos:

1. validar os detectores contra comportamento conhecido (o teste sabe quem
   estava cheatando, coisa que uma demo real nunca diz);
2. permitir rodar o programa inteiro de ponta a ponta sem ter uma demo em
   maos e sem depender do demoparser2.

Os perfis modelam o que o detector procura, entao um acerto aqui NAO prova
nada sobre demos reais - prova apenas que a matematica e o pipeline estao
corretos. A calibracao de verdade vem dos rotulos retroativos de ban.
"""

from __future__ import annotations

import math
import random

from ..features.geometry import desired_angles
from ..models import DemoData, Kill, PlayerInfo, PlayerTick, Round, Shot

TICKS_PER_ROUND = 1920  # 30 s a 64 tick
PROFILES = ("honest", "aimbot", "wallhack", "triggerbot", "silent")


class _Player:
    def __init__(self, steamid: int, name: str, team: int, profile: str):
        self.steamid = steamid
        self.name = name
        self.team = team
        self.profile = profile
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.alive = True

    def step(self, rng) -> None:
        """Passo de caminhada: sem movimento, seguir um alvo seria trivial."""
        if not self.alive:
            return
        self.vx = 0.92 * self.vx + rng.uniform(-0.9, 0.9)
        self.vy = 0.92 * self.vy + rng.uniform(-0.9, 0.9)
        self.x = max(-2000.0, min(2000.0, self.x + self.vx * 3.0))
        self.y = max(-2000.0, min(2000.0, self.y + self.vy * 3.0))

    def snapshot(self, tick: int, pitch: float, yaw: float, round_num: int) -> PlayerTick:
        return PlayerTick(
            tick=tick, steamid=self.steamid,
            x=self.x, y=self.y, z=self.z,
            pitch=pitch, yaw=yaw,
            is_alive=self.alive, team=self.team,
            health=100 if self.alive else 0, round_num=round_num,
        )


def make_synthetic_demo(
    rounds: int = 16,
    seed: int = 7,
    profiles: dict | None = None,
    tickrate: float = 64.0,
) -> DemoData:
    """Cria uma DemoData completa.

    profiles: {slot 0..9 -> perfil}. Slots 0-4 sao time 2 (T), 5-9 time 3 (CT).
    Sem argumento, o slot 3 usa aimbot e o slot 7 usa wallhack. Passe {} para
    uma partida inteiramente limpa (util para medir falso positivo).
    """
    rng = random.Random(seed)
    # {} significa "partida limpa"; so None cai no padrao
    if profiles is None:
        profiles = {3: "aimbot", 7: "wallhack"}

    players = []
    for slot in range(10):
        team = 2 if slot < 5 else 3
        profile = profiles.get(slot, "honest")
        players.append(
            _Player(
                steamid=76561197960265728 + 1000 + slot,
                name=f"{profile}_{slot}",
                team=team,
                profile=profile,
            )
        )

    demo = DemoData(source=f"sintetica_seed{seed}.dem", map_name="de_sintetico",
                    tickrate=tickrate)
    demo.capabilities = {"angles", "positions", "shots", "kills", "rounds",
                         "teams", "kill_flags"}
    for p in players:
        demo.players[p.steamid] = PlayerInfo(p.steamid, p.name, p.team)
        demo.ticks_by_player[p.steamid] = []

    tick = 1000
    for rnd in range(1, rounds + 1):
        start = tick
        _simulate_round(demo, players, rng, rnd, start, tickrate)
        tick = start + TICKS_PER_ROUND + 64
        demo.rounds.append(Round(number=rnd, start_tick=start, end_tick=tick - 1))

    demo.kills.sort(key=lambda k: k.tick)
    demo.shots.sort(key=lambda s: s.tick)
    return demo


def _simulate_round(demo, players, rng, rnd: int, start: int, tickrate: float) -> None:
    for p in players:
        p.alive = True
        p.x = rng.uniform(-1600, 1600)
        p.y = rng.uniform(-1600, 1600)
        p.z = rng.choice((0.0, 0.0, 64.0))
        p.vx = rng.uniform(-2.0, 2.0)
        p.vy = rng.uniform(-2.0, 2.0)

    plan = _plan_engagements(players, rng, start)
    for eng in plan:
        eng["fire_set"] = set(eng["fire_ticks"])
    tracks = _plan_wall_tracks(players, rng, start, plan)

    # yaw base de cada jogador (para onde ele olha quando nao esta em duelo)
    idle_base = {p.steamid: rng.uniform(0, 360) for p in players}
    idle_rate = {p.steamid: rng.uniform(0.25, 0.9) * rng.choice((-1, 1)) for p in players}

    for offset in range(TICKS_PER_ROUND):
        tick = start + offset
        for p in players:
            p.step(rng)
        for eng in plan:
            _script_approach(eng, tick)
        for p in players:
            pitch, yaw = _aim_for(p, tick, plan, tracks, idle_base, idle_rate, players, rng)
            demo.ticks_by_player[p.steamid].append(p.snapshot(tick, pitch, yaw, rnd))
        for eng in plan:
            if eng["kill_tick"] == tick:
                eng["victim"].alive = False
                demo.kills.append(
                    Kill(tick=tick, attacker=eng["shooter"].steamid,
                         victim=eng["victim"].steamid, weapon="ak47",
                         headshot=eng["headshot"], round_num=rnd,
                         through_smoke=eng["smoke"],
                         penetrated=eng["penetrated"],
                         noscope=eng["noscope"])
                )
            if tick in eng["fire_set"]:
                demo.shots.append(
                    Shot(tick=tick, steamid=eng["shooter"].steamid,
                         weapon="ak47", round_num=rnd)
                )


def _plan_engagements(players, rng, start: int) -> list:
    """Duelos espacados no round, um por janela, entre times opostos."""
    plan = []
    windows = [250, 600, 950, 1300, 1650]
    rng.shuffle(windows)
    for w in windows[: rng.randint(3, 5)]:
        shooter = rng.choice(players)
        enemies = [p for p in players if p.team != shooter.team]
        victim = rng.choice(enemies)
        entry = start + w + rng.randint(-40, 40)

        if shooter.profile in ("aimbot", "triggerbot"):
            reaction = rng.randint(4, 7)          # 60-110 ms
            settle = rng.randint(1, 2)
        else:
            reaction = rng.randint(11, 22)        # 170-340 ms
            settle = rng.randint(6, 12)

        fire = entry + reaction + settle
        n_shots = rng.randint(5, 9)
        fire_ticks = [fire + i * rng.randint(6, 9) for i in range(n_shots)]
        plan.append({
            "fire_ticks": fire_ticks,
            "n_shots": n_shots,
            "hold": rng.random() < 0.8,
            "held_yaw": None,
            "radius": rng.uniform(700.0, 1200.0),
            "shooter": shooter,
            "victim": victim,
            "entry_tick": entry,
            "reaction": reaction,
            "settle": settle,
            "fire_tick": fire,
            "kill_tick": fire_ticks[-1] + rng.randint(1, 3),
            "headshot": rng.random() < (0.85 if shooter.profile == "aimbot" else 0.45),
            # quem enxerga atraves das coisas acumula kills em contexto ruim
            "smoke": rng.random() < (0.30 if shooter.profile == "wallhack" else 0.03),
            "penetrated": (rng.randint(1, 2)
                           if rng.random() < (0.35 if shooter.profile == "wallhack"
                                              else 0.05) else 0),
            "noscope": rng.random() < 0.04,
        })
    plan.sort(key=lambda e: e["entry_tick"])
    return plan


APPROACH_TICKS = 96
FOV_EDGE = 55.0


def _script_approach(eng: dict, tick: int) -> None:
    """Nos duelos "hold", o alvo caminha em arco ate cruzar a borda do FOV.

    Sem isso o alvo so entraria no campo de visao porque o atirador girou a
    mira - e ai o tempo de reacao medido nao mede reacao nenhuma.
    """
    if not eng["hold"]:
        return
    entry = eng["entry_tick"]
    begin = entry - APPROACH_TICKS
    if not (begin <= tick <= eng["kill_tick"]):
        return

    shooter, victim = eng["shooter"], eng["victim"]
    if eng["held_yaw"] is None:
        eng["held_yaw"] = desired_angles(shooter, victim)[1] + FOV_EDGE + 20.0

    # peek de quina: o alvo fica fora do FOV e aparece de uma vez em `entry`.
    # Uma entrada gradual atrasaria a deteccao de exposicao e inflaria o tempo
    # de reacao medido de todo mundo por igual.
    if tick < entry:
        off = FOV_EDGE + 22.0
    elif tick == entry:
        off = FOV_EDGE - 1.0
    else:
        frac = min(1.0, (tick - entry) / 48.0)
        off = FOV_EDGE - 35.0 * frac               # 55 -> 20 graus

    ang = math.radians(eng["held_yaw"] - off)
    r = eng["radius"]
    victim.x = shooter.x + r * math.cos(ang)
    victim.y = shooter.y + r * math.sin(ang)
    victim.vx = victim.vy = 0.0


def _plan_wall_tracks(players, rng, start: int, plan: list) -> dict:
    """Janelas em que o wallhacker acompanha um inimigo distante sem atirar."""
    busy = [e["entry_tick"] for e in plan] + [e["kill_tick"] for e in plan]
    tracks = {}
    for p in players:
        if p.profile != "wallhack":
            continue
        windows = []
        for base in (120, 480, 820, 1160, 1500, 1800):
            begin = start + base + rng.randint(-30, 30)
            length = rng.randint(28, 60)
            if any(abs(begin - b) <= 110 for b in busy):
                continue
            enemies = [q for q in players if q.team != p.team]
            windows.append((begin, begin + length, rng.choice(enemies)))
        tracks[p.steamid] = windows
    return tracks


def _aim_for(p, tick, plan, tracks, idle_base, idle_rate, players, rng) -> tuple:
    if not p.alive:
        return 0.0, idle_base[p.steamid]

    for eng in plan:
        if eng["shooter"] is not p:
            continue
        if not (eng["entry_tick"] - 96 <= tick <= eng["kill_tick"] + 16):
            continue
        return _engagement_aim(p, eng, tick, idle_base)

    for begin, end, target in tracks.get(p.steamid, ()):
        if begin <= tick <= end and target.alive:
            pitch, yaw = desired_angles(p, target)
            return pitch + _n(rng, 0.15), yaw + _n(rng, 0.15)

    # ocioso: varredura suave
    yaw = idle_base[p.steamid] + idle_rate[p.steamid] * (tick % 512)
    pitch = 2.0 * math.sin(tick / 90.0)
    return pitch, yaw % 360.0


def _engagement_aim(p, eng, tick, idle_base) -> tuple:
    """Curva de mira do duelo, diferente para humano e para software."""
    victim = eng["victim"]
    want_pitch, want_yaw = desired_angles(p, victim)
    entry = eng["entry_tick"]
    rng = random.Random((p.steamid * 31 + tick) & 0xFFFFFFFF)

    if eng["hold"] and eng["held_yaw"] is not None:
        away_yaw = eng["held_yaw"]
        away_pitch = 0.0
    else:
        # sem hold: estava olhando ~100 graus para o lado
        away_yaw = want_yaw + 100.0
        away_pitch = want_pitch + 6.0
    if tick < entry:
        if eng["hold"] and p.profile != "wallhack":
            return away_pitch + _n(rng, 0.10), away_yaw + _n(rng, 0.12)
        if p.profile == "wallhack":
            # ja sabe onde o inimigo esta: pre-aim atraves da parede
            return want_pitch + _n(rng, 0.6), want_yaw + _n(rng, 0.8)
        return away_pitch, away_yaw

    t = tick - entry
    react = eng["reaction"]
    settle = eng["settle"]

    if t < react:                        # ainda nao reagiu
        if eng["hold"] and p.profile != "wallhack":
            return away_pitch + _n(rng, 0.10), away_yaw + _n(rng, 0.12)
        return away_pitch, away_yaw

    if p.profile == "silent":
        # aponta apenas no tick do disparo e volta ao angulo anterior
        if tick in eng["fire_set"]:
            return want_pitch + _n(rng, 0.05), want_yaw + _n(rng, 0.05)
        return away_pitch + _n(rng, 0.10), away_yaw + _n(rng, 0.12)

    if p.profile in ("aimbot", "triggerbot"):
        # transicao em 1-2 ticks, sem overshoot, e depois erro quase constante
        if t < react + settle:
            frac = (t - react + 1) / (settle + 1)
            return (_lerp(away_pitch, want_pitch, frac),
                    _lerp(away_yaw, want_yaw, frac))
        # durante a rajada a compensacao de recuo e exata: erro plano
        return want_pitch + _n(rng, 0.04), want_yaw + _n(rng, 0.04)

    # humano: curva suave, passa do alvo e corrige, com tremor residual
    if t < react + settle:
        frac = _ease((t - react + 1) / (settle + 1))
        over = 1.0 + 0.35 * math.sin(math.pi * frac)   # overshoot no meio
        return (_lerp(away_pitch, want_pitch, frac * over) + _n(rng, 0.8),
                _lerp(away_yaw, want_yaw, frac * over) + _n(rng, 1.2))
    if t < react + settle + 4:
        return want_pitch + _n(rng, 2.2) + 3.0, want_yaw + _n(rng, 3.0) + 4.0

    # rajada: o recuo sobe a mira e o jogador puxa de volta, em ciclo. E esse
    # vaivem que distingue compensacao humana de subtracao de vetor.
    phase = t - (react + settle)
    recoil = -2.6 * math.sin(phase / 3.2) - 1.2 * math.sin(phase / 7.0)
    return (want_pitch + recoil + _n(rng, 0.9),
            want_yaw + 0.8 * math.cos(phase / 4.1) + _n(rng, 1.1))


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, f))


def _ease(f: float) -> float:
    f = max(0.0, min(1.0, f))
    return f * f * (3 - 2 * f)


def _n(rng, scale: float) -> float:
    return rng.gauss(0.0, scale)


# ---------------------------------------------------------------------------
# Entrada 2D (osu!): quadros de cursor e teclas
# ---------------------------------------------------------------------------

OSU_PROFILES = ("human", "relax", "aim_assist", "replay_bot")


def make_osu_frames(profile: str = "human", seed: int = 1,
                    frames: int = 1800, interval_ms: int = 16) -> list:
    """Gera [(ms, x, y, keys)] com comportamento conhecido.

    Mesma logica dos perfis 3D: o simulador sabe quem estava trapaceando, o
    replay real nunca sabe. Serve para validar os detectores 2D, nao para
    calibrar contra jogadores de verdade.
    """
    rng = random.Random(seed)
    out = []
    clock = 0
    x, y = 256.0, 192.0
    target_x, target_y = x, y
    hold = 0
    key = 0
    next_press = rng.randint(8, 20)

    for i in range(frames):
        clock += interval_ms

        # a cada tanto, um novo alvo aparece em algum lugar da tela
        if i % 24 == 0:
            target_x = rng.uniform(60, 452)
            target_y = rng.uniform(50, 334)

        if profile == "replay_bot":
            # teleporta para o alvo e fica parado ate o proximo
            x, y = target_x, target_y
        elif profile in ("aim_assist", "relax"):
            # interpolacao limpa: sem tremor, sem correcao
            x += (target_x - x) * 0.28
            y += (target_y - y) * 0.28
            if profile == "aim_assist":
                x += rng.gauss(0, 0.05)
                y += rng.gauss(0, 0.05)
        else:
            # mao humana: aproxima com overshoot e corrige o tempo todo
            x += (target_x - x) * 0.22 + rng.gauss(0, 2.6)
            y += (target_y - y) * 0.22 + rng.gauss(0, 2.6)

        # teclas
        if hold > 0:
            hold -= 1
            if hold == 0:
                key = 0
        elif i >= next_press:
            key = 4 if rng.random() < 0.5 else 8
            if profile in ("relax", "replay_bot"):
                hold = 3                      # sempre a mesma duracao
                next_press = i + rng.randint(10, 14)
            else:
                hold = rng.randint(2, 7)      # mao varia bastante
                next_press = i + rng.randint(8, 22)

        out.append((clock, round(x, 2), round(y, 2), key))
    return out
