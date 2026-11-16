"""Todos os cálculos financeiros (sem nada de interface).

Aqui moram as fórmulas do sistema:

    Saldo atual         = saldo inicial + entradas - saídas - poupança
    Total de entradas   = soma das movimentações do tipo "entrada"
    Total de saídas     = soma das movimentações do tipo "saida"
    Total economizado   = soma das movimentações do tipo "poupanca"
    % de economia       = total economizado / total de entradas x 100

Poupança é um tipo PRÓPRIO: nunca entra em "saídas". Assim o mesmo dinheiro
não é contado duas vezes (uma como gasto e outra como economia).

Os cálculos são feitos em CENTAVOS inteiros e só convertidos para reais no final.
"""
import pandas as pd

from core import config

COLUNAS = [
    "id", "data", "descricao", "valor_centavos", "tipo", "categoria",
    "forma", "fonte", "meta_id", "observacoes", "criado_em",
]
ENTRADA, SAIDA, POUPANCA = config.TIPO_ENTRADA, config.TIPO_SAIDA, config.TIPO_POUPANCA


# --------------------------------------------------------------- DataFrame
def para_dataframe(registros) -> pd.DataFrame:
    """Transforma a lista vinda do banco em uma tabela do pandas."""
    df = pd.DataFrame(list(registros), columns=COLUNAS)
    df["valor_centavos"] = df["valor_centavos"].astype("int64")
    df["data"] = pd.to_datetime(df["data"])
    df["valor"] = df["valor_centavos"] / 100
    df["mes"] = df["data"].dt.to_period("M")
    return df


def filtrar_periodo(df, ano=None, mes=None):
    """Mantém só as linhas do ano (e mês) escolhidos. None = sem filtro."""
    if ano is not None:
        df = df[df["data"].dt.year == ano]
    if mes is not None:
        df = df[df["data"].dt.month == mes]
    return df


def fim_do_periodo(ano: int, mes=None) -> pd.Timestamp:
    """Último dia do mês (ou do ano, se mes=None)."""
    if mes is None:
        return pd.Timestamp(ano, 12, 31)
    return pd.Timestamp(ano, mes, 1) + pd.offsets.MonthEnd(0)


def anos_disponiveis(df, hoje) -> list:
    """Anos com movimentações + o ano atual, do mais novo para o mais antigo."""
    anos = set(df["data"].dt.year.unique().tolist()) | {hoje.year}
    return sorted((int(a) for a in anos), reverse=True)


def _somar(df, tipo) -> int:
    return int(df.loc[df["tipo"] == tipo, "valor_centavos"].sum())


# ------------------------------------------------------------------ resumo
def resumo(df, saldo_inicial_centavos=0, ate=None) -> dict:
    """Totais e saldo. Se `ate` for informado, considera só até essa data."""
    base = df if ate is None else df[df["data"] <= ate]
    entradas = _somar(base, ENTRADA)
    saidas = _somar(base, SAIDA)
    poupado = _somar(base, POUPANCA)
    saldo = saldo_inicial_centavos + entradas - saidas - poupado
    return {
        "saldo": saldo / 100,
        "entradas": entradas / 100,
        "saidas": saidas / 100,
        "poupado": poupado / 100,
        "percentual_economia": (poupado / entradas * 100) if entradas > 0 else 0.0,
    }


def resumo_mensal(df, saldo_inicial_centavos=0, hoje=None) -> pd.DataFrame:
    """Uma linha por mês (inclusive meses sem movimento).

    Colunas: entradas, saidas, poupanca, resultado (= entradas - saídas - poupança),
    saldo_final (acumulado até o fim do mês) e pct_economia. Valores em reais.
    """
    colunas = ["entradas", "saidas", "poupanca", "resultado", "saldo_final", "pct_economia"]
    if df.empty:
        return pd.DataFrame(columns=colunas, index=pd.PeriodIndex([], freq="M"), dtype="float64")

    soma = df.groupby(["mes", "tipo"])["valor_centavos"].sum().unstack(fill_value=0)
    for tipo in (ENTRADA, SAIDA, POUPANCA):
        if tipo not in soma.columns:
            soma[tipo] = 0

    hoje_mes = pd.Period(pd.Timestamp(hoje if hoje is not None else pd.Timestamp.today()), "M")
    meses = pd.period_range(soma.index.min(), max(soma.index.max(), hoje_mes), freq="M")
    soma = soma.reindex(meses, fill_value=0)

    centavos = pd.DataFrame({
        "entradas": soma[ENTRADA],
        "saidas": soma[SAIDA],
        "poupanca": soma[POUPANCA],
    })
    centavos["resultado"] = centavos["entradas"] - centavos["saidas"] - centavos["poupanca"]
    centavos["saldo_final"] = saldo_inicial_centavos + centavos["resultado"].cumsum()

    mensal = centavos / 100
    mensal["pct_economia"] = [
        (p / e * 100) if e > 0 else 0.0
        for p, e in zip(centavos["poupanca"], centavos["entradas"])
    ]
    return mensal[colunas]


# ------------------------------------------------------- análises por grupo
def gastos_por_categoria(df) -> pd.DataFrame:
    """Total gasto em cada categoria (maior primeiro) e % sobre o total gasto."""
    saidas = df[df["tipo"] == SAIDA]
    serie = saidas.groupby("categoria")["valor_centavos"].sum().sort_values(ascending=False)
    total = int(serie.sum())
    return pd.DataFrame({
        "categoria": serie.index.tolist(),
        "valor": (serie.values / 100).tolist(),
        "percentual": [(v / total * 100) if total > 0 else 0.0 for v in serie.values],
    })


