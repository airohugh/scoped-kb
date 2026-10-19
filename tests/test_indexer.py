"""索引器由 registry 驱动：scan / 位置键、activate 单事务、真增量、维护闸、schema 白名单。

DB 用例走 conftest 的 canary_conn（canary schema，表结构来自仓根 schema.sql）；
embedding 一律用 fake_embed 打桩，不连任何服务。
需要改动语料的用例用 corpus（tmp_path 下的可写副本），不往 tests/fixtures/ 里写。
"""
import conftest
import psycopg2.extensions as ext
import pytest

from scoped_kb import chunking, indexer, registry, scope, search


@pytest.fixture
def reg(fixture_paths):
    return registry.compile_registry(fixture_paths)


def _rows(conn, schema="canary"):
    cur = conn.cursor()
    cur.execute(f"SELECT collection_id, file_path, content_hash FROM {schema}.knowledge_index")
    return cur.fetchall()


def _long_text(title, extra=""):
    """超过 chunk() 的 60 字下限，才会被收录。"""
    return (f"# {title}\n\n{extra}正文必须超过分块的 60 字下限才会被收录，所以这里多写几句："
            "本文件内容全部是虚构的示例文字，仅用于验证增量索引的清理与向量化行为，"
            "写到这个长度就足够进入索引了。\n")


def _seed(canary_conn, reg):
    """用 activate(rebuild=True) 建好第一代索引。"""
    conftest.seed_canary_schema(canary_conn)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)


# ---- scan 由 registry 驱动 ----------------------------------------------

def test_scan_returns_collection_ids_and_location_keys(reg):
    out = chunking.scan(reg)
    assert out, "夹具语料不该扫出空"
    for cid, key, path in out:
        assert cid in reg.collections
        assert path.is_file()
        # 第二项是位置键，且能原路解析回这个文件
        assert isinstance(key, str) and "/" in key
        assert registry.resolve_location(reg, key) == path


def test_quarantine_and_excluded_not_in_scan(reg):
    """quarantine / excluded 不写进索引。"""
    cids = {cid for cid, _key, _p in chunking.scan(reg)}
    for cid in cids:
        assert reg.collections[cid].index_policy in ("active", "confirmed_manifest")
    assert "m-global-unresolved" not in cids


def test_scan_skips_excluded_dirs_and_skip_filenames(reg):
    names = {p.name for _c, _k, p in chunking.scan(reg)}
    assert "skip.md" not in names, "excludes 目录下的文件不得进 scan"
    assert "never_reviewed.md" not in names, "未审阅的全局笔记是 quarantine"
    assert {"a.md", "b.md", "confirmed.md", "mixed_work.md"} <= names


def test_scan_matches_registry_active_roots(reg):
    """scan 只遍历 registry 的 active roots。"""
    roots = reg.active_roots()
    for _cid, _key, path in chunking.scan(reg):
        assert any(r == path or r in path.parents for r in roots), path


def test_no_second_runtime_source_table():
    """归属完全由 registry 决定，不得再有模块级的静态来源表。"""
    for mod in (chunking, indexer):
        assert not hasattr(mod, "SOURCES")
        assert not hasattr(mod, "SKIP_DIRS")


def test_chunk_prefixes_only_the_file_stem(reg):
    """每个分块只前置文件名：不含位置键的 owner/目录，也不含任何绝对路径。"""
    for _cid, key, path in chunking.scan(reg):
        blocks = chunking.chunk(path)
        assert blocks, path
        for b in blocks:
            assert b.splitlines()[0] == path.stem
            assert key not in b
            assert str(path.parent) not in b


# ---- activate --rebuild：单事务提交 -------------------------------------

def test_activate_writes_rows_and_hash(canary_conn, fake_embed, reg):
    _seed(canary_conn, reg)
    rows = _rows(canary_conn)
    assert rows, "activate 后索引不该为空"
    cur = canary_conn.cursor()
    cur.execute("SELECT active_registry_hash FROM canary.knowledge_index_meta")
    assert cur.fetchone()[0] == reg.registry_hash


