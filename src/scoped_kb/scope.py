import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scoped_kb import settings


class ProfileError(Exception):
    """profile 解析失败。

    调用方必须转成非零退出，**不得回退全库**——可用性下降是可恢复问题，
    跨域返回是安全失败。
    """


@dataclass(frozen=True)
class Effective:
    """一次请求最终用于授权的 profile 与集合。

    base       —— cwd 推断出的 profile（cross 时仍保留，不被 include 覆盖）
    effective  —— base 或 "cross"
    included   —— cross 时合并的额外域，去重保序；同域为 ()
    why        —— cross 时去空白后非空；同域为 ""
    collections—— 最终 allowed collection IDs，已排序去重
    """
    base: str
    effective: str
    included: tuple
    why: str
    collections: tuple
    registry_hash: str


def _normalize_include(include):
    """把 --include 校验成一个非空的字符串元组，任何非法输入统一抛 ProfileError。

    不做类型检查会漏出三种与配置无关的异常，调用方看到的是栈回溯而不是提示：
      · `include=[{}]`  → `dict.fromkeys` 抛 TypeError: unhashable type
      · `include=[1]`   → 拼错误消息时 `', '.join([1])` 抛 TypeError
      · `include="work"`→ 裸字符串会被逐字符迭代成 'w','o','r','k' 四个「域」
    """
    if include is None:
        raise ProfileError("--profile cross 必须带非空 --include")
    if isinstance(include, (str, bytes)):
        raise ProfileError(
            f"--include 必须是字符串列表，不是裸字符串（收到 {include!r}）。\n"
            f"裸字符串会被逐字符当成域名。正确写法: --include research")
    if not isinstance(include, (list, tuple)):
        raise ProfileError(
            f"--include 类型非法: {include!r}（{type(include).__name__}），"
            f"必须是字符串列表或元组。")
    if not include:
        raise ProfileError("--profile cross 必须带非空 --include")
    for item in include:
        if not isinstance(item, str) or isinstance(item, bool):
            raise ProfileError(
                f"--include 的每一项必须是字符串，收到 {item!r}"
                f"（{type(item).__name__}）。")
        if not item.strip():
            raise ProfileError("--include 不得含空字符串项。")
    return tuple(include)


def _normalize_why(why):
    """--why 必须是 strip 后非空的字符串。

    `(123 or "").strip()` 会抛 AttributeError——非字符串必须统一成 ProfileError。
    """
    if why is None:
        raise ProfileError("--profile cross 必须带去空白后非空的 --why")
    if not isinstance(why, str) or isinstance(why, bool):
        raise ProfileError(
            f"--why 类型非法: {why!r}（{type(why).__name__}），必须是字符串。")
    if not why.strip():
        raise ProfileError(
            "--profile cross 必须带去空白后非空的 --why。\n"
            "跨域扩大范围要记录原因，否则事后无法复盘这次读取是否正当。")
    return why.strip()


