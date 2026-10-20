"""世代闸三项：自动换代可达性、显式 activate 强制 rebuild、cross 流水不因维护闸丢失。

三项都属于「不会当场报错」的那类失败，所以用回归测试钉住：

1. **自动换代必须可达。** 普通 index 发现 registry 换代时直接重建，不经显式
   `activate`（它要求维护闸存在 + override，而 `cmd_index` 在闸存在时提前拒绝，
   两个条件互斥）。否则改完配置后检索会全线因世代不符 fail closed，而触发者
   常是定时任务，没人在看那条输出。
2. **显式 activate 必须带 rebuild 才推进世代。** 否则被 deleted/excluded/quarantine
   的旧行原地留在库里，却被新世代标记背书——「已隔离内容照样能被检索到」。
3. **维护闸拒绝一次合法 cross 时也要留痕。** 若异常在 eff/trace 交回调用方之前
   就抛到 main 的兜底 handler，`_log_cross` 一次都跑不到，「范围已经被扩开过」
   这件事就从留痕里消失，而这恰恰是最该留痕的一类。
"""
import argparse
import inspect
import types

import pytest

from scoped_kb import cli, indexer, registry, scope, search


@pytest.fixture
def reg(fixture_paths):
    return registry.compile_registry(fixture_paths)


# ---- 1：自动换代必须可达，且不与显式切换共用权限条件 ------------------

def test_index_with_registry_takes_no_maintenance_override():
    """自动换代不得再接受 `maintenance_override`。

    只要这个参数还在签名里，`index` 入口就仍有一条通往特权切换动作的路，
    两条入口就还共享着同一个可误用的权限条件。
    """
    params = inspect.signature(indexer.index_with_registry).parameters
    assert "maintenance_override" not in params


def test_index_subcommand_exposes_no_privileged_flags():
    """`scoped-kb index` 的参数表里不得再出现 --rebuild / --maintenance-override。

    留着而永远失败比删掉更糟：它看起来是一条可用的重建入口。
    """
    parser = cli.build_parser()
    index_parser = next(sub for action in parser._subparsers._group_actions
                        for name, sub in action.choices.items() if name == "index")
    opts = {o for a in index_parser._actions for o in a.option_strings}
    assert "--rebuild" not in opts
    assert "--maintenance-override" not in opts


def test_drift_rebuild_is_reachable_without_maintenance_gate(reg, gate, monkeypatch):
    """闸不存在（自动触发的常态）时，换代必须真的走到重建。

    这是本项的核心：若自动换代也要求维护闸，这里会抛 ScopeError「维护闸不存在」。
    """
    gate(None)                       # 闸不存在
    calls = []
    monkeypatch.setattr(indexer, "read_active_hash",
                        lambda conn, schema="public": "0" * 64)
    monkeypatch.setattr(indexer, "_rebuild_generation",
                        lambda r, c, schema="public", rebuild=True:
                        calls.append(schema) or
                        {"files": 0, "chunks": 0, "embedded": 0,
                         "rebuilt": rebuild, "registry_hash": r.registry_hash})

    def _no_activate(*a, **k):
        raise AssertionError("自动换代不得再经过 public activate")

    monkeypatch.setattr(indexer, "activate", _no_activate)

    stats = indexer.index_with_registry(reg, conn=None)
    assert calls == ["public"], "换代时必须走内部重建"
    assert stats["rebuilt"] is True
    assert stats["registry_hash"] == reg.registry_hash


def test_no_drift_still_goes_incremental(reg, gate, monkeypatch):
    """世代未变时仍走真增量——自动换代可达，不能以每次 index 都全量重建为代价。"""
    gate(None)
    calls = []
    monkeypatch.setattr(indexer, "read_active_hash",
                        lambda conn, schema="public": reg.registry_hash)
    monkeypatch.setattr(indexer, "_incremental_index",
                        lambda r, c, schema="public": calls.append("inc") or
                        {"rebuilt": False, "registry_hash": r.registry_hash})
    monkeypatch.setattr(indexer, "_rebuild_generation",
                        lambda *a, **k: pytest.fail("世代未变不该重建"))
    indexer.index_with_registry(reg, conn=None)
    assert calls == ["inc"]


