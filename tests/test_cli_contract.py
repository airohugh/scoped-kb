"""CLI 契约：search / pack 走唯一候选 helper、输出回显、cross 的可追溯 ID 与落盘留痕、维护闸。
"""
import argparse
import inspect
import json
import sys

import pytest

from scoped_kb import cli, registry, scope, search, settings
from scoped_kb.provenance import PROVENANCE_PREFIX
from scoped_kb.scope import Effective, ProfileError
from scoped_kb.secret_guard import SECRET_GUARD_EXIT


@pytest.fixture
def reg(fixture_paths):
    return registry.compile_registry(fixture_paths)


def _eff(reg, profile="work"):
    return Effective(profile, profile, (), "",
                     tuple(sorted(set(reg.allowed_for(profile)))),
                     reg.registry_hash)


def _cross_eff(reg):
    return Effective("work", "cross", ("research",), "核对研究侧判例",
                     tuple(sorted(set(reg.allowed_for("work")) |
                                  set(reg.allowed_for("research")))),
                     reg.registry_hash)


def _cross_log_lines():
    path = settings.STATE_DIR / scope.CROSS_LOG_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- 输出回显：必须是实际 collection IDs，不是计数 ----------------------

def test_banner_lists_actual_collection_ids(reg, capsys):
    """输出明确显示 effective profile 与**实际 collections**。

    只打 `collections=10` 在验收上等于没有——分辨不出这 10 个里有没有混进
    另一个域，而那正是本件唯一要证明的事。
    """
    cli.print_scope_banner(_eff(reg))
    out = capsys.readouterr().out
    for cid in reg.allowed_for("work"):
        assert cid in out, f"{cid} 未出现在回显里"
    assert "collections=" not in out, "不得只打计数"


def test_banner_shows_profiles_and_hash(reg, capsys):
    cli.print_scope_banner(_eff(reg))
    out = capsys.readouterr().out
    assert "base=work" in out and "effective=work" in out
    assert reg.registry_hash[:8] in out


def test_banner_shows_cross_include_and_why(reg, capsys):
    cli.print_scope_banner(_cross_eff(reg))
    out = capsys.readouterr().out
    assert "include=research" in out
    assert "核对研究侧判例" in out


def test_banner_never_leaks_forbidden_ids(reg, capsys):
    """work 的回显里不得出现只属于 research 的 collection。"""
    cli.print_scope_banner(_eff(reg, "work"))
    out = capsys.readouterr().out
    exclusive = set(reg.allowed_for("research")) - set(reg.allowed_for("work"))
    assert exclusive, "夹具必须真的有 research 独占的 collection"
    for cid in exclusive:
        assert cid not in out, f"回显泄漏了 {cid}"


# ---- 没有 --project ------------------------------------------------------

# ---- cross 必须有真实 task/session ID（变量名来自 config.json 的 trace）----

@pytest.fixture
def reg_trace(fixture_paths, tmp_path, trace_vars):
    """trace 配成非缺省变量名的 registry；同时清掉进程里可能存在的这些变量。"""
    cfg = tmp_path / "trace-config.json"
    cfg.write_text(json.dumps({"schema_version": 1, **trace_vars.config}), encoding="utf-8")
    return registry.compile_registry({**fixture_paths, "config": cfg})


