"""Comandos: calibrate, history, explain, rings, recurring, share, plugins,
selftest, prune."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import analytics, plugins as plugins_mod, sharing
from .config import Config, default_home
from .storage import Store
from .steam import to_steam64


def _sid(raw):
    sid = to_steam64(raw)
    if not sid:
        print(f"steamid invalido: {raw}", file=sys.stderr)
    return sid


# --------------------------------------------------------------- calibrate


def cmd_calibrate(args) -> int:
    cfg = Config.load()
    with Store(args.db) as st:
        dist = analytics.signal_distribution(st)
        suggestion = analytics.suggest_threshold(st, percentile=args.percentile)

    if not dist["observacoes"]:
        print("banco vazio. Rode `csradar analyze` em algumas partidas antes.")
        return 1

    print(f"observacoes: {dist['observacoes']}")
    if "score" in dist:
        s = dist["score"]
        print(f"score: mediana {s['mediana']}, p90 {s['p90']}, "
              f"p99 {s['p99']}, maximo {s['maximo']}")

    print(f"\n{'sinal':<14}{'amostras':>9}{'!=0':>7}{'mediana':>9}"
          f"{'p90':>8}{'p99':>8}{'max':>8}")
    print("-" * 63)
    for name, stats in dist["sinais"].items():
        if "aviso" in stats:
            print(f"{name:<14}{stats['amostras']:>9}   {stats['aviso']}")
            continue
        print(f"{name:<14}{stats['amostras']:>9}"
              f"{stats['fracao_nao_zero']:>7.2f}{stats['mediana']:>9.3f}"
              f"{stats['p90']:>8.3f}{stats['p99']:>8.3f}{stats['maximo']:>8.3f}")

    alto = [n for n, s in dist["sinais"].items()
            if "mediana" in s and s["mediana"] > 0.25]
    if alto:
        print(f"\nATENCAO: {', '.join(alto)} tem mediana alta na sua base.")
        print("Um sinal que dispara para a maioria nao esta detectando trapaca;")
        print("esta detectando o seu jogo, o seu tickrate ou um vies do detector.")

    print("\nlimiar de revisao:")
    if "erro" in suggestion:
        print(f"  {suggestion['erro']}")
    else:
        print(f"  atual:     {cfg.scoring.review_threshold:g}")
        print(f"  sugerido:  {suggestion['limiar_sugerido']:g} "
              f"(percentil {suggestion['percentil']:.0%}, "
              f"{suggestion['acima_do_limiar']} de {suggestion['observacoes']} "
              f"observacoes acima)")
        print(f"  {suggestion['aviso']}")
        if args.apply:
            cfg.scoring.review_threshold = suggestion["limiar_sugerido"]
            cfg.save()
            print(f"\naplicado: limiar agora e "
                  f"{suggestion['limiar_sugerido']:g}")
        else:
            print("\n  use --apply para adotar o sugerido.")
    return 0


# ----------------------------------------------------------------- history


def cmd_history(args) -> int:
    sid = _sid(args.steamid)
    if not sid:
        return 1
    with Store(args.db) as st:
        data = analytics.player_history(st, sid)

    if not data.get("partidas"):
        print(f"nenhuma observacao para {sid}")
        return 1

    print(f"{data['nome'] or sid}  ({sid})")
    print(f"https://steamcommunity.com/profiles/{sid}")
    print(f"\npartidas: {data['partidas']}   media: {data['score_medio']}   "
          f"mediana: {data['score_mediano']}   maximo: {data['score_maximo']}")
    if data["banido"]:
        print(f"*** BANIDO (ha {data['dias_desde_ban']} dias) - este e um "
              f"rotulo positivo confirmado")

    print(f"\n{'fonte':<32}{'K/D':>8}{'score':>8}  sinais fortes")
    print("-" * 74)
    for entry in data["entradas"]:
        fortes = ", ".join(f"{k} {v:.2f}" for k, v in
                           sorted(entry["sinais"].items(),
                                  key=lambda kv: -(kv[1] or 0))[:3]
                           if (v or 0) >= 0.3)
        fonte = (entry["fonte"] or "")[:31]
        print(f"{fonte:<32}{entry['kills']}/{entry['deaths']:<4}"
              f"{entry['score'] or 0:>8.1f}  {fortes or '-'}")
    return 0


def cmd_explain(args) -> int:
    sid = _sid(args.steamid)
    if not sid:
        return 1
    with Store(args.db) as st:
        data = analytics.explain(st, sid, args.source or "")

    if "erro" in data:
        print(data["erro"])
        return 1
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    print(f"{data['nome']} ({data['steamid']}) em {data['fonte']} "
          f"[{data['mapa']}]")
    print(f"score {data['score']}  K/D {data['kills']}/{data['deaths']}  "
          f"HS {data['headshots']}")
    print(f"\n{'sinal':<14}{'valor':>7}{'amostras':>10}  detalhe")
    print("-" * 74)
    for name, sig in sorted(data["sinais"].items(),
                            key=lambda kv: -(kv[1].get("value") or 0)):
        conf = "" if sig.get("confident", True) else "  (amostra pequena)"
        raw = ", ".join(f"{k}={v}" for k, v in (sig.get("raw") or {}).items())
        print(f"{name:<14}{sig.get('value', 0):>7.2f}"
              f"{sig.get('samples', 0):>10}  {raw[:38]}{conf}")

    if data["evidencias"]:
        print("\nmomentos para conferir na demo:")
        for ev in data["evidencias"]:
            alvo = f" -> {ev['target']}" if ev.get("target") else ""
            print(f"  round {ev['round']:>2}  tick {ev['tick']:<9} "
                  f"{ev['kind']}{alvo}")
            print(f"      {ev['detail']}")
    if data["outras_partidas"]:
        print(f"\n(este jogador aparece em mais {data['outras_partidas']} "
              f"partida(s); veja `csradar history`)")
    return 0


# ------------------------------------------------------------------- rings


def cmd_rings(args) -> int:
    with Store(args.db) as st:
        pairs = analytics.co_occurrence(st, min_together=args.min_together,
                                        min_score=args.min_score)
    if not pairs:
        print("nenhum par recorrente com os filtros atuais.")
        print("Amigos jogam juntos: comece com --min-score alto para so ver")
        print("pares em que os DOIS pontuam.")
        return 0

    print(f"{'jogador A':<22}{'jogador B':<22}{'juntos':>7}{'time':>6}"
          f"{'media':>8}")
    print("-" * 66)
    for pair in pairs[:args.limit]:
        print(f"{(pair['nome_a'] or pair['a'])[:21]:<22}"
              f"{(pair['nome_b'] or pair['b'])[:21]:<22}"
              f"{pair['juntos']:>7}{pair['mesmo_time']:>6}"
              f"{pair['media_do_par']:>8.1f}")
    print("\nAparecer junto nao prova nada - amigos jogam juntos. O que")
    print("interessa e um grupo fixo em que varios pontuam alto.")
    return 0


def cmd_recurring(args) -> int:
    with Store(args.db) as st:
        rows = analytics.recurring_suspects(st, min_matches=args.min_matches,
                                            min_mean=args.min_mean)
    if not rows:
        print("ninguem com pontuacao alta de forma consistente.")
        return 0
    print(f"{'jogador':<24}{'partidas':>9}{'media':>8}{'min':>7}{'max':>7}"
          f"{'consist.':>9}")
    print("-" * 66)
    for row in rows[:args.limit]:
        print(f"{(row['nome'] or row['steamid'])[:23]:<24}"
              f"{row['partidas']:>9}{row['media']:>8.1f}{row['minimo']:>7.1f}"
              f"{row['maximo']:>7.1f}{row['consistencia']:>9.2f}")
        print(f"{'':<24}https://steamcommunity.com/profiles/{row['steamid']}")
    print("\nConsistencia perto de 1 = pontua alto sempre. Perto de 0 = um")
    print("pico isolado puxou a media.")
    return 0


# ------------------------------------------------------------------- share


def cmd_share(args) -> int:
    if args.export:
        with Store(args.db) as st:
            try:
                info = sharing.export_dataset(st, args.export, salt=args.salt or "",
                                              note=args.note or "")
            except ValueError as exc:
                print(f"erro: {exc}")
                return 1
        print(f"arquivo:      {info['arquivo']}")
        print(f"observacoes:  {info['observacoes']}")
        print(f"jogadores:    {info['jogadores']}")
        print(f"positivos:    {info['positivos']}")
        print(f"\nsal usado:    {info['sal']}")
        print(f"  {info['aviso']}")
        print("\nO arquivo NAO contem SteamID, nome, mapa nem nome de demo.")
        return 0

    if args.inspect:
        try:
            info = sharing.summary(args.inspect)
        except ValueError as exc:
            print(f"erro: {exc}")
            return 1
        for entry in info["arquivos"]:
            print(f"{entry['arquivo']}")
            print(f"  {entry['observacoes']} observacoes, "
                  f"{entry['jogadores']} jogadores, "
                  f"{entry['positivos']} positivos"
                  + (f" - {entry['nota']}" if entry['nota'] else ""))
        print(f"\ntotal: {info['observacoes']} observacoes, "
              f"{info['positivos']} positivos")
        return 0

    if args.train_with:
        from .ml import MIN_PLAYERS, MIN_POSITIVES, Model, evaluate, split_by_player
        from . import ml

        with Store(args.db) as st:
            try:
                rows = sharing.merge_for_training(st, args.train_with)
            except ValueError as exc:
                print(f"erro: {exc}")
                return 1

        players = len({r["steamid"] for r in rows})
        positives = len({r["steamid"] for r in rows if r["y"]})
        print(f"conjunto combinado: {len(rows)} observacoes, {players} "
              f"jogadores, {positives} positivos")
        if positives < MIN_POSITIVES or players < MIN_PLAYERS:
            print(f"ainda insuficiente (minimo {MIN_PLAYERS} jogadores e "
                  f"{MIN_POSITIVES} positivos).")
            return 1

        model = _train_rows(ml, rows, args)
        print("\nmetricas (divisao por jogador):")
        for key, value in model.metrics.items():
            if key != "aviso":
                print(f"  {key:>16}: {value}")
        path = Path(args.out) if args.out else default_home() / "modelo.json"
        model.save(path)
        print(f"\nmodelo salvo em {path}")
        return 0

    print("use --export ARQ, --inspect ARQ..., ou --train-with ARQ...")
    return 1


def _train_rows(ml, rows, args):
    """Treina sobre linhas ja montadas (locais + recebidas)."""
    train_rows, test_rows = ml.split_by_player(rows, 0.3, 13)
    n_features = len(ml.FEATURES)
    weights = [0.0] * n_features
    bias = 0.0
    n_pos = sum(r["y"] for r in train_rows) or 1
    n_neg = len(train_rows) - n_pos or 1
    w_pos = len(train_rows) / (2.0 * n_pos)
    w_neg = len(train_rows) / (2.0 * n_neg)

    for _ in range(args.epochs):
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for row in train_rows:
            z = bias + sum(weights[i] * row["x"][i] for i in range(n_features))
            pred = ml._sigmoid(z)
            err = (pred - row["y"]) * (w_pos if row["y"] else w_neg)
            for i in range(n_features):
                grad_w[i] += err * row["x"][i]
            grad_b += err
        scale = args.learning_rate / len(train_rows)
        for i in range(n_features):
            weights[i] -= scale * (grad_w[i] + 0.01 * weights[i])
        bias -= scale * grad_b

    model = ml.Model(
        weights={ml.FEATURES[i]: weights[i] for i in range(n_features)},
        bias=bias,
        trained_on={"observacoes": len(rows),
                    "jogadores": len({r["steamid"] for r in rows}),
                    "banidos": len({r["steamid"] for r in rows if r["y"]}),
                    "treino": len(train_rows), "teste": len(test_rows),
                    "fonte": "banco local + arquivos compartilhados"},
    )
    model.metrics = ml.evaluate(model, test_rows)
    return model


# ----------------------------------------------------------------- plugins


def cmd_plugins(args) -> int:
    if args.init:
        path = plugins_mod.scaffold()
        print(f"pasta de plugins: {path.parent}")
        print(f"exemplo criado:   {path}")
        print("\nedite o exemplo e rode: csradar analyze demo.dem --plugins")
        return 0

    found = plugins_mod.discover()
    if not found:
        print(f"nenhum plugin em {plugins_mod.plugins_dir()}")
        print("use `csradar plugins --init` para criar a pasta e um exemplo.")
        return 0

    detectors, errors = plugins_mod.load_all()
    print(f"pasta: {plugins_mod.plugins_dir()}\n")
    for name, _fn, requires, produces in detectors:
        print(f"  {name}")
        print(f"      exige:   {', '.join(sorted(requires)) or 'nada'}")
        print(f"      produz:  {', '.join(produces)}")
    for name, error in errors.items():
        print(f"  {name}: NAO CARREGOU")
        for line in str(error).splitlines():
            print(f"      {line}")
    if detectors:
        print("\nCarregar um plugin executa o arquivo. Por isso e explicito:")
        print("passe --plugins em `analyze` para ativa-los.")
    return 0


# ---------------------------------------------------------------- selftest


def cmd_selftest(args) -> int:
    """Roda o simulador e mede a separacao entre honestos e cheaters.

    Existe para o mantenedor: depois de mexer num detector, isto responde em
    trinta segundos se a mudanca melhorou ou estragou, sem precisar ler saida
    de teste unitario.
    """
    from .demo.synthetic import make_osu_frames, make_synthetic_demo
    from .games.osu_replay import read_osr, write_osr
    from .scoring import analyze_demo
    import tempfile

    cfg = Config.load()
    perfis = {3: "aimbot", 7: "wallhack", 5: "silent"}
    falhas = []

    print("=== 3D (CS2 sintetico) ===")
    print(f"{'seed':<6}{'pior cheater':>14}{'melhor honesto':>16}"
          f"{'margem':>9}  situacao")
    print("-" * 62)
    for seed in args.seeds:
        demo = make_synthetic_demo(rounds=args.rounds, seed=seed,
                                   profiles=perfis)
        report = analyze_demo(demo, cfg)
        cheats = [p.score for p in report.players
                  if not p.name.startswith("honest")]
        honest = [p.score for p in report.players
                  if p.name.startswith("honest")]
        pior, melhor = min(cheats), max(honest)
        margem = pior - melhor
        ok = margem > 0
        if not ok:
            falhas.append(f"seed {seed}: sobreposicao de {-margem:.1f} pontos")
        print(f"{seed:<6}{pior:>14.1f}{melhor:>16.1f}{margem:>9.1f}  "
              f"{'ok' if ok else 'SOBREPOSICAO'}")

    print("\n=== 2D (osu! sintetico) ===")
    print(f"{'perfil':<14}{'score':>8}  sinais fortes")
    print("-" * 62)
    with tempfile.TemporaryDirectory() as tmp:
        scores = {}
        for perfil in ("human", "relax", "aim_assist", "replay_bot"):
            path = write_osr(Path(tmp) / f"{perfil}.osr",
                             make_osu_frames(perfil, seed=5), player=perfil)
            report = analyze_demo(read_osr(path), cfg)
            player = report.players[0]
            scores[perfil] = player.score
            fortes = ", ".join(f"{k} {v.value:.2f}"
                               for k, v in sorted(player.signals.items())
                               if v.value >= 0.4)
            print(f"{perfil:<14}{player.score:>8.1f}  {fortes or '-'}")
        piores = min(v for k, v in scores.items() if k != "human")
        if scores["human"] >= piores:
            falhas.append("2D: o perfil humano nao ficou abaixo dos cheats")

    print()
    if falhas:
        print("FALHAS:")
        for falha in falhas:
            print(f"  - {falha}")
        print("\nO simulador modela o que os detectores procuram, entao uma")
        print("falha aqui e sinal claro de regressao no codigo.")
        return 1

    print("separacao preservada em todos os cenarios.")
    print("Lembre: isto valida a matematica, nao a calibracao contra demos")
    print("reais. Para isso so existe `csradar recheck` + `csradar eval`.")
    return 0


# ------------------------------------------------------------------- prune


def cmd_prune(args) -> int:
    with Store(args.db) as st:
        result = analytics.prune(st, keep_matches=args.keep,
                                 dry_run=not args.confirm)
    for key, value in result.items():
        print(f"{key:>22}: {value}")
    if not args.confirm and result.get("removeria"):
        print("\nnada foi removido. Use --confirm para aplicar.")
    return 0
