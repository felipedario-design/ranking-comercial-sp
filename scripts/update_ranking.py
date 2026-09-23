#!/usr/bin/env python3
"""Atualiza index.html com os números mais recentes do dashboard CAUP (São Paulo).

Roda dentro do GitHub Actions (hospedado no próprio GitHub, sem depender de
nenhum serviço externo do Claude): busca a API pública do dashboard, monta o
novo estado do ranking e substitui o bloco RANKING_DATA dentro do index.html.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

API_BASE = "https://dashcaup.v4ferrazpiai.com.br/api/sp-dash"
REFRESH_TRIGGER_URL = "https://dashcaup.v4ferrazpiai.com.br/api/refresh?escopo=sao-paulo"
REFRESH_STATUS_URL = "https://dashcaup.v4ferrazpiai.com.br/api/refresh"
EVENTO_API_BASE = "https://portal-comercial-delta.vercel.app/api/evento-vendedores"
TUDO_API_BASE = "https://portal-comercial-delta.vercel.app/api/tudo"
HTML_PATH = "index.html"

# Nome fixo do Key Account de São Paulo. O portal comercial só expõe o total
# do canal "Key Account" agregado (sem quebra por vendedor), mas hoje só a
# Cristine Rocha atua nesse funil em São Paulo — os negócios que antes
# apareciam como dela em "Closer" foram reclassificados para esse canal (o
# valor bateu exatamente com o que sumiu do ranking_closer). Se mais alguém
# entrar nesse funil, este nome vira uma lista e a lógica precisa mudar.
KEY_ACCOUNT_NOME = "Cristine Rocha"

MESES_PT = [
    "", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]

# São Paulo é UTC-3 o ano todo (sem horário de verão desde 2019).
SP_TZ = timezone(timedelta(hours=-3))


def trigger_refresh(max_wait_seconds: int = 90, poll_interval: int = 4) -> None:
    """Manda o dashboard recalcular os números antes de lermos eles.

    Descoberto observando o botão "Atualizar" do próprio dashboard: sem isso,
    a API /api/sp-dash devolve um retrato (snapshot) que só é recalculado
    quando alguém aperta esse botão manualmente — o que fazia nosso robô
    ficar sempre "atrasado" em relação ao que um humano via na tela. Chamamos
    o mesmo endpoint que o botão chama e esperamos o recálculo terminar.
    """
    req = urllib.request.Request(
        REFRESH_TRIGGER_URL, method="POST", headers={"User-Agent": "caup-ranking-bot"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        json.loads(resp.read().decode("utf-8"))

    waited = 0
    while waited < max_wait_seconds:
        time.sleep(poll_interval)
        waited += poll_interval
        status_req = urllib.request.Request(
            REFRESH_STATUS_URL, headers={"User-Agent": "caup-ranking-bot"}
        )
        with urllib.request.urlopen(status_req, timeout=30) as resp:
            status = json.loads(resp.read().decode("utf-8"))
        if not status.get("rodando"):
            return
    print("AVISO: recálculo do dashboard ainda em andamento após o tempo limite; seguindo com os dados disponíveis.", file=sys.stderr)


def fetch_dashboard(ano: int, mes: int) -> dict:
    url = f"{API_BASE}?tipo=tudo&ano={ano}&mes={mes}"
    req = urllib.request.Request(url, headers={"User-Agent": "caup-ranking-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_evento(ano: int, mes: int) -> list:
    """Busca o desempenho por vendedor do time de Eventos (Portal Comercial).

    Este endpoint não exige login (testado sem cookies/sessão), então não
    precisamos de nenhuma credencial guardada no GitHub Actions.
    """
    url = f"{EVENTO_API_BASE}?ano={ano}&mes={mes}"
    req = urllib.request.Request(url, headers={"User-Agent": "caup-ranking-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [
        {
            "nome": item.get("nome", "—"),
            "vendido": item.get("vendido", 0) or 0,
            "negocios": item.get("negocios", 0) or 0,
        }
        for item in data.get("vendedores", [])
    ]


def fetch_key_account(ano: int, mes: int) -> list:
    """Busca o faturamento do canal Key Account (Portal Comercial).

    Não existe um endpoint "por vendedor" para Key Account — só o total do
    canal, dentro de /api/tudo -> visaoGeralPorCanal.keyAccount. Como hoje só
    a Cristine Rocha atua nesse funil em São Paulo, atribuímos o total a ela.
    """
    url = f"{TUDO_API_BASE}?ano={ano}&mes={mes}"
    req = urllib.request.Request(url, headers={"User-Agent": "caup-ranking-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    canal = (data.get("visaoGeralPorCanal") or {}).get("keyAccount") or {}
    vendido = canal.get("realizado", 0) or 0
    negocios = canal.get("vendaQtd", 0) or 0
    if not vendido and not negocios:
        return []
    return [{"nome": KEY_ACCOUNT_NOME, "vendido": vendido, "negocios": negocios}]


def extract_sdr(data: dict) -> list:
    return [
        {
            "nome": item.get("nome", "—"),
            "agendamentos": item.get("agendRealizado", 0) or 0,
            "show": item.get("showRealizado", 0) or 0,
        }
        for item in data.get("ranking_sdr", [])
    ]


def extract_closer(data: dict) -> list:
    return [
        {
            "nome": item.get("nome", "—"),
            "show": item.get("showRealizado", 0) or 0,
            "vendaValor": item.get("vendaFaturamento", 0) or 0,
            "vendaQtd": item.get("vendaRealizado", 0) or 0,
        }
        for item in data.get("ranking_closer", [])
    ]


def dedupe_evento_closer(closer: list, evento: list) -> list:
    """Evita contar duas vezes o faturamento de quem atua nos dois funis.

    Alguns vendedores (ex.: Cristine Rocha) aparecem tanto em Closers quanto
    em Eventos. Quando o negócio é ganho como Closer, o valor já entra no
    faturamento de Closer — então subtraímos do total de Eventos o que já
    foi contabilizado em Closer para o mesmo vendedor, pra não duplicar.
    Só é aplicado sobre um retrato de Eventos recém-buscado da API (nunca
    sobre o fallback do último HTML, que já sai líquido).
    """
    closer_by_nome = {c["nome"]: c for c in closer}
    result = []
    for item in evento:
        nome = item.get("nome", "—")
        vendido = item.get("vendido", 0) or 0
        negocios = item.get("negocios", 0) or 0
        c = closer_by_nome.get(nome)
        if c:
            vendido = max(0, vendido - (c.get("vendaValor", 0) or 0))
            negocios = max(0, negocios - (c.get("vendaQtd", 0) or 0))
        result.append({"nome": nome, "vendido": vendido, "negocios": negocios})
    return result


def build_state(data: dict, ano: int, mes: int, sdr: list, closer: list, evento: list, keyaccount: list) -> dict:
    updated_at = (data.get("funil") or {}).get("AtualizadoEm") or datetime.now(timezone.utc).isoformat()
    period = f"{MESES_PT[mes]}/{ano} · mês inteiro"
    return {
        "updatedAt": updated_at,
        "period": period,
        "sdr": sdr,
        "closer": closer,
        "evento": evento,
        "keyaccount": keyaccount,
    }


def extract_current_state(html: str) -> dict:
    """Lê o bloco RANKING_DATA atual do index.html (usado como fallback)."""
    marker_re = re.compile(
        r"// RANKING_DATA:START.*?var state = (\{.*?\});\s*// RANKING_DATA:END",
        re.DOTALL,
    )
    m = marker_re.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


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
        trigger_refresh()
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: não consegui disparar o recálculo do dashboard, lendo o último retrato disponível: {exc}", file=sys.stderr)

    try:
        data = fetch_dashboard(ano, mes)
    except Exception as exc:  # noqa: BLE001
        print(f"ERRO ao buscar dashboard: {exc}", file=sys.stderr)
        return 1

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    sdr = extract_sdr(data)
    closer = extract_closer(data)

    try:
        evento_bruto = fetch_evento(ano, mes)
        evento = dedupe_evento_closer(closer, evento_bruto)
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: falha ao buscar dados de Eventos, mantendo os últimos valores: {exc}", file=sys.stderr)
        evento = extract_current_state(html).get("evento", [])

    try:
        keyaccount = fetch_key_account(ano, mes)
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: falha ao buscar dados de Key Account, mantendo os últimos valores: {exc}", file=sys.stderr)
        keyaccount = extract_current_state(html).get("keyaccount", [])

    state = build_state(data, ano, mes, sdr, closer, evento, keyaccount)

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
