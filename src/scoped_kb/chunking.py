import re

from scoped_kb import registry

NO_INDEX = "<!-- no-index -->"


# embedding 服务（如 Ollama）会对超长输入报 "input length exceeds the context length"。
# 限制按 token 算、中文密度高，所以字符数只能当保守代理：字符数更少的密集中文分块可能
# 超限，而字符数更多的偏英文/表格分块反而能过。阈值取得偏保守；
# 换 embedding 模型后如出现这个错，先把这个数调小。
_MAX_CHUNK_CHARS = 1600


def _split_oversized(part, max_chars=_MAX_CHUNK_CHARS):
    """把过大的分块在中文/英文句读处贪心切开，每段仍保留原标题行方便检索。"""
    if len(part) <= max_chars:
        return [part]
    lines = part.split("\n")
    heading = lines[0] if lines[0].startswith("## ") else ""
    body = "\n".join(lines[1:]) if heading else part
    pieces = re.split(r"(?<=[；。！？;.!?])", body)
    out, buf = [], ""
    for piece in pieces:
        if buf and len(buf) + len(piece) > max_chars:
            out.append(buf)
            buf = ""
        buf += piece
    if buf:
        out.append(buf)
    return [f"{heading}\n{p.strip()}" if heading else p.strip() for p in out if p.strip()]


def chunk(path):
    """按二级标题切块。每块前置文件名——标题往往是最强的检索信号，
    而正文里经常不重复它。只前置文件名、不前置位置键：目录名与 owner
    不是内容，混进分块会改变向量与排序。

    含 NO_INDEX 标记的段落整段跳过。用途是语音转写全文这类
    「留着给人读、但不该参与检索」的长正文——它们量大、口语化，
    全量入库会把真正验证过的结论挤出结果页。

    单个 "## " 块仍可能超出 embedding 模型的上下文上限（一段没有子标题的长
    正文），此时 _split_oversized 再按句读切——否则这一个分块会让整批
    embedding 请求 400，连带挡住同批次其它文件的索引。
    """
    text = re.sub(r"^---\n.*?\n---\n", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    parts, buf = [], []
    for line in text.splitlines():
        if line.startswith("## ") and buf:
            parts.append("\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        parts.append("\n".join(buf))
    out = []
    for p in parts:
        if len(p.strip()) <= 60 or NO_INDEX in p:
            continue
        for sub in _split_oversized(p.strip()):
            out.append(f"{path.stem}\n{sub}")
    return out


class ScanResult(list):
    """scan 的返回值：照旧是 [(collection_id, 位置键, 绝对路径)] 列表，
    另带 skipped——因真实路径越出所属 root 而被跳过的文件，供 index 计数提示。"""

    def __init__(self, *args):
        super().__init__(*args)
        self.skipped = []


def scan(reg):
    """遍历 registry 的 active roots，返回 [(collection_id, 位置键, 绝对路径)]。

    **来源只有 registry 这一份运行时权威**：归属完全由 registry 决定，
    不按目录名另算，也没有第二张静态来源表。excludes 是各 collection 自己的声明。

    **真实路径不在所属 root 内的文件一律不索引**（根内的符号链接指向根外，
    不论指向别的域还是指向域外任意位置）：否则别处的内容会以本域的名义进索引。
    跳过的文件记在返回值的 skipped 里，不静默。指向根内另一文件的链接照常索引。

    威胁模型边界：分域隔离防的是**配置与归属错误**，不防对笔记目录有写权限的本地对手。
    这里做完边界检查、到 chunk() 读取文件之间有一个窗口，若有人在此期间把文件换成指向
    域外的链接，域外正文会被索引进来。不为此做基于目录描述符的安全打开：能写笔记目录的人
    本来就能把任何内容直接写进任何 collection，堵这个窗口得不到额外的隔离。
    """
    out = ScanResult()
    for col in reg.collections.values():
        if col.index_policy not in registry.INDEXED_POLICIES:
            continue
        for root in col.roots:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*.md")):
                if f.name in reg.skip_filenames:
                    continue
                rel_parts = f.relative_to(root).parts[:-1]
                if col.excludes and set(rel_parts) & set(col.excludes):
                    continue
                if not registry.is_within(f, root):
                    out.skipped.append(f)
                    continue
                target = reg.collection_for_path(f)
                if target is None:
                    continue
                if reg.collections[target].index_policy not in registry.INDEXED_POLICIES:
                    continue          # manifest 判为 quarantine 的文件不进生产索引
                out.append((target, registry.location_key(reg, target, f), f))
    return out
