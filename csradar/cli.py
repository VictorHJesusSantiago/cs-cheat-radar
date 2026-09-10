"""Interface de linha de comando do cs-cheat-radar."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .cli_extra import (
    cmd_convert, cmd_daemon, cmd_doctor, cmd_platform, cmd_train, cmd_watchlist,
)
from .cli_games import cmd_games, cmd_ingest, cmd_realtime, cmd_scan
from .cli_more import (
    cmd_calibrate, cmd_explain, cmd_history, cmd_plugins, cmd_prune,
    cmd_recurring, cmd_rings, cmd_selftest, cmd_share,
)
from .config import Config, default_home
from .platforms import KEYLESS, PLATFORMS, cross_check, key_status
from .report import render_match, render_risk
from .scoring import analyze_demo
from .storage import Store

BANNER = ("cs-cheat-radar %s - triagem de suspeitas de cheat em "
          "replays, logs e APIs publicas" % __version__)


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\ninterrompido.")
        return 130
    except Exception as exc:  # erro de usuario deve sair legivel, nao traceback
        print(f"erro: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="csradar", description=BANNER)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", help="caminho do banco SQLite")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser(
        "analyze",
        help="analisa uma demo .dem, um log de servidor ou um arquivo de ingestao",
    )
    a.add_argument("path", help="arquivo (.dem/.log/.json/.jsonl/.csv) ou diretorio")
    a.add_argument("--game", default="", help="chave do jogo (veja `csradar games`)")
    a.add_argument("--adapter", default="", choices=["", "cs2_demo", "srcds_log",
                                                     "ingest"])
    a.add_argument("--me", help="seu SteamID, para destacar voce no relatorio")
    a.add_argument("--json", metavar="ARQ", help="salva o relatorio em JSON")
    a.add_argument("--no-save", action="store_true", help="nao grava no banco")
    a.add_argument("--verbose", "-v", action="store_true")
    a.add_argument("--tickrate", type=float, default=64.0)
    a.add_argument("--html", metavar="ARQ_OU_PASTA",
                   help="tambem gera relatorio HTML")
    a.add_argument("--watch-hits", action="store_true",
                   help="avisa se alguem da watchlist aparecer")
    a.add_argument("--plugins", action="store_true",
                   help="tambem roda os detectores da sua pasta de plugins")
    a.set_defaults(func=cmd_analyze)

    d = sub.add_parser("demo", help="roda o pipeline em uma partida sintetica")
    d.add_argument("--rounds", type=int, default=24)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--verbose", "-v", action="store_true")
    d.add_argument("--save", action="store_true", help="grava no banco")
    d.set_defaults(func=cmd_demo)

    w = sub.add_parser("watch", help="le o console.log ao vivo e faz triagem de contas")
    w.add_argument("--log", help="caminho do console.log")
    w.add_argument("--deep", action="store_true", help="consulta horas e amigos (lento)")
    w.add_argument("--from-start", action="store_true")
    w.set_defaults(func=cmd_watch)

    r = sub.add_parser("risk", help="perfil de risco de contas Steam")
    r.add_argument("steamids", nargs="+", help="SteamID em qualquer formato")
    r.add_argument("--deep", action="store_true")
    r.add_argument("--cross", action="store_true",
                   help="cruza com as plataformas externas que aceitam "
                        "SteamID (perfil publico da Steam sem chave; FACEIT e "
                        "Battlemetrics quando a chave existe)")
    r.set_defaults(func=cmd_risk)

    c = sub.add_parser("recheck", help="reconsulta bans (rotulo retroativo)")
    c.add_argument("--min-age-days", type=int, default=30)
    c.add_argument("--limit", type=int, default=500)
    c.set_defaults(func=cmd_recheck)

    e = sub.add_parser("eval", help="precisao/revocacao do score contra os bans")
    e.add_argument("--threshold", type=float)
    e.set_defaults(func=cmd_eval)

    t = sub.add_parser("top", help="maiores suspeitos acumulados no banco")
    t.add_argument("--limit", type=int, default=20)
    t.add_argument("--min-score", type=float, default=0.0)
    t.set_defaults(func=cmd_top)

    x = sub.add_parser("export", help="exporta o dataset rotulado em JSON")
    x.add_argument("out", help="arquivo de saida")
    x.set_defaults(func=cmd_export)

    s = sub.add_parser("stats", help="estado do banco")
    s.set_defaults(func=cmd_stats)

    cf = sub.add_parser("config", help="mostra ou grava a configuracao")
    cf.add_argument("--steam-key", help="grava a chave da Steam Web API")
    cf.add_argument("--review-threshold", type=float)
    cf.add_argument("--faceit-key")
    cf.add_argument("--battlemetrics-key")
    cf.add_argument("--pubg-key")
    cf.add_argument("--riot-key")
    cf.add_argument("--bungie-key")
    cf.add_argument("--wargaming-key", help="application_id da Wargaming")
    cf.add_argument("--ballchasing-key")
    cf.add_argument("--osu-key")
    cf.add_argument("--openxbl-key")
    cf.add_argument("--tracker-key", help="TRN-Api-Key da tracker.gg")
    cf.add_argument("--init", action="store_true", help="escreve o arquivo padrao")
    cf.set_defaults(func=cmd_config)

    g = sub.add_parser("games", help="catalogo de jogos e o que da para fazer em cada")
    g.add_argument("game", nargs="?", help="detalhe de um jogo especifico")
    g.add_argument("--spec", action="store_true",
                   help="mostra o formato de ingestao")
    g.add_argument("--verbose", "-v", action="store_true")
    g.set_defaults(func=cmd_games)

    sc = sub.add_parser("scan", help="procura jogos instalados em todos os launchers")
    sc.add_argument("--steam-dir", help="pasta da Steam, se estiver fora do padrao")
    sc.add_argument("--epic-dir", help="pasta de manifestos da Epic")
    sc.add_argument("--no-registry", action="store_true",
                    help="nao varrer o registro do Windows")
    sc.add_argument("--supported-only", action="store_true")
    sc.add_argument("--artifacts", action="store_true",
                    help="listar replays encontrados")
    sc.add_argument("--json", metavar="ARQ")
    sc.set_defaults(func=cmd_scan)

    ig = sub.add_parser("ingest", help="analisa dados de qualquer jogo no formato normalizado")
    ig.add_argument("path", nargs="?", help="arquivo .json, .jsonl ou .csv")
    ig.add_argument("--game", default="", help="chave do jogo, so para rotular")
    ig.add_argument("--tickrate", type=float, default=64.0)
    ig.add_argument("--spec", action="store_true", help="mostra o formato aceito")
    ig.add_argument("--no-save", action="store_true")
    ig.add_argument("--verbose", "-v", action="store_true")
    ig.set_defaults(func=cmd_ingest)

    rt = sub.add_parser("realtime", help="acompanha uma partida ao vivo por fontes oficiais")
    rt.add_argument("--gsi", action="store_true",
                    help="recebe o Game State Integration do CS2")
    rt.add_argument("--gsi-port", type=int, default=3000)
    rt.add_argument("--install-gsi", metavar="PASTA_CFG",
                    help="escreve o arquivo de config do GSI e sai")
    rt.add_argument("--log", help="console.log do cliente ou log do servidor")
    rt.add_argument("--from-start", action="store_true")
    rt.add_argument("--udp", metavar="HOST:PORTA",
                    help="escuta log de servidor dedicado (logaddress_add)")
    rt.add_argument("--rcon", metavar="HOST:PORTA:SENHA")
    rt.add_argument("--rcon-interval", type=float, default=30.0)
    rt.add_argument("--deep", action="store_true",
                    help="triagem de conta mais completa (lenta)")
    rt.add_argument("--no-risk", action="store_true",
                    help="nao consultar a Steam")
    rt.set_defaults(func=cmd_realtime)

    cv = sub.add_parser("convert",
                        help="converte saida de ferramenta externa para ingestao")
    cv.add_argument("path", nargs="?", help="arquivo de entrada")
    cv.add_argument("--from", dest="source", default="map",
                    choices=["pubg", "r6", "map"])
    cv.add_argument("--spec", help="arquivo de mapeamento (--from map)")
    cv.add_argument("--out", help="arquivo .jsonl de saida")
    cv.add_argument("--tickrate", type=float, default=64.0)
    cv.add_argument("--analyze", action="store_true",
                    help="ja analisa o resultado")
    cv.add_argument("--no-save", action="store_true")
    cv.add_argument("--example-spec", action="store_true",
                    help="imprime um mapeamento de exemplo")
    cv.set_defaults(func=cmd_convert)

    tr = sub.add_parser("train",
                        help="treina os pesos com os bans acumulados")
    tr.add_argument("--epochs", type=int, default=400)
    tr.add_argument("--learning-rate", type=float, default=0.35)
    tr.add_argument("--test-fraction", type=float, default=0.3)
    tr.add_argument("--seed", type=int, default=13)
    tr.add_argument("--out", help="onde salvar o modelo")
    tr.add_argument("--apply", action="store_true",
                    help="substitui os pesos da configuracao pelos aprendidos")
    tr.set_defaults(func=cmd_train)

    wl = sub.add_parser("watchlist", help="acompanha suspeitos entre partidas")
    wl.add_argument("--add", metavar="STEAMID")
    wl.add_argument("--remove", metavar="STEAMID")
    wl.add_argument("--label")
    wl.add_argument("--reason")
    wl.set_defaults(func=cmd_watchlist)

    dm = sub.add_parser("daemon",
                        help="vigia uma pasta e analisa replays novos sozinho")
    dm.add_argument("folder", help="pasta de replays/demos")
    dm.add_argument("--interval", type=float, default=30.0)
    dm.add_argument("--tickrate", type=float, default=64.0)
    dm.add_argument("--html", metavar="PASTA", help="grava relatorios HTML")
    dm.add_argument("--auto-watch", action="store_true",
                    help="poe na watchlist quem passar do limiar")
    dm.add_argument("--once", action="store_true", help="uma passada so")
    dm.add_argument("--verbose", "-v", action="store_true")
    dm.set_defaults(func=cmd_daemon)

    pl = sub.add_parser(
        "platform", help="consulta as plataformas de rotulo e risco")
    pl.add_argument("service", choices=sorted(PLATFORMS))
    pl.add_argument("target",
                    help="SteamID, ou o identificador daquela plataforma "
                         "(nome na PUBG, Riot ID, gamertag, usuario)")
    pl.add_argument("--game", default="cs2",
                    help="jogo, onde a plataforma cobre mais de um")
    pl.add_argument("--region", help="regiao (Riot, Wargaming)")
    pl.add_argument("--out", help="arquivo de saida (telemetria da PUBG)")
    pl.set_defaults(func=cmd_platform)

    dc = sub.add_parser("doctor", help="diagnostico do ambiente")
    dc.set_defaults(func=cmd_doctor)

    cb = sub.add_parser("calibrate",
                        help="distribuicao dos sinais na SUA base e limiar sugerido")
    cb.add_argument("--percentile", type=float, default=0.99)
    cb.add_argument("--apply", action="store_true",
                    help="adota o limiar sugerido")
    cb.set_defaults(func=cmd_calibrate)

    hi = sub.add_parser("history", help="trajetoria de um jogador entre partidas")
    hi.add_argument("steamid")
    hi.set_defaults(func=cmd_history)

    ex = sub.add_parser("explain",
                        help="tudo o que o banco sabe sobre um jogador numa partida")
    ex.add_argument("steamid")
    ex.add_argument("--source", help="filtra por nome da demo/log")
    ex.add_argument("--json", action="store_true")
    ex.set_defaults(func=cmd_explain)

    rg = sub.add_parser("rings", help="jogadores que aparecem sempre juntos")
    rg.add_argument("--min-together", type=int, default=3)
    rg.add_argument("--min-score", type=float, default=30.0)
    rg.add_argument("--limit", type=int, default=25)
    rg.set_defaults(func=cmd_rings)

    rc = sub.add_parser("recurring",
                        help="quem pontua alto de forma consistente")
    rc.add_argument("--min-matches", type=int, default=3)
    rc.add_argument("--min-mean", type=float, default=35.0)
    rc.add_argument("--limit", type=int, default=25)
    rc.set_defaults(func=cmd_recurring)

    sh = sub.add_parser("share",
                        help="troca dataset rotulado e anonimo com outras pessoas")
    sh.add_argument("--export", metavar="ARQ")
    sh.add_argument("--inspect", nargs="+", metavar="ARQ")
    sh.add_argument("--train-with", nargs="+", metavar="ARQ")
    sh.add_argument("--salt", help="reusar um sal para cruzar seus exports")
    sh.add_argument("--note", help="nota livre gravada no arquivo")
    sh.add_argument("--epochs", type=int, default=400)
    sh.add_argument("--learning-rate", type=float, default=0.35)
    sh.add_argument("--out", help="onde salvar o modelo treinado")
    sh.set_defaults(func=cmd_share)

    pg = sub.add_parser("plugins", help="detectores seus, carregados de uma pasta")
    pg.add_argument("--init", action="store_true",
                    help="cria a pasta e um exemplo funcional")
    pg.set_defaults(func=cmd_plugins)

    stf = sub.add_parser("selftest",
                         help="mede a separacao honesto/cheater no simulador")
    stf.add_argument("--rounds", type=int, default=20)
    stf.add_argument("--seeds", type=int, nargs="+", default=[7, 11, 23, 42])
    stf.set_defaults(func=cmd_selftest)

    pr = sub.add_parser("prune", help="descarta partidas antigas do banco")
    pr.add_argument("--keep", type=int, default=500)
    pr.add_argument("--confirm", action="store_true")
    pr.set_defaults(func=cmd_prune)

    return p


def _store(args) -> Store:
    return Store(args.db)


# ------------------------------------------------------------------- comandos


def cmd_analyze(args) -> int:
    from .demo import ParserUnavailable
    from .games import load_any

    cfg = Config.load()
    path = Path(args.path)
    if path.is_dir():
        demos = sorted(
            p for ext in ("*.dem", "*.log", "*.json", "*.jsonl", "*.csv")
            for p in path.glob(ext)
        )
    else:
        demos = [path]
    if not demos:
        print(f"nenhum arquivo analisavel em {path}")
        return 1

    me = int(args.me) if args.me and str(args.me).isdigit() else None
    extra = ()
    if getattr(args, "plugins", False):
        from .plugins import load_all

        extra, errors = load_all()
        for name, error in errors.items():
            print(f"plugin {name} nao carregou: {error}", file=sys.stderr)
        if extra:
            print(f"plugins ativos: "
                  f"{', '.join(n for n, _f, _r, _p in extra)}")
    results = []
    for dem in demos:
        print(f"lendo {dem.name} ...", flush=True)
        try:
            demo = load_any(dem, tickrate=args.tickrate, game=args.game,
                            adapter=args.adapter)
        except ParserUnavailable as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
        report = analyze_demo(demo, cfg, extra_detectors=extra)
        results.append(report)
        print()
        print(render_match(report, cfg, verbose=args.verbose, me=me))

        if not args.no_save:
            with _store(args) as st:
                st.save_report(report)
                if args.watch_hits:
                    from . import watchlist as wl

                    for hit in wl.check_report(st, report):
                        print(f"  !! WATCHLIST: {hit['nome']} (score agora "
                              f"{hit['score_agora']:.1f}, medio "
                              f"{hit['score_medio'] or 0:.1f} em "
                              f"{hit['partidas_anteriores']} partidas)")

        if args.html:
            from .htmlreport import write

            target = Path(args.html)
            out = (target / f"{dem.stem}.html"
                   if len(demos) > 1 or target.is_dir() else target)
            write(report, cfg, out, me=me)
            print(f"relatorio HTML: {out}")

    if args.json:
        Path(args.json).write_text(
            json.dumps([r.as_dict() for r in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nJSON salvo em {args.json}")
    return 0


def cmd_demo(args) -> int:
    from .demo import make_synthetic_demo

    cfg = Config.load()
    print("Partida sintetica: o slot 3 usa aimbot e o slot 7 usa wallhack.")
    print("Serve para conferir o pipeline, nao para calibrar contra a realidade.\n")
    demo = make_synthetic_demo(rounds=args.rounds, seed=args.seed)
    report = analyze_demo(demo, cfg)
    print(render_match(report, cfg, verbose=args.verbose))
    if args.save:
        with _store(args) as st:
            st.save_report(report)
        print("\ngravado no banco.")
    return 0


def cmd_watch(args) -> int:
    from .live import find_console_log, watch
    from .steam import SteamClient, risk_profile

    cfg = Config.load()
    log = find_console_log(args.log)
    if not log:
        print(
            "console.log nao encontrado.\n"
            "1) adicione -condebug nas opcoes de inicializacao do CS2;\n"
            "2) rode o jogo uma vez;\n"
            "3) passe o caminho com --log, ou defina CSRADAR_CONSOLE_LOG."
        )
        return 1

    client = SteamClient(cfg.steam.api_key, cfg.steam.request_timeout)
    print(f"lendo {log}")
    print("digite `status` no console do jogo para listar os jogadores. Ctrl+C encerra.\n")

    def on_players(found: dict) -> None:
        print(f"\n{len(found)} jogador(es) novos:")
        profiles = []
        for sid, nick in found.items():
            try:
                pr = risk_profile(client, sid, deep=args.deep)
            except Exception as exc:
                print(f"  {nick}: falha ao consultar ({exc})")
                continue
            pr["nome"] = pr["nome"] or nick
            profiles.append(pr)
        if profiles:
            print(render_risk(profiles))

    watch(log, on_players, from_start=args.from_start)
    return 0


def cmd_risk(args) -> int:
    from .steam import SteamClient, risk_profile, to_steam64

    cfg = Config.load()
    client = SteamClient(cfg.steam.api_key, cfg.steam.request_timeout)
    profiles = []
    for raw in args.steamids:
        sid = to_steam64(raw)
        if not sid:
            print(f"steamid invalido: {raw}", file=sys.stderr)
            continue
        profile = risk_profile(client, sid, deep=args.deep)
        if args.cross:
            _merge_cross(profile, cross_check(sid, cfg.platforms,
                                              cfg.steam.request_timeout))
        profiles.append(profile)
    if not profiles:
        return 1
    print(render_risk(profiles))
    return 0


def _merge_cross(profile: dict, results: list) -> None:
    """Funde o risco externo no perfil da Steam.

    Nao somo os riscos: cada plataforma ja devolve 0-100 na propria escala, e
    somar transformaria duas suspeitas fracas numa certeza falsa. Fica o maior
    entre o local e o externo, e os motivos de todos aparecem com a fonte.
    """
    profile["externo"] = results
    for item in results:
        for reason in item["motivos"]:
            profile["motivos"].append(f"[{item['fonte']}] {reason}")
        profile["risco"] = max(profile["risco"], round(item["risco"], 1))


def cmd_recheck(args) -> int:
    from .labeling import run_recheck
    from .steam import SteamClient

    cfg = Config.load()
    client = SteamClient(cfg.steam.api_key, cfg.steam.request_timeout)
    with _store(args) as st:
        result = run_recheck(
            st, client, min_age_days=args.min_age_days, limit=args.limit,
            verbose=print,
        )
    print(f"consultados: {result['consultados']}")
    print(f"ja conhecidos como banidos: {result['ja_banidos']}")
    novos = result["novos_banidos"]
    print(f"novos banidos: {len(novos)}")
    for b in novos:
        print(f"  {b['steamid']}  vac={b['vac']}  game_bans={b['game_bans']}  "
              f"ha {b['dias_desde_ban']} dias")
    if novos:
        print("\nEstes viram rotulo positivo. Rode `csradar eval` para ver se o")
        print("score os separava antes do ban.")
    return 0


def cmd_eval(args) -> int:
    from .labeling import evaluate_threshold, sweep_thresholds

    cfg = Config.load()
    with _store(args) as st:
        if args.threshold is not None:
            rows = [evaluate_threshold(st, args.threshold)]
        else:
            rows = sweep_thresholds(st)
    if "erro" in rows[0]:
        print(rows[0]["erro"])
        return 1
    print(f"{'limiar':>7}{'tp':>5}{'fp':>5}{'fn':>5}{'precisao':>10}{'revocacao':>11}")
    for r in rows:
        print(f"{r['limiar']:>7.0f}{r['tp']:>5}{r['fp']:>5}{r['fn']:>5}"
              f"{r['precisao']:>10.3f}{r['revocacao']:>11.3f}")
    print(f"\njogadores: {rows[0]['jogadores']}   "
          f"banidos conhecidos: {rows[0]['banidos_conhecidos']}   "
          f"prevalencia: {rows[0]['prevalencia']}")
    print(rows[0]["aviso"])
    print(f"limiar de revisao em uso: {cfg.scoring.review_threshold:g}")
    return 0


def cmd_top(args) -> int:
    with _store(args) as st:
        rows = st.top_suspects(limit=args.limit, min_score=args.min_score)
    if not rows:
        print("banco vazio. Rode `csradar analyze` em alguma demo primeiro.")
        return 0
    print(f"{'jogador':<24}{'partidas':>9}{'score medio':>13}{'maximo':>9}{'kills':>7}")
    print("-" * 64)
    for r in rows:
        print(f"{(r['name'] or r['steamid']):<24}{r['partidas']:>9}"
              f"{r['score_medio']:>13.1f}{r['score_max']:>9.1f}{r['kills'] or 0:>7}")
        print(f"{'':<24}https://steamcommunity.com/profiles/{r['steamid']}")
    return 0


def cmd_export(args) -> int:
    with _store(args) as st:
        rows = st.labeled_dataset()
    Path(args.out).write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    positives = sum(r["label"] for r in rows)
    print(f"{len(rows)} observacoes ({positives} rotuladas como banidas) -> {args.out}")
    return 0


def cmd_stats(args) -> int:
    with _store(args) as st:
        for k, v in st.stats().items():
            print(f"{k:>18}: {v}")
    return 0


def cmd_config(args) -> int:
    cfg = Config.load()
    changed = False
    if args.steam_key:
        cfg.steam.api_key = args.steam_key
        changed = True
    if args.review_threshold is not None:
        cfg.scoring.review_threshold = args.review_threshold
        changed = True
    for attr, value in (("faceit_key", args.faceit_key),
                        ("battlemetrics_key", args.battlemetrics_key),
                        ("pubg_key", args.pubg_key),
                        ("riot_key", args.riot_key),
                        ("bungie_key", args.bungie_key),
                        ("wargaming_key", args.wargaming_key),
                        ("ballchasing_key", args.ballchasing_key),
                        ("osu_key", args.osu_key),
                        ("openxbl_key", args.openxbl_key),
                        ("tracker_key", args.tracker_key)):
        if value:
            setattr(cfg.platforms, attr, value)
            changed = True
    if changed or args.init:
        path = cfg.save()
        print(f"configuracao gravada em {path}")
        return 0
    print(f"arquivo:  {Config.path()}")
    print(f"home:     {default_home()}")
    print(f"chave Steam: {'definida' if cfg.steam.api_key else 'ausente'}")
    for label, value, _ in key_status(cfg.platforms):
        print(f"chave {label}: {'definida' if value else 'ausente'}")
    print(f"sem chave (sempre disponiveis): {', '.join(KEYLESS)}")
    print(f"limiar de revisao: {cfg.scoring.review_threshold:g}")
    print(f"pesos: {cfg.scoring.weights}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