@pytest.fixture(autouse=True)
def _clean_trace_env(monkeypatch, trace_vars):
    for name in (trace_vars.task, *trace_vars.sessions,
                 "SCOPED_KB_TASK_ID", "SCOPED_KB_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)


def test_cross_without_session_id_refuses(reg_trace, trace_vars, monkeypatch):
    monkeypatch.setenv(trace_vars.task, "t-1")
    with pytest.raises(ProfileError) as exc:
        scope._require_trace_ids(reg_trace)
    msg = str(exc.value)
    assert "KB_TEST_SESSION_ID" in msg and "KB_TEST_RUN_ID" in msg, "要列出实际配置的变量名"
    assert trace_vars.task not in msg.split("缺少可追溯标识:")[1].splitlines()[0]


def test_cross_without_task_id_refuses(reg_trace, trace_vars, monkeypatch):
    monkeypatch.setenv(trace_vars.sessions[0], "s-1")
    with pytest.raises(ProfileError) as exc:
        scope._require_trace_ids(reg_trace)
    assert trace_vars.task in str(exc.value)


def test_blank_ids_count_as_missing(reg_trace, trace_vars, monkeypatch):
    monkeypatch.setenv(trace_vars.sessions[0], "   ")
    monkeypatch.setenv(trace_vars.task, "")
    with pytest.raises(ProfileError) as exc:
        scope._require_trace_ids(reg_trace)
    msg = str(exc.value)
    assert trace_vars.sessions[0] in msg and trace_vars.task in msg


def test_both_ids_present_returns_them(reg_trace, trace_vars, monkeypatch):
    monkeypatch.setenv(trace_vars.sessions[0], " s-1 ")
    monkeypatch.setenv(trace_vars.task, "t-1")
    assert scope._require_trace_ids(reg_trace) == ("t-1", "s-1")


def test_never_writes_unknown_placeholder():
    """占位会让「已记录」这个信号本身失效——一条跨域记录无法回接到
    发起任务，事后追责与复盘都断链。"""
    src = inspect.getsource(scope._require_trace_ids)
    assert "unknown" not in src.lower()


def test_later_session_variable_is_also_accepted(reg_trace, trace_vars, monkeypatch):
    """session_id_env 是按顺序取第一个非空的列表：前面的没设，就用后面的。"""
    monkeypatch.setenv(trace_vars.sessions[1], "cs-1")
    monkeypatch.setenv(trace_vars.task, "t-1")
    assert scope._require_trace_ids(reg_trace) == ("t-1", "cs-1")


def test_earlier_session_variable_wins_over_later(reg_trace, trace_vars, monkeypatch):
    monkeypatch.setenv(trace_vars.sessions[0], "first")
    monkeypatch.setenv(trace_vars.sessions[1], "second")
    monkeypatch.setenv(trace_vars.task, "t-1")
    assert scope._require_trace_ids(reg_trace) == ("t-1", "first")


def test_any_variable_names_can_be_configured(fixture_paths, tmp_path, monkeypatch):
    """换成别的变量名也能用：名字完全来自配置，库里没有写死的变量名。"""
    cfg = tmp_path / "other-trace.json"
    cfg.write_text(json.dumps({"schema_version": 1, "trace": {
        "task_id_env": "OTHER_JOB", "session_id_env": ["OTHER_RUN"]}}), encoding="utf-8")
    other = registry.compile_registry({**fixture_paths, "config": cfg})
    monkeypatch.setenv("OTHER_JOB", "job-9")
    monkeypatch.setenv("OTHER_RUN", "run-9")
    assert scope._require_trace_ids(other) == ("job-9", "run-9")


def test_default_variable_names_are_scoped_kb_prefixed(reg, monkeypatch):
    """没配 trace 时用缺省名；配了之后缺省名不再生效（见下一条）。"""
    monkeypatch.setenv("SCOPED_KB_TASK_ID", "t-d")
    monkeypatch.setenv("SCOPED_KB_SESSION_ID", "s-d")
    assert scope._require_trace_ids(reg) == ("t-d", "s-d")


def test_default_names_are_ignored_once_trace_is_configured(reg_trace, monkeypatch):
    monkeypatch.setenv("SCOPED_KB_TASK_ID", "t-d")
    monkeypatch.setenv("SCOPED_KB_SESSION_ID", "s-d")
    with pytest.raises(ProfileError):
        scope._require_trace_ids(reg_trace)


def test_resolve_for_cli_reads_trace_from_installed_config(
        install_config, trace_vars, fixture_base, monkeypatch):
    """CLI 入口走的是配置目录里的 config.json，不是测试直接塞的 registry。"""
    install_config(config=trace_vars.config)
    monkeypatch.chdir(fixture_base / "repo_a")
    args = argparse.Namespace(profile="cross", include=["research"], why="核对")
    monkeypatch.setenv(trace_vars.task, "t-1")
    monkeypatch.setenv(trace_vars.sessions[1], "s-2")
    _reg, eff, trace = cli._resolve_for_cli(args)
    assert eff.effective == "cross" and trace == ("t-1", "s-2")


def test_resolve_for_cli_refuses_cross_without_ids(
        install_config, trace_vars, fixture_base, monkeypatch, capsys):
    install_config(config=trace_vars.config)
    monkeypatch.chdir(fixture_base / "repo_a")
    args = argparse.Namespace(profile="cross", include=["research"], why="核对")
    with pytest.raises(SystemExit) as exc:
        cli._resolve_for_cli(args)
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    err = capsys.readouterr().err
    assert trace_vars.task in err and trace_vars.sessions[0] in err


def test_same_domain_needs_no_trace_ids(install_config, fixture_base, monkeypatch):
    install_config()
    monkeypatch.chdir(fixture_base / "repo_a")
    args = argparse.Namespace(profile=None, include=None, why=None)
    _reg, eff, trace = cli._resolve_for_cli(args)
    assert eff.effective == "work" and trace == (None, None)


def test_resolve_for_cli_uses_default_profile(install_config, tmp_path, monkeypatch):
    """配了 default_profile，从仓库根之外的目录调用也能同域检索。"""
    install_config(scope_policy=lambda d: d.__setitem__("default_profile", "research"))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    args = argparse.Namespace(profile=None, include=None, why=None)
    _reg, eff, trace = cli._resolve_for_cli(args)
    assert eff.base == "research" and trace == (None, None)


def test_resolve_for_cli_without_default_profile_fails_closed(
        install_config, tmp_path, monkeypatch, capsys):
    install_config()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    args = argparse.Namespace(profile=None, include=None, why=None)
    with pytest.raises(SystemExit) as exc:
        cli._resolve_for_cli(args)
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    assert "不属于任何已注册仓库根" in capsys.readouterr().err


# ---- search 共用同一 helper ---------------------------------------------

def test_search_uses_the_single_candidate_helper():
    """候选只有一个来源：cmd_search 不自拼 SQL。"""
    src = inspect.getsource(cli.cmd_search)
    assert "retrieve_candidates" in src, "cmd_search 没走唯一 helper"
    assert "FROM knowledge_index" not in src, "cmd_search 仍在自拼 SQL"
    assert "embedding <=>" not in src, "cmd_search 仍在自拼向量 SQL"


def test_search_and_pack_use_same_helper():
    """同一个候选 helper，不再各拼 SQL。"""
    for fn in (cli.cmd_search, cli.cmd_pack):
        src = inspect.getsource(fn)
        assert "retrieve_candidates" in src, f"{fn.__name__} 没走唯一 helper"
        assert "FROM knowledge_index" not in src, f"{fn.__name__} 仍在自拼 SQL"
        assert "embedding <=>" not in src, f"{fn.__name__} 仍在自拼向量 SQL"


def test_search_does_not_read_project_id_for_filtering():
    """任何权限判断、候选查询都不得读 project_id（表里没有这一列，归属只由 registry 决定）。"""
    for fn in (cli.cmd_search, search.retrieve_candidates):
        src = inspect.getsource(fn)
        assert "project_id" not in src


def test_pack_does_not_read_project_id_for_filtering():
    src = inspect.getsource(cli.cmd_pack)
    assert "project_id = " not in src and "project_id=" not in src


def test_pack_budget_strategy_runs_after_filtering():
    """pack 的覆盖面+预算策略只在候选集过滤之后执行。"""
    src = inspect.getsource(cli.cmd_pack)
    assert src.index("retrieve_candidates") < src.index("select_for_pack")
    assert "want_new_file" in inspect.getsource(cli.select_for_pack)


def test_scope_error_exit_code_used_by_cli():
    """兜底 handler 在 main 里，不在 build_parser 里。"""
    src = inspect.getsource(cli.main)
    assert "SCOPE_ERROR_EXIT" in src, "CLI 必须用统一的非零退出码"


# ---- B：读路径的维护闸 --------------------------------------------------

def _search_args():
    return argparse.Namespace(query="任意", limit=5, profile=None, include=None, why=None)


@pytest.fixture
def repo_a_env(install_config, fixture_base, monkeypatch):
    """装好配置目录并站进 repo_a（work）。"""
    install_config()
    monkeypatch.chdir(fixture_base / "repo_a")


def test_maintenance_gate_blocks_reads(repo_a_env, gate, monkeypatch, capsys):
    """search 在 profile 解析完成后、connect/embed 之前检查维护闸。

    迁移期索引处于「旧行已清、新行未写」的中间态，返回半份结果比报错更危险——
    调用方拿到的是看似正常的答案。

    **断言走 cmd_search 而不是 _resolve_for_cli**：闸不在解析函数里，
    因为在那里抛异常会让 eff/trace 还没交回调用方，被拒的 cross 一条流水都留不下
    （见 test_generation_gate.py）。对着真实入口断言，搬家不会让这条覆盖失效。
    """
    gate("registry 迁移中")
    monkeypatch.setattr(cli, "connect", lambda: pytest.fail("维护期不该建连接"))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_search(_search_args())
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    err = capsys.readouterr().err
    assert "索引维护中" in err
    assert "registry 迁移中" in err
    assert "不是「没有命中」" in err


def test_maintenance_block_still_echoes_profile(repo_a_env, gate, monkeypatch, capsys):
    """拒绝时仍要回显 profile——否则排查不出是哪个 profile 被挡。"""
    gate("迁移中")
    monkeypatch.setattr(cli, "connect", lambda: pytest.fail("维护期不该建连接"))
    with pytest.raises(SystemExit):
        cli.cmd_search(_search_args())
    assert "base=work" in capsys.readouterr().out


def test_maintenance_check_happens_before_connect(repo_a_env, gate, monkeypatch):
    """闸命中时不得建数据库连接、不得向量化——白建连接白跑向量化没有意义。"""
    gate("迁移中")
    monkeypatch.setattr(cli, "connect", lambda: pytest.fail("维护期不该建连接"))
    monkeypatch.setattr(search, "embed", lambda *a, **k: pytest.fail("维护期不该向量化"))
    with pytest.raises(SystemExit):
        cli.cmd_search(_search_args())


def test_no_gate_is_a_noop(repo_a_env, gate):
    gate(None)
    args = argparse.Namespace(profile=None, include=None, why=None)
    _reg, eff, _trace = cli._resolve_for_cli(args)
    assert eff.base == "work"


# ---- C：cross 留痕落盘，覆盖全部结果 --------------------------------------

REQUIRED_KEYS = ("base_profile", "effective_profile", "include", "collections",
                 "policy_hash", "why", "paths", "task_id", "session_id")


def test_cross_log_has_all_required_fields(reg):
    scope._log_cross("问题", _cross_eff(reg), ("t-1", "s-1"), ["a.md"], "search")
    (line,) = _cross_log_lines()
    meta = line["metadata"]
    assert line["outcome"] == "ok" and meta["outcome"] == "ok"
    for k in REQUIRED_KEYS:
        assert k in meta, f"流水缺 {k}"
    assert meta["task_id"] == "t-1" and meta["session_id"] == "s-1"
    assert meta["base_profile"] == "work" and meta["effective_profile"] == "cross"
    assert meta["include"] == ["research"]
    assert meta["why"] == "核对研究侧判例"
    assert meta["paths"] == ["a.md"]
    assert meta["policy_hash"] == reg.registry_hash
    assert meta["collections"] == list(_cross_eff(reg).collections)
    assert line["ts"], "落盘的记录要有时间"


def test_cross_log_on_no_hit(reg):
    """无命中同样要留痕——「扩过域但什么也没读到」也是一次跨域读取行为。"""
    scope._log_cross("问题", _cross_eff(reg), ("t-1", "s-1"), [], "search",
                     outcome="ok", note="无命中")
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok" and "无命中" in line["summary"]
    assert line["metadata"]["paths"] == []
    for k in REQUIRED_KEYS:
        assert k in line["metadata"]


def test_cross_log_on_failed_outcome(reg):
    """被拒绝 / 中止的 cross 最需要留痕：范围已扩开，只是没交付。"""
    scope._log_cross("任务", _cross_eff(reg), ("t-1", "s-1"), ["a.md"], "search",
                     outcome="failed", note="输出阶段中止")
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed"
    assert "输出阶段中止" in line["summary"]
    assert line["metadata"]["outcome"] == "failed"
    assert line["module"] == "scoped-kb-search"
    for k in REQUIRED_KEYS:
        assert k in line["metadata"]


def test_cross_log_never_contains_unknown(reg):
    for outcome, note in (("ok", ""), ("ok", "无命中"), ("failed", "scope 拒绝")):
        scope._log_cross("q", _cross_eff(reg), ("t-1", "s-1"), [], "search",
                         outcome=outcome, note=note)
    lines = _cross_log_lines()
    assert len(lines) == 3
    for line in lines:
        for k in ("task_id", "session_id"):
            assert line["metadata"][k] and "unknown" not in str(line["metadata"][k]).lower()


def test_cross_log_appends_and_creates_state_dir(reg, tmp_path, monkeypatch):
    """只追加；状态目录不存在就建（含多级）。"""
    nested = tmp_path / "does" / "not" / "exist"
    monkeypatch.setattr(settings, "STATE_DIR", nested)
    for i in range(3):
        scope._log_cross(f"q{i}", _cross_eff(reg), ("t-1", "s-1"), [], "search")
    text = (nested / "cross-domain.jsonl").read_text(encoding="utf-8")
    assert len(text.splitlines()) == 3 and text.endswith("\n")
    assert [json.loads(x)["metadata"]["query_chars"]
            for x in text.splitlines()] == [2, 2, 2]


def test_cross_log_follows_state_dir_changes(reg, tmp_path, monkeypatch):
    """路径每次调用时现算，不是 import 时算死的常量。"""
    first, second = tmp_path / "s1", tmp_path / "s2"
    monkeypatch.setattr(settings, "STATE_DIR", first)
    scope._log_cross("a", _cross_eff(reg), ("t", "s"), [], "search")
    monkeypatch.setattr(settings, "STATE_DIR", second)
    scope._log_cross("b", _cross_eff(reg), ("t", "s"), [], "search")
    assert (first / "cross-domain.jsonl").exists() and (second / "cross-domain.jsonl").exists()


def _four_grams(query, n=4):
    return {query[i:i + n] for i in range(len(query) - n + 1)}


# 用生僻字造查询：留痕里固定文案不可能含它们的 4 字片段，命中即泄漏。
_SHORT_Q = "甲乙丙丁戊己庚辛"                               # 8 字，短于 40
_EXACT_Q = "壹贰叁肆伍陆柒捌玖拾" * 4                       # 恰好 40 字
_LONG_Q = "子丑寅卯辰巳午未申酉戌亥" * 9                     # 108 字


@pytest.mark.parametrize("query", [_SHORT_Q, _EXACT_Q, _LONG_Q],
                         ids=["short-8", "exactly-40", "long-108"])
def test_cross_log_contains_no_fragment_of_the_query(reg, query):
    """留痕里不出现查询文本的任何片段（短查询的前缀就是全文）；只记长度。"""
    scope._log_cross(query, _cross_eff(reg), ("t-1", "s-1"), [], "search")
    text = (settings.STATE_DIR / scope.CROSS_LOG_NAME).read_text(encoding="utf-8")
    # 这三条查询全由生僻字组成，固定文案里不会有它们的相邻两字组合：连 2 字片段都查
    leaked = [g for g in _four_grams(query, 2) if g in text]
    assert not leaked, f"查询片段落盘了: {leaked[:3]}"
    (line,) = _cross_log_lines()
    assert f"{len(query)} 字" in line["summary"]
    assert line["metadata"]["query_chars"] == len(query)


def test_cross_log_writes_nothing_to_stderr(reg, capsys):
    """留痕只落盘：stdout 与 stderr 上都不输出。"""
    scope._log_cross("问题", _cross_eff(reg), ("t-1", "s-1"), [], "search")
    captured = capsys.readouterr()
    assert captured.err == "" and captured.out == ""


def test_cross_log_write_failure_raises_scope_error(reg, tmp_path, monkeypatch):
    """状态目录是一个普通文件 → 建不出目录、写不进去 → 拒绝，不静默。"""
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "STATE_DIR", blocker)
    with pytest.raises(scope.ScopeError) as exc:
        scope._log_cross("q", _cross_eff(reg), ("t-1", "s-1"), [], "search")
    assert "跨域留痕写入失败" in str(exc.value)
    assert str(blocker) in str(exc.value)


