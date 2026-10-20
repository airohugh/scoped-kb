"""Secret-guard test samples (pure data, independent of the implementation).

Every positive credential is assembled at runtime from split fragments plus a
deterministic pseudo-random body, so no string literal in this file is a
complete credential (the repo is public and scanned by secret scanners).
"""
import base64
import json
import random

_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_UPPER_DIGIT = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_URLSAFE = _ALNUM + "_-"
_HEX = "0123456789abcdef"
_DIGITS = "0123456789"


def _rnd(n, seed, alphabet=_ALNUM):
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


def _b64(obj):
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt(seed):
    head = _b64({"alg": "HS256", "typ": "JWT"})
    body = _b64({"sub": f"user-{seed}", "iat": 1700000000 + seed, "role": "admin"})
    return head + "." + body + "." + _rnd(43, seed, _URLSAFE)


def _pem(label, seed):
    head = "-----BEGIN " + label + "-----"
    foot = "-----END " + label + "-----"
    body = _rnd(64, seed, _ALNUM + "+/")
    return head + "\n" + body + "\n" + foot


_gh = "gh"
_tok = {
    "openai_legacy": "s" + "k-" + _rnd(48, 1),
    "openai_proj": "s" + "k-" + "proj-" + _rnd(64, 2, _URLSAFE),
    "anthropic": "s" + "k-" + "ant-" + "api03-" + _rnd(86, 3, _URLSAFE) + "AA",
    "ghp": _gh + "p_" + _rnd(36, 4),
    "gho": _gh + "o_" + _rnd(36, 5),
    "ghs": _gh + "s_" + _rnd(36, 6),
    "gh_pat": "github_" + "pat_" + _rnd(22, 7) + "_" + _rnd(59, 8),
    "slack_bot": "xo" + "xb-" + _rnd(12, 9, _DIGITS) + "-" + _rnd(12, 10, _DIGITS) + "-" + _rnd(24, 11),
    "slack_user": "xo" + "xp-" + _rnd(12, 12, _DIGITS) + "-" + _rnd(12, 13, _DIGITS) + "-" + _rnd(12, 14, _DIGITS) + "-" + _rnd(32, 15, _HEX),
    "google": "AI" + "za" + _rnd(35, 16, _URLSAFE),
    "gitlab": "glp" + "at-" + _rnd(20, 17, _URLSAFE),
    "hf": "h" + "f_" + _rnd(34, 18, _LETTERS),
    "npm": "np" + "m_" + _rnd(36, 19),
    "stripe_sk": "sk_" + "live_" + _rnd(24, 20),
    "stripe_rk": "rk_" + "live_" + _rnd(24, 21),
}
_akid = "AK" + "IA" + _rnd(16, 22, _UPPER_DIGIT)
_asia = "AS" + "IA" + _rnd(16, 23, _UPPER_DIGIT)
_akid2 = "AK" + "IA" + _rnd(16, 24, _UPPER_DIGIT)
_asia2 = "AS" + "IA" + _rnd(16, 25, _UPPER_DIGIT)
_aws_secret = _rnd(40, 26, _ALNUM + "+/")