def test_activate_every_row_has_valid_collection_id(canary_conn, fake_embed, reg):
    _seed(canary_conn, reg)
    for cid, _fp, _h in _rows(canary_conn):
        assert cid in reg.collections
        assert reg.collections[cid].index_policy in ("active", "confirmed_manifest")


def test_activate_stores_location_keys_not_paths(canary_conn, fake_embed, reg):
    """库里的 file_path 都是 "<owner>/<相对路径>"：每个 owner 都在 registry 里，
    且没有任何一行带绝对路径。"""
    _seed(canary_conn, reg)
    paths = {fp for _c, fp, _h in _rows(canary_conn)}
    assert paths
    for fp in paths:
        assert not fp.startswith("/")
        assert fp.split("/", 1)[0] in reg.location_bases
        assert registry.resolve_location(reg, fp).is_file()


def test_activate_is_idempotent_on_meta_row(canary_conn, fake_embed, reg):
    _seed(canary_conn, reg)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)
    cur = canary_conn.cursor()
    cur.execute("SELECT count(*) FROM canary.knowledge_index_meta")
    assert cur.fetchone()[0] == 1


def test_activate_rolls_back_everything_on_failure(canary_conn, fake_embed, monkeypatch, reg):
    """写世代标记那步失败时，全量 DELETE 也必须回滚。

    绝不允许出现「索引被清空、世代还是旧的」——那是停机窗口里最难收拾的状态。
    """
    conftest.set_active_hash(canary_conn, "old" + "0" * 61)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)
    before = set(_rows(canary_conn))
    assert before

    real = indexer._commit_generation

    def explode(*a, **k):
        real(*a, **k)
        raise RuntimeError("模拟提交世代时失败")

    monkeypatch.setattr(indexer, "_commit_generation", explode)
    with pytest.raises(RuntimeError):
        indexer.activate(reg, canary_conn, schema="canary", rebuild=True)
    canary_conn.rollback()
    assert set(_rows(canary_conn)) == before, "失败后索引应保持原样"


def test_activate_aborts_when_embedding_fails(canary_conn, monkeypatch, reg):
    """向量化失败时整体中止、不进事务。

    不允许「降级写 NULL embedding 却标了新世代」——那会让世代标记为一份
    残缺索引背书。与 backfill 的增量降级路径不同：那是增量场景，此处是切换场景。
    """
    conftest.set_active_hash(canary_conn, "old" + "0" * 61)

    def dead(*a, **k):
        raise ConnectionError("embedding 服务不可达")

    monkeypatch.setattr(indexer, "embed", dead)
    with pytest.raises(ConnectionError):
        indexer.activate(reg, canary_conn, schema="canary", rebuild=True)
    canary_conn.rollback()
    cur = canary_conn.cursor()
    cur.execute("SELECT active_registry_hash FROM canary.knowledge_index_meta")
    assert cur.fetchone()[0].startswith("old"), "失败不得推进世代"
    cur.execute("SELECT count(*) FROM canary.knowledge_index")
    assert cur.fetchone()[0] == 0, "失败不得留下半份索引"


def test_reindex_updates_collection_when_content_unchanged(canary_conn, fake_embed, reg):
    """正文 hash 不变、归属变化时索引行仍更新到新 collection。

    `ON CONFLICT DO NOTHING` 会静默跳过——这正是重新归类后旧 scope 残留的路径。
    """
    conftest.seed_canary_schema(canary_conn)
    vec = conftest.text_vector("内容")
    cur = canary_conn.cursor()
    cur.execute("""
        INSERT INTO canary.knowledge_index
          (collection_id, file_path, content, content_hash, embedding, embedding_model)
        VALUES ('b-notes', %s, %s, %s, %s::vector, 'test')
    """, ("repo_b/x.md", "内容", "a" * 64, vec))
    canary_conn.commit()

    # 同样的 (file_path, content_hash) 但不同 collection 再写一次
    indexer.index_rows(canary_conn, [("a-decisions", "repo_b/x.md", "内容", "a" * 64)],
                       [vec], schema="canary")
    canary_conn.commit()
    cur.execute("SELECT collection_id FROM canary.knowledge_index WHERE file_path='repo_b/x.md'")
    assert cur.fetchall() == [("a-decisions",)]


