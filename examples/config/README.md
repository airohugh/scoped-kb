# 配置示例（只读参考，不要直接复制使用）

这两套文件不是能直接拷到配置目录里跑的"模板"——`fingerprint` 和绝对路径都是机器相关值，复制过去只会让 `scoped-kb registry --check` 报指纹不一致。它们的作用是让你在跑任何命令之前，先看懂配置长什么样、字段分别管什么。

真正要用的配置，单域场景直接跑 `scoped-kb init <笔记目录>` 生成；多域场景照着 `multi-domain/` 手写——注意配置目录里的实际布局：`central/` 下的两份文件直接放在配置目录顶层（与 `config.json` 并列），`repos/` 下的每份 `collections.json` 放进对应仓库、再把它们的路径写进 `config.json` 的 `repos`（示例里是相对路径，便于阅读）。写完用 `scoped-kb registry --fingerprints` 算出真实指纹再填进 `scope-policy.json`。

## single-domain/

对应 `scoped-kb init` 自动生成的三份文件：一个笔记目录、一个 collection（固定叫 `notes`）、一个 profile（固定叫 `default`）。单域用户不需要读这份示例就能用——`init` 全自动——但想知道它背后写了什么，可以对照这里。

## multi-domain/

进阶场景：两个独立的"仓库"（各自声明自己的 collection，图例叫 `cooking-repo` 和 `reading-repo`），加一个"本机来源"（图例叫 `inbox`，用 `confirmed_manifest` 策略——文件先落进隔离分区，人工确认归属后才能进对应 profile）。两个 profile 互相不能默认读到对方，要跨域得在 `cross_allowlist` 里显式登记组合，查询时还要带 `--profile cross --include <域> --why "原因"`。

**两层谁管什么**（`docs/design.md` 有更完整的理由）：
- **仓库声明**（`collections.json` / `machine-sources.json`）只回答"我这里有什么目录、分别是什么性质"——它说了不算，不能自己把自己提升成某个 profile 能读的东西。
- **中央策略**（`scope-policy.json`）回答"谁能读什么"——`profiles`、`cross_allowlist`、每个 collection 的 `scope`/`index_policy`/`fingerprint` 只能写在这里；仓库声明里出现这些字段，编译直接报错。
