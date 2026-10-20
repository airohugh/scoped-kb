"""作用域 canary：带人工标注 ground truth 的碰撞语料。🔒 硬门槛

两层门槛：
  结构层——任一 profile（含合法 cross）的候选集里，forbidden collection 计数严格为 0；
           判据是 index unit 的 collection 归属。
  分类层——人工标注的语料不得被归到错误的域，也不得被其它 profile 的默认检索命中。
           **不使用关键词判定**，**也不得只凭「该单元标着某个 scope」自证合法**——
           标签是被检验的对象，证据是 tests/fixtures/canary/ground_truth.json 里
           人工写下的标注（每个文件属于哪个域、哪些 profile 应当读到它）。

语料（tests/fixtures/canary/）：
  reading / cooking —— 两个业务域，**同名文件、正文逐字相同**（只换一个占位词），
                       关键词和文件名都分不出谁属于哪个域；
  common            —— 两个域都该读到的公共规则；
  archive           —— active 但不在任何 profile 里：真的进了索引，检索却一条都拿不到；
  inbox             —— confirmed_manifest：人工确认过的文件进 canary-inbox-reading，
                       没确认的落 canary-inbox-held（quarantine），不进索引、谁也读不到。

canary 的硬约束：
  · 走隔离的 canary schema，但**过完整的生产管线**——同一个 registry 编译器、同一个
    scan()、同一个 chunk()、同一个 retrieve_candidates()、同一条候选 SQL。
    只有 embedding 被桩掉（见下），数据库 schema 是独立的；
  · 不进入 public 表（防自指污染）。

**embedding 是桩，这是与「真实向量」的差别，不是疏漏。** 桩向量是文本的确定性哈希，
相似度没有语义。但范围过滤发生在 SQL 里（`collection_id = ANY(允许集)`），与向量无关，
所以「forbidden 命中为 0」「可见集合恰好等于标注」仍是有效断言：只要某行被放进候选集，
不论排第几，都会被下面的集合比较抓到。**真实 embedding 链路不在测试范围，
列入未在 CI 验证**。
"""
import inspect
import itertools
import json
from pathlib import Path

import conftest
import pytest

from scoped_kb import chunking, cli, indexer, registry, scope, search
from scoped_kb import embed as embed_mod

CORPUS = Path(__file__).parent / "fixtures" / "canary"
OWNER = "canaryrepo"            # 位置键前缀，写在 fixtures/canary/collections.json 里

UNLISTED = ("canary-archive",)                      # active，但不在任何 profile 里
QUARANTINED = ("canary-inbox-held",)                # quarantine 分区，不进索引
NOT_IN_ANY_PROFILE = UNLISTED + QUARANTINED + ("canary-inbox",)   # inbox 基础集合本身也不对外


def _index_canary(reg, conn):
    """把 canary 语料经**完整生产管线**写进隔离 schema（embedding 由 fake_embed 提供）。"""
    conftest.seed_canary_schema(conn)
    indexer.activate(reg, conn, schema="canary", rebuild=True)


@pytest.fixture
def indexed_canary(canary_conn, canary_registry, fake_embed):
    reg, paths = canary_registry
    _index_canary(reg, canary_conn)
    return reg, paths


def _eff(reg, profile):
    return scope.Effective(profile, profile, (), "",
                           tuple(sorted(set(reg.allowed_for(profile)))),
                           reg.registry_hash)


def _cross_eff(reg, base, include):
    return scope.resolve_effective_profile(
        reg, reg.collections[f"canary-{base}"].roots[0], profile="cross",
        include=[include], why="canary 验证 cross 并集")


def _forbidden(reg, eff):
    allowed = set(eff.collections)
    return [c for c in reg.collections if c not in allowed]


def _truth():
    """人工标注：{ 相对路径: {"domain": …, "visible_to": […]} }。"""
    return json.loads((CORPUS / "ground_truth.json").read_text(encoding="utf-8"))


def _key(rel):
    return f"{OWNER}/{rel}"


def _all_md():
    return sorted(p.relative_to(CORPUS).as_posix() for p in CORPUS.rglob("*.md"))