def test_canary_rows_stay_out_of_public_tables(canary_conn, fake_embed, reg):
    """canary 打的是 canary schema：public 里即使有同名表，也不得出现这次写入的行。

    （只断言方向：测试库的 public 表不被写。）
    """
    _seed(canary_conn, reg)
    assert _rows(canary_conn), "canary 里应有行"
    cur = canary_conn.cursor()
    for table in ("knowledge_index", "knowledge_index_meta"):
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        if cur.fetchone()[0] is not None:
            cur.execute(f"SELECT count(*) FROM public.{table}")
            assert cur.fetchone()[0] == 0, f"canary 数据泄漏进 public.{table}"


# ---- 维护闸 -------------------------------------------------------------

def test_index_refuses_when_maintenance_flag_present(gate):
    gate("索引迁移中")
    with pytest.raises(SystemExit) as exc:
        indexer._assert_not_in_maintenance()
    assert exc.value.code != 0


def test_maintenance_message_names_the_reason(gate):
    """闸文件内容要打进消息——否则接手的人不知道是谁、为什么设的闸。"""
    gate("由运维设置，迁移期间")
    with pytest.raises(SystemExit) as exc:
        indexer._assert_not_in_maintenance()
    assert "迁移期间" in str(exc.value)


def test_no_maintenance_flag_is_a_noop(gate):
    flag = gate(None)
    assert not flag.exists()
    indexer._assert_not_in_maintenance()          # 不抛即通过


def test_activate_runs_despite_maintenance_flag(canary_conn, fake_embed, reg, gate):
    """activate 是迁移动作本身，不被自己拦住。"""
    gate("迁移中")
    _seed(canary_conn, reg)
    assert _rows(canary_conn)


def test_canary_activate_needs_neither_gate_nor_override(canary_conn, fake_embed, reg, gate):
    """canary 是隔离库，演练不受生产闸约束。"""
    assert not gate(None).exists()
    _seed(canary_conn, reg)
    assert _rows(canary_conn)


# ---- 配置变化 → 自动换代 -----------------------------------------------

def test_index_escalates_to_rebuild_when_hash_changed(canary_conn, fake_embed, reg):
    """普通 index 发现 registry 换代时，必须自动全量重建并更新 active hash。

    只做增量的话，改一次配置就会让所有 search 因世代不符而永久 fail closed，
    而触发者可能是定时任务——没有人在看输出。
    """
    conftest.set_active_hash(canary_conn, "stale" + "0" * 59)
    stats = indexer.index_with_registry(reg, canary_conn, schema="canary")
    assert stats["rebuilt"] is True, "世代不同必须升级为全量重建"
    cur = canary_conn.cursor()
    cur.execute("SELECT active_registry_hash FROM canary.knowledge_index_meta")
    assert cur.fetchone()[0] == reg.registry_hash


def test_index_stays_incremental_when_hash_unchanged(canary_conn, fake_embed, reg):
    _seed(canary_conn, reg)
    stats = indexer.index_with_registry(reg, canary_conn, schema="canary")
    assert stats["rebuilt"] is False, "世代未变不得触发全量重建"
    assert stats["embedded"] == 0, "世代未变且内容未变时，不该重算任何向量"


