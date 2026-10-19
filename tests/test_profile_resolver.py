"""effective_profile 解析器。

核心契约：
  · cwd 只用于推断**默认值**，不提供授权；
  · 未知 / 歧义 / 冲突一律 fail closed，不猜测、不回退全库；
  · 唯一的「兜底」是中央策略里**显式配置**的 default_profile，它只补
    「cwd 没有任何仓库根映射」的缺口，不覆盖显式映射，也不绕过 cross 规则；
  · 扩大范围只有 `cross + 非空 include + 去空白后非空 why` 一条路径，
    且组合必须在中央 allowlist 内；
  · cross 是**并集**语义，不是切换语义；
  · 环境变量与包装器不改变 allowed collections。

夹具：fixtures/ok 里 repo_a → work、repo_b → research 两个仓库根，
各有一个独占的 collection（a-decisions / b-notes），machine 来源 m-global 两边共有。
"""
import dataclasses

import pytest

from scoped_kb import registry
from scoped_kb.scope import ProfileError, resolve_effective_profile


@pytest.fixture
def reg(fixture_paths):
    return registry.compile_registry(fixture_paths)


@pytest.fixture
def repo_a(fixture_base):
    return fixture_base / "repo_a"


@pytest.fixture
def repo_b(fixture_base):
    return fixture_base / "repo_b"


@pytest.fixture
def reg3(make_paths, fixture_base):
    """三个 profile：work / research / shared。

    shared 的仓库根指向夹具里的 machine 目录；白名单里有一条两个 include 的组合
    （shared + work,research），用来覆盖多域并集、include 顺序、三域重复项。
    """
    def _patch(d):
        d["profiles"]["shared"] = ["m-global"]
        d["repo_roots"][str(fixture_base / "machine")] = "shared"
        d["cross_allowlist"].append({"base": "shared", "include": ["work", "research"]})
    return registry.compile_registry(make_paths(scope_policy=_patch))


@pytest.fixture
def shared_dir(fixture_base):
    return fixture_base / "machine" / "global"


def _fake_registry(reg, repo_roots, profiles=None, default_profile=None):
    """手工构造的 Registry——绕过编译器，用来测 resolver 自己的第二层防御。"""
    return registry.Registry(
        schema_version=1, collections=reg.collections,
        profiles=profiles if profiles is not None else reg.profiles,
        repo_roots=repo_roots, cross_allowlist=reg.cross_allowlist,
        manifest={}, manifest_default={}, registry_hash="x" * 64,
        default_profile=default_profile)


def _exclusive(reg, profile, other):
    """只属于 other、不属于 profile 的 collection。"""
    return set(reg.allowed_for(other)) - set(reg.allowed_for(profile))


# ---- cwd 推断 -----------------------------------------------------------

@pytest.mark.parametrize("sub,expected", [
    ("repo_a", "work"),
    ("repo_a/docs/decisions", "work"),
    ("repo_b", "research"),
    ("repo_b/notes", "research"),
])
def test_cwd_infers_profile(reg, fixture_base, sub, expected):
    assert resolve_effective_profile(reg, fixture_base / sub).base == expected


def test_unknown_cwd_fails_closed(reg, tmp_path):
    """cwd 不属于任何已注册仓根 → 非零退出，不猜测、不回退全库（没配 default_profile 时）。"""
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, tmp_path)
    assert "不属于任何已注册仓库根" in str(exc.value)
    assert "default_profile" in str(exc.value), "报错要指出可配的出路"


def test_repo_root_itself_resolves(reg, repo_a):
    """仓根本身（不是子目录）也要能解析。"""
    assert resolve_effective_profile(reg, repo_a).base == "work"