def _retrieve(conn, reg, paths, eff, query="tidy-up log", limit=1000):
    return search.retrieve_candidates(conn, query, eff, limit, schema="canary",
                                      config_paths=paths)


# ---- canary 自身的硬约束 --------------------------------------------------

def test_canary_uses_production_pipeline_with_only_embedding_stubbed(indexed_canary):
    """管线没被绕过：索引走 activate → _rebuild_generation → scan/chunk，检索走生产 helper。

    这里**明确**只有 embedding 是桩——并且证明
    其余环节仍是生产对象，而不是 canary 自带的一份等价物。
    """
    src = inspect.getsource(_index_canary)
    assert "indexer.activate" in src, "管线被绕过了"
    code = src.split('"""', 2)[-1]
    assert "monkeypatch" not in code
    # activate 只做权限判断，机制在 _rebuild_generation——整条委派链都要跟到
    assert "_rebuild_generation(" in inspect.getsource(indexer.activate)
    assert "_chunks_for(reg)" in inspect.getsource(indexer._rebuild_generation)
    chunks = inspect.getsource(indexer._chunks_for)
    assert "scan(reg)" in chunks and "chunk(path)" in chunks

    # 运行时身份：这些符号就是生产模块里的那一份
    assert indexer.scan is chunking.scan and indexer.chunk is chunking.chunk
    assert cli.retrieve_candidates is search.retrieve_candidates
    # 唯一的桩：embedding（indexer 与 search 两处都被换成同一个假实现）
    assert indexer.embed is not embed_mod.embed
    assert indexer.embed is search.embed


def test_canary_registry_comes_from_production_compiler(canary_registry):
    reg, _paths = canary_registry
    assert isinstance(reg, registry.Registry)
    assert set(reg.collections) == {
        "canary-reading", "canary-cooking", "canary-common", "canary-archive",
        "canary-inbox", "canary-inbox-reading", "canary-inbox-held"}


def test_canary_not_in_production_index(indexed_canary, canary_conn):
    """canary 文件绝不能出现在 public.knowledge_index 里（防自指污染）。

    canary 已经被索引进 canary schema，此时 public 表（若测试库里建了）必须是空的。
    """
    cur = canary_conn.cursor()
    cur.execute("SELECT to_regclass('public.knowledge_index')")
    if cur.fetchone()[0] is None:
        return          # 测试库没建 public 表：不可能有泄漏
    cur.execute("SELECT count(*) FROM public.knowledge_index "
                "WHERE file_path LIKE %s OR collection_id LIKE %s",
                (f"{OWNER}/%", "canary-%"))
    assert cur.fetchone()[0] == 0, "canary 数据泄漏进了 public 表"


def test_every_file_has_manual_ground_truth():
    """标注清单与语料文件**双向**一一对应：不漏标，也没有指向不存在文件的标注。"""
    truth = _truth()
    assert sorted(truth["files"]) == _all_md()
    assert len(truth["files"]) == 13
    assert {f["domain"] for f in truth["files"].values()} == set(truth["domains"])
    for rel, entry in truth["files"].items():
        assert set(entry["visible_to"]) <= {"reading", "cooking"}, rel


# ---- 分类层：人工标注 ↔ registry 的归属 ---------------------------------

def test_manual_scope_matches_collection_scope(indexed_canary):
    """**直接断言人工标注对应的 collection scope。**

    标签是被检验的对象，不是证据——拿人工标注的域去比对 registry 给出的 scope，
    两者对不上就是归属判定错了。
    """
    reg, _paths = indexed_canary
    for rel, entry in _truth()["files"].items():
        cid = reg.collection_for_path(CORPUS / rel)
        assert cid is not None, f"{rel} 无归属"
        assert reg.collections[cid].scope == entry["domain"], (
            f"{rel}: 人工标注 {entry['domain']}，registry 判为 {reg.collections[cid].scope}")


