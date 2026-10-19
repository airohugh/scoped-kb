"""配置的严格类型与 manifest 形状。

这一组针对的是「值看起来对、类型不对」——Python 的宽松比较会让它们静默通过：
  · `True == 1` 为真，`isinstance(True, int)` 也为真；
  · `1.0 == 1` 为真。
于是 `schema_version: true` 和 `schema_version: 1.0` 都能骗过 `== SCHEMA_VERSION`。
JSON 里这两个值都是合法字面量，手写配置时很容易写出来。
"""
import json
from pathlib import Path

import pytest

from scoped_kb import registry


def _reseal(paths):
    """patch 之后把中央指纹重算回填。

    make_paths 的 patch 施加在 conftest 注入指纹**之后**，所以任何改动 roots 的
    补丁都会先撞上指纹检查。本函数让测试越过指纹这一关，命中后面要验的规则。
    """
    p = Path(paths["scope_policy"])
    pol = json.loads(p.read_text(encoding="utf-8"))
    for cid, fp in registry.print_fingerprints(paths).items():
        if cid in pol["collections"]:
            pol["collections"][cid]["fingerprint"] = fp
    p.write_text(json.dumps(pol, ensure_ascii=False, indent=2), encoding="utf-8")
    return paths


# ---- 1&2. 三处 schema_version 的严格类型 --------------------------------

