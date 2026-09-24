#!/usr/bin/env python3
"""Atualiza index.html com os números mais recentes do dashboard CAUP (São Paulo).

Roda dentro do GitHub Actions (hospedado no próprio GitHub, sem depender de
nenhum serviço externo do Claude): busca a API pública do Portal Comercial,
monta o novo estado do ranking e substitui o bloco RANKING_DATA dentro do
index.html.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

TUDO_API_BASE = "https://portal-comercial-delta.vercel.app/api/tudo"
EVENTO_API_BASE = "https://portal-comercial-delta.vercel.app/api/evento-vendedores"
HTML_PATH = "index.html"

# O Portal Comercial (portal-comercial-delta) é único para a empresa toda —
# os rankings de Closer e SDR que ele devolve misturam gente de Fortaleza,
# Alphaville e São Paulo no mesmo array, sem nenhum campo de região confiável
# (o campo "unidade" que aparece em alguns lugares é só um rótulo legado do
# squad, não reflete a região real). O time de São Paulo hoje é só este:
# Closers: Sarah Limas, Geovane Paschoal, Clayton Martins (squad "resultado")
# SDRs: Raissa Borges, Guilherme Sousa (squad "resultado")
# Se o time mudar, ajuste as listas abaixo.
SP_CLOSER_NOMES = ["Sarah Limas", "Geovane Paschoal", "Clayton Martins"]
SP_SDR_NOMES = ["Raissa Borges", "Guilherme Sousa"]

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


def fetch_tudo(ano: int, mes: int) -> dict:
    """Busca o retrato geral da empresa (Portal Comercial).

    Esse endpoint já vem atualizado sozinho (sem precisar de um botão
    "Atualizar" como a fonte antiga) — o campo atualizadoEm bate com o
    horário real do sistema.
    """
    url = f"{TUDO_API_BASE}?ano={ano}&mes={mes}"
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


def extract_closer(data: dict) -> list:
    """Filtra o ranking_closer (empresa toda) para só os closers de São Paulo."""
    by_nome = {item.get("nome"): item for item in data.get("ranking_closer", [])}
    result = []
    for nome in SP_CLOSER_NOMES:
        item = by_nome.get(nome) or {}
        result.append({
            "nome": nome,
            "show": item.get("showRealizado", 0) or 0,
            "vendaValor": item.get("vendaFaturamento", 0) or 0,
            "vendaQtd": item.get("vendaRealizado", 0) or 0,
        })
    return result


def extract_sdr(data: dict) -> list:
    """Filtra o ranking_sdr (empresa toda) para só os SDRs de São Paulo."""
    by_nome = {item.get("nome"): item for item in data.get("ranking_sdr", [])}
    result = []
    for nome in SP_SDR_NOMES:
        item = by_nome.get(nome) or {}
        result.append({
            "nome": nome,
            "agendamentos": item.get("agendRealizado", 0) or 0,
            "show": item.get("showRealizado", 0) or 0,
        })
    return result


def extract_key_account(data: dict) -> list:
    """Extrai o faturamento do canal Key Account (Portal Comercial).

    Não existe um endpoint "por vendedor" para Key Account — só o total do
    canal, dentro de /api/tudo -> visaoGeralPorCanal.keyAccount. Como hoje só
    a Cristine Rocha atua nesse funil em São Paulo, atribuímos o total a ela.
    """
    canal = (data.get("visaoGeralPorCanal") or {}).get("keyAccount") or {}
    vendido = canal.get("realizado", 0) or 0
    negocios = canal.get("vendaQtd", 0) or 0
    if not vendido and not negocios:
        return []
    return [{"nome": KEY_ACCOUNT_NOME, "vendido": vendido, "negocios": negocios}]


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

    with open(HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    current_state = extract_current_state(html)

    try:
        tudo = fetch_tudo(ano, mes)
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: falha ao buscar dados gerais (Closer/SDR/Key Account), mantendo os últimos valores: {exc}", file=sys.stderr)
        tudo = None

    if tudo is not None:
        sdr = extract_sdr(tudo)
        closer = extract_closer(tudo)
        keyaccount = extract_key_account(tudo)
        updated_at = tudo.get("atualizadoEm") or datetime.now(timezone.utc).isoformat()
    else:
        sdr = current_state.get("sdr", [])
        closer = current_state.get("closer", [])
        keyaccount = current_state.get("keyaccount", [])
        updated_at = current_state.get("updatedAt") or datetime.now(timezone.utc).isoformat()

    try:
        evento_bruto = fetch_evento(ano, mes)
        evento = dedupe_evento_closer(closer, evento_bruto)
    except Exception as exc:  # noqa: BLE001
        print(f"AVISO: falha ao buscar dados de Eventos, mantendo os últimos valores: {exc}", file=sys.stderr)
        evento = current_state.get("evento", [])

    period = f"{MESES_PT[mes]}/{ano} · mês inteiro"
    state = {
        "updatedAt": updated_at,
        "period": period,
        "sdr": sdr,
        "closer": closer,
        "evento": evento,
        "keyaccount": keyaccount,
    }

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