def test_scoped_evidence_is_never_classified_as_shared(indexed_canary):
    """reading / cooking 的业务证据不得被归为公共（shared）。

    具体业务证据混进公共集合，就会对所有 profile 可见。
    """
    reg, _paths = indexed_canary
    for rel, entry in _truth()["files"].items():
        if entry["domain"] in ("reading", "cooking"):
            cid = reg.collection_for_path(CORPUS / rel)
            assert reg.collections[cid].scope != "shared", (
                f"{rel} 是 {entry['domain']} 侧证据，却被归为 shared")


def test_visible_set_equals_manual_annotation_for_every_profile(indexed_canary, canary_conn):
    """**双向**：每个 profile 读到的文件集合恰好等于人工标注的 visible_to。

    多一个是越界；少一个是隔离过度（读不到本该读到的）。cross 的并集同理。
    limit 取得足够大，使库里每个允许的分块都进候选集——任何过滤失效都躲不过。
    """
    reg, paths = indexed_canary
    truth = _truth()["files"]
    cases = [(profile, _eff(reg, profile), {profile}) for profile in ("reading", "cooking")]
    cases += [("reading+cooking", _cross_eff(reg, "reading", "cooking"), {"reading", "cooking"}),
              ("cooking+reading", _cross_eff(reg, "cooking", "reading"), {"reading", "cooking"})]
    for name, eff, profiles in cases:
        got = {r[0] for r in _retrieve(canary_conn, reg, paths, eff)}
        want = {_key(rel) for rel, e in truth.items() if profiles & set(e["visible_to"])}
        assert got == want, (f"{name}: 越界 {sorted(got - want)}；"
                             f"漏读 {sorted(want - got)}")


def test_shared_canary_is_visible_to_both_domains(indexed_canary, canary_conn):
    """反向确认：公共规则两个业务域都该读到——隔离不得过度执行成「什么都读不到」。"""
    reg, paths = indexed_canary
    for profile in ("reading", "cooking"):
        rows = _retrieve(canary_conn, reg, paths, _eff(reg, profile), "house rule")
        assert any(r[2] == "canary-common" for r in rows), f"{profile} 读不到 shared"


def test_reading_and_cooking_see_only_their_own_evidence(indexed_canary, canary_conn):
    reg, paths = indexed_canary
    for profile, own, other in (("reading", "canary-reading", "canary-cooking"),
                                ("cooking", "canary-cooking", "canary-reading")):
        rows = _retrieve(canary_conn, reg, paths, _eff(reg, profile), "tidy-up log", 100)
        cids = {r[2] for r in rows}
        assert own in cids
        assert other not in cids, f"{profile} 看到了 {other}"


def test_ground_truth_is_not_keyword_based():
    """两个域的碰撞文件同名、正文逐字相同——关键词与文件名判定在这里无从下手。"""
    pairs = sorted(p.name for p in (CORPUS / "reading").glob("*.md"))
    assert pairs == sorted(p.name for p in (CORPUS / "cooking").glob("*.md"))
    assert len(pairs) == 3
    for name in pairs:
        r = (CORPUS / "reading" / name).read_text(encoding="utf-8")
        c = (CORPUS / "cooking" / name).read_text(encoding="utf-8")
        assert r != c, "两侧必须有且仅有占位词不同，否则这一对不构成碰撞"
        assert r.replace("placeholder-reading", "X") == c.replace("placeholder-cooking", "X")


# ---- 不在任何 profile 里的集合 / quarantine 分区 -------------------------

def test_unlisted_and_quarantined_collections_are_in_no_profile(indexed_canary):
    reg, _paths = indexed_canary
    for profile in reg.profiles:
        for cid in NOT_IN_ANY_PROFILE:
            assert cid not in reg.allowed_for(profile)


def test_unlisted_and_quarantined_are_in_no_legal_cross_union(indexed_canary):
    """**任何合法 cross 组合的并集都不得含它们。**

    cross 只合并已授权的域，不解除 quarantine，也不会把没人授权的集合带进来。
    """
    reg, _paths = indexed_canary
    assert reg.cross_allowlist, "夹具必须真的配了 cross，否则这条什么也没测"
    for base, includes in reg.cross_allowlist:
        union = set(reg.allowed_for(base))
        for d in includes:
            union |= set(reg.allowed_for(d))
        for cid in NOT_IN_ANY_PROFILE:
            assert cid not in union, f"{base}+{sorted(includes)} 含 {cid}"


