"""registry 校验规则，全部 fail closed。

覆盖九条校验规则、「本地声明不得含中央字段」，
以及 manifest 悬空与越界两条。
"""
import pytest

from scoped_kb import registry

# ---- 九类坏配置 --------------------------------------------------------

def test_duplicate_collection_id(make_paths):
    """两个仓库声明了同一个 collection_id。"""
    drifted = make_paths(repos=lambda d: d["collections"].append({
        "collection_id": "b-notes",          # 与 repo_b 撞名
        "roots": ["docs/other"], "excludes": [],
        "kind_default": "note", "resolution_default": "document"}))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "重复 collection ID" in str(exc.value)


def test_overlapping_roots(make_paths):
    """同一文件可被两个 collection 认领 —— 归属不唯一，必须拒绝。"""
    drifted = make_paths(repos=lambda d: d["collections"].append({
        "collection_id": "a-docs-all",
        "roots": ["docs"],                   # 包含了 a-decisions 的 docs/decisions
        "excludes": [], "kind_default": "note", "resolution_default": "document"}))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "root 重叠" in str(exc.value)


def test_root_escapes_owner_repo(make_paths):
    """root realpath 后落在别的仓库里 —— 仓库不能声明别人的目录。"""
    drifted = make_paths(repos=lambda d: d["collections"][0].__setitem__(
        "roots", ["../repo_b/notes"]))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "root 越界" in str(exc.value)


def test_policy_references_unknown_collection(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["profiles"].__setitem__(
        "work", ["a-decisions", "does-not-exist"]))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "未知引用" in str(exc.value)


def test_fingerprint_drift(make_paths):
    """保留已授权 ID、偷换 roots/excludes —— 指纹对不上就必须拒绝。"""
    drifted = make_paths(repos=lambda d: d["collections"][0].__setitem__(
        "excludes", ["draft"]))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "声明指纹不一致" in str(exc.value)


def test_unauthorized_new_collection(make_paths):
    """仓库新增了 collection，中央策略没处理它。"""
    drifted = make_paths(repos=lambda d: d["collections"].append({
        "collection_id": "a-brand-new",
        "roots": ["docs/new"], "excludes": [],
        "kind_default": "note", "resolution_default": "document"}))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "未授权的新 collection" in str(exc.value)


