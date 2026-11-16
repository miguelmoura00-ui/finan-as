"""Camada de banco de dados (SQLite).

Tudo o que lê ou grava no arquivo dados/financas.db passa por aqui.
Regras importantes:
  * Dinheiro é guardado em CENTAVOS (número inteiro) -> sem erro de arredondamento.
  * Entradas e saídas nunca aceitam valor negativo ou zero.
  * Poupança aceita depósito (positivo) e resgate (negativo).
  * Toda gravação é validada antes de chegar ao banco.
"""
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from core import config
from core.utils import brl, reais_para_centavos

TIPOS_VALIDOS = (config.TIPO_ENTRADA, config.TIPO_SAIDA, config.TIPO_POUPANCA)
LIMITE_CENTAVOS = 10_000_000_000  # R$ 100 milhões: trava digitação absurda


class ErroValidacao(ValueError):
    """Erro com mensagem amigável, pronta para ser mostrada ao usuário."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS metas (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nome                TEXT NOT NULL UNIQUE COLLATE NOCASE,
    valor_alvo_centavos INTEGER NOT NULL CHECK (valor_alvo_centavos > 0),
    criado_em           TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS movimentacoes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    data           TEXT NOT NULL,                 -- AAAA-MM-DD
    descricao      TEXT NOT NULL,
    valor_centavos INTEGER NOT NULL,
    tipo           TEXT NOT NULL CHECK (tipo IN ('entrada', 'saida', 'poupanca')),
    categoria      TEXT NOT NULL,
    forma          TEXT,                          -- forma de pagamento / tipo de recebimento
    fonte          TEXT,                          -- fonte de renda (só entradas)
    meta_id        INTEGER REFERENCES metas(id) ON DELETE SET NULL,
    observacoes    TEXT,
    criado_em      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    CHECK (
        (tipo = 'poupanca' AND valor_centavos <> 0) OR
        (tipo <> 'poupanca' AND valor_centavos > 0)
    )
);
CREATE INDEX IF NOT EXISTS idx_mov_data ON movimentacoes (data);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS planejado (
    categoria      TEXT PRIMARY KEY,
    valor_centavos INTEGER NOT NULL CHECK (valor_centavos > 0)
);

CREATE TABLE IF NOT EXISTS fontes_renda (
    nome TEXT PRIMARY KEY COLLATE NOCASE
);
"""


# ------------------------------------------------------------------ conexão
@contextmanager
def conexao():
    """Abre o banco, confirma (commit) no final ou desfaz (rollback) se der erro."""
    conn = sqlite3.connect(config.CAMINHO_BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def inicializar_banco() -> None:
    """Cria a pasta e as tabelas, se ainda não existirem."""
    Path(config.CAMINHO_BANCO).parent.mkdir(parents=True, exist_ok=True)
    with conexao() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- validação
def _normalizar_data(valor) -> str:
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, str):
        try:
            valor = date.fromisoformat(valor)
        except ValueError as erro:
            raise ErroValidacao("Data inválida.") from erro
    if not isinstance(valor, date):
        raise ErroValidacao("Data inválida.")
    if not 2000 <= valor.year <= 2100:
        raise ErroValidacao("Use uma data entre os anos 2000 e 2100.")
    return valor.isoformat()


def _texto_ou_none(valor, limite: int, nome_campo: str):
    texto = " ".join((valor or "").split())
    if len(texto) > limite:
        raise ErroValidacao(f"{nome_campo} pode ter no máximo {limite} caracteres.")
    return texto or None


