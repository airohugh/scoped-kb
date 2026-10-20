"""凭据守卫：pack 交付前扫一遍整份包，检出疑似凭据就中止。

**只报不改。** 静默脱敏会制造虚假安心——人以为安全了，实际只是没看见；
而且被改过的包看起来和没改过的一样，事后无从核对。所以检出即中止，
由人确认是误报后再用 `--force` 放行。

**扫描零命中不等于干净。** 正则只认有固定形态的东西：没有字段名提示的裸密码、
短于 8 位的值、全小写的连字符口令都会漏。守卫是最后一道网，不是第一道。

报告里**不回显凭据本身**：守卫的输出会进终端、CI 日志、聊天记录，
把命中原文打出来等于守卫自己泄露了它要拦的东西。只给规则、行号与掩码。
"""

import re
from typing import NamedTuple

SECRET_GUARD_EXIT = 4          # 与 argparse 用法错误的 2、作用域错误的 3 区分

# ---- 规则 -----------------------------------------------------------------
#
# 每条规则一行，规则 id 即测试样本里的 category。删掉任何一行，
# tests/test_secret_guard.py 里对应类别的用例必须变红（变异测试，见该文件）。

_KEYWORDS = (r"pass(?:word|wd|phrase)|pwd|secret|token|api[_-]?key|access[_-]?key"
             r"|private[_-]?key|auth(?!or)|credentials?")

_ASSIGNMENT = (
    r"(?i)(?<![A-Za-z0-9])"
    r"(?P<key>[A-Za-z0-9_.\-]*(?:" + _KEYWORDS + r")[A-Za-z0-9_.\-]*|密码|口令|密钥|令牌)"
    r"['\"]?\s*[:=：]\s*(?P<q>['\"]?)(?P<val>[^\s'\"`,;]{8,})"
)

SECRET_PATTERNS = {
    "api_token_prefix": re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|glpat-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,})"),
    "assignment": re.compile(_ASSIGNMENT),
    "connection_string": re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:(?P<val>[^\s/@]+)@"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{10,}"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "long_hex": re.compile(r"\b(?:0x)?[a-fA-F0-9]{40,}\b"),
}

SECRET_LABELS = {
    "api_token_prefix": "带厂商前缀的 token",
    "assignment": "赋值型密钥",
    "connection_string": "连接串里的密码",
    "private_key": "私钥",
    "jwt": "JWT",
    "aws_access_key_id": "AWS access key ID",
    "long_hex": "长十六进制串",
}


# ---- 赋值型的排除项 -------------------------------------------------------
#
# 字段名命中关键词的地方太多：变量引用、占位符、文档里的字段说明。下面这些值
# 不是凭据本身，排除掉。每条排除也有负例守着（删掉排除项，负例变红）。

# 模板与占位：${X}、$X、<your-token>、{{ x }}、%(x)s、[x]
_PLACEHOLDER_START = re.compile(r"^[$<{%\[]")
# 代码引用：os.environ[...]、get_token()、settings.API_KEY、process.env.SECRET
_CODE_REF = re.compile(r"^[A-Za-z_][\w.]*[(\[]|^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
# 普通词：bearer、required、your_api_key_here、REPLACE_ME（全小写或全大写的词，可用 _ - 连接）
_PLAIN_WORDS = re.compile(r"^(?:[a-z]+(?:[_\-][a-z]+)+|[a-z]{1,15}|[A-Z]+(?:_[A-Z]+)*)$")
# 打码：********、xxxxxxxx、........
_MASKED = re.compile(r"^(?:\*+|[xX]+|\.+)$")
# 值里有非 ASCII 字符（如「密码：请联系管理员获取」）：是叙述，不是凭据
_NON_ASCII = re.compile(r"[^\x21-\x7e]")


def _is_assignment_noise(val):
    return bool(_PLACEHOLDER_START.match(val) or _CODE_REF.match(val)
                or _PLAIN_WORDS.match(val) or _MASKED.match(val)
                or _NON_ASCII.search(val))


class SecretHit(NamedTuple):
    rule: str        # SECRET_PATTERNS 的 id
    line: int        # 在被扫文本里的行号（1 起）
    preview: str     # 掩码后的提示，不含凭据原文


def _mask(s):
    """只露开头极少几位（便于认出是哪一类、哪一处）和长度。"""
    shown = s[:min(4, len(s) // 4)]
    return f"{shown}…（{len(s)} 字符）"


def scan_secrets(text):
    """扫描 text，返回按出现位置排序的 SecretHit 列表；没有命中返回空列表。"""
    hits = []
    for rule, pat in SECRET_PATTERNS.items():
        for m in pat.finditer(text):
            if rule == "assignment":
                val = m.group("val")
                if _is_assignment_noise(val):
                    continue
                preview = f"{m.group('key')} = {_mask(val)}"
            elif rule == "private_key":
                preview = m.group(0)          # 只是 PEM 头，不是密钥本身
            elif rule == "connection_string":
                preview = f"{m.group(0).split('://')[0]}://… 密码 {_mask(m.group('val'))}"
            else:
                preview = _mask(m.group(0))
            hits.append((m.start(), SecretHit(rule, text.count("\n", 0, m.start()) + 1, preview)))
    hits.sort(key=lambda x: x[0])
    return [h for _, h in hits]
