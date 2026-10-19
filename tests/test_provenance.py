"""出处进**呈现层**，不进 embedding。

核心不变量：这里加的任何东西都**不得进 knowledge_index.content**——出处只在
cmd_search（以及尚未实现的 pack）的输出里出现，因此对排序零影响。
"""
import inspect
import json

import conftest
import pytest

from scoped_kb import chunking, cli, provenance, registry


def _with_notes_dir(r, tmp_path):
    """给 registry 补一个 owner "t"，基准目录是 tmp_path：测试文件写在 tmp_path 下，
    位置键就是 "t/<文件名>"。笔记目录可以在任何地方，不依赖 $HOME。

    出处回读还核对文件当前归属的 collection，所以 s.md / m.md 归外部采集分区 fx-raw，
    其余文件归自有分区 fx-own（夹具 registry 里本来没有 kind_default == "raw" 的 collection）。
    """
    return conftest.attribute_notes_dir(
        r, tmp_path, default="fx-own",
        kinds={"fx-own": "note", "fx-raw": "raw"},
        assignments={"s.md": "fx-raw", "m.md": "fx-raw"})


@pytest.fixture
def reg(fixture_paths, tmp_path):
    """夹具 registry 里没有 kind_default == "raw" 的 collection，
    补一个进去——分类判据正是这个字段。"""
    return _with_notes_dir(registry.compile_registry(fixture_paths), tmp_path)


@pytest.fixture
def reg_author(fixture_paths, tmp_path):
    """显式在 config.json 的 provenance_fields 里加上 source_author 的 registry。

    作者字段不在缺省里，要靠用户自己的配置打开——这里走的就是 config.json 这条真路径。
    """
    cfg = tmp_path / "author-config.json"
    cfg.write_text(json.dumps({
        "schema_version": 1,
        "provenance_fields": [*registry.DEFAULT_PROVENANCE_FIELDS, "source_author"],
    }), encoding="utf-8")
    return _with_notes_dir(registry.compile_registry({**fixture_paths, "config": cfg}), tmp_path)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---- 白名单：缺省只出三个字段，作者字段要自己配 ---------------------------

def test_read_provenance_takes_only_whitelisted_fields(tmp_path, reg):
    """白名单以外的字段（如 project_id / status / external_id）一律不输出。"""
    _write(tmp_path, "a.md", "---\n"
           "source_url: https://example.com/post/123\n"
           "captured_at: 2026-08-05\n"
           "trust_tier: T3\n"
           "project_id: demo\n"
           "status: inbox\n"
           'external_id: "0000000000000000042"\n'
           "source_author: Jane Doe\n"
           "---\n\n# 标题\n\n正文\n")
    # 缺省白名单：三个字段，没有作者
    got = provenance.read_provenance(reg, "t/a.md")
    assert set(got) <= set(registry.DEFAULT_PROVENANCE_FIELDS)
    assert got["trust_tier"] == "T3"
    assert got["captured_at"] == "2026-08-05"
    assert "source_author" not in got
    assert "project_id" not in got and "status" not in got
    assert "external_id" not in got
    # 显式配置 provenance_fields 含 source_author 之后才出作者
    fields = (*registry.DEFAULT_PROVENANCE_FIELDS, "source_author")
    configured = provenance.read_provenance(reg, "t/a.md", fields)
    assert configured["source_author"] == "Jane Doe"
    assert "project_id" not in configured and "status" not in configured
    assert "external_id" not in configured


def test_default_config_does_not_render_author(tmp_path, reg):
    """缺省配置下作者字段不出现在出处行里（即便文件的 frontmatter 写了）。"""
    _write(tmp_path, "s.md", "---\ntrust_tier: T3\ncaptured_at: 2026-08-05\n"
           "source_author: Jane Doe\n---\n\n正文\n")
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    line = provenance.provenance_line("t/s.md", raw_cid, reg)
    assert "Jane Doe" not in line and "@" not in line
    assert "T3 社区/二手" in line and "2026-08-05" in line
    # 没有 registry 就解析不了位置键，读不到 frontmatter，如实标「未标注」
    line = provenance.provenance_line("t/s.md", "x", None)
    assert "Jane Doe" not in line and "未标注" in line


def test_read_provenance_survives_missing_file_and_no_frontmatter(tmp_path, reg):
    """索引滞后于磁盘是常态；读不到不能炸，也不能猜。"""
    _write(tmp_path, "plain.md", "# 只有正文\n\n没有 frontmatter。\n")
    assert provenance.read_provenance(reg, "t/plain.md") == {}
    assert provenance.read_provenance(reg, "t/不存在的文件.md") == {}