def test_cmd_index_still_refuses_while_gate_is_up(gate):
    """闸存在时 index 仍必须拒绝——迁移中途写入会让索引与世代标记不一致。

    换代在**闸不存在**时可达，闸本身照样生效。
    """
    gate("迁移中，勿动")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_index(argparse.Namespace())
    assert "维护" in str(exc.value)


# ---- 2：显式生产 activate 必须强制 rebuild ---------------------------

def test_public_activate_refuses_without_rebuild(reg, gate):
    """闸与 override 都齐了，仍不足以在不 rebuild 的情况下推进生产世代。

    不 rebuild 就换代 = 旧 collection 的行原地留下、被新世代背书。
    `conn=None` 同时证明这一步发生在碰数据库之前。
    """
    gate("演练")
    with pytest.raises(scope.ScopeError) as exc:
        indexer.activate(reg, None, schema="public", rebuild=False,
                     maintenance_override=True)
    msg = str(exc.value)
    assert "rebuild" in msg
    assert "残留" in msg or "旧行" in msg


def test_public_activate_still_needs_gate_and_override(reg, gate):
    """自动换代可达，不等于放松显式 activate：维护闸与 override 仍然缺一不可。"""
    gate(None)
    with pytest.raises(scope.ScopeError) as exc:
        indexer.activate(reg, None, schema="public", rebuild=True,
                     maintenance_override=True)
    assert "维护闸" in str(exc.value)

    gate("演练")
    with pytest.raises(scope.ScopeError) as exc:
        indexer.activate(reg, None, schema="public", rebuild=True,
                     maintenance_override=False)
    assert "override" in str(exc.value)


def test_canary_activate_is_not_forced_to_rebuild(reg, gate, monkeypatch):
    """canary 是隔离测试库，不受生产的强制 rebuild 约束。

    把生产的纪律照搬到 canary 会让增量路径失去测试覆盖。
    """
    gate(None)
    seen = {}
    monkeypatch.setattr(indexer, "_rebuild_generation",
                        lambda r, c, schema="public", rebuild=False:
                        seen.update(schema=schema, rebuild=rebuild) or
                        {"files": 0, "chunks": 0, "embedded": 0,
                         "rebuilt": rebuild, "registry_hash": r.registry_hash})
    indexer.activate(reg, None, schema="canary", rebuild=False)
    assert seen == {"schema": "canary", "rebuild": False}


# ---- 3：维护闸拒绝合法 cross 时仍要留完整流水 -----------------------

def _cross_args(query="跨域测试"):
    return argparse.Namespace(query=query, limit=5,
                              profile="cross", include=["research"],
                              why="核对研究侧判例")


@pytest.fixture
def cross_env(monkeypatch, gate, install_config, trace_vars, fixture_base):
    """站在 repo_a 里发起一次**合法**的 cross，并把闸拉起来。

    可追溯标识的变量名来自 config.json 的 trace 段（不是缺省名），
    cwd 用夹具里的仓库根，配置装成真实的配置目录。
    """
    install_config(config=trace_vars.config)
    monkeypatch.chdir(fixture_base / "repo_a")
    monkeypatch.setenv(trace_vars.task, "trace-task")
    monkeypatch.setenv(trace_vars.sessions[0], "sess-abc123")
    gate("迁移中，勿动")


