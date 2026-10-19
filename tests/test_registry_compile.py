"""registry 编译与确定性 hash。"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import platformdirs
import pytest

from scoped_kb import registry, settings

SRC_DIR = str(Path(registry.__file__).resolve().parent.parent)


def test_compile_returns_all_collections(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert set(reg.collections) == {
        "a-decisions", "b-notes", "m-global", "m-global-work", "m-global-unresolved"}


def test_roots_are_absolute_and_resolved(fixture_paths, fixture_base):
    reg = registry.compile_registry(fixture_paths)
    assert reg.collections["a-decisions"].roots == (
        (fixture_base / "repo_a/docs/decisions").resolve(),)
    assert reg.collections["b-notes"].roots == (
        (fixture_base / "repo_b/notes").resolve(),)


def test_virtual_partitions_have_no_root_and_inherit_fingerprint(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    base = reg.collections["m-global"]
    for cid in ("m-global-work", "m-global-unresolved"):
        assert reg.collections[cid].roots == ()
        assert reg.collections[cid].fingerprint == base.fingerprint


def test_profiles_expose_allowed_ids(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert reg.allowed_for("work") == ("a-decisions", "m-global", "m-global-work")
    assert reg.allowed_for("research") == ("b-notes", "m-global")


def test_allowed_for_unknown_profile_raises(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    with pytest.raises(registry.RegistryError):
        reg.allowed_for("nope")


def test_hash_is_deterministic(fixture_paths):
    a = registry.compile_registry(fixture_paths)
    b = registry.compile_registry(fixture_paths)
    assert a.registry_hash == b.registry_hash
    assert len(a.registry_hash) == 64


def test_hash_changes_when_policy_changes(fixture_paths, make_paths):
    """漂移必须由真实改写配置文件制造，让编译器重走一遍完整流程。

    禁止用 object.__setattr__ 直接改 frozen dataclass 的 registry_hash——
    那绕过编译器，测的是断言自己，不是真实漂移路径。
    """
    a = registry.compile_registry(fixture_paths)
    drifted = make_paths(scope_policy=lambda d: d["profiles"].__setitem__(
        "work", ["a-decisions", "m-global"]))       # 去掉 m-global-work，合法但不同
    b = registry.compile_registry(drifted)
    assert a.registry_hash != b.registry_hash


def test_default_paths_resolve_from_config_dir(tmp_path, monkeypatch):
    """默认配置路径来自 settings.CONFIG_DIR，与 checkout 位置、HOME 无关。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path)
    got = registry.default_paths()
    assert got["scope_policy"] == tmp_path / "scope-policy.json"
    assert got["machine_sources"] == tmp_path / "machine-sources.json"
    assert got["config"] == tmp_path / "config.json"
    assert got["repos"] == []                       # config.json 缺失 → 空列表


def test_default_paths_are_computed_at_call_time(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    monkeypatch.setattr(settings, "CONFIG_DIR", first)
    assert registry.default_paths()["scope_policy"] == first / "scope-policy.json"
    monkeypatch.setattr(settings, "CONFIG_DIR", second)
    assert registry.default_paths()["scope_policy"] == second / "scope-policy.json"


def test_compile_without_paths_reads_the_config_dir(fixture_paths, tmp_path, monkeypatch):
    """compile_registry() 不传路径时，三份文件都从 settings.CONFIG_DIR 读。"""
    cfg_dir = tmp_path / "userconf"
    cfg_dir.mkdir()
    shutil.copy(fixture_paths["scope_policy"], cfg_dir / "scope-policy.json")
    shutil.copy(fixture_paths["machine_sources"], cfg_dir / "machine-sources.json")
    (cfg_dir / "config.json").write_text(json.dumps({
        "schema_version": 1, "repos": [str(p) for p in fixture_paths["repos"]],
        "skip_filenames": ["a.md"]}), encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_DIR", cfg_dir)
    reg = registry.compile_registry()
    assert set(reg.collections) == set(registry.compile_registry(fixture_paths).collections)
    assert reg.skip_filenames == ("a.md",)


def test_config_dir_env_var_wins(tmp_path):
    env = {**os.environ, "PYTHONPATH": SRC_DIR, "SCOPED_KB_CONFIG_DIR": str(tmp_path / "custom"),
           "SCOPED_KB_STATE_DIR": str(tmp_path / "state")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from scoped_kb import settings; print(settings.CONFIG_DIR); print(settings.STATE_DIR)"],
        env=env, capture_output=True, text=True, check=True,
        cwd=str(tmp_path)).stdout.split()
    assert out == [str(tmp_path / "custom"), str(tmp_path / "state")]


def test_config_dir_falls_back_to_platformdirs(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("SCOPED_KB_CONFIG_DIR", "SCOPED_KB_STATE_DIR")}
    env["PYTHONPATH"] = SRC_DIR
    out = subprocess.run(
        [sys.executable, "-c",
         "from scoped_kb import settings; print(settings.CONFIG_DIR); print(settings.STATE_DIR)"],
        env=env, capture_output=True, text=True, check=True,
        cwd=str(tmp_path)).stdout.split("\n")[:2]
    assert out == [platformdirs.user_config_dir("scoped-kb"),
                   platformdirs.user_state_dir("scoped-kb")]


def test_repo_roots_map_to_default_profiles(fixture_paths, fixture_base):
    reg = registry.compile_registry(fixture_paths)
    mapping = dict(reg.repo_roots)
    assert mapping[(fixture_base / "repo_a").resolve()] == "work"
    assert mapping[(fixture_base / "repo_b").resolve()] == "research"


def test_cross_allowlist_is_normalised(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert ("work", frozenset({"research"})) in reg.cross_allowlist
    assert ("research", frozenset({"work"})) in reg.cross_allowlist
    assert ("work", frozenset({"knowledge"})) not in reg.cross_allowlist


def test_collection_carries_scope_and_policy(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    a = reg.collections["a-decisions"]
    assert (a.scope, a.authority, a.index_policy) == ("work", "authority", "active")
    q = reg.collections["m-global-unresolved"]
    assert q.index_policy == "quarantine"
    assert q.quarantine_reason == "unresolved-mixed"
    assert q.exit_path