def test_config_change_makes_retrieval_work_again(canary_conn, fake_embed,
                                                  fixture_paths, reg):
    """端到端：世代不符 → search 拒绝；跑一次普通 index → 恢复可用。"""
    conftest.set_active_hash(canary_conn, "stale" + "0" * 59)
    eff = scope.Effective("work", "work", (), "",
                          tuple(sorted(set(reg.allowed_for("work")))), reg.registry_hash)
    with pytest.raises(scope.ScopeError):
        search.retrieve_candidates(canary_conn, "q", eff, 5,
                                   schema="canary", config_paths=fixture_paths)
    indexer.index_with_registry(reg, canary_conn, schema="canary")
    rows = search.retrieve_candidates(canary_conn, "q", eff, 5,
                                      schema="canary", config_paths=fixture_paths)
    # 不再抛 ScopeError 还不够——空结果也算“没抛”。要真的拿回 work 域里那三个文件
    assert {(cid, key) for key, _content, cid, _sim in rows} == {
        ("a-decisions", "repo_a/docs/decisions/a.md"),
        ("m-global", "m-global/confirmed.md"),
        ("m-global-work", "m-global/mixed_work.md"),
    }


# ---- 真增量索引 ---------------------------------------------------------

def test_incremental_only_embeds_new_chunks(canary_conn, fake_embed, corpus):
    """核心：世代未变、内容未变时**一个向量都不重算**。

    拿 activate(rebuild=False) 当增量用是错的——它无条件对全部分块重新向量化。
    1000 分块的库每次触发都要跑几分钟 embedding，而其中 999 个与库里那份逐字节相同。
    """
    reg = registry.compile_registry(corpus.paths())
    _seed(canary_conn, reg)

    fake_embed.clear()
    stats = indexer._incremental_index(reg, canary_conn, schema="canary")
    assert stats["embedded"] == 0
    assert sum(len(c) for c in fake_embed) == 0, f"不该有任何 embedding 调用，实际 {fake_embed}"

    # 新增一个文件 → 只对它的分块做 embedding
    (corpus.root / "repo_a/docs/decisions/added.md").write_text(
        _long_text("新增判例"), encoding="utf-8")
    fake_embed.clear()
    stats = indexer._incremental_index(reg, canary_conn, schema="canary")
    assert stats["embedded"] == 1, stats
    assert sum(len(c) for c in fake_embed) == 1, f"只该向量化 1 个新分块，实际 {fake_embed}"


def test_incremental_deletes_rows_of_deleted_file(canary_conn, fake_embed, corpus):
    """删除文件后，普通 index 必须删掉它的旧行（索引是派生物，不留孤儿）。"""
    reg = registry.compile_registry(corpus.paths())
    gone = corpus.root / "repo_a/docs/decisions/willgo.md"
    gone.write_text(_long_text("待删"), encoding="utf-8")
    _seed(canary_conn, reg)
    assert "repo_a/docs/decisions/willgo.md" in {fp for _c, fp, _h in _rows(canary_conn)}

    gone.unlink()
    stats = indexer._incremental_index(reg, canary_conn, schema="canary")
    assert stats["removed_files"] == 1
    assert "repo_a/docs/decisions/willgo.md" not in {fp for _c, fp, _h in _rows(canary_conn)}


def test_incremental_drops_stale_chunk_hashes(canary_conn, fake_embed, corpus):
    """内容变化后旧 content_hash 的行必须消失。"""
    reg = registry.compile_registry(corpus.paths())
    f = corpus.root / "repo_a/docs/decisions/mutable.md"
    key = "repo_a/docs/decisions/mutable.md"
    f.write_text(_long_text("可变", "第一版。"), encoding="utf-8")
    _seed(canary_conn, reg)
    old_hashes = {h for _c, fp, h in _rows(canary_conn) if fp == key}
    assert old_hashes

    f.write_text(_long_text("可变", "第二版与第一版完全不同。"), encoding="utf-8")
    stats = indexer._incremental_index(reg, canary_conn, schema="canary")
    assert stats["stale_chunks"] >= 1
    now = {h for _c, fp, h in _rows(canary_conn) if fp == key}
    assert now and not (old_hashes & now), "旧 content_hash 仍在库里"


