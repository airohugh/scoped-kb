"""`scoped-kb init`：给单域用户生成配置，不必手写三份 JSON 与指纹。

契约：
  · 写出的三份配置能被生产的 compile_registry 编译，指纹是真算出来的；
  · 目标文件任一已存在且没给 --force → 拒绝，一个字节都不写；
  · 写完立即编译自检，失败就还原，不留半套配置；
  · 端到端：init → index → 从无关目录 search，拿到带出处的、可打开的绝对路径。

embedding 一律打桩（fake_embed），不连任何服务。
"""
import argparse
import json
from pathlib import Path

import conftest
import pytest

from scoped_kb import cli, indexer, provenance, registry, scope, search, settings

FILES = ("machine-sources.json", "scope-policy.json", "config.json")


def _notes_dir(tmp_path, name="my-notes"):
    d = tmp_path / name
    d.mkdir()
    return d


def _run_init(notes, *extra):
    args = cli.build_parser().parse_args(["init", str(notes), *extra])
    args.fn(args)


def _written(cfg_dir):
    return {name: (cfg_dir / name).read_bytes() for name in FILES
            if (cfg_dir / name).exists()}


def _sentinels(cfg_dir, names=FILES):
    """预先放进配置目录的占位文件：内容与任何合法配置都不同，便于确认逐字节未变。"""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in names:
        data = f"# 占位 {name}\n".encode()
        (cfg_dir / name).write_bytes(data)
        out[name] = data
    return out


# ---- ① 写出的配置能编译，字段正确 ----------------------------------------

def test_init_writes_single_domain_config_that_compiles(tmp_path, capsys):
    notes = _notes_dir(tmp_path)
    _run_init(notes)

    reg = registry.compile_registry()
    assert reg.default_profile == "default"
    assert reg.profiles == {"default": ("notes",)}
    assert reg.cross_allowlist == frozenset()
    assert reg.repo_roots == ((notes.resolve(), "default"),)
    assert reg.collections["notes"].roots == (notes.resolve(),)
    assert reg.collections["notes"].index_policy == "active"

    pol = json.loads((settings.CONFIG_DIR / "scope-policy.json").read_text(encoding="utf-8"))
    assert pol["cross_allowlist"] == []              # 显式写出空数组，不是省略
    assert pol["default_profile"] == "default"
    # 指纹由输入独立算出（不经 print_fingerprints），与写进策略的值、编译结果三方一致
    expected = registry._fingerprint("machine", "notes", (notes.resolve(),), ())
    assert pol["collections"]["notes"]["fingerprint"] == expected
    assert reg.collections["notes"].fingerprint == expected
    assert registry.print_fingerprints() == {"notes": expected}

    ms = json.loads((settings.CONFIG_DIR / "machine-sources.json").read_text(encoding="utf-8"))
    assert [c["collection_id"] for c in ms["collections"]] == ["notes"]

    cfg = json.loads((settings.CONFIG_DIR / "config.json").read_text(encoding="utf-8"))
    assert cfg == {
        "schema_version": 1, "repos": [], "skip_filenames": [],
        "provenance_fields": list(registry.DEFAULT_PROVENANCE_FIELDS),
        "trace": {"task_id_env": registry.DEFAULT_TRACE_TASK_ENV,
                  "session_id_env": list(registry.DEFAULT_TRACE_SESSION_ENVS)}}

    out = capsys.readouterr().out
    assert str(settings.CONFIG_DIR) in out
    for name in FILES:
        assert name in out
    assert "scoped-kb index" in out and "scoped-kb search" in out


def test_init_stores_the_resolved_absolute_path(tmp_path, monkeypatch):
    """相对路径、含 .. 的写法都落成同一个已 resolve 的绝对路径。"""
    notes = _notes_dir(tmp_path)
    monkeypatch.chdir(tmp_path)
    _run_init("my-notes/../my-notes")

    ms = json.loads((settings.CONFIG_DIR / "machine-sources.json").read_text(encoding="utf-8"))
    assert ms["collections"][0]["root"] == str(notes.resolve())
    pol = json.loads((settings.CONFIG_DIR / "scope-policy.json").read_text(encoding="utf-8"))
    assert list(pol["repo_roots"]) == [str(notes.resolve())]


# ---- ② 已存在且没给 --force：拒绝，一个字节都不写 -----------------------

