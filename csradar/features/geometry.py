"""Matematica de angulos no espaco do CS2.

Convencoes da Source: yaw 0 aponta para +X e cresce no sentido anti-horario;
pitch positivo significa olhar para BAIXO. Alturas de olho aproximadas:
64 unidades em pe, 46 agachado.
"""

from __future__ import annotations

import math

EYE_HEIGHT = 64.0
HEAD_OFFSET = 64.0


def distance(a, b) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def angle_diff(a: float, b: float) -> float:
    """Diferenca sinalizada entre dois angulos, normalizada em (-180, 180]."""
    d = (a - b + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def desired_angles(shooter, target, eye_height: float = EYE_HEIGHT,
                   head_offset: float = HEAD_OFFSET) -> tuple:
    """(pitch, yaw) que apontariam da cabeca do atirador para a cabeca do alvo."""
    dx = target.x - shooter.x
    dy = target.y - shooter.y
    dz = (target.z + head_offset) - (shooter.z + eye_height)
    yaw = math.degrees(math.atan2(dy, dx))
    horiz = math.hypot(dx, dy)
    pitch = -math.degrees(math.atan2(dz, horiz)) if (horiz or dz) else 0.0
    return pitch, yaw


def angular_error(shooter, target, **kw) -> float:
    """Erro angular total (graus) entre a mira atual e a cabeca do alvo.

    Usa a distancia angular esferica real, nao a soma dos eixos, para nao
    superestimar o erro quando o alvo esta muito acima ou abaixo.
    """
    want_pitch, want_yaw = desired_angles(shooter, target, **kw)
    return _spherical_delta(shooter.pitch, shooter.yaw, want_pitch, want_yaw)


def _spherical_delta(p1: float, y1: float, p2: float, y2: float) -> float:
    a = _unit(p1, y1)
    b = _unit(p2, y2)
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def _unit(pitch: float, yaw: float) -> tuple:
    p = math.radians(-pitch)  # pitch positivo = para baixo
    y = math.radians(yaw)
    cp = math.cos(p)
    return (cp * math.cos(y), cp * math.sin(y), math.sin(p))


def aim_vector(tick) -> tuple:
    return _unit(tick.pitch, tick.yaw)


def angular_speed(prev, cur) -> float:
    """Deslocamento angular (graus) entre dois ticks consecutivos."""
    return _spherical_delta(prev.pitch, prev.yaw, cur.pitch, cur.yaw)


def in_fov(shooter, target, fov_deg: float) -> bool:
    return angular_error(shooter, target) <= fov_deg


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def stdev(values) -> float:
    values = list(values)
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def median(values) -> float:
    values = sorted(values)
    n = len(values)
    if not n:
        return 0.0
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def ramp(value: float, low: float, high: float) -> float:
    """Normaliza value para 0..1 entre low e high (aceita low > high = invertido)."""
    if low == high:
        return 0.0
    return clamp01((value - low) / (high - low))