def validar_movimentacao(*, tipo, descricao, valor, data, categoria=None,
                         forma=None, fonte=None, meta_id=None,
                         observacoes=None) -> dict:
    """Confere todos os campos e devolve um dicionário limpo, pronto para gravar.

    `valor` é em reais. Para poupança, valor negativo significa RESGATE.
    Levanta ErroValidacao com mensagem amigável se algo estiver errado.
    """
    if tipo not in TIPOS_VALIDOS:
        raise ErroValidacao("Tipo de movimentação inválido.")

    descricao = " ".join((descricao or "").split())
    if not descricao:
        raise ErroValidacao("Informe uma descrição.")
    if len(descricao) > 120:
        raise ErroValidacao("A descrição pode ter no máximo 120 caracteres.")

    try:
        centavos = reais_para_centavos(valor)
    except ValueError as erro:
        raise ErroValidacao("Valor inválido.") from erro

    if tipo == config.TIPO_POUPANCA:
        if centavos == 0:
            raise ErroValidacao("O valor não pode ser zero.")
    elif centavos <= 0:
        raise ErroValidacao(
            "O valor deve ser maior que zero. "
            "Entradas e despesas não aceitam valores negativos."
        )
    if abs(centavos) > LIMITE_CENTAVOS:
        raise ErroValidacao("Valor alto demais. Confira se digitou corretamente.")

    if tipo == config.TIPO_POUPANCA:
        categoria = config.CATEGORIA_POUPANCA
    else:
        categoria = (categoria or "").strip()
        if not categoria:
            raise ErroValidacao("Escolha uma categoria.")

    return {
        "tipo": tipo,
        "descricao": descricao,
        "valor_centavos": centavos,
        "data": _normalizar_data(data),
        "categoria": categoria,
        # "forma" só existe em entradas (tipo de recebimento) e saídas (pagamento)
        "forma": None if tipo == config.TIPO_POUPANCA
        else _texto_ou_none(forma, 40, "A forma de pagamento"),
        "fonte": _texto_ou_none(fonte, 60, "A fonte de renda")
        if tipo == config.TIPO_ENTRADA else None,
        "meta_id": meta_id if tipo == config.TIPO_POUPANCA else None,
        "observacoes": _texto_ou_none(observacoes, 300, "A observação"),
    }


# -------------------------------------------------- regras da poupança
def _total_poupanca(conn, meta_id=None, ignorar_id=None) -> int:
    sql = "SELECT COALESCE(SUM(valor_centavos), 0) FROM movimentacoes WHERE tipo = 'poupanca'"
    parametros = []
    if meta_id is not None:
        sql += " AND meta_id = ?"
        parametros.append(meta_id)
    if ignorar_id is not None:
        sql += " AND id <> ?"
        parametros.append(ignorar_id)
    return conn.execute(sql, parametros).fetchone()[0]


def _checar_poupanca(conn, dados: dict, ignorar_id=None) -> None:
    """Impede que a poupança fique negativa (resgatar mais do que existe)."""
    if dados["tipo"] != config.TIPO_POUPANCA:
        return
    total = _total_poupanca(conn, None, ignorar_id) + dados["valor_centavos"]
    if total < 0:
        guardado = _total_poupanca(conn, None, ignorar_id)
        raise ErroValidacao(
            f"Resgate maior que o total guardado (você tem {brl(guardado / 100)})."
        )
    if dados["meta_id"] is not None:
        total_meta = _total_poupanca(conn, dados["meta_id"], ignorar_id) + dados["valor_centavos"]
        if total_meta < 0:
            raise ErroValidacao("Resgate maior que o valor guardado nessa meta.")


# ------------------------------------------------------ movimentações (CRUD)
def adicionar_movimentacao(**campos) -> int:
    """Valida e grava uma nova movimentação. Devolve o id criado."""
    dados = validar_movimentacao(**campos)
    with conexao() as conn:
        _checar_poupanca(conn, dados)
        try:
            cursor = conn.execute(
                """INSERT INTO movimentacoes
                   (data, descricao, valor_centavos, tipo, categoria, forma, fonte, meta_id, observacoes)
                   VALUES (:data, :descricao, :valor_centavos, :tipo, :categoria,
                           :forma, :fonte, :meta_id, :observacoes)""",
                dados,
            )
        except sqlite3.IntegrityError as erro:
            raise ErroValidacao("Não foi possível salvar: confira os dados (a meta ainda existe?).") from erro
        return cursor.lastrowid


def atualizar_movimentacao(id_mov: int, **campos) -> None:
    """Valida e atualiza uma movimentação existente."""
    dados = validar_movimentacao(**campos)
    dados["id"] = id_mov
    with conexao() as conn:
        _checar_poupanca(conn, dados, ignorar_id=id_mov)
        try:
            cursor = conn.execute(
                """UPDATE movimentacoes SET
                   data = :data, descricao = :descricao, valor_centavos = :valor_centavos,
                   tipo = :tipo, categoria = :categoria, forma = :forma, fonte = :fonte,
                   meta_id = :meta_id, observacoes = :observacoes
                   WHERE id = :id""",
                dados,
            )
        except sqlite3.IntegrityError as erro:
            raise ErroValidacao("Não foi possível salvar: confira os dados (a meta ainda existe?).") from erro
        if cursor.rowcount == 0:
            raise ErroValidacao("Registro não encontrado (talvez já tenha sido excluído).")


