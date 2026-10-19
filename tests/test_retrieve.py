"""唯一候选检索 helper 与索引世代校验。

三段式顺序不可调换：
  1. 预检——在 embedding 之前挡掉空 allowlist 与世代不一致；
  2. 快照内复检 + 取候选——REPEATABLE READ READ ONLY 消除 TOCTOU；
  3. 出结果前用**同一份 config source** 复编译，配置中途变化则丢弃结果。

embedding 一律打桩（fake_embed），测试里不连任何服务。
"""
import inspect

import conftest
import pytest

from scoped_kb import registry, scope, search


class _Spy:
    """记录所有发往数据库的 SQL 文本，其余行为透传。"""

    def __init__(self, cur, sink):
        self._cur, self._sink = cur, sink

    def execute(self, sql, params=None):
        self._sink.append(sql)
        return self._cur.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ConnSpy:
    """连接代理。psycopg2 的 connection.cursor 是只读属性，monkeypatch 不上，
    所以包一层而不是打补丁。"""

    def __init__(self, conn, sink):
        self._conn, self._sink = conn, sink

    def cursor(self, *a, **k):
        return _Spy(self._conn.cursor(*a, **k), self._sink)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _spy_sql(conn):
    sink = []
    return _ConnSpy(conn, sink), sink


def _code_without_comments_and_strings(func):
    """取函数源码，剔除注释与字符串字面量。

    静态断言只该看**代码**：注释里写「不能裸调 compile_registry()」
    是在说明为什么，不是在调用它。
    """
    import io
    import tokenize
    src = inspect.getsource(func)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def _eff(reg, profile="work", registry_hash=None):
    return scope.Effective(profile, profile, (), "",
                           tuple(sorted(set(reg.allowed_for(profile)))),
                           registry_hash or reg.registry_hash)


@pytest.fixture
def reg(fixture_paths):
    return registry.compile_registry(fixture_paths)


# ---- 空 allowlist / 世代不一致：必须在 embedding 之前退出 ---------------

def test_empty_allowlist_exits_before_embedding(canary_conn, fake_embed, fixture_paths):
    """配置错误时没有 embedding 请求、没有正文查询、没有全库回退。"""
    spy, sql = _spy_sql(canary_conn)
    eff = scope.Effective("work", "work", (), "", (), "d" * 64)
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(spy, "任意问题", eff, 5,
                                   schema="canary", config_paths=fixture_paths)
    assert "allowed collections 为空" in str(exc.value)
    assert fake_embed == [], "空 allowlist 不应发出 embedding 请求"
    assert not any("knowledge_index" in s and "SELECT" in s.upper() for s in sql)


def test_generation_mismatch_exits_before_embedding(canary_conn, fake_embed,
                                                    fixture_paths, reg):
    conftest.set_active_hash(canary_conn, "f" * 64)
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(canary_conn, "任意问题",
                                   _eff(reg, registry_hash="0" * 64),
                                   5, schema="canary", config_paths=fixture_paths)
    msg = str(exc.value)
    assert "registry 世代" in msg
    assert "scoped-kb index" in msg, "世代不一致要指向 index（它会自动全量重建）"
    assert fake_embed == []


def test_missing_meta_row_fails_closed(canary_conn, fake_embed, fixture_paths, reg):
    """meta 表存在但没有行 = 世代未提交，同样拒绝。"""
    conftest.seed_canary_schema(canary_conn)
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(canary_conn, "q", _eff(reg), 5,
                                   schema="canary", config_paths=fixture_paths)
    assert "世代未提交" in str(exc.value)
    assert fake_embed == []


# ---- 候选 SQL 的结构 ----------------------------------------------------

