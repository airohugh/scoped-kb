# 上手清单

> **English summary.** A checklist of what you need before installing scoped-kb (macOS/Linux, Python 3.9+, Docker, a host-installed Ollama) and the most reliable install path: Postgres in a container, Ollama on the host. The README explains each step in detail.

这份清单只写**你要自己动手的部分**：装软件、起服务、换成自己的笔记、卡住时怎么提问。每条命令为什么这样写，[README](../README.md) 里都有解释；这里只给一条最稳的路。

**推荐路线：数据库用容器跑，Ollama 装在本机。** README 里实测跑通的就是这个组合。另一条「Ollama 也用容器」的路，在开着 Clash 这类代理的网络下，拉模型那一步会失败。

## 开始之前准备好

| 项 | 要求 | 怎么确认 |
|---|---|---|
| 电脑 | macOS 或 Linux。**不支持 Windows** | — |
| Python | 3.9 或更新 | 终端跑 `python3 --version` |
| Docker | Docker Desktop、OrbStack、colima 任选一个 | `docker compose version` 能打出版本号 |
| Ollama | 从 <https://ollama.com> 下载，装在本机，不装在容器里 | `ollama --version` |
| 磁盘 | 留几 GB 空闲：embedding 模型约 1.2 GB，另外还有数据库镜像 | — |
| 网络 | 终端能访问 github.com、Docker Hub、ollama.com 的模型库 | 下面第 1 步能拉下模型就算通 |
| 笔记 | 一个放满 `.md` 文件的文件夹。没有也行，先用仓库自带的示例 | — |

## 动手步骤

1. **拉 embedding 模型**（在本机 Ollama 里拉，不进容器）：

   ```bash
   ollama pull bge-m3
   ```

2. **拿到仓库，起数据库**（只起数据库，不起容器里的 Ollama）：

   ```bash
   git clone https://github.com/airohugh/scoped-kb.git ~/scoped-kb
   cd ~/scoped-kb
   docker compose up -d db
   ```

   ⚠️ 如果本机已经在跑 Postgres（比如装过 Postgres.app），5432 端口会冲突。改用下面这行起库，第 4 步的地址也要换成 `55432`：

   ```bash
   SCOPED_KB_PG_PORT=55432 docker compose up -d db
   ```

   ⚠️ 用 colima 的，仓库别 clone 到 `/tmp` 下面，否则建表脚本挂不进容器。放在 `~` 下面就没事。

3. **装这个工具**：

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```

4. **告诉它数据库在哪**：

   ```bash
   export SCOPED_KB_DATABASE_URL="postgresql://scoped_kb:scoped_kb_dev@127.0.0.1:5432/scoped_kb"
   ```

   想以后不用每次都输，把这行加进 `~/.zshrc`（Linux 一般是 `~/.bashrc`）。

5. **先用示例笔记跑通一遍**：

   ```bash
   scoped-kb init examples/notes
   scoped-kb index
   scoped-kb search "酸种发酵过头了怎么办"
   ```

   第一条结果通常是 `sourdough-starter.md`（排序会随模型版本小幅波动），下面带一行「出处」，就说明装好了。示例里的内容全是编的。

6. **换成你自己的笔记**：

   ```bash
   scoped-kb init ~/你的笔记目录 --force
   scoped-kb index
   ```

   `--force` 会覆盖刚才那份示例配置。笔记多的话，第一次建索引要等一会儿，以后只处理改动过的文件。

## 日常怎么用

| 想做什么 | 命令 |
|---|---|
| 改完笔记，更新索引 | `scoped-kb index` |
| 找东西 | `scoped-kb search "你的问题"` |
| 给网页版 AI（ChatGPT、Gemini 等）喂资料 | `scoped-kb pack "任务描述" --out ctx.md`，再把 `ctx.md` 的内容贴过去 |
| 看库里有多少东西 | `scoped-kb status` |

关于 `pack`：它在交出资料前会扫一遍有没有夹带密码、密钥，一旦发现就停下，并告诉你是第几个片段。它不会替你删内容。确认是误报、真要放行，再加 `--force`。扫描没发现问题，也不等于一定干净，贴出去之前自己再看一眼。

## 卡住了怎么提问

到 [Issues](https://github.com/airohugh/scoped-kb/issues) 提一个，附上：

- 卡在第几步，那条命令和**完整的报错原文**（别只截最后一行）。
- `docker compose ps` 和 `scoped-kb status` 的输出。
- 系统版本和 `python3 --version`。

⚠️ **别把你的笔记内容、`pack` 打出来的包、`.zshrc` 整份贴进 issue**，issue 是公开的，里面可能有你自己的私人内容。

这是个人项目，不保证响应时间。

## 不想用了

```bash
cd ~/scoped-kb && docker compose down -v      # 停掉数据库，连数据一起删
ollama rm bge-m3                              # 删模型
rm -rf ~/scoped-kb                            # 删仓库
```

配置和状态目录也删掉：macOS 是 `~/Library/Application Support/scoped-kb/`，Linux 是 `~/.config/scoped-kb/` 和 `~/.local/state/scoped-kb/`。你的笔记目录它从来不改，不用管。