POSITIVES = [
    # ---- api_token_prefix ----
    ("api_token_prefix", "OpenAI key for the sandbox account: " + _tok["openai_legacy"] + " (rotate monthly)"),
    ("api_token_prefix", "Tried the new project key " + _tok["openai_proj"] + " and it worked."),
    ("api_token_prefix", "Claude call failing?\nUsed " + _tok["anthropic"] + " in the curl header."),
    ("api_token_prefix", "gh auth: " + _tok["ghp"]),
    ("api_token_prefix", "OAuth app token (" + _tok["gho"] + ") expires in 8h."),
    ("api_token_prefix", "Actions runner printed " + _tok["ghs"] + " in the log, need to redact."),
    ("api_token_prefix", "Fine-grained PAT: " + _tok["gh_pat"]),
    ("api_token_prefix", "Slack bot -> " + _tok["slack_bot"] + " posts to #alerts"),
    ("api_token_prefix", "user token " + _tok["slack_user"] + ", scopes: chat:write"),
    ("api_token_prefix", "Maps key is " + _tok["google"] + " restricted to the staging referrer."),
    ("api_token_prefix", "CI uses " + _tok["gitlab"] + " for the registry."),
    ("api_token_prefix", "HF download needs " + _tok["hf"] + " set first."),
    ("api_token_prefix", "//registry.npmjs.org/:_authToken=" + _tok["npm"]),
    ("api_token_prefix", "Stripe live secret " + _tok["stripe_sk"] + " (do not commit)"),
    ("api_token_prefix", "Restricted key: " + _tok["stripe_rk"] + " read-only on charges."),
    # ---- assignment: camelCase names ----
    ("assignment", "dbPassword = " + _rnd(16, 30)),
    ("assignment", "clientSecret: " + _rnd(32, 31)),
    ("assignment", "const accessToken = \"" + _rnd(40, 32) + "\";"),
    ("assignment", "{\"apiKey\": \"" + _rnd(28, 33) + "\"}"),
    ("assignment", "refreshToken=" + _rnd(48, 34, _URLSAFE)),
    # ---- assignment: prefixed/suffixed names ----
    ("assignment", "AWS_SECRET_ACCESS_KEY=" + _aws_secret),
    ("assignment", "DB_PASSWORD=" + _rnd(20, 35)),
    ("assignment", "export SLACK_BOT_TOKEN=" + _rnd(30, 36)),
    ("assignment", "MY_APP_API_KEY: " + _rnd(24, 37)),
    ("assignment", "private_key_passphrase = '" + _rnd(18, 38) + "'"),
    # ---- assignment: short values (8 and 13 chars) ----
    ("assignment", "password=" + _rnd(13, 39)),
    ("assignment", "Note to self: the admin pwd is " + "pwd: " + _rnd(13, 40)),
    ("assignment", "passwd = " + _rnd(8, 41)),
    ("assignment", "DB_PASSWORD=" + _rnd(10, 42)),
    ("assignment", "secret: " + _rnd(9, 43)),
    # ---- assignment: values with symbols ----
    ("assignment", "password: " + "Xy7!kq2Lm9" + "#pQ"),
    ("assignment", "dbPassword=\"" + "r" + "T4$vN8" + "@wZ1%" + "\""),
    ("assignment", "export APP_SECRET='" + "p9^Qe" + "3&mK" + "*7Lz!" + "'"),
    ("assignment", "pwd = " + "Hj5#" + "ud8!" + "Wq2$"),
    # ---- assignment: style variants ----
    ("assignment", "api_key : " + _rnd(26, 44)),
    ("assignment", "apikey=" + _rnd(26, 45)),
    ("assignment", "access_key = \"" + _rnd(22, 46) + "\""),
    ("assignment", "private_key=" + _rnd(44, 47, _ALNUM + "+/")),
    ("assignment", "auth = " + _rnd(30, 48)),
    ("assignment", "credentials: " + _rnd(24, 49)),
    ("assignment", "credential=" + _rnd(24, 50)),
    ("assignment", "services:\n  web:\n    environment:\n      password: " + _rnd(18, 51)),
    ("assignment", "{\n  \"clientSecret\" : \"" + _rnd(36, 52, _URLSAFE) + "\"\n}"),
    ("assignment", "export FOO_TOKEN=" + _rnd(32, 53)),
    ("assignment", "MY_APP_AUTH_TOKEN = " + _rnd(32, 54)),
    ("assignment", "Reminder: login with user alice, Password: " + "Zr8@" + "kT3#" + "m"),
    # ---- connection_string ----
    ("connection_string", "DATABASE_URL points at postgres://app:" + _rnd(14, 60) + "@db.example.internal:5432/main"),
    ("connection_string", "mysql://alice:" + _rnd(12, 61) + "@db.example.internal/shop"),
    ("connection_string", "uri = mongodb+srv://app:" + _rnd(16, 62) + "@cluster0.example.com/test?retryWrites=true"),
    ("connection_string", "redis://default:" + _rnd(20, 63) + "@cache.example.internal:6379/0"),
    ("connection_string", "amqp://alice:" + _rnd(10, 64) + "@mq.example.internal:5672/vhost"),
    ("connection_string", "git clone https://alice:" + _rnd(18, 65) + "@git.example.com/team/repo.git"),
    ("connection_string", "postgresql://app:" + "p%40ss" + "!w0rd" + "@localhost:5432/mydb"),
    # ---- private_key ----
    ("private_key", _pem("RSA " + "PRIVATE KEY", 70)),
    ("private_key", "pasted by mistake:\n" + _pem("OPENSSH " + "PRIVATE KEY", 71)),
    ("private_key", _pem("EC " + "PRIVATE KEY", 72)),
    ("private_key", _pem("PRIVATE " + "KEY", 73)),
    ("private_key", "deploy key header only: -----BEGIN " + "RSA PRIVATE KEY-----"),
    # ---- jwt ----
    ("jwt", "Authorization: Bearer " + _jwt(80)),
    ("jwt", "session cookie decoded from the browser: " + _jwt(81)),
    ("jwt", "id_token " + _jwt(82) + " (expired yesterday)"),
    ("jwt", "Test token:\n" + _jwt(83)),
    # ---- aws_access_key_id ----
    ("aws_access_key_id", "aws_access_key_id = " + _akid),
    ("aws_access_key_id", "Temporary session creds start with " + _asia + " and last 1h."),
    ("aws_access_key_id", "The IAM user's id was " + _akid2 + ", deleted now."),
    ("aws_access_key_id", "STS: " + _asia2),
    # ---- long_hex ----
    ("long_hex", "commit " + _rnd(40, 90, _HEX) + " broke the build"),
    ("long_hex", "sha256: " + _rnd(64, 91, _HEX)),
    ("long_hex", "hex-encoded signing key 0x" + _rnd(64, 92, _HEX) + " from the HSM export"),
    ("long_hex", "Merged as " + _rnd(40, 93, _HEX) + ", then reverted at " + _rnd(40, 94, _HEX)),
    ("long_hex", "key bytes: " + _rnd(128, 95, _HEX)),
    # ---- assignment：中文字段名与全角冒号 ----
    ("assignment", "测试环境的数据库密码：" + _rnd(6, 96) + "!" + _rnd(5, 97)),
    ("assignment", "令牌=" + _rnd(24, 98)),
    ("assignment", "export GPG_PASS" + "PHRASE=" + _rnd(10, 99) + "%" + _rnd(3, 100)),
    # ---- 第二批：外部复核报出的漏检形态（复核方按形态描述，这里照描述造）----
    # 排除项只看首字符时会被放过的值：守着「排除项别放宽」
    ("assignment", "password=$" + "Kq7x" + _rnd(12, 101)),
    ("assignment", "password=%" + "Rt5w" + _rnd(12, 102)),
    ("assignment", "password=[" + "Mz3p" + _rnd(12, 103)),
    ("assignment", "password=" + "Kq7x(" + "9mP2" + _rnd(8, 104)),
    ("assignment", "password=" + "Kq7x9." + "mP2Lm" + _rnd(4, 105)),
    ("assignment", "数据库 password=" + "Hv6t" + _rnd(12, 106) + "。下次换掉"),
    # markdown 与引号里的值
    ("assignment", "password: `" + "Jw2c" + _rnd(12, 107) + "`"),
    ("assignment", "password=\"blue " + "Pn4k" + _rnd(8, 108) + "\""),
    ("assignment", "| password | " + "Ld8s" + _rnd(12, 109) + " |"),
    ("assignment", "签名密钥=" + _rnd(20, 125)),
    ("assignment", "路由器管理口令：" + _rnd(12, 174)),
    ("assignment", "password=" + _rnd(12, 126, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
    # ---- 第三批：第二轮复核报出的漏检形态 ----
    ("assignment", "password=get_token()" + _rnd(16, 130)),
    ("assignment", "password=\"get_token()" + _rnd(16, 131) + "\""),
    ("assignment", "password:\n  " + _rnd(16, 132)),
    ("assignment", "{\n  \"password\":\n    \"" + _rnd(18, 133) + "\"\n}"),
    ("assignment", "password=\"a'" + _rnd(14, 134) + "\""),
    ("assignment", "password='x\"" + _rnd(14, 135) + "'"),
    ("assignment", "password: `a'" + _rnd(14, 136) + "`"),
    ("assignment", "password=/" + "Kq7x" + _rnd(10, 137)),
    ("assignment", "api_key: https://api.example.com/v1?key=" + _rnd(24, 138)),
    # ---- 第四批：第三轮复核报出的漏检形态 ----
    ("assignment", "credentials:\n  password: " + _rnd(16, 150)),
    ("assignment", "auth:\n  clientSecret: " + _rnd(20, 151)),
    ("assignment", "config:\n  api_key: " + _rnd(24, 152)),
    ("assignment", "password=\"密" + _rnd(16, 153) + "\""),
    ("assignment", "{\"password\":\"a\\\"" + _rnd(16, 154) + "\"}"),
    ("assignment", "password: 'a''" + _rnd(16, 155) + "'"),
    ("assignment", "password=\"Kq7x" + _rnd(12, 156) + ":\""),
    ("assignment", "password=Kq7x" + _rnd(12, 157) + ":"),
    ("assignment", "password=密" + _rnd(16, 163)),
    # ---- 第五批：第四轮复核报出的漏检形态 ----
    ("assignment", "password:\n  '" + _rnd(16, 164) + "'"),
    ("assignment", "password:\n  " + _rnd(16, 165) + ":x"),
    ("assignment", "token: https://app.example.com/callback#access_" + "token=" + _rnd(16, 166)),
    # ---- 第六批：第五轮复核报出的漏检形态（外层被放过时吞掉了里面的凭据）----
    ("assignment", "token_url:\n  https://app.example.com/callback#access_" + "token=" + _rnd(16, 167)),
    ("assignment", "auth_url:\n  https://api.example.com/v1?api_" + "key=" + _rnd(16, 168)),
    ("assignment", "token_url:\n  'https://app.example.com/cb?access_" + "token=" + _rnd(16, 169) + "'"),
    ("assignment", "token_url: https://api.example.com/v1?api_" + "key=" + _rnd(16, 170)),
    ("assignment", "token: https://app.example.com/cb#" + "key=" + _rnd(16, 171)),
    # ---- 第七批：第六轮复核报出的形态（shell 参数展开的默认值）----
    ("assignment", "password: ${DB_PASSWORD:-" + _rnd(16, 172) + "}"),
    ("assignment", "export API_TOKEN=${API_TOKEN:=" + _rnd(20, 173) + "}"),
    ("assignment", "DB_PASSWORD=${ENABLE_AUTH:+" + _rnd(16, 175) + "}"),
    ("assignment", "token_url: ${TOKEN_URL:-https://api.example.com/v1?access_" + "token=" + _rnd(16, 177) + "}"),
    ("assignment", "token_url: \"${TOKEN_URL:-https://api.example.com/v1?access_" + "token=" + _rnd(16, 178) + "}\""),
    ("assignment", "token: \"https://api.example.com/v1;api_" + "key=" + _rnd(16, 176) + "\""),
    # ---- cli_flag ----
    ("cli_flag", "mysql -u app --password " + "Tg5n" + _rnd(10, 110) + " shop"),
    ("cli_flag", "tool --api-key " + _rnd(24, 111) + " --verbose"),
    ("cli_flag", "deploy --token '" + _rnd(20, 112) + "'"),
    ("cli_flag", "backup --secret \"" + "Wq3!" + _rnd(10, 113) + "\""),
    ("cli_flag", "deploy --token \"a'" + _rnd(16, 139) + "\""),
    ("cli_flag", "tool --password \"密" + _rnd(16, 158) + "\""),
    # ---- auth_header ----
    ("auth_header", "Authorization: Bear" + "er " + _rnd(32, 114)),
    ("auth_header", "Authorization: Bas" + "ic " + base64.b64encode(("alice:" + _rnd(12, 115)).encode()).decode()),
    ("auth_header", "curl -H 'Authorization: Tok" + "en " + _rnd(40, 116, _HEX) + "' https://api.example.com"),
    ("auth_header", "{\"Authorization\": \"Bear" + "er " + _rnd(36, 117, _URLSAFE) + "\"}"),
    ("auth_header", "Authorization: Bear" + "er /" + _rnd(12, 159) + "/" + _rnd(16, 160)),
    ("auth_header", "Authorization: Tok" + "en /" + _rnd(12, 161) + "/" + _rnd(16, 162)),
    # ---- 其它漏检 ----
    ("connection_string", "redis://:" + _rnd(20, 118) + "@cache.example.internal:6379/0"),
    ("api_token_prefix", "user-to-server token " + _gh + "u_" + _rnd(36, 119)),
    ("api_token_prefix", "refresh token " + _gh + "r_" + _rnd(36, 120)),
    ("api_token_prefix", "mail key " + "S" + "G." + _rnd(22, 121, _URLSAFE) + "." + _rnd(43, 122, _URLSAFE)),
    ("api_token_prefix", "bot " + _rnd(10, 123, _DIGITS) + ":" + "AA" + _rnd(33, 124, _URLSAFE)),
    ("api_token_prefix", "legacy app token " + "xo" + "xa-" + _rnd(12, 140, _DIGITS) + "-" + _rnd(24, 141)),
    ("api_token_prefix", "workspace token " + "xo" + "xo-" + _rnd(12, 142, _DIGITS) + "-" + _rnd(24, 143)),
    ("api_token_prefix", "refresh " + "xo" + "xr-" + _rnd(12, 144, _DIGITS) + "-" + _rnd(24, 145)),
    ("api_token_prefix", "session " + "xo" + "xs-" + _rnd(12, 146, _DIGITS) + "-" + _rnd(24, 147)),
    ("api_token_prefix", "test-mode key " + "sk_" + "test_" + _rnd(24, 148)),
    ("api_token_prefix", "restricted test key " + "rk_" + "test_" + _rnd(24, 149)),
]

NEGATIVES = [
    # placeholders
    ("placeholder angle brackets", "API_TOKEN=<your-token>"),
    ("placeholder shell expansion", "password: ${DB_PASSWORD}"),
    ("placeholder shell var", "export API_KEY=$API_KEY"),
    ("placeholder template", "token: {{ secrets.TOKEN }}"),
    ("placeholder xxx", "password = xxx"),
    ("placeholder stars", "secret: ***"),
    ("placeholder changeme", "DB_PASSWORD=changeme"),
    ("placeholder your_api_key_here", "api_key = your_api_key_here"),
    ("placeholder REPLACE_ME", "clientSecret: REPLACE_ME"),
    ("placeholder in json", "{\"apiKey\": \"<API_KEY>\"}"),
    # code references
    ("env lookup", "password = os.environ[\"DB_PASSWORD\"]"),
    ("function call", "token = get_token()"),
    ("settings attribute", "api_key=settings.API_KEY"),
    ("js env", "secret: process.env.SECRET"),
    ("getenv call", "dbPassword = os.getenv('DB_PASSWORD')"),
    ("config object access", "const accessToken = config.auth.accessToken;"),
    # empty values
    ("empty assignment", "PASSWORD="),
    ("empty string yaml", "token: \"\""),
    ("null value", "api_key: null"),
    ("empty single quotes", "secret = ''"),
    # prose
    ("prose zh token env", "请把 token 写进环境变量，不要提交到仓库。"),
    ("prose password required", "password 字段必填，长度至少八位。"),
    ("prose set in shell", "Set `API_KEY` in your shell before running the script."),
    ("prose see section", "See the `secret` section below for how rotation works."),
    ("prose password manager", "Store the password in your password manager, never in notes."),
    ("prose zh long sentence", "这个服务的认证流程是先拿临时令牌再换取访问凭证，细节见上一节的流程图说明。"),
    # urls
    ("plain docs url", "https://example.com/docs/authentication-tokens"),
    ("db url without password", "postgresql://localhost:5432/mydb"),
    ("url with user only", "ssh://alice@example.com/repo.git"),
    ("url with port not password", "http://localhost:8080/api/v1/users"),
    # short hex and colors
    ("short git sha 7", "fixed in 3fa9c1e"),
    ("short git sha 12", "see commit 3fa9c1e0b4d2"),
    ("css color", "color: #ff00aa;"),
    ("uuid", "request id 123e4567-e89b-12d3-a456-426614174000"),
    # keyword-like names with plain values
    ("token_type bearer", "token_type: bearer"),
    ("auth required", "auth: required"),
    ("password_policy strict", "password_policy: strict"),
    ("secret none", "secret: none"),
    ("auth basic", "auth = basic"),
    ("credentials optional", "credentials: optional"),
    # misc
    ("long ordinary word", "The pseudopseudohypoparathyroidism example is a long but harmless word."),
    ("long file path", "/home/alice/projects/example-service/src/authentication/tokens/refresh_handler.py"),
    ("version string", "version = 1.2.3-rc.4+build.20260101"),
    ("sk- hyphen word", "the sk-learn-style naming is confusing"),
    ("heading mention", "## Rotating the API key"),
    ("table row", "| password | string | required |"),
    ("masked value stars", "password: ************"),
    ("masked value dots", "token = ............"),
    ("chinese narrative value", "密码：请联系管理员获取临时账号与初始口令"),
    ("chinese narrative token", "令牌：在设置页面里生成，有效期三十天"),
    # ---- 第二批：每个排除项各有一条只靠它放行的负例 ----
    ("placeholder printf style", "password = %(db_password)s"),
    ("placeholder single braces", "api_token: {API_TOKEN}"),
    ("placeholder lowercase shell var", "token = $api_key_value"),
    ("masked uppercase", "password: XXXXXXXXXXXX"),
    ("masked long lowercase", "secret = xxxxxxxxxxxxxxxxxxxx"),
    ("path value", "private_key: ~/.ssh/id_ed25519"),
    ("docs url value", "api_key: https://example.com/docs/api-keys"),
    ("policy field", "password_policy: minLength12"),
    ("trailing period", "auth: required."),
    ("quoted plain sentence", "password: \"see the vault\""),
    ("auth header placeholder", "Authorization: Bearer <token>"),
    ("auth header shell var", "Authorization: Bearer $TOKEN"),
    ("cli flag file", "mysql --password-file /run/secrets/db"),
    ("cli flag without value", "./run --verbose --token"),
    # ---- 第三批：第二轮复核报出的误报形态 ----
    ("cli boolean flag then option", "curl --anyauth --location https://example.com"),
    ("cli negated flag then option", "psql --no-password --dbname=demo"),
    ("cli negated flag then argument", "psql --no-password analytics2024"),
    ("auth header placeholder upper", "Authorization: Bearer YOUR_API_KEY"),
    ("auth header masked", "Authorization: Bearer xxxxxxxxxxxx"),
    ("auth header code ref", "Authorization: Bearer process.env.API_KEY"),
    ("quoted chinese narrative", "password: \"请联系管理员获取临时账号与初始口令\""),
    ("single-quoted chinese narrative", "password: '请联系管理员获取临时口令和账号'"),
    # ---- 第四批：第三轮复核报出的误报形态 ----
    ("single-segment relative path", "private_key: ./id_ed25519"),
    ("doc url with ordinary query", "api_key: https://example.com/docs?version=2"),
    ("absolute key file path", "private_key: /etc/ssl/private/server.key"),
    ("yaml nested empty child key", "auth:\n  username:\n    alice"),
    ("markdown label then unindented line", "Token:\nRotated2024Q3"),
    # ---- 第五批：第四轮复核报出的误报形态 ----
    ("auth header lowercase placeholder", "Authorization: Bearer your_api_key_here"),
    ("auth header lowercase placeholder 2", "curl -H 'Authorization: Bear" + "er your_access_token'"),
    # ---- 第七批：第六轮复核报出的误报形态 ----
    ("shell default placeholder", "password: ${DB_PASSWORD:-changeme}"),
    ("shell required message", "password: ${DB_PASSWORD:?required}"),
    ("shell required long message", "token: ${API_TOKEN:?set API_TOKEN before running}"),
    ("shell nested name", "token: ${env:API_TOKEN}"),
    ("shell default on descriptive field", "password_policy: ${PASSWORD_POLICY:-minLength12}"),
    ("shell default on type field", "token_type: ${TOKEN_TYPE:-BearerToken}"),
    ("shell default url with placeholder", "token_url: ${TOKEN_URL:-https://api.example.com/v1?access_token=YOUR_API_KEY}"),
    ("shell default url with shell var", "auth_url: \"${AUTH_URL:-https://api.example.com/v1?api_key=$API_KEY}\""),
    ("yaml nested key on next line", "credentials:\n  username: alice"),
    ("heading then prose on next line", "Password:\nRemember to rotate it every quarter."),
]