@pytest.mark.parametrize("bad", [True, 1.0], ids=["bool_true", "float_one"])
def test_repo_schema_version_must_be_int(make_paths, bad):
    """repo declaration。`True` 与 `1.0` 都 == 1，必须靠 type() 挡。"""
    drifted = make_paths(repos=lambda d: d.__setitem__("schema_version", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


@pytest.mark.parametrize("bad", [True, 1.0], ids=["bool_true", "float_one"])
def test_machine_sources_schema_version_must_be_int(make_paths, bad):
    drifted = make_paths(machine_sources=lambda d: d.__setitem__("schema_version", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


@pytest.mark.parametrize("bad", [True, 1.0], ids=["bool_true", "float_one"])
def test_scope_policy_schema_version_must_be_int(make_paths, bad):
    drifted = make_paths(scope_policy=lambda d: d.__setitem__("schema_version", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


@pytest.mark.parametrize("bad", ["1", None, [1]], ids=["str", "null", "list"])
def test_schema_version_other_wrong_types(make_paths, bad):
    drifted = make_paths(scope_policy=lambda d: d.__setitem__("schema_version", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


# ---- 3. index_policy 必须属于四个合法值 ---------------------------------

@pytest.mark.parametrize("bad", ["Active", "disabled", "", 123, True, None],
                         ids=["wrong_case", "unknown", "empty", "int", "bool", "null"])
def test_index_policy_must_be_one_of_four(make_paths, bad):
    """不校验的话，未知 index_policy 会静默落进 classify_all 的 unclassified 桶，
    或者在 INDEXED_POLICIES 判断里被当成「不进索引」——两种都不报错。"""
    drifted = make_paths(scope_policy=lambda d: d["collections"]["a-decisions"]
                         .__setitem__("index_policy", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "index_policy" in str(exc.value)


@pytest.mark.parametrize("bad", ["Quarantine", "hold", 0],
                         ids=["wrong_case", "unknown", "int"])
def test_partition_index_policy_must_be_one_of_four(make_paths, bad):
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-work"].__setitem__("index_policy", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "index_policy" in str(exc.value)


# ---- 4. quarantine_reason / exit_path 必须是 strip 后非空的字符串 --------

@pytest.mark.parametrize("field", ["quarantine_reason", "exit_path"])
@pytest.mark.parametrize("bad", [123, True, ["reason"], {"a": 1}, None],
                         ids=["int", "bool", "list", "dict", "null"])
def test_quarantine_metadata_must_be_string(make_paths, field, bad):
    """不先验类型，非字符串会让 `(v or "").strip()` 直接 AttributeError（崩而非报错），
    或者 `123 or ""` → 123 被当成「填了」。两种都不是 fail closed。"""
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-unresolved"].__setitem__(field, bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert field in str(exc.value)


@pytest.mark.parametrize("field", ["quarantine_reason", "exit_path"])
def test_top_level_quarantine_metadata_must_be_string(make_paths, field):
    def patch(d):
        d["collections"]["b-notes"]["index_policy"] = "quarantine"
        d["collections"]["b-notes"]["quarantine_reason"] = "占位原因"
        d["collections"]["b-notes"]["exit_path"] = "占位退出路径"
        d["collections"]["b-notes"][field] = 42
        d["profiles"]["research"] = ["m-global"]      # 移出 profile，避免撞第 5 条
    drifted = make_paths(scope_policy=patch)
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert field in str(exc.value)


# ---- 5. confirmed_manifest 基础 collection 必须恰好一个物理 root ---------

def _make_confirmed_manifest_repo_collection(roots):
    """把 a-decisions 改成 confirmed_manifest 并配上自己的 manifest，
    roots 由调用方指定，用于验证 root 数量约束。"""
    def patch_repo(d):
        d["collections"][0]["roots"] = roots

    def patch_pol(d):
        d["collections"]["a-decisions"]["index_policy"] = "confirmed_manifest"
        d["manifest_partitions"]["a-decisions"] = {
            "default_partition": "a-unresolved",
            "partitions": {
                "a-unresolved": {
                    "scope": "unclassified",
                    "index_policy": "quarantine",
                    "quarantine_reason": "unresolved-mixed",
                    "exit_path": "人工 review 后重新归类",
                },
            },
            "assignments": {},
        }
    return patch_repo, patch_pol


def test_confirmed_manifest_base_rejects_two_roots(make_paths):
    """两个 root 时，manifest 的相对路径该相对谁？若默认取 roots[0]，
    第二个 root 下的文件永远匹配不到 assignment，静默落 default 分区。"""
    pr, pp = _make_confirmed_manifest_repo_collection(["docs/decisions", "docs/extra"])
    paths = _reseal(make_paths(repos=pr, scope_policy=pp))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "恰好一个物理 root" in str(exc.value)


def test_confirmed_manifest_base_rejects_zero_roots(make_paths):
    """零个 root 时不校验就会在 base.roots[0] 上 IndexError——崩溃而非 fail closed。"""
    pr, pp = _make_confirmed_manifest_repo_collection([])
    paths = _reseal(make_paths(repos=pr, scope_policy=pp))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "恰好一个物理 root" in str(exc.value)


def test_confirmed_manifest_base_accepts_exactly_one_root(make_paths):
    """反向确认：一个 root 是正当形状，不得误伤。"""
    pr, pp = _make_confirmed_manifest_repo_collection(["docs/decisions"])
    paths = _reseal(make_paths(repos=pr, scope_policy=pp))
    reg = registry.compile_registry(paths)
    assert reg.manifest_default["a-decisions"] == "a-unresolved"


# ---- 兜底 ---------------------------------------------------------------

def test_ok_fixture_still_compiles(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert len(reg.collections) == 5


# ---- index_policy 先验类型再验枚举 ----------------------------

@pytest.mark.parametrize("bad", [[], {}, {"a": 1}, ["active"]],
                         ids=["list", "dict", "dict_kv", "list_of_str"])
def test_index_policy_unhashable_types_raise_registry_error(make_paths, bad):
    """`value not in frozenset` 对 list/dict 会抛 TypeError: unhashable type
    ——那是崩溃，不是 fail closed。必须先验「是字符串」再做枚举。"""
    drifted = make_paths(scope_policy=lambda d: d["collections"]["a-decisions"]
                         .__setitem__("index_policy", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "index_policy" in str(exc.value)


@pytest.mark.parametrize("bad", [[], {"x": 1}], ids=["list", "dict"])
def test_partition_index_policy_unhashable_types(make_paths, bad):
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-work"].__setitem__("index_policy", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "index_policy" in str(exc.value)
