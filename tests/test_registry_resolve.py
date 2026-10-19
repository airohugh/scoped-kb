"""文件归属解析、active roots、dry-run 全量分类。"""
from scoped_kb import registry


def test_path_maps_to_owning_collection(fixture_paths, fixture_base):
    reg = registry.compile_registry(fixture_paths)
    assert reg.collection_for_path(
        fixture_base / "repo_a/docs/decisions/a.md") == "a-decisions"
    assert reg.collection_for_path(
        fixture_base / "repo_b/notes/b.md") == "b-notes"


def test_excluded_subdir_is_not_claimed(fixture_paths, fixture_base):
    """excludes 命中 = 明确排除，不是「没归属」。"""
    reg = registry.compile_registry(fixture_paths)
    assert reg.collection_for_path(fixture_base / "repo_b/notes/raw/skip.md") is None


def test_unregistered_path_returns_none(fixture_paths, tmp_path):
    reg = registry.compile_registry(fixture_paths)
    assert reg.collection_for_path(tmp_path / "elsewhere/q.md") is None


def test_manifest_assigned_file_goes_to_named_partition(fixture_paths, fixture_base):
    reg = registry.compile_registry(fixture_paths)
    g = fixture_base / "machine/global"
    assert reg.collection_for_path(g / "confirmed.md") == "m-global"
    assert reg.collection_for_path(g / "mixed_work.md") == "m-global-work"


def test_unlisted_manifest_file_defaults_to_quarantine(fixture_paths, fixture_base):
    """放置位置不授予权限：没被人确认的文件一律落 review-only quarantine。"""
    reg = registry.compile_registry(fixture_paths)
    cid = reg.collection_for_path(fixture_base / "machine/global/never_reviewed.md")
    assert cid == "m-global-unresolved"
    assert reg.collections[cid].index_policy == "quarantine"


def test_unresolved_partition_is_in_no_profile(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    for profile in reg.profiles:
        assert "m-global-unresolved" not in reg.allowed_for(profile)


def test_active_roots_include_confirmed_manifest_root(fixture_paths, fixture_base):
    reg = registry.compile_registry(fixture_paths)
    roots = reg.active_roots()
    assert (fixture_base / "repo_a/docs/decisions").resolve() in roots
    assert (fixture_base / "machine/global").resolve() in roots   # 要扫，逐文件裁定


def test_virtual_partitions_contribute_no_roots(fixture_paths):
    """虚拟分区无独立 root，不得让 active_roots 多出条目。"""
    reg = registry.compile_registry(fixture_paths)
    assert len(reg.active_roots()) == 3      # repo_a/docs/decisions, repo_b/notes, machine/global


def test_classify_all_leaves_nothing_unclassified(fixture_paths):
    rep = registry.classify_all(registry.compile_registry(fixture_paths))
    assert rep["unclassified"] == []
    assert any(cid == "m-global-unresolved" for _p, cid, _r in rep["quarantine"])
    for _path, _cid, reason in rep["quarantine"]:
        assert reason, "quarantine 必须带原因"


def test_classify_all_buckets_every_real_file(fixture_paths):
    """每个文件**且只能**落进 active / excluded / quarantine 三类之一。

    命中 excludes 的文件属于「明确 excluded」，必须进桶并带原因——
    不进任何桶等于静默漏项，正是要杜绝的。
    """
    rep = registry.classify_all(registry.compile_registry(fixture_paths))
    buckets = ("active", "quarantine", "excluded")
    seen = [p.name for b in buckets for p, _c, _r in rep[b]]
    assert sorted(seen) == sorted([
        "a.md", "b.md", "confirmed.md", "mixed_work.md", "never_reviewed.md", "skip.md"])
    assert len(seen) == len(set(seen)), "同一文件不得落进多个桶"
    assert {p.name for p, _c, _r in rep["active"]} == {
        "a.md", "b.md", "confirmed.md", "mixed_work.md"}
    assert {p.name for p, _c, _r in rep["quarantine"]} == {"never_reviewed.md"}
    assert {p.name for p, _c, _r in rep["excluded"]} == {"skip.md"}


def test_excluded_entries_carry_reason(fixture_paths):
    rep = registry.classify_all(registry.compile_registry(fixture_paths))
    for _p, _cid, reason in rep["excluded"]:
        assert reason, "excluded 也要带原因，否则无法复核为什么它不在索引里"


def test_quarantine_entries_carry_exit_path(fixture_paths):
    """没有退出路径的 quarantine 会积压成只进不出的黑洞。"""
    reg = registry.compile_registry(fixture_paths)
    c = reg.collections["m-global-unresolved"]
    assert c.quarantine_reason == "unresolved-mixed"
    assert c.exit_path


def test_all_roots_superset_of_active_roots(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert set(reg.active_roots()) <= set(reg.all_roots())