def categoria_maior_gasto(df):
    """(categoria, valor em reais) com maior gasto, ou None se não houve gastos."""
    gastos = gastos_por_categoria(df)
    if gastos.empty:
        return None
    return gastos.iloc[0]["categoria"], float(gastos.iloc[0]["valor"])


def entradas_por_fonte(df) -> pd.DataFrame:
    """Total recebido por fonte de renda."""
    entradas = df[df["tipo"] == ENTRADA].copy()
    entradas["fonte"] = entradas["fonte"].fillna("Sem fonte informada")
    serie = entradas.groupby("fonte")["valor_centavos"].sum().sort_values(ascending=False)
    return pd.DataFrame({"fonte": serie.index.tolist(), "valor": (serie.values / 100).tolist()})


def comparar_com_anterior(mensal, periodo):
    """Compara um mês com o mês anterior. None se o mês anterior não existe."""
    anterior = periodo - 1
    if periodo not in mensal.index or anterior not in mensal.index:
        return None
    comparacao = {}
    for coluna in ("entradas", "saidas", "poupanca"):
        atual = float(mensal.loc[periodo, coluna])
        antes = float(mensal.loc[anterior, coluna])
        comparacao[coluna] = {
            "atual": atual,
            "anterior": antes,
            "diferenca": atual - antes,
            "percentual": ((atual - antes) / antes * 100) if antes != 0 else None,
        }
    return comparacao


def meses_maior_economia(mensal, quantidade=3) -> pd.DataFrame:
    """Os meses em que mais dinheiro foi guardado (só considera meses > 0)."""
    guardou = mensal[mensal["poupanca"] > 0]
    return guardou.sort_values("poupanca", ascending=False).head(quantidade)


# ------------------------------------------------------------------- metas
def progresso_metas(metas, df) -> list:
    """Para cada meta: quanto já foi guardado, quanto falta e o percentual."""
    poupancas = df[df["tipo"] == POUPANCA]
    resultado = []
    for meta in metas:
        guardado = int(poupancas.loc[poupancas["meta_id"] == meta["id"], "valor_centavos"].sum())
        alvo = meta["valor_alvo_centavos"]
        resultado.append({
            "id": meta["id"],
            "nome": meta["nome"],
            "alvo": alvo / 100,
            "guardado": guardado / 100,
            "falta": max(alvo - guardado, 0) / 100,
            "percentual": guardado / alvo * 100 if alvo > 0 else 0.0,
        })
    return resultado


# ---------------------------------------------------- limite e planejamento
def status_limite(gasto: float, limite: float) -> dict:
    """Situação do gasto frente ao limite: sem_limite, ok, alerta ou estourado."""
    if limite <= 0:
        return {"nivel": "sem_limite", "percentual": 0.0, "restante": 0.0}
    percentual = gasto / limite * 100
    if percentual >= 100:
        nivel = "estourado"
    elif percentual >= config.PERCENTUAL_ALERTA_LIMITE:
        nivel = "alerta"
    else:
        nivel = "ok"
    return {"nivel": nivel, "percentual": percentual, "restante": limite - gasto}


def planejado_vs_real(df_periodo, planejado_centavos: dict, meses=1) -> pd.DataFrame:
    """Compara o gasto planejado com o gasto real de cada categoria.

    `meses` multiplica o planejado (use 12 para um ano inteiro).
    """
    real = df_periodo[df_periodo["tipo"] == SAIDA].groupby("categoria")["valor_centavos"].sum()
    categorias = sorted(set(planejado_centavos) | set(real.index))
    linhas = []
    for categoria in categorias:
        plano = planejado_centavos.get(categoria, 0) * meses
        gasto = int(real.get(categoria, 0))
        linhas.append({
            "categoria": categoria,
            "planejado": plano / 100,
            "real": gasto / 100,
            "diferenca": (plano - gasto) / 100,   # positivo = sobrou, negativo = estourou
            "percentual_usado": (gasto / plano * 100) if plano > 0 else None,
        })
    return pd.DataFrame(linhas, columns=["categoria", "planejado", "real", "diferenca", "percentual_usado"])


# ------------------------------------------------------- histórico / filtros
ORDENACOES = {
    "Data (mais recente primeiro)": ("data", False),
    "Data (mais antiga primeiro)": ("data", True),
    "Valor (maior primeiro)": ("valor_centavos", False),
    "Valor (menor primeiro)": ("valor_centavos", True),
}


def filtrar_movimentacoes(df, texto="", tipos=None, categorias=None,
                          inicio=None, fim=None,
                          ordem="Data (mais recente primeiro)") -> pd.DataFrame:
    """Pesquisa por descrição, filtra por tipo/categoria/período e ordena."""
    resultado = df
    texto = (texto or "").strip()
    if texto:
        resultado = resultado[resultado["descricao"].str.contains(texto, case=False, regex=False, na=False)]
    if tipos:
        resultado = resultado[resultado["tipo"].isin(tipos)]
    if categorias:
        resultado = resultado[resultado["categoria"].isin(categorias)]
    if inicio is not None:
        resultado = resultado[resultado["data"] >= pd.Timestamp(inicio)]
    if fim is not None:
        resultado = resultado[resultado["data"] <= pd.Timestamp(fim)]
    coluna, crescente = ORDENACOES[ordem]
    # "id" desempata: lançamentos do mesmo dia aparecem na ordem em que foram criados
    return resultado.sort_values([coluna, "id"], ascending=[crescente, crescente])