def test_cross_log_unwritable_target_file_raises(reg, tmp_path, monkeypatch):
    """目录在，但 cross-domain.jsonl 本身是个目录 → 同样拒绝。"""
    state = tmp_path / "state"
    (state / scope.CROSS_LOG_NAME).mkdir(parents=True)
    monkeypatch.setattr(settings, "STATE_DIR", state)
    with pytest.raises(scope.ScopeError):
        scope._log_cross("q", _cross_eff(reg), ("t-1", "s-1"), [], "search")


# 「每个出口都留痕」由文件末尾的 test_cmd_search_cross_every_exit_leaves_a_log 按出口逐一实跑断言：
# 只数源码里 _log_cross 出现几次证明不了——少一个出口、多一处无关调用，计数照样过。


def test_pack_logs_cross_on_every_outcome():
    src = inspect.getsource(cli.cmd_pack)
    assert src.count("_log_cross") >= 2


def test_pack_no_hit_returns_zero():
    """pack 无命中是正常结果：打印提示并正常返回，不是 sys.exit。"""
    src = inspect.getsource(cli.cmd_pack)
    i = src.index("if not candidates:")
    j = src.index("select_for_pack")
    assert "sys.exit" not in src[i:j], "无命中不得非零退出"
    assert "return" in src[i:j]


