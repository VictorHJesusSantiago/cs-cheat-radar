"""Aprender os pesos em vez de chutar - fechando o ciclo do rotulo.

Os pesos em `ScoringConfig` sao palpite informado. O banco acumula, a cada
partida, as features de cada jogador; o `recheck` marca meses depois quem a
Valve baniu. Com isso da para ajustar uma regressao logistica e substituir o
palpite por numero medido.

Implementacao em Python puro de proposito: o projeto nao deve exigir scikit-
learn para uma coisa deste tamanho, e um modelo que cabe em uma tela e um
modelo que da para auditar.

Os cuidados que dominam este problema, e que estao codificados aqui:

- classe rara: banidos sao poucos por cento. Acuracia nao significa nada, e o
  treino usa peso de classe para o gradiente nao ignorar os positivos.
- rotulo ruidoso e ATRASADO: "nao banido" quer dizer "ainda nao", nao "limpo".
  Todo negativo e potencialmente um falso negativo.
- vazamento: um mesmo jogador aparece em varias partidas. A divisao treino/
  teste e POR JOGADOR, nunca por observacao, senao o modelo decora gente.
- amostra pequena: abaixo de um minimo, o treino recusa rodar em vez de
  devolver numeros bonitos e sem sentido.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

FEATURES = ("snap", "jitter", "reaction", "tracking", "prefire",
            "recoil", "movement", "context", "burst")

MIN_POSITIVES = 15
MIN_PLAYERS = 60


class NotEnoughData(RuntimeError):
    pass


@dataclass
class Model:
    weights: dict = field(default_factory=dict)
    bias: float = 0.0
    features: tuple = FEATURES
    trained_on: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)

    def score(self, signals: dict) -> float:
        """Probabilidade 0-100 de o jogador vir a ser banido."""
        z = self.bias
        for name in self.features:
            z += self.weights.get(name, 0.0) * _value_of(signals, name)
        return round(100.0 * _sigmoid(z), 1)

    def as_dict(self) -> dict:
        # sem arredondar: o arquivo e o artefato do treino, e um modelo que
        # muda de resposta ao ser salvo e recarregado nao serve de artefato.
        # O arredondamento e assunto da apresentacao, nao da persistencia.
        return {
            "weights": dict(self.weights),
            "bias": self.bias,
            "features": list(self.features),
            "trained_on": self.trained_on,
            "metrics": self.metrics,
        }

    def save(self, path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.as_dict(), indent=2, ensure_ascii=False),
                     encoding="utf-8")
        return p

    @classmethod
    def load(cls, path) -> "Model":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            weights=dict(raw.get("weights") or {}),
            bias=float(raw.get("bias", 0.0)),
            features=tuple(raw.get("features") or FEATURES),
            trained_on=raw.get("trained_on") or {},
            metrics=raw.get("metrics") or {},
        )

    def to_scoring_weights(self) -> dict:
        """Converte os coeficientes em pesos positivos normalizados.

        Serve para alimentar `ScoringConfig.weights` e manter o score 0-100
        legivel. Coeficiente negativo vira zero: um sinal que empurra para
        "limpo" nao deve subtrair de uma fila de suspeita, so nao contribuir.
        """
        positives = {k: max(0.0, v) for k, v in self.weights.items()}
        total = sum(positives.values())
        if total <= 0:
            return {}
        return {k: round(v / total, 4) for k, v in positives.items() if v > 0}


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def _value_of(signals: dict, name: str) -> float:
    entry = signals.get(name)
    if entry is None:
        return 0.0
    if isinstance(entry, dict):
        return float(entry.get("value", 0.0) or 0.0)
    return float(getattr(entry, "value", 0.0) or 0.0)


# ------------------------------------------------------------------- dados


def build_dataset(store) -> tuple:
    """(linhas, positivos) a partir do banco.

    Uma linha por OBSERVACAO (jogador numa partida), com o steamid junto para
    a divisao por jogador mais adiante.
    """
    rows = []
    for entry in store.labeled_dataset():
        signals = entry.get("signals") or {}
        rows.append({
            "steamid": entry["steamid"],
            "name": entry.get("name", ""),
            "x": [_value_of(signals, f) for f in FEATURES],
            "y": int(entry.get("label", 0)),
        })
    positives = len({r["steamid"] for r in rows if r["y"]})
    return rows, positives


def split_by_player(rows: list, test_fraction: float = 0.3, seed: int = 13) -> tuple:
    """Divide POR JOGADOR.

    Dividir por observacao vazaria: o mesmo jogador cairia nos dois lados e o
    modelo memorizaria pessoas em vez de aprender comportamento.
    """
    players = sorted({r["steamid"] for r in rows})
    rng = random.Random(seed)
    rng.shuffle(players)
    cut = max(1, int(len(players) * test_fraction))
    test_ids = set(players[:cut])
    train = [r for r in rows if r["steamid"] not in test_ids]
    test = [r for r in rows if r["steamid"] in test_ids]
    return train, test


def train(store, epochs: int = 400, learning_rate: float = 0.35,
          l2: float = 0.01, test_fraction: float = 0.3,
          seed: int = 13) -> Model:
    rows, positives = build_dataset(store)
    players = len({r["steamid"] for r in rows})

    if positives < MIN_POSITIVES or players < MIN_PLAYERS:
        raise NotEnoughData(
            f"dados insuficientes para treinar: {players} jogadores e "
            f"{positives} banidos conhecidos.\n"
            f"O minimo aqui e {MIN_PLAYERS} jogadores e {MIN_POSITIVES} "
            f"positivos - abaixo disso o modelo aprende ruido e o numero de "
            f"precisao que ele mostrar sera mentira.\n"
            f"Continue rodando `csradar analyze` e `csradar recheck`."
        )

    train_rows, test_rows = split_by_player(rows, test_fraction, seed)
    if not train_rows or not test_rows:
        raise NotEnoughData("divisao treino/teste ficou vazia")

    n_features = len(FEATURES)
    weights = [0.0] * n_features
    bias = 0.0

    # peso de classe: sem isto o gradiente simplesmente ignora os positivos
    n_pos = sum(r["y"] for r in train_rows) or 1
    n_neg = len(train_rows) - n_pos or 1
    w_pos = len(train_rows) / (2.0 * n_pos)
    w_neg = len(train_rows) / (2.0 * n_neg)

    for _ in range(epochs):
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for row in train_rows:
            z = bias + sum(weights[i] * row["x"][i] for i in range(n_features))
            pred = _sigmoid(z)
            weight = w_pos if row["y"] else w_neg
            err = (pred - row["y"]) * weight
            for i in range(n_features):
                grad_w[i] += err * row["x"][i]
            grad_b += err
        scale = learning_rate / len(train_rows)
        for i in range(n_features):
            weights[i] -= scale * (grad_w[i] + l2 * weights[i])
        bias -= scale * grad_b

    model = Model(
        weights={FEATURES[i]: weights[i] for i in range(n_features)},
        bias=bias,
        trained_on={
            "observacoes": len(rows),
            "jogadores": players,
            "banidos": positives,
            "treino": len(train_rows),
            "teste": len(test_rows),
        },
    )
    model.metrics = evaluate(model, test_rows)
    return model


def evaluate(model: Model, rows: list, threshold: float = 50.0) -> dict:
    """Precisao/revocacao no conjunto de teste, por JOGADOR."""
    by_player: dict = {}
    for row in rows:
        signals = {FEATURES[i]: {"value": row["x"][i]} for i in range(len(FEATURES))}
        score = model.score(signals)
        current = by_player.setdefault(row["steamid"], {"score": 0.0, "y": 0})
        current["score"] = max(current["score"], score)
        current["y"] = max(current["y"], row["y"])

    tp = fp = fn = tn = 0
    for entry in by_player.values():
        flagged = entry["score"] >= threshold
        if flagged and entry["y"]:
            tp += 1
        elif flagged:
            fp += 1
        elif entry["y"]:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {
        "limiar": threshold,
        "jogadores_teste": len(by_player),
        "positivos_teste": tp + fn,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precisao": round(precision, 3),
        "revocacao": round(recall, 3),
        "f1": round(f1, 3),
        "auc": round(_auc(by_player), 3),
        "aviso": ("negativo significa 'ainda nao banido', nao 'limpo'; "
                  "a precisao real e no minimo esta"),
    }


def _auc(by_player: dict) -> float:
    """AUC por contagem de pares concordantes - robusto a classe rara."""
    pos = [e["score"] for e in by_player.values() if e["y"]]
    neg = [e["score"] for e in by_player.values() if not e["y"]]
    if not pos or not neg:
        return 0.0
    wins = ties = 0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def default_model_path(home) -> Path:
    return Path(home) / "modelo.json"