def test_longest_root_match_is_used(reg, tmp_path):
    """嵌套仓根时取最长匹配。

    路径的祖先构成一条链，最长匹配天然唯一；配置层面的重复 root
    已由 registry 编译期拒绝，所以这里不存在真正的歧义。
    """
    outer, inner = (tmp_path / "outer").resolve(), (tmp_path / "outer" / "inner").resolve()
    inner.mkdir(parents=True)
    fake = _fake_registry(
        reg, ((outer, "work"), (inner, "research")),
        profiles={"work": ("a-decisions",), "research": ("b-notes",)})
    assert resolve_effective_profile(fake, inner / "a").base == "research"
    assert resolve_effective_profile(fake, outer / "a").base == "work"


# ---- 显式 profile ------------------------------------------------------

def test_explicit_profile_matching_cwd_is_ok(reg, repo_a):
    eff = resolve_effective_profile(reg, repo_a, profile="work")
    assert eff.effective == "work" and eff.base == "work"


@pytest.mark.parametrize("sub,bad", [("repo_a", "research"), ("repo_b", "work")])
def test_single_domain_profiles_cannot_be_swapped(reg, fixture_base, sub, bad):
    """单域 profile 之间不得直接互换——cross 是并集，不是切换。"""
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, fixture_base / sub, profile=bad)
    assert "不得互换" in str(exc.value) or "不接受" in str(exc.value)


def test_unknown_explicit_profile_rejected(reg, repo_a):
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg, repo_a, profile="nope")


# ---- 默认集合的域隔离 --------------------------------------------------

@pytest.mark.parametrize("sub,mine,other", [("repo_a", "work", "research"),
                                            ("repo_b", "research", "work")])
def test_default_collections_have_no_other_domains_collection(
        reg, fixture_base, sub, mine, other):
    """同域检索的集合恰好是本 profile 声明的那些，不含任何只属于另一个域的 collection。"""
    eff = resolve_effective_profile(reg, fixture_base / sub)
    assert set(eff.collections) == set(reg.allowed_for(mine))
    assert not set(eff.collections) & _exclusive(reg, mine, other)
    assert _exclusive(reg, mine, other), "夹具必须真的有另一个域独占的 collection"


@pytest.mark.parametrize("sub", ["repo_a", "repo_b"])
def test_default_collections_never_include_quarantine_or_excluded(reg, fixture_base, sub):
    eff = resolve_effective_profile(reg, fixture_base / sub)
    for cid in eff.collections:
        assert reg.collections[cid].index_policy in ("active", "confirmed_manifest"), cid


# ---- cross 是并集 -------------------------------------------------------

def test_cross_is_union_not_switch(reg, repo_a):
    base = resolve_effective_profile(reg, repo_a)
    eff = resolve_effective_profile(
        reg, repo_a, profile="cross", include=["research"],
        why="本任务需要核对另一个域的判例")
    assert eff.base == "work" and eff.effective == "cross"
    assert eff.included == ("research",)
    assert set(base.collections) < set(eff.collections)          # 严格超集
    assert set(eff.collections) == set(reg.allowed_for("work")) | set(
        reg.allowed_for("research"))


def test_cross_result_is_sorted_and_deduped(reg3, shared_dir):
    """两个域共有的 collection（m-global）在并集里只出现一次，且整体已排序。"""
    eff = resolve_effective_profile(
        reg3, shared_dir, profile="cross", include=["work", "research"], why="全域核对")
    assert list(eff.collections) == sorted(set(eff.collections))
    assert eff.collections.count("m-global") == 1


def test_cross_carries_stripped_why(reg, repo_a):
    eff = resolve_effective_profile(
        reg, repo_a, profile="cross", include=["research"], why="  有理由  ")
    assert eff.why == "有理由"


@pytest.mark.parametrize("include,why,keyword", [
    ([], "有理由", "非空 --include"),
    (None, "有理由", "非空 --include"),
    (["research"], "   ", "--why"),
    (["research"], "", "--why"),
    (["research"], None, "--why"),
    (["nope"], "有理由", "未知的域"),
    (["work"], "有理由", "自身"),
])
def test_illegal_cross_fails_closed(reg, repo_a, include, why, keyword):
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, repo_a, profile="cross",
                                  include=include, why=why)
    assert keyword in str(exc.value)


