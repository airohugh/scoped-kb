import re

from scoped_kb import registry

# ---- 出处 ---------------------------------------------------------------
#
# **只在呈现层，绝不进 embedding。**
#
# 为什么不进块内文本：块内文本 = knowledge_index.content，进 embedding、影响排序；
# 而真正的痛点（外部 agent 分不清自有的已验证判例和外部采集的转述）完全落在输出给
# 它的那份文本上。在前者里加出处要付排序风险的代价，却不多解决任何问题。
#
# 渲染哪些字段由 config.json 的 provenance_fields 决定（默认见
# registry.DEFAULT_PROVENANCE_FIELDS）：缺省只有 trust_tier / source_url / captured_at
# 这三个声明可信度的字段。source_author（「谁说的」）要在 provenance_fields 里自己加。
# 白名单以外的 frontmatter 字段（如 project_id / status）一律不输出。


PROVENANCE_PREFIX = "> 出处："


# pack 的 token 成本约束：出处开销占到预算这一比例以上，就把 source_url 降级成域名。
# 为什么不按「单行超 N 字就降级」：长链接普遍让单行超 90 字，那等于**默认**降级，
# 而域名回不到原文——图例偏偏要求「引用前回源文件与 source_url 核对」，自相矛盾。
# 0.10 取「出处开销占 5–10% 可接受」的上界。
PROVENANCE_BUDGET_SHARE = 0.10


_TIER_LABEL = {"T1": "T1 官方/一手", "T2": "T2 权威机构", "T3": "T3 社区/二手"}


# 归属类别取自 registry 的 kind_default，**不靠路径匹配**。
# 理由：目录约定不是运行时权威（见 scan() 的注释），而且自有笔记也可能带
# trust_tier，只看 frontmatter 会误判。
_KIND_LABEL = {
    "raw": "外部采集",
    "decision": "自有产出·判例",
    "session_digest": "自有产出·会话蒸馏",
    "memory": "自有产出·记忆",
    "note": "自有产出·笔记",
    "digest": "自有产出·摘要",
    "reference": "自有产出·参考",
}


PROVENANCE_LEGEND = (
    "**出处图例：** `自有产出` = 本人验证过的判例／会话蒸馏／记忆／笔记；"
    "`外部采集` = 抓来的原始资料，按 trust_tier 分 T1 官方/一手、T2 权威机构、"
    "T3 社区/二手。**T3 是未经核实的个人观点，引用前必须回源文件与 source_url "
    "核对。** 无标注即该文件没有声明，不是「已核实」。"
)


def read_provenance(reg, file_path, fields=None, collection_id=None):
    """回读源文件 frontmatter，只取白名单字段（fields 缺省取 reg.provenance_fields）。

    file_path 是位置键，经 registry.resolve_location 找回文件。

    collection_id 是索引行记的 collection。给了就还要核对：文件**真实路径**当前归属的
    collection 必须正是它、且是可索引策略，否则不读。为什么 owner 边界代替不了：
    同一 owner 下的几个分区（已确认 / 另一分区 / quarantine）共用同一个基准目录，
    索引后文件被换成指向同目录另一分区的链接，hash 不变、owner 边界也过，
    读出来的却是不属于这一行的文件。归属判定用 collection_for_path（它先解析真实
    路径、再按 root 与 manifest 判定），不另造第二套规则。同一 collection 内链接到
    另一个文件照常可读。不给则只有 owner 边界这一层（仅供不经索引行的直接调用）。

    为什么回读文件而不是加 DB 列：位置键已知、文件就在盘上，读一次几乎零成本，
    不需要动 schema、不需要重建索引。**文件是事实源，索引是派生物**——这里顺着
    同一个方向取值。

    读不到返回空 dict：出处缺失是可以如实呈现的，崩掉整个包不是。包括
    没有 registry、位置键解析不了（owner 未知，索引与配置不一致）、
    索引滞后于磁盘、文件已挪走。
    """
    if reg is None:
        return {}
    if fields is None:
        fields = reg.provenance_fields
    try:
        path = registry.resolve_location(reg, file_path)
        if collection_id is not None:
            actual = reg.collection_for_path(path)
            if actual != collection_id or \
                    reg.collections[actual].index_policy not in registry.INDEXED_POLICIES:
                return {}
        text = path.read_text(encoding="utf-8")
    except (registry.RegistryError, OSError):
        return {}
    m = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        # 只认顶层键。记忆文件的 metadata 是嵌套块，缩进键不是本文件的出处声明。
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not kv:
            continue
        # 剥 YAML 行内注释。判据与 YAML 一致：`#` 前必须有空白，所以
        # `https://example.com/a#frag` 这种 URL 片段不会被误切。
        # 典型例子：`trust_tier: T1      # 自有产出`——整段注释不得带进出处行。
        val = re.sub(r"\s+#.*$", "", kv.group(2)).strip().strip('"\'')
        key = kv.group(1)
        if key in fields and val:
            out[key] = val
    return out


