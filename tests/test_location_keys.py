"""位置键：库里的 file_path 是 "<owner>/<相对路径>"，与 $HOME、与笔记放在哪台机器无关。

owner 的规则：
  · 仓库声明里的 collection → repo_id，相对路径相对 repo_root；
  · machine-sources 里的 collection → 它自己的 collection_id，相对路径相对它的 root；
  · manifest 分区 → 沿用基础 collection 的 owner（文件在分区之间换归属时键不变）。
"""
import conftest
import pytest

from scoped_kb import chunking, cli, indexer, provenance, registry, scope


def _keys(reg):
    return {key for _cid, key, _p in chunking.scan(reg)}


def _paths_in_db(conn):
    cur = conn.cursor()
    cur.execute("SELECT file_path FROM canary.knowledge_index ORDER BY file_path")
    return [r[0] for r in cur.fetchall()]


# ---- 键的形状 -----------------------------------------------------------

def test_repo_and_machine_collections_get_owner_relative_keys(fixture_paths):
    """仓库 collection 用 repo_id、machine collection 用自己的 id；分区共享基础的 owner。"""
    reg = registry.compile_registry(fixture_paths)
    by_name = {p.name: (cid, key) for cid, key, p in chunking.scan(reg)}
    assert by_name["a.md"] == ("a-decisions", "repo_a/docs/decisions/a.md")
    assert by_name["b.md"] == ("b-notes", "repo_b/notes/b.md")
    assert by_name["confirmed.md"] == ("m-global", "m-global/confirmed.md")
    # mixed_work.md 属于虚拟分区 m-global-work，但键的 owner 仍是基础 collection
    assert by_name["mixed_work.md"] == ("m-global-work", "m-global/mixed_work.md")


def test_partitions_share_the_base_owner(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    base = reg.collections["m-global"].location_owner
    assert base == "m-global"
    for pid in ("m-global-work", "m-global-unresolved"):
        assert reg.collections[pid].location_owner == base
    assert reg.collections["a-decisions"].location_owner == "repo_a"
    assert set(reg.location_bases) == {"repo_a", "repo_b", "m-global"}


def test_location_key_roundtrips_through_resolve_location(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    scanned = chunking.scan(reg)
    assert scanned
    for cid, key, path in scanned:
        assert registry.location_key(reg, cid, path) == key
        assert registry.resolve_location(reg, key) == path


def test_location_key_ignores_how_the_parent_directory_is_spelled(fixture_paths, fixture_base):
    """传入未规范化的父目录（含 ..）也得到同一个键。"""
    reg = registry.compile_registry(fixture_paths)
    messy = fixture_base / "repo_a" / "docs" / ".." / "docs" / "decisions" / "a.md"
    assert registry.location_key(reg, "a-decisions", messy) == "repo_a/docs/decisions/a.md"


# ---- ① 笔记不在 HOME 下，也能索引与读回 --------------------------------

def test_notes_outside_home_are_indexed_and_read_back(canary_conn, fake_embed, corpus,
                                                      tmp_path, monkeypatch):
    elsewhere = tmp_path / "some-other-home"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(elsewhere))     # HOME 指到与语料毫无关系的地方
    assert not str(corpus.root).startswith(str(elsewhere))

    (corpus.root / "repo_a/docs/decisions/fm.md").write_text(
        "---\ntrust_tier: T1\ncaptured_at: 2026-01-02\n---\n\n"
        "# 带出处的判例\n\n正文必须超过分块的 60 字下限才会被收录，所以这里多写几句："
        "本文件仅用于验证位置键读回 frontmatter，内容全部是虚构的示例文字。\n",
        encoding="utf-8")
    reg = registry.compile_registry(corpus.paths())
    conftest.seed_canary_schema(canary_conn)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)

    stored = _paths_in_db(canary_conn)
    assert "repo_a/docs/decisions/fm.md" in stored
    for fp in stored:
        assert str(corpus.root) not in fp and not fp.startswith("/"), fp
        assert registry.resolve_location(reg, fp).is_file(), fp
    # 读回 frontmatter 也经位置键
    assert provenance.read_provenance(reg, "repo_a/docs/decisions/fm.md") == {
        "trust_tier": "T1", "captured_at": "2026-01-02"}
    line = provenance.provenance_line("repo_a/docs/decisions/fm.md", "a-decisions", reg)
    assert "T1 官方/一手" in line and "2026-01-02" in line


