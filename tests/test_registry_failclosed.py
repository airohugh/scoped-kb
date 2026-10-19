"""编译器 fail-closed 缺口的负向测试。

这些缺口的共同特征是**不报错**：配置看起来合法、registry 编译通过，
但作用域边界已经破了。覆盖六类缺口。
"""
import shutil

import conftest
import pytest

from scoped_kb import registry

# ---- 1. 指纹必须钉规范化后的实际 roots，不是原始声明字符串 --------------

def test_fingerprint_tracks_resolved_target_not_declared_string(fixture_base, tmp_path):
    """两份配置的声明字符串完全相同，但 root 真实指向不同 → 指纹必须不同。

    只按声明字符串算指纹时二者相等，于是「同一段文本、指向另一棵树」
    可以顶着已授权的指纹通过——中央钉住的东西就名存实亡了。
    """
    a = conftest._materialize(fixture_base, tmp_path / "A", fixture_base)
    altbase = tmp_path / "altbase"
    shutil.copytree(fixture_base, altbase)
    b = conftest._materialize(fixture_base, tmp_path / "B", altbase)

    fa = registry.print_fingerprints(a)
    fb = registry.print_fingerprints(b)
    assert fa["a-decisions"] != fb["a-decisions"], "声明字符串相同但真实指向不同，指纹却一样"
    assert fa["m-global"] != fb["m-global"]


def test_repo_root_change_without_central_update_fails(make_paths, fixture_base, tmp_path):
    """本地把 repo_root 改指向别处、roots 字符串不变，而中央指纹未更新 → 编译失败。

    这是「保留已授权 ID、偷换 root」的一个变体：不动 roots 那一行，
    改上面的 repo_root 就能整体换树。
    """
    alt = tmp_path / "altrepo"
    shutil.copytree(fixture_base / "repo_a", alt)
    drifted = make_paths(repos=lambda d: d.__setitem__("repo_root", str(alt)))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "声明指纹不一致" in str(exc.value)


def test_symlinked_root_retarget_changes_fingerprint(fixture_base, tmp_path):
    """root 是符号链接、指向被换掉时，指纹必须变。

    resolve() 会跟随符号链接，所以「真实指向」才是该钉的东西。
    """
    def build(target_name):
        base = tmp_path / f"base_{target_name}"
        shutil.copytree(fixture_base, base)
        real = tmp_path / target_name
        real.mkdir(exist_ok=True)
        link = base / "repo_a" / "docs" / "decisions"
        shutil.rmtree(link)
        link.symlink_to(real, target_is_directory=True)
        return conftest._materialize(fixture_base, tmp_path / f"cfg_{target_name}", base)

    fp1 = registry.print_fingerprints(build("t1"))["a-decisions"]
    fp2 = registry.print_fingerprints(build("t2"))["a-decisions"]
    assert fp1 != fp2


# ---- 2. manifest assignment 的合法目标 ----------------------------------