def test_select_and_render_are_production_functions():
    """最终上下文要走生产的选择/渲染函数，所以它们必须是可复用的。"""
    assert callable(cli.select_for_pack)
    assert callable(cli.render_pack)
    src = inspect.getsource(cli.cmd_pack)
    assert "select_for_pack(" in src and "render_pack(" in src


def test_select_for_pack_preserves_collection_id():
    """选择函数必须保留 collection_id，否则验收无法断言归属。"""
    cands = [("a.md", "x" * 50, "c1", 0.9), ("b.md", "y" * 50, "c2", 0.8)]
    picked, used, files = cli.select_for_pack(cands, budget=10_000)
    assert {p[2] for p in picked} == {"c1", "c2"}
    assert used == 100 and files == {"a.md", "b.md"}


# ---- 经 cmd_search 真实落盘：每种 outcome 都留一行 -------------------------

# 库里存的是位置键 "<owner>/<相对路径>"（owner 是 repo_id），不是路径。
ROWS = [("repo_a/docs/decisions/a.md", "判例正文", "a-decisions", 0.91),
        ("repo_b/notes/b.md", "笔记正文", "b-notes", 0.80)]


@pytest.fixture
def cross_search(install_config, trace_vars, fixture_base, gate, monkeypatch):
    """repo_a 里发起合法 cross；索引与向量化被替换成可控的返回值，不碰数据库。"""
    install_config(config=trace_vars.config)
    monkeypatch.chdir(fixture_base / "repo_a")
    monkeypatch.setenv(trace_vars.task, "task-77")
    monkeypatch.setenv(trace_vars.sessions[0], "sess-77")
    gate(None)
    state = {"rows": ROWS, "raise": None}
    monkeypatch.setattr(cli, "connect", lambda: object())

    def _retrieve(conn, query, eff, limit):
        if state["raise"] is not None:
            raise state["raise"]
        return state["rows"]

    monkeypatch.setattr(cli, "retrieve_candidates", _retrieve)
    return state


def _cross_args(query="跨域问题", **kw):
    base = {"query": query, "limit": 5, "profile": "cross", "include": ["research"],
            "why": "核对研究侧判例"}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_search_cross_ok_writes_one_line(cross_search, capsys):
    cli.cmd_search(_cross_args())
    (line,) = _cross_log_lines()
    meta = line["metadata"]
    assert line["outcome"] == "ok" and line["module"] == "scoped-kb-search"
    assert meta["paths"] == [r[0] for r in ROWS]
    assert meta["task_id"] == "task-77" and meta["session_id"] == "sess-77"
    assert meta["why"] == "核对研究侧判例" and meta["include"] == ["research"]
    assert "b-notes" in meta["collections"], "留痕里要有 cross 并进来的 collection"
    captured = capsys.readouterr()
    assert captured.err == "", "stderr 上不再有留痕行"
    assert "判例正文" in captured.out


def test_cmd_search_cross_no_hit_writes_one_line(cross_search, capsys):
    cross_search["rows"] = []
    cli.cmd_search(_cross_args())
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok" and "无命中" in line["summary"]
    assert line["metadata"]["paths"] == []
    assert "没有命中" in capsys.readouterr().out


def test_cmd_search_cross_json_mode_also_logs(cross_search, fixture_base, capsys):
    cli.cmd_search(_cross_args(json=True))
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok"
    out = capsys.readouterr().out
    # 留痕里是位置键，交给调用方的是能直接打开的绝对路径
    assert json.loads(out)[0]["file_path"] == str(
        (fixture_base / "repo_a/docs/decisions/a.md").resolve()), "stdout 只有一个 JSON"


def test_cmd_search_cross_scope_rejection_writes_failed_line(cross_search, capsys):
    cross_search["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。\n详情")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_search(_cross_args())
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed"
    assert "scope 拒绝: registry 世代不一致" in line["summary"]
    assert line["metadata"]["paths"] == []
    assert line["metadata"]["task_id"] == "task-77"
    assert "registry 世代不一致" in capsys.readouterr().err


def test_cmd_search_maintenance_rejection_writes_failed_line(cross_search, gate, capsys):
    gate("迁移中")
    with pytest.raises(SystemExit):
        cli.cmd_search(_cross_args())
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and "索引维护中" in line["summary"]


def test_cmd_search_same_domain_writes_no_cross_log(cross_search, capsys):
    cli.cmd_search(argparse.Namespace(query="同域", limit=5, profile=None,
                                      include=None, why=None))
    assert not (settings.STATE_DIR / scope.CROSS_LOG_NAME).exists()


def test_cmd_search_cross_log_does_not_hold_full_query(cross_search):
    query = "很长的跨域查询" * 20
    cli.cmd_search(_cross_args(query=query))
    text = (settings.STATE_DIR / scope.CROSS_LOG_NAME).read_text(encoding="utf-8")
    assert query not in text
    assert not [g for g in _four_grams(query) if g in text]