def test_file_moved_to_excluded_is_no_longer_reachable(canary_conn, fake_embed, reg,
                                                       corpus):
    """文件移入 excluded 后，任何 profile 都不得再命中它。

    这是四类清理里后果最隐蔽的一种：残留行会继续被它**原来的** collection
    命中，而那个 collection 可能仍是 active——于是一份已被隔离的文件照样
    能被检索到，且不报错。
    """
    _seed(canary_conn, reg)
    assert {fp for _c, fp, _h in _rows(canary_conn) if fp.startswith("repo_b/")}, \
        "b-notes 应有行"

    # 把 b-notes 整体改成 excluded（同时移出 profile，避免撞「profile 不得含
    # excluded」那条编译期规则）
    def patch(d):
        d["collections"]["b-notes"]["index_policy"] = "excluded"
        d["profiles"]["research"] = ["m-global"]
    paths2 = corpus.paths(scope_policy=patch)
    reg2 = registry.compile_registry(paths2)

    indexer._incremental_index(reg2, canary_conn, schema="canary")
    assert not {fp for _c, fp, _h in _rows(canary_conn) if fp.startswith("repo_b/")}, \
        "excluded 后仍有残留行"

    # 端到端：research profile 检索不到它。
    # 复核必须用**产出 reg2 的那一份** config_paths：换一份就是两个世代，
    # 会被世代校验挡下——那是校验在起作用，不是被测行为出错。
    eff = scope.Effective("research", "research", (), "",
                          tuple(sorted(set(reg2.allowed_for("research")))),
                          reg2.registry_hash)
    rows = search.retrieve_candidates(canary_conn, "笔记", eff, 50, schema="canary",
                                      config_paths=paths2)
    assert all(not r[0].startswith("repo_b/") for r in rows)


def test_file_moved_to_quarantine_is_no_longer_reachable(canary_conn, fake_embed, reg,
                                                         corpus):
    """移入 quarantine 同理。"""
    _seed(canary_conn, reg)
    assert {fp for _c, fp, _h in _rows(canary_conn) if fp.startswith("repo_b/")}

    def patch(d):
        d["collections"]["b-notes"]["index_policy"] = "quarantine"
        d["collections"]["b-notes"]["quarantine_reason"] = "测试用隔离"
        d["collections"]["b-notes"]["exit_path"] = "review 后转回 active"
        d["profiles"]["research"] = ["m-global"]
    reg2 = registry.compile_registry(corpus.paths(scope_policy=patch))

    indexer._incremental_index(reg2, canary_conn, schema="canary")
    assert not {fp for _c, fp, _h in _rows(canary_conn) if fp.startswith("repo_b/")}


def test_embedding_not_inside_open_transaction(canary_conn, monkeypatch, reg):
    """embedding 期间连接不得处于事务中（idle-in-transaction 阻塞 vacuum）。"""
    conftest.seed_canary_schema(canary_conn)
    seen = []

    def probe(texts, **_kwargs):
        seen.append(canary_conn.status)      # psycopg2 连接事务状态
        return [conftest.text_vector(t) for t in texts]

    monkeypatch.setattr(indexer, "embed", probe)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)
    assert seen, "没有捕获到 embedding 调用"
    for st in seen:
        assert st == ext.STATUS_READY, f"embedding 发生在事务内（status={st}）"


def test_read_active_hash_ends_transaction(canary_conn, reg):
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    indexer.read_active_hash(canary_conn, schema="canary")
    assert canary_conn.status == ext.STATUS_READY


# ---- schema 白名单 ------------------------------------------------------

@pytest.mark.parametrize("bad", ["pg_catalog", "canary; DROP SCHEMA public",
                                 "", "PUBLIC", "information_schema"])
def test_schema_whitelist_rejects_anything_else(bad):
    with pytest.raises(ValueError) as exc:
        indexer._safe_schema(bad)
    assert "非法 schema" in str(exc.value)


@pytest.mark.parametrize("ok", ["public", "canary"])
def test_schema_whitelist_allows_the_two(ok):
    assert indexer._safe_schema(ok) == ok
