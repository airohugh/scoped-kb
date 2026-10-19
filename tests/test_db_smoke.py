"""数据库冒烟：canary 表结构与 schema.sql 一致，index_rows 能写能读。

需要专用测试库（SCOPED_KB_TEST_DATABASE_URL）；没配时 canary_conn 夹具 skip。
"""
import hashlib
import re
from pathlib import Path

import pytest

from scoped_kb import db, indexer

SCHEMA_SQL = Path(__file__).parent.parent / "schema.sql"


def _fake_vec(seed, dim=1024):
    """确定性假向量：不调 embedding 服务。"""
    return [((seed * 31 + i) % 17) / 17.0 for i in range(dim)]


def _row(cid, path, text):
    return (cid, path, text, hashlib.sha256(text.encode()).hexdigest())


def test_connect_requires_database_url(monkeypatch):
    monkeypatch.delenv("SCOPED_KB_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        db.connect()
    assert "SCOPED_KB_DATABASE_URL" in str(exc.value)


def test_index_rows_roundtrip(canary_conn):
    rows = [_row("c-one", "a/one.md", "第一段"), _row("c-two", "a/two.md", "第二段")]
    indexer.index_rows(canary_conn, rows, [_fake_vec(1), _fake_vec(2)], schema="canary")
    canary_conn.commit()

    cur = canary_conn.cursor()
    cur.execute("SELECT collection_id, file_path, content, content_hash, embedding_model "
                "FROM canary.knowledge_index ORDER BY file_path")
    got = cur.fetchall()
    assert [(g[0], g[1], g[2], g[3]) for g in got] == [r for r in rows]
    assert all(g[4] for g in got)                # 有向量就记下了模型名

    cur.execute("SELECT embedding FROM canary.knowledge_index WHERE file_path = 'a/one.md'")
    stored = [float(x) for x in cur.fetchone()[0].strip("[]").split(",")]
    assert len(stored) == 1024
    assert stored[:3] == pytest.approx(_fake_vec(1)[:3], abs=1e-5)


def test_index_rows_conflict_updates_collection(canary_conn):
    row = _row("c-old", "a/x.md", "同一段正文")
    indexer.index_rows(canary_conn, [row], [_fake_vec(3)], schema="canary")
    indexer.index_rows(canary_conn, [("c-new",) + row[1:]], [_fake_vec(3)], schema="canary")
    canary_conn.commit()
    cur = canary_conn.cursor()
    cur.execute("SELECT collection_id FROM canary.knowledge_index WHERE file_path = 'a/x.md'")
    assert cur.fetchall() == [("c-new",)]


def _declared_columns(table):
    """从 schema.sql 里解析某张表声明的列名（不依赖数据库）。"""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", sql, re.DOTALL).group(1)
    cols = set()
    for line in body.splitlines():
        m = re.match(r"\s+([a-z_]+)\s+\S", line)
        if m:
            cols.add(m.group(1))
    return cols


@pytest.mark.parametrize("table", ["knowledge_index", "knowledge_index_meta"])
def test_canary_columns_match_schema_sql(canary_conn, table):
    cur = canary_conn.cursor()
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'canary' AND table_name = %s", (table,))
    actual = {r[0] for r in cur.fetchall()}
    assert actual == _declared_columns(table)
    assert "project_id" not in actual


def test_canary_has_unique_chunk_index(canary_conn):
    cur = canary_conn.cursor()
    cur.execute("""
        SELECT array_agg(a.attname::text ORDER BY a.attname)
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
        WHERE n.nspname = 'canary' AND t.relname = 'knowledge_index' AND i.indisunique
        GROUP BY i.indexrelid""")
    unique_sets = [sorted(r[0]) for r in cur.fetchall()]
    assert ["content_hash", "file_path"] in unique_sets
