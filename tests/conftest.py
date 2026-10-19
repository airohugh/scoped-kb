"""registry 测试的共享夹具。

三个注入点：
  1. registry —— compile_registry(config_paths)
  2. schema   —— retrieve_candidates(..., schema=) / index_into(..., schema=)
  3. 连接     —— 所有 DB 函数接受外部传入的 conn

除此之外不得再有第四个注入面；断言必须直接引用生产符号本身。

## 关于 __FIXTURE_BASE__

committed 的 fixture JSON 里，root 写成 `__FIXTURE_BASE__/...`。
`fixture_paths` 会把它实例化到 tmp_path 并替换成真实绝对路径。

两个理由：
  1. 仓库里不能写死某台机器的绝对路径；
  2. manifest 悬空校验要 stat 真实文件，所以 fixture 的 root 下必须有真文件
     （tests/fixtures/ok/ 里备好），不能指向 /tmp 下一个不存在的目录。

这是**测试夹具的实例化**，不是生产行为——生产配置里写的就是真实绝对路径。
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import psycopg2
import pytest

from scoped_kb import settings

HERE = Path(__file__).parent
SCHEMA_SQL = HERE.parent / "schema.sql"

CANARY_SCHEMA = "canary"
FIXTURE_TOKEN = "__FIXTURE_BASE__"


def _materialize(src_dir, dst_dir, base):
    """把 fixture 配置复制到 dst_dir，替换 __FIXTURE_BASE__，并注入真实指纹。

    指纹为什么在这里算、而不是写死在 committed fixture 里：
    machine adapter 的 root 是绝对路径（替换后含本机用户目录），
    写死的指纹会变成机器相关值，换台机器跑测试必然全红。
    committed 文件里写 `__AUTO__`，实例化时由生产的 print_fingerprints 填。

    这不削弱漂移测试：make_paths 的 patch 在本函数**之后**施加，
    改了 roots/excludes 就与已注入的指纹对不上，drift 照样被抓。
    """
    from scoped_kb import registry

    dst_dir.mkdir(parents=True, exist_ok=True)
    out = {"repos": []}
    for name, key in (("repo_a/collections.json", None),
                      ("repo_b/collections.json", None),
                      ("central/machine-sources.json", "machine_sources"),
                      ("central/scope-policy.json", "scope_policy")):
        src = src_dir / name
        dst = dst_dir / name.replace("/", "__")
        dst.write_text(
            src.read_text(encoding="utf-8").replace(FIXTURE_TOKEN, str(base)),
            encoding="utf-8")
        if key:
            out[key] = dst
        else:
            out["repos"].append(dst)

    pol_path = Path(out["scope_policy"])
    pol = json.loads(pol_path.read_text(encoding="utf-8"))
    for cid, fp in registry.print_fingerprints(out).items():
        if cid in pol["collections"] and pol["collections"][cid]["fingerprint"] == "__AUTO__":
            pol["collections"][cid]["fingerprint"] = fp
    pol_path.write_text(json.dumps(pol, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def materialize_layout(src_dir, dst_dir, base, layout):
    """通用实例化：layout 是 [(相对源路径, config_paths 的键或 None)]。

    `_materialize` 写死了 ok/ 夹具的四个文件名；canary 夹具是另一套布局
    （单个仓库声明加一套中央策略与 machine-sources），需要同一套替换与指纹注入逻辑，
    所以抽出来共用——**不是另写一份等价物**。
    """
    from scoped_kb import registry

    dst_dir.mkdir(parents=True, exist_ok=True)
    out = {"repos": []}
    for name, key in layout:
        dst = dst_dir / name.replace("/", "__")
        dst.write_text(
            (src_dir / name).read_text(encoding="utf-8").replace(FIXTURE_TOKEN, str(base)),
            encoding="utf-8")
        if key:
            out[key] = dst
        else:
            out["repos"].append(dst)

    pol_path = Path(out["scope_policy"])
    pol = json.loads(pol_path.read_text(encoding="utf-8"))
    for cid, fp in registry.print_fingerprints(out).items():
        if cid in pol["collections"] and pol["collections"][cid]["fingerprint"] == "__AUTO__":
            pol["collections"][cid]["fingerprint"] = fp
    pol_path.write_text(json.dumps(pol, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def attribute_notes_dir(reg, base, *, default, kinds, assignments=None):
    """给 registry 补一个 owner "t"（基准目录 base）并让 base 下的文件有真实的 collection 归属。

    出处回读会核对文件真实路径当前归属的 collection，所以只补 owner 不够：base 要是一个
    confirmed_manifest 的 root，文件按 assignments（文件名 → cid）分到各分区，
    没列出的落 default。kinds 是 {分区 cid: kind_default}，分区都是 active、无 root。
    走的是生产的 collection_for_path，不是另写一套归属规则。
    分区排在原有 collection 之前，方便用例按 kind_default 取第一个。
    """
    import dataclasses
    template = next(iter(reg.collections.values()))
    parts = {cid: dataclasses.replace(
        template, collection_id=cid, roots=(), excludes=(), kind_default=kind,
        index_policy="active", location_owner="t") for cid, kind in kinds.items()}
    host = dataclasses.replace(
        template, collection_id="t-base", roots=(base,), excludes=(), kind_default="note",
        index_policy="confirmed_manifest", location_owner="t")
    return dataclasses.replace(
        reg,
        collections={**parts, **reg.collections, "t-base": host},
        manifest={**reg.manifest, "t-base": dict(assignments or {})},
        manifest_default={**reg.manifest_default, "t-base": default},
        location_bases={**reg.location_bases, "t": base})


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    """所有用例默认指向一个空配置目录，不读开发者本机真实的用户配置。"""
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "_empty_config_dir")


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """状态目录同样默认指向 tmp_path：跨域留痕会写这里，不能落进开发者本机的真实状态目录。"""
    monkeypatch.setattr(settings, "STATE_DIR", tmp_path / "_state_dir")


@pytest.fixture
def fixture_base():
    """committed fixture 语料的真实根目录（含 repo_a/ repo_b/ machine/ 下的真文件）。"""
    return HERE / "fixtures" / "ok"


@pytest.fixture
def fixture_paths(tmp_path, fixture_base):
    """注入点 1：实例化后的 config_paths dict，root 指向真实存在的 fixture 目录。"""
    return _materialize(fixture_base, tmp_path / "cfg0", fixture_base)


@pytest.fixture
def make_paths(tmp_path, fixture_base):
    """注入点 1b：实例化一份可原地改写的配置，返回新的 config_paths。

    **测试制造配置漂移必须走这里**——不得用 object.__setattr__ 去改 frozen dataclass
    的 registry_hash：那绕过了编译器，测的是断言自己，不是真实漂移路径。

    patch 的键与 config_paths 同名；`repos` 的 patch 作用于第 0 份仓库声明。
    可用 base= 覆盖 __FIXTURE_BASE__ 的替换目标（用于制造悬空/越界场景）。
    """
    counter = {"n": 0}

    def _make(base=None, **patches):
        counter["n"] += 1
        out = _materialize(fixture_base, tmp_path / f"cfg{counter['n']}",
                           base or fixture_base)
        for key, mutate in patches.items():
            target = Path(out["repos"][0]) if key == "repos" else Path(out[key])
            data = json.loads(target.read_text(encoding="utf-8"))
            mutate(data)
            target.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return out

    return _make


@pytest.fixture
def corpus(tmp_path, fixture_base, make_paths):
    """committed 夹具语料的**可写副本**，放在 tmp_path 下（不在 $HOME 里）。

    需要增删改文件的用例一律用它，不得往 tests/fixtures/ 里写。
    root 是已 resolve 的副本根；paths(**patches) 产出指向副本的 config_paths，
    patches 与 make_paths 相同。
    """
    root = tmp_path / "kb"
    shutil.copytree(fixture_base, root)
    root = root.resolve()
    return SimpleNamespace(
        root=root, paths=lambda **patches: make_paths(base=root, **patches))


def text_vector(text, dim=None):
    """确定性假向量：同一段文本永远得到同一个向量，且全部分量非零（余弦相似度有定义）。"""
    dim = dim or settings.DIM
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(digest[i % len(digest)] % 17 + 1) / 18.0 for i in range(dim)]


@pytest.fixture
def fake_embed(monkeypatch):
    """把 embedding 换成确定性假向量，**不连任何 embedding 服务**。

    indexer 与 search 各自 import 了 embed，所以两处都要打桩。
    返回调用记录：每次调用传入的文本列表。
    """
    from scoped_kb import indexer, search
    calls = []

    def _embed(texts, **_kwargs):
        calls.append(list(texts))
        return [text_vector(t) for t in texts]

    monkeypatch.setattr(indexer, "embed", _embed)
    monkeypatch.setattr(search, "embed", _embed)
    return calls


@pytest.fixture
def trace_vars():
    """测试用的可追溯标识变量名，**故意不是**缺省名，证明变量名确实来自配置。

    config 可直接作为 install_config 的 config 参数（写进 config.json 的 trace 段）；
    set() 把两个 ID 设进当前进程环境。
    """
    names = SimpleNamespace(
        task="KB_TEST_TASK_ID",
        sessions=("KB_TEST_SESSION_ID", "KB_TEST_RUN_ID"),
    )
    names.config = {"trace": {"task_id_env": names.task,
                              "session_id_env": list(names.sessions)}}
    return names


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """把维护闸指到 tmp_path，返回一个 (on/off) 开关。**绝不碰真实的闸文件。**

    MAINTENANCE_FLAG 是 indexer 模块 import 时算出来的常量，所以只能 patch 它的属性。
    """
    from scoped_kb import indexer
    flag = tmp_path / "index.maintenance"
    monkeypatch.setattr(indexer, "MAINTENANCE_FLAG", flag)

    def _set(reason=None):
        if reason is None:
            flag.unlink(missing_ok=True)
        else:
            flag.write_text(reason, encoding="utf-8")
        return flag

    return _set


@pytest.fixture
def install_config(make_paths, tmp_path, monkeypatch):
    """把夹具配置装成**真实的配置目录**，并把 settings.CONFIG_DIR 指过去。

    CLI 入口（cmd_search 等）裸调 compile_registry()，走的是 default_paths()，
    只认配置目录里的三份文件；make_paths 产出的是带前缀的散文件，不能直接用。
    指纹只依赖 roots，搬文件不影响。

    config  —— 合并进 config.json 的额外字段（如 trace）
    base    —— 语料根目录，缺省是 committed 夹具；要改动文件时传 corpus.root
    其余关键字 —— 原样交给 make_paths，作为 scope_policy / machine_sources 的 patch
    返回配置目录。
    """
    counter = {"n": 0}

    def _install(config=None, base=None, **patches):
        counter["n"] += 1
        paths = make_paths(base=base, **patches)
        d = tmp_path / f"installed_config{counter['n']}"
        d.mkdir()
        shutil.copy(paths["machine_sources"], d / "machine-sources.json")
        shutil.copy(paths["scope_policy"], d / "scope-policy.json")
        cfg = {"schema_version": 1, "repos": [str(r) for r in paths["repos"]]}
        cfg.update(config or {})
        (d / "config.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(settings, "CONFIG_DIR", d)
        return d

    return _install


CANARY_TABLES = ("knowledge_index", "knowledge_index_meta")


# canary schema 是**跨进程共享**的单一资源：两个 pytest 同时跑会各自
# TRUNCATE、各自写入，轻则互相把对方的行清掉（让 forbidden 计数偶然成立），
# 重则两边的 TRUNCATE 互等对方的表锁而死锁。用一把咨询锁把整个会话串起来。
# 数字是任取的常量，只要两边一致即可。
CANARY_ADVISORY_LOCK = 0x7CA9A_11


REQUIRE_CANARY_ENV = "SCOPED_KB_REQUIRE_CANARY"

# 测试库连接串。**独立于运行时的 SCOPED_KB_DATABASE_URL，也不回退到它**——
# 测试会 TRUNCATE，绝不能因为忘了设测试库变量就打到正在使用的知识库上。
TEST_DATABASE_URL_ENV = "SCOPED_KB_TEST_DATABASE_URL"


def pytest_addoption(parser):
    parser.addoption(
        "--require-canary", action="store_true", default=False,
        help="最终验收模式：测试库未配置或 canary schema 不可用时**失败**而不是 skip")


def canary_is_required(config=None):
    """两条开关任一打开即生效：命令行 `--require-canary` 或环境变量。

    留环境变量这条是给脚本用的——验收脚本里 `pytest` 可能被
    包在别的命令后面，加不上命令行参数。
    """
    if os.environ.get(REQUIRE_CANARY_ENV, "").strip() not in ("", "0", "false"):
        return True
    return bool(config and config.getoption("--require-canary"))


def _canary_missing(reason):
    """按模式决定是 skip 还是 fail。

    **开发期 skip、最终验收 fail，两者不能是同一个行为。**
    一直 skip 的话，忘记配测试库会得到一整套绿色的套件——
    而碰撞 canary、跨域对抗检索这类用例其实一条都没跑。
    绿色本身成了错误信号，这比红色危险得多。
    """
    if canary_is_required():
        pytest.fail(f"[require-canary] {reason}\n"
                    f"最终验收模式下测试库必须就绪：设置 {TEST_DATABASE_URL_ENV} "
                    f"指向一个专用的空库。")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def canary_conn(request):
    """注入点 3：测试全程打测试库里的 canary schema，绝不碰 public 表。

    连接串只认 SCOPED_KB_TEST_DATABASE_URL。测试角色就是测试库的 owner，
    所以 canary schema 由本夹具自己创建，表结构直接加载仓根的 schema.sql——
    测的永远是随仓发布的那份 DDL。

    **安全闸**：若 public.knowledge_index 已有数据，说明这不是专用测试库，
    立即失败，一行都不写。

    search_path 必须带 public：vector 类型装在 public，只写 canary 会报
    `type "vector" does not exist`。

    **取跨进程 advisory lock 后才动 schema**：并发的第二个 pytest 会在这里阻塞
    等待，而不是和第一个抢同一批表——串行等待是对的，死锁不是。
    """
    if canary_is_required(request.config):
        os.environ[REQUIRE_CANARY_ENV] = "1"      # 传给子进程里的 canary 用例
    dsn = os.environ.get(TEST_DATABASE_URL_ENV)
    if not dsn:
        _canary_missing(f"未设置 {TEST_DATABASE_URL_ENV}——需要一个专用的空测试库")
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute("SELECT to_regclass('public.knowledge_index')")
    if cur.fetchone()[0] is not None:
        cur.execute("SELECT count(*) FROM public.knowledge_index")
        if cur.fetchone()[0] > 0:
            conn.close()
            pytest.fail("测试库必须是空库，不能指向正在使用的知识库："
                        f"{TEST_DATABASE_URL_ENV} 指向的库里 public.knowledge_index 已有数据。")
    conn.commit()
    cur.execute("SELECT pg_advisory_lock(%s)", (CANARY_ADVISORY_LOCK,))
    conn.commit()
    try:
        seed_canary_schema(conn)
        yield conn
    finally:
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_unlock(%s)", (CANARY_ADVISORY_LOCK,))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def canary_clean(request):
    """每个用例前后都清空 canary 表。

    不清的两个后果：上一个用例的残留会让「forbidden 命中数为 0」偶然成立或
    偶然失败，而 canary 正是硬门槛；canary 数据还会一直堆在测试库里。

    **清理失败必须 rollback。** psycopg2 在一条语句报错后会把连接置为
    aborted，后续任何语句一律 `current transaction is aborted`——一个用例的
    清理失败会把**其后所有**用例连累成同一个看不懂的错误。
    """
    if "canary_conn" not in request.fixturenames:
        yield
        return
    conn = request.getfixturevalue("canary_conn")

    def _wipe():
        try:
            cur = conn.cursor()
            for t in CANARY_TABLES:
                cur.execute("SELECT to_regclass(%s)", (f"canary.{t}",))
                if cur.fetchone()[0] is not None:
                    cur.execute(f"TRUNCATE TABLE canary.{t}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    _wipe()
    yield
    _wipe()


def seed_canary_schema(conn):
    """在 canary schema 里加载仓根的 schema.sql（幂等）。

    **表结构必须就是随仓发布的 DDL**，不得手写简化版——否则测的就不是生产 SQL。
    schema.sql 全是 IF NOT EXISTS，且表名不带 schema 前缀，所以把 search_path
    首位设成 canary 即可让它建在 canary 里。

    先在 public 里确保 vector 扩展存在：schema.sql 自带的 CREATE EXTENSION
    在扩展已装时是空操作，但若没装，search_path 首位是 canary，扩展就会
    装进 canary 而不是 public，vector 类型从此对其它 schema 不可见。
    """
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS canary")
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    cur.execute("SET search_path TO canary, public")
    cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    conn.commit()


def set_active_hash(conn, active_hash):
    seed_canary_schema(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO canary.knowledge_index_meta (id, active_registry_hash)
        VALUES (true, %s)
        ON CONFLICT (id) DO UPDATE
          SET active_registry_hash = EXCLUDED.active_registry_hash""",
        (active_hash,))
    conn.commit()


CANARY_LAYOUT = (
    ("collections.json", None),
    ("central/machine-sources.json", "machine_sources"),
    ("central/scope-policy.json", "scope_policy"),
)


@pytest.fixture
def canary_registry(tmp_path):
    """碰撞 canary registry。

    **走生产的 compile_registry**——注入的是配置路径，不是 Registry 对象。
    换句话说 canary 用的编译器、校验规则、指纹逻辑与生产完全同一份。
    """
    from scoped_kb import registry
    base = HERE / "fixtures" / "canary"
    paths = materialize_layout(base, tmp_path / "canarycfg", base, CANARY_LAYOUT)
    return registry.compile_registry(paths), paths