@pytest.mark.parametrize("existing", FILES)
def test_init_refuses_when_any_target_exists(tmp_path, existing):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR, names=(existing,))

    with pytest.raises(SystemExit) as exc:
        _run_init(notes)
    assert exc.value.code not in (0, None)
    assert "--force" in str(exc.value.code)

    assert _written(settings.CONFIG_DIR) == before, "已存在的文件必须逐字节不变，其余文件不得被创建"


def test_init_refuses_when_all_targets_exist(tmp_path):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    with pytest.raises(SystemExit):
        _run_init(notes)
    assert _written(settings.CONFIG_DIR) == before


# ---- ②b 目标是符号链接（含悬空）：不论有没有 --force 都拒绝，一个字节都不写 ----
#
# 写入会跟随链接创建/改写链接指向的文件，回滚却只能按路径删除/改写——对悬空链接
# 无法还原。与其声称还原，不如不碰：init 不处理符号链接，请用户手工处理。

@pytest.mark.parametrize("force", [[], ["--force"]], ids=["no-force", "force"])
@pytest.mark.parametrize("dangling", [True, False], ids=["dangling", "to-existing"])
@pytest.mark.parametrize("link_name", FILES)
def test_init_refuses_symlinked_target_and_touches_nothing(
        tmp_path, link_name, dangling, force):
    notes = _notes_dir(tmp_path)
    cfg = settings.CONFIG_DIR
    cfg.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere" / "real.json"
    target.parent.mkdir()
    if not dangling:
        target.write_bytes("# 链接指向的已有文件\n".encode())
    (cfg / link_name).symlink_to(target)
    others = _sentinels(cfg, names=tuple(n for n in FILES if n != link_name))

    with pytest.raises(SystemExit) as exc:
        _run_init(notes, *force)
    msg = str(exc.value.code)
    assert "符号链接" in msg and link_name in msg
    assert "手工处理" in msg

    link = cfg / link_name
    assert link.is_symlink() and Path(link).readlink() == target, "链接本身不得被改动"
    if dangling:
        assert not target.exists(), "悬空链接的目标不得被创建"
    else:
        assert target.read_bytes() == "# 链接指向的已有文件\n".encode(), "链接指向的文件不得被改写"
    assert {n: (cfg / n).read_bytes() for n in others} == others, "其余文件逐字节不变"
    assert not list(cfg.glob("*.bak-init*"))


def test_init_refuses_when_several_targets_are_symlinks_and_names_them_all(tmp_path):
    notes = _notes_dir(tmp_path)
    cfg = settings.CONFIG_DIR
    cfg.mkdir(parents=True, exist_ok=True)
    for name in ("machine-sources.json", "config.json"):
        (cfg / name).symlink_to(tmp_path / f"gone-{name}")
    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    msg = str(exc.value.code)
    assert "machine-sources.json" in msg and "config.json" in msg
    assert not (cfg / "scope-policy.json").exists()


# ---- ③ --force 覆盖 --------------------------------------------------------

def test_force_overwrites_existing_config(tmp_path):
    first = _notes_dir(tmp_path, "first")
    second = _notes_dir(tmp_path, "second")
    _run_init(first)
    assert registry.compile_registry().collections["notes"].roots == (first.resolve(),)

    _sentinels(settings.CONFIG_DIR, names=("config.json",))   # 再放一个坏文件进去
    _run_init(second, "--force")

    reg = registry.compile_registry()
    assert reg.collections["notes"].roots == (second.resolve(),)
    assert reg.repo_roots == ((second.resolve(), "default"),)


# ---- ④ 笔记目录不合法 ------------------------------------------------------

