"""Comandos de catalogo, ingestao e tempo real."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .catalog import find_artifacts, scan_all
from .config import Config
from .games import ADAPTERS, GAMES, INGEST_SPEC, Support, by_key
from .games.registry import expand_path
from .report import render_match

SUPPORT_ORDER = [Support.FULL, Support.POSITIONAL, Support.EVENTS,
                 Support.ACCOUNT, Support.NONE]

SUPPORT_HELP = {
    Support.FULL: "replay com angulo por tick: todos os sinais rodam",
    Support.POSITIONAL: "posicoes por tick, sem mira: sinais parciais",
    Support.EVENTS: "so eventos: apenas ritmo de kills (triagem fraca)",
    Support.ACCOUNT: "sem replay legivel: so risco de conta e ban retroativo",
    Support.NONE: "nada",
}


# ------------------------------------------------------------------- games


def cmd_games(args) -> int:
    if args.spec:
        print(INGEST_SPEC)
        return 0
    if args.game:
        g = by_key(args.game)
        if not g:
            print(f"jogo desconhecido: {args.game}. Use `csradar games` para a lista.")
            return 1
        print(f"{g.name}  [{g.key}]")
        print(f"  suporte:    {g.support}  - {SUPPORT_HELP[g.support]}")
        print(f"  adaptador:  {g.adapter or '(nenhum)'}")
        print(f"  launchers:  {', '.join(g.launchers) or '-'}")
        print(f"  tempo real: {', '.join(g.live) or 'nada oficial disponivel'}")
        for a in g.artifacts:
            print(f"  artefato:   {expand_path(a)}")
        if g.note:
            print(f"  nota:       {g.note}")
        return 0

    groups: dict = {}
    for g in GAMES.values():
        groups.setdefault(g.support, []).append(g)

    for support in SUPPORT_ORDER:
        items = sorted(groups.get(support, []), key=lambda g: g.name.lower())
        if not items:
            continue
        print(f"\n{str(support).upper()} - {SUPPORT_HELP[support]}")
        print("-" * 74)
        for g in items:
            live = f"  ao vivo: {','.join(g.live)}" if g.live else ""
            print(f"  {g.name:<44} {g.key:<16}{live}")
            if args.verbose and g.note:
                print(f"      {g.note}")

    print("\nAdaptadores de leitura:")
    for a in ADAPTERS.values():
        req = f" (requer {', '.join(a.requires)})" if a.requires else ""
        print(f"  {a.name:<12} {a.description}{req}")
    print("\nJogo nao listado? Se ele exporta replay ou log, converta para o")
    print("formato de ingestao (`csradar games --spec`) e use `csradar ingest`.")
    return 0


# -------------------------------------------------------------------- scan


def cmd_scan(args) -> int:
    print("varrendo launchers ...", flush=True)
    games = scan_all(
        steam_dir=args.steam_dir,
        epic_dir=args.epic_dir,
        use_registry=not args.no_registry,
    )
    if not games:
        print("nenhum jogo encontrado. Passe --steam-dir se a Steam estiver")
        print("num caminho fora do padrao.")
        return 1

    if args.supported_only:
        games = [g for g in games if g.support != Support.NONE
                 and g.profile is not None]

    by_support: dict = {}
    for g in games:
        by_support.setdefault(str(g.support), []).append(g)

    print(f"\n{len(games)} jogo(s) encontrados\n")
    for support in SUPPORT_ORDER:
        items = by_support.get(str(support), [])
        if not items:
            continue
        print(f"{str(support).upper()} - {SUPPORT_HELP[support]}")
        for g in items:
            size = f"{g.size_bytes / 1024**3:.1f} GB" if g.size_bytes else ""
            print(f"  {g.name:<44} {g.launcher:<10} {size}")
            if args.artifacts and g.profile:
                for path in find_artifacts(g)[:5]:
                    print(f"      replay: {path}")
        print()

    unknown = [g for g in games if g.profile is None]
    if unknown and not args.supported_only:
        print(f"{len(unknown)} jogo(s) fora do catalogo (nenhuma analise "
              f"disponivel). Use --supported-only para ocultar.")

    if args.json:
        Path(args.json).write_text(
            json.dumps([g.as_dict() for g in games], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"JSON salvo em {args.json}")
    return 0


# ------------------------------------------------------------------ ingest


def cmd_ingest(args) -> int:
    from .games import load_ingest
    from .scoring import analyze_demo
    from .storage import Store

    if args.spec:
        print(INGEST_SPEC)
        return 0
    if not args.path:
        print("informe o arquivo, ou use --spec para ver o formato.")
        return 1

    cfg = Config.load()
    demo = load_ingest(args.path, tickrate=args.tickrate, game=args.game)
    report = analyze_demo(demo, cfg)
    print(render_match(report, cfg, verbose=args.verbose))
    if not args.no_save:
        with Store(args.db) as st:
            st.save_report(report)
    return 0


# ---------------------------------------------------------------- realtime


def cmd_realtime(args) -> int:
    from .realtime import (
        FileLogSource, GSIServer, LiveEngine, RconClient, UdpLogSource,
        install_gsi_config,
    )

    cfg = Config.load()

    if args.install_gsi:
        target = install_gsi_config(args.install_gsi, port=args.gsi_port)
        print(f"config do GSI escrita em {target}")
        print("reinicie o CS2 para o jogo passar a enviar o estado.")
        return 0

    risk_lookup = None
    if not args.no_risk:
        if not cfg.steam.api_key:
            print("aviso: sem chave da Steam, a triagem de conta fica desligada.")
            print("       `csradar config --steam-key SUACHAVE` para ligar.\n")
        else:
            from .steam import SteamClient, risk_profile

            client = SteamClient(cfg.steam.api_key, cfg.steam.request_timeout)
            risk_lookup = lambda sid: risk_profile(client, sid, deep=args.deep)  # noqa: E731

    engine = LiveEngine(cfg, on_alert=lambda a: print(a.line(), flush=True),
                        risk_lookup=risk_lookup)

    sources = []
    started = []

    if args.udp:
        host, _, port = args.udp.rpartition(":")
        src = UdpLogSource(host or "0.0.0.0", int(port))
        src.start()
        sources.append(src)
        started.append(f"UDP {src.host}:{src.port} (use logaddress_add no servidor)")

    if args.log:
        src = FileLogSource(args.log, from_start=args.from_start)
        src.start()
        sources.append(src)
        started.append(f"arquivo {args.log}")

    gsi = None
    if args.gsi:
        gsi = GSIServer(port=args.gsi_port, sink=engine.feed_gsi)
        gsi.start()
        started.append(f"GSI em http://127.0.0.1:{args.gsi_port}")

    rcon = None
    if args.rcon:
        host, port, password = _parse_rcon(args.rcon)
        rcon = RconClient(host, port, password)
        rcon.connect()
        started.append(f"RCON {host}:{port}")

    if not started:
        print("nenhuma fonte escolhida. Opcoes:")
        print("  --gsi                 estado do CS2 (recurso oficial da Valve)")
        print("  --log ARQ             console.log do cliente ou log do servidor")
        print("  --udp 0.0.0.0:27500   log de servidor dedicado seu")
        print("  --rcon host:porta:senha")
        print("\nPrimeira vez com GSI: `csradar realtime --install-gsi <pasta cfg>`")
        return 1

    print("fontes ativas:")
    for s in started:
        print(f"  - {s}")
    print("\nAnalise de mira NAO roda ao vivo: nenhuma fonte oficial expoe o")
    print("angulo de visao dos outros jogadores. Ao final, analise a demo.")
    print("Ctrl+C encerra.\n")

    last_rcon = 0.0
    try:
        while True:
            got = False
            for src in sources:
                for line in src.drain():
                    got = True
                    engine.feed_log_line(line)
            if rcon and time.time() - last_rcon > args.rcon_interval:
                last_rcon = time.time()
                try:
                    engine.feed_status_text(rcon.command("status"))
                except Exception as exc:
                    print(f"rcon: {exc}", file=sys.stderr)
            if not got:
                time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nencerrando ...")
    finally:
        for src in sources:
            src.stop()
        if gsi:
            gsi.stop()
        if rcon:
            rcon.close()

    summary = engine.summary()
    print(f"\ncoletado: {summary['jogadores']} jogadores, {summary['kills']} "
          f"kills, {summary['rounds']} rounds em {summary['mapa']}")
    if engine.demo.kills:
        print()
        print(render_match(engine.snapshot(), cfg))
    return 0


def _parse_rcon(text: str) -> tuple:
    parts = text.split(":")
    if len(parts) < 3:
        raise ValueError("use --rcon host:porta:senha")
    return parts[0], int(parts[1]), ":".join(parts[2:])
