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
# 每条规则一行，规则 id 即测试样本里的 category。删掉任何一行、任何一个分支或
# 关键词，tests/test_secret_guard.py 里都有用例变红（变异测试，见该文件）。

_KEYWORDS = (r"pass(?:word|wd|phrase)|pwd|secret|token|api[_-]?key|access[_-]?key"
             r"|private[_-]?key|auth(?!or)|credentials?")

# 值：成对引号（双引号、单引号、反引号各按自己的闭合符）里的整段，或一段不含空白、
# 引号、逗号分号的可见 ASCII。都只收 ASCII：「password=<值>。」里的中文句号不得并进值里，
# 引号里的中文说明文字也不当成值。
_VALUE = (r"(?:\"(?P<qd>[\x20-\x21\x23-\x7e]{8,200}?)\""
          r"|'(?P<qs>[\x20-\x26\x28-\x7e]{8,200}?)'"
          r"|`(?P<qb>[\x20-\x5f\x61-\x7e]{8,200}?)`"
          r"|['\"`]?(?P<val>(?:(?![`'\",;])[\x21-\x7e]){8,}))")
# 分隔符之后允许换一次行：YAML「password:」下一行缩进写值、JSON 冒号后换行写字符串。
_SEP_TAIL = r"[ \t]*(?:\r?\n[ \t]*)?"

_KEY = r"[A-Za-z0-9_.\-]*(?:" + _KEYWORDS + r")[A-Za-z0-9_.\-]*"

SECRET_PATTERNS = {
    "api_token_prefix": re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|glpat-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}|\d{8,10}:[A-Za-z0-9_\-]{35})(?![A-Za-z0-9])"),
    "assignment": re.compile(r"(?i)(?<![A-Za-z0-9])(?P<key>" + _KEY + r"|密码|口令|密钥|令牌)['\"`]?[ \t]*(?:[:=：]|\|)" + _SEP_TAIL + _VALUE),
    "cli_flag": re.compile(r"(?i)(?<![\w-])--?(?!-|no-)(?P<key>" + _KEY + r")[ \t]+(?!-)" + _VALUE),
    "auth_header": re.compile(r"(?i)\bauthorization['\"]?[ \t]*[:=][ \t]*['\"]?(?:bearer|basic|token)[ \t]+(?P<val>[A-Za-z0-9._~+/=\-]{8,})"),
    "connection_string": re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]*:(?P<val>[^\s/@]+)@"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{10,}"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "long_hex": re.compile(r"\b(?:0x)?[a-fA-F0-9]{40,}\b"),
}

SECRET_LABELS = {
    "api_token_prefix": "带厂商前缀的 token",
    "assignment": "赋值型密钥",
    "cli_flag": "命令行参数里的密钥",
    "auth_header": "Authorization 头",
    "connection_string": "连接串里的密码",
    "private_key": "私钥",
    "jwt": "JWT",
    "aws_access_key_id": "AWS access key ID",
    "long_hex": "长十六进制串",
}


# ---- 赋值型与命令行参数的排除项 ---------------------------------------------
#
# 字段名命中关键词的地方太多：变量引用、占位符、文档里的字段说明。下面这些
# 不是凭据本身，排除掉。每个排除项都按**完整形态**匹配整个值（只看首字符会放过
# 「password=$<随机串>」），一行一个，各有负例守着；它们放宽一点就会漏，
# 所以也各有正例守着「别放宽」。

_IDENT = r"(?:[a-z_][a-z0-9_]*|[a-z]+(?:[A-Z][a-z0-9]*)+|[A-Z][A-Z0-9_]*)"

