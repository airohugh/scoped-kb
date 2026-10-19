import hashlib
import sys

from scoped_kb import settings
from scoped_kb.chunking import chunk, scan
from scoped_kb.embed import embed
from scoped_kb.scope import ScopeError


def embed_batched(texts, size=None):
    """分批。一次性提交几百条会被 Ollama 以 HTTP 400 拒掉；分批由 embed 完成。"""
    return embed(texts, batch_size=size)


# ---- 维护闸 -------------------------------------------------------------

MAINTENANCE_FLAG = settings.STATE_DIR / "index.maintenance"


def _maintenance_reason():
    """维护闸文件的内容；不在维护态时返回 None。"""
    if MAINTENANCE_FLAG.exists():
        return MAINTENANCE_FLAG.read_text(encoding="utf-8").strip()
    return None


def _assert_not_in_maintenance():
    """所有会写 knowledge_index 的入口（index / backfill）开头都要调。

    唯一豁免是 activate——它**就是**迁移动作本身。

    为什么用文件而不是长驻进程持锁：flock 只在进程活着时有效，而迁移中途要
    换库 owner 身份执行 DDL，中间可能长达几十分钟，任何一个会话手敲
    `scoped-kb index` 都能插进来。闸是文件，跨会话、跨进程、跨崩溃都在。
    """
    if MAINTENANCE_FLAG.exists():
        sys.exit(
            f"索引处于维护状态，拒绝执行。\n"
            f"  原因: {MAINTENANCE_FLAG.read_text(encoding='utf-8').strip()}\n"
            f"  迁移完成后删除该文件；**不要手工删它绕过**——"
            f"迁移中途写入会让索引与世代标记不一致。")


# ---- schema 白名单 ------------------------------------------------------

# 只有这两个 schema 合法：public 是生产，canary 是隔离测试库。
# **不接受任意输入直接 f-string 进 SQL。** 这里不是「防外部攻击」——
# schema 参数来自本进程的 CLI 或测试——而是防手滑：写错一个名字会让索引
# 静默写进一个不存在或不该写的地方，而那类错误信息极难定位。
ALLOWED_SCHEMAS = frozenset({"public", "canary"})


def _safe_schema(schema):
    if schema not in ALLOWED_SCHEMAS:
        raise ValueError(
            f"非法 schema: {schema!r}，只允许 {', '.join(sorted(ALLOWED_SCHEMAS))}")
    return schema


def read_active_hash(conn, schema="public"):
    """读当前索引世代，**读完立刻结束事务**。

    psycopg2 的一条 SELECT 就会隐式开启事务。不显式结束的话，随后几分钟的
    embedding 全程都挂着一个 idle-in-transaction 连接——它阻塞 vacuum、
    占着快照，而这条连接看上去什么也没在做。
    """
    sch = _safe_schema(schema)
    cur = conn.cursor()
    cur.execute(f"SELECT active_registry_hash FROM {sch}.knowledge_index_meta")
    row = cur.fetchone()
    conn.commit()                      # 立刻结束事务，别把 embedding 圈进来
    return row[0] if row else None


class _ChunkList(list):
    skipped = ()


def _chunks_for(reg):
    """扫描并切块，返回 [(collection_id, 位置键, content, content_hash)]。

    返回值是 list 的子类，多带一个 skipped：scan 因符号链接越界而跳过的文件。"""
    scanned = scan(reg)
    out = _ChunkList()
    out.skipped = scanned.skipped
    for cid, rel, path in scanned:
        for c in chunk(path):
            out.append((cid, rel, c, hashlib.sha256(c.encode()).hexdigest()))
    return out


def index_rows(conn, rows, vecs, schema="public"):
    """写入分块。rows 是 [(collection_id, file_path, content, content_hash)]。

    冲突动作是 **DO UPDATE 而非 DO NOTHING**：
    「正文没变、归属变了」必须更新到新 collection——DO NOTHING 会静默
    跳过，那正是重新归类后旧 scope 残留的路径。
    """
    schema = _safe_schema(schema)
    cur = conn.cursor()
    for (cid, rel, content, h), v in zip(rows, vecs):
        cur.execute(
            f"INSERT INTO {schema}.knowledge_index"
            f" (collection_id, source_agent, file_path, content,"
            f"  content_hash, embedding, embedding_model)"
            f" VALUES (%s,%s,%s,%s,%s,%s,%s)"
            f" ON CONFLICT (file_path, content_hash)"
            f" DO UPDATE SET collection_id = EXCLUDED.collection_id",
            (cid, settings.AGENT, rel, content, h, v, settings.MODEL if v else None))


