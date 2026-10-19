"""位置键与扫描不得经符号链接逃出基准目录。

威胁：已索引的文件被换成指向另一个域的符号链接。registry hash 不变，
字符串层面的位置键检查也照样通过，但读出来的是域外内容——
索引时会把域外内容索引进本域，读出处时会把域外文件的 source_url 输出给调用方。
边界检查必须落在**解析后的真实路径**上，而且越界时 fail-closed，不猜。
"""
import argparse

import pytest

from scoped_kb import chunking, cli, indexer, provenance, registry

FOREIGN_MARK = "FOREIGN-DOMAIN-MARKER"
FOREIGN_URL = "https://foreign.example.org/secret-source"

BODY = ("正文必须超过分块的 60 字下限才会被收录，所以这里多写几句："
        "本文件内容全部是虚构的示例文字，仅用于验证符号链接边界，写到这个长度就够了。\n")


@pytest.fixture
def world(corpus):
    """可写语料副本 + 一个在 repo_b 里的“域外”文件（带 source_url 与可识别标记）。"""
    foreign = corpus.root / "repo_b/notes/foreign.md"
    foreign.write_text(
        f"---\nsource_url: {FOREIGN_URL}\ntrust_tier: T1\n---\n\n"
        f"# {FOREIGN_MARK}\n\n{FOREIGN_MARK} {BODY}", encoding="utf-8")
    own = corpus.root / "repo_a/docs/decisions"
    return corpus, own, foreign


def _reg(corpus):
    return registry.compile_registry(corpus.paths())


def _keys(reg):
    return {key for _cid, key, _p in chunking.scan(reg)}


# ---- resolve_location：真实路径越界一律报错 -------------------------------

def test_resolve_location_rejects_file_symlink_out_of_base(world):
    corpus, own, foreign = world
    (own / "link.md").symlink_to(foreign)
    reg = _reg(corpus)
    with pytest.raises(registry.RegistryError, match="符号链接|基准目录"):
        registry.resolve_location(reg, "repo_a/docs/decisions/link.md")


def test_resolve_location_rejects_path_through_dir_symlink(world):
    corpus, own, foreign = world
    (own / "dirlink").symlink_to(foreign.parent, target_is_directory=True)
    reg = _reg(corpus)
    with pytest.raises(registry.RegistryError, match="符号链接|基准目录"):
        registry.resolve_location(reg, "repo_a/docs/decisions/dirlink/foreign.md")


def test_resolve_location_rejects_dangling_link_out_of_base(world, tmp_path):
    """目标不存在也不放行：真实路径（非严格解析）仍在基准目录之外。"""
    corpus, own, _foreign = world
    (own / "gone.md").symlink_to(tmp_path / "nowhere" / "x.md")
    reg = _reg(corpus)
    with pytest.raises(registry.RegistryError):
        registry.resolve_location(reg, "repo_a/docs/decisions/gone.md")


def test_resolve_location_rejects_symlink_loop(world):
    corpus, own, _foreign = world
    (own / "loop1.md").symlink_to(own / "loop2.md")
    (own / "loop2.md").symlink_to(own / "loop1.md")
    reg = _reg(corpus)
    with pytest.raises(registry.RegistryError):
        registry.resolve_location(reg, "repo_a/docs/decisions/loop1.md")


def test_resolve_location_allows_link_to_another_file_inside_base(world):
    """合法用法不误伤：基准目录内部的链接照常可用。"""
    corpus, own, _foreign = world
    (own / "alias.md").symlink_to(own / "a.md")
    reg = _reg(corpus)
    got = registry.resolve_location(reg, "repo_a/docs/decisions/alias.md")
    assert got.read_text(encoding="utf-8") == (own / "a.md").read_text(encoding="utf-8")


# ---- read_provenance：不读域外文件 -----------------------------------------

def test_read_provenance_does_not_follow_link_out_of_base(world):
    corpus, own, foreign = world
    (own / "link.md").symlink_to(foreign)
    reg = _reg(corpus)
    got = provenance.read_provenance(reg, "repo_a/docs/decisions/link.md")
    assert got == {}
    line = provenance.provenance_line("repo_a/docs/decisions/link.md", "a-decisions", reg)
    assert FOREIGN_URL not in line and "foreign.example.org" not in line