def test_cmd_search_short_cross_query_is_not_logged_either(cross_search):
    cli.cmd_search(_cross_args(query=_SHORT_Q))
    text = (settings.STATE_DIR / scope.CROSS_LOG_NAME).read_text(encoding="utf-8")
    assert not [g for g in _four_grams(_SHORT_Q, 2) if g in text]


# ---- 输出失败的出口：先留痕、后输出；交付失败要补 failed ------------------------
#
# 合法 cross 尝试之后，每一个出口都要有流水：拒绝分支在打印诊断之前先留痕，
# 结果的格式化与输出放在同一个保护范围里，输出失败补一行 failed（ok 行已写过也补）。

class _BoomStream:
    """写入就抛指定异常的流；``only`` 非空时只在写入内容含该串时才抛。
    ``flush_only`` 时 write 总是成功、只有 flush 抛（管道断开常在 flush 才暴露）。"""

    def __init__(self, exc, only=None, flush_only=False):
        self.exc, self.only, self.flush_only = exc, only, flush_only

    def write(self, text):
        if not self.flush_only and (self.only is None or self.only in text):
            raise self.exc
        return len(text)

    def flush(self):
        if self.flush_only:
            raise self.exc


_OUT_FAULTS = [
    lambda: BrokenPipeError("管道已关闭"),
    lambda: UnicodeEncodeError("ascii", "跨域", 0, 1, "ordinal not in range(128)"),
]
_OUT_FAULT_IDS = ["BrokenPipeError", "UnicodeEncodeError"]


def _same_domain_args(**kw):
    return argparse.Namespace(query="同域", limit=5, profile=None, include=None,
                              why=None, **kw)


@pytest.mark.parametrize("make_exc", _OUT_FAULTS, ids=_OUT_FAULT_IDS)
@pytest.mark.parametrize("reject", ["scope", "maintenance"])
def test_cross_rejection_is_logged_before_diagnostic_is_printed(
        cross_search, gate, monkeypatch, make_exc, reject):
    """拒绝分支：stderr 已断，打印诊断抛异常——流水必须已经在了（先留痕、后输出）。"""
    if reject == "scope":
        cross_search["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。\n详情")
    else:
        gate("迁移中")
    exc = make_exc()
    monkeypatch.setattr(sys, "stderr", _BoomStream(exc))
    with pytest.raises(type(exc)) as got:
        cli.cmd_search(_cross_args())
    assert got.value is exc
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and line["metadata"]["paths"] == []
    assert "scope 拒绝" in line["metadata"]["note"]


@pytest.mark.parametrize("make_exc", _OUT_FAULTS, ids=_OUT_FAULT_IDS)
@pytest.mark.parametrize("rows,marker", [(ROWS, "判例正文"), ([], "没有命中")],
                         ids=["hits", "no-hit"])
def test_cross_body_output_failure_adds_failed_after_ok(
        cross_search, monkeypatch, make_exc, rows, marker):
    """正常取得结果、正文输出失败：留下 ok 之后又补一行 failed，事后能看出
    「检索成功、交付失败」。"""
    cross_search["rows"] = rows
    exc = make_exc()
    monkeypatch.setattr(sys, "stdout", _BoomStream(exc, only=marker))
    with pytest.raises(type(exc)) as got:
        cli.cmd_search(_cross_args())
    assert got.value is exc
    ok, failed = _cross_log_lines()
    assert ok["outcome"] == "ok" and failed["outcome"] == "failed"
    assert failed["metadata"]["outcome"] == "failed"
    assert "交付失败" in failed["metadata"]["note"]
    assert type(exc).__name__ in failed["metadata"]["note"]
    assert failed["metadata"]["paths"] == [], "failed 行不再重复路径"


@pytest.mark.parametrize("make_exc", _OUT_FAULTS, ids=_OUT_FAULT_IDS)
def test_cross_json_output_failure_adds_failed_after_ok(
        cross_search, monkeypatch, make_exc):
    exc = make_exc()
    monkeypatch.setattr(sys, "stdout", _BoomStream(exc, only="判例正文"))
    with pytest.raises(type(exc)) as got:
        cli.cmd_search(_cross_args(json=True))
    assert got.value is exc
    ok, failed = _cross_log_lines()
    assert ok["outcome"] == "ok" and failed["outcome"] == "failed"
    assert "交付失败" in failed["metadata"]["note"]


def test_cross_stdout_flush_failure_adds_failed_after_ok(cross_search, monkeypatch):
    """管道断开常在 flush 时才暴露：flush 也在保护范围内。"""
    exc = BrokenPipeError("flush 时才发现管道已断")
    monkeypatch.setattr(sys, "stdout", _BoomStream(exc, flush_only=True))
    monkeypatch.setattr(cli, "print_scope_banner", lambda _eff: None)
    with pytest.raises(BrokenPipeError):
        cli.cmd_search(_cross_args(json=True))
    ok, failed = _cross_log_lines()
    assert ok["outcome"] == "ok" and failed["outcome"] == "failed"


@pytest.mark.parametrize("make_exc", _OUT_FAULTS, ids=_OUT_FAULT_IDS)
@pytest.mark.parametrize("where", ["stderr-rejection", "stdout-body", "stdout-json"])
def test_same_domain_output_failures_still_write_no_cross_log(
        cross_search, monkeypatch, make_exc, where):
    """同域没有扩权动作：同样的输出故障下仍不写留痕，行为不变。"""
    exc = make_exc()
    kw = {}
    if where == "stderr-rejection":
        cross_search["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。")
        monkeypatch.setattr(sys, "stderr", _BoomStream(exc))
    else:
        monkeypatch.setattr(sys, "stdout", _BoomStream(exc, only="判例正文"))
        kw["json"] = where == "stdout-json"
    with pytest.raises(type(exc)):
        cli.cmd_search(_same_domain_args(**kw))
    assert not (settings.STATE_DIR / scope.CROSS_LOG_NAME).exists()


# ---- 写不进去：拒绝，CLI 以 SCOPE_ERROR_EXIT 退出，结果不交付 -------------

@pytest.fixture
def blocked_state(tmp_path, monkeypatch):
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(settings, "STATE_DIR", blocker)
    return blocker


def test_cmd_search_refuses_to_deliver_when_log_cannot_be_written(
        cross_search, blocked_state, capsys):
    with pytest.raises(scope.ScopeError):
        cli.cmd_search(_cross_args())
    out = capsys.readouterr().out
    assert "判例正文" not in out and "笔记正文" not in out, "没留上痕的跨域结果不得交付"


