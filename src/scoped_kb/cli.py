"""scoped-kb —— 带分域作用域的本地知识库命令行入口。

所有 agent 都经它读写向量库，不直连 SQL。决定性理由：不同 agent 若各用各的
embedding 模型，向量不可比，整个共享索引失效；embedding 调用必须被这一个入口独占。

设计取舍：
  - 纯向量检索，不做混合。
  - 文件是事实源，本表是派生物：整表删掉重建即可，不必备份。

用法：
    scoped-kb init <笔记目录>  # 生成单域配置（首次使用）
    scoped-kb index      # 增量索引（registry 换代时自动全量重建）
    scoped-kb search "…" # 检索
    scoped-kb pack "…"   # 按任务打包一份可粘贴给外部 agent 的上下文（带凭据守卫）
    scoped-kb backfill   # 补算 embedding 为 NULL 的行
    scoped-kb status     # 看索引状态
    scoped-kb registry   # 编译校验、dry-run、指纹、世代激活
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from scoped_kb import indexer, registry, settings
from scoped_kb.db import connect
from scoped_kb.provenance import PROVENANCE_PREFIX, provenance_line, render_pack
from scoped_kb.scope import (
    SCOPE_ERROR_EXIT,
    ProfileError,
    ScopeError,
    _log_cross,
    _require_trace_ids,
    resolve_effective_profile,
)
from scoped_kb.search import (
    _assert_index_readable,
    retrieve_candidates,
    select_for_pack,
)
from scoped_kb.secret_guard import SECRET_GUARD_EXIT, SECRET_LABELS, scan_secrets

# init 写出的单域配置的固定名字与文件清单
INIT_COLLECTION = "notes"
INIT_PROFILE = "default"
INIT_FILES = ("machine-sources.json", "scope-policy.json", "config.json")


def _save_original(target, data):
    """还原失败的文件：把它原来的内容存到旁边的 <文件>.bak-init，返回写给人看的说明。

    备份不覆盖已有文件（上一次失败留下的备份可能才是真正的原件）；
    原来就不存在的文件无内容可存，只提示手工删除。
    """
    if data is None:
        return "该文件原本不存在，请手工删除它"
    for i in range(100):
        bak = target.with_name(target.name + ".bak-init" + (f".{i}" if i else ""))
        try:
            with open(bak, "xb") as f:
                f.write(data)
            return f"原内容备份在 {bak}"
        except FileExistsError:
            continue
        except OSError:
            break
    try:                                  # 配置目录旁写不了：退到系统临时目录
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".bak-init-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return f"配置目录旁写不了备份，原内容备份在 {tmp}"
    except OSError:
        return "原内容没能备份下来（配置目录与临时目录都不可写）"


def cmd_init(args):
    """为单域用户生成一套配置：一个笔记目录、一个 collection、一个 profile。

    写三份文件到配置目录，写完立刻用 compile_registry 编译自检；任何一步失败都把
    本次写出的文件还原，不留半套配置——半套配置比没有配置更糟：缺文件会立刻报错，
    指纹对不上的配置却会让后续每个命令都报一个与根因无关的错。

    只覆盖这三份文件，配置目录里的其它东西不碰。已有任一份且没给 --force 时
    一个字节都不写：这些文件可能是用户手工改过的多域配置。三份里任一是符号链接
    （含悬空）时同样一个字节都不写，不论有没有 --force。
    """
    notes = Path(args.notes_dir).expanduser()
    if not notes.is_dir():
        sys.exit(f"⛔ 笔记目录不存在或不是目录: {notes}")
    notes = notes.resolve()

    cfg_dir = settings.CONFIG_DIR
    targets = {name: cfg_dir / name for name in INIT_FILES}
    # 符号链接（含悬空）一律不处理，且不看 --force：写入会跟随链接创建/改写它指向的
    # 文件，而回滚只能按路径删除/改写——对悬空链接还原不了，却会声称已还原。
    linked = [name for name, p in targets.items() if p.is_symlink()]
    if linked:
        sys.exit(f"⛔ 配置文件是符号链接，init 不处理，请手工处理后重试: "
                 f"{', '.join(linked)}（目录 {cfg_dir}）")
    existing = [name for name, p in targets.items() if os.path.lexists(p)]
    if existing and not args.force:
        sys.exit(f"⛔ 配置已存在，拒绝覆盖: {', '.join(existing)}（目录 {cfg_dir}）\n"
                 f"确认要覆盖请加 --force。")
    backup = {name: (p.read_bytes() if p.exists() else None) for name, p in targets.items()}

    def write(name, data):
        targets[name].write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        # machine-sources 先写：指纹要从已写出的声明里算，复用 registry 的唯一实现。
        write("machine-sources.json", {
            "schema_version": registry.SCHEMA_VERSION,
            "collections": [{
                "collection_id": INIT_COLLECTION, "root": str(notes), "excludes": [],
                # note → 出处行显示「自有产出·笔记」；document → 按整篇文档处理，对笔记最中性
                "kind_default": "note", "resolution_default": "document"}]})
        write("config.json", {
            "schema_version": registry.SCHEMA_VERSION, "repos": [], "skip_filenames": [],
            "provenance_fields": list(registry.DEFAULT_PROVENANCE_FIELDS),
            "trace": {"task_id_env": registry.DEFAULT_TRACE_TASK_ENV,
                      "session_id_env": list(registry.DEFAULT_TRACE_SESSION_ENVS)}})
        fingerprint = registry.print_fingerprints({
            "repos": [], "machine_sources": targets["machine-sources.json"]})[INIT_COLLECTION]
        write("scope-policy.json", {
            "schema_version": registry.SCHEMA_VERSION,
            "default_profile": INIT_PROFILE,
            "repo_roots": {str(notes): INIT_PROFILE},
            # owner_repo 对 machine 来源固定是 "machine"；scope / authority 只存储不参与判定
            "collections": {INIT_COLLECTION: {
                "owner_repo": "machine", "scope": INIT_PROFILE, "authority": "authority",
                "index_policy": "active", "fingerprint": fingerprint}},
            "profiles": {INIT_PROFILE: [INIT_COLLECTION]},
            "cross_allowlist": []})
        registry.compile_registry()
    except BaseException as exc:  # 任何失败（含写盘、Ctrl-C）都要还原
        # 逐个文件独立还原：一个文件还原失败不能让循环提前中止，
        # 否则排在它后面的文件会留着新内容，与已还原的旧内容混成一套。
        stuck = []
        for name, p in targets.items():
            try:
                if backup[name] is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(backup[name])
            except Exception as restore_exc:  # noqa: BLE001
                stuck.append((name, backup[name], restore_exc))
        if not stuck:
            msg = f"⛔ 配置生成失败，已还原，未留下半套配置：\n{exc}"
        else:
            head = (f"⛔ 配置生成失败，且有 {len(stuck)} 个文件没能还原，"
                    f"配置目录里现在是新旧混合的半套配置：")
            lines = [f"{head}\n{exc}", ""]
            for name, old, restore_exc in stuck:
                kept = _save_original(targets[name], old)
                lines.append(f"  · {targets[name]}：还原失败（{restore_exc}）；{kept}")
            lines.append("请先按上面的备份手工还原这些文件，再重新运行 init。")
            msg = "\n".join(lines)
        if isinstance(exc, Exception):
            sys.exit(msg)
        # 中断（KeyboardInterrupt / SystemExit）：还原完成后原样抛出，调用方照常看到中断；
        # 只有没能还原时才需要把这段话说出来，免得用户不知道配置目录是半套。
        if stuck:
            print(msg, file=sys.stderr)
        raise

    print(f"已写入单域配置，目录: {cfg_dir}")
    for name in INIT_FILES:
        print(f"  {name}")
    print(f"笔记目录: {notes}（collection={INIT_COLLECTION}，profile={INIT_PROFILE}）")
    print("下一步：")
    print("  scoped-kb index")
    print('  scoped-kb search "<问题>"')
    print("改过配置后重新运行 `scoped-kb index`。")


def cmd_index(args):
    """串行化：多个会话或定时任务可能同时跑索引。

    竞态后果不严重（唯一索引挡重复、索引可重建、下次自愈），但一次删一次插
    交错会让某些分块暂时消失、检索静默变差——不值得为省一把锁承担。
    """
    import fcntl
    indexer._assert_not_in_maintenance()
    settings.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(settings.STATE_DIR / "index.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("另一个索引正在跑，本次跳过")
            return
        _index_locked(args)      # 锁在 with 退出时释放，异常路径也释放


def _index_locked(args):
    try:
        reg = registry.compile_registry()
    except registry.RegistryError as exc:
        sys.exit(f"⛔ registry 编译失败，拒绝索引（坏配置不得破坏上一份可用索引）：\n{exc}")

    conn = connect()

    # 本入口只有一条路径：世代未变走增量，换代自动重建。
    # **显式的生产切换不在这里**——它走 `registry --activate --rebuild
    # --maintenance-override`，条件更严。两条入口刻意不共用任何权限参数。
    active = indexer.read_active_hash(conn)
    if active != reg.registry_hash:
        print(f"检测到 registry 换代 {str(active)[:12]}… → "
              f"{reg.registry_hash[:12]}…，自动执行全量重建")
    stats = indexer.index_with_registry(reg, conn)

    print(f"扫到 {stats['files']} 个文件 / {stats['chunks']} 个分块，"
          f"本次向量化 {stats['embedded']} 个"
          + ("（全量重建）" if stats["rebuilt"] else "（增量）"))
    print(f"世代 {stats['registry_hash'][:12]}… 已提交")
    if stats.get("skipped_links"):
        print(f"⚠ 跳过 {stats['skipped_links']} 个文件：符号链接指向所属目录之外，不索引")
    cmd_status(args, conn)


def cmd_backfill(args):
    indexer._assert_not_in_maintenance()
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT id, content FROM knowledge_index WHERE embedding IS NULL ORDER BY ts")
    rows = cur.fetchall()
    if not rows:
        print("没有待补算的行")
        return
    print(f"待补算 {len(rows)} 行")
    vecs = indexer.embed_batched([c for _, c in rows])
    for (rid, _), v in zip(rows, vecs):
        cur.execute("UPDATE knowledge_index SET embedding=%s, embedding_model=%s WHERE id=%s",
                    (v, settings.MODEL, rid))
    conn.commit()
    print(f"已补算 {len(rows)} 行")


def cmd_search(args):
    reg, eff, trace = _resolve_for_cli(args)
    as_json = getattr(args, "json", False)
    cross = eff.effective == "cross"

    # **整个函数只有这一个保护范围。** eff/trace 一旦解析出来，范围就已经被扩开过，
    # 此后无论从哪里出去（拒绝、异常、中断、输出失败）都必须有流水：
    #   检索  横幅 → 索引闸 → 候选 → 位置解析 → 把整份输出渲染成文本（出错 = 没有结果）
    #   留痕  写 ok 行（先留痕、后输出：没留上痕的结果不交付）
    #   交付  把渲染好的文本写 stdout 并 flush（出错 = 检索成功、交付失败，ok 之后补 failed）
    phase = "检索"
    try:
        # 横幅也在检索阶段：它打印失败（编码不支持、管道已断）同样不能让这次尝试没有流水。
        if not as_json:
            print_scope_banner(eff)
        _assert_index_readable()      # 闸在建连接之前
        conn = connect()
        rows = retrieve_candidates(conn, args.query, eff, args.limit)
        # 库里存的是位置键；给调用方看的是能直接打开的绝对路径。
        # 解析不了说明索引与当前配置对不上，按世代不一致处理（同样留痕、同样退出码）。
        try:
            shown = [str(registry.resolve_location(reg, r[0])) for r in rows]
        except registry.RegistryError as exc:
            raise ScopeError(
                f"索引里的位置键无法解析，拒绝交付本次结果。\n  原因: {exc}\n"
                f"请运行 `scoped-kb index`（检测到配置变化会自动全量重建）后再查。") from exc
        text = _render_search(reg, rows, shown, as_json)

        phase = "留痕"
        if cross:
            _log_cross(args.query, eff, trace, [r[0] for r in rows], "search",
                       outcome="ok", note="" if rows else "无命中")

        phase = "交付"
        print(text)
        sys.stdout.flush()            # 管道断开常在 flush 才暴露，不能留到解释器退出时
    except BaseException as exc:
        # ok 行自己写不进去：再写 failed 也是同一个错，交给 main 以 SCOPE_ERROR_EXIT 终止。
        # 只有「留痕写不进去」（ScopeError）才跳过补记；留痕阶段的中断等其它异常照常补 failed。
        if phase == "留痕" and isinstance(exc, ScopeError):
            raise
        if cross:
            # 先留痕、后输出：下面任何打印都可能因管道/编码失败，流水不能排在它后面。
            # note 只记异常类名（交付失败另标「交付失败」）——异常全文可能带进查询内容。
            if phase == "交付":
                note = f"交付失败: {type(exc).__name__}"
            elif isinstance(exc, ScopeError):
                note = f"scope 拒绝: {(str(exc).splitlines() or [''])[0]}"
            else:
                note = f"异常退出: {type(exc).__name__}"
            try:
                _log_cross(args.query, eff, trace, [], "search",
                           outcome="failed", note=note)
            except ScopeError:
                # 留痕本身写失败仍以那个 ScopeError 终止（不交付），原始错误先打印，
                # 免得被它盖掉。
                print(exc if isinstance(exc, ScopeError)
                      else f"原始错误: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise
        if phase == "检索" and isinstance(exc, ScopeError):
            print(exc, file=sys.stderr)
            sys.exit(SCOPE_ERROR_EXIT)
        raise


def _render_search(reg, rows, shown, as_json):
    """把检索结果整份渲染成要写到 stdout 的文本（不写出）。纯格式化，出错在留痕之前。"""
    if as_json:
        # 钩子按行解析，stdout 上只能有这一个 JSON；无命中就是空数组。
        return json.dumps([{"file_path": path, "collection_id": cid, "sim": sim,
                            "content": content}
                           for path, (_key, content, cid, sim) in zip(shown, rows)],
                          ensure_ascii=False)
    if not rows:
        # 「无命中」与「profile/config 非法」必须能分辨：前者退 0，
        # 后者退 SCOPE_ERROR_EXIT 且不提示重建索引。
        return ("没有命中。若确需跨域，用 "
                '--profile cross --include <域> --why "原因"')
    out = []
    for i, (path, (key, content, cid, sim)) in enumerate(zip(shown, rows), 1):
        first = content.splitlines()[0] if content else ""
        body = " ".join(content.split())[:150]
        out.append(f"\n{i}. [{sim:.4f}] ({cid}) {path}\n"
                   "   出处：" + provenance_line(key, cid, reg).removeprefix(PROVENANCE_PREFIX)
                   + f"\n   {first}\n   {body}…")
    return "\n".join(out)


def cmd_pack(args):
    """按任务挑一包资料，产出可直接粘贴给外部 agent 的 markdown。

    stdout 上只有这一包（横幅与提示都走 stderr），便于直接管道给剪贴板。
    交付前过凭据守卫：检出疑似凭据就中止并以 SECRET_GUARD_EXIT 退出，
    确认是误报才用 --force。

    留痕纪律与 cmd_search 相同：eff/trace 解析出来后，无论从哪里出去
    （拒绝、无命中、守卫中止、异常、交付失败）cross 都必须留一行。
    pack 失败时记下已选中的位置键——内容已经读出来了，只是没交付。
    """
    reg, eff, trace = _resolve_for_cli(args)
    cross = eff.effective == "cross"
    files = set()
    phase = "检索"
    try:
        print_scope_banner(eff, file=sys.stderr)
        _assert_index_readable()      # 闸在建连接之前
        conn = connect()
        candidates = retrieve_candidates(conn, args.task, eff, args.limit)

        if not candidates:
            # 无命中是正常结果，不是错误：退 0。合法的 cross 尝试仍要留痕。
            phase = "留痕"
            if cross:
                _log_cross(args.task, eff, trace, [], "pack", outcome="ok", note="无命中")
            phase = "交付"
            print("没有检索到相关内容。若确需跨域，用 "
                  '--profile cross --include <域> --why "原因"', file=sys.stderr)
            return

        # 覆盖面 + 预算策略只在候选集过滤之后执行。
        picked, used, files = select_for_pack(candidates, args.budget)
        try:
            for key in files:
                registry.resolve_location(reg, key)
        except registry.RegistryError as exc:
            raise ScopeError(
                f"索引里的位置键无法解析，拒绝交付本次结果。\n  原因: {exc}\n"
                f"请运行 `scoped-kb index`（检测到配置变化会自动全量重建）后再查。") from exc
        pkg, spans = render_pack(args.task, picked, used, files, reg, args.budget,
                                 with_spans=True)
        hits = scan_secrets(pkg)

        if hits and not args.force:
            phase = "中止留痕"
            if cross:
                _log_cross(args.task, eff, trace, sorted(files), "pack", outcome="failed",
                           note=f"凭据守卫中止，检出 {len(hits)} 处")
            phase = "已中止"
            print("⛔ 包里检出疑似凭据，已中止。这一包本来是要粘给外部 agent 的：\n",
                  file=sys.stderr)
            for h in hits[:20]:
                print(f"   第 {h.line} 行 {_pack_source_of(spans, h.line)} "
                      f"[{SECRET_LABELS[h.rule]}] {h.preview}", file=sys.stderr)
            if len(hits) > 20:
                print(f"   …另有 {len(hits) - 20} 处", file=sys.stderr)
            print("\n先回源文件确认。确属误报（变量名引用、示例值）再加 --force。",
                  file=sys.stderr)
            sys.exit(SECRET_GUARD_EXIT)

        phase = "留痕"
        if cross:
            _log_cross(args.task, eff, trace, sorted(files), "pack", outcome="ok",
                       note=f"--force 跳过 {len(hits)} 处凭据告警" if hits else "")

        phase = "交付"
        if args.out:
            Path(args.out).write_text(pkg, encoding="utf-8")
            print(f"已写入 {args.out}（{len(picked)} 片段 / {len(files)} 文件 / {used} 字）",
                  file=sys.stderr)
        else:
            print(pkg)
            sys.stdout.flush()        # 管道断开常在 flush 才暴露
        phase = "已交付"              # 之后只剩 stderr 提示；它失败不算交付失败
        if hits:
            print(f"⚠️  --force 已跳过 {len(hits)} 处疑似凭据告警", file=sys.stderr)
    except BaseException as exc:
        # 留痕本身写不进去：交给 main 以 SCOPE_ERROR_EXIT 终止，不再补记。
        # 守卫中止已经留过痕、已交付的 ok 行如实，原样退出。
        if (phase in ("留痕", "中止留痕") and isinstance(exc, ScopeError)) \
                or phase in ("已中止", "已交付"):
            raise
        if cross:
            if phase == "交付":
                note = f"交付失败: {type(exc).__name__}"
            elif isinstance(exc, ScopeError):
                note = f"scope 拒绝: {(str(exc).splitlines() or [''])[0]}"
            else:
                note = f"异常退出: {type(exc).__name__}"
            try:
                _log_cross(args.task, eff, trace, sorted(files), "pack",
                           outcome="failed", note=note)
            except ScopeError:
                print(exc if isinstance(exc, ScopeError)
                      else f"原始错误: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise
        if phase == "检索" and isinstance(exc, ScopeError):
            print(exc, file=sys.stderr)
            sys.exit(SCOPE_ERROR_EXIT)
        raise


def _pack_source_of(spans, line_no):
    """命中行属于包里的哪个片段，按渲染时记下的行区间查，不解析包的正文。

    位置键来自索引而非正文；它本身若带疑似凭据（文件名里写了密钥），也不打出来。
    """
    for first, last, i, key in spans:
        if first <= line_no <= last:
            shown = "路径含疑似凭据，已隐去" if scan_secrets(key) else key
            return f"（片段 {i}：{shown}）"
    return "（包头：任务描述或说明）"


def cmd_status(args, conn=None):
    conn = conn or connect()
    cur = conn.cursor()
    cur.execute("SELECT count(*), count(embedding), count(DISTINCT file_path) FROM knowledge_index")
    total, vec, files = cur.fetchone()
    print(f"\n索引状态：{files} 个文件 / {total} 个分块 / {vec} 条有向量"
          f"{f'（{total - vec} 条待补算）' if total > vec else ''}")
    cur.execute("SELECT collection_id, count(*) FROM knowledge_index GROUP BY 1 ORDER BY 2 DESC")
    for cid, n in cur.fetchall():
        print(f"  {cid:<12} {n}")


def print_scope_banner(eff, file=None):
    """输出首部回显。**必须列出实际 collection IDs，不是计数。**

    只打 `collections=10` 等于没有——分辨不出这 10 个里有没有混进不该有的域，
    而那正是回显要证明的事。IDs 排序输出，便于 diff 与留痕对账。
    """
    file = file or sys.stdout
    print(f"[profile] base={eff.base} effective={eff.effective} "
          f"registry={eff.registry_hash[:12]}…", file=file)
    if eff.effective == "cross":
        print(f"[cross] include={','.join(eff.included)} why={eff.why}", file=file)
    print(f"[collections] {', '.join(eff.collections)}", file=file)


def _resolve_for_cli(args):
    """CLI 侧的统一解析：解析 profile → cross 要追溯 ID。

    profile 解析在**核心**完成（resolve_effective_profile），这里只做 CLI 编排；
    绕过包装脚本直接调本命令得到的权限完全一致。
    """
    try:
        reg = registry.compile_registry()
    except registry.RegistryError as exc:
        sys.exit(f"⛔ registry 编译失败：\n{exc}")
    include = getattr(args, "include", None)
    if include:
        include = [d.strip() for part in include for d in part.split(",") if d.strip()]
    try:
        eff = resolve_effective_profile(
            reg, Path.cwd(), profile=getattr(args, "profile", None),
            include=include or None, why=getattr(args, "why", None))
        trace = _require_trace_ids(reg) if eff.effective == "cross" else (None, None)
    except ProfileError as exc:
        print(exc, file=sys.stderr)
        sys.exit(SCOPE_ERROR_EXIT)

    return reg, eff, trace


def cmd_registry(args):
    """registry 子命令：编译校验、打印指纹、全量分类 dry-run、世代激活。

    **除 `--activate` 外，本子命令不连数据库、不写任何文件**，纯粹是 registry 模块的 CLI 外壳。
    """
    from scoped_kb import registry as _registry

    if args.fingerprints:
        for cid, fp in _registry.print_fingerprints().items():
            print(f"{cid}\t{fp}")
        return

    try:
        reg = _registry.compile_registry()
    except _registry.RegistryError as exc:
        sys.exit(f"⛔ registry 编译失败：\n{exc}")

    if args.check:
        print(f"registry 编译通过，{len(reg.collections)} 个 collection，"
              f"hash={reg.registry_hash}")
        return

    if args.activate:
        conn = connect()
        stats = indexer.activate(reg, conn, schema=args.schema, rebuild=args.rebuild,
                                 maintenance_override=args.maintenance_override)
        print(f"schema={args.schema}　文件 {stats['files']}　分块 {stats['chunks']}")
        print(f"世代已提交: {stats['registry_hash']}")
        return

    if args.dry_run:
        rep = _registry.classify_all(reg)
        print(f"# registry dry-run　hash={reg.registry_hash}")
        print(f"# collections={len(reg.collections)}　"
              f"active_roots={len(reg.active_roots())}\n")

        by_cid = {}
        for bucket in ("active", "quarantine", "excluded", "unclassified"):
            for path, cid, reason in rep[bucket]:
                by_cid.setdefault((bucket, cid, reason), []).append(path)

        for bucket, label in (("active", "ACTIVE（进生产索引）"),
                              ("quarantine", "QUARANTINE（隔离，不进索引）"),
                              ("excluded", "EXCLUDED（明确排除）"),
                              ("unclassified", "UNCLASSIFIED（部署阻断项）")):
            rows = [(cid, reason, paths) for (b, cid, reason), paths in by_cid.items()
                    if b == bucket]
            total = sum(len(p) for _c, _r, p in rows)
            print(f"## {label}　文件数={total}")
            for cid, reason, paths in sorted(rows, key=lambda r: (r[0] or "")):
                col = reg.collections.get(cid)
                extra = ""
                if col is not None and bucket == "quarantine":
                    extra = f"　退出路径: {col.exit_path or '（未注明——黑洞风险）'}"
                print(f"  {cid or '—':32s} {len(paths):5d}  {reason}{extra}")
            print()

        n_unclassified = len(rep["unclassified"])
        print(f"unclassified: {n_unclassified}")
        if n_unclassified:
            for path, _cid, reason in rep["unclassified"][:20]:
                print(f"  ⛔ {path}　{reason}")
            sys.exit(1)
        return

    sys.exit("请指定 --check / --dry-run / --fingerprints / --activate 之一")


def build_parser():
    """参数表单独成函数，便于测试直接断言各入口暴露了哪些参数。

    「某个特权参数有没有被悄悄加回 index 入口」这件事只能对着 parser 断言——
    对着文档断言等于没断言。
    """
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="为一个笔记目录生成单域配置（首次使用）")
    p.add_argument("notes_dir", help="笔记目录（里面放 .md 文件）")
    p.add_argument("--force", action="store_true",
                   help="配置目录里已有这三份文件时覆盖它们（默认拒绝）")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("registry", help="两层 scope 配置的编译校验、dry-run 与世代激活")
    p.add_argument("--check", action="store_true", help="只编译校验并打印 hash")
    p.add_argument("--dry-run", action="store_true", help="全量分类报告")
    p.add_argument("--fingerprints", action="store_true",
                   help="打印各 collection 的声明指纹，供填入中央策略")
    p.add_argument("--activate", action="store_true",
                   help="把当前 registry 提交为索引世代（与索引写入同事务）")
    p.add_argument("--rebuild", action="store_true",
                   help="配合 --activate：先全量清空再重建")
    p.add_argument("--maintenance-override", action="store_true",
                   help="生产 activate 的显式确认（还需维护闸存在）")
    p.add_argument("--schema", default="public",
                   help="目标 schema。测试与演练打 canary，绝不碰生产")
    p.set_defaults(fn=cmd_registry)

    # **刻意不提供 --rebuild / --maintenance-override。** 显式的生产世代切换
    # 只有 `registry --activate --rebuild --maintenance-override` 一个入口；
    # 在这里再开一个同权限的旁路，等于把维护闸纪律降级成一个可选参数。
    # 本入口发现 registry 换代时会自动重建（见 index_with_registry）。
    p = sub.add_parser("index", help="增量索引（registry 换代时自动全量重建）")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="检索")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--profile", help="任务身份。同域可省（由 cwd 推断）；"
                                     "跨域用 cross")
    p.add_argument("--include", action="append",
                   help="cross 时合并的额外域，可重复或逗号分隔")
    p.add_argument("--why", help="cross 时必填：为什么需要跨域")
    p.add_argument("--json", action="store_true",
                   help="机器可读输出：只打一个 JSON 数组，不打横幅与出处（供脚本/钩子解析）")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("pack", help="按任务打包一份可粘贴给外部 agent 的上下文（带凭据守卫）")
    p.add_argument("task", help="任务描述，越具体挑得越准")
    p.add_argument("--profile", help="任务身份。同域可省（由 cwd 推断）；"
                                     "跨域用 cross")
    p.add_argument("--include", action="append",
                   help="cross 时合并的额外域，可重复或逗号分隔")
    p.add_argument("--why", help="cross 时必填：为什么需要跨域")
    p.add_argument("--limit", type=int, default=20, help="最多检索几个片段")
    p.add_argument("--budget", type=int, default=8000, help="字数预算（按字符计）")
    p.add_argument("--out", help="写入文件而非打印")
    p.add_argument("--force", action="store_true",
                   help="确认凭据告警是误报后才用，别默认加上")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("backfill", help="补算 embedding 为 NULL 的行")
    p.set_defaults(fn=cmd_backfill)

    p = sub.add_parser("status", help="看索引状态")
    p.set_defaults(fn=cmd_status)

    return ap


def main():
    args = build_parser().parse_args()
    try:
        args.fn(args)
    except (ProfileError, ScopeError) as exc:
        # 兜底：任何路径漏出的作用域错误都走同一个非零退出码，
        # 与「无命中」的 0 区分，且不提示重建索引。
        print(exc, file=sys.stderr)
        sys.exit(SCOPE_ERROR_EXIT)


if __name__ == "__main__":
    main()