def excluir_movimentacao(id_mov: int) -> None:
    """Exclui uma movimentação (a interface pede confirmação antes)."""
    with conexao() as conn:
        linha = conn.execute("SELECT * FROM movimentacoes WHERE id = ?", (id_mov,)).fetchone()
        if linha is None:
            raise ErroValidacao("Registro não encontrado (talvez já tenha sido excluído).")
        if linha["tipo"] == config.TIPO_POUPANCA and linha["valor_centavos"] > 0:
            mensagem = ("Não é possível excluir este depósito: existem resgates que "
                        "dependem dele. Exclua os resgates primeiro.")
            if _total_poupanca(conn, None, ignorar_id=id_mov) < 0:
                raise ErroValidacao(mensagem)
            if linha["meta_id"] is not None and _total_poupanca(conn, linha["meta_id"], ignorar_id=id_mov) < 0:
                raise ErroValidacao(mensagem)
        conn.execute("DELETE FROM movimentacoes WHERE id = ?", (id_mov,))


def listar_movimentacoes() -> list:
    """Todas as movimentações, em ordem de data."""
    with conexao() as conn:
        linhas = conn.execute("SELECT * FROM movimentacoes ORDER BY data, id").fetchall()
    return [dict(linha) for linha in linhas]


def buscar_movimentacao(id_mov: int):
    with conexao() as conn:
        linha = conn.execute("SELECT * FROM movimentacoes WHERE id = ?", (id_mov,)).fetchone()
    return dict(linha) if linha else None


def existe_duplicado(dados: dict, ignorar_id=None) -> bool:
    """True se já existe lançamento com mesma data, tipo, valor e descrição.

    `dados` é o resultado de validar_movimentacao(). A comparação da
    descrição ignora maiúsculas/minúsculas.
    """
    with conexao() as conn:
        candidatos = conn.execute(
            """SELECT id, descricao FROM movimentacoes
               WHERE data = ? AND tipo = ? AND valor_centavos = ? AND categoria = ?""",
            (dados["data"], dados["tipo"], dados["valor_centavos"], dados["categoria"]),
        ).fetchall()
    alvo = dados["descricao"].casefold()
    return any(c["id"] != ignorar_id and c["descricao"].casefold() == alvo for c in candidatos)


def total_poupanca_centavos(meta_id=None) -> int:
    with conexao() as conn:
        return _total_poupanca(conn, meta_id)


# ------------------------------------------------------------ configurações
def obter_config(chave: str, padrao=None):
    with conexao() as conn:
        linha = conn.execute("SELECT valor FROM configuracoes WHERE chave = ?", (chave,)).fetchone()
    return linha["valor"] if linha else padrao


def salvar_config(chave: str, valor) -> None:
    with conexao() as conn:
        conn.execute(
            "INSERT INTO configuracoes (chave, valor) VALUES (?, ?) "
            "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
            (chave, str(valor)),
        )


def obter_saldo_inicial_centavos() -> int:
    return int(obter_config("saldo_inicial_centavos", 0))


def salvar_saldo_inicial(valor_reais) -> None:
    """O saldo inicial pode ser negativo (conta no vermelho)."""
    try:
        centavos = reais_para_centavos(valor_reais)
    except ValueError as erro:
        raise ErroValidacao("Saldo inicial inválido.") from erro
    if abs(centavos) > LIMITE_CENTAVOS:
        raise ErroValidacao("Valor alto demais. Confira se digitou corretamente.")
    salvar_config("saldo_inicial_centavos", centavos)


def obter_limite_mensal_centavos() -> int:
    """Limite mensal de gastos. 0 significa 'sem limite'."""
    return int(obter_config("limite_mensal_centavos", 0))


def salvar_limite_mensal(valor_reais) -> None:
    try:
        centavos = reais_para_centavos(valor_reais)
    except ValueError as erro:
        raise ErroValidacao("Limite inválido.") from erro
    if centavos < 0:
        raise ErroValidacao("O limite mensal não pode ser negativo.")
    if centavos > LIMITE_CENTAVOS:
        raise ErroValidacao("Valor alto demais. Confira se digitou corretamente.")
    salvar_config("limite_mensal_centavos", centavos)


# ------------------------------------------------------------------- metas
def _validar_meta(nome, valor_alvo):
    nome = " ".join((nome or "").split())
    if not nome:
        raise ErroValidacao("Informe o nome da meta.")
    if len(nome) > 60:
        raise ErroValidacao("O nome da meta pode ter no máximo 60 caracteres.")
    try:
        centavos = reais_para_centavos(valor_alvo)
    except ValueError as erro:
        raise ErroValidacao("Valor da meta inválido.") from erro
    if centavos <= 0:
        raise ErroValidacao("O valor da meta deve ser maior que zero.")
    if centavos > LIMITE_CENTAVOS:
        raise ErroValidacao("Valor alto demais. Confira se digitou corretamente.")
    return nome, centavos