def test_cross_combo_outside_allowlist_rejected(make_paths, repo_a):
    """表外组合全部拒绝。两个域都已注册，但白名单里没有 work → research 这一行。"""
    only_reverse = registry.compile_registry(make_paths(
        scope_policy=lambda d: d.__setitem__(
            "cross_allowlist", [{"base": "research", "include": ["work"]}])))
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(only_reverse, repo_a, profile="cross",
                                  include=["research"], why="要读")
    assert "未允许该 cross 组合" in str(exc.value)
    assert "research + work" in str(exc.value), "要列出实际允许的组合"


def test_cross_allowlist_matches_exact_combination_only(reg3, shared_dir, fixture_base):
    """白名单只认精确组合：超集、子集、换一个域都不行，不由实现者自行扩展。"""
    # shared + [work, research] 在表内
    resolve_effective_profile(reg3, shared_dir, profile="cross",
                              include=["work", "research"], why="x")
    for include in (["work"], ["research"]):                    # 子集：表里没有
        with pytest.raises(ProfileError) as exc:
            resolve_effective_profile(reg3, shared_dir, profile="cross",
                                      include=include, why="x")
        assert "未允许该 cross 组合" in str(exc.value)
    # work + [research, shared]：超集，表里只有 work + [research]
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg3, fixture_base / "repo_a", profile="cross",
                                  include=["research", "shared"], why="x")
    assert "未允许该 cross 组合" in str(exc.value)


def test_every_allowlisted_combo_works(reg3, fixture_base):
    """白名单里的每一行都能走通，且结果恰好是 base 与各 include 的并集。"""
    cwd_for = {"work": fixture_base / "repo_a", "research": fixture_base / "repo_b",
               "shared": fixture_base / "machine"}
    assert len(reg3.cross_allowlist) == 3
    for base, include in sorted(reg3.cross_allowlist, key=lambda r: (r[0], sorted(r[1]))):
        eff = resolve_effective_profile(
            reg3, cwd_for[base], profile="cross", include=sorted(include), why="验证白名单")
        assert eff.effective == "cross" and eff.base == base
        expected = set(reg3.allowed_for(base))
        for d in include:
            expected |= set(reg3.allowed_for(d))
        assert set(eff.collections) == expected


def test_cross_include_order_does_not_matter(reg3, shared_dir):
    a = resolve_effective_profile(reg3, shared_dir, profile="cross",
                                  include=["work", "research"], why="x")
    b = resolve_effective_profile(reg3, shared_dir, profile="cross",
                                  include=["research", "work"], why="x")
    assert set(a.collections) == set(b.collections)


def test_cross_cannot_reach_quarantine(reg3, shared_dir):
    """cross 只合并已授权的域，不解除 quarantine。"""
    eff = resolve_effective_profile(reg3, shared_dir, profile="cross",
                                    include=["work", "research"], why="全域核对")
    assert "m-global-unresolved" not in eff.collections
    for cid in eff.collections:
        assert reg3.collections[cid].index_policy in ("active", "confirmed_manifest")


def test_cross_duplicate_include_rejected(reg, repo_a):
    """重复的 include 项必须拒绝，**不是去重放行**。

    重复、空集合、未知域或表外组合均拒绝，不由实现者自行扩展。去重放行是实现者的
    自行扩展——它让 `--include research,research` 这种明显写错的命令静默成功，
    也让「白名单只认精确组合」这条规则出现一个由实现决定的例外。
    """
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(
            reg, repo_a, profile="cross", include=["research", "research"], why="x")
    assert "重复" in str(exc.value)


def test_cross_duplicate_include_three_way(reg3, shared_dir):
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(
            reg3, shared_dir, profile="cross",
            include=["work", "research", "work"], why="x")
    assert "重复" in str(exc.value)
    assert "work" in str(exc.value)