def test_read_provenance_swapped_after_indexing(world):
    """已索引的文件事后被换成链接：hash 不变，读出处仍不得跟出去。"""
    corpus, own, foreign = world
    target = own / "swap.md"
    target.write_text(f"---\ntrust_tier: T2\n---\n\n# 自己的判例\n\n{BODY}", encoding="utf-8")
    reg = _reg(corpus)
    key = "repo_a/docs/decisions/swap.md"
    assert provenance.read_provenance(reg, key) == {"trust_tier": "T2"}
    target.unlink()
    target.symlink_to(foreign)
    assert _reg(corpus).registry_hash == reg.registry_hash
    assert provenance.read_provenance(reg, key) == {}


def test_read_provenance_allows_link_inside_base(world):
    corpus, own, _foreign = world
    (own / "src.md").write_text(
        f"---\ntrust_tier: T3\n---\n\n# 来源\n\n{BODY}", encoding="utf-8")
    (own / "alias2.md").symlink_to(own / "src.md")
    reg = _reg(corpus)
    assert provenance.read_provenance(reg, "repo_a/docs/decisions/alias2.md") == {
        "trust_tier": "T3"}


# ---- read_provenance：owner 边界不等于 collection / 分区边界 -------------------
#
# m-global / m-global-work / m-global-unresolved 三个分区共用同一个 owner 与基准目录，
# 所以“真实路径仍在 owner 基准目录内”不能证明读到的文件还属于索引行的那个 collection。

QUARANTINED_URL = "https://quarantined.example.org/never-reviewed"


def _machine_global(corpus):
    g = corpus.root / "machine/global"
    (g / "never_reviewed.md").write_text(
        f"---\nsource_url: {QUARANTINED_URL}\ntrust_tier: T1\n---\n\n# 隔离区文件\n\n{BODY}",
        encoding="utf-8")
    return g


def test_read_provenance_rejects_link_into_quarantine_partition(world):
    """索引后 confirmed.md 被换成指向同目录 never_reviewed.md（quarantine）的链接：
    hash 不变、owner 边界也过，但出处不得输出隔离文件的 frontmatter。"""
    corpus, _own, _foreign = world
    g = _machine_global(corpus)
    reg = _reg(corpus)
    key = "m-global/confirmed.md"
    (g / "confirmed.md").unlink()
    (g / "confirmed.md").symlink_to(g / "never_reviewed.md")
    assert _reg(corpus).registry_hash == reg.registry_hash
    assert registry.resolve_location(reg, key).is_file(), "owner 边界挡不住这种换法"
    line = provenance.provenance_line(key, "m-global", reg)
    assert QUARANTINED_URL not in line and "quarantined.example.org" not in line
    assert "T1" not in line, "隔离文件声明的 trust_tier 不得出现"
    assert provenance.read_provenance(reg, key, collection_id="m-global") == {}


def test_read_provenance_rejects_link_into_other_collection_same_owner(world):
    """同 owner、不同 collection（m-global → m-global-work，两者都可索引）也不读。"""
    corpus, _own, _foreign = world
    g = corpus.root / "machine/global"
    (g / "mixed_work.md").write_text(
        f"---\nsource_url: {QUARANTINED_URL}\ntrust_tier: T2\n---\n\n# 另一个分区\n\n{BODY}",
        encoding="utf-8")
    reg = _reg(corpus)
    assert reg.collections["m-global-work"].index_policy in registry.INDEXED_POLICIES
    (g / "confirmed.md").unlink()
    (g / "confirmed.md").symlink_to(g / "mixed_work.md")
    assert provenance.read_provenance(
        reg, "m-global/confirmed.md", collection_id="m-global") == {}
    # 对照：以它真正归属的 collection 读，照常可读
    assert provenance.read_provenance(
        reg, "m-global/mixed_work.md", collection_id="m-global-work") == {
        "source_url": QUARANTINED_URL, "trust_tier": "T2"}


def test_read_provenance_rejects_row_whose_collection_is_no_longer_indexed(world):
    """索引行记的 collection 与当前归属一致、但当前归属已是 quarantine：不读。"""
    corpus, _own, _foreign = world
    _machine_global(corpus)
    reg = _reg(corpus)
    assert reg.collections["m-global-unresolved"].index_policy not in registry.INDEXED_POLICIES
    assert provenance.read_provenance(
        reg, "m-global/never_reviewed.md", collection_id="m-global-unresolved") == {}


def test_read_provenance_allows_link_inside_same_collection(world):
    """合法用法不误伤：同一 collection 内链接到另一个文件，带 collection_id 也照常可读。"""
    corpus, own, _foreign = world
    (own / "src.md").write_text(
        f"---\ntrust_tier: T3\n---\n\n# 来源\n\n{BODY}", encoding="utf-8")
    (own / "alias3.md").symlink_to(own / "src.md")
    reg = _reg(corpus)
    assert provenance.read_provenance(
        reg, "repo_a/docs/decisions/alias3.md", collection_id="a-decisions") == {
        "trust_tier": "T3"}
    assert "T3" in provenance.provenance_line(
        "repo_a/docs/decisions/alias3.md", "a-decisions", reg)


