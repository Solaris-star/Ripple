from __future__ import annotations

from copy import deepcopy

from ripple.campaign_enrichment import (
    apply_agent_draft,
    campaign_missing_fields,
    empty_submission_spec,
    filter_draft_by_evidence,
    infer_submission_spec,
    normalize_submission_spec,
    should_agent_enrich,
)


def _campaign(**overrides):
    value = {
        "id": "c1",
        "platform": "bilibili",
        "source_type": "platform_public",
        "eligibility": [],
        "submission_spec": empty_submission_spec(),
        "prizes": [],
        "reward_summary": "",
        "winning_conditions": [],
        "rule_evidence_fingerprint": "fp-1",
        "last_agent_fingerprint": "",
        "agent_attempt_fingerprint": "",
        "agent_attempt_count": 0,
        "agent_next_retry_at": 0,
        "user_confirmed_fields": [],
        "field_evidence": {},
    }
    value.update(overrides)
    return value


def test_submission_spec_unknown_stays_unknown():
    spec = normalize_submission_spec({})
    assert spec["formats"] == []
    assert spec["duration_seconds"] == {"min": None, "max": None}
    assert spec["original_required"] is None
    assert spec["first_publish_required"] is None
    assert spec["exclusive_required"] is None
    assert spec["max_entries"] is None


def test_submission_spec_infers_only_explicit_constraints():
    spec = infer_submission_spec("参赛作品需为原创竖屏短视频，时长至少30秒，不超过3分钟，比例9:16，1080P。最多投稿3个作品。")
    assert spec["formats"] == ["short_video"]
    assert spec["duration_seconds"] == {"min": 30, "max": 180}
    assert spec["aspect_ratios"] == ["9:16"]
    assert spec["orientation"] == "vertical"
    assert spec["resolutions"] == ["1080P"]
    assert spec["original_required"] is True
    assert spec["max_entries"] == 3
    assert spec["first_publish_required"] is None


def test_agent_dedup_uses_rule_evidence_fingerprint_and_force_is_explicit():
    item = _campaign()
    allowed, reason = should_agent_enrich(item)
    assert allowed is True and reason == "new_evidence"

    item["last_agent_fingerprint"] = "fp-1"
    allowed, reason = should_agent_enrich(item)
    assert allowed is False and reason == "already_analyzed"

    item["rule_evidence_fingerprint"] = "fp-2"
    allowed, reason = should_agent_enrich(item)
    assert allowed is True and reason == "new_evidence"

    manual = _campaign(source_type="user_import")
    assert should_agent_enrich(manual) == (False, "manual_import")
    assert should_agent_enrich(manual, force=True) == (True, "forced")


def test_agent_attempts_stop_after_two_failures_for_same_evidence():
    item = _campaign(agent_attempt_fingerprint="fp-1", agent_attempt_count=2, agent_next_retry_at=0)
    assert should_agent_enrich(item) == (False, "retry_exhausted")


def test_agent_evidence_filter_drops_unsupported_claims():
    draft = {
        "eligibility": ["必须报名"],
        "prizes": ["单人奖金10万元"],
        "submission_spec": {"formats": ["video"]},
        "field_evidence": {
            "eligibility": ["参加活动前必须报名"],
            "prizes": ["单人奖金10万元"],
            "submission_spec": ["视频作品"],
        },
    }
    evidence = "活动规则：参加活动前必须报名。视频作品需带指定话题。总奖池10万元。"
    filtered = filter_draft_by_evidence(draft, evidence)
    assert filtered["eligibility"] == ["必须报名"]
    assert filtered["prizes"] == []
    assert filtered["submission_spec"]["formats"] == ["video"]


def test_agent_draft_only_fills_missing_and_respects_user_confirmed_fields():
    item = _campaign(
        eligibility=["用户确认：需报名"],
        user_confirmed_fields=["eligibility"],
        field_evidence={},
    )
    draft = {
        "eligibility": ["Agent：无需报名"],
        "prizes": ["瓜分5000元"],
        "submission_spec": {"formats": ["video"]},
        "field_evidence": {
            "eligibility": ["需报名"],
            "prizes": ["瓜分5000元"],
            "submission_spec": ["视频作品"],
        },
    }
    enriched = apply_agent_draft(deepcopy(item), draft)
    assert enriched["eligibility"] == ["用户确认：需报名"]
    assert enriched["prizes"] == ["瓜分5000元"]
    assert enriched["submission_spec"]["formats"] == ["video"]
    assert campaign_missing_fields(enriched) == ["winning_conditions"]