def test_read_provenance_returns_empty_when_location_cannot_be_resolved(tmp_path, reg):
    """没有 registry、owner 未知、想跳出基准目录：都读不到，也不猜、不崩。"""
    _write(tmp_path, "a.md", "---\ntrust_tier: T3\n---\n\n正文\n")
    assert provenance.read_provenance(None, "t/a.md") == {}
    assert provenance.read_provenance(reg, "nope/a.md") == {}
    assert provenance.read_provenance(reg, "t/../a.md") == {}


def test_read_provenance_ignores_indented_keys(tmp_path, reg):
    """记忆文件的 metadata 是嵌套块，缩进键不是顶层字段，不得被当成出处。"""
    _write(tmp_path, "m.md", "---\nname: x\nmetadata:\n  trust_tier: T1\n---\n\n正文\n")
    assert provenance.read_provenance(reg, "t/m.md") == {}


# ---- 归属类别取自 registry，不靠路径猜 ----------------------------------

def test_raw_collection_is_labelled_external(tmp_path, reg_author):
    """kind_default == raw ⟺ 外部采集。这是 registry 的裁决，不是路径匹配。"""
    reg = reg_author
    _write(tmp_path, "s.md", "---\ntrust_tier: T3\ncaptured_at: 2026-08-05\n"
           "source_author: Jane Doe\n---\n\n正文\n")
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    line = provenance.provenance_line("t/s.md", raw_cid, reg)
    assert "外部采集" in line
    assert "T3 社区/二手" in line
    assert "Jane Doe" in line
    assert "2026-08-05" in line


def test_own_output_without_frontmatter_is_labelled_not_blank(tmp_path, reg):
    """无 frontmatter 的自有产出标「自有产出」，不留空、不猜。"""
    _write(tmp_path, "own.md", "# 判例\n\n正文\n")
    own_cid = next(c for c, v in reg.collections.items() if v.kind_default != "raw")
    line = provenance.provenance_line("t/own.md", own_cid, reg)
    assert "自有产出" in line
    assert line.strip().endswith("自有产出") or "自有产出·" in line


def test_unknown_collection_does_not_guess(tmp_path, reg):
    """registry 里没有这个 cid 时不许编一个归属出来。"""
    _write(tmp_path, "x.md", "# 无 frontmatter\n")
    for r in (None, reg):
        line = provenance.provenance_line("t/x.md", "根本不存在的-cid", r)
        assert "自有产出" not in line and "外部采集" not in line
        assert "未标注" in line


# ---- 降级：source_url → 域名，只在超预算时 ------------------------------

def _external(tmp_path, name="s.md"):
    url = "https://example.com/post/123"
    _write(tmp_path, name, f"---\ntrust_tier: T3\ncaptured_at: 2026-08-05\n"
           f"source_author: Jane Doe\nsource_url: {url}\n---\n\n正文\n")
    return url


def test_url_is_kept_in_full_by_default(tmp_path, reg):
    """默认不降级。**域名回不到原视频**，而图例刚要求「回源与 source_url 核对」——
    默认砍成域名会让这条指令无法执行。"""
    url = _external(tmp_path)
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    assert url in provenance.provenance_line("t/s.md", raw_cid, reg)


def test_compact_degrades_url_to_domain_only(tmp_path, reg_author):
    """降级只砍 URL，作者名与日期照旧——截半个作者名比一行长更糟。"""
    reg = reg_author
    url = _external(tmp_path)
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    line = provenance.provenance_line("t/s.md", raw_cid, reg, compact=True)
    assert url not in line and "example.com" in line
    assert "Jane Doe" in line and "2026-08-05" in line


@pytest.mark.skip(reason="pack is not implemented yet")
def test_render_pack_degrades_only_when_over_budget(tmp_path, reg):
    """token 成本约束：出处开销占到预算 10% 以上才降级。"""
    url = _external(tmp_path)
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    picked = [("t/s.md", "内容" * 30, raw_cid, 0.7)] * 4

    assert url in provenance.render_pack("t", picked, 240, {"t/s.md"}, reg, budget=8000)
    assert url not in provenance.render_pack("t", picked, 240, {"t/s.md"}, reg, budget=1000)
    assert "example.com" in provenance.render_pack("t", picked, 240, {"t/s.md"}, reg, budget=1000)
    # 不给 budget 就不判断（cmd_search 走这条）
    assert url in provenance.render_pack("t", picked, 240, {"t/s.md"}, reg)


# ---- 装进包 --------------------------------------------------------------