def test_missing_field(make_paths):
    drifted = make_paths(repos=lambda d: d["collections"][0].pop("resolution_default"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "配置缺字段" in str(exc.value)


def test_empty_allowlist(make_paths):
    """profile 的 allowlist 为空 —— 空集合意味着配置失效，不是「什么都不给读」。"""
    drifted = make_paths(scope_policy=lambda d: d["profiles"].__setitem__("work", []))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "allowlist 为空" in str(exc.value)


def test_local_declares_central_field(make_paths):
    """本地声明出现 scope —— 自我授权，直接拒绝。"""
    drifted = make_paths(repos=lambda d: d["collections"][0].__setitem__(
        "scope", "global"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "本地不得声明中央字段" in str(exc.value)


def test_bad_json_is_not_silently_skipped(fixture_paths, tmp_path):
    broken = tmp_path / "collections.json"
    broken.write_text("{ not json", encoding="utf-8")
    paths = dict(fixture_paths, repos=[broken])
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "不是合法 JSON" in str(exc.value)


def test_profile_names_and_scopes_carry_no_builtin_rule(make_paths):
    """profile 名与 collection 的 scope 之间没有写死的对应规则。

    profiles 是中央策略里显式列出的 ID 列表，哪个 profile 能读什么由它自己说了算；
    库不预设任何 profile 名，也不替用户规定「某个名字的 profile 只许含某类 scope」。
    """
    ok = make_paths(scope_policy=lambda d: d["profiles"].__setitem__(
        "work", ["a-decisions", "b-notes"]))        # b-notes 的 scope 是 research
    reg = registry.compile_registry(ok)
    assert reg.profiles["work"] == ("a-decisions", "b-notes")


# ---- default_profile（scope-policy.json 的可选字段）-----------------------

def test_default_profile_is_optional_and_defaults_to_none(fixture_paths):
    assert registry.compile_registry(fixture_paths).default_profile is None


def test_default_profile_is_read(make_paths):
    paths = make_paths(scope_policy=lambda d: d.__setitem__("default_profile", "work"))
    assert registry.compile_registry(paths).default_profile == "work"


def test_default_profile_must_be_a_registered_profile(make_paths):
    paths = make_paths(scope_policy=lambda d: d.__setitem__("default_profile", "nope"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "default_profile" in str(exc.value) and "nope" in str(exc.value)


@pytest.mark.parametrize("bad", [1, True, None, "", "   ", [], ["work"], {}, {"work": 1}],
                         ids=["int", "bool", "null", "empty", "blank", "empty_list",
                              "list", "empty_dict", "dict"])
def test_default_profile_type_errors_are_rejected(make_paths, bad):
    """类型错一律 RegistryError，不得漏出 unhashable 的 TypeError；显式写 null 也算类型错。"""
    paths = make_paths(scope_policy=lambda d: d.__setitem__("default_profile", bad))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "default_profile" in str(exc.value)


def test_default_profile_changes_registry_hash(make_paths):
    """它改变「谁能读到什么」，所以进 hash：改了它必须触发索引换代。"""
    none = registry.compile_registry(make_paths()).registry_hash
    work = registry.compile_registry(make_paths(
        scope_policy=lambda d: d.__setitem__("default_profile", "work"))).registry_hash
    inv = registry.compile_registry(make_paths(
        scope_policy=lambda d: d.__setitem__("default_profile", "research"))).registry_hash
    assert len({none, work, inv}) == 3


def test_default_profile_is_central_only(make_paths):
    """本地声明里出现 default_profile 同样按越权字段拒绝（放置位置不授予权限）。"""
    paths = make_paths(repos=lambda d: d["collections"][0].__setitem__(
        "default_profile", "work"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "default_profile" in str(exc.value)


def test_central_owner_repo_must_match_local(make_paths):
    """中央声明的 owner_repo 与本地 repo_id 不一致 —— 指纹基准会错位。"""
    drifted = make_paths(scope_policy=lambda d: d["collections"]["a-decisions"]
                         .__setitem__("owner_repo", "repo_b"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "owner_repo 不一致" in str(exc.value)


def test_cross_allowlist_rejects_base_in_include(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["cross_allowlist"].append(
        {"base": "work", "include": ["work"]}))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "cross_allowlist" in str(exc.value)


def test_cross_allowlist_rejects_unknown_profile(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["cross_allowlist"].append(
        {"base": "work", "include": ["nope"]}))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "未知引用" in str(exc.value)


# ---- manifest 悬空与越界---------------------------------------

def test_manifest_dangling_assignment_fails_closed(make_paths):
    """assignments 指向不存在的文件 → 非零退出。

    不挡的后果：那条已被人工确认的 global claim 会随文件改名静默落回
    quarantine 默认分区，对所有 profile 消失，且没有任何提示。
    """
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["assignments"].__setitem__("was_renamed.md", "m-global"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "manifest 悬空路径" in str(exc.value)
    assert "was_renamed.md" in str(exc.value), "必须指名道姓，否则无法修"


def test_manifest_path_outside_base_root_fails_closed(make_paths):
    """assignments 的键必须是相对基础 root 的路径，越界即拒绝。"""
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["assignments"].__setitem__("../../repo_a/docs/decisions/a.md",
                                                     "m-global"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "manifest 路径越界" in str(exc.value)


def test_manifest_assignment_to_unknown_partition(make_paths):
    """指向压根不存在的分区。

    消息要说「assignment 目标非法」而不是泛化的「未知引用」，
    并列出合法目标集合——「未声明」与「已知但无关」是两种不同的错，
    提示要能区分，否则修的人会去别处找那个 ID。
    """
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["assignments"].__setitem__("confirmed.md", "m-global-nope"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "assignment 目标非法" in str(exc.value)
    assert "未声明" in str(exc.value)


def test_ok_fixture_still_compiles(fixture_paths):
    """兜底：所有新增校验不得误伤合法配置。"""
    reg = registry.compile_registry(fixture_paths)
    assert len(reg.collections) == 5
