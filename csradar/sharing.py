"""Troca de dataset rotulado entre usuarios, sem expor ninguem.

Este modulo existe por causa de uma limitacao dura: uma pessoa sozinha demora
MUITO para juntar rotulo suficiente. Cheater e classe rara; para chegar aos 15
banidos que o treino exige, sao centenas de partidas e meses de espera pelos
bans. Duas ou tres pessoas trocando features chegam la muito antes.

O que sai daqui NAO identifica jogador:

- o SteamID vira um hash com sal escolhido por quem exporta. Sem o sal, nao da
  para voltar ao SteamID nem por forca bruta (o espaco de SteamID e pequeno o
  bastante para ser varrido - sem sal, hash nao protegeria nada).
- nome, mapa e nome de arquivo ficam de fora.
- sobram os valores dos sinais e o rotulo, que e o que serve para treinar.

O efeito colateral proposital: dois exports com sais diferentes nao se
cruzam. Se voce quer que o MESMO jogador seja reconhecido entre os seus
proprios exports, use o mesmo sal - e entenda que quem tiver o sal consegue
testar se um SteamID especifico esta no arquivo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from .ml import FEATURES

FORMAT_VERSION = 1


def new_salt() -> str:
    return secrets.token_hex(16)


def pseudonymize(steamid: int, salt: str) -> str:
    """Hash com sal, truncado. HMAC para o sal nao ser so um prefixo."""
    digest = hmac.new(salt.encode("utf-8"), str(int(steamid)).encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return digest[:20]


def export_dataset(store, path, salt: str = "", note: str = "") -> dict:
    """Grava um arquivo de troca com features + rotulo, sem identificacao."""
    salt = salt or new_salt()
    rows = store.labeled_dataset()
    if not rows:
        raise ValueError("banco vazio: nada para exportar")

    records = []
    for row in rows:
        signals = row.get("signals") or {}
        features = {}
        for name in FEATURES:
            entry = signals.get(name)
            if isinstance(entry, dict) and entry.get("value") is not None:
                features[name] = round(float(entry["value"]), 4)
        if not features:
            continue
        records.append({
            "p": pseudonymize(row["steamid"], salt),
            "f": features,
            "y": int(row.get("label", 0)),
            "s": round(float(row.get("score") or 0.0), 1),
        })

    payload = {
        "formato": FORMAT_VERSION,
        "gerado_em": int(time.time()),
        "nota": note,
        "features": list(FEATURES),
        "observacoes": len(records),
        "jogadores": len({r["p"] for r in records}),
        "positivos": len({r["p"] for r in records if r["y"]}),
        "registros": records,
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {
        "arquivo": str(out),
        "sal": salt,
        "observacoes": payload["observacoes"],
        "jogadores": payload["jogadores"],
        "positivos": payload["positivos"],
        "aviso": ("guarde o sal se quiser que exports futuros seus cruzem com "
                  "este; nao publique o sal junto do arquivo"),
    }


def read_dataset(path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "registros" not in raw:
        raise ValueError(f"{Path(path).name} nao e um dataset do csradar")
    if int(raw.get("formato", 0)) > FORMAT_VERSION:
        raise ValueError(
            f"o arquivo usa o formato {raw['formato']} e este programa entende "
            f"ate o {FORMAT_VERSION}; atualize o csradar")
    return raw


def merge_for_training(store, paths) -> list:
    """Junta o banco local com arquivos recebidos, em linhas de treino.

    Devolve o formato que `ml.train` consome, com identificadores que nao
    colidem entre fontes: cada arquivo ganha um prefixo proprio, porque dois
    exports com sais diferentes podem ter pseudonimos iguais por acaso.
    """
    from .ml import _value_of

    rows = []
    for entry in store.labeled_dataset():
        signals = entry.get("signals") or {}
        rows.append({
            "steamid": f"local:{entry['steamid']}",
            "name": "",
            "x": [_value_of(signals, f) for f in FEATURES],
            "y": int(entry.get("label", 0)),
        })

    for index, path in enumerate(paths):
        payload = read_dataset(path)
        tag = f"ext{index}"
        for record in payload.get("registros") or []:
            features = record.get("f") or {}
            rows.append({
                "steamid": f"{tag}:{record.get('p', '')}",
                "name": "",
                "x": [float(features.get(f, 0.0)) for f in FEATURES],
                "y": int(record.get("y", 0)),
            })
    return rows


def summary(paths) -> dict:
    """Resumo do que ha nos arquivos, antes de misturar com o seu banco."""
    total = positives = players = 0
    arquivos = []
    for path in paths:
        payload = read_dataset(path)
        total += int(payload.get("observacoes") or 0)
        positives += int(payload.get("positivos") or 0)
        players += int(payload.get("jogadores") or 0)
        arquivos.append({
            "arquivo": str(path),
            "observacoes": payload.get("observacoes"),
            "jogadores": payload.get("jogadores"),
            "positivos": payload.get("positivos"),
            "nota": payload.get("nota", ""),
        })
    return {"arquivos": arquivos, "observacoes": total,
            "jogadores": players, "positivos": positives}
