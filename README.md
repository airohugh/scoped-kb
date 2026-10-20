# scoped-kb

A local knowledge base for markdown notes: chunked indexing, pgvector semantic search, and a two-layer access-scope model for keeping separate note collections separate when more than one agent or project reads from the same machine.

**English summary.** scoped-kb indexes `.md` files (split on `## ` headings, incremental, content-hash deduped) into Postgres+pgvector and searches them with pure vector similarity via an Ollama embedding model (default `bge-m3`). Provenance (`trust_tier` / `source_url` / `captured_at`) lives in each file's YAML frontmatter and is rendered at query time, never embedded. A generation gate refuses to serve search results if the index was built from a different configuration than the one currently in effect — avoiding silently wrong scope boundaries. `scoped-kb pack` assembles a budgeted, provenance-tagged context bundle for agents that cannot read your files, and refuses to deliver it if a credential scanner flags anything (report-only, never silently redacted; a clean scan is not proof the bundle is clean). For single-notebook use, `scoped-kb init <dir>` sets up a minimal single-domain configuration and you're done; a more advanced two-layer scope system (local declarations + central policy, explicit cross-domain allowlists) is available for running several isolated note collections on one machine. See [`docs/design.md`](docs/design.md) for the reasoning, and [Known limitations](#已知限制) below for what this does not do. This is a personal project with no guaranteed response time — see [SECURITY.md](SECURITY.md).

---

> **个人项目，不保证响应。** 有问题欢迎提 issue，但没有响应时限承诺。

## 这是什么

一个本地知识库核心：把一堆 markdown 笔记切块、向量化、存进 Postgres+pgvector，支持纯向量语义检索；同时提供一套可选的分域机制——当你需要在同一台机器上，让不同的 agent 或项目只读到各自该读的那部分笔记时用得上；只有一份笔记目录的话，`scoped-kb init` 会把这套机制隐藏到你完全不用关心。

适合谁：会用命令行、能跑 Docker（或自己装 Postgres+pgvector）、本地有成百上千篇 markdown 笔记、想要"检索结果带出处"而不是"一个黑箱答案"的人。

## 已知限制

- **检索只走向量，不做关键词混合**。短术语、型号、专有名词这类"精确匹配"查询会弱于关键词检索——这是设计取舍，不是未实现，理由见 [`docs/design.md`](docs/design.md)。
- **换 embedding 模型要全库重算**。向量维度或模型一变，库里旧向量和新查询向量不可比，`backfill`/`index` 不会帮你自动重算旧行，需要你清空重建；换的模型维度不是 1024 的话，还要先改 `schema.sql` 里的 `vector(1024)` 并重建表。
- **没有向量索引（HNSW/IVFFlat）**。几千个分块量级一次全表扫描足够快；超过大约一万个分块之后建议自己加索引（`schema.sql` 故意没建，避免在你还没到这个量级时就引入额外的维护成本）。
- **锁文件用 `fcntl.flock`，不支持 Windows**。本项目只在 macOS / Linux 上验证过。
- **真实 Ollama 链路没有在 CI 里跑过**——CI 跑 lint 和全部测试（DB 测试连 Postgres 服务容器），embedding 在 CI 里打桩。已实测的组合是「Postgres 走 `compose.yaml` 容器 + Ollama 走宿主机原生安装」；「两个都用容器」这条路径没有完整实测过（容器内 `ollama pull` 在有透明代理的网络下会失败，见下面"容器里拉模型失败怎么办"）。
- **`pack` 的凭据守卫只认有固定形态的东西**。能抓：带厂商前缀的 token（OpenAI / Anthropic 风格的 `sk-`、GitHub、Slack、Google API key、GitLab、Hugging Face、npm、Stripe、SendGrid、Telegram bot），字段名提示的赋值（`password=`、`apiKey:`、`DB_PASSWORD=`、`密码：`、markdown 表格里字段与值相邻、行内代码或引号包着的值、YAML/JSON 里换到下一行、缩进写的单个值，值至少 8 位），命令行参数（`--password <值>`；`--no-` 开头的否定选项和紧跟着另一个选项的不算），`Authorization: Bearer/Basic/Token` 头，连接串里的密码，私钥头，JWT，AWS key ID，40 位以上十六进制。
  - **形态扫描原理上抓不到的**：没有字段名、厂商前缀或上下文提示的裸密码（笔记里随手写了一个词，是不是密码只有你知道）；被拆开、编码、变换过的凭据。
  - **规则有意放过的**（为了少误报）：短于 8 位的值；看起来像占位符、代码引用、路径、文档链接或普通词的值（`${VAR}`、`${VAR:?提示}`（`${VAR:-默认值}` 只检查默认值）、`<your-token>`、`os.environ[...]`、`settings.API_KEY`、`~/.ssh/id_ed25519`、15 位以内的纯小写字母、`your_api_key_here` 这类用 `_`/`-` 连起来的小写词、`REPLACE_ME` 这类带下划线的大写词、引号里一句全是字母的普通英文）；字段名本身说明不是凭据的（`password_policy`、`token_type`、`api_key_file` 等以 policy / type / url / file / path / name / id 等结尾的）。`Authorization` 头只排除 `YOUR_API_KEY`、`your_api_key_here` 这类占位词、打码值和 `process.env.API_KEY` 这类代码引用。用小写字母加点号写成的值（如 `abc123.def456`）会被当成对象属性引用放过；值里有中文等非 ASCII 字符、又没有 8 位以上连续 ASCII 字符的，当成说明文字放过。
  - **会误报的**：完整的 git 提交号和 SHA-256 校验值（40 位以上十六进制，和十六进制编码的密钥长得一样，宁可拦下来让你确认）；字段名带关键词、值又像随机串的非凭据字段。
  - 扫描零命中不等于这一包干净，粘出去之前自己再看一眼。
- **分域隔离防的是配置与归属错误，不防拿到笔记目录写权限的本地对手**。详见 [`docs/design.md`](docs/design.md)「威胁模型边界」一节，以及 `chunking.scan` 的 docstring。

## 依赖

| 名称 | 用途 | 许可证 | 需要你自己装吗 |
|---|---|---|---|
| [PostgreSQL](https://www.postgresql.org/) | 存储分块与向量 | PostgreSQL License | 是（或用下面的 `compose.yaml` 跑容器，镜像钉的是 17；更低版本没有实测过，理论上支持 pgvector 的版本都能用） |
| [pgvector](https://github.com/pgvector/pgvector) | Postgres 的向量类型与索引扩展 | PostgreSQL License | 同上（`compose.yaml` 用官方 `pgvector/pgvector` 镜像，已经装好这个扩展） |
| [Ollama](https://ollama.com/) | 跑本地 embedding 模型 | MIT | 是（或用 `compose.yaml` 里的容器） |
| [bge-m3](https://ollama.com/library/bge-m3) | 默认 embedding 模型，1024 维 | MIT | `ollama pull bge-m3`，本仓不打包任何模型权重 |
| `psycopg2-binary` | Python 连接 Postgres | LGPL-3.0（带静态链接例外） | 否——`pip install` 自动装 |
| `platformdirs` | 按平台约定找配置/状态目录 | MIT | 否——`pip install` 自动装 |

本仓不镶嵌、不镜像、不改造任何上面列出的软件，只声明依赖关系；专有或有使用条款的部分（这里没有，但如果你接插件时引入了）只链接官方页面，不随仓分发。

## 快速开始

下面这条路径在一台临时 `$HOME`、隔离的 Postgres 容器下走过一遍，不依赖本机已有的任何配置文件（Ollama 部分走的是宿主机原生安装，不是下面第 1 步写的容器方式——原因和局限见"已知限制"与"容器里拉模型失败怎么办"）。

```bash
git clone https://github.com/airohugh/scoped-kb.git
cd scoped-kb

# 1. 起 Postgres+pgvector 和 Ollama（容器方式；容器起库时会自动加载 schema.sql）
#    也可以用你自己装的 Postgres（需装好 pgvector 扩展）+ Ollama 跳过这步，但要先手动建表：
#    psql "$SCOPED_KB_DATABASE_URL" -f schema.sql
docker compose up -d
docker compose exec ollama ollama pull bge-m3

# 2. 装这个包（可编辑安装，改代码不用重装）
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# 3. 告诉它数据库在哪
export SCOPED_KB_DATABASE_URL="postgresql://scoped_kb:scoped_kb_dev@127.0.0.1:5432/scoped_kb"

# 4. 给一个笔记目录生成单域配置（随便指一个全是 .md 文件的目录；先试试本仓自带的示例笔记也行）
scoped-kb init examples/notes

# 5. 建索引（首次全量；以后改了笔记内容再跑这条，只会增量处理变化的部分）
scoped-kb index

# 6. 检索
scoped-kb search "酸种发酵过头了怎么办"
```

`examples/notes/` 里的笔记、来源机构、链接和数字全部是虚构的演示语料，`trust_tier` 只是演示格式，不代表任何真实来源被核实过。

第 6 步应该能看到类似这样的结果（下面是实测截取，相似度分数和排序会随 embedding 模型版本小幅波动）——每条结果带一行"出处"，告诉你这段内容是自有产出还是外部采集、可信度标到哪一级；结果正文第一行是文件名（切块时固定前置，方便肉眼核对来源），第二行是该分块的摘要：

```
1. [0.6706] (notes) /.../examples/notes/sourdough-starter.md
   出处：自有产出·笔记
   sourdough-starter
   sourdough-starter ## 判断酸种是否健康的几个土办法 闻味道：健康的酸种闻起来是酸奶加一点点酒味，不应该有刺鼻的指甲油味…
```

带 `trust_tier` 标注的结果，出处行会多出可信度标签，比如外部采集的一条会显示 `出处：自有产出·笔记 · T1 官方/一手 · 采集 2026-01-10 · https://example.com/...`——"自有产出·笔记"是这个 collection 的归属标签（`scoped-kb init` 固定写成 `note`），`T1 官方/一手`是这个文件自己 frontmatter 里标的可信度，两者是独立的两件事，见 [`docs/design.md`](docs/design.md)。

### `docker compose up` 报 "Is a directory" 或容器起不来

如果你用 colima（而不是 Docker Desktop）跑 Docker，并且把这个仓库 clone 在 `/tmp` 之类 colima 默认不挂载的路径下，`./schema.sql:/docker-entrypoint-initdb.d/schema.sql` 这条 bind mount 在虚拟机里会看到一个空目录而不是这个文件——Docker 发现挂载源"不存在"时会自动在容器里创建一个同名目录，initdb 跑到这一步会报 `could not read from input file: Is a directory`，容器随即退出。解法是把仓库 clone 在 colima 能看见的路径下（默认挂载 `$HOME` 和 `/tmp/colima`，具体以 `colima status`/配置为准），不要用系统 `/tmp` 下的任意临时目录。

### 容器里拉模型失败怎么办

`docker compose exec ollama ollama pull bge-m3` 如果报类似 "redirect target not allowed" 或解析到一个奇怪的内网地址的错误，通常是本机的透明代理（Clash/mihomo 之类用 fake-ip 方案的代理）把容器内的 DNS 解析劫持到了一个不可路由的地址，跟这个项目本身无关。两个绕过办法：让容器所在的虚拟机网络不经过那个代理；或者干脆不用容器里的 Ollama，在宿主机上装一个（`ollama pull bge-m3` 原生跑一般没有这个问题）。注意第 1 步起的 Ollama 容器占着宿主机的 `11434` 端口，拉模型失败并不会释放它：先 `docker compose stop ollama`，再启动宿主机的 Ollama 并 `ollama pull bge-m3`；以后只用 `docker compose up -d db` 起数据库，别再把 Ollama 容器拉起来。宿主机 Ollama 的默认端口就是 `11434`，`SCOPED_KB_OLLAMA_URL` 不用改。

### 本机已有 Postgres 怎么办

`compose.yaml` 默认把容器的 5432 映射到宿主机 `127.0.0.1:5432`——如果你本机已经在用 Postgres（装过 Postgres.app、或者已经跑着别的服务），这会端口冲突。用环境变量换一个端口：

```bash
SCOPED_KB_PG_PORT=55432 docker compose up -d
export SCOPED_KB_DATABASE_URL="postgresql://scoped_kb:scoped_kb_dev@127.0.0.1:55432/scoped_kb"
```

## 配置

`scoped-kb init <笔记目录>` 会把下面三份文件写进配置目录（按平台约定自动选目录——macOS 是 `~/Library/Application Support/scoped-kb/`，Linux 是 `~/.config/scoped-kb/`；也可以用 `SCOPED_KB_CONFIG_DIR` 指定别处），单域用户不需要手改它们。想了解里面写了什么、或者要手配多域场景，看 [`examples/config/`](examples/config/)。

| 文件 | 内容 |
|---|---|
| `machine-sources.json` | 本机笔记目录声明（哪个目录是哪个 collection） |
| `scope-policy.json` | 谁能读什么：profile、跨域白名单、每个 collection 的访问策略 |
| `config.json` | 仓库声明路径列表（多域时用）、要跳过的文件名、出处字段白名单、追溯 ID 读哪个环境变量 |

改了这三份文件（或多域场景下它们引用的仓库声明）之后，记得跑一次 `scoped-kb index`——检测到配置变化（`registry_hash` 变了）会自动触发全量重建，不需要手动区分"这次是不是要重建"。例外：`config.json` 里的 `provenance_fields`（出处行显示哪些字段）和 `trace`（可追溯 ID 读哪些环境变量）只影响输出与留痕，不进 `registry_hash`，改了立即生效、不会触发重建；跑一次 `index` 也无妨，只是增量。

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `SCOPED_KB_DATABASE_URL` | （必须设置） | Postgres 连接串，libpq URI 或键值对格式均可 |
| `SCOPED_KB_CONFIG_DIR` | 平台约定的用户配置目录 | 三份配置文件所在目录 |
| `SCOPED_KB_STATE_DIR` | 平台约定的用户状态目录 | 索引锁、维护闸、跨域留痕日志所在目录 |
| `SCOPED_KB_AGENT` | `scoped-kb` | 写入 `source_agent` 列，用来区分是谁写进来的；多个 agent 共用一个库时各自设成自己的名字 |
| `SCOPED_KB_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama 服务地址 |
| `SCOPED_KB_EMBED_MODEL` | `bge-m3` | embedding 模型名 |
| `SCOPED_KB_EMBED_DIM` | `1024` | 期望的向量维度，模型返回的维度不符会在写库前直接拒绝 |
| `SCOPED_KB_EMBED_BATCH` | `32` | 一次向量化请求打包多少条文本 |
| `SCOPED_KB_TASK_ID` / `SCOPED_KB_SESSION_ID` | （无） | 跨域检索的可追溯标识来源，变量名本身可在 `config.json` 的 `trace` 字段里改 |
| `SCOPED_KB_PG_PORT` | `5432` | 仅 `compose.yaml` 用：容器映射到宿主机的端口 |

## 命令参考

```
scoped-kb init <笔记目录> [--force]   # 生成单域配置（首次使用；--force 覆盖已有配置）
scoped-kb index                        # 增量索引；检测到配置变化会自动全量重建
scoped-kb search "<问题>" [--limit N] [--json]
                                        # 检索；--json 只输出机器可读 JSON（供脚本/钩子解析）
scoped-kb pack "<任务描述>" [--limit N] [--budget 字数] [--out 文件] [--force]
                                        # 按任务打包一份可粘贴给外部 agent 的上下文（带出处、带凭据守卫）
scoped-kb backfill                     # 补算 embedding 为 NULL 的行（比如换过一次嵌入模型后清空重算）
scoped-kb status                       # 看索引总共多少文件/分块/已向量化的行，并按 collection 列出分块数
scoped-kb registry --check             # 只编译校验配置并打印 hash，不连库、不写文件
scoped-kb registry --dry-run           # 全量分类报告：哪些文件会进索引/被隔离/被排除/没归属
scoped-kb registry --fingerprints      # 打印各 collection 的规范化声明指纹，填进 scope-policy.json
scoped-kb registry --activate [--rebuild] [--maintenance-override] [--schema S]
                                        # 显式提交索引世代；对 public 需维护闸存在且同时带 --rebuild 与 --maintenance-override，日常用 index 即可
```

### 给不能读文件的 agent 打包：`pack`

网页版或桌面版的 agent 读不到你的笔记目录，只能看到你粘进去的东西。`pack` 按任务检索，挑出一包片段：每个文件先取最相关的一块，预算有剩再补同一文件的其他块，这样覆盖的来源更多。每个片段带一行出处，包头有出处图例。片段标题用位置键（`<仓库 id>/<相对路径>`），不带本机绝对路径；片段正文原样照搬，正文里自己写着的路径不会被改写。

```bash
scoped-kb pack "我要改酸种配方，先看看以前记过什么" --out ctx.md   # 写文件
scoped-kb pack "..." | pbcopy                                       # 或直接进剪贴板（stdout 上只有这一包，横幅和提示都走 stderr）
```

交付前整包过一遍凭据守卫。检出疑似凭据就中止：不写文件、不打印包，只在 stderr 列出命中的行号、所在片段、规则类别和掩码（报告里不回显凭据原文，也不回显字段名和文件路径——文件名里同样可能写着密钥，所以只给片段序号）。要看是哪个文件，用 `--force --out <本机文件>` 写到本机，打开看对应的片段；只在本机看，确认前别粘出去。**守卫只报不改**，不会替你脱敏：被悄悄改过的包看起来和没改过的一样，人会以为安全了。先回源文件看清楚，确认是误报（示例值、变量名引用）再加 `--force` 放行。能抓什么、会漏什么，见上面「已知限制」。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 成功，或没有命中（没有命中不算错误） |
| 1 | 配置编译失败等其它运行错误 |
| 2 | 参数用法错误（argparse） |
| 3 | 作用域或世代校验失败（`search` 同样用这个码） |
| 4 | 凭据守卫中止 |

### 多域与跨域（进阶）

单域用户用不到这一节。如果你要在同一台机器上维护几个互不默认可读的笔记域（比如工作笔记和个人阅读笔记各自归一个 agent），照 [`examples/config/multi-domain/`](examples/config/multi-domain/) 的结构手写配置，写完用 `scoped-kb registry --fingerprints` 算出真实指纹填进 `scope-policy.json`。

跨域查询需要显式声明，且目标组合必须出现在中央 `scope-policy.json` 的 `cross_allowlist` 里：

```bash
scoped-kb search "问题" --profile cross --include <另一个域> --why "为什么需要跨域"
scoped-kb pack "任务" --profile cross --include <另一个域> --why "为什么需要跨域"
```

这会要求环境里存在可追溯的任务/会话 ID（变量名见上面环境变量表），缺失就拒绝执行而不是留空记录。为什么要这么麻烦，见 [`docs/design.md`](docs/design.md) 的"为什么分两层 scope"一节。

## 插件

本仓不提供插件运行时。"接一个新的内容来源"约定成"写一个把内容转成规定格式 `.md` 文件的脚本"，契约和一个范例（抖音收藏，仅思路、无可运行代码）在 [`plugins/README.md`](plugins/README.md)。

## 开发 / 测试

```bash
pip install -e ".[test]" ruff

# 起一个专用的测试库（和开发/生产用的库必须是不同的库，测试会 TRUNCATE 数据）。
# 单独的 compose 工程名 = 单独的数据卷，不会和快速开始那个库共用数据
COMPOSE_PROJECT_NAME=scoped-kb-test SCOPED_KB_PG_PORT=55432 docker compose up -d db
export SCOPED_KB_TEST_DATABASE_URL="postgresql://scoped_kb:scoped_kb_dev@127.0.0.1:55432/scoped_kb"

python3 -m pytest                          # 不连库的用例全跑；DB 相关用例在没配测试库时会 skip
python3 -m pytest --require-canary         # 验收模式：测试库没配置好就直接 fail，不悄悄 skip
ruff check src tests                       # lint（用的是 ruff 默认规则集，仓库没有自定义配置）
```

测试库的安全闸：连上去之后如果发现 `public.knowledge_index` 表已经有数据，测试会直接失败退出，不会往下执行任何 `TRUNCATE`——这是为了防止手滑把测试指向一个正在使用的真实知识库。

## License

MIT, see [LICENSE](LICENSE).
