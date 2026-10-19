import os
from pathlib import Path

import platformdirs

from scoped_kb.embed import DEFAULT_DIM, DEFAULT_MODEL

# 谁在写库。每个 agent 在自己的环境里设 SCOPED_KB_AGENT，写入就能归属到人头上——
# 否则 source_agent 列形同虚设（建了但永远是同一个值）。
AGENT = os.environ.get("SCOPED_KB_AGENT", "scoped-kb")

MODEL = os.environ.get("SCOPED_KB_EMBED_MODEL") or DEFAULT_MODEL
DIM = int(os.environ.get("SCOPED_KB_EMBED_DIM") or DEFAULT_DIM)

# 配置目录：config.json、scope-policy.json、machine-sources.json 都放这里。
CONFIG_DIR = Path(os.environ.get("SCOPED_KB_CONFIG_DIR")
                  or platformdirs.user_config_dir("scoped-kb"))

# 状态目录：维护闸与索引锁所在目录。
STATE_DIR = Path(os.environ.get("SCOPED_KB_STATE_DIR")
                 or platformdirs.user_state_dir("scoped-kb"))