def test_quarantined_partition_is_never_indexed(indexed_canary, canary_conn):
    """quarantine 压根不进索引；没人确认的 inbox 文件一行都不在库里。"""
    cur = canary_conn.cursor()
    cur.execute("SELECT count(*) FROM canary.knowledge_index WHERE collection_id = ANY(%s)",
                (list(QUARANTINED),))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM canary.knowledge_index WHERE file_path LIKE %s",
                (f"{OWNER}/inbox/inbox-unreviewed-%",))
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM canary.knowledge_index WHERE file_path = %s",
                (_key("inbox/inbox-confirmed-1.md"),))
    assert cur.fetchone()[0] > 0, "人工确认过的 inbox 文件应当在索引里"


def test_unlisted_collection_is_indexed_but_unreachable(indexed_canary, canary_conn):
    """archive 是 active：它**真的在索引里**，挡住它的只有候选 SQL 的 collection 过滤。"""
    reg, paths = indexed_canary
    cur = canary_conn.cursor()
    cur.execute("SELECT count(*) FROM canary.knowledge_index WHERE collection_id = 'canary-archive'")
    assert cur.fetchone()[0] > 0, "前提不成立：archive 没进索引，这条就什么也没测"
    effs = [_eff(reg, p) for p in ("reading", "cooking")] + [
        _cross_eff(reg, "reading", "cooking"), _cross_eff(reg, "cooking", "reading")]
    for eff in effs:
        rows = _retrieve(canary_conn, reg, paths, eff, "closed project")
        assert all(r[2] != "canary-archive" for r in rows)


def test_unreachable_collections_stay_unreachable_even_if_rows_are_forced_in(
        indexed_canary, canary_conn):
    """双保险：即便有人把 quarantine 分区的行硬写进索引，候选 SQL 也不得返回它。"""
    reg, paths = indexed_canary
    cur = canary_conn.cursor()
    cur.execute("""
        INSERT INTO canary.knowledge_index
          (collection_id, source_agent, file_path, content,
           content_hash, embedding, embedding_model)
        SELECT 'canary-inbox-held', 'test', 'forced/held.md',
               content, 'f' || substr(content_hash, 2), embedding, embedding_model
        FROM canary.knowledge_index LIMIT 1""")
    canary_conn.commit()
    cur.execute("SELECT count(*) FROM canary.knowledge_index "
                "WHERE collection_id = 'canary-inbox-held'")
    assert cur.fetchone()[0] == 1, "前提不成立：强插的行没进去"
    effs = [_eff(reg, p) for p in ("reading", "cooking")] + [
        _cross_eff(reg, "reading", "cooking")]
    for eff in effs:
        rows = _retrieve(canary_conn, reg, paths, eff)
        assert all(r[2] not in NOT_IN_ANY_PROFILE for r in rows)
        assert all(r[0] != "forced/held.md" for r in rows)


# ---- 结构层：候选集 -> 选择 -> 渲染 -----------------------------------

def test_forbidden_zero_through_real_pack_pipeline(indexed_canary, canary_conn):
    """候选集 → 选择 → 渲染：三处分别由生产函数产出，不是同一份 rows 改名。

    最终上下文必须是 render_pack 真正渲染出来的文本；只把 rows 换个变量名
    叫「最终上下文」，等于没测最后一环。
    """
    reg, paths = indexed_canary
    cooking_files = {p.name for p in (CORPUS / "cooking").glob("*.md")}
    for profile in ("reading", "cooking"):
        eff = _eff(reg, profile)
        forb = set(_forbidden(reg, eff))
        candidates = _retrieve(canary_conn, reg, paths, eff, "tidy-up log", 100)
        assert candidates
        assert not [r for r in candidates if r[2] in forb], f"{profile} 候选集越界"

        picked, used, files = cli.select_for_pack(candidates, budget=100_000)
        assert picked
        assert not [p for p in picked if p[2] in forb], f"{profile} 选择后越界"

        pkg = cli.render_pack("tidy-up log", picked, used, files)
        if profile == "reading":
            for name in cooking_files:
                assert f"cooking/{name}" not in pkg, f"最终上下文含越界文件 {name}"
        for cid in forb:
            assert cid not in pkg, pkg[:200]