# ---- resolver 的第二层歧义防御 ------------------------------------------

def test_equal_length_ambiguous_roots_fail_closed(reg, tmp_path):
    """同一个 root 映射到两个不同 profile → 必须 ProfileError。

    registry 编译已经挡住这种配置（repo_roots 规范化唯一），本条是
    **第二层防御**：resolver 拿到手工构造或未来其它来源的 registry 时，
    不得用 max() 静默取首项——那样解析结果取决于元组顺序，且不报错。
    """
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    fake = _fake_registry(reg, ((root, "work"), (root, "research")))
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(fake, root / "sub" / "a.md")
    assert "歧义" in str(exc.value)


def test_equal_length_same_profile_is_not_ambiguous(reg, tmp_path):
    """两条重复但指向同一 profile 时答案唯一，不该误伤。"""
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    fake = _fake_registry(reg, ((root, "work"), (root, "work")))
    assert resolve_effective_profile(fake, root / "a.md").base == "work"


def test_nested_roots_still_take_longest(reg, tmp_path):
    """合法嵌套仓根仍取最长匹配——防御不得退化成「一有多条就拒绝」。"""
    outer = (tmp_path / "outer").resolve()
    inner = outer / "inner"
    inner.mkdir(parents=True)
    fake = _fake_registry(reg, ((outer, "work"), (inner, "research")))
    assert resolve_effective_profile(fake, inner / "deep" / "a").base == "research"
    assert resolve_effective_profile(fake, outer / "a").base == "work"


def test_three_level_nesting_takes_deepest(reg, tmp_path):
    a = tmp_path.resolve() / "a"
    b = a / "b"
    c = b / "c"
    c.mkdir(parents=True)
    profiles = {"work": ("a-decisions",), "research": ("b-notes",),
                "shared": ("m-global",)}
    fake = _fake_registry(
        reg, ((a, "work"), (b, "research"), (c, "shared")), profiles=profiles)
    assert resolve_effective_profile(fake, c / "x").base == "shared"
    assert resolve_effective_profile(fake, b / "x").base == "research"
    assert resolve_effective_profile(fake, a / "x").base == "work"


# ---- cross 参数的严格类型 -----------------------------------------------

@pytest.mark.parametrize("bad", [
    [{}], [1], [None], [True], [["research"]], [b"research"],
    ["research", 2], [""], ["   "],
], ids=["dict_item", "int_item", "none_item", "bool_item", "list_item",
        "bytes_item", "mixed", "empty_str", "blank_str"])
def test_include_items_must_be_nonempty_strings(reg, repo_a, bad):
    """非字符串项原会泄漏 TypeError（`', '.join([1])`、
    `dict.fromkeys([{}])` 的 unhashable）——那是崩溃，不是 fail closed。"""
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, repo_a, profile="cross",
                                  include=bad, why="有理由")
    assert "--include" in str(exc.value)


@pytest.mark.parametrize("bad", ["research", b"x", 1, {}, {"a": 1}, object()],
                         ids=["bare_str", "bytes", "int", "dict", "dict_kv", "object"])
def test_include_container_must_be_list_or_tuple(reg, repo_a, bad):
    """裸字符串尤其危险：`for d in "work"` 会逐字符迭代，
    得到一串单字母「域名」，错误消息会变得莫名其妙。"""
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, repo_a, profile="cross",
                                  include=bad, why="有理由")
    assert "--include" in str(exc.value)


def test_include_tuple_is_accepted(reg, repo_a):
    """tuple 与 list 等价，不得只认 list。"""
    eff = resolve_effective_profile(reg, repo_a, profile="cross",
                                    include=("research",), why="x")
    assert eff.included == ("research",)


@pytest.mark.parametrize("bad", [123, True, [], {}, ["理由"], b"reason", object()],
                         ids=["int", "bool", "list", "dict", "list_of_str",
                              "bytes", "object"])