def provenance_line(file_path, collection_id, reg=None, compact=False):
    """渲染一行出处。约 40–70 字；compact 时 source_url 只留域名。"""
    fm = read_provenance(reg, file_path, collection_id=collection_id)
    parts = []

    col = reg.collections.get(collection_id) if reg is not None else None
    if col is not None:
        label = _KIND_LABEL.get(col.kind_default)
        if label:
            parts.append(label)
    # reg 缺失或 kind 不认识时**不猜**归属，宁可只报 frontmatter 里有的东西。

    if "trust_tier" in fm:
        parts.append(_TIER_LABEL.get(fm["trust_tier"], fm["trust_tier"]))
    if "source_author" in fm:
        parts.append(f"@{fm['source_author']}")
    if "captured_at" in fm:
        parts.append(f"采集 {fm['captured_at']}")

    url = fm.get("source_url", "")
    if url and not url.startswith(("http://", "https://")):
        # 手工录入的资料可能把 source_url 写成描述性文字，其中可能夹着联系方式。
        # **那种值不能原样输出**：输出可能被粘给外部 agent。
        # frontmatter 本来不进索引，出处回读是新开的暴露面，必须在这里堵住。
        url = "非链接来源（见源文件）"
    elif url and compact:
        # 只降 URL，不截断作者名或日期——截半个作者名比一行长更糟。
        url = re.sub(r"^https?://", "", url).split("/")[0]
    line = " · ".join(parts + ([url] if url else []))
    if not line:
        # 不留空、不猜。
        line = "未标注 —— 回源文件核实"
    return PROVENANCE_PREFIX + line


def render_pack(task, picked, used, files, reg=None, budget=None, with_spans=False):
    """把选出的片段渲染成可粘贴的 markdown。**生产渲染逻辑。**

    片段标题用位置键（`<owner>/<相对路径>`），不用绝对路径：这一包是要粘给
    外部 agent 的，绝对路径会把本机目录结构一起送出去。片段正文原样保留，
    正文里自己写着的路径不在此列。
    reg 可选：拿不到 registry 就降级成「只报 frontmatter 里有的」，不崩、不猜归属。
    budget 给了才做降级判断；不给就按完整 source_url 渲染。

    with_spans 时返回 (包, spans)，spans 是 [(首行, 末行, 片段序号, 位置键)]，行号从 1 起。
    凭据守卫靠它把命中行对回片段——不能回头解析包里的「### 来源」标题，
    正文里同样可以写出这样一行。
    """
    lines = [provenance_line(fp, cid, reg) for fp, _c, cid, _s in picked]
    if budget and sum(len(x) for x in lines) > budget * PROVENANCE_BUDGET_SHARE:
        lines = [provenance_line(fp, cid, reg, compact=True) for fp, _c, cid, _s in picked]
    frags = [f"### 来源 {i}：`{fp}`　（相关度 {sim:.3f}）\n{line}\n\n{content}"
             for i, ((fp, content, _cid, sim), line) in enumerate(zip(picked, lines), 1)]
    head = (f"# 任务上下文包\n\n**任务：** {task}\n\n"
            f"**这一包是按任务从知识库检索出来的 {len(picked)} 个片段"
            f"（{len(files)} 个文件、{used} 字），不是全部资料。** "
            f"引用的结论请回源文件核实。\n\n"
            f"{PROVENANCE_LEGEND}\n\n---\n\n")
    pkg = head + "\n\n".join(frags) + "\n"
    if not with_spans:
        return pkg
    spans, first = [], head.count("\n") + 1
    for i, ((fp, _c, _cid, _s), frag) in enumerate(zip(picked, frags), 1):
        last = first + frag.count("\n")
        spans.append((first, last, i, fp))
        first = last + 2              # 片段之间隔一个空行
    return pkg, spans