def test_init_rejects_missing_notes_dir(tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run_init(tmp_path / "does-not-exist")
    assert exc.value.code not in (0, None)
    assert not settings.CONFIG_DIR.exists(), "笔记目录不合法时不得创建配置目录"


def test_init_rejects_a_file_as_notes_dir(tmp_path):
    f = tmp_path / "a-file.md"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _run_init(f)
    assert exc.value.code not in (0, None)
    assert not settings.CONFIG_DIR.exists()


# ---- ⑤ 编译失败：还原，不留半套配置 -----------------------------------------

def _fail_compile(monkeypatch):
    def boom(*_a, **_k):
        raise registry.RegistryError("编译自检失败（测试注入）")
    monkeypatch.setattr(registry, "compile_registry", boom)


def test_failed_self_check_leaves_no_files_on_fresh_install(tmp_path, monkeypatch):
    notes = _notes_dir(tmp_path)
    _fail_compile(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        _run_init(notes)
    assert "编译自检失败" in str(exc.value.code)
    assert _written(settings.CONFIG_DIR) == {}


def test_failed_self_check_restores_the_previous_config_under_force(tmp_path, monkeypatch):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR, names=("config.json", "scope-policy.json"))
    _fail_compile(monkeypatch)

    with pytest.raises(SystemExit):
        _run_init(notes, "--force")
    # 原来有的逐字节还原；原来没有的（machine-sources.json）不得残留
    assert _written(settings.CONFIG_DIR) == before


# ---- ⑤a 被中断（Ctrl-C 等）：同样还原，再把中断原样抛出 ----------------------

def _interrupt_on_second_file(monkeypatch, exc):
    """写第二份文件（config.json）写到一半时注入 exc。它不是 Exception 的子类。"""
    real_text = Path.write_text

    def write_text(self, data, *a, **k):
        if self.name == "config.json":
            real_text(self, "{半截", *a, **k)
            raise exc
        return real_text(self, data, *a, **k)

    monkeypatch.setattr(Path, "write_text", write_text)


@pytest.mark.parametrize("make_exc", [KeyboardInterrupt, lambda: SystemExit(130)],
                         ids=["KeyboardInterrupt", "SystemExit"])
def test_interrupt_leaves_no_files_on_fresh_install(tmp_path, monkeypatch, make_exc):
    notes = _notes_dir(tmp_path)
    exc = make_exc()
    _interrupt_on_second_file(monkeypatch, exc)

    with pytest.raises(type(exc)) as got:
        _run_init(notes)
    assert got.value is exc, "中断原样向上传播，不改成别的"
    assert _written(settings.CONFIG_DIR) == {}


@pytest.mark.parametrize("make_exc", [KeyboardInterrupt, lambda: SystemExit(130)],
                         ids=["KeyboardInterrupt", "SystemExit"])
def test_interrupt_restores_the_previous_config_under_force(tmp_path, monkeypatch, make_exc):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR, names=("config.json", "scope-policy.json"))
    exc = make_exc()
    _interrupt_on_second_file(monkeypatch, exc)

    with pytest.raises(type(exc)) as got:
        _run_init(notes, "--force")
    assert got.value is exc
    # 原来有的逐字节还原；原来没有的（machine-sources.json）不得残留
    assert _written(settings.CONFIG_DIR) == before


def test_interrupt_with_unrestorable_file_still_reports_where_the_backup_is(
        tmp_path, monkeypatch, capsys):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    real_text, real_bytes = Path.write_text, Path.write_bytes

    def write_text(self, data, *a, **k):
        if self.name == "config.json":
            raise KeyboardInterrupt
        return real_text(self, data, *a, **k)

    def write_bytes(self, data):
        if self.name == "machine-sources.json":
            raise OSError(13, "Permission denied")
        return real_bytes(self, data)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)

    with pytest.raises(KeyboardInterrupt):
        _run_init(notes, "--force")
    err = capsys.readouterr().err
    bak = settings.CONFIG_DIR / "machine-sources.json.bak-init"
    assert bak.read_bytes() == before["machine-sources.json"]
    assert "还原失败" in err and str(bak) in err
    for name in ("config.json", "scope-policy.json"):
        assert (settings.CONFIG_DIR / name).read_bytes() == before[name], name


# ---- ⑤b 还原本身也失败：逐个还原，备份并列出 -------------------------------

def _break_writes(monkeypatch, *, init_fails, rollback_fails):
    """让指定文件的 init 写入（write_text，写一半就失败）与回滚（write_bytes）失败。

    只改这两个入口，且只对点名的文件名生效；备份文件名不同，不受影响。
    """
    real_text, real_bytes = Path.write_text, Path.write_bytes

    def write_text(self, data, *a, **k):
        if self.name in init_fails:
            real_text(self, "{半截", *a, **k)          # 写了一半才失败：文件已被破坏
            raise OSError(28, "No space left on device")
        return real_text(self, data, *a, **k)

    def write_bytes(self, data):
        if self.name in rollback_fails:
            raise OSError(13, "Permission denied")
        return real_bytes(self, data)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)