def test_why_must_be_string(reg, repo_a, bad):
    """`(123 or "").strip()` 会 AttributeError；非字符串必须统一成 ProfileError。"""
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg, repo_a, profile="cross",
                                  include=["research"], why=bad)
    assert "--why" in str(exc.value)


def test_no_typeerror_or_attributeerror_leaks(reg, repo_a):
    """兜底：一批畸形输入里不得有任何一个漏出 ProfileError 之外的异常。"""
    bad_inputs = [
        (["research"], 123), (["research"], []), ([1], "x"), ([{}], "x"),
        ("research", "x"), ({}, "x"), ([None], "x"), ([True], "x"),
        (["research", "research"], "x"), ([], "x"), (None, "x"),
        (["research"], None), (["research"], "  "),
    ]
    for include, why in bad_inputs:
        with pytest.raises(ProfileError):
            resolve_effective_profile(reg, repo_a, profile="cross",
                                      include=include, why=why)


# ---- 同域 collections 也排序去重 -----------------------------------------

@pytest.mark.parametrize("sub", ["repo_a", "repo_b"])
def test_same_domain_collections_are_sorted_and_deduped(reg, fixture_base, sub):
    """dataclass 合同写的是「已排序去重」，同域与 cross 必须一致。

    不一致的话，两条路径产出的 Effective 无法直接比较，
    流水里记录的 collection 顺序也会随 profile 声明顺序漂移。
    """
    eff = resolve_effective_profile(reg, fixture_base / sub)
    assert list(eff.collections) == sorted(set(eff.collections))


def test_same_domain_and_cross_share_ordering_contract(reg, repo_a):
    same = resolve_effective_profile(reg, repo_a)
    cross = resolve_effective_profile(reg, repo_a, profile="cross",
                                      include=["research"], why="x")
    for eff in (same, cross):
        assert list(eff.collections) == sorted(set(eff.collections))


# ---- 环境变量不提权 -----------------------------------------------------

@pytest.mark.parametrize("var", ["SCOPED_KB_PROFILE", "SCOPED_KB_INCLUDE",
                                 "SCOPED_KB_SCOPE", "SCOPED_KB_AGENT",
                                 "SCOPED_KB_ALLOWED_COLLECTIONS"])
def test_env_var_cannot_widen(reg, repo_a, monkeypatch, var):
    before = resolve_effective_profile(reg, repo_a).collections
    monkeypatch.setenv(var, "research")
    after = resolve_effective_profile(reg, repo_a).collections
    assert before == after


def test_env_cannot_supply_missing_cwd(reg, tmp_path, monkeypatch):
    """未注册 cwd 下，环境变量自报 profile 也拿不到权限。"""
    monkeypatch.setenv("SCOPED_KB_PROFILE", "work")
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg, tmp_path)


# ---- Effective 的字段契约 ----------------------------------------------

def test_effective_carries_registry_hash(reg, repo_a):
    eff = resolve_effective_profile(reg, repo_a)
    assert eff.registry_hash == reg.registry_hash


def test_same_domain_has_empty_included_and_why(reg, repo_a):
    eff = resolve_effective_profile(reg, repo_a)
    assert eff.included == () and eff.why == ""


def test_effective_is_immutable(reg, repo_a):
    """Effective 是授权结论，产出后不得被下游改写。"""
    eff = resolve_effective_profile(reg, repo_a)
    with pytest.raises(dataclasses.FrozenInstanceError):
        eff.collections = ()


# ---- default_profile：单域开箱即用，但不松动任何纪律 ----------------------

def _with_default(make_paths, name="work", **extra_patches):
    def _patch(d):
        d["default_profile"] = name
    return registry.compile_registry(make_paths(scope_policy=_patch, **extra_patches))