def criar_meta(nome, valor_alvo) -> int:
    nome, centavos = _validar_meta(nome, valor_alvo)
    with conexao() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO metas (nome, valor_alvo_centavos) VALUES (?, ?)", (nome, centavos))
        except sqlite3.IntegrityError as erro:
            raise ErroValidacao("Já existe uma meta com esse nome.") from erro
        return cursor.lastrowid


def atualizar_meta(id_meta: int, nome, valor_alvo) -> None:
    nome, centavos = _validar_meta(nome, valor_alvo)
    with conexao() as conn:
        try:
            conn.execute(
                "UPDATE metas SET nome = ?, valor_alvo_centavos = ? WHERE id = ?",
                (nome, centavos, id_meta))
        except sqlite3.IntegrityError as erro:
            raise ErroValidacao("Já existe uma meta com esse nome.") from erro


def excluir_meta(id_meta: int) -> None:
    """Exclui a meta. Os depósitos continuam existindo, só ficam 'sem meta'."""
    with conexao() as conn:
        conn.execute("DELETE FROM metas WHERE id = ?", (id_meta,))


def listar_metas() -> list:
    with conexao() as conn:
        linhas = conn.execute(
            "SELECT id, nome, valor_alvo_centavos FROM metas ORDER BY nome COLLATE NOCASE").fetchall()
    return [dict(linha) for linha in linhas]


# ---------------------------------------------------- planejado por categoria
def salvar_planejado(valores_reais: dict) -> None:
    """Salva o gasto planejado por mês de cada categoria (0 remove o plano)."""
    with conexao() as conn:
        for categoria, valor in valores_reais.items():
            centavos = reais_para_centavos(valor)
            if centavos < 0:
                raise ErroValidacao(f"O valor planejado de {categoria} não pode ser negativo.")
            if centavos > LIMITE_CENTAVOS:
                raise ErroValidacao("Valor alto demais. Confira se digitou corretamente.")
            if centavos == 0:
                conn.execute("DELETE FROM planejado WHERE categoria = ?", (categoria,))
            else:
                conn.execute(
                    "INSERT INTO planejado (categoria, valor_centavos) VALUES (?, ?) "
                    "ON CONFLICT(categoria) DO UPDATE SET valor_centavos = excluded.valor_centavos",
                    (categoria, centavos))


def listar_planejado() -> dict:
    """{categoria: centavos planejados por mês}"""
    with conexao() as conn:
        linhas = conn.execute("SELECT categoria, valor_centavos FROM planejado").fetchall()
    return {linha["categoria"]: linha["valor_centavos"] for linha in linhas}


# ---------------------------------------------------------- fontes de renda
def listar_fontes() -> list:
    with conexao() as conn:
        linhas = conn.execute("SELECT nome FROM fontes_renda ORDER BY nome COLLATE NOCASE").fetchall()
    return [linha["nome"] for linha in linhas]


def adicionar_fonte(nome) -> None:
    nome = " ".join((nome or "").split())
    if not nome:
        raise ErroValidacao("Informe o nome da fonte de renda.")
    if len(nome) > 60:
        raise ErroValidacao("O nome pode ter no máximo 60 caracteres.")
    with conexao() as conn:
        try:
            conn.execute("INSERT INTO fontes_renda (nome) VALUES (?)", (nome,))
        except sqlite3.IntegrityError as erro:
            raise ErroValidacao("Essa fonte de renda já está cadastrada.") from erro


def remover_fonte(nome: str) -> None:
    """Remove a fonte da lista. Entradas antigas continuam com o nome gravado."""
    with conexao() as conn:
        conn.execute("DELETE FROM fontes_renda WHERE nome = ?", (nome,))


# ------------------------------------------------------------------- backup
def criar_backup() -> Path:
    """Copia o banco para dados/backups/ com data e hora no nome."""
    pasta = Path(config.CAMINHO_BANCO).parent / "backups"
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / f"financas_{datetime.now():%Y%m%d_%H%M%S}.db"
    _copiar_banco(destino)
    return destino


def bytes_do_banco() -> bytes:
    """Cópia consistente do banco em memória (para o botão de download)."""
    with tempfile.TemporaryDirectory() as pasta:
        destino = Path(pasta) / "backup.db"
        _copiar_banco(destino)
        return destino.read_bytes()


def _copiar_banco(destino: Path) -> None:
    # A API de backup do SQLite faz uma cópia segura mesmo com o banco em uso.
    with conexao() as origem:
        copia = sqlite3.connect(destino)
        try:
            origem.backup(copia)
        finally:
            copia.close()