def test_main_exits_with_scope_error_code_when_log_cannot_be_written(
        cross_search, blocked_state, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "scoped-kb", "search", "跨域问题", "--profile", "cross",
        "--include", "research", "--why", "核对研究侧判例"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    captured = capsys.readouterr()
    assert "跨域留痕写入失败" in captured.err
    assert "判例正文" not in captured.out


def test_main_json_mode_prints_no_results_when_log_cannot_be_written(
        cross_search, blocked_state, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "scoped-kb", "search", "q", "--json", "--profile", "cross",
        "--include", "research", "--why", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    assert capsys.readouterr().out == ""


def test_rejected_cross_keeps_original_reason_when_log_also_fails(
        cross_search, blocked_state, monkeypatch, capsys):
    """被 scope 拒绝、同时留痕也写不进去：两个原因都要让人看得到。"""
    cross_search["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。")
    monkeypatch.setattr(sys, "argv", [
        "scoped-kb", "search", "q", "--profile", "cross",
        "--include", "research", "--why", "x"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    err = capsys.readouterr().err
    assert "registry 世代不一致" in err and "跨域留痕写入失败" in err


def test_same_domain_search_does_not_need_writable_state_dir(
        cross_search, blocked_state, capsys):
    """同域检索没有扩权动作，不写留痕，状态目录不可写也照常工作。"""
    cli.cmd_search(argparse.Namespace(query="同域", limit=5, profile=None,
                                      include=None, why=None))
    assert "判例正文" in capsys.readouterr().out


# ---- 任何异常退出，合法的 cross 尝试都要留下 failed 一行 ----------------------
#
# eff/trace 一旦解析出来，范围就已经被扩开过。只捕获 ScopeError 的话，复编译抛
# RegistryError、数据库或 embedding 报错时整次尝试没有流水。

class _EmbedBoom(Exception):
    pass


def _db_error():
    import psycopg2
    return psycopg2.OperationalError("connection to server lost: 机密查询正文")


@pytest.mark.parametrize("make_exc", [
    lambda: registry.RegistryError("配置编译失败: 机密查询正文"),
    _db_error,
    lambda: _EmbedBoom("embedding 服务不可用: 机密查询正文"),
], ids=["RegistryError", "OperationalError", "embedding"])
def test_cmd_search_cross_logs_failed_line_on_any_exception(cross_search, make_exc):
    exc = make_exc()
    cross_search["raise"] = exc
    with pytest.raises(type(exc)) as got:
        cli.cmd_search(_cross_args())
    assert got.value is exc, "原异常原样抛出，不改成别的"
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and line["metadata"]["outcome"] == "failed"
    assert type(exc).__name__ in line["metadata"]["note"]
    assert line["metadata"]["paths"] == []
    assert line["metadata"]["task_id"] == "task-77"
    raw = (settings.STATE_DIR / scope.CROSS_LOG_NAME).read_text(encoding="utf-8")
    assert "机密查询正文" not in raw, "留痕只记异常类名，不记异常全文"


@pytest.mark.parametrize("make_exc", [
    lambda: UnicodeEncodeError("ascii", "跨域", 0, 1, "ordinal not in range(128)"),
    lambda: BrokenPipeError("管道已关闭"),
], ids=["UnicodeEncodeError", "BrokenPipeError"])
def test_cmd_search_cross_logs_failed_line_when_banner_output_fails(
        cross_search, monkeypatch, make_exc):
    """横幅是 eff/trace 解析出来之后的第一处输出：它打印失败（编码不支持、管道已断）
    也不能让这次合法的 cross 没有流水。"""
    exc = make_exc()

    def _boom(_eff):
        raise exc

    monkeypatch.setattr(cli, "print_scope_banner", _boom)
    with pytest.raises(type(exc)) as got:
        cli.cmd_search(_cross_args())
    assert got.value is exc, "原异常原样抛出，不改成别的"
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and line["metadata"]["outcome"] == "failed"
    assert type(exc).__name__ in line["metadata"]["note"]
    assert line["metadata"]["paths"] == []


def test_cmd_search_cross_recompile_error_after_candidates_is_logged(
        cross_search, monkeypatch):
    """走真实的 retrieve_candidates：候选查询之后配置变坏，复编译抛 RegistryError。"""
    from scoped_kb import indexer

    class _Cur:
        def __init__(self, h):
            self.h = h

        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (self.h,)

        def fetchall(self):
            return []

    class _Conn:
        def __init__(self, h):
            self.h = h

        def cursor(self):
            return _Cur(self.h)

        def commit(self):
            pass

        def rollback(self):
            pass

    real_compile = registry.compile_registry
    state = {"n": 0}

    def _compile(*a, **k):
        state["n"] += 1
        if state["n"] > 1:                      # 第一次给 _resolve_for_cli，之后都是复编译
            raise registry.RegistryError("配置在查询过程中变得非法")
        return real_compile(*a, **k)

    monkeypatch.setattr(cli, "connect", lambda: _Conn(real_compile().registry_hash))
    monkeypatch.setattr(cli, "retrieve_candidates", search.retrieve_candidates)
    monkeypatch.setattr(search, "embed", lambda texts, **k: [[0.0] * 3 for _ in texts])
    monkeypatch.setattr(indexer, "read_active_hash",
                        lambda conn, schema="public": conn.h)
    monkeypatch.setattr(registry, "compile_registry", _compile)
    with pytest.raises(registry.RegistryError):
        cli.cmd_search(_cross_args())
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and "RegistryError" in line["metadata"]["note"]


def test_cmd_search_same_domain_exception_writes_no_cross_log(cross_search):
    cross_search["raise"] = _EmbedBoom("x")
    with pytest.raises(_EmbedBoom):
        cli.cmd_search(argparse.Namespace(query="同域", limit=5, profile=None,
                                          include=None, why=None))
    assert not (settings.STATE_DIR / scope.CROSS_LOG_NAME).exists()


def test_cmd_search_unexpected_error_and_log_failure_refuses_and_shows_both(
        cross_search, blocked_state, capsys):
    """意外异常、同时留痕也写不进去：仍以留痕失败的 ScopeError 终止，原始错误也要看得到。"""
    cross_search["raise"] = _EmbedBoom("embedding 服务不可用")
    with pytest.raises(scope.ScopeError) as exc:
        cli.cmd_search(_cross_args())
    assert "跨域留痕写入失败" in str(exc.value)
    err = capsys.readouterr().err
    assert "_EmbedBoom" in err and "embedding 服务不可用" in err


# ---- 出口矩阵：合法 cross 尝试的每一个出口都留下流水 -----------------------------

def _exit_ok(cs, mp, gate):
    pass


def _exit_ok_no_hit(cs, mp, gate):
    cs["rows"] = []


def _exit_scope_rejection(cs, mp, gate):
    cs["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。")


def _exit_maintenance(cs, mp, gate):
    gate("迁移中")


def _exit_unexpected_exception(cs, mp, gate):
    cs["raise"] = _EmbedBoom("x")


def _exit_interrupt(cs, mp, gate):
    cs["raise"] = KeyboardInterrupt()


def _exit_banner_fails(cs, mp, gate):
    mp.setattr(cli, "print_scope_banner",
               lambda _e: (_ for _ in ()).throw(BrokenPipeError("x")))


def _exit_unresolvable_location(cs, mp, gate):
    cs["rows"] = [("no_such_owner/x.md", "正文", "a-decisions", 0.5)]


def _exit_render_fails(cs, mp, gate):
    def _boom(*a, **k):
        raise ValueError("出处渲染失败")
    mp.setattr(cli, "provenance_line", _boom)


def _exit_stderr_broken_on_rejection(cs, mp, gate):
    cs["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。")
    mp.setattr(sys, "stderr", _BoomStream(BrokenPipeError("x")))


def _exit_stdout_broken(cs, mp, gate):
    mp.setattr(sys, "stdout", _BoomStream(BrokenPipeError("x"), only="判例正文"))


def _exit_connect_fails(cs, mp, gate):
    def _boom():
        raise ConnectionError("数据库不可达")
    mp.setattr(cli, "connect", _boom)


def _interrupt_around_ok_log(after_write):
    def _setup(cs, mp, gate):
        real = cli._log_cross

        def _log(query, eff, trace, paths, module, outcome="ok", note=""):
            if outcome == "ok" and not after_write:
                raise KeyboardInterrupt()
            real(query, eff, trace, paths, module, outcome=outcome, note=note)
            if outcome == "ok" and after_write:
                raise KeyboardInterrupt()
        mp.setattr(cli, "_log_cross", _log)
    _setup.__name__ = "_exit_interrupt_" + ("after" if after_write else "before") + "_ok_log"
    return _setup


# 第四列是出口形态：None = 正常返回；整数 = SystemExit 的退出码；异常类 = 原样抛出该异常。
@pytest.mark.parametrize("setup,json_mode,outcomes,exit_form", [
    (_exit_ok, False, ["ok"], None),
    (_exit_ok, True, ["ok"], None),
    (_exit_ok_no_hit, False, ["ok"], None),
    (_exit_scope_rejection, False, ["failed"], scope.SCOPE_ERROR_EXIT),
    (_exit_maintenance, False, ["failed"], scope.SCOPE_ERROR_EXIT),
    (_exit_connect_fails, False, ["failed"], ConnectionError),
    (_exit_unexpected_exception, False, ["failed"], _EmbedBoom),
    (_exit_interrupt, False, ["failed"], KeyboardInterrupt),
    (_exit_banner_fails, False, ["failed"], BrokenPipeError),
    (_exit_unresolvable_location, False, ["failed"], scope.SCOPE_ERROR_EXIT),
    (_exit_render_fails, False, ["failed"], ValueError),  # 渲染在 ok 之前：没有结果就不记 ok
    (_interrupt_around_ok_log(False), False, ["failed"], KeyboardInterrupt),
    (_interrupt_around_ok_log(True), False, ["ok", "failed"], KeyboardInterrupt),
    (_exit_stderr_broken_on_rejection, False, ["failed"], BrokenPipeError),
    (_exit_stdout_broken, False, ["ok", "failed"], BrokenPipeError),
    (_exit_stdout_broken, True, ["ok", "failed"], BrokenPipeError),
], ids=lambda v: v.__name__.replace("_exit_", "") if callable(v) else str(v))
def test_cmd_search_cross_every_exit_leaves_a_log(
        cross_search, gate, monkeypatch, setup, json_mode, outcomes, exit_form):
    out, err = sys.stdout, sys.stderr
    setup(cross_search, monkeypatch, gate)
    raised = None
    try:
        cli.cmd_search(_cross_args(json=json_mode))
    except BaseException as exc:      # noqa: BLE001 — 出口形态本身就是被测对象
        raised = exc
    finally:
        sys.stdout, sys.stderr = out, err     # 被打桩的流还原后再读流水
    assert [line["outcome"] for line in _cross_log_lines()] == outcomes
    if exit_form is None:
        assert raised is None, f"应正常返回，实际抛出 {raised!r}"
    elif isinstance(exit_form, int):
        assert isinstance(raised, SystemExit) and raised.code == exit_form, raised
    else:
        assert type(raised) is exit_form, f"应原样抛出 {exit_form.__name__}，实际 {raised!r}"


def test_cross_ok_log_failure_is_not_followed_by_a_second_failed_attempt(
        cross_search, monkeypatch, capsys):
    """ok 行自己写不进去：不再补写 failed（同一个错），ScopeError 原样交给 main 终止，
    结果不交付，stderr 上也没有「原始错误」的重复回显。"""
    real = cli._log_cross
    calls = []

    def _flaky(query, eff, trace, paths, module, outcome="ok", note=""):
        calls.append(outcome)
        if outcome == "ok":
            raise scope.ScopeError("跨域留痕写入失败，拒绝交付本次结果: x")
        return real(query, eff, trace, paths, module, outcome=outcome, note=note)

    monkeypatch.setattr(cli, "_log_cross", _flaky)
    with pytest.raises(scope.ScopeError):
        cli.cmd_search(_cross_args())
    assert calls == ["ok"]
    assert _cross_log_lines() == []
    captured = capsys.readouterr()
    assert "原始错误" not in captured.err and "判例正文" not in captured.out


# ---- cmd_pack：真实跑各出口 ---------------------------------------------------

# 运行时拼出来的假凭据：源码里不出现完整形态（仓库公开，会被扫描）。
_FAKE_PW = "Q7x" + "!kq2" + "Lm9#p"
SECRET_ROWS = [("repo_a/docs/decisions/a.md", "判例正文\nDB_PASS" + "WORD=" + _FAKE_PW,
                "a-decisions", 0.91)] + ROWS[1:]


def _pack_args(task="跨域打包", **kw):
    base = {"task": task, "limit": 5, "budget": 8000, "out": None, "force": False,
            "profile": "cross", "include": ["research"], "why": "核对研究侧判例"}
    base.update(kw)
    return argparse.Namespace(**base)


def test_cmd_pack_cross_ok_delivers_package_and_logs(cross_search, fixture_base, capsys):
    cli.cmd_pack(_pack_args())
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok" and line["module"] == "scoped-kb-pack"
    assert line["metadata"]["paths"] == sorted(r[0] for r in ROWS)
    out, err = capsys.readouterr()
    assert out.startswith("# 任务上下文包") and "判例正文" in out and "笔记正文" in out
    assert out.count(PROVENANCE_PREFIX) == len(ROWS)
    assert "[profile]" not in out and "[profile]" in err, "stdout 只放这一包，横幅走 stderr"
    assert str(fixture_base) not in out, "包里只出位置键，不出本机绝对路径"


def test_cmd_pack_no_hit_returns_normally_and_logs(cross_search, capsys):
    cross_search["rows"] = []
    cli.cmd_pack(_pack_args())
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok" and "无命中" in line["summary"]
    out, err = capsys.readouterr()
    assert out == "" and "没有检索到" in err


def test_cmd_pack_secret_guard_aborts_without_echoing(cross_search, tmp_path, capsys):
    cross_search["rows"] = SECRET_ROWS
    target = tmp_path / "ctx.md"
    with pytest.raises(SystemExit) as exc:
        cli.cmd_pack(_pack_args(out=str(target)))
    assert exc.value.code == SECRET_GUARD_EXIT
    assert not target.exists(), "中止时不得写出任何文件"
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and "凭据守卫中止" in line["summary"]
    assert line["metadata"]["paths"] == sorted(r[0] for r in SECRET_ROWS), \
        "内容已经读出来了，留痕要带位置键"
    out, err = capsys.readouterr()
    assert out == ""
    assert _FAKE_PW not in err and _FAKE_PW[:5] not in err, "报告不得回显凭据"
    assert "repo_a/docs/decisions/a.md" in err, "报告要指出是哪个片段"


def test_cmd_pack_force_delivers_and_records_it(cross_search, capsys):
    cross_search["rows"] = SECRET_ROWS
    cli.cmd_pack(_pack_args(force=True))
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok" and "--force 跳过 1 处" in line["summary"]
    out, err = capsys.readouterr()
    assert _FAKE_PW in out and "--force 已跳过" in err


def test_cmd_pack_scope_rejection_writes_failed_line(cross_search, capsys):
    cross_search["raise"] = scope.ScopeError("registry 世代不一致，拒绝检索。\n详情")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_pack(_pack_args())
    assert exc.value.code == scope.SCOPE_ERROR_EXIT
    (line,) = _cross_log_lines()
    assert line["outcome"] == "failed" and "scope 拒绝" in line["summary"]


def test_cmd_pack_out_writes_file_not_stdout(cross_search, tmp_path, capsys):
    target = tmp_path / "ctx.md"
    cli.cmd_pack(_pack_args(out=str(target)))
    assert target.read_text(encoding="utf-8").startswith("# 任务上下文包")
    out, err = capsys.readouterr()
    assert out == "" and "已写入" in err


def test_cmd_pack_delivery_failure_logs_failed_after_ok(cross_search, tmp_path):
    with pytest.raises(OSError):
        cli.cmd_pack(_pack_args(out=str(tmp_path / "no-such-dir" / "ctx.md")))
    ok, failed = _cross_log_lines()
    assert ok["outcome"] == "ok" and failed["outcome"] == "failed"
    assert "交付失败" in failed["summary"]


def test_cmd_pack_same_domain_writes_no_cross_log(cross_search, capsys):
    cli.cmd_pack(_pack_args(profile=None, include=None, why=None))
    assert not (settings.STATE_DIR / scope.CROSS_LOG_NAME).exists()
    assert capsys.readouterr().out.startswith("# 任务上下文包")


def test_pack_parser_exposes_expected_flags():
    sub = cli.build_parser()._subparsers._group_actions[0].choices["pack"]
    flags = {a.dest for a in sub._actions}
    assert {"task", "profile", "include", "why", "limit", "budget", "out", "force"} <= flags


def test_cmd_pack_forged_source_heading_in_body_does_not_leak(cross_search, capsys):
    """正文里写一行假的「### 来源」标题，报告也不能把那一行（连同里面的凭据）打出来。"""
    forged = "### 来源 9：`password=" + _FAKE_PW + "`"
    cross_search["rows"] = [("repo_a/docs/decisions/a.md", "判例正文\n" + forged,
                             "a-decisions", 0.91)]
    with pytest.raises(SystemExit):
        cli.cmd_pack(_pack_args())
    err = capsys.readouterr().err
    assert _FAKE_PW[:5] not in err
    assert "片段 1：repo_a/docs/decisions/a.md" in err


def test_render_pack_spans_point_at_fragment_headings():
    rows = [("r/a.md", "一\n二\n三", "c1", 0.9), ("r/b.md", "四", "c1", 0.8),
            ("r/c.md", "五\n六", "c1", 0.7)]
    pkg, spans = cli.render_pack("任务\n第二行", rows, 7, {"r/a.md", "r/b.md", "r/c.md"},
                                 with_spans=True)
    lines = pkg.splitlines()
    for (first, last, i, key), (fp, content, _c, _s) in zip(spans, rows):
        assert lines[first - 1].startswith(f"### 来源 {i}：`{key}`") and key == fp
        assert lines[last - 1] == content.splitlines()[-1]
    assert pkg == cli.render_pack("任务\n第二行", rows, 7, {"r/a.md", "r/b.md", "r/c.md"})


def test_cmd_pack_notice_failure_after_delivery_keeps_single_ok(cross_search, monkeypatch, capsys):
    """包已经交付后，stderr 上的提示写失败不算交付失败，不补 failed 行。"""
    cross_search["rows"] = SECRET_ROWS

    class _BoomErr:
        def write(self, text):
            if "--force 已跳过" in text:
                raise BrokenPipeError
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stderr", _BoomErr())
    with pytest.raises(BrokenPipeError):
        cli.cmd_pack(_pack_args(force=True))
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok"


def test_pack_report_hides_location_key_that_looks_like_a_secret():
    """位置键来自文件名；文件名里写着密钥时，报告只说「已隐去」。"""
    key = "repo_a/notes/api_key=" + "Kq7x" + "9mP2Lm4z.md"
    shown = cli._pack_source_of([(10, 20, 2, key)], 15)
    assert "Kq7x9" not in shown and "已隐去" in shown and "片段 2" in shown
    assert cli._pack_source_of([(10, 20, 2, "repo_a/notes/ok.md")], 15) == \
        "（片段 2：repo_a/notes/ok.md）"
    assert "包头" in cli._pack_source_of([(10, 20, 2, key)], 3)


def test_pack_report_hides_location_key_with_secret_shaped_like_attribute():
    """文件名「password=<小写字母数字>.md」会被扫描当成属性引用放过；报告仍不得打出它。"""
    key = "repo/notes/password=" + "kq7x" + "9mp2lm4zr8.md"
    assert "kq7x9" not in cli._pack_source_of([(1, 9, 1, key)], 5)


def test_cmd_pack_notice_failure_after_file_write_keeps_single_ok(cross_search, tmp_path,
                                                                   monkeypatch):
    """文件已写出后，「已写入」提示写失败不算交付失败。"""

    class _BoomErr:
        def write(self, text):
            if "已写入" in text:
                raise BrokenPipeError
            return len(text)

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stderr", _BoomErr())
    target = tmp_path / "ctx.md"
    with pytest.raises(BrokenPipeError):
        cli.cmd_pack(_pack_args(out=str(target)))
    assert target.exists()
    (line,) = _cross_log_lines()
    assert line["outcome"] == "ok"