def test_gate_rejected_cross_still_writes_trail(cross_env, monkeypatch, capsys):
    """维护闸拒绝一次合法 cross 时，流水必须照留。

    若 ScopeError 在 `_resolve_for_cli` 内抛出，eff/trace 还没交回调用方，
    `_log_cross` 一次都跑不到——扩权读取的意图就从留痕里消失了。
    """
    logged = []
    monkeypatch.setattr(cli, "_log_cross",
                        lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(cli, "connect",
                        lambda: pytest.fail("维护闸必须挡在建连接之前"))

    with pytest.raises(SystemExit) as exc:
        cli.cmd_search(_cross_args())
    assert exc.value.code == scope.SCOPE_ERROR_EXIT

    assert logged, "被维护闸拒绝的 cross 没有留下任何流水"
    (_query, eff, trace, paths, module), kwargs = logged[0]
    assert module == "search"
    assert kwargs["outcome"] == "failed"
    assert eff.effective == "cross"
    assert eff.included == ("research",)
    assert eff.why == "核对研究侧判例"
    assert trace == ("trace-task", "sess-abc123"), "task/session ID 必须已解析"
    assert paths == []


def test_gate_rejected_cross_pack_also_writes_trail(cross_env, monkeypatch):
    """pack 与 search 走同一条纪律，不能只修一边。"""
    logged = []
    monkeypatch.setattr(cli, "_log_cross",
                        lambda *a, **k: logged.append((a, k)))
    monkeypatch.setattr(cli, "connect",
                        lambda: pytest.fail("维护闸必须挡在建连接之前"))
    args = argparse.Namespace(task="跨域打包", limit=5, budget=8000, out=None,
                              force=False, profile="cross",
                              include=["research"], why="核对研究侧判例")
    with pytest.raises(SystemExit):
        cli.cmd_pack(args)
    assert logged and logged[0][1]["outcome"] == "failed"
    assert logged[0][0][4] == "pack"


def test_gate_rejection_still_prints_banner(cross_env, monkeypatch, capsys):
    """维护期的拒绝仍要带正确的 profile 回显，便于排查是谁被挡。"""
    monkeypatch.setattr(cli, "_log_cross", lambda *a, **k: None)
    monkeypatch.setattr(cli, "connect", lambda: pytest.fail("不该建连接"))
    with pytest.raises(SystemExit):
        cli.cmd_search(_cross_args())
    out = capsys.readouterr().out
    assert "base=work" in out and "effective=cross" in out


def test_same_domain_gate_rejection_needs_no_trail(monkeypatch, gate, capsys,
                                                   install_config, fixture_base):
    """同域检索没有扩权动作，被闸拒绝时不写 cross 流水（写了才是噪声）。"""
    install_config()
    monkeypatch.chdir(fixture_base / "repo_a")
    gate("迁移中")
    monkeypatch.setattr(cli, "_log_cross",
                        lambda *a, **k: pytest.fail("同域不该写 cross 流水"))
    monkeypatch.setattr(cli, "connect", lambda: pytest.fail("不该建连接"))
    args = argparse.Namespace(query="同域", limit=5,
                              profile=None, include=None, why=None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_search(args)
    assert exc.value.code == scope.SCOPE_ERROR_EXIT


# ---- 4：世代门的报错要把人指向 `scoped-kb index` ---------------------------

def _fixture_eff(reg):
    return scope.Effective("work", "work", (), "",
                           tuple(sorted(reg.allowed_for("work"))), reg.registry_hash)


def test_empty_meta_error_points_to_index(reg, monkeypatch):
    """索引世代还没提交：正确的指引是运行 index（首次建库会全量构建），
    不是 `registry --activate`——后者是带维护闸的特权动作，普通用户不该被指过去。"""
    monkeypatch.setattr(indexer, "read_active_hash", lambda conn, schema="public": None)
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(None, "q", _fixture_eff(reg), 5)
    msg = str(exc.value)
    assert "knowledge_index_meta 为空" in msg
    assert "scoped-kb index" in msg
    assert "--activate" not in msg


def test_generation_mismatch_error_points_to_index(reg, monkeypatch):
    """改过配置后世代对不上：运行 index 即可，检测到配置变化会自动全量重建。"""
    monkeypatch.setattr(indexer, "read_active_hash", lambda conn, schema="public": "0" * 64)
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(None, "q", _fixture_eff(reg), 5)
    msg = str(exc.value)
    assert "registry 世代不一致" in msg
    assert "scoped-kb index" in msg and "自动全量重建" in msg
    assert "--activate" not in msg


def test_config_changed_during_query_error_points_to_index(reg, monkeypatch):
    """查询期间本地配置变了：同样指向 index，而不是 registry 激活。"""
    eff = _fixture_eff(reg)

    class _Cur:
        def execute(self, *a, **k):
            pass

        def fetchone(self):
            return (eff.registry_hash,)

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

        def rollback(self):
            pass

    monkeypatch.setattr(indexer, "read_active_hash",
                        lambda conn, schema="public": eff.registry_hash)
    monkeypatch.setattr(search, "embed", lambda texts: [[0.0] for _ in texts])
    monkeypatch.setattr(search.registry, "compile_registry",
                        lambda paths=None: types.SimpleNamespace(registry_hash="f" * 64))
    with pytest.raises(scope.ScopeError) as exc:
        search.retrieve_candidates(_Conn(), "q", eff, 5)
    msg = str(exc.value)
    assert "本地配置在查询过程中发生变化" in msg
    assert "scoped-kb index" in msg
    assert "--activate" not in msg
