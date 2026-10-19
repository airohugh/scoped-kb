"""config.json：仓库声明路径、skip_filenames、provenance_fields。"""
import json

import conftest
import pytest

from scoped_kb import chunking, provenance, registry, settings


def _with_config(paths, tmp_path, body):
    """给一份 config_paths 加上写好内容的 config.json（body 为 dict 或原始文本）。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return {**paths, "config": cfg}


def _cfg(**fields):
    return {"schema_version": 1, **fields}


# ---- ① 缺省走默认 ------------------------------------------------------

def test_missing_config_falls_back_to_defaults(fixture_paths, tmp_path):
    assert "config" not in fixture_paths
    reg = registry.compile_registry(fixture_paths)
    assert reg.skip_filenames == ()
    assert reg.provenance_fields == registry.DEFAULT_PROVENANCE_FIELDS
    # 指向不存在文件的 config 键同样按缺省处理
    absent = {**fixture_paths, "config": tmp_path / "nope.json"}
    assert registry.compile_registry(absent).registry_hash == reg.registry_hash


def test_load_config_defaults_when_path_is_none(tmp_path):
    cfg = registry.load_config(None)
    assert cfg == {"repos": [], "skip_filenames": (),
                   "provenance_fields": registry.DEFAULT_PROVENANCE_FIELDS,
                   "trace": {"task_id_env": "SCOPED_KB_TASK_ID",
                             "session_id_env": ("SCOPED_KB_SESSION_ID",)}}


def test_valid_config_is_read(fixture_paths, tmp_path):
    paths = _with_config(fixture_paths, tmp_path, _cfg(
        repos=[], skip_filenames=["INDEX.md", "TOC.md"],
        provenance_fields=["trust_tier"]))
    reg = registry.compile_registry(paths)
    assert reg.skip_filenames == ("INDEX.md", "TOC.md")
    assert reg.provenance_fields == ("trust_tier",)


def test_repos_expand_home_and_resolve_relative_to_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    (tmp_path / "config.json").write_text(json.dumps(_cfg(
        repos=["~/r1/collections.json", "sub/collections.json"])), encoding="utf-8")
    got = registry.load_config(tmp_path / "config.json")["repos"]
    assert got == [tmp_path / "h/r1/collections.json", tmp_path / "sub/collections.json"]


# ---- ② 未知字段拒绝 ----------------------------------------------------

def test_unknown_field_is_rejected(fixture_paths, tmp_path):
    paths = _with_config(fixture_paths, tmp_path, _cfg(skip_filename=["x.md"]))
    with pytest.raises(registry.RegistryError, match="未知字段"):
        registry.compile_registry(paths)


# ---- ③ 类型错拒绝 ------------------------------------------------------

@pytest.mark.parametrize("body", [
    _cfg(repos="a.json"),
    _cfg(repos=[1]),
    _cfg(skip_filenames="README.md"),
    _cfg(skip_filenames=[None]),
    _cfg(skip_filenames=["docs/README.md"]),
    _cfg(skip_filenames=[""]),
    _cfg(provenance_fields="trust_tier"),
    _cfg(provenance_fields=[["trust_tier"]]),
    {"schema_version": True},
    {"schema_version": 2},
    {"skip_filenames": []},                       # 缺 schema_version
    [],                                           # 顶层不是对象
    "{ not json",
])
def test_bad_config_is_rejected(fixture_paths, tmp_path, body):
    paths = _with_config(fixture_paths, tmp_path, body)
    with pytest.raises(registry.RegistryError):
        registry.compile_registry(paths)


# ---- ④ skip_filenames 进 hash，provenance_fields 不进 -------------------

def test_skip_filenames_changes_registry_hash(fixture_paths, tmp_path):
    base = registry.compile_registry(fixture_paths).registry_hash
    skipped = registry.compile_registry(
        _with_config(fixture_paths, tmp_path, _cfg(skip_filenames=["a.md"]))).registry_hash
    assert skipped != base


def test_skip_filenames_hash_ignores_order_and_duplicates(fixture_paths, tmp_path):
    one = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(skip_filenames=["a.md", "b.md"]))).registry_hash
    two = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(skip_filenames=["b.md", "a.md", "a.md"]))).registry_hash
    assert one == two


def test_provenance_fields_do_not_change_registry_hash(fixture_paths, tmp_path):
    base = registry.compile_registry(fixture_paths).registry_hash
    changed = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(provenance_fields=["trust_tier"]))).registry_hash
    assert changed == base


# ---- ⑤ skip_filenames 里的文件不进 scan / classify_all -------------------

def test_skipped_filename_is_not_scanned_or_classified(fixture_paths, tmp_path):
    plain = registry.compile_registry(fixture_paths)
    assert "a.md" in {p.name for _, _, p in chunking.scan(plain)}
    assert "a.md" in {f.name for rows in registry.classify_all(plain).values()
                      for f, _, _ in rows}

    skipping = registry.compile_registry(
        _with_config(fixture_paths, tmp_path, _cfg(skip_filenames=["a.md"])))
    assert "a.md" not in {p.name for _, _, p in chunking.scan(skipping)}
    assert "a.md" not in {f.name for rows in registry.classify_all(skipping).values()
                          for f, _, _ in rows}
    # 只跳指定的名字，别的文件照扫
    assert "b.md" in {p.name for _, _, p in chunking.scan(skipping)}


# ---- 默认路径来自 settings.CONFIG_DIR ----------------------------------

def test_default_paths_repos_come_from_config_json(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps(_cfg(repos=[str(tmp_path / "r/collections.json")])), encoding="utf-8")
    assert registry.default_paths()["repos"] == [tmp_path / "r/collections.json"]


# ---- provenance_fields 决定出处行读哪些字段 -----------------------------

def test_provenance_fields_limit_what_is_rendered(fixture_paths, tmp_path):
    (tmp_path / "s.md").write_text(
        "---\ntrust_tier: T3\nsource_author: Jane Doe\ncaptured_at: 2026-08-05\n---\n\n正文\n",
        encoding="utf-8")

    def _with_notes_dir(reg):
        # 补一个 owner "t"，基准目录是 tmp_path；位置键 "t/s.md" 即这份测试文件，
        # 它归属的 collection 就是下面用的 "x"（出处回读会核对归属）
        return conftest.attribute_notes_dir(
            reg, tmp_path, default="x", kinds={"x": "note"})

    # 缺省不含作者字段；要显示作者，得在 provenance_fields 里自己加
    default = _with_notes_dir(registry.compile_registry(fixture_paths))
    assert "Jane Doe" not in provenance.provenance_line("t/s.md", "x", default)

    everything = _with_notes_dir(registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(provenance_fields=[
            *registry.DEFAULT_PROVENANCE_FIELDS, "source_author"]))))
    assert "Jane Doe" in provenance.provenance_line("t/s.md", "x", everything)

    only_tier = _with_notes_dir(registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(provenance_fields=["trust_tier"]))))
    line = provenance.provenance_line("t/s.md", "x", only_tier)
    assert "T3" in line
    assert "Jane Doe" not in line and "2026-08-05" not in line
    assert provenance.read_provenance(only_tier, "t/s.md", only_tier.provenance_fields) == {
        "trust_tier": "T3"}


# ---- trace：跨域检索的可追溯标识变量名 ---------------------------------

def test_trace_defaults_are_neutral_scoped_kb_names(fixture_paths):
    reg = registry.compile_registry(fixture_paths)
    assert reg.trace_task_env == "SCOPED_KB_TASK_ID"
    assert reg.trace_session_envs == ("SCOPED_KB_SESSION_ID",)


def test_trace_is_read_into_registry(fixture_paths, tmp_path):
    reg = registry.compile_registry(_with_config(fixture_paths, tmp_path, _cfg(
        trace={"task_id_env": "MY_TASK", "session_id_env": ["MY_SESS", "MY_THREAD"]})))
    assert reg.trace_task_env == "MY_TASK"
    assert reg.trace_session_envs == ("MY_SESS", "MY_THREAD")


def test_trace_partial_keeps_defaults_for_the_rest(fixture_paths, tmp_path):
    only_task = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(trace={"task_id_env": "MY_TASK"})))
    assert only_task.trace_task_env == "MY_TASK"
    assert only_task.trace_session_envs == registry.DEFAULT_TRACE_SESSION_ENVS
    only_sess = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(trace={"session_id_env": ["MY_SESS"]})))
    assert only_sess.trace_task_env == registry.DEFAULT_TRACE_TASK_ENV
    assert only_sess.trace_session_envs == ("MY_SESS",)


@pytest.mark.parametrize("trace", [
    "MY_TASK",                                           # 不是对象
    ["MY_TASK"],
    {"task_id": "MY_TASK"},                              # 未知子字段
    {"task_id_env": "MY_TASK", "extra": 1},
    {"task_id_env": ""},                                 # 空 / 空白 / 非字符串
    {"task_id_env": "  "},
    {"task_id_env": 1},
    {"task_id_env": ["MY_TASK"]},
    {"task_id_env": None},
    {"session_id_env": "MY_SESS"},                       # 必须是列表，不接受裸字符串
    {"session_id_env": []},                              # 空列表
    {"session_id_env": [""]},
    {"session_id_env": [1]},
    {"session_id_env": None},
    {"task_id_env": "SAME", "session_id_env": ["SAME"]},     # 同一个变量不能身兼两职
    {"task_id_env": "SCOPED_KB_SESSION_ID"},                 # 与缺省的会话变量撞名
], ids=["str", "list", "unknown_key", "unknown_key_with_valid", "empty_task", "blank_task",
        "int_task", "list_task", "null_task", "bare_str_session", "empty_list_session",
        "empty_name_session", "int_session", "null_session", "same_var", "clash_with_default"])
def test_bad_trace_is_rejected(fixture_paths, tmp_path, trace):
    paths = _with_config(fixture_paths, tmp_path, _cfg(trace=trace))
    with pytest.raises(registry.RegistryError) as exc:
        registry.compile_registry(paths)
    assert "trace" in str(exc.value)


def test_trace_does_not_change_registry_hash(fixture_paths, tmp_path):
    """变量名只决定去哪读标识，不改变谁能读到什么，所以不进 hash（改了不触发重建）。"""
    base = registry.compile_registry(fixture_paths).registry_hash
    changed = registry.compile_registry(_with_config(
        fixture_paths, tmp_path, _cfg(trace={"task_id_env": "MY_TASK",
                                             "session_id_env": ["MY_SESS"]}))).registry_hash
    assert changed == base
