"""Relatorio em HTML: um arquivo, sem dependencia, sem rede.

O relatorio de terminal serve para olhar de relance. Quando a fila tem vinte
jogadores e voce quer conferir evidencia por evidencia com a demo aberta ao
lado, uma pagina que da para rolar e filtrar funciona melhor.

Tudo fica embutido num arquivo so - nada de CDN, nada de fonte remota - para
o relatorio continuar abrindo daqui a um ano e para nao vazar quem voce esta
analisando para servidor nenhum.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .report import SIGNAL_LABELS, TEAM_LABELS

CSS = """
:root{color-scheme:light dark;
 --bg:#faf9f7;--fg:#1c1b19;--muted:#6b6862;--line:#e2ded7;--card:#fff;
 --hi:#b4341f;--mid:#b8860b;--ok:#3f7d3f;--chip:#f0ede8}
@media (prefers-color-scheme:dark){:root{
 --bg:#16151a;--fg:#eceaf0;--muted:#9b96a3;--line:#2e2c35;--card:#1e1d24;
 --hi:#ff7a5c;--mid:#e0a93a;--ok:#7fc07f;--chip:#26242d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;margin-bottom:20px}
.warn{border-left:3px solid var(--mid);background:var(--card);
 padding:12px 16px;margin:18px 0;border-radius:0 6px 6px 0}
.warn b{color:var(--mid)}
table{width:100%;border-collapse:collapse;margin-top:8px}
th{text-align:left;font-weight:600;font-size:12px;color:var(--muted);
 text-transform:uppercase;letter-spacing:.04em;padding:8px 10px;
 border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.p{cursor:pointer}
tr.p:hover{background:var(--card)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.score{font-weight:700;font-variant-numeric:tabular-nums}
.s-hi{color:var(--hi)}.s-mid{color:var(--mid)}.s-ok{color:var(--muted)}
.chip{display:inline-block;background:var(--chip);border-radius:10px;
 padding:1px 8px;font-size:11px;margin:0 4px 3px 0;color:var(--muted)}
.chip.on{color:var(--hi)}
.bar{display:inline-block;width:110px;height:7px;background:var(--chip);
 border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:8px}
.bar>i{display:block;height:100%;background:var(--hi)}
.det{display:none;background:var(--card)}
.det.open{display:table-row}
.det td{padding:14px 18px 18px}
.sig{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}
.sig .nm{width:190px;color:var(--muted)}
.ev{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
.ev div{font-size:12.5px;padding:3px 0;color:var(--muted)}
.ev b{color:var(--fg);font-weight:600}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}
.foot{margin-top:34px;color:var(--muted);font-size:12.5px;
 border-top:1px solid var(--line);padding-top:14px}
"""

JS = """
document.addEventListener('click',function(e){
  var row=e.target.closest('tr.p'); if(!row) return;
  var d=document.getElementById('d-'+row.dataset.i);
  if(d) d.classList.toggle('open');
});
"""


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _score_class(score: float, threshold: float) -> str:
    if score >= threshold:
        return "s-hi"
    if score >= threshold * 0.55:
        return "s-mid"
    return "s-ok"


def render(report, cfg, me: int | None = None, title: str = "") -> str:
    threshold = cfg.scoring.review_threshold
    players = report.sorted_players()
    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    heading = title or f"cs-cheat-radar - {report.source}"

    parts = [
        "<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{_esc(heading)}</title><style>{CSS}</style></head><body>",
        "<div class=\"wrap\">",
        f"<h1>{_esc(heading)}</h1>",
        f"<div class=\"sub\">jogo: {_esc(report.game)} &middot; mapa: "
        f"{_esc(report.map_name)} &middot; tick: {report.tickrate:g} &middot; "
        f"gerado em {generated}</div>",
    ]

    if report.skipped_signals:
        faltou = sorted({c for s in report.skipped_signals for c in s["faltou"]})
        omitidos = ", ".join(SIGNAL_LABELS.get(s["sinal"], s["sinal"])
                             for s in report.skipped_signals)
        parts.append(
            f"<div class=\"warn\"><b>Fonte limitada.</b> Nao ha "
            f"{_esc(', '.join(faltou))} neste dado, entao estes sinais nao "
            f"rodaram: {_esc(omitidos)}. Score baixo aqui <b>nao</b> quer "
            f"dizer jogador limpo.</div>"
        )

    parts.append(
        "<table><thead><tr><th>jogador</th><th>time</th>"
        "<th class=\"num\">K/D</th><th class=\"num\">HS%</th>"
        "<th class=\"num\">score</th><th>sinais</th></tr></thead><tbody>"
    )

    for i, p in enumerate(players):
        mark = " &#9733;" if me and p.steamid == me else ""
        chips = "".join(
            f"<span class=\"chip{' on' if f.startswith(('forte', 'REVISAR')) else ''}\">"
            f"{_esc(f)}</span>" for f in p.flags
        )
        parts.append(
            f"<tr class=\"p\" data-i=\"{i}\">"
            f"<td><b>{_esc(p.name)}</b>{mark}<br>"
            f"<code>{p.steamid}</code></td>"
            f"<td>{_esc(TEAM_LABELS.get(p.team, p.team or '?'))}</td>"
            f"<td class=\"num\">{p.kills}/{p.deaths}</td>"
            f"<td class=\"num\">{p.hs_pct:.0f}%</td>"
            f"<td class=\"num score {_score_class(p.score, threshold)}\">"
            f"{p.score:.1f}</td>"
            f"<td>{chips or '&ndash;'}</td></tr>"
        )
        parts.append(f"<tr class=\"det\" id=\"d-{i}\"><td colspan=\"6\">")
        for name, sig in sorted(p.signals.items(), key=lambda kv: -kv[1].value):
            pct = int(round(sig.value * 100))
            suffix = "" if sig.confident else " <i>(amostra pequena)</i>"
            parts.append(
                f"<div class=\"sig\"><span class=\"nm\">"
                f"{_esc(SIGNAL_LABELS.get(name, name))}</span>"
                f"<span class=\"bar\"><i style=\"width:{pct}%\"></i></span>"
                f"<span>{sig.value:.2f}{suffix}</span></div>"
            )
        evidence = p.top_evidence(12)
        if evidence:
            parts.append("<div class=\"ev\">")
            for ev in evidence:
                alvo = f" &rarr; {_esc(ev.target)}" if ev.target else ""
                parts.append(
                    f"<div>round <b>{ev.round_num}</b> &middot; tick "
                    f"<b>{ev.tick}</b> &middot; {_esc(ev.kind)}{alvo}<br>"
                    f"{_esc(ev.detail)}</div>"
                )
            parts.append("</div>")
        parts.append("</td></tr>")

    parts.append("</tbody></table>")
    parts.append(
        "<div class=\"foot\">Clique numa linha para ver os sinais e as "
        "evidencias. Cada evidencia traz round e tick para voce conferir na "
        "demo.<br><b>Isto e uma fila de priorizacao, nao um veredito.</b> "
        "Falso positivo em jogador bom acontece. Assista antes de reportar."
        "</div></div>"
    )
    parts.append(f"<script>{JS}</script></body></html>")
    return "\n".join(parts)


def write(report, cfg, path, me: int | None = None, title: str = "") -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report, cfg, me=me, title=title), encoding="utf-8")
    return out


def write_index(reports: list, cfg, path) -> Path:
    """Uma pagina com varias partidas, para revisar um lote."""
    rows = []
    for rep in reports:
        top = rep.sorted_players()[:3]
        resumo = ", ".join(f"{_esc(p.name)} ({p.score:.0f})" for p in top)
        rows.append(
            f"<tr><td><b>{_esc(rep.source)}</b><br>"
            f"<code>{_esc(rep.game)}</code> {_esc(rep.map_name)}</td>"
            f"<td>{resumo}</td></tr>"
        )
    body = (
        "<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>cs-cheat-radar - lote</title><style>{CSS}</style></head><body>"
        "<div class=\"wrap\"><h1>Lote analisado</h1>"
        f"<div class=\"sub\">{len(reports)} partida(s) &middot; "
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}</div>"
        "<table><thead><tr><th>fonte</th><th>maiores scores</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></body></html>"
    )
    out = Path(path)
    out.write_text(body, encoding="utf-8")
    return out
