from __future__ import annotations

import pytest

import ripple.wechat_public_rules as rules
from ripple.campaign_enrichment import campaign_missing_fields
from ripple.publishing import WorkflowError


def test_wechat_official_account_public_rules_are_structured_without_fixed_prize(monkeypatch):
    monkeypatch.setattr(rules, "_render_text", lambda url: """
公众号流量主
开通流程
微信广告平台为公众号流量主提供流量分享和内容定制两种变现合作模式
程序化广告
返佣商品
公众号互选广告
公众号关注用户达到 100 人
符合平台运营规范
""")
    result = rules.preview_public_wechat_rule("https://ad.weixin.qq.com/docs/45", "wechat")
    draft = result["draft"]
    assert draft["platform"] == "wechat"
    assert draft["title"] == "微信公众号流量主"
    assert draft["activity_type"] == "长期创作变现计划"
    assert draft["eligibility"] == ["公众号关注用户达到 100 人", "符合平台运营规范"]
    assert draft["prizes"] == []
    assert draft["winning_conditions"] == []
    assert "互选合作收入" in draft["reward_summary"]
    assert draft["qualification_state"] == "unknown"
    assert campaign_missing_fields({**draft, "source_type": "wechat_public_official"}) == []


def test_channels_revenue_share_public_rules_preserve_account_qualification_unknown(monkeypatch):
    monkeypatch.setattr(rules, "_render_text", lambda url: """
视频号创作分成计划
产品介绍
参与门槛
有效关注人数（粉丝数）在 100 及以上
符合内容规范
优质原创作者
内测阶段，平台将对优质原创作者分批邀请
加入计划后发表原创视频并声明原创
""")
    result = rules.preview_public_wechat_rule("https://ad.weixin.qq.com/docs/273", "weixin-channels")
    draft = result["draft"]
    assert draft["platform"] == "weixin-channels"
    assert draft["title"] == "微信视频号创作分成计划"
    assert "有效关注人数达到 100 人及以上" in draft["eligibility"]
    assert any("分批邀请" in row for row in draft["eligibility"])
    assert draft["submission_spec"]["formats"] == ["video"]
    assert draft["submission_spec"]["original_required"] is True
    assert draft["qualification_state"] == "unknown"
    assert draft["prizes"] == []
    assert campaign_missing_fields({**draft, "source_type": "wechat_public_official"}) == []


def test_wechat_official_rule_can_correct_target_platform(monkeypatch):
    monkeypatch.setattr(rules, "_render_text", lambda url: """
视频号流量主
创作分成计划
视频号互选广告
创作者可选择不同方式进行变现
""")
    result = rules.preview_public_wechat_rule("https://ad.weixin.qq.com/docs/76", "wechat")
    assert result["draft"]["platform"] == "weixin-channels"
    assert result["detected_platform"] == "weixin-channels"
    assert "对应weixin-channels" in result["warning"]


def test_wechat_article_preview_keeps_selected_platform_and_does_not_claim_activity(monkeypatch):
    monkeypatch.setattr(rules, "_http_text", lambda url: "某品牌创作征集公告\n这里是公开文章正文")
    result = rules.preview_public_wechat_rule(
        "https://mp.weixin.qq.com/s?__biz=fixture&mid=1", "weixin-channels",
    )
    draft = result["draft"]
    assert draft["platform"] == "weixin-channels"
    assert draft["activity_type"] == "待确认活动/激励"
    assert draft["qualification_state"] == "unknown"
    assert "尚未确认" in result["warning"]


@pytest.mark.parametrize("url", [
    "http://ad.weixin.qq.com/docs/45",
    "https://127.0.0.1/docs/45",
    "https://example.com/docs/45",
    "https://ad.weixin.qq.com:8443/docs/45",
])
def test_wechat_public_reader_rejects_unapproved_urls(url):
    with pytest.raises(WorkflowError):
        rules.preview_public_wechat_rule(url, "wechat")


def test_official_program_catalog_is_platform_specific():
    assert rules.official_program_urls("wechat") == ["https://ad.weixin.qq.com/docs/45"]
    assert rules.official_program_urls("weixin-channels") == ["https://ad.weixin.qq.com/docs/273"]
    assert rules.official_program_urls("bilibili") == []