def resolve_effective_profile(reg, cwd, profile=None, include=None, why=None):
    """核心 resolver。**必须在核心库内执行，不能只存在于外层包装器。**

    只在包装脚本或某个 hook 里推断，会留下一条绕过包装器直接调用
    本命令就能拿到不同权限的入口。

    契约：cwd 只提供**默认值**不提供授权；未知、歧义、冲突一律
    非零退出；扩大范围只有 cross 一条路径且组合须在中央 allowlist 内。

    cwd 不属于任何已注册仓库根时：中央策略配了 default_profile 就以它为 base，
    没配就报错（不猜测、不回退全库）。default_profile 只补「没有映射」的缺口，
    **不覆盖**显式的仓库根映射；确定 base 之后，显式 --profile 校验与 cross 规则
    对两种来源完全一样。
    """
    cwd = Path(cwd).resolve()

    # 最长仓根匹配。路径的祖先构成一条链，所以最长的那一条在**路径层面**唯一；
    # 但 registry 里可能有两条映射指向同一个 root（编译期已挡，见
    # registry._build_repo_roots），此处是**第二层防御**：手工构造或未来其它来源
    # 的 registry 同样要 fail closed，不得用 max() 静默取首项——那样解析结果
    # 取决于元组顺序，且不报错。
    matches = [(root, prof) for root, prof in reg.repo_roots
               if cwd == root or root in cwd.parents]
    if not matches:
        if reg.default_profile is None:
            raise ProfileError(
                f"cwd 不属于任何已注册仓库根: {cwd}\n"
                f"已注册: {', '.join(str(r) for r, _ in reg.repo_roots)}\n"
                f"不猜测、不回退全库。请从任务所属仓库目录调用，"
                f"或在 scope-policy.json 里配置 default_profile。")
        base = reg.default_profile
    else:
        deepest = max(len(str(root)) for root, _ in matches)
        winners = [(root, prof) for root, prof in matches if len(str(root)) == deepest]
        profiles_at_depth = {prof for _root, prof in winners}
        if len(profiles_at_depth) > 1:
            detail = "；".join(f"{root} → {prof}" for root, prof in winners)
            raise ProfileError(
                f"仓库根解析歧义: cwd={cwd} 的最长匹配对应多个不同 profile。\n"
                f"  {detail}\n"
                f"拒绝解析——静默取其中一条会让权限取决于声明顺序。"
                f"请修正中央策略的 repo_roots。")
        base = profiles_at_depth.pop()

    if profile in (None, base):
        # 同域也排序去重，与 cross 分支保持同一个 dataclass 合同：
        # 不一致的话两条路径产出的 Effective 无法直接比较，
        # 流水里记录的 collection 顺序还会随 profile 的声明顺序漂移。
        return Effective(base, base, (), "",
                         tuple(sorted(set(reg.allowed_for(base)))),
                         reg.registry_hash)

    if profile != "cross":
        known = ", ".join(sorted(reg.profiles))
        raise ProfileError(
            f"cwd 推断为 profile={base}，不接受 --profile {profile}。\n"
            f"单域 profile 之间**不得互换**（已注册: {known}）；\n"
            f"扩大范围只有一条路径：\n"
            f"  --profile cross --include <域> --why <原因>")

    inc = _normalize_include(include)

    # 重复项**拒绝**，不去重放行。规则是：重复、空集合、未知域或表外组合均拒绝。
    # 去重放行会让明显写错的命令静默成功，
    # 也给「白名单只认精确组合」开了一个例外。
    dupes = sorted({d for d in inc if inc.count(d) > 1})
    if dupes:
        raise ProfileError(
            f"--include 含重复项: {', '.join(dupes)}。"
            f"重复必须拒绝，不做去重放行。")

    if base in inc:
        raise ProfileError(
            f"--include 不得包含 base profile 自身（{base}）——"
            f"cross 是并集语义，base 本来就在其中")
    unknown = [d for d in inc if d not in reg.profiles]
    if unknown:
        raise ProfileError(
            f"未知的域: {', '.join(unknown)}"
            f"（已注册: {', '.join(sorted(reg.profiles))}）")

    why = _normalize_why(why)

    if (base, frozenset(inc)) not in reg.cross_allowlist:
        allowed = "；".join(
            f"{b} + {','.join(sorted(i))}" for b, i in sorted(
                reg.cross_allowlist, key=lambda x: (x[0], sorted(x[1]))))
        raise ProfileError(
            f"中央策略未允许该 cross 组合: base={base} include={','.join(inc)}\n"
            f"允许的组合: {allowed}")

    cols = set(reg.allowed_for(base))
    for d in inc:
        cols |= set(reg.allowed_for(d))
    return Effective(base, "cross", inc, why,
                     tuple(sorted(cols)), reg.registry_hash)


SCOPE_ERROR_EXIT = 3          # 与「无命中」的 0 区分


