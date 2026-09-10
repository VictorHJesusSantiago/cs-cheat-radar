"""Detectores escritos por voce, carregados de uma pasta.

Justificativa dentro da restricao do projeto: o programa e mantido por uma
pessoa. Cada detector novo no nucleo e codigo para sempre - precisa de teste,
de calibracao e de manutencao quando a engine do jogo muda. Um detector que
serve para o SEU caso (uma comunidade, um jogo, um formato de log) nao deveria
exigir mexer no nucleo nem virar responsabilidade de manutencao de ninguem.

Um plugin e um arquivo .py em ~/.csradar/plugins/ com duas coisas:

    REQUIRES = {"angles", "positions", "kills"}   # capacidades necessarias
    PRODUCES = ("meu_sinal",)                     # nomes dos sinais

    def analyze(demo, steamid, cfg, index=None):
        ...
        return {"meu_sinal": SignalResult("meu_sinal", 0.0)}

O contrato e o mesmo dos detectores internos, de proposito: um plugin que
amadurece pode ser movido para dentro sem reescrita.

Regras de seguranca praticas, nao teatrais: carregar um plugin EXECUTA aquele
arquivo. Isso e inevitavel em qualquer sistema de plugin em Python. Por isso o
carregamento e explicito (`--plugins`, ou `plugins.enabled` na configuracao),
nunca automatico, e a lista carregada aparece no relatorio.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

from .config import default_home

TEMPLATE = '''"""Detector de exemplo do csradar.

Copie, renomeie e edite. Rode com:
    csradar analyze demo.dem --plugins
"""

from csradar.models import Evidence, SignalResult

# Capacidades que este detector exige. Sem elas ele nao roda, e o relatorio
# diz que ficou de fora - o mesmo tratamento dos detectores internos.
REQUIRES = {"angles", "positions", "kills"}
PRODUCES = ("exemplo",)


def analyze(demo, steamid, cfg, index=None):
    kills = [k for k in demo.kills if k.attacker == steamid]
    # troque isto pela sua ideia
    valor = min(1.0, len(kills) / 40.0)
    return {
        "exemplo": SignalResult(
            name="exemplo",
            value=valor,
            samples=len(kills),
            confident=len(kills) >= 10,
            raw={"kills": len(kills)},
            evidence=[
                Evidence(kind="exemplo", round_num=k.round_num, tick=k.tick,
                         target=demo.name_of(k.victim), detail="kill contada")
                for k in kills[:3]
            ],
        )
    }
'''


class PluginError(RuntimeError):
    pass


def plugins_dir(home=None) -> Path:
    return Path(home or default_home()) / "plugins"


def scaffold(home=None) -> Path:
    """Cria a pasta e um exemplo funcional."""
    folder = plugins_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    example = folder / "exemplo.py"
    if not example.exists():
        example.write_text(TEMPLATE, encoding="utf-8")
    return example


def discover(home=None) -> list:
    folder = plugins_dir(home)
    if not folder.exists():
        return []
    return sorted(p for p in folder.glob("*.py")
                  if not p.name.startswith("_"))


def load(path) -> tuple:
    """Carrega um plugin e devolve (nome, requires, produces, analyze)."""
    p = Path(path)
    spec = importlib.util.spec_from_file_location(f"csradar_plugin_{p.stem}", p)
    if spec is None or spec.loader is None:
        raise PluginError(f"nao consegui carregar {p.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:      # noqa: BLE001 - erro do usuario, nao nosso
        raise PluginError(
            f"{p.name} falhou ao carregar: {exc}\n"
            + "".join(traceback.format_exception_only(type(exc), exc))
        ) from exc

    analyze = getattr(module, "analyze", None)
    if not callable(analyze):
        raise PluginError(f"{p.name} nao define uma funcao analyze(...)")
    requires = set(getattr(module, "REQUIRES", set()) or set())
    produces = tuple(getattr(module, "PRODUCES", ()) or ())
    if not produces:
        raise PluginError(
            f"{p.name} nao declara PRODUCES; sem isso nao da para saber quais "
            f"sinais ele gera nem avisar quando ficam de fora")
    return p.stem, requires, produces, analyze


def load_all(home=None, names=None) -> tuple:
    """(detectores, erros) - detectores no formato de scoring.DETECTORS."""
    detectors = []
    errors = {}
    for path in discover(home):
        if names and path.stem not in names:
            continue
        try:
            name, requires, produces, analyze = load(path)
        except PluginError as exc:
            errors[path.stem] = str(exc)
            continue
        detectors.append((f"plugin:{name}", _guard(analyze, path.stem),
                          requires, produces))
    return detectors, errors


def _guard(analyze, name: str):
    """Um plugin quebrado nao pode derrubar a analise inteira."""

    def wrapped(demo, steamid, cfg, index=None):
        try:
            result = analyze(demo, steamid, cfg, index)
        except Exception as exc:      # noqa: BLE001
            return {f"plugin_{name}_erro": _error_signal(name, exc)}
        if not isinstance(result, dict):
            return {f"plugin_{name}_erro": _error_signal(
                name, TypeError("analyze deve devolver um dict"))}
        return result

    return wrapped


def _error_signal(name: str, exc: Exception):
    from .models import SignalResult

    return SignalResult(
        name=f"plugin_{name}_erro", value=0.0, samples=0, confident=False,
        raw={"erro": f"{type(exc).__name__}: {exc}"},
    )