# ---- 对抗式检索 ------------------------------------------------------------

ADVERSARIAL_TERMS = [
    "tidy-up", "shelf", "label", "donate", "drawer",
    "box", "spare", "sort", "keep", "pile",
]


def _adversarial_queries(n):
    """查询由 canary 正文里的词组合生成，不是随机字符串。"""
    out = [f"{a} {b}" for a, b in itertools.permutations(ADVERSARIAL_TERMS, 2)]
    for a, b, c in itertools.permutations(ADVERSARIAL_TERMS, 3):
        if len(out) >= n:
            break
        out.append(f"{a} {b} {c}")
    assert len(set(out[:n])) == n, "对抗查询不得重复"
    return out[:n]


def test_adversarial_terms_come_from_the_corpus():
    text = " ".join(p.read_text(encoding="utf-8").lower() for p in CORPUS.rglob("*.md"))
    for term in ADVERSARIAL_TERMS:
        assert term in text, f"{term!r} 不在语料正文里"


def test_100_adversarial_scope_queries_hit_no_forbidden_collection(indexed_canary, canary_conn):
    """100 条对抗查询 × 每个 profile 与合法 cross：forbidden 命中总数为 0。

    **桩 embedding 下相似度没有语义，但这条断言依然有效**：范围过滤在 SQL 里
    （`collection_id = ANY(允许集)`），与向量无关。limit 取 100，大于库里的全部分块，
    于是每个允许的行都会进候选集；任何一行 forbidden 的行只要过得了过滤，就一定出现。
    """
    reg, paths = indexed_canary
    queries = _adversarial_queries(100)
    assert len(queries) == 100
    effs = [_eff(reg, p) for p in ("reading", "cooking")] + [
        _cross_eff(reg, "reading", "cooking"), _cross_eff(reg, "cooking", "reading")]

    forbidden_hits = 0
    total_rows = 0
    for q in queries:
        for eff in effs:
            rows = _retrieve(canary_conn, reg, paths, eff, q, 100)
            total_rows += len(rows)
            forb = set(_forbidden(reg, eff))
            forbidden_hits += sum(1 for r in rows if r[2] in forb)

    assert total_rows > 0, "对抗检索必须真的命中东西，否则等于没测"
    assert forbidden_hits == 0, f"{forbidden_hits} 次越界"


def test_cross_reaches_both_domains_but_stays_bounded(indexed_canary, canary_conn):
    """合法 cross 能同时看到两侧 + shared，但仍不越出并集（尤其不含 archive 与 quarantine）。"""
    reg, paths = indexed_canary
    eff = _cross_eff(reg, "reading", "cooking")
    rows = _retrieve(canary_conn, reg, paths, eff, "tidy-up log", 100)
    cids = {r[2] for r in rows}
    assert cids >= {"canary-reading", "canary-cooking", "canary-common"}
    assert not cids & set(NOT_IN_ANY_PROFILE)
    assert set(eff.collections) == {"canary-reading", "canary-cooking", "canary-common",
                                    "canary-inbox-reading"}


# ---- canary 数据不得进 public ----------------------------------------------

def test_canary_rows_never_land_in_public_table(indexed_canary, canary_conn):
    """方向不变量：canary 打的是 canary schema，它的行一条都不能落进 public。

    public 表若存在，其中 canary 的 collection_id 计数必须为 0。
    表结构本身（列、唯一索引）由 test_db_smoke 对着 schema.sql 断言，这里不重复。
    """
    cur = canary_conn.cursor()
    cur.execute("SELECT to_regclass('public.knowledge_index')")
    if cur.fetchone()[0] is None:
        return
    cur.execute("SELECT count(*) FROM public.knowledge_index "
                "WHERE collection_id LIKE 'canary-%'")
    assert cur.fetchone()[0] == 0, "canary 数据泄漏进 public 表"
