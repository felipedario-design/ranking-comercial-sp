#!/usr/bin/env python3
"""Atualiza index.html com os números mais recentes do dashboard CAUP (São Paulo).

Roda dentro do GitHub Actions (hospedado no próprio GitHub, sem depender de
nenhum serviço externo do Claude): busca a API pública do dashboard, monta o
novo estado do ranking e substitui o bloco RANKING_DATA dentro do index.html.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

API_BASE = "https://dashcaup.v4ferrazpiai.com.br/api/sp-dash"
HTML_PATH = "index.html"

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

# São Paulo é UTC-3 o ano todo (sem horário de verão desde 2019).
SP_TZ = timezone(timedelta(hours=-3))


def fetch_dashboard(ano: int, mes: int) -> dict:
    url = f"{API_BASE}?tipo=tudo&ano={ano}&mes={mes}"
    req = urllib.request.Request(url, headers={"User-Agent": "caup-ranking-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_state(data: dict, ano: int, mes: int) -> dict:
    sdr = [
        {
            "nome": item.get("nome", "—"),
            "agendamentos": item.get("agendRealizado", 0) or 0,
            "show": item.get("showRealizado", 0) or 0,
        }
        for item in data.get("ranking_sdr", [])
    ]
    closer = [
        {
            "nome": item.get("nome", "—"),
            "show": item.get("showRealizado", 0) or 0,
            "vendaValor": item.get("vendaFaturamento", 0) or 0,
            "vendaQtd": item.get("vendaRealizado", 0) or 0,
        }
        for item in data.get("ranking_closer", [])
    ]
    updated_at = (data.get("funil") or {}).get("AtualizadoEm") or datetime.now(timezone.utc).isoformat()
    period = f"{MESES_PT[mes]}/{ano} · mês inteiro"
    return {"updatedAt": updated_at, "period": period, "sdr": sdr, "closer": closer}


def replace_state(html: str, state: dict) -> str:
    marker_re = re.compile(
        r"(// RANKING_DATA:START.*?var state = )\{.*?\};(\s*// RANKING_DATA:END)",
        re.DOTALL,
    )
    new_obj = json.dumps(state, ensure_ascii=False, indent=4)
    if not marker_re.search(html):
        raise RuntimeError("Marcadores RANKING_DATA:START/END não encontrados em index.html")
    return marker_re.sub(lambda m: m.group(1) + new_obj + ";" + m.group(2), html, count=1)


def main() -> int:
    now_sp = datetime.now(SP_TZ)
    ano, mes = now_sp.year, now_sp.month

    try:
        data = fetch_dashboard(ano, mes)
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO ao buscar dashboard: {exc}", file=sys.stderr)
        return 1

    state = build_state(data, ano, mes)

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    try:
        new_html = replace_state(html, state)
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    if new_html != html:
        with open(HTML_PATH, "w", encoding="utf-8") as f:
            f.write(new_html)
        print("index.html atualizado.")
    else:
        print("Sem mudanças no ranking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