def test_rollback_failure_on_one_file_does_not_stop_restoring_the_others(tmp_path, monkeypatch):
    """第三份写到一半失败；回滚时排在最前面的 machine-sources.json 又还原不了。
    其余两份必须仍被逐字节还原，不能因为循环提前中止而留着新内容。"""
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    _break_writes(monkeypatch, init_fails={"scope-policy.json"},
                  rollback_fails={"machine-sources.json"})

    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    assert exc.value.code not in (0, None)
    msg = str(exc.value.code)

    for name in ("scope-policy.json", "config.json"):
        assert (settings.CONFIG_DIR / name).read_bytes() == before[name], name
    # 没能还原的那份：原内容备份在旁边，报错里点名并给出备份位置
    bak = settings.CONFIG_DIR / "machine-sources.json.bak-init"
    assert bak.read_bytes() == before["machine-sources.json"]
    assert f"{settings.CONFIG_DIR / 'machine-sources.json'}：还原失败" in msg
    assert str(bak) in msg
    for name in ("scope-policy.json", "config.json"):
        assert str(settings.CONFIG_DIR / name) not in msg, "已还原的不该被报成没还原"


def test_every_unrestorable_file_is_listed_with_its_own_backup(tmp_path, monkeypatch):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    _break_writes(monkeypatch, init_fails={"scope-policy.json"},
                  rollback_fails={"machine-sources.json", "config.json"})

    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    msg = str(exc.value.code)
    for name in ("machine-sources.json", "config.json"):
        bak = settings.CONFIG_DIR / f"{name}.bak-init"
        assert bak.read_bytes() == before[name]
        assert f"{settings.CONFIG_DIR / name}：还原失败" in msg
        assert str(bak) in msg
    assert (settings.CONFIG_DIR / "scope-policy.json").read_bytes() == \
        before["scope-policy.json"]


def test_unrestorable_new_file_is_reported_without_a_backup(tmp_path, monkeypatch):
    """原本不存在的文件还原失败：没有原内容可备份，报错里要点名并提示手工删除。"""
    notes = _notes_dir(tmp_path)
    _break_writes(monkeypatch, init_fails={"scope-policy.json"}, rollback_fails=set())
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "machine-sources.json":
            raise OSError(13, "Permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(SystemExit) as exc:
        _run_init(notes)
    msg = str(exc.value.code)
    assert str(settings.CONFIG_DIR / "machine-sources.json") in msg and "手工删除" in msg
    assert not list(settings.CONFIG_DIR.glob("*.bak-init*"))
    assert not (settings.CONFIG_DIR / "config.json").exists(), "能删的照样删掉"


def test_existing_backup_is_not_overwritten(tmp_path, monkeypatch):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    older = settings.CONFIG_DIR / "machine-sources.json.bak-init"
    older.write_bytes(b"older original")
    _break_writes(monkeypatch, init_fails={"scope-policy.json"},
                  rollback_fails={"machine-sources.json"})

    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    assert older.read_bytes() == b"older original"
    second = settings.CONFIG_DIR / "machine-sources.json.bak-init.1"
    assert second.read_bytes() == before["machine-sources.json"]
    assert str(second) in str(exc.value.code)


def test_backup_falls_back_to_temp_dir_when_config_dir_is_unwritable(tmp_path, monkeypatch):
    """配置目录旁写不了备份（如磁盘写满）：退到系统临时目录，报错里给出那个位置。"""
    import builtins
    import tempfile

    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    _break_writes(monkeypatch, init_fails={"scope-policy.json"},
                  rollback_fails={"machine-sources.json"})
    real_open = builtins.open

    def fake_open(file, *a, **k):
        if ".bak-init" in str(file) and str(settings.CONFIG_DIR) in str(file):
            raise OSError(28, "No space left on device")
        return real_open(file, *a, **k)

    monkeypatch.setattr(cli, "open", fake_open, raising=False)
    tmpd = tmp_path / "tmpd"
    tmpd.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpd))

    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    (saved,) = tmpd.glob("machine-sources.json.bak-init-*")
    assert saved.read_bytes() == before["machine-sources.json"]
    assert str(saved) in str(exc.value.code)
    assert not list(settings.CONFIG_DIR.glob("*.bak-init*"))