_VALUE_NOISE = (
    re.compile(r"^\$\{[^{}]+\}$"),                                   # ${DB_PASSWORD}
    re.compile(r"^\$(?:[A-Z_][A-Z0-9_]*|[a-z_][a-z0-9_]*)$"),        # $API_KEY / $api_key
    re.compile(r"^<[^<>]+>$"),                                       # <your-token>
    re.compile(r"^\{\{?\s*[A-Za-z_][\w.]*\s*\}\}?$"),                # {API_TOKEN} / {{ x }}
    re.compile(r"^%\([A-Za-z_]\w*\)s$"),                             # %(db_password)s
    re.compile(r"^(?:[a-z_][a-z0-9_]*\.)*[a-z_][a-z0-9_]*\(\)?$"),    # get_token() / os.getenv(
    re.compile(r"^(?:[a-z_][a-z0-9_]*\.)+[A-Za-z_]\w*\[$"),          # os.environ[
    re.compile(r"^(?:" + _IDENT + r"\.)+" + _IDENT + r"$"),         # settings.API_KEY / process.env.SECRET
    re.compile(r"^(?:~|\.{1,2})?(?:/[\w.\-]+){2,}/?$"),             # ~/.ssh/id_ed25519
    re.compile(r"^https?://[^\s=]*$"),                               # 文档链接（带密码的 URL 归连接串规则）
    re.compile(r"^[a-z]+(?:[_\-][a-z]+)+$"),                         # your_api_key_here
    re.compile(r"^[A-Z]?[a-z]{1,15}$"),                              # bearer / required / Remember
    re.compile(r"^[A-Za-z_][\w\-]*:$"),                              # YAML 下一行是嵌套的键：「username:」
    re.compile(r"^[A-Z]+(?:_[A-Z]+)+$"),                             # REPLACE_ME / DB_PASSWORD
    re.compile(r"^(?:\*+|x+|X+|\.+)$"),                              # ******** / xxxxxxxx
    re.compile(r"^[A-Za-z]+(?: [A-Za-z]+)+$"),                       # 引号里的一句普通英文
)

# 字段名本身说明它装的不是凭据：password_policy、token_type、api_key_file……
_KEY_NOISE = re.compile(r"(?i)(?:policy|type|length|len|url|uri|file|path|env|name|id|mode"
                        r"|method|header|field|format|scope|ttl|expiry|expires|endpoint)$")


# 值要过排除项的规则：字段名提示的三类。厂商前缀等有固定形态的规则不过。
_VALUE_CHECKED = ("assignment", "cli_flag", "auth_header")


def _is_value_noise(val):
    val = val.rstrip(".") or val      # 「auth: required.」——句末的点不是值的一部分
    return any(p.match(val) for p in _VALUE_NOISE)


class SecretHit(NamedTuple):
    rule: str        # SECRET_PATTERNS 的 id
    line: int        # 在被扫文本里的行号（1 起）
    preview: str     # 掩码后的提示，不含凭据原文，也不含字段名等任何来自被扫文本的片段


def _mask(s):
    """只露开头极少几位（便于认出是哪一类、哪一处）和长度。"""
    shown = s[:min(4, len(s) // 4)]
    return f"{shown}…（{len(s)} 字符）"


def looks_sensitive(text):
    """比 scan_secrets 保守得多的判断：带关键词、`=`、`:` 或任何命中，就当它可能含凭据。

    给「这段文本要不要打到报告里」用（如位置键）。宁可多藏，不能靠扫描零命中放行。
    """
    return bool(re.search(r"(?i)(?:" + _KEYWORDS + r")|[=:]", text) or scan_secrets(text))


def scan_secrets(text):
    """扫描 text，返回按出现位置排序的 SecretHit 列表；没有命中返回空列表。"""
    hits = []
    for rule, pat in SECRET_PATTERNS.items():
        for m in pat.finditer(text):
            groups = m.groupdict()
            secret = (groups.get("qd") or groups.get("qs") or groups.get("qb")
                      or groups.get("val") or m.group(0))
            if "key" in groups and _KEY_NOISE.search(groups["key"]):
                continue
            if rule in _VALUE_CHECKED and _is_value_noise(secret):
                continue
            preview = "PEM 私钥头" if rule == "private_key" else _mask(secret)
            hits.append((m.start(), SecretHit(rule, text.count("\n", 0, m.start()) + 1, preview)))
    hits.sort(key=lambda x: x[0])
    return [h for _, h in hits]