def _commit_generation(conn, registry_hash, schema="public"):
    schema = _safe_schema(schema)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {schema}.knowledge_index_meta (id, active_registry_hash, indexed_at)"
        f" VALUES (true, %s, now())"
        f" ON CONFLICT (id) DO UPDATE"
        f"   SET active_registry_hash = EXCLUDED.active_registry_hash,"
        f"       indexed_at = EXCLUDED.indexed_at",
        (registry_hash,))


def _rebuild_generation(reg, conn, schema="public", rebuild=True):
    """全量重建并提交世代的**机制**，本身不含任何权限判断。

    **DELETE + INSERT + 写 active_registry_hash 必须在同一事务里提交。**
    拆成两条命令 = 两个事务 = 中间必然存在「新索引已提交、世代标记还是旧的」
    的窗口，期间所有 search 因世代不符而拒绝服务；若第二条失败，
    索引与标记永久不一致。

    embedding 在事务外先算完：向量化要调 Ollama，几百个分块可达数分钟，
    不能圈在写事务里长时间持锁。**向量化失败则整体中止、不进事务**——
    不允许「降级写 NULL embedding 却标了新世代」，那会让世代标记为一份
    残缺索引背书。

    **权限判断留在调用方，不在这里。** 两条入口的判断标准本就不同：
      · 显式 `registry --activate` 是人发起的切换动作
        → 维护闸 + override + 强制 rebuild（见 activate）；
      · 监听配置变化而自动触发的普通 index 发现换代后自动重建
        → 已持有 index flock，且闸存在时 cmd_index 早已拒绝，不需要 override。
    把两套判断塞进同一个函数，必然出现「一个入口要求闸存在、另一个要求闸不
    存在」的互斥，自动换代就永远走不到。
    """
    schema = _safe_schema(schema)
    pending = _chunks_for(reg)
    vecs = embed_batched([c for _cid, _rel, c, _h in pending]) if pending else []

    cur = conn.cursor()
    try:
        if rebuild:
            cur.execute(f"DELETE FROM {schema}.knowledge_index")
        index_rows(conn, pending, vecs, schema=schema)
        _commit_generation(conn, reg.registry_hash, schema=schema)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"files": len({r[1] for r in pending}), "chunks": len(pending),
            "embedded": len(vecs), "rebuilt": bool(rebuild),
            "skipped_links": len(pending.skipped),
            "registry_hash": reg.registry_hash}


def activate(reg, conn, schema="public", rebuild=False, maintenance_override=False):
    """**显式**的世代切换入口——只应由 `registry --activate` 调用。

    打生产 public 时三个条件缺一不可：

    1. **维护闸存在**——系统确实处于维护态；
    2. **显式 maintenance_override**——闸存在只说明处于维护态，不代表这一次
       调用是有意的切换；两者都要，才不会被顺手敲出来的一条命令清掉生产索引；
    3. **rebuild 为真**——不 rebuild 就推进世代，等于让新世代标记为一份仍含
       旧行的索引背书。被 deleted / excluded / quarantine 的行原地残留，
       却拿到了「当前世代」的认证，于是一份已经被隔离的文件照样能被检索到，
       而且不报错。生产切换没有「只换标记不换内容」的合法用法。

    canary 不受此限：它本来就是隔离测试库，增量路径也需要覆盖。
    """
    schema = _safe_schema(schema)
    if schema == "public":
        if _maintenance_reason() is None:
            raise ScopeError(
                "拒绝在生产 public 上 activate：维护闸不存在。\n"
                f"请先建 {MAINTENANCE_FLAG}（写明是谁、为什么、何时删）再重试。")
        if not maintenance_override:
            raise ScopeError(
                "拒绝在生产 public 上 activate：缺显式 maintenance override。\n"
                "闸存在只说明系统处于维护态，不代表这一次调用是有意的切换动作。")
        if not rebuild:
            raise ScopeError(
                "拒绝在生产 public 上不 rebuild 就推进世代：请加 --rebuild。\n"
                "只换世代标记不重建内容，会让 deleted / excluded / quarantine 的"
                "旧行残留在索引里，却被新世代背书——\n"
                "于是一份已经被隔离的文件照样能被检索到，且不会报错。")

    return _rebuild_generation(reg, conn, schema=schema, rebuild=rebuild)


