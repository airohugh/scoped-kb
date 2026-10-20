from scoped_kb import indexer, registry
from scoped_kb.embed import embed
from scoped_kb.scope import ScopeError


def _assert_index_readable():
    """读路径的维护闸。**必须由调用方在拿到 eff/trace 之后调用。**

    位置是 profile 解析之后、connect/embed 之前：
      · 之后 —— 维护期的拒绝仍带正确的 profile 回显，便于排查是谁被挡；
      · 之前 —— 不白建连接、不白跑向量化。
    迁移期索引处于「旧行已清、新行未写」的中间态，此时返回半份结果
    比报错更危险：调用方拿到的是看似正常的答案。

    **为什么不留在 `_resolve_for_cli` 里：** 在那里抛异常时 eff 与 trace 还没
    交回调用方，异常会一路飞到 main 的兜底 handler，`_log_cross` 一次都跑不到。
    于是一次被闸拒绝的合法 cross 就完全没有流水——而「范围已经被扩开过」正是
    最该留痕的一类。放在这里，它落在调用方的 `except ScopeError` 里，
    与其它 scope 拒绝走同一条留痕路径。
    """
    reason = indexer._maintenance_reason()
    if reason is not None:
        raise ScopeError(
            f"索引维护中，拒绝检索——这不是「没有命中」。\n  原因: {reason}")


_CANDIDATE_SQL = """
SELECT file_path, content, collection_id, 1-(embedding <=> %s::vector) AS sim
FROM {schema}.knowledge_index
WHERE embedding IS NOT NULL
  AND collection_id = ANY(%s)
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def retrieve_candidates(conn, query_text, eff, limit, schema="public",
                        config_paths=None):
    """search 与 pack 唯一的候选来源。返回 [(file_path, content, collection_id, sim)]。

    三段式，顺序不可调换：
      1. 预检——在 embedding 之前挡掉空 allowlist 与世代不一致；
      2. 快照内复检 + 取候选——消除 TOCTOU；
      3. 出结果前**用同一份 config source** 复编译。

    `config_paths` 必须与产出 `eff` 时用的那一份完全相同（`None` 也算一份，
    表示 default_paths()）。调用方从 resolve_effective_profile 一路透传下来，
    中途不得替换、不得省略——理由见第 3 段注释。
    """
    schema = indexer._safe_schema(schema)
    allowed = list(eff.collections)
    if not allowed:
        raise ScopeError(
            "allowed collections 为空，拒绝检索。\n"
            "空 allowlist 意味着配置失效，宁可临时无结果，也不回退全库搜索。")

    # ── 1. 预检：便宜，必须在 embedding 之前；读完立刻结束事务 ──────────
    active = indexer.read_active_hash(conn, schema=schema)
    if active is None:
        raise ScopeError(
            "索引世代未提交（knowledge_index_meta 为空）。\n"
            "请先运行 `scoped-kb index`（首次会全量构建；"
            "改过配置后检测到变化会自动全量重建）。")
    if active != eff.registry_hash:
        raise ScopeError(
            f"registry 世代不一致，拒绝检索。\n"
            f"  当前配置编译出: {eff.registry_hash[:12]}…\n"
            f"  索引提交的世代: {active[:12]}…\n"
            f"不能拿新 policy 查询旧归类结果。请运行 `scoped-kb index`"
            f"（检测到配置变化会自动全量重建），完成后再试。")

    # ── 2. embedding：**不在任何已开启的事务内** ────────────────────────
    # 预检那条 SELECT 已由 read_active_hash 提交结束。把 embedding 留在事务里
    # 的话，一次网络往返期间连接就是 idle-in-transaction——阻塞 vacuum、
    # 占着快照，而它看上去什么也没在做。
    qv = embed([query_text])[0]

    # ── 3. 快照内复检 + 取候选 ─────────────────────────────────────────
    # 只写 BEGIN ... READ ONLY 不够：PostgreSQL 默认 READ COMMITTED，READ ONLY
    # 只禁写、不提供跨语句 snapshot 稳定性，同事务两条 SELECT 仍各取新 snapshot。
    #
    # 复检不能省：预检到取候选之间有一段窗口（含 embedding），期间可能跑完
    # 一次 rebuild。复检与候选查询同处一个 REPEATABLE READ 快照，
    # 二者之间不可能再插进一次换代。
    cur = conn.cursor()
    cur.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        cur.execute(f"SELECT active_registry_hash FROM {schema}.knowledge_index_meta")
        snap = cur.fetchone()
        if snap is None or snap[0] != eff.registry_hash:
            raise ScopeError("registry 世代在查询过程中发生变化，本次结果作废。")
        cur.execute(_CANDIDATE_SQL.format(schema=schema), (qv, allowed, qv, limit))
        rows = [(fp, content, cid, sim) for fp, content, cid, sim in cur.fetchall()]
    finally:
        conn.rollback()          # 只读事务，rollback 即释放快照

    # ── 3. 出结果前复编译本地配置 ──────────────────────────────────────
    #
    # **必须传 config_paths，不能裸调 compile_registry()。**
    # 裸调会退回 default_paths()，于是一次 retrieve 里出现两个配置来源：
    #   · eff.registry_hash 来自调用方注入的那一份；
    #   · 这里的复核却读生产默认那一份。
    # 后果两个方向都不报错：测试/canary 场景永远「检测到漂移」而假失败；
    # 生产场景下复核基准是错的，「配置未变」这个结论本身失效——
    # 而它正是丢弃结果的唯一依据。
    if registry.compile_registry(config_paths).registry_hash != eff.registry_hash:
        raise ScopeError(
            "本地配置在查询过程中发生变化，本次结果已丢弃。\n"
            "请运行 `scoped-kb index`（检测到配置变化会自动全量重建）后再查。")
    return rows


def select_for_pack(candidates, budget):
    """从候选行里挑出要装包的片段。**生产选择逻辑，canary 的最终上下文也走它。**

    装包策略：**先保覆盖面，再补深度。**
    第一轮每个文件只取最相关的那一块，第二轮才用剩余预算补同文件的其他块。

    为什么不按相关度直接装：同一个文件的几个分块常常挤占前几名，把另一个文件里
    真正该看的那一块挤出预算。预算有限时，覆盖更多不同来源比把少数来源读透更有用——
    打包器的职责是「让接手的 agent 别漏掉某一类信息」，不是「把某一篇讲全」。
    预算按字符数计；第一块即使超预算也照装，保证非空。

    输入是 retrieve_candidates 的四元组，返回 (picked, used, files)，
    picked 里保留 collection_id，便于调用方与验收断言归属。
    """
    picked, used, files = [], 0, set()
    for want_new_file in (True, False):
        for fp, content, cid, sim in candidates:
            if (fp in files) == want_new_file:
                continue
            if any(c is content for _, c, _, _ in picked):
                continue
            if used + len(content) > budget and picked:
                continue
            picked.append((fp, content, cid, sim))
            used += len(content)
            files.add(fp)
    picked.sort(key=lambda r: -r[3])
    return picked, used, files