@pytest.mark.skip(reason="pack is not implemented yet")
def test_render_pack_carries_legend_and_one_line_per_fragment(tmp_path, reg):
    """外部 agent 不知道目录约定，所以图例必须在包头，出处必须逐片段跟着。"""
    _write(tmp_path, "s.md", "---\ntrust_tier: T3\nsource_author: Jane Doe\n"
           "project_id: demo\nstatus: inbox\n---\n\n正文\n")
    _write(tmp_path, "own.md", "# 判例\n")
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    own_cid = next(c for c, v in reg.collections.items() if v.kind_default != "raw")
    picked = [("t/s.md", "外部内容" * 20, raw_cid, 0.75),
              ("t/own.md", "判例内容" * 20, own_cid, 0.69)]
    pkg = provenance.render_pack("任务", picked, 160, {"t/s.md", "t/own.md"}, reg, budget=8000)

    assert pkg.count(provenance.PROVENANCE_PREFIX) == len(picked)
    assert "T3" in pkg and "外部采集" in pkg and "自有产出" in pkg
    # 图例：T3 的含义与「引用前回源核对」的指令都要在
    assert "T3 社区/二手" in pkg and "核对" in pkg
    # 白名单在包一级也成立
    assert "status" not in pkg and "inbox" not in pkg


@pytest.mark.skip(reason="pack is not implemented yet")
def test_render_pack_without_registry_still_renders(reg):
    """canary 等场景不传 registry：允许降级，但不得崩。"""
    picked = [("t/a.md", "x" * 80, "canary-work", 0.9)]
    pkg = provenance.render_pack("任务", picked, 80, {"t/a.md"})
    assert "# 任务上下文包" in pkg and provenance.PROVENANCE_PREFIX in pkg


# ---- 不变量：出处绝不进 embedding ---------------------------------------

def test_provenance_never_enters_indexed_text(tmp_path):
    """出处的全部价值都建立在「不影响排序」上。chunk() 必须仍然丢掉
    frontmatter——一旦出处漏进 content，就会影响排序，风险完全不同。"""
    p = _write(tmp_path, "s.md", "---\ntrust_tier: T3\nsource_author: Jane Doe\n"
               "---\n\n## 段落\n\n" + "正文" * 40 + "\n")
    blocks = chunking.chunk(p)
    assert blocks
    assert not [b for b in blocks if "trust_tier" in b or "Jane Doe" in b]
    src = inspect.getsource(chunking.chunk)
    assert "provenance" not in src.lower()


@pytest.mark.skip(reason="pack is not implemented yet")
def test_search_and_pack_both_show_provenance():
    """两个出口都要有出处分级：在终端看 search 结果的人同样需要它。"""
    for fn in (cli.cmd_search, cli.cmd_pack):
        src = inspect.getsource(fn)
        assert "provenance_line" in src or "render_pack" in src, fn.__name__


# ---- 两个边界情况 -------------------------------------------

def test_yaml_inline_comment_is_stripped(tmp_path, reg):
    """YAML 行内注释不得被带进出处行。

    判据与 YAML 一致：`#` 前必须有空白才算注释，所以 URL 里的 #fragment 不会被误切。
    """
    _write(tmp_path, "n.md", "---\ntrust_tier: T1     # 自有产出，结论均已核实\n"
           "source_url: https://example.com/a#frag\n---\n\n正文\n")
    own_cid = next(c for c, v in reg.collections.items() if v.kind_default != "raw")
    line = provenance.provenance_line("t/n.md", own_cid, reg)
    assert "自有产出，结论均已核实" not in line
    assert "T1 官方/一手" in line
    assert "https://example.com/a#frag" in line, "URL 的 #fragment 不是 YAML 注释，不得切掉"


def test_non_url_source_url_is_not_dumped_into_the_pack(tmp_path, reg):
    """手工录入允许把 source_url 写成描述性文字，其中可能夹着联系方式。

    这一包是要粘给外部 agent 的；frontmatter 原本不进索引，出处回读是新开的
    暴露面，必须在这里堵住。
    """
    _write(tmp_path, "m.md", "---\ntrust_tier: T3\n"
           "source_url: N/A（朋友私下转发的文件，无公开链接；联系电话 555-0100）\n"
           "---\n\n正文\n")
    raw_cid = next(c for c, v in reg.collections.items() if v.kind_default == "raw")
    for compact in (False, True):
        line = provenance.provenance_line("t/m.md", raw_cid, reg, compact=compact)
        assert "555-0100" not in line and "私下转发" not in line
        assert "非链接来源" in line
