"""两层 scope 配置的编译器。

**纯函数：不连数据库、不发网络、不写文件。** 只读配置文件与
manifest 指向的文件是否存在——后者是发现「悬空 assignment」的唯一手段。

设计约束：
  · 一个 index unit 恰好绑定一个已注册 collection_id；
  · 仓库声明只回答「我有什么」，中央策略回答「谁能读什么」；
  · 本地声明出现中央独占字段即编译失败，不存在「本地覆盖中央」。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from pathlib import Path, PurePosixPath

from scoped_kb import settings

SCHEMA_VERSION = 1

# 出处行默认渲染的 frontmatter 字段。可在 config.json 的 provenance_fields 里改；
# 只影响渲染，不进 registry_hash。
# 作者字段（source_author）不在缺省里，需要的话在 provenance_fields 里自己加。
DEFAULT_PROVENANCE_FIELDS = ("trust_tier", "source_url", "captured_at")

# 跨域检索的可追溯标识来自哪些环境变量。可在 config.json 的 trace 里改；
# 只决定去哪读，不进 registry_hash。
DEFAULT_TRACE_TASK_ENV = "SCOPED_KB_TASK_ID"
DEFAULT_TRACE_SESSION_ENVS = ("SCOPED_KB_SESSION_ID",)
TRACE_FIELDS = ("task_id_env", "session_id_env")

# config.json 允许的全部顶层字段；出现其它字段直接拒绝，免得拼错的键被静默忽略。
CONFIG_FIELDS = ("schema_version", "repos", "skip_filenames", "provenance_fields", "trace")


def default_paths():
    """默认配置路径，**每次调用时**按 settings.CONFIG_DIR 现算。

    不做成模块级常量：CONFIG_DIR 可由环境变量或测试改写，import 时算死会让
    后来的改动不生效。配置目录里放三份文件：
      · config.json          —— 仓库声明路径、跳过的文件名、出处字段（可缺省）
      · scope-policy.json    —— 中央策略
      · machine-sources.json —— 本机来源声明
    """
    config = settings.CONFIG_DIR / "config.json"
    return {
        "repos": load_config(config)["repos"],
        "machine_sources": settings.CONFIG_DIR / "machine-sources.json",
        "scope_policy": settings.CONFIG_DIR / "scope-policy.json",
        "config": config,
    }


# 本地声明里出现任何一个都直接编译失败——放置位置不授予权限，
# 仓库不能通过改自己的声明扩大读取范围。
CENTRAL_ONLY_FIELDS = {"scope", "authority", "index_policy", "profiles",
                       "cross_allowlist", "repo_roots", "manifest_partitions",
                       "fingerprint", "default_profile"}

REPO_COLLECTION_FIELDS = ("collection_id", "roots", "excludes",
                          "kind_default", "resolution_default")

# 进生产索引 ⟺ index_policy 属于这两者。
# confirmed_manifest 的含义是「这个 root 要扫，但每个文件的归属由中央 manifest 逐条裁定」。
INDEXED_POLICIES = ("active", "confirmed_manifest")

# index_policy 的全部合法取值。大小写敏感，不接受其它任何值。
VALID_INDEX_POLICIES = frozenset(
    {"active", "confirmed_manifest", "quarantine", "excluded"})


class RegistryError(Exception):
    """配置编译失败。所有消费者都必须非零退出，不得回退旧行为。"""


@dataclass(frozen=True)
class Collection:
    collection_id: str
    owner_repo: str
    roots: tuple
    excludes: tuple
    kind_default: str
    resolution_default: str
    scope: str
    authority: str
    index_policy: str
    fingerprint: str
    quarantine_reason: str = ""
    exit_path: str = ""
    # 位置键的 owner 前缀（见 location_key）。manifest 分区沿用基础 collection 的 owner，
    # 所以文件在分区之间换归属时位置键不变。
    location_owner: str = ""


@dataclass(frozen=True)
class Registry:
    schema_version: int
    collections: dict
    profiles: dict
    repo_roots: tuple
    cross_allowlist: frozenset
    manifest: dict
    manifest_default: dict
    registry_hash: str
    # 扫描时跳过的文件名。决定哪些文件进索引，所以进 registry_hash。
    skip_filenames: tuple = ()
    # 出处行渲染哪些 frontmatter 字段。只影响呈现，不进 registry_hash。
    provenance_fields: tuple = DEFAULT_PROVENANCE_FIELDS
    # 跨域检索的可追溯标识读哪些环境变量。只影响去哪读，不进 registry_hash。
    trace_task_env: str = DEFAULT_TRACE_TASK_ENV
    trace_session_envs: tuple = DEFAULT_TRACE_SESSION_ENVS
    # cwd 不属于任何已注册仓库根时采用的 profile；None 表示不配，此时照旧报错。
    # 它改变「谁能读到什么」，所以进 registry_hash。
    default_profile: str | None = None
    # owner → 该 owner 的基准目录（已 resolve）。位置键 "<owner>/<相对路径>" 的相对路径
    # 就是相对这个目录。它决定库里存的 file_path 字节，所以进 registry_hash。
    location_bases: dict = dataclass_field(default_factory=dict)

    def allowed_for(self, profile):
        if profile not in self.profiles:
            raise RegistryError(
                f"未知 profile: {profile}（已注册: {', '.join(sorted(self.profiles))}）")
        return self.profiles[profile]

    def collection_for_path(self, abs_path):
        """返回该文件所属的 collection_id；被 excludes 排除或不属任何 root 时返回 None。

        编译期已禁止 root 重叠，因此至多一个 collection 能认领一个路径。
        """
        p = Path(abs_path).resolve()
        for cid, col in self.collections.items():
            for root in col.roots:
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                if col.excludes and rel.parts[:-1] and \
                        set(rel.parts[:-1]) & set(col.excludes):
                    return None          # 明确排除，不是「没归属」
                if col.index_policy == "confirmed_manifest":
                    table = self.manifest.get(cid, {})
                    return table.get(rel.as_posix(), self.manifest_default[cid])
                return cid
        return None

    def active_roots(self):
        """进生产索引的物理 root（含 confirmed_manifest 的 root）。"""
        out = []
        for col in self.collections.values():
            if col.index_policy in INDEXED_POLICIES:
                out.extend(col.roots)
        return tuple(sorted(set(out), key=str))

    def all_roots(self):
        """全部有 root 的 collection，含 quarantine / excluded，供 dry-run 全量分类。"""
        out = []
        for col in self.collections.values():
            out.extend(col.roots)
        return tuple(sorted(set(out), key=str))


def _load(path):
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RegistryError(f"配置缺失: {p}")
    except json.JSONDecodeError as exc:
        raise RegistryError(f"配置不是合法 JSON: {p} —— {exc}")


def _fingerprint(owner_repo, cid, resolved_roots, excludes):
    """归一化声明指纹。中央策略钉住这个值。

    **用 `realpath` 规范化后的实际 roots，不用配置里的原始声明字符串。**

    原始字符串顶不住三种改动——它们都不改那一行文本，却整体换掉了指向的树：
      1. 改上面的 `repo_root`（roots 仍写 `docs/decisions`）；
      2. 把 root 换成指向别处的符号链接；
      3. 路径里含 `..` 或大小写/规范化差异。
    这三种都能让「同一段声明文本」指向另一棵树，顶着已授权的指纹通过，
    中央钉住的东西就名存实亡。规范化后的绝对路径才是真正被授权的对象。

    代价：指纹含机器相关前缀（用户目录路径），因此
      · 生产 `scope-policy.json` 里的值是**本机专属**的，换机器要重算；
      · 提交进仓库的测试夹具写 `__AUTO__`，由 conftest 实例化时注入
        （见 tests/conftest.py 的 `_materialize`）。
    """
    payload = json.dumps({
        "owner_repo": owner_repo,
        "collection_id": cid,
        "roots": sorted(str(r) for r in resolved_roots),
        "excludes": sorted(excludes),
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _require(d, keys, where):
    missing = [k for k in keys if k not in d]
    if missing:
        raise RegistryError(f"配置缺字段: {where} 缺 {', '.join(missing)}")


def _check_schema_version(value, where):
    """schema_version 必须**恰好是 int 1**，不接受等值的其它类型。

    Python 的宽松比较会放行两个 JSON 里合法、手写时又很容易出现的值：
      · `true == 1` 为真，且 `isinstance(True, int)` 也为真（bool 是 int 的子类）；
      · `1.0 == 1` 为真。
    只写 `value == SCHEMA_VERSION` 时两者都通过，于是「版本校验」形同虚设——
    真到了 schema v2，写错类型的旧配置照样被当成已适配。用 `type() is int` 挡住。
    """
    if type(value) is not int or value != SCHEMA_VERSION:
        raise RegistryError(
            f"schema_version 非法: {where} 是 {value!r}（{type(value).__name__}），"
            f"必须恰好是 int {SCHEMA_VERSION}。"
            f"注意 true 与 1.0 在 Python 里都 == 1，但不是合法取值。")


def _check_index_policy(value, where):
    """index_policy 必须先是字符串，再严格属于四个合法值。

    **类型检查必须在枚举检查之前**：`value not in frozenset` 遇到 list / dict
    会抛 `TypeError: unhashable type`——那是崩溃，不是 fail closed，
    调用方看到的是一个跟配置无关的栈回溯。

    未知取值若不报错，后果分两种，同样都不报错：
      · 不在 INDEXED_POLICIES 里 → 被当成「不进索引」，一整个 collection 静默消失；
      · classify_all 里落进 unclassified 桶 → 迟至 dry-run 才发现。
    大小写错误（"Active"）是最容易写出来的一种。
    """
    if not isinstance(value, str) or isinstance(value, bool):
        raise RegistryError(
            f"index_policy 类型非法: {where} 是 {value!r}"
            f"（{type(value).__name__}），必须是字符串。")
    if value not in VALID_INDEX_POLICIES:
        raise RegistryError(
            f"index_policy 非法: {where} 是 {value!r}，"
            f"必须是 {', '.join(sorted(VALID_INDEX_POLICIES))} 之一（大小写敏感）。")


def _build_repo_roots(raw, profiles, where):
    """校验并规范化 repo_roots。

    两条要求：
      1. **规范化后必须唯一。** `/repo` 与 `/repo/.`、`/repo/` 是同一个目录，
         原始字符串却不同。放行的话 resolver 会拿到两条长度相同的祖先匹配，
         只能靠元组顺序静默取一条——同一个 cwd 解析出哪个 profile 取决于
         dict 的插入顺序，而这不会报错。即使两条指向同一 profile 也拒绝：
         重复本身就是配置错误，放行会让「改一处忘另一处」变成潜伏问题。
      2. **每个值必须是已注册 profile。** 写错的话该仓库下的调用会解析出一个
         `allowed_for()` 查不到的 profile，报错时机推迟到检索那一刻。
    """
    if not isinstance(raw, dict):
        raise RegistryError(
            f"repo_roots 类型非法: {where} 是 {type(raw).__name__}，必须是对象。")
    if not raw:
        raise RegistryError(f"repo_roots 为空: {where} 没有注册任何仓库根。")

    seen = {}
    out = []
    for declared, prof in raw.items():
        if not isinstance(prof, str) or isinstance(prof, bool) or not prof.strip():
            raise RegistryError(
                f"repo_roots 的值非法: {declared} → {prof!r}"
                f"（{type(prof).__name__}），必须是非空字符串。")
        if prof not in profiles:
            raise RegistryError(
                f"repo_roots 指向未知 profile: {declared} → {prof}"
                f"（已注册: {', '.join(sorted(profiles))}）")
        root = Path(declared).resolve()
        if root in seen:
            raise RegistryError(
                f"repo_roots 规范化后重复: {declared!r} 与 {seen[root]!r} "
                f"都指向 {root}。\n"
                f"同一个目录只能注册一次——两条映射会让 resolver 拿到长度相同的"
                f"多条匹配，解析结果取决于声明顺序且不报错。")
        seen[root] = declared
        out.append((root, prof))
    return tuple(out)


def _check_nonempty_str(value, field, where):
    """必须是 strip 后非空的字符串。数字、bool、list、dict、None 一律拒绝。"""
    if not isinstance(value, str) or isinstance(value, bool):
        raise RegistryError(
            f"{where} 的 {field} 类型非法: {value!r}（{type(value).__name__}），"
            f"必须是非空字符串。")
    if not value.strip():
        raise RegistryError(
            f"{where} 的 {field} 为空（或只有空白字符），必须写明内容。")


def _check_str_list(value, field, where, bare_name=False):
    """必须是由非空字符串组成的列表。bare_name 时还要求不含路径分隔符。"""
    if not isinstance(value, list):
        raise RegistryError(
            f"{where} 的 {field} 类型非法: {value!r}（{type(value).__name__}），"
            f"必须是字符串列表。")
    for item in value:
        _check_nonempty_str(item, field, where)
        if bare_name and ("/" in item or "\\" in item):
            raise RegistryError(
                f"{where} 的 {field} 含路径: {item!r}，只能写文件名，"
                f"写成路径永远匹配不上，等于没写。")


def load_config(config_path=None):
    """读 config.json，返回规范化后的 dict。文件缺失按默认值处理。

    返回 {"repos": [Path, ...], "skip_filenames": tuple, "provenance_fields": tuple,
    "trace": {"task_id_env": str, "session_id_env": tuple}}。
    字段类型错、出现未知字段一律 RegistryError，不静默忽略。
    repos 里的 `~` 会展开；相对路径相对于 config.json 所在目录。
    """
    out = {"repos": [], "skip_filenames": (),
           "provenance_fields": DEFAULT_PROVENANCE_FIELDS,
           "trace": {"task_id_env": DEFAULT_TRACE_TASK_ENV,
                     "session_id_env": DEFAULT_TRACE_SESSION_ENVS}}
    if config_path is None or not Path(config_path).exists():
        return out
    where = str(config_path)
    data = _load(config_path)
    if not isinstance(data, dict):
        raise RegistryError(f"配置顶层必须是对象: {where} 是 {type(data).__name__}")
    unknown = set(data) - set(CONFIG_FIELDS)
    if unknown:
        raise RegistryError(
            f"配置含未知字段: {where} 的 {', '.join(sorted(unknown))}。"
            f"允许的字段只有 {', '.join(CONFIG_FIELDS)}。")
    _require(data, ("schema_version",), where)
    _check_schema_version(data["schema_version"], where)
    if "repos" in data:
        _check_str_list(data["repos"], "repos", where)
        base = Path(config_path).parent
        out["repos"] = [(base / Path(r).expanduser()) for r in data["repos"]]
    if "skip_filenames" in data:
        _check_str_list(data["skip_filenames"], "skip_filenames", where, bare_name=True)
        out["skip_filenames"] = tuple(sorted(set(data["skip_filenames"])))
    if "provenance_fields" in data:
        _check_str_list(data["provenance_fields"], "provenance_fields", where)
        out["provenance_fields"] = tuple(data["provenance_fields"])
    if "trace" in data:
        out["trace"] = _check_trace(data["trace"], where)
    return out


def _check_trace(value, where):
    """校验 config.json 的 trace 段，缺省的子字段用默认值补齐。

    task_id_env 是一个变量名，session_id_env 是按顺序取第一个非空值的变量名列表。
    未知子字段、类型错、空列表一律拒绝：这两个名字配错了，cross 就会永远被拒
    （或者更糟，读到一个不该读的变量），而不是静默放行。
    task_id_env 不得同时出现在 session_id_env 里——同一个变量不能既当任务标识又当
    会话标识，否则「两个标识」只剩一个。
    """
    if not isinstance(value, dict):
        raise RegistryError(
            f"{where} 的 trace 类型非法: {value!r}（{type(value).__name__}），必须是对象。")
    unknown = set(value) - set(TRACE_FIELDS)
    if unknown:
        raise RegistryError(
            f"{where} 的 trace 含未知字段: {', '.join(sorted(unknown))}。"
            f"允许的字段只有 {', '.join(TRACE_FIELDS)}。")
    task = value.get("task_id_env", DEFAULT_TRACE_TASK_ENV)
    _check_nonempty_str(task, "trace.task_id_env", where)
    if "session_id_env" in value:
        sessions = value["session_id_env"]
        _check_str_list(sessions, "trace.session_id_env", where)
        if not sessions:
            raise RegistryError(
                f"{where} 的 trace.session_id_env 为空列表，至少要写一个变量名。")
        sessions = tuple(sessions)
    else:
        sessions = DEFAULT_TRACE_SESSION_ENVS
    if task in sessions:
        raise RegistryError(
            f"{where} 的 trace 冲突: {task} 同时是任务标识与会话标识的变量名，"
            f"两者必须是不同的变量。")
    return {"task_id_env": task, "session_id_env": sessions}


def _read_declarations(paths):
    """读全部仓库声明 + machine-sources，返回 {cid: 本地声明 dict} 与 repo_root 映射。"""
    local = {}
    declared_repo_roots = {}

    for rp in paths["repos"]:
        data = _load(rp)
        _require(data, ("schema_version", "repo_id", "repo_root", "collections"), str(rp))
        _check_schema_version(data["schema_version"], str(rp))
        _check_nonempty_str(data["repo_id"], "repo_id", str(rp))
        repo_root = Path(data["repo_root"]).resolve()
        declared_repo_roots[data["repo_id"]] = repo_root
        for col in data["collections"]:
            _require(col, REPO_COLLECTION_FIELDS, f"{rp} 的 collection")
            stray = CENTRAL_ONLY_FIELDS & set(col)
            if stray:
                raise RegistryError(
                    f"本地不得声明中央字段: {rp} 的 {col.get('collection_id')} "
                    f"出现 {', '.join(sorted(stray))}。"
                    f"放置位置不授予权限，scope 类字段由中央策略独占。")
            cid = col["collection_id"]
            if cid in local:
                raise RegistryError(f"重复 collection ID: {cid}")
            local[cid] = {
                "owner_repo": data["repo_id"],
                "repo_root": repo_root,
                "location_owner": data["repo_id"],
                "location_base": repo_root,
                "location_kind": "repo",
                "roots": tuple((repo_root / r).resolve() for r in col["roots"]),
                "excludes": tuple(col["excludes"]),
                "kind_default": col["kind_default"],
                "resolution_default": col["resolution_default"],
                "source": str(rp),
            }

    ms = _load(paths["machine_sources"])
    _require(ms, ("schema_version", "collections"), str(paths["machine_sources"]))
    _check_schema_version(ms["schema_version"], str(paths["machine_sources"]))
    for col in ms["collections"]:
        _require(col, ("collection_id", "root", "excludes",
                       "kind_default", "resolution_default"),
                 f"{paths['machine_sources']} 的 collection")
        stray = CENTRAL_ONLY_FIELDS & set(col)
        if stray:
            raise RegistryError(
                f"本地不得声明中央字段: machine adapter 的 {col.get('collection_id')} "
                f"出现 {', '.join(sorted(stray))}")
        cid = col["collection_id"]
        if cid in local:
            raise RegistryError(f"重复 collection ID: {cid}")
        local[cid] = {
            "owner_repo": "machine",
            "repo_root": None,           # machine adapter 不受 repo 边界约束
            "location_owner": cid,
            "location_base": Path(col["root"]).resolve(),
            "location_kind": "machine",
            "roots": (Path(col["root"]).resolve(),),
            "excludes": tuple(col["excludes"]),
            "kind_default": col["kind_default"],
            "resolution_default": col["resolution_default"],
            "source": str(paths["machine_sources"]),
        }

    return local, declared_repo_roots


def _canonical(collections, profiles, repo_roots, cross_allowlist,
               manifest, manifest_default, skip_filenames, default_profile=None,
               location_bases=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "default_profile": default_profile,
        "skip_filenames": sorted(skip_filenames),
        "collections": {
            cid: {**asdict(c), "roots": sorted(str(r) for r in c.roots),
                  "excludes": sorted(c.excludes)}
            for cid, c in sorted(collections.items())},
        "profiles": {p: list(ids) for p, ids in sorted(profiles.items())},
        "repo_roots": sorted((str(r), p) for r, p in repo_roots),
        "cross_allowlist": sorted((b, sorted(inc)) for b, inc in cross_allowlist),
        "manifest": {b: dict(sorted(m.items())) for b, m in sorted(manifest.items())},
        "manifest_default": dict(sorted(manifest_default.items())),
        "location_bases": {o: str(b) for o, b in sorted((location_bases or {}).items())},
    }


def _check_root_boundaries(local):
    """规则 5：root realpath 后必须在 owner repo 内（machine adapter 例外）。"""
    for cid, loc in local.items():
        if loc["repo_root"] is None:
            continue
        for root in loc["roots"]:
            if loc["repo_root"] != root and loc["repo_root"] not in root.parents:
                raise RegistryError(
                    f"root 越界: {cid} 声明的 {root} 不在 owner repo "
                    f"{loc['repo_root']} 内。仓库只能声明自己的目录。")


def _check_root_overlap(local):
    """规则 6：任意两个 collection 的 root 不得互为前缀（含相等）。

    重叠意味着同一个文件能被两个 collection 认领，「一个 index unit 恰好绑定一个
    collection」这条不变量就不成立了。
    """
    flat = [(cid, r) for cid, loc in local.items() for r in loc["roots"]]
    for i, (cid_a, ra) in enumerate(flat):
        for cid_b, rb in flat[i + 1:]:
            if ra == rb or ra in rb.parents or rb in ra.parents:
                raise RegistryError(
                    f"root 重叠: {cid_a} 的 {ra} 与 {cid_b} 的 {rb} 互相包含，"
                    f"同一文件会匹配多个 collection。")


def _check_location_owners(local):
    """位置键的 owner 前缀：合法性、唯一性，返回 {owner: 基准目录}。

    位置键是 "<owner>/<相对路径>"，库里存的 file_path 就是它。三条要求，缺一条
    都会让两个不同的文件落到同一个键、或让一个键解析到别处，而且都不报错：
      1. owner 必须是不含 "/" 的非空字符串——否则按第一个 "/" 切键时切错位置；
      2. 仓库声明的 owner 是 repo_id，machine 来源的 owner 是它自己的 collection_id，
         **两类不得撞名**——撞名意味着两个目录共用一个前缀；
      3. 同一个 repo_id 在多份仓库声明里出现时，repo_root 必须一致，
         否则同一个前缀对应两个基准目录。
    """
    bases = {}
    for cid, loc in local.items():
        owner = loc["location_owner"]
        if not isinstance(owner, str) or not owner.strip() or "/" in owner:
            raise RegistryError(
                f"位置键 owner 非法: {cid} 的 owner 是 {owner!r}，必须是不含 '/' 的"
                f"非空字符串（仓库声明用 repo_id，machine 来源用 collection_id）。")
        seen = bases.get(owner)
        if seen is None:
            bases[owner] = (loc["location_base"], loc["location_kind"], cid)
            continue
        base, kind, first_cid = seen
        if kind != loc["location_kind"]:
            raise RegistryError(
                f"位置键 owner 撞名: {owner!r} 既是仓库声明的 repo_id，"
                f"又是 machine 来源的 collection_id（{first_cid} 与 {cid}）。\n"
                f"两个目录会共用一个位置键前缀，库里的 file_path 无法区分。")
        if base != loc["location_base"]:
            raise RegistryError(
                f"repo_id 重复且 repo_root 不一致: {owner!r} 同时指向 {base} 与 "
                f"{loc['location_base']}（{first_cid} 与 {cid}）。")
    return {owner: base for owner, (base, _kind, _cid) in bases.items()}


def _check_default_profile(value, profiles, where):
    """default_profile 必须是已注册的 profile 名。

    它是 cwd 匹配不到任何仓库根时的兜底身份，写错的话兜底会指向一个
    `allowed_for()` 查不到的 profile，报错时机被推迟到检索那一刻。
    类型检查在成员检查之前：list / dict 做成员检查会抛 unhashable 的 TypeError。
    """
    if not isinstance(value, str) or isinstance(value, bool) or not value.strip():
        raise RegistryError(
            f"default_profile 类型非法: {where} 是 {value!r}"
            f"（{type(value).__name__}），必须是非空字符串。")
    if value not in profiles:
        raise RegistryError(
            f"default_profile 指向未知 profile: {where} 是 {value}"
            f"（已注册: {', '.join(sorted(profiles))}）")


def _check_quarantine_metadata(cid, col_dict, where):
    """quarantine 必须有非空 quarantine_reason 与 exit_path。

    没有原因 → 事后无人知道它为什么被隔离；
    没有退出路径 → 它会积压成只进不出的黑洞。
    空白字符串不算填了。
    """
    if col_dict.get("index_policy") != "quarantine":
        return
    for field in ("quarantine_reason", "exit_path"):
        if field not in col_dict:
            raise RegistryError(
                f"{where} 的 {cid} 是 quarantine，但缺 {field}。"
                f"quarantine 必须写明原因与退出路径，否则会变成只进不出的黑洞。")
        _check_nonempty_str(col_dict[field], field, f"{where} 的 {cid}")


def _check_manifest_assignments(base_cid, base, spec, own_partitions, collections):
    """assignments 的目标、键的位置与键指向的文件是否存在。

    **target 只能是基础 collection 自身、或它自己声明的虚拟分区。**
    放宽成「任何已知 collection」时，一条 assignment 就能把一份全局笔记
    指派给别的域的真实 collection——语法合法、编译通过、不报错，
    但归属已经跨域搬运了。
    """
    assignments = spec["assignments"]
    legal = {base_cid} | set(own_partitions)
    root = base.roots[0]
    dangling = []
    for rel, target in assignments.items():
        if target not in legal:
            known = "已知但无关" if target in collections else "未声明"
            raise RegistryError(
                f"assignment 目标非法: {base_cid} 的 {rel} 指向 {target}（{known}）。\n"
                f"合法目标只有基础来源自身或它自己的虚拟分区: "
                f"{', '.join(sorted(legal))}")
        p = (root / rel).resolve()
        if p != root and root not in p.parents:
            raise RegistryError(
                f"manifest 路径越界: {base_cid} 的 assignment 键 {rel} "
                f"解析为 {p}，不在基础 root {root} 内。")
        if not p.is_file():
            dangling.append(rel)
    if dangling:
        raise RegistryError(
            f"manifest 悬空路径: {base_cid} 的 assignments 指向不存在的文件: "
            f"{', '.join(sorted(dangling))}。\n"
            f"悬空条目会让已确认的 claim 随文件改名静默落回 quarantine 默认分区，"
            f"对所有 profile 消失且无提示。请修正 assignments 或补回文件。")


def compile_registry(config_paths=None):
    """从两层源文件确定性生成内存 registry。相同输入必得相同 registry_hash。

    校验顺序有意为之：先结构（越界/重叠），再授权覆盖，再指纹，最后 profile 与
    manifest。顺序错了会让错误消息指向表象而非根因。
    """
    paths = config_paths or default_paths()
    config = load_config(paths.get("config"))
    local, _declared_repo_roots = _read_declarations(paths)

    _check_root_boundaries(local)
    _check_root_overlap(local)
    location_bases = _check_location_owners(local)

    pol = _load(paths["scope_policy"])
    _require(pol, ("schema_version", "repo_roots", "collections",
                   "profiles", "cross_allowlist"), str(paths["scope_policy"]))
    _check_schema_version(pol["schema_version"], str(paths["scope_policy"]))

    unknown_central = set(pol["collections"]) - set(local)
    if unknown_central:
        raise RegistryError(
            f"未知引用: 中央策略声明了没有任何仓库拥有的 collection: "
            f"{', '.join(sorted(unknown_central))}")

    collections = {}
    for cid, loc in local.items():
        central = pol["collections"].get(cid)
        if central is None:
            raise RegistryError(
                f"未授权的新 collection: {cid} 已被仓库声明，但中央策略未处理它。"
                f"仓库不能通过新增目录自行扩大可读范围。")
        _require(central, ("owner_repo", "scope", "authority", "index_policy",
                           "fingerprint"), f"scope-policy 的 {cid}")
        _check_index_policy(central["index_policy"], f"scope-policy 的 {cid}")
        if central["owner_repo"] != loc["owner_repo"]:
            raise RegistryError(
                f"owner_repo 不一致: {cid} 中央声明为 {central['owner_repo']}，"
                f"本地声明在 {loc['owner_repo']}（{loc['source']}）。"
                f"指纹基准会错位。")
        expected_fp = _fingerprint(loc["owner_repo"], cid,
                                   loc["roots"], loc["excludes"])
        if central["fingerprint"] != expected_fp:
            raise RegistryError(
                f"声明指纹不一致: {cid}\n"
                f"  本地声明算出: {expected_fp}\n"
                f"  中央策略钉住: {central['fingerprint']}\n"
                f"本地 roots/excludes 变更后，必须由中央同步确认新指纹才能生效。")
        collections[cid] = Collection(
            collection_id=cid,
            owner_repo=central["owner_repo"],
            roots=loc["roots"],
            excludes=loc["excludes"],
            kind_default=loc["kind_default"],
            resolution_default=loc["resolution_default"],
            scope=central["scope"],
            authority=central["authority"],
            index_policy=central["index_policy"],
            fingerprint=expected_fp,
            quarantine_reason=central.get("quarantine_reason", ""),
            exit_path=central.get("exit_path", ""),
            location_owner=loc["location_owner"],
        )
        _check_quarantine_metadata(cid, central, "scope-policy.collections")

    # ---- manifest 虚拟分区：无独立 root，继承基础来源的声明指纹 ----------
    manifest_spec = pol.get("manifest_partitions", {})

    # 每个 confirmed_manifest collection 都必须有对应 manifest 配置。
    # 缺了的话，collection_for_path 会在 manifest_default 上 KeyError，
    # 或者更糟——退化成把整个 root 当作基础 collection 直接开放。
    for cid, col in collections.items():
        if col.index_policy == "confirmed_manifest" and cid not in manifest_spec:
            raise RegistryError(
                f"缺少 manifest_partitions: {cid} 的 index_policy 是 confirmed_manifest，"
                f"但中央策略没有为它声明 manifest_partitions。\n"
                f"该 policy 的含义是「逐文件裁定归属」，没有裁定表就无从裁定。")

    manifest = {}
    manifest_default = {}
    for base_cid, spec in manifest_spec.items():
        if base_cid not in collections:
            raise RegistryError(f"未知引用: manifest_partitions 指向未知 collection {base_cid}")
        base = collections[base_cid]
        if base.index_policy != "confirmed_manifest":
            raise RegistryError(
                f"manifest_partitions 只能配给 confirmed_manifest 的 collection，"
                f"而 {base_cid} 的 index_policy 是 {base.index_policy}")
        # manifest 的 assignment 键是**相对基础 root 的路径**，因此基础来源必须
        # 恰好有一个物理 root：
        #   · 零个 → 相对谁都无从谈起，不校验就会在 base.roots[0] 上 IndexError（崩溃）；
        #   · 多个 → 相对第一个？那第二个 root 下的文件永远匹配不到任何 assignment，
        #     全部静默落 default 分区，而人以为自己已经确认过它们了。
        if len(base.roots) != 1:
            raise RegistryError(
                f"confirmed_manifest 的 {base_cid} 必须恰好一个物理 root，"
                f"实际 {len(base.roots)} 个: "
                f"{', '.join(str(r) for r in base.roots) or '（无）'}。\n"
                f"assignment 的键是相对该 root 的路径，多于一个就无法确定相对谁。")
        _require(spec, ("default_partition", "partitions", "assignments"),
                 f"manifest_partitions.{base_cid}")
        own_partitions = set(spec["partitions"])
        for pid, pspec in spec["partitions"].items():
            if pid in collections:
                raise RegistryError(f"重复 collection ID: 虚拟分区 {pid} 与已有 collection 同名")
            _require(pspec, ("scope", "index_policy"),
                     f"manifest_partitions.{base_cid}.partitions.{pid}")
            _check_index_policy(
                pspec["index_policy"],
                f"manifest_partitions.{base_cid}.partitions.{pid}")
            _check_quarantine_metadata(
                pid, pspec, f"manifest_partitions.{base_cid}.partitions")
            collections[pid] = Collection(
                collection_id=pid,
                owner_repo=base.owner_repo,
                roots=(),                        # 无独立 root，只经 manifest 查表到达
                excludes=(),
                kind_default=base.kind_default,
                resolution_default=base.resolution_default,
                scope=pspec["scope"],
                authority=base.authority,
                index_policy=pspec["index_policy"],
                fingerprint=base.fingerprint,    # 继承：基础指纹变则全部分区一并失效
                quarantine_reason=pspec.get("quarantine_reason", ""),
                exit_path=pspec.get("exit_path", ""),
                location_owner=base.location_owner,   # 与基础共享，换归属时位置键不变
            )
        # default_partition 必须是**该基础来源自己的 quarantine 分区**。
        #
        # 它是「未被人工确认的文件落到哪里」的答案。指向 active 的任何东西
        # （基础来源自身、或某个 active 分区），就等于放置位置直接授予了权限——
        # 与「放置位置不授予权限」的原则正面冲突，且完全不报错：
        # 新扔进目录的文件会立刻被对应 profile 读到。
        default_pid = spec["default_partition"]
        if default_pid not in own_partitions:
            raise RegistryError(
                f"default_partition 非法: {base_cid} 指向 {default_pid}，"
                f"它不是该基础来源自己声明的分区。\n"
                f"可选: {', '.join(sorted(own_partitions))}")
        if collections[default_pid].index_policy != "quarantine":
            raise RegistryError(
                f"default_partition 非法: {base_cid} 的 {default_pid} 的 index_policy 是 "
                f"{collections[default_pid].index_policy}，必须是 quarantine。\n"
                f"未经人工确认的文件只能落 review-only quarantine——"
                f"指向 active 等于放置位置直接授予权限。")
        manifest_default[base_cid] = default_pid
        _check_manifest_assignments(base_cid, base, spec, own_partitions, collections)
        manifest[base_cid] = dict(spec["assignments"])

    # ---- profiles / cross ------------------------------------------------
    profiles = {}
    for prof, ids in pol["profiles"].items():
        unknown = [c for c in ids if c not in collections]
        if unknown:
            raise RegistryError(
                f"未知引用: profile {prof} 含未知 collection {', '.join(unknown)}")
        if not ids:
            raise RegistryError(
                f"allowlist 为空: profile {prof} 没有任何允许的 collection。"
                f"空集合意味着配置失效，不是「什么都不给读」。")
        # profile 里出现 quarantine / excluded = 隔离形同虚设，且不报错。
        # quarantine 本就不进生产索引，excluded 连扫都不扫；把它们列进 allowlist
        # 只会让「allowed collections」与「实际可读内容」长期不一致，
        # 掩盖真正的配置错误。此外，未解决归属的混合内容
        # （unresolved-mixed）对所有 profile 均应不可读，连 cross 都不可达。
        bad = [c for c in ids
               if collections[c].index_policy in ("quarantine", "excluded")]
        if bad:
            detail = ", ".join(f"{c}({collections[c].index_policy})" for c in bad)
            raise RegistryError(
                f"profile {prof} 含 quarantine/excluded collection: {detail}。\n"
                f"隔离与排除的集合不得出现在任何 profile 的 allowlist 里。")
        profiles[prof] = tuple(ids)

    default_profile = None
    if "default_profile" in pol:           # 显式写了 null 也算写了，按类型错拒绝
        default_profile = pol["default_profile"]
        _check_default_profile(default_profile, profiles, str(paths["scope_policy"]))

    cross = set()
    for row in pol["cross_allowlist"]:
        _require(row, ("base", "include"), "cross_allowlist 条目")
        base_p, inc = row["base"], row["include"]
        if base_p not in profiles:
            raise RegistryError(f"未知引用: cross_allowlist 的 base {base_p} 不是已知 profile")
        unknown_inc = [d for d in inc if d not in profiles]
        if unknown_inc:
            raise RegistryError(
                f"未知引用: cross_allowlist 的 include 含未知 profile "
                f"{', '.join(unknown_inc)}")
        if not inc:
            raise RegistryError(f"cross_allowlist: base {base_p} 的 include 不得为空")
        if base_p in inc:
            raise RegistryError(
                f"cross_allowlist: base {base_p} 的 include 不得包含自身")
        cross.add((base_p, frozenset(inc)))

    repo_roots = _build_repo_roots(
        pol["repo_roots"], profiles, str(paths["scope_policy"]))

    canonical = _canonical(collections, profiles, repo_roots, cross,
                           manifest, manifest_default, config["skip_filenames"],
                           default_profile, location_bases)
    registry_hash = hashlib.sha256(json.dumps(
        canonical, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()

    return Registry(
        schema_version=SCHEMA_VERSION,
        collections=collections,
        profiles=profiles,
        repo_roots=repo_roots,
        cross_allowlist=frozenset(cross),
        manifest=manifest,
        manifest_default=manifest_default,
        registry_hash=registry_hash,
        skip_filenames=config["skip_filenames"],
        provenance_fields=config["provenance_fields"],
        trace_task_env=config["trace"]["task_id_env"],
        trace_session_envs=config["trace"]["session_id_env"],
        default_profile=default_profile,
        location_bases=location_bases,
    )


def location_key(reg, collection_id, abs_path):
    """文件的位置键 "<owner>/<相对路径>"（POSIX 分隔符），库里的 file_path 存的就是它。

    为什么不存相对 $HOME 的路径：笔记不在家目录下就没法用，换机器、换用户名后
    整张表的路径全部失效。位置键只依赖 registry 里声明的目录，与机器无关。

    owner 取自 collection 的 location_owner（manifest 分区沿用基础 collection 的），
    相对路径相对 owner 的基准目录。abs_path 只规范化父目录，文件本身不跟随符号链接。
    """
    col = reg.collections.get(collection_id)
    owner = col.location_owner if col is not None else ""
    base = reg.location_bases.get(owner)
    if base is None:
        raise RegistryError(f"无法计算位置键: collection {collection_id!r} 没有可用的 owner")
    p = Path(abs_path)
    try:
        rel = (p.parent.resolve() / p.name).relative_to(base)
    except ValueError:
        raise RegistryError(
            f"无法计算位置键: {abs_path} 不在 owner {owner!r} 的基准目录 {base} 内")
    return f"{owner}/{rel.as_posix()}"


def resolve_location(reg, key):
    """位置键 → 绝对路径。owner 未知、格式不对、相对部分想跳出基准目录都报错，不猜。"""
    owner, sep, rel = str(key).partition("/")
    if not sep or not rel:
        raise RegistryError(f"位置键格式非法: {key!r}，应为 '<owner>/<相对路径>'")
    base = reg.location_bases.get(owner)
    if base is None:
        raise RegistryError(
            f"位置键的 owner 未知: {owner!r}（已知: {', '.join(sorted(reg.location_bases))}）。"
            f"索引与当前配置不一致。")
    parts = PurePosixPath(rel).parts
    if PurePosixPath(rel).is_absolute() or ".." in parts:
        raise RegistryError(f"位置键的相对路径非法: {key!r}")
    path = base.joinpath(*parts)
    # 字符串检查挡不住符号链接：已索引的文件事后被换成指向别处的链接，registry hash 不变，
    # 键也仍然合法，读出来的却是基准目录之外的文件。边界必须落在解析后的真实路径上。
    if not is_within(path, base):
        raise RegistryError(
            f"位置键解析后的真实路径越出基准目录（符号链接？）: {key!r}，拒绝读取。\n"
            f"请检查该文件是否被换成指向别处的链接；确认无误后运行 `scoped-kb index`。")
    return path


def is_within(path, base):
    """path 的真实路径（跟随全部符号链接）是否在 base 的真实路径之内。

    目标不存在也能判（非严格解析）：悬空链接指向基准目录外同样算越界。
    链接成环、路径过长等无法解析的情形一律按越界处理——fail-closed，不猜。
    """
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        # Python 3.13 起 resolve() 遇到成环的链接不再抛错，要显式探一次。
        os.stat(path)
    except FileNotFoundError:
        pass
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def classify_all(reg):
    """dry-run 全量分类：遍历所有已声明 root 下的 *.md，逐个判定归属。

    要求「所有现有来源和文件必须且只能属于三类之一」：
      · active     —— 唯一 active collection
      · quarantine —— 明确隔离，带原因与退出路径
      · excluded   —— 明确排除（整个 collection excluded，或命中 excludes 目录）
    第四个桶 `unclassified` 非空即部署阻断——它代表静默漏项。

    返回 {bucket: [(abs_path, collection_id, reason), ...]}。
    """
    out = {"active": [], "quarantine": [], "excluded": [], "unclassified": []}
    seen = set()

    for cid, col in reg.collections.items():
        for root in col.roots:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*.md")):
                if f.name in reg.skip_filenames or f in seen:
                    continue
                seen.add(f)
                rel = f.relative_to(root)
                hit = set(rel.parts[:-1]) & set(col.excludes)
                if hit:
                    out["excluded"].append(
                        (f, cid, f"命中 {cid} 的 excludes: {', '.join(sorted(hit))}"))
                    continue
                target = reg.collection_for_path(f)
                if target is None:
                    out["unclassified"].append((f, None, "无法归入任何 collection"))
                    continue
                tcol = reg.collections[target]
                if tcol.index_policy in INDEXED_POLICIES:
                    out["active"].append((f, target, ""))
                elif tcol.index_policy == "quarantine":
                    out["quarantine"].append(
                        (f, target,
                         tcol.quarantine_reason or "quarantine（未注明原因）"))
                elif tcol.index_policy == "excluded":
                    out["excluded"].append((f, target, f"{target} 整体 excluded"))
                else:
                    out["unclassified"].append(
                        (f, target, f"未知 index_policy: {tcol.index_policy}"))
    return out


def print_fingerprints(config_paths=None):
    """开发辅助：算出各 collection 的规范化声明指纹，供填入中央策略。"""
    paths = config_paths or default_paths()
    local, _ = _read_declarations(paths)
    return {cid: _fingerprint(loc["owner_repo"], cid,
                              loc["roots"], loc["excludes"])
            for cid, loc in sorted(local.items())}
