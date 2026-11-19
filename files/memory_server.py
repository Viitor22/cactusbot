"""
Servidor MCP de memória do atendente de WhatsApp.

Persiste em SQLite:
  - mensagens  : histórico da conversa por telefone
  - fatos      : o que se sabe sobre o cliente (nome, cidade, serviço de interesse...)
  - estado     : se a conversa está com humano (IA pausada)
  - vistas     : ids de mensagens do WhatsApp já processadas (dedupe que sobrevive a reinício)

Rodar:  python memory_server.py        (streamable HTTP em http://127.0.0.1:8001/mcp)

Atenção: o banco guarda dados pessoais (telefone + conversa). Mantenha o servidor em
127.0.0.1 ou atrás de autenticação, e use `esquecer_cliente` para atender pedidos de
exclusão (LGPD).
"""

import json
import os
import sqlite3
import time
from contextlib import closing

from mcp.server.fastmcp import FastMCP

DB_PATH = os.getenv("MEMORY_DB", "memoria.db")
HOST = os.getenv("MEMORY_HOST", "127.0.0.1")
PORT = int(os.getenv("MEMORY_PORT", "8001"))
MAX_FATOS_POR_CLIENTE = 30
MAX_VALOR = 500

mcp = FastMCP("memoria-whatsapp", host=HOST, port=PORT)


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _init() -> None:
    with closing(_db()) as con, con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefone TEXT NOT NULL,
                papel TEXT NOT NULL CHECK (papel IN ('user','assistant')),
                conteudo TEXT NOT NULL,
                criado_em REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_msg_tel ON mensagens (telefone, id);
            CREATE TABLE IF NOT EXISTS fatos (
                telefone TEXT NOT NULL,
                chave TEXT NOT NULL,
                valor TEXT NOT NULL,
                atualizado_em REAL NOT NULL,
                PRIMARY KEY (telefone, chave)
            );
            CREATE TABLE IF NOT EXISTS estado (
                telefone TEXT PRIMARY KEY,
                humano INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vistas (
                message_id TEXT PRIMARY KEY,
                criado_em REAL NOT NULL
            );
            """
        )


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------- Histórico ----------------
@mcp.tool()
def salvar_mensagem(telefone: str, papel: str, conteudo: str) -> str:
    """Grava uma mensagem da conversa. papel: 'user' (cliente) ou 'assistant' (atendente)."""
    if papel not in ("user", "assistant"):
        return _j({"ok": False, "erro": "papel inválido"})
    with closing(_db()) as con, con:
        con.execute(
            "INSERT INTO mensagens (telefone, papel, conteudo, criado_em) VALUES (?,?,?,?)",
            (telefone, papel, conteudo, time.time()),
        )
    return _j({"ok": True})


@mcp.tool()
def obter_historico(telefone: str, limite: int = 20) -> str:
    """Devolve as últimas `limite` mensagens do cliente, da mais antiga para a mais nova."""
    limite = max(1, min(limite, 100))
    with closing(_db()) as con:
        rows = con.execute(
            "SELECT papel, conteudo FROM mensagens WHERE telefone=? ORDER BY id DESC LIMIT ?",
            (telefone, limite),
        ).fetchall()
    return _j([{"role": r["papel"], "content": r["conteudo"]} for r in reversed(rows)])


# ---------------- Fatos sobre o cliente ----------------
@mcp.tool()
def salvar_fato(telefone: str, chave: str, valor: str) -> str:
    """Guarda (ou atualiza) um fato durável sobre o cliente, ex.: chave='nome'."""
    chave = chave.strip().lower()[:60]
    valor = valor.strip()[:MAX_VALOR]
    if not chave or not valor:
        return _j({"ok": False, "erro": "chave e valor são obrigatórios"})
    with closing(_db()) as con, con:
        existe = con.execute(
            "SELECT 1 FROM fatos WHERE telefone=? AND chave=?", (telefone, chave)
        ).fetchone()
        total = con.execute("SELECT COUNT(*) FROM fatos WHERE telefone=?", (telefone,)).fetchone()[0]
        if not existe and total >= MAX_FATOS_POR_CLIENTE:
            return _j({"ok": False, "erro": "limite de fatos atingido"})
        con.execute(
            "INSERT INTO fatos (telefone, chave, valor, atualizado_em) VALUES (?,?,?,?) "
            "ON CONFLICT(telefone, chave) DO UPDATE SET valor=excluded.valor, "
            "atualizado_em=excluded.atualizado_em",
            (telefone, chave, valor, time.time()),
        )
    return _j({"ok": True})


@mcp.tool()
def listar_fatos(telefone: str) -> str:
    """Devolve um objeto {chave: valor} com o que se sabe sobre o cliente."""
    with closing(_db()) as con:
        rows = con.execute(
            "SELECT chave, valor FROM fatos WHERE telefone=? ORDER BY chave", (telefone,)
        ).fetchall()
    return _j({r["chave"]: r["valor"] for r in rows})


@mcp.tool()
def esquecer_fato(telefone: str, chave: str) -> str:
    """Apaga um fato específico do cliente."""
    with closing(_db()) as con, con:
        n = con.execute(
            "DELETE FROM fatos WHERE telefone=? AND chave=?", (telefone, chave.strip().lower())
        ).rowcount
    return _j({"ok": True, "apagados": n})


@mcp.tool()
def esquecer_cliente(telefone: str) -> str:
    """Apaga TUDO do cliente (mensagens, fatos e estado). Use para pedidos de exclusão (LGPD)."""
    with closing(_db()) as con, con:
        m = con.execute("DELETE FROM mensagens WHERE telefone=?", (telefone,)).rowcount
        f = con.execute("DELETE FROM fatos WHERE telefone=?", (telefone,)).rowcount
        con.execute("DELETE FROM estado WHERE telefone=?", (telefone,))
    return _j({"ok": True, "mensagens": m, "fatos": f})


# ---------------- Estado da conversa ----------------
@mcp.tool()
def definir_modo_humano(telefone: str, ativo: bool) -> str:
    """Liga/desliga o atendimento humano (quando ligado, a IA não responde)."""
    with closing(_db()) as con, con:
        con.execute(
            "INSERT INTO estado (telefone, humano) VALUES (?,?) "
            "ON CONFLICT(telefone) DO UPDATE SET humano=excluded.humano",
            (telefone, 1 if ativo else 0),
        )
    return _j({"ok": True})


@mcp.tool()
def obter_modo_humano(telefone: str) -> str:
    """Retorna true se a conversa está com a equipe humana."""
    with closing(_db()) as con:
        row = con.execute("SELECT humano FROM estado WHERE telefone=?", (telefone,)).fetchone()
    return _j(bool(row and row["humano"]))


# ---------------- Dedupe de webhooks ----------------
@mcp.tool()
def marcar_mensagem_vista(message_id: str) -> str:
    """Retorna true se a mensagem é nova (e a registra); false se já tinha sido processada."""
    with closing(_db()) as con, con:
        cur = con.execute(
            "INSERT OR IGNORE INTO vistas (message_id, criado_em) VALUES (?,?)",
            (message_id, time.time()),
        )
        con.execute("DELETE FROM vistas WHERE criado_em < ?", (time.time() - 7 * 86400,))
    return _j(cur.rowcount == 1)


if __name__ == "__main__":
    _init()
    mcp.run(transport="streamable-http")  # endpoint: http://HOST:PORT/mcp