def _incremental_index(reg, conn, schema="public"):
    """真增量：**只对新增/变化的分块做 embedding**，不重算全库。

    拿 activate(rebuild=False) 当增量用是错的——它无条件把全部分块重新
    向量化。1000 个分块的库每次自动触发都要跑几分钟 Ollama，
    而其中 999 个的向量与库里那份逐字节相同。

    四类清理缺一不可，都是「行不该再留着」：
      · 文件被删除            → 它的全部行成孤儿；
      · 文件正文变化          → 旧 content_hash 的行不再对应任何分块；
      · 文件移入 excluded     → 它从 scan 输出里消失；
      · 文件移入 quarantine   → 同上。
    后三类与「被删除」在 scan 输出上表现一致（都不再出现），所以同一条
    alive 判定覆盖得住。但后果不同：移入 excluded/quarantine 后若不删，
    残留行会继续被它**原来的** collection 命中，而那个 collection 可能仍是
    active——于是一份已经被隔离的文件照样能被检索到，且不报错。
    """
    schema = _safe_schema(schema)
    desired = _chunks_for(reg)
    alive_files = {rel for _cid, rel, _c, _h in desired}
    want = {(rel, h): cid for cid, rel, _c, h in desired}

    cur = conn.cursor()
    cur.execute(f"SELECT file_path, content_hash, collection_id"
                f" FROM {schema}.knowledge_index")
    existing = {(fp, h.strip()): cid for fp, h, cid in cur.fetchall()}
    conn.commit()          # 结束读事务，embedding 不留在事务里

    gone_files = sorted({fp for fp, _h in existing} - alive_files)
    stale_keys = [k for k in existing if k[0] in alive_files and k not in want]
    recat = [(k, want[k]) for k in existing if k in want and existing[k] != want[k]]
    new_keys = {k for k in want if k not in existing}

    # **只对新增的分块做 embedding**——这是本函数存在的全部意义。
    new_rows = [(want[(rel, h)], rel, c, h)
                for _cid, rel, c, h in desired if (rel, h) in new_keys]
    vecs = embed_batched([c for _cid, _rel, c, _h in new_rows]) if new_rows else []

    try:
        if gone_files:
            cur.execute(f"DELETE FROM {schema}.knowledge_index"
                        f" WHERE file_path = ANY(%s)", (gone_files,))
        for fp, h in stale_keys:
            cur.execute(f"DELETE FROM {schema}.knowledge_index"
                        f" WHERE file_path=%s AND content_hash=%s", (fp, h))
        for (fp, h), cid in recat:
            cur.execute(f"UPDATE {schema}.knowledge_index SET collection_id=%s"
                        f" WHERE file_path=%s AND content_hash=%s", (cid, fp, h))
        index_rows(conn, new_rows, vecs, schema=schema)
        _commit_generation(conn, reg.registry_hash, schema=schema)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"files": len(alive_files), "chunks": len(desired),
            "embedded": len(new_rows), "removed_files": len(gone_files),
            "stale_chunks": len(stale_keys), "recategorised": len(recat),
            "rebuilt": False, "skipped_links": len(desired.skipped),
            "registry_hash": reg.registry_hash}


def index_with_registry(reg, conn, schema="public"):
    """世代未变走真增量；**发现 registry 换代时自动升级为全量重建**。

    返回 stats（含 rebuilt 标志）。

    为什么换代要自动重建而不是报错让人手动 activate：配置变更可能由监听器
    自动触发索引，触发者是定时/后台任务，没有人在看输出。报错只会静默失败，
    而检索已经全线因世代不符而拒绝——故障与提示不在同一个人面前。
    自动换代把「改配置」的语义补完整：改完就生效，自行恢复。

    代价是一次配置改动触发全量重建。可接受：配置改动罕见，
    且归属变化本来就需要全量重算。世代未变时走真增量，不重算全库。

    **这里直接调 `_rebuild_generation`，不经 `activate`。** 经 activate 的话，
    它要求维护闸存在，而 `cmd_index` 在闸存在时早已拒绝执行——两个条件互斥，
    自动换代永远走不到。本函数**不接受 maintenance_override**：这条路径的
    授权来自「已持有 index flock 且闸不存在」，与显式切换不共用权限条件，
    免得 `index` 变成一条绕过维护闸纪律的旁路。
    """
    schema = _safe_schema(schema)
    active = read_active_hash(conn, schema=schema)     # 读完即结束事务
    if active != reg.registry_hash:
        return _rebuild_generation(reg, conn, schema=schema, rebuild=True)
    return _incremental_index(reg, conn, schema=schema)