# ---- scan：根内链接指向根外 → 不索引、且可见 --------------------------------

def test_scan_skips_file_link_pointing_outside_root_and_reports_it(world):
    corpus, own, foreign = world
    (own / "link.md").symlink_to(foreign)
    reg = _reg(corpus)
    out = chunking.scan(reg)
    assert "repo_a/docs/decisions/link.md" not in {k for _c, k, _p in out}
    assert [p.name for p in out.skipped] == ["link.md"], "跳过要可见，不静默"


def test_scan_skips_dir_link_pointing_outside_root(world):
    corpus, own, foreign = world
    (own / "dirlink").symlink_to(foreign.parent, target_is_directory=True)
    reg = _reg(corpus)
    keys = _keys(reg)
    assert not any(k.startswith("repo_a/docs/decisions/dirlink/") for k in keys)
    # 即使该目录链接被遍历到，也只会因越界被跳过，不会进索引
    assert all("foreign.md" not in k or k.startswith("repo_b/") for k in keys)


def test_scan_skips_link_to_other_collection_in_same_owner(world):
    """指向同一仓库里另一个 collection 的文件同样越界：边界是该 collection 自己的根。"""
    corpus, own, _foreign = world
    other = corpus.root / "repo_a/docs/other"
    other.mkdir(parents=True)
    (other / "o.md").write_text(f"# 别的域\n\n{FOREIGN_MARK} {BODY}", encoding="utf-8")
    (own / "olink.md").symlink_to(other / "o.md")
    reg = _reg(corpus)
    out = chunking.scan(reg)
    assert "repo_a/docs/decisions/olink.md" not in {k for _c, k, _p in out}
    assert [p.name for p in out.skipped] == ["olink.md"]


def test_scan_keeps_link_to_another_file_inside_root(world):
    corpus, own, _foreign = world
    (own / "alias.md").symlink_to(own / "a.md")
    reg = _reg(corpus)
    out = chunking.scan(reg)
    assert "repo_a/docs/decisions/alias.md" in {k for _c, k, _p in out}
    assert out.skipped == []


def test_chunks_never_contain_foreign_content(world):
    corpus, own, foreign = world
    (own / "link.md").symlink_to(foreign)
    (own / "dirlink").symlink_to(foreign.parent, target_is_directory=True)
    reg = _reg(corpus)
    chunks = indexer._chunks_for(reg)
    own_chunks = [c for cid, _k, c, _h in chunks if cid == "a-decisions"]
    assert own_chunks, "自己的内容仍应被索引"
    assert not any(FOREIGN_MARK in c for c in own_chunks)
    assert len(chunks.skipped) >= 1


def test_index_stats_report_skipped_links(world, canary_conn, fake_embed):
    import conftest
    corpus, own, foreign = world
    (own / "link.md").symlink_to(foreign)
    reg = _reg(corpus)
    conftest.seed_canary_schema(canary_conn)
    stats = indexer._rebuild_generation(reg, canary_conn, schema="canary")
    assert stats["skipped_links"] == 1
    cur = canary_conn.cursor()
    cur.execute("SELECT file_path FROM canary.knowledge_index "
                "WHERE file_path LIKE 'repo_a/%link.md'")
    assert cur.fetchall() == []
    stats = indexer._incremental_index(reg, canary_conn, schema="canary")
    assert stats["skipped_links"] == 1


def test_cmd_index_prints_skipped_links(monkeypatch, capsys, world):
    corpus, _own, _foreign = world
    reg = _reg(corpus)          # 先编译：下面会替换 compile_registry
    monkeypatch.setattr(cli, "connect", lambda: object())
    monkeypatch.setattr(registry, "compile_registry", lambda *a, **k: reg)
    monkeypatch.setattr(indexer, "read_active_hash", lambda conn, schema="public": None)
    monkeypatch.setattr(
        indexer, "index_with_registry",
        lambda reg, conn: {"files": 1, "chunks": 1, "embedded": 1, "rebuilt": True,
                           "registry_hash": "0" * 64, "skipped_links": 2})
    monkeypatch.setattr(cli, "cmd_status", lambda args, conn=None: None)
    cli._index_locked(argparse.Namespace())
    out = capsys.readouterr().out
    assert "2" in out and "符号链接" in out
