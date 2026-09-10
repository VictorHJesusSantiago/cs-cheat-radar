"""Comandos: convert, train, watch-list, daemon, platform, doctor."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import Config, default_home
from .storage import Store


# ----------------------------------------------------------------- convert


def cmd_convert(args) -> int:
    from .games.converters import (
        MAP_SPEC_EXAMPLE, ConversionError, from_mapping, from_pubg_telemetry,
        from_r6_dissect, write_ingest,
    )

    if args.example_spec:
        print(json.dumps(MAP_SPEC_EXAMPLE, indent=2, ensure_ascii=False))
        return 0
    if not args.path:
        print("informe o arquivo de entrada (ou --example-spec).")
        return 1

    try:
        if args.source == "pubg":
            demo = from_pubg_telemetry(args.path, tickrate=args.tickrate)
        elif args.source == "r6":
            demo = from_r6_dissect(args.path)
        elif args.source == "map":
            if not args.spec:
                print("--from map exige --spec ARQUIVO. Veja --example-spec.")
                return 1
            demo = from_mapping(args.path, args.spec, tickrate=args.tickrate)
        else:
            print(f"origem desconhecida: {args.source}")
            return 1
    except ConversionError as exc:
        print(f"erro de conversao: {exc}", file=sys.stderr)
        return 1

    out = args.out or str(Path(args.path).with_suffix(".ingest.jsonl"))
    write_ingest(demo, out)
    print(f"{len(demo.players)} jogadores, {len(demo.kills)} kills, "
          f"{sum(len(v) for v in demo.ticks_by_player.values())} ticks")
    print(f"capacidades: {', '.join(sorted(demo.capabilities)) or 'nenhuma'}")
    print(f"-> {out}")

    if args.analyze:
        from .report import render_match
        from .scoring import analyze_demo

        cfg = Config.load()
        report = analyze_demo(demo, cfg)
        print()
        print(render_match(report, cfg))
        if not args.no_save:
            with Store(args.db) as st:
                st.save_report(report)
    return 0


# ------------------------------------------------------------------- train


def cmd_train(args) -> int:
    from .ml import Model, NotEnoughData, default_model_path, train

    cfg = Config.load()
    with Store(args.db) as st:
        try:
            model = train(st, epochs=args.epochs,
                          learning_rate=args.learning_rate,
                          test_fraction=args.test_fraction, seed=args.seed)
        except NotEnoughData as exc:
            print(str(exc))
            return 1

    print("treinado com:")
    for key, value in model.trained_on.items():
        print(f"  {key:>14}: {value}")
    print("\nmetricas no conjunto de teste (divisao POR JOGADOR):")
    for key, value in model.metrics.items():
        if key == "aviso":
            continue
        print(f"  {key:>16}: {value}")
    print(f"\n  {model.metrics['aviso']}")

    print("\ncoeficientes (maior = empurra mais para 'sera banido'):")
    for name, weight in sorted(model.weights.items(), key=lambda kv: -kv[1]):
        print(f"  {name:>10}: {weight:+.3f}")

    path = Path(args.out) if args.out else default_model_path(default_home())
    model.save(path)
    print(f"\nmodelo salvo em {path}")

    if args.apply:
        weights = model.to_scoring_weights()
        if not weights:
            print("nenhum coeficiente positivo; pesos nao aplicados.")
            return 0
        cfg.scoring.weights = weights
        cfg.save()
        print(f"pesos aplicados na configuracao: {weights}")
    else:
        print("use --apply para substituir os pesos chutados por estes.")
    return 0


# --------------------------------------------------------------- watchlist


def cmd_watchlist(args) -> int:
    from . import watchlist as wl
    from .steam import to_steam64

    with Store(args.db) as st:
        if args.add:
            sid = to_steam64(args.add)
            if not sid:
                print(f"steamid invalido: {args.add}")
                return 1
            novo = wl.add(st, sid, label=args.label or "", reason=args.reason or "")
            print(f"{'adicionado' if novo else 'atualizado'}: {sid}")
            return 0

        if args.remove:
            sid = to_steam64(args.remove)
            if not sid:
                print(f"steamid invalido: {args.remove}")
                return 1
            print("removido" if wl.remove(st, sid) else "nao estava na lista")
            return 0

        rows = wl.entries(st)
        if not rows:
            print("watchlist vazia. Use `csradar watchlist --add STEAMID`.")
            return 0
        print(f"{'jogador':<24}{'partidas':>9}{'medio':>8}{'max':>7}"
              f"{'visto':>7}  situacao")
        print("-" * 74)
        for r in rows:
            nome = r["nome"] or r["label"] or str(r["steamid"])
            medio = f"{r['score_medio']:.1f}" if r["score_medio"] is not None else "-"
            maximo = f"{r['score_max']:.1f}" if r["score_max"] is not None else "-"
            situacao = "BANIDO" if r["bans"] else (r["reason"] or "")
            print(f"{nome[:23]:<24}{r['partidas'] or 0:>9}{medio:>8}"
                  f"{maximo:>7}{r['times_seen']:>7}  {situacao[:24]}")
            print(f"{'':<24}https://steamcommunity.com/profiles/{r['steamid']}")
    return 0


# ------------------------------------------------------------------ daemon


def cmd_daemon(args) -> int:
    from . import watchlist as wl
    from .games import load_any
    from .report import render_match
    from .scoring import analyze_demo

    cfg = Config.load()
    folder = Path(args.folder)
    if not folder.exists():
        print(f"pasta nao encontrada: {folder}")
        return 1

    print(f"vigiando {folder}")
    print(f"intervalo: {args.interval:g}s   Ctrl+C encerra.\n")

    try:
        while True:
            with Store(args.db) as st:
                try:
                    pending = wl.pending_files(st, folder)
                except FileNotFoundError as exc:
                    print(f"erro: {exc}")
                    return 1

                for path in pending:
                    if not wl.is_stable(path):
                        continue          # ainda sendo escrito
                    stamp = time.strftime("%H:%M:%S")
                    print(f"[{stamp}] analisando {path.name} ...", flush=True)
                    try:
                        demo = load_any(path, tickrate=args.tickrate)
                        report = analyze_demo(demo, cfg)
                    except Exception as exc:   # noqa: BLE001 - vigia nao morre
                        print(f"[{stamp}] falhou: {exc}")
                        wl.mark_analyzed(st, path)
                        continue

                    match_id = st.save_report(report)
                    wl.mark_analyzed(st, path, match_id)

                    flagged = [p for p in report.sorted_players()
                               if "REVISAR" in p.flags]
                    hits = wl.check_report(st, report)
                    if args.auto_watch:
                        wl.auto_add_from_report(st, report, cfg)

                    if flagged or hits or args.verbose:
                        print(render_match(report, cfg))
                    else:
                        top = report.sorted_players()[0]
                        print(f"[{stamp}] nada acima do limiar "
                              f"(maior: {top.name} {top.score:.1f})")
                    for hit in hits:
                        print(f"  !! WATCHLIST: {hit['nome']} apareceu de novo "
                              f"(score agora {hit['score_agora']:.1f}, "
                              f"medio {hit['score_medio'] or 0:.1f} em "
                              f"{hit['partidas_anteriores']} partidas)")
                    if args.html:
                        from .htmlreport import write

                        out = Path(args.html) / f"{path.stem}.html"
                        out.parent.mkdir(parents=True, exist_ok=True)
                        write(report, cfg, out)
                        print(f"[{stamp}] relatorio: {out}")

            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nencerrando ...")
        return 0


# ---------------------------------------------------------------- platform


def cmd_platform(args) -> int:
    """Uma consulta a uma plataforma, impressa como JSON.

    O despacho e uma tabela porque cada plataforma pede um identificador
    diferente: umas aceitam SteamID, outras so conhecem o apelido delas
    proprias. Misturar isso em ifs encadeados so esconde a diferenca.
    """
    from . import platforms as P
    from .steam import to_steam64

    cfg = Config.load()
    plat = cfg.platforms
    service = args.service

    def steamid():
        sid = to_steam64(args.target)
        if not sid:
            raise P.PlatformError(f"steamid invalido: {args.target}")
        return sid

    timeout = cfg.steam.request_timeout

    handlers = {
        "faceit": lambda: P.FaceitClient(plat.faceit_key, timeout).profile(
            steamid(), game=args.game or "cs2"),
        "battlemetrics": lambda: P.BattlemetricsClient(
            plat.battlemetrics_key, timeout).risk(steamid()),
        "steamcommunity": lambda: P.SteamCommunityClient(
            timeout=timeout).profile(steamid()),
        "opendota": lambda: P.OpenDotaClient(timeout=timeout).profile(steamid()),
        "ballchasing": lambda: P.BallchasingClient(
            plat.ballchasing_key, timeout).profile(steamid()),
        "bungie": lambda: P.BungieClient(plat.bungie_key, timeout).profile(
            steamid()),
        "riot": lambda: P.RiotClient(
            plat.riot_key, args.region or plat.riot_region, timeout).profile(
                args.target),
        "wargaming": lambda: P.WargamingClient(
            plat.wargaming_key, args.region or plat.wargaming_region,
            args.game if args.game in ("wot", "wows") else "wot",
            timeout).profile(args.target),
        "gametools": lambda: P.GametoolsClient(timeout=timeout).profile(
            args.target, game=args.game if args.game != "cs2" else "bf2042"),
        "osu": lambda: P.OsuClient(plat.osu_key, timeout).profile(args.target),
        "openxbl": lambda: P.OpenXblClient(plat.openxbl_key, timeout).profile(
            args.target),
        "tracker": lambda: P.TrackerClient(plat.tracker_key, timeout).profile(
            steamid(), game=args.game if args.game != "cs2" else "cs2"),
        "lichess": lambda: P.LichessClient(timeout=timeout).profile(args.target),
        "chesscom": lambda: P.ChessComClient(timeout=timeout).profile(
            args.target),
        "runescape": lambda: P.RunescapeClient(timeout=timeout).profile(
            args.target,
            mode=args.game if args.game in ("osrs", "rs3") else "osrs"),
    }

    try:
        if service == "pubg":
            return _pubg_telemetry(cfg, args)
        handler = handlers.get(service)
        if handler is None:
            print(f"servico desconhecido: {service}", file=sys.stderr)
            return 1
        print(json.dumps(handler(), indent=2, ensure_ascii=False))
        return 0
    except P.PlatformError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1


def _pubg_telemetry(cfg, args) -> int:
    """A PUBG e a unica que baixa arquivo em vez de imprimir perfil."""
    from .platforms import PubgClient

    client = PubgClient(cfg.platforms.pubg_key, shard=cfg.platforms.pubg_shard)
    matches = client.player_matches(args.target)
    if not matches:
        print(f"nenhuma partida recente para {args.target}")
        return 1
    print(f"{len(matches)} partida(s); baixando a mais recente ...")
    url = client.telemetry_url(matches[0])
    if not url:
        print("a partida nao expos telemetria")
        return 1
    out = args.out or f"pubg_{matches[0]}.json"
    client.download_telemetry(url, out)
    print(f"-> {out}")
    print(f"agora: csradar convert --from pubg {out} --analyze")
    return 0


# ------------------------------------------------------------------ doctor


def cmd_doctor(args) -> int:
    """Diagnostico: o que esta pronto e o que falta configurar."""
    import platform as _platform

    cfg = Config.load()
    ok, warn, bad = [], [], []

    ok.append(f"Python {_platform.python_version()} em {_platform.system()}")

    try:
        import demoparser2  # noqa: F401

        ok.append("demoparser2 instalado (demos de CS2 legiveis)")
    except ImportError:
        bad.append("demoparser2 AUSENTE - `pip install demoparser2` para "
                   "analisar demos de CS2")

    home = default_home()
    ok.append(f"configuracao em {home}") if home.exists() else warn.append(
        f"{home} ainda nao existe (sera criada no primeiro uso)")

    if cfg.steam.api_key:
        ok.append("chave da Steam configurada")
    else:
        warn.append("sem chave da Steam - `risk`, `recheck` e a triagem ao "
                    "vivo ficam desligados")
    from .platforms import KEYLESS, key_status

    for label, value, where in key_status(cfg.platforms):
        (ok if value else warn).append(
            f"chave {label} {'configurada' if value else f'ausente ({where})'}")
    ok.append(f"{len(KEYLESS)} plataformas sem chave sempre disponiveis: "
              f"{', '.join(KEYLESS)}")

    from .live import find_console_log

    log = find_console_log()
    if log:
        ok.append(f"console.log encontrado: {log}")
    else:
        warn.append("console.log nao encontrado - adicione -condebug ao CS2")

    try:
        with Store(args.db) as st:
            stats = st.stats()
        ok.append(f"banco: {stats['partidas']} partidas, "
                  f"{stats['jogadores']} jogadores, "
                  f"{stats['banidos']} banidos conhecidos")
        if stats["partidas"] == 0:
            warn.append("banco vazio - rode `csradar analyze` em alguma demo")
        elif stats["consultas_de_ban"] == 0:
            warn.append("nenhum recheck feito ainda - agende `csradar recheck`")
    except Exception as exc:   # noqa: BLE001
        bad.append(f"banco inacessivel: {exc}")

    from .catalog import scan_all

    try:
        games = scan_all(only=("steam", "epic"))
        conhecidos = [g for g in games if g.profile]
        ok.append(f"scanners: {len(games)} jogos vistos, "
                  f"{len(conhecidos)} no catalogo")
    except Exception as exc:   # noqa: BLE001
        warn.append(f"varredura de jogos falhou: {exc}")

    for title, items, mark in (("OK", ok, "  + "), ("ATENCAO", warn, "  ! "),
                               ("PROBLEMA", bad, "  x ")):
        if not items:
            continue
        print(f"\n{title}")
        for item in items:
            print(f"{mark}{item}")

    print()
    return 1 if bad else 0