def test_normal_rollback_leaves_no_backup_files(tmp_path, monkeypatch):
    notes = _notes_dir(tmp_path)
    before = _sentinels(settings.CONFIG_DIR)
    _fail_compile(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _run_init(notes, "--force")
    assert "已还原" in str(exc.value.code)
    assert _written(settings.CONFIG_DIR) == before
    assert not list(settings.CONFIG_DIR.glob("*.bak-init*"))


# ---- ⑥ 端到端：init → index → 从无关目录 search ------------------------------

NOTES = {
    "pasta-night.md": (
        "---\ntrust_tier: T2\nsource_url: https://example.org/pasta\n"
        "captured_at: 2026-01-05\n---\n\n"
        "# Pasta night\n\n## Sauce\n\nSimmer the tomatoes slowly with garlic and a pinch of salt "
        "until the sauce thickens, then fold in fresh basil just before serving.\n"),
    "reading-list.md": (
        "# Reading list\n\n## Next up\n\nA long novel about lighthouse keepers, followed by a short "
        "essay collection on gardening; finish one chapter every evening before bed.\n"),
    "bike-repair.md": (
        "---\ntrust_tier: T3\n---\n\n"
        "# Bike repair\n\n## Flat tyre\n\nRemove the wheel, lever the tyre off the rim, patch the "
        "tube with a small kit, and check the inside of the tyre for the thorn.\n"),
}


@pytest.fixture
def initialized(tmp_path, monkeypatch, fake_embed, canary_conn, gate):
    """init 一个自编笔记目录，并用生产 indexer 把它索引进 canary schema。"""
    notes = _notes_dir(tmp_path, "notes-home")
    for name, text in NOTES.items():
        (notes / name).write_text(text, encoding="utf-8")
    _run_init(notes)
    gate(None)
    conftest.seed_canary_schema(canary_conn)
    reg = registry.compile_registry()
    stats = indexer.index_with_registry(reg, canary_conn, schema="canary")
    assert stats["rebuilt"] and stats["files"] == 3

    elsewhere = tmp_path / "elsewhere"          # 与笔记目录毫无关系的 cwd
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return notes.resolve(), reg


def test_end_to_end_search_from_an_unrelated_directory(initialized, canary_conn, capsys,
                                                       monkeypatch):
    notes, reg = initialized
    cwd = Path.cwd().resolve()
    assert cwd != notes and notes not in cwd.parents, "cwd 必须在笔记目录之外"

    # 单域用户不在笔记目录里调用：走 default_profile
    eff = scope.resolve_effective_profile(reg, Path.cwd())
    assert (eff.base, eff.effective, eff.collections) == ("default", "default", ("notes",))

    rows = search.retrieve_candidates(canary_conn, "how do I fix a flat tyre", eff, 10,
                                      schema="canary")
    assert rows and {r[2] for r in rows} == {"notes"}
    keys = {r[0] for r in rows}
    assert keys == {"notes/pasta-night.md", "notes/reading-list.md", "notes/bike-repair.md"}
    for key in keys:
        opened = registry.resolve_location(reg, key)
        assert opened.is_absolute() and opened.is_file() and notes in opened.parents

    line = provenance.provenance_line("notes/pasta-night.md", "notes", reg)
    assert "T2" in line and "example.org" in line

    # 同一条路径走 CLI 的 search：输出里是可打开的绝对路径与出处
    real = cli.retrieve_candidates
    monkeypatch.setattr(cli, "connect", lambda: canary_conn)
    monkeypatch.setattr(cli, "retrieve_candidates",
                        lambda conn, q, e, limit: real(conn, q, e, limit, schema="canary"))
    cli.cmd_search(argparse.Namespace(query="pasta sauce", limit=10, profile=None,
                                      include=None, why=None))
    out = capsys.readouterr().out
    assert str(notes / "pasta-night.md") in out
    assert "出处：" in out and "T2" in out


# ---- ⑦ init 之后改 config.json：世代门拒绝，index 后恢复 ------------------------

def test_editing_config_after_init_is_gated_until_reindex(initialized, canary_conn):
    _notes, reg = initialized
    old_eff = scope.resolve_effective_profile(reg, Path.cwd())

    cfg_path = settings.CONFIG_DIR / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["skip_filenames"] = ["bike-repair.md"]
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    new_reg = registry.compile_registry()
    assert new_reg.registry_hash != reg.registry_hash, "skip_filenames 决定谁进索引，必须进世代"

    with pytest.raises(scope.ScopeError) as exc:      # 索引还是旧世代
        search.retrieve_candidates(canary_conn, "flat tyre", old_eff, 10, schema="canary")
    assert "scoped-kb index" in str(exc.value)

    new_eff = scope.resolve_effective_profile(new_reg, Path.cwd())
    with pytest.raises(scope.ScopeError) as exc2:
        search.retrieve_candidates(canary_conn, "flat tyre", new_eff, 10, schema="canary")
    assert "scoped-kb index" in str(exc2.value)

    stats = indexer.index_with_registry(new_reg, canary_conn, schema="canary")
    assert stats["rebuilt"]
    rows = search.retrieve_candidates(canary_conn, "flat tyre", new_eff, 10, schema="canary")
    assert {r[0] for r in rows} == {"notes/pasta-night.md", "notes/reading-list.md"}
