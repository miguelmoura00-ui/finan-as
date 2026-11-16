"""Minhas Finanças - ponto de entrada do programa.

Para abrir:   streamlit run app.py
"""
from types import SimpleNamespace

import streamlit as st

# set_page_config precisa ser o PRIMEIRO comando do Streamlit
st.set_page_config(page_title="Minhas Finanças", page_icon="💰", layout="wide")

from core import calculos, database as db  # noqa: E402
from ui import (  # noqa: E402
    componentes, estilos, pagina_configuracoes, pagina_dashboard, pagina_despesas,
    pagina_entradas, pagina_historico, pagina_poupanca, pagina_relatorios,
)


def carregar_contexto() -> SimpleNamespace:
    """Lê tudo o que as abas precisam, uma única vez por atualização da tela."""
    df = calculos.para_dataframe(db.listar_movimentacoes())
    ano, mes = componentes.seletor_periodo(df)
    saldo_inicial = db.obter_saldo_inicial_centavos()
    return SimpleNamespace(
        df=df, ano=ano, mes=mes,
        saldo_inicial=saldo_inicial,                       # em centavos
        limite=db.obter_limite_mensal_centavos(),          # em centavos
        metas=db.listar_metas(),
        planejado=db.listar_planejado(),                   # {categoria: centavos}
        mensal=calculos.resumo_mensal(df, saldo_inicial),  # uma linha por mês
    )


def main() -> None:
    estilos.aplicar()
    db.inicializar_banco()
    componentes.mostrar_aviso_pendente()

    st.title("💰 Minhas Finanças")
    ctx = carregar_contexto()

    abas = st.tabs([
        "📊 Dashboard", "💵 Entradas", "💸 Despesas", "🐷 Poupança",
        "📈 Gráficos e relatórios", "🗂️ Histórico", "⚙️ Configurações",
    ])
    paginas = [
        pagina_dashboard, pagina_entradas, pagina_despesas, pagina_poupanca,
        pagina_relatorios, pagina_historico, pagina_configuracoes,
    ]
    for aba, pagina in zip(abas, paginas):
        with aba:
            pagina.renderizar(ctx)


main()