def test_sql_always_carries_collection_filter(canary_conn, fake_embed, fixture_paths, reg):
    """不存在无 collection_id 条件的分支。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    spy, sql = _spy_sql(canary_conn)
    search.retrieve_candidates(spy, "测试", _eff(reg), 5,
                               schema="canary", config_paths=fixture_paths)
    body = [s for s in sql
            if "knowledge_index" in s and "SELECT" in s.upper()
            and "knowledge_index_meta" not in s and "information_schema" not in s]
    assert body, "没有捕获到正文查询"
    for stmt in body:
        assert "collection_id = ANY" in stmt


def test_generation_check_uses_repeatable_read_snapshot(canary_conn, fake_embed,
                                                        fixture_paths, reg):
    """世代校验与候选查询必须读同一个 snapshot。

    只写 `BEGIN ... READ ONLY` 不够——PostgreSQL 默认 READ COMMITTED，
    READ ONLY 只禁写、不提供跨语句 snapshot 稳定性，同事务两条 SELECT
    仍各取新 snapshot，TOCTOU 窗口依旧存在。
    """
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    spy, sql = _spy_sql(canary_conn)
    search.retrieve_candidates(spy, "测试", _eff(reg), 5,
                               schema="canary", config_paths=fixture_paths)
    begins = [s for s in sql if s.strip().upper().startswith("BEGIN")]
    assert begins, "没有显式声明事务隔离级别"
    assert any("REPEATABLE READ" in s.upper() and "READ ONLY" in s.upper()
               for s in begins), f"隔离级别不足，实际发出: {begins}"
    inside = sql[sql.index(begins[0]):]
    assert any("knowledge_index_meta" in s for s in inside), "复检未落在同一事务内"
    assert any("collection_id = ANY" in s for s in inside), "候选查询未落在同一事务内"


def test_limit_is_passed_through(canary_conn, fake_embed, fixture_paths, reg):
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    for i in range(3):
        _insert(canary_conn, "a-decisions", f"repo_a/docs/decisions/{i}.md", f"内容{i}")
    spy, sql = _spy_sql(canary_conn)
    rows = search.retrieve_candidates(spy, "测试", _eff(reg), 2,
                                      schema="canary", config_paths=fixture_paths)
    assert any("LIMIT" in s.upper() for s in sql)
    assert len(rows) == 2, "limit 必须真的限制返回条数"


# ---- 同一份 config source ----------------------------------------------

def test_final_recheck_uses_injected_config_not_production_defaults(
        canary_conn, fake_embed, monkeypatch, fixture_paths, reg):
    """末次复核必须用传入的 config_paths，不得退回默认配置目录。

    裸调会让一次 retrieve 出现两个配置来源：eff 来自注入的那份，复核却读默认那份，
    于是测试场景永远「检测到漂移」而假失败，生产场景下复核基准是错的。
    """
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    seen = []
    real = registry.compile_registry
    monkeypatch.setattr(registry, "compile_registry",
                        lambda config_paths=None: seen.append(config_paths) or real(config_paths))
    search.retrieve_candidates(canary_conn, "测试", _eff(reg), 5,
                               schema="canary", config_paths=fixture_paths)
    assert seen, "末次复核没有调用 compile_registry"
    assert all(p == fixture_paths for p in seen), f"复核用了别的配置来源: {seen}"


def test_no_bare_compile_registry_call_in_retrieval_path():
    """静态兜底：检索路径里不得出现不带参数的 compile_registry()。"""
    code = _code_without_comments_and_strings(search.retrieve_candidates)
    assert "compile_registry ( )" not in code, "裸调会退回默认配置目录"


def test_local_config_change_midflight_discards_result(canary_conn, fake_embed, monkeypatch,
                                                       fixture_paths, make_paths, reg):
    """输出结果前再次确认本地配置 hash 未变化，变了就丢弃。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    # retrieve_candidates 内部只调一次 compile_registry（第 3 段的末次复核），
    # 所以直接让它读到「已被改写的那份配置」——这正是真实场景：
    # eff 产出之后、结果输出之前，磁盘上的配置被人改了。
    drifted = make_paths(scope_policy=lambda d: d["profiles"].__setitem__(
        "work", ["a-decisions", "m-global"]))
    real = registry.compile_registry
    monkeypatch.setattr(registry, "compile_registry",
                        lambda config_paths=None: real(drifted))
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(canary_conn, "测试", _eff(reg), 5,
                                   schema="canary", config_paths=fixture_paths)
    assert "本地配置在查询过程中发生变化" in str(exc.value)