def test_default_profile_applies_when_cwd_matches_no_root(make_paths, tmp_path):
    """配了 default_profile，cwd 落在任何仓库根之外也能解析——base 就是它。"""
    reg_d = _with_default(make_paths)
    eff = resolve_effective_profile(reg_d, tmp_path)
    assert eff.base == "work" and eff.effective == "work"
    assert set(eff.collections) == set(reg_d.allowed_for("work"))
    assert eff.registry_hash == reg_d.registry_hash
    # 子目录同理
    sub = tmp_path / "x" / "y"
    sub.mkdir(parents=True)
    assert resolve_effective_profile(reg_d, sub).base == "work"


def test_no_default_profile_still_fails_closed(make_paths, tmp_path):
    """没配 default_profile 时行为原样：cwd 不匹配就报错。"""
    reg_n = registry.compile_registry(make_paths())
    assert reg_n.default_profile is None
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg_n, tmp_path)


def test_default_profile_does_not_override_explicit_root_mapping(make_paths, repo_b, repo_a):
    """cwd 落在已注册仓库根里仍按仓库根解析——default 只补缺口，不覆盖显式映射。"""
    reg_d = _with_default(make_paths, "work")
    assert resolve_effective_profile(reg_d, repo_b).base == "research"
    assert resolve_effective_profile(reg_d, repo_b / "notes").base == "research"
    assert resolve_effective_profile(reg_d, repo_a).base == "work"
    reg_i = _with_default(make_paths, "research")
    assert resolve_effective_profile(reg_i, repo_a).base == "work"


def test_default_profile_does_not_rescue_ambiguous_roots(make_paths, tmp_path):
    """有匹配但歧义时仍然拒绝：default 只在**没有任何匹配**时才出场。"""
    reg_d = _with_default(make_paths)
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    fake = _fake_registry(reg_d, ((root, "work"), (root, "research")),
                          default_profile="work")
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(fake, root / "a.md")
    assert "歧义" in str(exc.value)


def test_explicit_profile_rules_are_the_same_under_default(make_paths, tmp_path):
    """显式 --profile 的校验与匹配到仓库根时完全一样。"""
    reg_d = _with_default(make_paths)
    assert resolve_effective_profile(reg_d, tmp_path, profile="work").base == "work"
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg_d, tmp_path, profile="research")
    assert "不得互换" in str(exc.value) or "不接受" in str(exc.value)
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg_d, tmp_path, profile="nope")


def test_cross_under_default_profile_still_needs_allowlist(make_paths, tmp_path):
    """默认 profile 不是 cross 的通行证：白名单、include、why 的纪律一条不松。"""
    reg_d = _with_default(make_paths)
    # 白名单里有 work + [research]：通过，且并集语义不变
    eff = resolve_effective_profile(reg_d, tmp_path, profile="cross",
                                    include=["research"], why="核对")
    assert eff.base == "work" and eff.effective == "cross"
    assert set(eff.collections) == set(reg_d.allowed_for("work")) | set(
        reg_d.allowed_for("research"))
    # why / include 缺失照样拒绝
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg_d, tmp_path, profile="cross",
                                  include=["research"], why="  ")
    with pytest.raises(ProfileError):
        resolve_effective_profile(reg_d, tmp_path, profile="cross", include=[], why="x")
    # 白名单里没有这个组合：拒绝
    stripped = dataclasses.replace(reg_d, cross_allowlist=frozenset())
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(stripped, tmp_path, profile="cross",
                                  include=["research"], why="核对")
    assert "未允许该 cross 组合" in str(exc.value)


def test_cross_allowlist_in_policy_is_honoured_under_default(make_paths, tmp_path):
    """同上，但走真实的编译路径：策略里只有反方向的组合时，默认 profile 发起的正向 cross 被拒。"""
    reg_d = registry.compile_registry(make_paths(scope_policy=lambda d: (
        d.__setitem__("default_profile", "work"),
        d.__setitem__("cross_allowlist", [{"base": "research", "include": ["work"]}]))))
    with pytest.raises(ProfileError) as exc:
        resolve_effective_profile(reg_d, tmp_path, profile="cross",
                                  include=["research"], why="核对")
    assert "未允许该 cross 组合" in str(exc.value)