def test_assignment_to_unrelated_known_collection_fails(make_paths):
    """assignment 指向一个**已知但无关**的 collection → 必须拒绝。

    b-notes 是 research scope 的真实 collection，语法上「已知」，
    但把一份全局笔记指派给它等于跨域搬运归属，且不会报错。
    """
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["assignments"].__setitem__("confirmed.md", "b-notes"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "assignment 目标非法" in str(exc.value)
    assert "b-notes" in str(exc.value)


def test_assignment_to_base_itself_is_allowed(fixture_paths):
    """反向确认：指向基础 collection 自身是情形 1 的正当用法，不得误伤。"""
    reg = registry.compile_registry(fixture_paths)
    assert reg.manifest["m-global"]["confirmed.md"] == "m-global"


def test_assignment_to_other_bases_partition_fails(make_paths):
    """指向**别的**基础来源的分区同样非法（本夹具只有一个基础来源，
    用一个不属于 m-global partitions 的已知 ID 等价验证）。"""
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["assignments"].__setitem__("mixed_work.md", "a-decisions"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "assignment 目标非法" in str(exc.value)


# ---- 3. default_partition 必须是自己的 quarantine 分区 -------------------

def test_default_partition_cannot_be_active_base(make_paths):
    """default 指向 active 基础来源 = 未审阅的文件直接进 active global。

    这是最危险的一种：放置位置直接授予了权限，与「放置位置不授予权限」的原则正面冲突，
    而且完全不报错。
    """
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         .__setitem__("default_partition", "m-global"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "default_partition" in str(exc.value)


def test_default_partition_cannot_be_active_partition(make_paths):
    """default 指向 active 分区（m-global-work）同样不行——未审阅即可被 work 读到。"""
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         .__setitem__("default_partition", "m-global-work"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "default_partition" in str(exc.value)


def test_default_partition_cannot_be_foreign_collection(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         .__setitem__("default_partition", "b-notes"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "default_partition" in str(exc.value)


# ---- 4. confirmed_manifest 必须有对应 manifest_partitions ---------------

def test_confirmed_manifest_without_partitions_fails(make_paths):
    """index_policy=confirmed_manifest 却没有 manifest 配置 → 编译失败。

    不挡的话，collection_for_path 会在 manifest_default 上 KeyError，
    或者更糟——退化成把整个 root 当 active global。
    """
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"].pop("m-global"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "缺少 manifest_partitions" in str(exc.value)


# ---- 5. profile 不得含 quarantine / excluded ----------------------------

def test_profile_with_quarantine_partition_fails(make_paths):
    """把 review-only quarantine 塞进 profile → 编译失败。

    unresolved-mixed 对所有 profile 均应不可读，
    连 cross 都不可达。靠人眼校对 profile 列表挡不住这个。
    """
    drifted = make_paths(scope_policy=lambda d: d["profiles"]["work"]
                         .append("m-global-unresolved"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "quarantine" in str(exc.value)


def test_profile_with_excluded_collection_fails(make_paths):
    def patch(d):
        d["collections"]["b-notes"]["index_policy"] = "excluded"
    drifted = make_paths(scope_policy=patch)
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "excluded" in str(exc.value)


# ---- 6a. schema_version 值校验 -----------------------------------------

def test_bad_schema_version_in_machine_sources(make_paths):
    drifted = make_paths(machine_sources=lambda d: d.__setitem__("schema_version", 99))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


def test_bad_schema_version_in_scope_policy(make_paths):
    drifted = make_paths(scope_policy=lambda d: d.__setitem__("schema_version", 0))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "schema_version" in str(exc.value)


# ---- 6b. quarantine 必须有 reason 与 exit_path --------------------------

def test_quarantine_partition_without_reason_fails(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-unresolved"].pop("quarantine_reason"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "quarantine_reason" in str(exc.value)


def test_quarantine_partition_without_exit_path_fails(make_paths):
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-unresolved"].pop("exit_path"))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "exit_path" in str(exc.value)


def test_top_level_quarantine_without_reason_fails(make_paths):
    """顶层 collection 的 quarantine 同样要求 reason 与 exit_path。"""
    def patch(d):
        d["collections"]["b-notes"]["index_policy"] = "quarantine"
        d["profiles"]["research"] = ["m-global"]      # 先移出 profile，避免撞第 5 条
    drifted = make_paths(scope_policy=patch)
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "quarantine_reason" in str(exc.value)


def test_empty_string_reason_is_not_enough(make_paths):
    """空字符串不算填了——必须非空。"""
    drifted = make_paths(scope_policy=lambda d: d["manifest_partitions"]["m-global"]
                         ["partitions"]["m-global-unresolved"]
                         .__setitem__("exit_path", "   "))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(drifted)
    assert "exit_path" in str(exc.value)


# ---- 兜底：合法配置不得被上述任何一条误伤 -------------------------------

def test_ok_fixture_still_compiles(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert len(reg.collections) == 5
    assert reg.manifest_default["m-global"] == "m-global-unresolved"


# ---- repo_roots 的规范化唯一性与 profile 有效性 ---------------

def test_repo_roots_duplicate_after_normalization_fails(make_paths):
    """`/repo` 与 `/repo/.` 是同一个目录，resolve 后完全相同。

    不挡的后果：resolver 拿到两条祖先匹配、长度相同、profile 不同，
    只能靠 max() 的稳定性静默取首项——同一个 cwd 解析出哪个 profile
    取决于 dict 的插入顺序，而这不会报错。
    """
    def patch(d):
        k = next(iter(d["repo_roots"]))          # 已是替换后的绝对路径
        d["repo_roots"][k + "/."] = "research"
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(make_paths(scope_policy=patch))
    assert "repo_roots 规范化后重复" in str(exc.value)


def test_repo_roots_duplicate_same_profile_also_fails(make_paths):
    """即使两条映射到**同一个** profile 也拒绝——重复本身就是配置错误，
    放行会让「改一处忘另一处」变成潜伏问题。"""
    def patch(d):
        k = next(iter(d["repo_roots"]))
        d["repo_roots"][k + "/./"] = d["repo_roots"][k]
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(make_paths(scope_policy=patch))
    assert "repo_roots 规范化后重复" in str(exc.value)


def test_repo_roots_trailing_slash_duplicate_fails(make_paths):
    def patch(d):
        k = next(iter(d["repo_roots"]))
        d["repo_roots"][k + "/../" + k.rsplit("/", 1)[-1]] = "research"
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(make_paths(scope_policy=patch))
    assert "repo_roots 规范化后重复" in str(exc.value)


@pytest.mark.parametrize("bad", ["nope", "", 123, None],
                         ids=["unknown", "empty", "int", "null"])
def test_repo_roots_value_must_be_known_profile(make_paths, bad):
    """映射值必须是已注册 profile。写错的话该仓库的 cwd 会解析出一个
    allowed_for() 查不到的 profile，报错时机推迟到检索那一刻。"""
    def patch(d):
        k = next(iter(d["repo_roots"]))
        d["repo_roots"][k] = bad
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(make_paths(scope_policy=patch))
    assert "repo_roots" in str(exc.value)


def test_ok_fixture_repo_roots_still_compile(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert len(reg.repo_roots) == 2
    assert len({r for r, _ in reg.repo_roots}) == 2