def _first_env(names):
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def _require_trace_ids(reg):
    """cross 是跨域扩权动作，必须可回接到发起它的那次任务。

    读哪些环境变量由配置决定（reg.trace_task_env / reg.trace_session_envs，
    缺省值见 registry）：变量由调用方的运行环境注入，这里只负责取值。
    session 按配置顺序取第一个非空的。

    缺任一 ID 时**拒绝执行**，不写占位。跨域读取必须记录
    「当前任务或会话 ID」；写占位会让记录**看起来完整而实际断链**——
    一条跨域记录无法回接到发起任务，事后追责与复盘都断。
    落一堆占位比没有记录更糟：它让「已记录」这个信号本身失效。

    这与「环境变量不承担授权」不冲突：变量在这里不扩大任何权限，
    只作为**执行前置条件**，缺失时是拒绝执行（更严）而非放宽。
    同域检索没有扩权动作，完全不受此约束。
    """
    session = _first_env(reg.trace_session_envs)
    task = (os.environ.get(reg.trace_task_env) or "").strip()
    missing = []
    if not session:
        missing.append(" / ".join(reg.trace_session_envs))
    if not task:
        missing.append(reg.trace_task_env)
    if missing:
        raise ProfileError(
            f"跨域检索缺少可追溯标识: {', '.join(missing)}。\n"
            f"cross 会扩大可读范围，必须记录当前任务或会话 ID。\n"
            f"没有这两个 ID，事后无法回接到是哪次任务发起的跨域读取。\n"
            f"请在调用环境提供（变量名可在 config.json 的 trace 里配置），"
            f"或改用同域检索（不带 --profile cross）。")
    return task, session


CROSS_LOG_NAME = "cross-domain.jsonl"


def _log_cross(query, eff, trace, paths, module, outcome="ok", note=""):
    """跨域检索留痕。落点是状态目录下的 cross-domain.jsonl，一次一行 JSON，只追加。

    **每一次合法的 cross 尝试都要留痕**，不论结果——无命中、被 scope 拒绝，
    都是「范围已经被扩开」这件事发生过。只在成功交付时记，
    会让记录看起来一切正常，而实际发生过的扩权读取不见了。

    以下各项缺一不可：base/effective profile、请求域、解析后的 collection IDs、
    policy hash、why、返回路径、task ID、session ID；outcome 记录真实结果。
    另有 ts（UTC 时间）。**查询文本的任何片段都不写进记录**（短查询的前缀就是全文），
    只记查询长度（query_chars）；要回溯是哪次检索，靠 task / session ID 与时间。

    **写不进去就拒绝，不静默。** 留痕失败却照常交付结果，等于做了一次
    没有记录的扩权读取；调用方（CLI）把 ScopeError 转成非零退出。
    路径每次现算，不做成模块常量：状态目录可由环境变量或测试改写。
    """
    task, session = trace
    summary = f"跨域检索 {eff.base}+{','.join(eff.included)} · 查询 {len(query)} 字"
    if note:
        summary += f" · {note}"
    line = json.dumps(
        {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "module": f"scoped-kb-{module}", "outcome": outcome, "summary": summary,
         "metadata": {"base_profile": eff.base, "effective_profile": eff.effective,
                      "include": list(eff.included), "collections": list(eff.collections),
                      "policy_hash": eff.registry_hash, "why": eff.why,
                      "paths": list(paths), "task_id": task, "session_id": session,
                      "outcome": outcome, "note": note, "query_chars": len(query)}},
        ensure_ascii=False)
    log_path = settings.STATE_DIR / CROSS_LOG_NAME
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        raise ScopeError(
            f"跨域留痕写入失败，拒绝交付本次结果: {log_path}\n"
            f"  原因: {exc}\n"
            f"cross 扩大了可读范围，没有留痕的扩权读取不被允许。"
            f"请检查状态目录是否可写（SCOPED_KB_STATE_DIR）。") from exc


class ScopeError(Exception):
    """作用域 / 世代校验失败。

    必须非零退出。**世代不一致**（索引世代未提交，或与当前配置编译出的世代对不上）
    时指向 `scoped-kb index`——它检测到配置变化会自动全量重建；**其它作用域错误**
    （空 allowlist、维护闸、留痕写入失败等）不得提示重建索引，索引可能完全正常，
    错的是配置或环境。
    """
