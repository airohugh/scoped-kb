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

# 值：成对引号里的整段（双引号认反斜杠转义，单引号认 YAML 的 '' 转义，反引号原样），
# 或一段不含空白、引号、逗号分号的字符。允许非 ASCII；纯说明文字由排除项识别。
_VALUE = (r"(?:\"(?P<qd>(?:[^\"\\\n]|\\.){8,200}?)\""
          r"|'(?P<qs>(?:[^'\n]|''){8,200}?)'(?!')"
          r"|`(?P<qb>[^`\n]{8,200}?)`"
          r"|['\"`]?(?P<val>[^\s'\"`,;]{8,}))")
# 分隔符所在行没有值时，下一行**缩进**且整行只有一个值（YAML 标量、JSON 换行写的字符串）
# 才算它的值。下一行是「username: alice」「username:」这样的子键时不吃掉它（未加引号的值
# 不能以冒号结尾、中间不能有空白），子键留给后面的匹配。
_NEXT_LINE = (r"[ \t]*\r?\n[ \t]+(?:\"(?P<nqd>(?:[^\"\\\n]|\\.){8,200}?)\""
              r"|'(?P<nqs>(?:[^'\n]|''){8,200}?)'(?!')"
              r"|(?P<nval>[^\s'\"`,;]{7,}[^\s:'\"`,;]))[ \t]*,?[ \t]*(?=\r?\n|\Z)")

_KEY = r"[A-Za-z0-9_.\-]*(?:" + _KEYWORDS + r")[A-Za-z0-9_.\-]*"

SECRET_PATTERNS = {
    "api_token_prefix": re.compile(r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|xox[abposr]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{35}|glpat-[A-Za-z0-9_\-]{20,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}|\d{8,10}:[A-Za-z0-9_\-]{35})(?![A-Za-z0-9])"),
    "assignment": re.compile(r"(?i)(?<![A-Za-z0-9])(?P<key>" + _KEY + r"|密码|口令|密钥|令牌)['\"`]?[ \t]*(?:[:=：]|\|)(?:[ \t]*" + _VALUE + "|" + _NEXT_LINE + ")"),
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

_P_SHELL_VAR = re.compile(r"^\$(?:[A-Z_][A-Z0-9_]*|[a-z_][a-z0-9_]*)$")   # $API_KEY / $api_key
_P_UPPER_SNAKE = re.compile(r"^[A-Z]+(?:_[A-Z]+)+$")                      # REPLACE_ME / YOUR_API_KEY
_P_MASKED = re.compile(r"^(?:\*+|x+|X+|\.+)$")                            # ******** / xxxxxxxx
_P_DOTTED = re.compile(r"^(?:" + _IDENT + r"\.)+" + _IDENT + r"$")       # settings.API_KEY
_P_SNAKE_WORDS = re.compile(r"^[a-z]+(?:[_\-][a-z]+)+$")                  # your_api_key_here

_VALUE_NOISE = (
    re.compile(r"^\$\{[^{}]+\}$"),                                   # ${DB_PASSWORD}
    _P_SHELL_VAR,
    re.compile(r"^<[^<>]+>$"),                                       # <your-token>
    re.compile(r"^\{\{?\s*[A-Za-z_][\w.]*\s*\}\}?$"),                # {API_TOKEN} / {{ x }}
    re.compile(r"^%\([A-Za-z_]\w*\)s$"),                             # %(db_password)s
    re.compile(r"^(?:[a-z_][a-z0-9_]*\.)*[a-z_][a-z0-9_]*\(\)?$"),    # get_token() / os.getenv(
    re.compile(r"^(?:[a-z_][a-z0-9_]*\.)+[A-Za-z_]\w*\[$"),          # os.environ[
    _P_DOTTED,
    re.compile(r"^(?:~|\.{1,2})/[\w.\-]+(?:/[\w.\-]+)*/?$"),        # ~/.ssh/id_ed25519 / ./id_ed25519
    re.compile(r"^(?:/[\w.\-]+){2,}/?$"),                            # /run/secrets/db
    re.compile(r"^https?://(?![^\s]*[?&#][\w.\-]*(?i:" + _KEYWORDS + r"|key|sig(?:nature)?)[\w.\-]*=)\S*$"),  # 文档链接，查询参数里没有密钥类字段
    _P_SNAKE_WORDS,
    re.compile(r"^[a-z]{1,15}$"),                                    # bearer / required
    _P_UPPER_SNAKE,
    _P_MASKED,
    re.compile(r"^[A-Za-z]+(?: [A-Za-z]+)+$"),                       # 引号里的一句普通英文
    re.compile(r"^(?=[\s\S]*[^\x00-\x7f])(?![\s\S]*[\x21-\x7e]{8})"),  # 中文说明：有非 ASCII、没有 8 位以上的 ASCII 串
)

# 认证头后面跟的本来就是 token，只排除明确的占位、打码和代码引用（$VAR、<x> 进不了
# 认证头的字符集，不用排除）；路径、普通词这类通用排除不套用（「Bearer /<随机>/<随机>」也是 token）。
_AUTH_NOISE = (_P_UPPER_SNAKE, _P_SNAKE_WORDS, _P_MASKED, _P_DOTTED)

# 字段名本身说明它装的不是凭据：password_policy、token_type、api_key_file……
_KEY_NOISE = re.compile(r"(?i)(?:policy|type|length|len|url|uri|file|path|env|name|id|mode"
                        r"|method|header|field|format|scope|ttl|expiry|expires|endpoint)$")


def _is_value_noise(val, noise=_VALUE_NOISE):
    val = val.rstrip(".。，、；！？") or val   # 「auth: required.」——句末标点不是值的一部分
    return any(p.match(val) for p in noise)


class SecretHit(NamedTuple):
    rule: str        # SECRET_PATTERNS 的 id
    line: int        # 在被扫文本里的行号（1 起）
    preview: str     # 掩码后的提示，不含凭据原文，也不含字段名等任何来自被扫文本的片段


def _mask(s):
    """只露开头极少几位（便于认出是哪一类、哪一处）和长度。"""
    shown = s[:min(4, len(s) // 4)]
    return f"{shown}…（{len(s)} 字符）"


_SECRET_GROUPS = ("qd", "qs", "qb", "val", "nqd", "nqs", "nval")


def scan_secrets(text):
    """扫描 text，返回按出现位置排序的 SecretHit 列表；没有命中返回空列表。

    一条匹配被排除项放过时，**从它的值的起点接着扫**，不跳过整段匹配：
    「token_url: https://…#access_token=<随机>」外层因字段名放过，里面的
    access_token= 仍要被看到。finditer 会把被放过的那段整个跳过。
    """
    hits = []
    for rule, pat in SECRET_PATTERNS.items():
        pos = 0
        while True:
            m = pat.search(text, pos)
            if m is None:
                break
            groups = m.groupdict()
            g = next((g for g in _SECRET_GROUPS if groups.get(g)), None)
            secret = groups[g] if g else m.group(0)
            excluded = (rule in ("assignment", "cli_flag") and (
                            _KEY_NOISE.search(groups["key"]) or _is_value_noise(secret))) or \
                       (rule == "auth_header" and _is_value_noise(secret, _AUTH_NOISE))
            if excluded:
                pos = max(m.start() + 1, m.start(g) if g else 0)
                continue
            pos = m.end()
            preview = "PEM 私钥头" if rule == "private_key" else _mask(secret)
            hits.append((m.start(), SecretHit(rule, text.count("\n", 0, m.start()) + 1, preview)))
    hits.sort(key=lambda x: x[0])
    return [h for _, h in hits]
