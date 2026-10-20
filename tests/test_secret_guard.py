"""凭据守卫的正例 / 负例 / 变异测试。

样本在 secret_samples.py，按「要抓什么、不该抓什么」的类别清单独立于实现编写，
没有对照正则造。正例断言的是**对应类别的那条规则**命中，不是「任意规则命中」：
所以删掉任何一条规则，它那一类的用例全部变红——这正是变异测试要的性质，
test_every_rule_is_load_bearing 在套件内把它跑一遍。
"""

import re

import pytest
from secret_samples import NEGATIVES, POSITIVES

from scoped_kb import secret_guard
from scoped_kb.secret_guard import SECRET_PATTERNS, scan_secrets


def _rules(text):
    return {h.rule for h in scan_secrets(text)}


@pytest.mark.parametrize("category,text", POSITIVES,
                         ids=[f"{c}-{i}" for i, (c, _) in enumerate(POSITIVES)])
def test_positive_caught_by_its_own_rule(category, text):
    assert category in _rules(text)


@pytest.mark.parametrize("why,text", NEGATIVES,
                         ids=[f"neg-{i}" for i in range(len(NEGATIVES))])
def test_negative_not_reported(why, text):
    assert scan_secrets(text) == [], why


def test_every_category_has_a_rule_and_samples():
    """样本类别与规则一一对应：多一条没样本的规则，或多一类没规则的样本，都算漏测。"""
    assert {c for c, _ in POSITIVES} == set(SECRET_PATTERNS)
    assert set(secret_guard.SECRET_LABELS) == set(SECRET_PATTERNS)


@pytest.mark.parametrize("rule", sorted(SECRET_PATTERNS))
def test_every_rule_is_load_bearing(rule, monkeypatch):
    """去掉一条规则，它那一类至少有一条正例不再被任何规则抓到。

    只断言「对应规则不再命中」是同义反复；这里要的是更强的性质：该规则不是
    别的规则的子集，删掉它真的会漏。
    """
    reduced = {k: v for k, v in SECRET_PATTERNS.items() if k != rule}
    monkeypatch.setattr(secret_guard, "SECRET_PATTERNS", reduced)
    own = [t for c, t in POSITIVES if c == rule]
    assert any(scan_secrets(t) == [] for t in own), f"{rule} 被其它规则完全覆盖"


def test_preview_never_echoes_the_secret():
    """报告进终端与日志，不得回显凭据原文。"""
    for _category, text in POSITIVES:
        for hit in scan_secrets(text):
            # 样本里带数字、长度 ≥ 8 的片段都可能是凭据主体（字段名、协议名、PEM 头
            # 不带数字），掩码里不得出现其中任何一个的前 5 位
            for tok in re.findall(r"[^\s'\"`,;=:@/]{8,}", text):
                if re.search(r"\d", tok):
                    assert tok[:5] not in hit.preview, (hit.rule, hit.preview)


def test_hit_line_numbers_point_at_the_secret():
    text = "第一行\n第二行\n" + POSITIVES[0][1]
    first = scan_secrets(text)[0]
    assert first.line >= 3


def test_exit_code_is_distinct():
    from scoped_kb.scope import SCOPE_ERROR_EXIT
    assert secret_guard.SECRET_GUARD_EXIT not in (0, 1, 2, SCOPE_ERROR_EXIT)