def test_keys_do_not_change_when_the_corpus_moves(corpus, make_paths, tmp_path):
    """换机器、换用户名：同一份语料放到另一个绝对路径下，库里存的键逐字相同。"""
    import shutil
    reg1 = registry.compile_registry(corpus.paths())
    moved = (tmp_path / "another" / "place").resolve()
    shutil.copytree(corpus.root, moved)
    reg2 = registry.compile_registry(make_paths(base=moved))

    assert reg1.location_bases != reg2.location_bases
    assert _keys(reg1) == _keys(reg2) and _keys(reg1)
    # 基准目录进世代：搬家后旧索引会被自动重建，不会带着失效的键继续用
    assert reg1.registry_hash != reg2.registry_hash


def test_repo_root_change_with_same_roots_changes_keys_and_hash(make_paths, fixture_paths):
    """repo_root 下移一层、roots 相应缩短：扫到的文件、指纹都不变，位置键却变了。

    这种改动只有 location_bases 能让世代跟着变——否则旧索引里存的键会静默失效，
    检索还照常通过世代校验。
    """
    def shift(d):
        d["repo_root"] += "/docs"
        d["collections"][0]["roots"] = ["decisions"]

    reg0 = registry.compile_registry(fixture_paths)
    reg1 = registry.compile_registry(make_paths(repos=shift))
    assert reg0.collections["a-decisions"].fingerprint == reg1.collections["a-decisions"].fingerprint
    assert reg0.collections["a-decisions"].roots == reg1.collections["a-decisions"].roots
    assert "repo_a/docs/decisions/a.md" in _keys(reg0)
    assert "repo_a/decisions/a.md" in _keys(reg1)
    assert reg0.registry_hash != reg1.registry_hash


# ---- ③ 文件在两个 manifest 分区之间换归属 ------------------------------

def _reassign(rel, target):
    def patch(d):
        d["manifest_partitions"]["m-global"]["assignments"][rel] = target
    return patch


def test_partition_move_keeps_the_key_and_leaves_no_stale_row(canary_conn, fake_embed,
                                                              corpus):
    reg1 = registry.compile_registry(corpus.paths())
    conftest.seed_canary_schema(canary_conn)
    indexer.activate(reg1, canary_conn, schema="canary", rebuild=True)
    before = _paths_in_db(canary_conn)
    cur = canary_conn.cursor()
    cur.execute("SELECT collection_id FROM canary.knowledge_index "
                "WHERE file_path = 'm-global/mixed_work.md'")
    assert cur.fetchall() == [("m-global-work",)]

    # 把 mixed_work.md 从 m-global-work 改派到 m-global（两个分区之间换归属）
    reg2 = registry.compile_registry(
        corpus.paths(scope_policy=_reassign("mixed_work.md", "m-global")))
    assert reg2.registry_hash != reg1.registry_hash
    fake_embed.clear()
    stats = indexer._incremental_index(reg2, canary_conn, schema="canary")

    # 键没变 → 增量逻辑按 file_path 找到旧行，只更新归属，不删不重算
    assert _paths_in_db(canary_conn) == before, "换归属后 file_path 集合必须逐字相同"
    assert stats["recategorised"] == 1
    assert stats["embedded"] == 0 and fake_embed == []
    assert stats["removed_files"] == 0
    cur.execute("SELECT collection_id FROM canary.knowledge_index "
                "WHERE file_path = 'm-global/mixed_work.md'")
    assert cur.fetchall() == [("m-global",)], "按 file_path 只剩一份，且归属是新的"


def test_partition_move_into_quarantine_removes_the_rows(canary_conn, fake_embed, corpus):
    """从 active 分区改派回默认的 quarantine 分区：文件不再被扫到，旧行必须清掉。"""
    reg1 = registry.compile_registry(corpus.paths())
    conftest.seed_canary_schema(canary_conn)
    indexer.activate(reg1, canary_conn, schema="canary", rebuild=True)
    assert "m-global/mixed_work.md" in _paths_in_db(canary_conn)

    reg2 = registry.compile_registry(
        corpus.paths(scope_policy=_reassign("mixed_work.md", "m-global-unresolved")))
    indexer._incremental_index(reg2, canary_conn, schema="canary")
    assert "m-global/mixed_work.md" not in _paths_in_db(canary_conn)


# ---- ④ owner 撞名 / 非法被拒 -------------------------------------------

def test_repo_id_colliding_with_machine_collection_id_is_rejected(make_paths):
    def patch(d):
        d["repo_id"] = "m-global"
    with pytest.raises(registry.RegistryError, match="撞名"):
        registry.compile_registry(make_paths(repos=patch))