# ---- 退出码与「无命中」的区分 -------------------------------------------

def test_no_hit_returns_empty_not_error(canary_conn, fake_embed, fixture_paths, reg):
    """「无命中」与「profile/config 非法」使用不同提示和退出码。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    rows = search.retrieve_candidates(canary_conn, "测试", _eff(reg), 5,
                                      schema="canary", config_paths=fixture_paths)
    assert rows == []


def test_scope_error_exit_code_is_nonzero_and_distinct():
    assert scope.SCOPE_ERROR_EXIT == 3
    assert scope.SCOPE_ERROR_EXIT != 0


# ---- 真实候选：写入 canary 后能按 collection 过滤 -----------------------

def _insert(conn, cid, key, content):
    """直接写一行（embedding 取内容的假向量）。key 是位置键。"""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO canary.knowledge_index
          (collection_id, source_agent, file_path, content,
           content_hash, embedding, embedding_model)
        VALUES (%s, 'test', %s, %s, md5(%s)||md5(%s), %s::vector, 'test')
        ON CONFLICT (file_path, content_hash) DO NOTHING""",
        (cid, key, content, content, key, conftest.text_vector(content)))
    conn.commit()


def test_forbidden_collection_never_in_candidates(canary_conn, fake_embed,
                                                  fixture_paths, reg):
    """work 的候选集里不得出现 research scope 的 b-notes——
    过滤发生在 SQL 的 WHERE 里，不是取回后再筛。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    _insert(canary_conn, "a-decisions", "repo_a/docs/decisions/a.md", "季度目标复盘")
    _insert(canary_conn, "b-notes", "repo_b/notes/b.md", "季度目标复盘")
    rows = search.retrieve_candidates(canary_conn, "季度目标复盘", _eff(reg, "work"), 10,
                                      schema="canary", config_paths=fixture_paths)
    cids = {r[2] for r in rows}
    assert "a-decisions" in cids
    assert "b-notes" not in cids, f"越界: {cids}"


def test_research_profile_sees_only_its_own(canary_conn, fake_embed, fixture_paths, reg):
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    _insert(canary_conn, "a-decisions", "repo_a/docs/decisions/a.md", "季度目标复盘")
    _insert(canary_conn, "b-notes", "repo_b/notes/b.md", "季度目标复盘")
    rows = search.retrieve_candidates(canary_conn, "季度目标复盘",
                                      _eff(reg, "research"), 10,
                                      schema="canary", config_paths=fixture_paths)
    assert {r[2] for r in rows} == {"b-notes"}


def test_quarantine_rows_unreachable_from_any_profile(canary_conn, fake_embed,
                                                      fixture_paths, reg):
    """review-only quarantine 的行即使写进了索引，也不得被任何 profile 命中。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    _insert(canary_conn, "m-global-unresolved", "m-global/never_reviewed.md", "季度目标复盘")
    for prof in ("work", "research"):
        rows = search.retrieve_candidates(canary_conn, "季度目标复盘", _eff(reg, prof), 10,
                                          schema="canary", config_paths=fixture_paths)
        assert all(r[2] != "m-global-unresolved" for r in rows)


def test_rows_carry_collection_id(canary_conn, fake_embed, fixture_paths, reg):
    """返回四元组 (file_path, content, collection_id, sim)，file_path 是库里存的位置键。"""
    conftest.set_active_hash(canary_conn, reg.registry_hash)
    _insert(canary_conn, "a-decisions", "repo_a/docs/decisions/a.md", "内容")
    rows = search.retrieve_candidates(canary_conn, "内容", _eff(reg), 5,
                                      schema="canary", config_paths=fixture_paths)
    assert len(rows[0]) == 4
    fp, content, cid, sim = rows[0]
    assert (fp, content, cid) == ("repo_a/docs/decisions/a.md", "内容", "a-decisions")
    assert 0.0 <= sim <= 1.0 + 1e-6