def test_same_repo_id_with_different_repo_roots_is_rejected(make_paths):
    """两份仓库声明用同一个 repo_id、指向不同目录：同一个前缀对应两个基准目录。"""
    def patch(d):
        d["repo_id"] = "repo_b"          # 与第二份声明的 repo_id 相同，repo_root 不同
    with pytest.raises(registry.RegistryError, match="repo_id 重复"):
        registry.compile_registry(make_paths(repos=patch))


@pytest.mark.parametrize("owner", ["a/b", "", "  ", 7])
def test_owner_must_be_a_nonempty_string_without_slash(make_paths, owner):
    def patch(d):
        d["repo_id"] = owner
    with pytest.raises(registry.RegistryError):
        registry.compile_registry(make_paths(repos=patch))


def test_machine_collection_id_with_slash_is_rejected(make_paths):
    def patch(d):
        d["collections"][0]["collection_id"] = "m/global"
    with pytest.raises(registry.RegistryError, match="owner 非法"):
        registry.compile_registry(make_paths(machine_sources=patch))


# ---- ⑤ resolve_location 不猜 -------------------------------------------

def test_resolve_location_unknown_owner_raises(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    with pytest.raises(registry.RegistryError, match="owner 未知"):
        registry.resolve_location(reg, "nobody/a.md")


@pytest.mark.parametrize("key", ["", "repo_a", "repo_a/", "/repo_a/a.md",
                                 "repo_a//etc/passwd", "repo_a/../repo_b/notes/b.md",
                                 "repo_a/docs/../../repo_b/notes/b.md"])
def test_resolve_location_rejects_malformed_or_escaping_keys(fixture_paths, key):
    reg = registry.compile_registry(fixture_paths)
    with pytest.raises(registry.RegistryError):
        registry.resolve_location(reg, key)


def test_location_key_outside_base_or_for_unknown_collection_raises(fixture_paths, tmp_path):
    reg = registry.compile_registry(fixture_paths)
    with pytest.raises(registry.RegistryError, match="不在 owner"):
        registry.location_key(reg, "a-decisions", tmp_path / "stray.md")
    with pytest.raises(registry.RegistryError, match="owner"):
        registry.location_key(reg, "没有这个-collection", tmp_path / "stray.md")


# ---- ⑥ search 输出里是可以直接打开的绝对路径 ----------------------------

@pytest.fixture
def indexed_search(canary_conn, fake_embed, corpus, install_config, trace_vars,
                   monkeypatch, tmp_path, gate):
    """在可写语料副本上真建一份索引，再让 cmd_search 打这份 canary 索引。"""
    install_config(base=corpus.root, config=trace_vars.config)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.chdir(corpus.root / "repo_a")
    monkeypatch.setenv(trace_vars.task, "task-1")
    monkeypatch.setenv(trace_vars.sessions[0], "sess-1")
    gate(None)
    reg = registry.compile_registry()
    conftest.seed_canary_schema(canary_conn)
    indexer.activate(reg, canary_conn, schema="canary", rebuild=True)

    real = cli.retrieve_candidates
    monkeypatch.setattr(cli, "connect", lambda: canary_conn)
    monkeypatch.setattr(cli, "retrieve_candidates",
                        lambda conn, q, eff, limit: real(conn, q, eff, limit, schema="canary"))
    return reg


def _args(**kw):
    import argparse
    base = {"query": "判例", "limit": 5, "profile": None, "include": None, "why": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_search_prints_openable_absolute_paths(indexed_search, corpus, capsys):
    cli.cmd_search(_args())
    out = capsys.readouterr().out
    expected = str(corpus.root / "repo_a/docs/decisions/a.md")
    assert expected in out
    assert "repo_a/docs/decisions/a.md" in out
    assert "出处：" in out


def test_search_json_has_absolute_file_path(indexed_search, corpus, capsys):
    import json
    cli.cmd_search(_args(json=True))
    got = json.loads(capsys.readouterr().out)
    assert got
    for item in got:
        assert item["file_path"].startswith(str(corpus.root))
        with open(item["file_path"], encoding="utf-8") as f:       # 调用方能直接打开
            assert f.read()


def test_search_refuses_when_a_stored_key_cannot_be_resolved(indexed_search, canary_conn,
                                                             capsys):
    """库里有解析不了的位置键（索引与配置对不上）：不猜，按世代不一致处理并退出 3。"""
    cur = canary_conn.cursor()
    cur.execute("UPDATE canary.knowledge_index SET file_path = 'ghost/' || "
                "split_part(file_path, '/', 2) || '/x' WHERE collection_id = 'a-decisions'")
    canary_conn.commit()
    with pytest.raises(SystemExit) as exc:
        cli.cmd_search(_args())
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    err = capsys.readouterr().err
    assert "位置键无法解析" in err and "scoped-kb index" in err
