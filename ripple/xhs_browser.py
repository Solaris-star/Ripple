"""Bounded Xiaohongshu browser operations used by Ripple's isolated native worker.

This module contains first-party, task-scoped extraction and interaction logic.
It never owns credentials and never persists browser data outside the account's
private profile. Public projections are sanitized by :mod:`xhs_ops`.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit


XHS_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com", "creator.xiaohongshu.com"}
NOTE_PATH_RE = re.compile(r"/(?:explore|discovery/item|item)/([0-9A-Za-z]+)")
CREATOR_DETAIL_CACHE_TTL = 24 * 60 * 60
CREATOR_ACTIVITY_MAX = 500
CREATOR_DETAIL_CACHE_MAX = 600
CREATOR_DETAIL_CACHE_VERSION = 3

CREATOR_DSL_DETAIL_JS = r"""() => {
  const dsl = window.__SETUP_SERVER_STATE__ && window.__SETUP_SERVER_STATE__.DSL;
  const root = dsl && Array.isArray(dsl.componentsTree) ? dsl.componentsTree : null;
  const out = { prizes: [], task_rules: [], eligibility: [], winning_conditions: [], content_rules: [], evidence: [], component_types: [] };
  const seenPrize = new Set(), seenRule = new Set(), seenEligibility = new Set(), seenWinning = new Set(), seenContent = new Set(), seenType = new Set();
  let visited = 0;
  const hidden = (node) => {
    if (!node || typeof node !== 'object') return false;
    const props = node.props && typeof node.props === 'object' ? node.props : {};
    const style = (props.style && typeof props.style === 'object') ? props.style :
      ((node.style && typeof node.style === 'object') ? node.style : {});
    return node.visible === false || node.hidden === true || node.disabled === true ||
      props.visible === false || props.hidden === true || props.disabled === true ||
      style.display === 'none' || style.visibility === 'hidden';
  };
  const pushPrize = (title, path) => {
    title = String(title || '').replace(/\s+/g, ' ').trim().slice(0, 240);
    if (!title || seenPrize.has(title)) return;
    seenPrize.add(title); out.prizes.push(title);
    out.evidence.push({ field: 'prizes', path, original: title });
  };
  const pushRule = (text, path) => {
    text = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 500);
    if (!text || seenRule.has(text)) return;
    seenRule.add(text); out.task_rules.push(text);
    out.evidence.push({ field: 'content_requirements', path, original: text });
  };
  const pushField = (field, text, path, seen) => {
    text = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 800);
    if (!text || text.length < 5 || seen.has(text)) return;
    seen.add(text); out[field].push(text);
    out.evidence.push({ field, path, original: text });
  };
  const walk = (node, path, depth) => {
    if (++visited > 18000 || depth > 24 || !node || typeof node !== 'object' || hidden(node)) return;
    if (Array.isArray(node)) {
      node.slice(0, 400).forEach((child, index) => walk(child, path + '[' + index + ']', depth + 1));
      return;
    }
    for (const [key, value] of Object.entries(node)) {
      const next = path + '.' + key;
      if ((key === 'componentName' || key === 'componentType' || key === 'name') && typeof value === 'string' && value.length < 120) {
        if (!seenType.has(value)) { seenType.add(value); out.component_types.push(value); }
      }
      if (typeof value === 'string') {
        const text = value.replace(/\s+/g, ' ').trim();
        if (/(参与条件|参与要求|参与资格|报名条件)/.test(text) && text.length > 6) {
          pushField('eligibility', text, next, seenEligibility);
        }
        if (/(获奖条件|评选规则|评选标准|中奖条件|奖励条件)/.test(text) && text.length > 6) {
          pushField('winning_conditions', text, next, seenWinning);
        }
        if (/(投稿要求|作品要求|创作要求|内容要求)/.test(text) && text.length > 6) {
          pushField('content_rules', text, next, seenContent);
        }
      }
      if (key === 'prizeInfo' && Array.isArray(value)) {
        value.slice(0, 50).forEach((row, index) => {
          if (row && typeof row === 'object') pushPrize(row.title, next + '[' + index + '].title');
        });
      }
      if (key === 'taskList' && Array.isArray(value) && path.includes('playSection')) {
        value.slice(0, 80).forEach((row, index) => {
          if (!row || typeof row !== 'object' || hidden(row)) return;
          const text = row.description || row.title || row.name || '';
          if (/发布|投稿|创作/.test(String(text || ''))) {
            pushRule(text, next + '[' + index + '].description');
          }
        });
      }
      walk(value, next, depth + 1);
    }
  };
  if (root) walk(root, 'DSL.componentsTree', 0);
  out.prizes = out.prizes.slice(0, 30);
  out.task_rules = out.task_rules.slice(0, 40);
  out.eligibility = out.eligibility.slice(0, 30);
  out.winning_conditions = out.winning_conditions.slice(0, 30);
  out.content_rules = out.content_rules.slice(0, 40);
  out.evidence = out.evidence.slice(0, 80);
  out.component_types = out.component_types.slice(0, 40);
  out.has_visual_assets = out.component_types.some((value) => /Banner|自定义图片|HotArea|热区图/.test(value));
  return out;
}"""


def _unique_text(values: list[Any], limit: int, max_len: int = 240) -> list[str]:
    out: list[str] = []
    for raw in values:
        text = re.sub(r'\s+', ' ', str(raw or '')).strip()[:max_len]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _rewardish_summary(text: str) -> str:
    value = re.sub(r'\s+', ' ', str(text or '')).strip()[:600]
    return value if value and re.search(r'(现金|奖金|奖池|瓜分|奖品|礼包|流量|积分|音符|奖励|周边|实物|礼品|券)', value) else ''


def _creator_dsl_detail(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    prizes = _unique_text(data.get('prizes') if isinstance(data.get('prizes'), list) else [], 30)
    task_rules = _unique_text(data.get('task_rules') if isinstance(data.get('task_rules'), list) else [], 40, 500)
    content_rules = _unique_text(data.get('content_rules') if isinstance(data.get('content_rules'), list) else [], 40, 800)
    rules = _unique_text(task_rules + content_rules, 40, 800)
    eligibility = _unique_text(data.get('eligibility') if isinstance(data.get('eligibility'), list) else [], 30, 800)
    winning = _unique_text(data.get('winning_conditions') if isinstance(data.get('winning_conditions'), list) else [], 30, 800)
    evidence = [row for row in data.get('evidence', []) if isinstance(row, dict)][:100] if isinstance(data.get('evidence'), list) else []
    field_evidence: dict[str, Any] = {}
    if prizes:
        field_evidence['prizes'] = {
            'source': 'xhs_ditto_dsl',
            'paths': [str(row.get('path') or '')[:300] for row in evidence if row.get('field') == 'prizes'][:30],
            'originals': prizes[:30],
        }
    if rules:
        field_evidence['content_requirements'] = {
            'source': 'xhs_ditto_dsl',
            'paths': [str(row.get('path') or '')[:300] for row in evidence if row.get('field') == 'content_requirements'][:40],
            'originals': rules[:40],
        }
    if eligibility:
        field_evidence['eligibility'] = {
            'source': 'xhs_ditto_dsl',
            'paths': [str(row.get('path') or '')[:300] for row in evidence if row.get('field') == 'eligibility'][:30],
            'originals': eligibility[:30],
        }
    if winning:
        field_evidence['winning_conditions'] = {
            'source': 'xhs_ditto_dsl',
            'paths': [str(row.get('path') or '')[:300] for row in evidence if row.get('field') == 'winning_conditions'][:30],
            'originals': winning[:30],
        }
    return {
        'prizes': prizes,
        'content_requirements': rules,
        'eligibility': eligibility,
        'winning_conditions': winning,
        'field_evidence': field_evidence,
        'dsl_component_types': _unique_text(data.get('component_types') if isinstance(data.get('component_types'), list) else [], 40, 120),
        'has_visual_assets': bool(data.get('has_visual_assets')),
    }


def _creator_milestone_detail(values: list[Any]) -> dict[str, Any]:
    prizes: list[str] = []
    conditions: list[str] = []
    reward_rules: list[str] = []
    paths: list[str] = []
    originals: list[str] = []
    for payload_index, value in enumerate(values[:8]):
        data = value.get('data') if isinstance(value, dict) and isinstance(value.get('data'), dict) else (
            value if isinstance(value, dict) else {}
        )
        milestones = data.get('mile_stone_info') if isinstance(data.get('mile_stone_info'), list) else []
        for index, row in enumerate(milestones[:50]):
            if not isinstance(row, dict):
                continue
            reward = row.get('reward_info') if isinstance(row.get('reward_info'), dict) else {}
            milestone = row.get('mile_stone') if isinstance(row.get('mile_stone'), dict) else {}
            title = re.sub(r'\s+', ' ', str(reward.get('title') or '')).strip()[:240]
            point = milestone.get('point_number')
            if title and title not in prizes:
                prizes.append(title)
                paths.append(f'milestone[{payload_index}].mile_stone_info[{index}].reward_info.title')
                originals.append(title)
            try:
                point_number = int(point)
            except (TypeError, ValueError):
                point_number = 0
            if point_number > 0:
                condition = f'活动进度达到 {point_number}（进度单位以活动页说明为准）'
                if condition not in conditions:
                    conditions.append(condition)
                if title:
                    rule = f'{condition}：对应奖励「{title}」'
                    if rule not in reward_rules:
                        reward_rules.append(rule)
    field_evidence: dict[str, Any] = {}
    if prizes:
        field_evidence['prizes'] = {
            'source': 'xhs_milestone_api', 'paths': paths[:30], 'originals': originals[:30],
        }
    if conditions:
        field_evidence['winning_conditions'] = {
            'source': 'xhs_milestone_api',
            'originals': conditions[:30],
            'note': '接口返回进度阈值；未推断进度单位，也未推断必然获奖。',
        }
    if reward_rules:
        field_evidence['reward_rules'] = {
            'source': 'xhs_milestone_api', 'originals': reward_rules[:30],
        }
    return {
        'prizes': prizes[:30],
        'winning_conditions': conditions[:30],
        'reward_rules': reward_rules[:30],
        'field_evidence': field_evidence,
    }


def _merge_field_evidence(*values: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, row in value.items():
            if isinstance(row, dict):
                merged[str(key)[:80]] = row
    return merged


def _creator_detail_from_sources(task_payload: Any, dsl_value: Any, milestone_values: list[Any]) -> dict[str, Any]:
    task = _creator_task_detail(task_payload if isinstance(task_payload, dict) else {})
    dsl = _creator_dsl_detail(dsl_value)
    milestone = _creator_milestone_detail(milestone_values)
    content_requirements = _unique_text(
        list(task.get('content_requirements') or []) + list(dsl.get('content_requirements') or []), 30, 800
    )
    eligibility = _unique_text(list(dsl.get('eligibility') or []), 20, 800)
    prizes = _unique_text(list(milestone.get('prizes') or []) + list(dsl.get('prizes') or []), 30)
    winning_conditions = _unique_text(
        list(milestone.get('winning_conditions') or []) + list(dsl.get('winning_conditions') or []), 30, 800
    )
    reward_rules = _unique_text(
        list(task.get('reward_rules') or []) + list(milestone.get('reward_rules') or []), 30, 500
    )
    parsed = bool(
        eligibility or content_requirements or prizes or winning_conditions or reward_rules
        or task.get('required_topics') or (task.get('submission_spec') or {}).get('submission_method')
    )
    return {
        **task,
        'eligibility': eligibility,
        'content_requirements': content_requirements,
        'prizes': prizes,
        'winning_conditions': winning_conditions,
        'reward_rules': reward_rules,
        'field_evidence': _merge_field_evidence(
            task.get('field_evidence'), dsl.get('field_evidence'), milestone.get('field_evidence')
        ),
        'dsl_component_types': dsl.get('dsl_component_types') or [],
        'detail_source': '+'.join([
            name for name, present in (
                ('creator_task_api', bool(task_payload)),
                ('xhs_ditto_dsl', bool((dsl.get('prizes') or dsl.get('content_requirements')))),
                ('xhs_milestone_api', bool(milestone_values)),
            ) if present
        ]) or 'creator_event_page',
        'xhs_detail_status': 'parsed' if parsed else ('needs_visual_review' if dsl.get('has_visual_assets') else 'no_structured_rules'),
        'xhs_detail_version': CREATOR_DETAIL_CACHE_VERSION,
        'xhs_detail_fetched_at': int(time.time()),
        'xhs_detail_error': '',
    }


def _capture_creator_detail(page, url: str) -> dict[str, Any]:
    task_payload: dict[str, Any] | None = None
    milestone_payloads: list[dict[str, Any]] = []

    def on_detail(response):
        nonlocal task_payload
        try:
            parsed = urlsplit(response.url)
            if parsed.hostname != 'edith.xiaohongshu.com' or int(response.status or 0) != 200:
                return
            if 'json' not in str(response.headers.get('content-type') or '').lower():
                return
            if parsed.path == '/api/sns/v1/activity_platform/config/task/preview_task_list':
                value = response.json()
                if isinstance(value, dict):
                    task_payload = value
            elif parsed.path == '/api/sns/v1/activity_platform/milestone/info':
                value = response.json()
                if isinstance(value, dict):
                    milestone_payloads.append(value)
        except Exception:
            pass

    page.on('response', on_detail)
    try:
        _goto(page, url, wait=3000)
        page.wait_for_timeout(850)
        dsl = page.evaluate(CREATOR_DSL_DETAIL_JS) or {}
        return _creator_detail_from_sources(task_payload, dsl, milestone_payloads)
    finally:
        try:
            page.remove_listener('response', on_detail)
        except Exception:
            pass


def creator_event_detail(directory: Path, url: str, activity_id: str = '', *, force: bool = False) -> dict[str, Any]:
    try:
        parsed = urlsplit(str(url or '').strip())
    except ValueError:
        raise XhsBrowserError('invalid_event_url') from None
    if parsed.scheme != 'https' or not parsed.hostname or not _host_ok(parsed.hostname):
        raise XhsBrowserError('invalid_event_url')
    cache_key = str(activity_id or parsed.path.rstrip('/').rsplit('/', 1)[-1] or '')[:160]
    cache = _read_creator_detail_cache(directory)
    now = int(time.time())
    cached = cache.get(cache_key) if cache_key else None
    if (not force and isinstance(cached, dict) and cached.get('url') == parsed.geturl()
            and now - int(cached.get('at') or 0) < CREATOR_DETAIL_CACHE_TTL
            and isinstance(cached.get('detail'), dict)):
        return {'external_id': str(activity_id or '')[:160], 'url': parsed.geturl(),
                'cached': True, **cached['detail']}

    p, context, page = _launch(directory)
    try:
        detail = _capture_creator_detail(page, parsed.geturl())
    except Exception as exc:
        return {
            'external_id': str(activity_id or '')[:160], 'url': parsed.geturl(), 'cached': False,
            'xhs_detail_status': 'failed', 'xhs_detail_version': CREATOR_DETAIL_CACHE_VERSION,
            'xhs_detail_fetched_at': int(time.time()),
            'xhs_detail_error': (str(exc).strip() or type(exc).__name__)[:120],
            'eligibility': [], 'content_requirements': [], 'prizes': [], 'winning_conditions': [],
            'reward_rules': [], 'required_topics': [], 'submission_spec': {},
            'qualification_state': 'unknown',
            'qualification_basis': '详情读取失败，未对账号参赛资格作结论。',
        }
    finally:
        _close(p, context)
    if cache_key:
        cache[cache_key] = {'at': now, 'url': parsed.geturl(), 'detail': detail}
        if len(cache) > CREATOR_DETAIL_CACHE_MAX:
            cache = dict(sorted(
                cache.items(), key=lambda pair: int((pair[1] or {}).get('at') or 0), reverse=True
            )[:CREATOR_DETAIL_CACHE_MAX])
        _write_creator_detail_cache(directory, cache)
    return {'external_id': str(activity_id or '')[:160], 'url': parsed.geturl(),
            'cached': False, **detail}


CARD_JS = r"""(limit) => {
  const seen = new Set(); const out = [];
  const anchors = Array.from(document.querySelectorAll("a[href*='/explore/'],a[href*='/discovery/item/'],a[href*='/item/']"));
  for (const a of anchors) {
    if (out.length >= limit) break;
    const href = a.href || ''; if (!href || seen.has(href)) continue; seen.add(href);
    let card = a;
    for (let i=0; i<5 && card.parentElement; i++) {
      if ((card.className || '').toString().match(/note|card|feed|cover/i)) break;
      card = card.parentElement;
    }
    const text = (card.innerText || a.innerText || '').trim();
    const titleEl = card.querySelector("[class*='title'],.title,span") || a;
    const authorEl = card.querySelector("[class*='author'],[class*='name'],.author,.name");
    const img = card.querySelector('img');
    out.push({href, title:(titleEl.innerText||titleEl.textContent||'').trim().slice(0,180),
      author:(authorEl?.innerText||authorEl?.textContent||'').trim().slice(0,80),
      cover:img?.src||'', text:text.slice(0,600)});
  }
  return out;
}"""
NOTE_JS = r"""() => {
  const firstText = (sels) => { for (const s of sels) { const e=document.querySelector(s); const t=(e?.innerText||e?.textContent||'').trim(); if(t) return t; } return ''; };
  const title=firstText(['#detail-title','.title','[class*=title]']);
  const body=firstText(['#detail-desc','.desc','.note-text','[class*=desc]','[class*=content]']);
  const author=firstText(['.author-wrapper .name','.author .name','[class*=author] [class*=name]','[class*=user] [class*=name]']);
  const allText=(document.body?.innerText||'').slice(0,10000);
  const images=Array.from(document.querySelectorAll("[class*=note] img,[class*=swiper] img,[class*=carousel] img"))
    .map(x=>x.src||'').filter(Boolean).slice(0,20);
  return {title:title.slice(0,200), body:body.slice(0,10000), author:author.slice(0,100), images, allText};
}"""
MY_NOTES_JS = r"""(limit) => {
  const out=[]; const seen=new Set();
  const cards=Array.from(document.querySelectorAll(".note-card,[class*=note-card],[class*=noteCard]"));
  for(const c of cards) {
    const a=c.querySelector("a[href*='/explore/'],a[href*='/item/'],a[href*='xsec_token']");
    let noteId='';
    try { const raw=JSON.parse(c.getAttribute('data-impression')||'{}'); noteId=raw?.noteTarget?.value?.noteId||raw?.note_id||raw?.id||''; } catch(e) {}
    const key=noteId || a?.href || '';
    if(!key || seen.has(key)) continue;
    seen.add(key);
    const title=(c.querySelector("[class*=title],.title")?.textContent||'').trim();
    const cover=c.querySelector('img')?.src||'';
    out.push({href:a?.href||'', noteId, title:title.slice(0,180), cover, text:(c.innerText||'').slice(0,800)});
    if(out.length >= limit) break;
  }
  return out;
}"""
PROFILE_NOTES_JS = r"""(owner) => {
  const rows=[]; const seen=new Set();
  for(const a of document.querySelectorAll('a[href]')) {
    const u=new URL(a.href);
    const m=u.pathname.match(/^\/user\/profile\/([^/]+)\/([0-9A-Za-z]+)$/);
    if(!m || m[1]!==owner || !u.searchParams.get('xsec_token') || seen.has(m[2])) continue;
    seen.add(m[2]); u.pathname='/explore/'+m[2];
    rows.push({noteId:m[2], href:u.href});
  }
  return rows;
}"""
CREATOR_EVENTS_JS = r"""(limit) => {
  const out=[]; const seen=new Set();
  const nodes=Array.from(document.querySelectorAll("a[href], [class*=event], [class*=activity], [class*=task], [class*=card]"));
  for(const node of nodes) {
    if(out.length>=limit) break;
    const text=(node.innerText||node.textContent||'').replace(/\s+/g,' ').trim();
    if(!text || text.length<4 || !/(活动|征稿|激励|创作|任务|招募|挑战)/.test(text)) continue;
    const a=node.matches?.('a[href]') ? node : node.querySelector?.('a[href]');
    const href=a?.href||'';
    const title=(node.querySelector?.("[class*=title],[class*=name],h1,h2,h3,h4")?.textContent||text.split('  ')[0]||text).trim().slice(0,200);
    const key=(href||title).toLowerCase(); if(!key||seen.has(key)) continue; seen.add(key);
    out.push({title, url:href, text:text.slice(0,1600)});
  }
  return out;
}"""


def _campaign_candidates(value: Any, *, limit: int = 50) -> list[dict[str, Any]]:
    """Extract bounded activity-like rows from creator JSON without depending on one response schema."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    title_keys = ("activity_name", "activityName", "event_name", "eventName", "task_name", "taskName",
                  "title", "name", "subject")
    id_keys = ("activity_id", "activityId", "event_id", "eventId", "task_id", "taskId", "id")
    start_keys = ("start_time", "startTime", "begin_time", "beginTime", "start_at", "startAt")
    end_keys = ("end_time", "endTime", "deadline", "submit_deadline", "submitDeadline", "expire_time", "expireTime")
    url_keys = ("jump_url", "jumpUrl", "url", "link", "h5_url", "h5Url")
    desc_keys = ("description", "desc", "summary", "sub_title", "subTitle")

    def walk(node: Any, depth: int = 0) -> None:
        if len(rows) >= limit or depth > 7:
            return
        if isinstance(node, list):
            for child in node[:200]:
                walk(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        title = next((str(node.get(k) or '').strip() for k in title_keys if str(node.get(k) or '').strip()), '')
        activityish = any(k in node for k in id_keys + start_keys + end_keys) or any(
            word in title for word in ("活动", "征稿", "激励", "创作", "任务", "招募", "挑战")
        )
        if title and activityish and len(title) <= 300:
            external_id = next((str(node.get(k) or '').strip() for k in id_keys if str(node.get(k) or '').strip()), '')
            key = (external_id or title).casefold()
            if key not in seen:
                seen.add(key)
                rows.append({
                    "external_id": external_id[:160],
                    "title": title[:240],
                    "url": next((str(node.get(k) or '').strip() for k in url_keys if str(node.get(k) or '').strip()), '')[:2048],
                    "starts_at": next((str(node.get(k) or '').strip() for k in start_keys if str(node.get(k) or '').strip()), '')[:80],
                    "ends_at": next((str(node.get(k) or '').strip() for k in end_keys if str(node.get(k) or '').strip()), '')[:80],
                    "description": next((str(node.get(k) or '').strip() for k in desc_keys if str(node.get(k) or '').strip()), '')[:3000],
                })
        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child, depth + 1)

    walk(value)
    return rows[:limit]

def _creator_activity_rows(value: Any, *, limit: int = CREATOR_ACTIVITY_MAX) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    data = value.get('data') if isinstance(value.get('data'), dict) else {}
    values = data.get('activity_list') if isinstance(data.get('activity_list'), list) else []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        activity_id = str(raw.get('activity_id') or '').strip()[:160]
        title = str(raw.get('activity_name') or '').strip()[:240]
        if not activity_id or not title or activity_id in seen:
            continue
        seen.add(activity_id)
        topics: list[dict[str, str]] = []
        for topic in raw.get('topic_infos') if isinstance(raw.get('topic_infos'), list) else []:
            if not isinstance(topic, dict):
                continue
            name = str(topic.get('name') or '').strip()[:120]
            if name:
                topics.append({'id': str(topic.get('id') or '')[:100], 'name': name,
                               'link': str(topic.get('link') or '')[:2048]})
        rows.append({
            'external_id': activity_id, 'title': title,
            'url': str(raw.get('activity_link') or '')[:2048],
            'starts_at': raw.get('start_time') or '', 'ends_at': raw.get('end_time') or '',
            'description': str(raw.get('activity_reward') or '')[:1200],
            'promotion_summary': str(raw.get('activity_reward') or '')[:600],
            'reward_summary': _rewardish_summary(str(raw.get('activity_reward') or '')),
            'page_id': str(raw.get('page_id') or '')[:100],
            'instance_id': str(raw.get('instance_id') or '')[:100],
            'publish_url': str(raw.get('pc_post_link') or '')[:2048],
            'activity_status': int(raw.get('activity_status') or 0),
            'topics': topics[:20],
        })
        if len(rows) >= limit:
            break
    return rows

def _creator_task_detail(value: Any, body: str = '') -> dict[str, Any]:
    data = value.get('data') if isinstance(value, dict) and isinstance(value.get('data'), dict) else {}
    tasks = data.get('tasks') if isinstance(data.get('tasks'), list) else []
    topics: list[str] = []
    content_requirements: list[str] = []
    reward_rules: list[str] = []
    prizes: list[str] = []
    account_tasks: list[dict[str, Any]] = []
    post_note = False
    for task in tasks[:100]:
        if not isinstance(task, dict):
            continue
        name = re.sub(r'\s+', ' ', str(task.get('name') or '')).strip()[:240]
        desc = re.sub(r'\s+', ' ', str(task.get('description') or '')).strip()[:240]
        event_type = str(task.get('event_type') or '')[:80]
        finished = bool(task.get('finished'))
        status = task.get('status') if isinstance(task.get('status'), dict) else {}
        account_tasks.append({'name': name, 'event_type': event_type, 'finished': finished,
                              'button_name': str(status.get('button_name') or '')[:80],
                              'progress': task.get('progress_info') if isinstance(task.get('progress_info'), dict) else {}})
        if name and desc:
            rule = f'{name}：{desc}'
            if rule not in reward_rules:
                reward_rules.append(rule)
        if event_type == 'post_note':
            post_note = True
            if name and name not in content_requirements:
                content_requirements.append(name)
            for match in re.findall(r'#([^#\s]+)', name):
                topic = match.strip('，。；;、')[:120]
                if topic and topic not in topics:
                    topics.append(topic)
            extend = task.get('extend_field') if isinstance(task.get('extend_field'), dict) else {}
            raw_topics = extend.get('publish_note_topic')
            if isinstance(raw_topics, str) and raw_topics.strip():
                try:
                    parsed = json.loads(raw_topics)
                except (ValueError, TypeError):
                    parsed = []
                if isinstance(parsed, list):
                    for row in parsed:
                        if isinstance(row, dict):
                            topic = str(row.get('name') or '').strip()[:120]
                            if topic and topic not in topics:
                                topics.append(topic)
        extend = task.get('extend_field') if isinstance(task.get('extend_field'), dict) else {}
        invite = extend.get('booster_invite_card')
        if isinstance(invite, str) and invite.strip():
            try:
                invite = json.loads(invite)
            except (ValueError, TypeError):
                invite = {}
        if isinstance(invite, dict):
            for key in ('title', 'desc'):
                text = re.sub(r'\s+', ' ', str(invite.get(key) or '')).strip()[:300]
                if text and re.search(r'(现金|奖金|奖池|瓜分|奖品|礼包|流量|积分|奖励)', text) and text not in prizes:
                    prizes.append(text)
    completed = sum(1 for row in account_tasks if row.get('finished') is True)
    field_evidence: dict[str, Any] = {}
    if content_requirements:
        field_evidence['content_requirements'] = {
            'source': 'xhs_creator_task_api', 'originals': content_requirements[:30],
        }
    if reward_rules:
        field_evidence['reward_rules'] = {
            'source': 'xhs_creator_task_api', 'originals': reward_rules[:30],
        }
    if topics:
        field_evidence['required_topics'] = {
            'source': 'xhs_creator_task_api', 'originals': topics[:20],
        }
    if post_note:
        field_evidence['submission_spec'] = {
            'source': 'xhs_creator_task_api',
            'originals': ['平台返回 post_note 投稿任务；未据此推断图文/视频格式。'],
        }
    return {
        'eligibility': [],
        'content_requirements': content_requirements[:30], 'required_topics': topics[:20],
        'reward_rules': reward_rules[:30], 'prizes': prizes[:20], 'winning_conditions': [],
        'submission_spec': {'formats': [],
                            'content_directions': [f'围绕 #{topic} 创作' for topic in topics[:12]],
                            'style_requirements': [],
                            'submission_method': '通过活动页带指定话题发布笔记' if post_note else '',
                            'required_mentions': [], 'required_music': []},
        'qualification_state': 'unknown',
        'qualification_basis': (
            '创作者后台向当前账号返回了可执行投稿任务；这只证明任务可见，不代表满足全部参赛或领奖条件。'
            if post_note else
            '活动对当前账号可见，但平台未返回明确的账号参赛资格结论。'
        ),
        'account_task_available': bool(tasks),
        'account_tasks': account_tasks[:50],
        'task_progress': {'total': len(account_tasks), 'completed': completed,
                          'pending': max(0, len(account_tasks) - completed),
                          'score_name': str(data.get('score_name') or '')[:80]},
        'field_evidence': field_evidence,
        'detail_source': 'creator_task_api' if tasks else 'creator_event_page',
        'detail_start_time': data.get('start_time') or '', 'detail_end_time': data.get('end_time') or '',
    }

def _creator_detail_cache_path(directory: Path) -> Path:
    return directory / 'xhs-creator-event-details.json'


def _read_creator_detail_cache(directory: Path) -> dict[str, Any]:
    path = _creator_detail_cache_path(directory)
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or int(value.get('version') or 0) != CREATOR_DETAIL_CACHE_VERSION:
            return {}
        items = value.get('items')
        return items if isinstance(items, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_creator_detail_cache(directory: Path, value: dict[str, Any]) -> None:
    path = _creator_detail_cache_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'version': CREATOR_DETAIL_CACHE_VERSION, 'items': value}, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)

COMMENT_TARGET_COUNT_JS = r"""([id,nickname,content]) => {
  const uniq=[];
  const push=(e)=>{ if(e && !uniq.includes(e)) uniq.push(e); };
  if(id) {
    for(const e of document.querySelectorAll('[data-comment-id],[data-id],[id]')) {
      if(e.getAttribute('data-comment-id')===id || e.getAttribute('data-id')===id || e.id===id) push(e);
    }
  }
  if(!uniq.length && nickname) {
    for(const a of document.querySelectorAll('.author,[class*=author]')) {
      if((a.textContent||'').trim()!==nickname) continue;
      let c=a.parentElement;
      for(let i=0;i<6 && c;i++,c=c.parentElement) {
        const t=(c.innerText||c.textContent||'').trim();
        if(!content || t.includes(content)) { push(c); break; }
      }
    }
  }
  return uniq.length;
}"""
COMMENT_TARGET_JS = r"""([id,nickname,content]) => {
  const uniq=[];
  const push=(e)=>{ if(e && !uniq.includes(e)) uniq.push(e); };
  if(id) {
    for(const e of document.querySelectorAll('[data-comment-id],[data-id],[id]')) {
      if(e.getAttribute('data-comment-id')===id || e.getAttribute('data-id')===id || e.id===id) push(e);
    }
  }
  if(!uniq.length && nickname) {
    for(const a of document.querySelectorAll('.author,[class*=author]')) {
      if((a.textContent||'').trim()!==nickname) continue;
      let c=a.parentElement;
      for(let i=0;i<6 && c;i++,c=c.parentElement) {
        const t=(c.innerText||c.textContent||'').trim();
        if(!content || t.includes(content)) { push(c); break; }
      }
    }
  }
  return uniq.length===1 ? uniq[0] : null;
}"""
VISIBLE_TEXT_JS = r"""(text) => Array.from(document.querySelectorAll('body *')).some(e => {
  if(e.matches('textarea,input,[contenteditable=true]') || e.children.length) return false;
  const s=(e.textContent||'').trim(); const st=getComputedStyle(e); return s===text && st.display!=='none' && st.visibility!=='hidden';
})"""


class XhsBrowserError(RuntimeError):
    pass


def _host_ok(host: str) -> bool:
    host = host.lower().rstrip('.')
    return any(host == root or host.endswith('.' + root) for root in XHS_HOSTS)


def parse_note_url(value: str) -> tuple[str, str, str]:
    try:
        parsed = urlsplit(str(value or '').strip())
    except ValueError:
        raise XhsBrowserError('invalid_note_url') from None
    if parsed.scheme != 'https' or not parsed.hostname or not _host_ok(parsed.hostname) or parsed.username or parsed.password or parsed.fragment:
        raise XhsBrowserError('invalid_note_url')
    match = NOTE_PATH_RE.search(parsed.path)
    if not match:
        raise XhsBrowserError('invalid_note_url')
    token = (parse_qs(parsed.query).get('xsec_token') or [''])[0]
    return match.group(1), token[:2048], parsed.geturl()[:4096]


def public_note_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ''
    if parsed.scheme != 'https' or not parsed.hostname or not _host_ok(parsed.hostname):
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:2048]

def _safe_creator_page(value: str) -> str:
    """Return only origin + path for diagnostics; never keep query/fragment."""
    try:
        parsed = urlsplit(str(value or ''))
    except ValueError:
        return ''
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:512]


def _risk(page) -> None:
    url = page.url or ''
    body = ''
    try:
        body = (page.locator('body').inner_text(timeout=1000) or '')[:3000]
    except Exception:
        pass
    if 'error_code=300012' in url or '/captcha' in url or 'NeedVerify' in body or '验证' in body and '安全' in body:
        raise XhsBrowserError('risk_control')
    if '登录' in body and ('扫码' in body or '手机号' in body) and 'login' in url.lower():
        raise XhsBrowserError('login_required')
    login = page.locator('.login-container').first
    if login.count() and login.is_visible():
        raise XhsBrowserError('login_required')


def _metric_number(text: str, labels: tuple[str, ...]) -> int | None:
    for label in labels:
        m = re.search(rf"(?:{re.escape(label)})\s*([0-9]+(?:\.[0-9]+)?)(万|w|W|亿)?", text)
        if not m:
            m = re.search(rf"([0-9]+(?:\.[0-9]+)?)(万|w|W|亿)?\s*(?:{re.escape(label)})", text)
        if m:
            value = float(m.group(1)); unit = m.group(2) or ''
            if unit in {'万','w','W'}: value *= 10000
            elif unit == '亿': value *= 100000000
            return int(value)
    return None


def _metrics(text: str) -> dict[str, int | None]:
    return {
        'views': _metric_number(text, ('浏览','观看','曝光')),
        'likes': _metric_number(text, ('点赞','赞')),
        'collects': _metric_number(text, ('收藏',)),
        'comments': _metric_number(text, ('评论',)),
        'shares': _metric_number(text, ('分享','转发')),
    }


def _note_id(href: str, fallback: str = '') -> str:
    try:
        return parse_note_url(href)[0]
    except XhsBrowserError:
        return str(fallback or '')[:100]


def _card(item: dict[str, Any], scope: str) -> tuple[dict[str, Any], tuple[str, str] | None]:
    href = str(item.get('href') or '')
    note_id = _note_id(href)
    title = str(item.get('title') or '').strip() or '(无标题)'
    text = str(item.get('text') or '')
    result = {
        'note_id': note_id, 'title': title[:180], 'author': str(item.get('author') or '')[:80],
        'url': public_note_url(href), 'scope': scope, 'metrics': _metrics(text),
    }
    locator = (note_id, href[:4096]) if note_id and href else None
    return result, locator


def _launch(directory: Path, *, headed: bool = False):
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    profile = directory / 'browser' / 'XiaohongshuProfile'
    profile.mkdir(parents=True, exist_ok=True)
    try:
        context = p.chromium.launch_persistent_context(
            str(profile), headless=not headed, locale='zh-CN',
            args=['--no-first-run','--no-default-browser-check'], chromium_sandbox=True,
        )
    except Exception:
        p.stop(); raise
    page = context.pages[0] if context.pages else context.new_page()
    return p, context, page


def _close(p, context) -> None:
    try: context.close()
    finally: p.stop()


def _goto(page, url: str, *, wait: int = 1400) -> None:
    page.goto(url, wait_until='domcontentloaded', timeout=30000)
    page.wait_for_timeout(wait)
    _risk(page)


def _read_cards(directory: Path, url: str, scope: str, limit: int) -> dict[str, Any]:
    p, context, page = _launch(directory)
    try:
        _goto(page, url, wait=1800)
        for _ in range(2):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(700)
        raw = page.evaluate(CARD_JS, max(1, min(limit, 30))) or []
        items, locators = [], []
        for raw_item in raw:
            item, locator = _card(raw_item if isinstance(raw_item, dict) else {}, scope)
            if item['note_id'] or item['title'] != '(无标题)': items.append(item)
            if locator: locators.append({'note_id': locator[0], 'url': locator[1]})
        return {'items': items[:limit], '_locators': locators[:limit]}
    finally:
        _close(p, context)


def feed(directory: Path, limit: int) -> dict[str, Any]:
    return _read_cards(directory, 'https://www.xiaohongshu.com/explore', 'recommended_feed', limit)


def search(directory: Path, query: str, limit: int) -> dict[str, Any]:
    query = str(query or '').strip()
    if not query or len(query) > 100:
        raise XhsBrowserError('invalid_query')
    return _read_cards(directory, 'https://www.xiaohongshu.com/search_result?keyword=' + quote(query), 'keyword_search', limit)


def account_notes(directory: Path, limit: int, expected_account_remote_id: str = '') -> dict[str, Any]:
    p, context, page = _launch(directory)
    try:
        from .xhs_reply import IDENTITY_JS
        _goto(page, 'https://www.xiaohongshu.com/explore', wait=1600)
        if not expected_account_remote_id:
            raise XhsBrowserError('account_identity_missing')
        if page.evaluate(IDENTITY_JS) != [expected_account_remote_id]:
            raise XhsBrowserError('account_mismatch')
        _goto(page, 'https://creator.xiaohongshu.com/new/note-manager', wait=2200)
        for _ in range(2):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); page.wait_for_timeout(700)
        raw = page.evaluate(MY_NOTES_JS, max(1, min(limit, 30))) or []
        # 创作者卡片可能没有可打开的链接，从本人主页补充平台给出的完整定位链接。
        if any(not row.get('href') for row in raw):
            _goto(page, f'https://www.xiaohongshu.com/user/profile/{expected_account_remote_id}', wait=1800)
            public_rows = page.evaluate(CARD_JS, 100) or []
            links = {_note_id(str(item.get('href') or '')): item for item in public_rows if isinstance(item, dict)}
            for item in page.evaluate(PROFILE_NOTES_JS, expected_account_remote_id) or []:
                links[item['noteId']] = item
            for row in raw:
                if not row.get('href'):
                    matched = links.get(str(row.get('noteId') or '')) or {}
                    row['href'] = matched.get('href', '')
        items, locators = [], []
        seen = set()
        for row in raw:
            href = str((row or {}).get('href') or '')
            nid = _note_id(href, str((row or {}).get('noteId') or ''))
            if not nid or nid in seen or not href:
                continue
            seen.add(nid)
            text = str((row or {}).get('text') or '')
            item = {'note_id': nid, 'title': str((row or {}).get('title') or '(无标题)')[:180],
                    'url': public_note_url(href), 'scope': 'account_notes', 'metrics': _metrics(text)}
            items.append(item)
            if nid and href: locators.append({'note_id': nid, 'url': href[:4096]})
        return {'items': items[:limit], '_locators': locators[:limit]}
    finally:
        _close(p, context)


def _creator_activity_order(value: Any, *, limit: int = CREATOR_ACTIVITY_MAX) -> dict[str, Any]:
    data = value.get('data') if isinstance(value, dict) and isinstance(value.get('data'), dict) else {}
    values = data.get('activity_list') if isinstance(data.get('activity_list'), list) else []
    ids: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, dict):
            continue
        activity_id = str(raw.get('activity_id') or '').strip()[:160]
        if activity_id and activity_id not in seen:
            seen.add(activity_id)
            ids.append(activity_id)
    cap = max(1, min(int(limit), CREATOR_ACTIVITY_MAX))
    return {
        'activity_ids': ids[:cap],
        'raw_count': len(values),
        'unique_count': len(ids),
        'truncated': len(ids) > cap,
    }


def creator_events(directory: Path, limit: int = CREATOR_ACTIVITY_MAX, detail_limit: int = 12) -> dict[str, Any]:
    p, context, page = _launch(directory)
    activity_payloads: dict[str, dict[str, Any]] = {}
    sort_errors: dict[str, str] = {}
    response_diag = {
        'list_seen': 0, 'query_mismatch': 0, 'accepted': 0,
        'non_json': 0, 'invalid_payload': 0, 'parse_errors': 0,
        'http_statuses': [], 'blocking_status': 0,
    }
    try:
        def on_response(response):
            try:
                parsed = urlsplit(response.url)
                if parsed.hostname != 'creator.xiaohongshu.com' or parsed.path != '/api/galaxy/v2/creator/activity_center/list':
                    return
                response_diag['list_seen'] += 1
                status = int(response.status or 0)
                query = parse_qs(parsed.query)
                sort_value = (query.get('sort') or [''])[0]
                sort_name = 'default' if sort_value == '1' else 'latest' if sort_value == '2' else ''
                if status != 200:
                    statuses = response_diag['http_statuses']
                    if status not in statuses and len(statuses) < 6:
                        statuses.append(status)
                    if status in {401, 403, 429}:
                        response_diag['blocking_status'] = status
                    sort_errors[sort_name or 'endpoint'] = f'http_{status}'
                    return
                if any(query.get(key) != [value] for key, value in (
                    ('type', '1'), ('source', '3'), ('topic_activity', '0'),
                )) or not sort_name:
                    response_diag['query_mismatch'] += 1
                    return
                if 'json' not in (response.headers.get('content-type') or '').lower():
                    response_diag['non_json'] += 1
                    sort_errors[sort_name] = 'non_json_response'
                    return
                value = response.json()
                data = value.get('data') if isinstance(value, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get('activity_list'), list):
                    response_diag['invalid_payload'] += 1
                    sort_errors[sort_name] = 'invalid_activity_payload'
                    return
                if value.get('success') is False:
                    sort_errors[sort_name] = 'api_unsuccessful'
                    return
                activity_payloads[sort_name] = value
                response_diag['accepted'] += 1
                sort_errors.pop(sort_name, None)
            except Exception:
                response_diag['parse_errors'] += 1

        def wait_activity_payload(sort_name: str, wait_ms: int) -> None:
            deadline = time.monotonic() + max(0, wait_ms) / 1000
            while (
                sort_name not in activity_payloads
                and not response_diag['blocking_status']
                and time.monotonic() < deadline
            ):
                page.wait_for_timeout(250)

        def navigation_failure(stage: str, exc: Exception) -> dict[str, Any]:
            error_type = type(exc).__name__[:80]
            code = 'navigation_timeout' if 'timeout' in error_type.lower() else 'navigation_error'
            return {
                'items': [], 'source': 'creator_events_dom',
                'page_url': 'https://creator.xiaohongshu.com/new/events',
                'api_observed': False, 'raw_count': 0, 'listed_count': 0,
                'orders': {}, 'detail_count': 0, 'detail_fetched': 0,
                'diagnostics': {
                    'code': code, 'stage': stage, 'error_type': error_type,
                    'final_page': _safe_creator_page(getattr(page, 'url', '')),
                    'body_state': 'unknown',
                    'api_list_responses_seen': int(response_diag['list_seen']),
                    'api_responses_accepted': int(response_diag['accepted']),
                    'query_mismatch_count': int(response_diag['query_mismatch']),
                    'non_json_count': int(response_diag['non_json']),
                    'invalid_payload_count': int(response_diag['invalid_payload']),
                    'parse_error_count': int(response_diag['parse_errors']),
                    'http_statuses': [int(v) for v in response_diag['http_statuses'][:6]],
                    'sort_errors': {key: str(value)[:80] for key, value in sort_errors.items()},
                },
            }

        def click_visible_text(label: str) -> bool:
            try:
                matches = page.get_by_text(label, exact=True)
                for index in range(matches.count()):
                    candidate = matches.nth(index)
                    if candidate.is_visible():
                        candidate.click(timeout=3000)
                        return True
            except Exception:
                return False
            return False

        page.on('response', on_response)
        try:
            _goto(page, 'https://creator.xiaohongshu.com/new/events', wait=1600)
        except XhsBrowserError:
            raise
        except Exception as exc:
            return navigation_failure('initial_navigation', exc)
        wait_activity_payload('default', 6000)
        if 'default' not in activity_payloads and response_diag['list_seen'] == 0:
            try:
                page.reload(wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(1800)
                _risk(page)
            except XhsBrowserError:
                raise
            except Exception as exc:
                return navigation_failure('reload', exc)
            wait_activity_payload('default', 6000)
        if 'default' not in activity_payloads and response_diag['list_seen'] == 0:
            page.wait_for_timeout(2500)

        body = ''
        try:
            body = (page.locator('body').inner_text(timeout=1500) or '')[:5000]
        except Exception:
            pass
        if 'login' in (page.url or '').lower() or ('登录' in body and ('扫码' in body or '手机号' in body)):
            raise XhsBrowserError('login_required')

        if 'default' in activity_payloads:
            try:
                if click_visible_text('默认排序'):
                    page.wait_for_timeout(350)
                    if click_visible_text('最新排序'):
                        wait_activity_payload('latest', 4500)
                    else:
                        sort_errors.setdefault('latest', 'latest_sort_option_missing')
                else:
                    sort_errors.setdefault('latest', 'default_sort_control_missing')
            except Exception:
                sort_errors.setdefault('latest', 'latest_sort_switch_failed')
        try: page.remove_listener('response', on_response)
        except Exception: pass

        cap = max(1, min(limit, CREATOR_ACTIVITY_MAX))
        default_payload = activity_payloads.get('default')
        latest_payload = activity_payloads.get('latest')
        api_observed = bool(activity_payloads)
        default_order = _creator_activity_order(default_payload, limit=cap) if default_payload else {
            'activity_ids': [], 'raw_count': 0, 'unique_count': 0, 'truncated': False,
        }
        latest_order = _creator_activity_order(latest_payload, limit=cap) if latest_payload else {
            'activity_ids': [], 'raw_count': 0, 'unique_count': 0, 'truncated': False,
        }
        observed_at = int(time.time())
        orders = {
            'default': {**default_order, 'observed': bool(default_payload), 'sort': 1,
                        'query': {'sort': '1', 'type': '1', 'source': '3', 'topic_activity': '0'},
                        'observed_at': observed_at, 'error': str(sort_errors.get('default') or '')[:120]},
            'latest': {**latest_order, 'observed': bool(latest_payload), 'sort': 2,
                       'query': {'sort': '2', 'type': '1', 'source': '3', 'topic_activity': '0'},
                       'observed_at': observed_at, 'error': str(sort_errors.get('latest') or '')[:120]},
        }

        raw_count = int(default_order.get('raw_count') or latest_order.get('raw_count') or 0)
        items: list[dict[str, Any]] = []
        if default_payload:
            items.extend(_creator_activity_rows(default_payload, limit=cap))
        if latest_payload:
            seen_ids = {str(item.get('external_id') or '') for item in items}
            for row in _creator_activity_rows(latest_payload, limit=cap):
                external_id = str(row.get('external_id') or '')
                if external_id and external_id not in seen_ids:
                    items.append(row)
                    seen_ids.add(external_id)
                    if len(items) >= cap:
                        break
        source = 'creator_activity_center_api' if api_observed else 'creator_events_dom'
        if not items:
            raw = page.evaluate(CREATOR_EVENTS_JS, max(1, min(limit, 50))) or []
            if raw:
                source = 'creator_events_dom'
            for row in raw:
                if not isinstance(row, dict):
                    continue
                title = str(row.get('title') or '').strip()[:240]
                if not title:
                    continue
                items.append({'external_id': '', 'title': title, 'url': str(row.get('url') or '')[:2048],
                              'starts_at': '', 'ends_at': '', 'description': str(row.get('text') or '')[:3000]})

        detail_cache = _read_creator_detail_cache(directory)
        cache_changed = False
        now = int(time.time())
        detail_count = 0
        detail_fetched = 0
        fetch_budget = max(0, min(int(detail_limit), 20))
        for item in items:
            cache_key = str(item.get('external_id') or item.get('page_id') or '')[:160]
            url = str(item.get('url') or '')
            cached = detail_cache.get(cache_key) if cache_key else None
            if (isinstance(cached, dict) and cached.get('url') == url
                    and now - int(cached.get('at') or 0) < CREATOR_DETAIL_CACHE_TTL
                    and isinstance(cached.get('detail'), dict)):
                item.update(cached['detail'])
                detail_count += 1
                continue
            if detail_fetched >= fetch_budget:
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme != 'https' or not parsed.hostname or not _host_ok(parsed.hostname):
                continue
            try:
                detail = _capture_creator_detail(page, url)
                item.update(detail)
                detail_count += 1
                if cache_key:
                    detail_cache[cache_key] = {'at': now, 'url': url, 'detail': detail}
                    cache_changed = True
            except Exception:
                item.update({
                    'xhs_detail_status': 'failed',
                    'xhs_detail_version': CREATOR_DETAIL_CACHE_VERSION,
                    'xhs_detail_fetched_at': int(time.time()),
                    'xhs_detail_error': 'detail_read_failed',
                })
            detail_fetched += 1
        if cache_changed:
            if len(detail_cache) > CREATOR_DETAIL_CACHE_MAX:
                detail_cache = dict(sorted(detail_cache.items(), key=lambda pair: int((pair[1] or {}).get('at') or 0), reverse=True)[:CREATOR_DETAIL_CACHE_MAX])
            _write_creator_detail_cache(directory, detail_cache)
        listed = items[:max(1, min(limit, CREATOR_ACTIVITY_MAX))]
        statuses = [int(v) for v in response_diag['http_statuses'][:6]]
        if response_diag['blocking_status']:
            diagnostic_code = f"http_{int(response_diag['blocking_status'])}"
        elif statuses:
            diagnostic_code = f"http_{statuses[0]}"
        elif response_diag['non_json']:
            diagnostic_code = 'non_json_response'
        elif response_diag['invalid_payload']:
            diagnostic_code = 'invalid_activity_payload'
        elif response_diag['list_seen'] and response_diag['query_mismatch'] >= response_diag['list_seen']:
            diagnostic_code = 'query_mismatch'
        elif not api_observed and not listed:
            diagnostic_code = 'api_not_observed_dom_empty'
        else:
            diagnostic_code = ''
        diagnostics = {
            'code': diagnostic_code,
            'stage': 'activity_list',
            'error_type': '',
            'final_page': _safe_creator_page(page.url),
            'body_state': 'nonempty' if body.strip() else 'blank',
            'api_list_responses_seen': int(response_diag['list_seen']),
            'api_responses_accepted': int(response_diag['accepted']),
            'query_mismatch_count': int(response_diag['query_mismatch']),
            'non_json_count': int(response_diag['non_json']),
            'invalid_payload_count': int(response_diag['invalid_payload']),
            'parse_error_count': int(response_diag['parse_errors']),
            'http_statuses': statuses,
            'sort_errors': {key: str(value)[:80] for key, value in sort_errors.items()},
        }
        return {'items': listed, 'source': source,
                'page_url': 'https://creator.xiaohongshu.com/new/events',
                'api_observed': api_observed, 'raw_count': raw_count, 'listed_count': len(listed),
                'orders': orders, 'detail_count': detail_count, 'detail_fetched': detail_fetched,
                'diagnostics': diagnostics}
    finally:
        _close(p, context)


def note_detail(directory: Path, url: str) -> dict[str, Any]:
    note_id, _, locator = parse_note_url(url)
    p, context, page = _launch(directory)
    try:
        _goto(page, locator, wait=2200)
        raw = page.evaluate(NOTE_JS) or {}
        text = str(raw.get('allText') or '')
        return {'note': {
            'note_id': note_id, 'title': str(raw.get('title') or '(无标题)')[:200],
            'body': str(raw.get('body') or '')[:10000], 'author': str(raw.get('author') or '')[:100],
            'url': public_note_url(locator), 'scope': 'note_detail', 'metrics': _metrics(text),
            'images': [str(x)[:2048] for x in (raw.get('images') or []) if str(x).startswith('https://')][:20],
        }, '_locators': [{'note_id': note_id, 'url': locator}]}
    finally:
        _close(p, context)


def comments(directory: Path, url: str, limit: int, expected_account_remote_id: str = '') -> dict[str, Any]:
    note_id, _, locator = parse_note_url(url)
    import xhs_comment
    p, context, page = _launch(directory)
    raw_pages: list[Any] = []
    try:
        def on_response(response):
            if 'comment/page' in response.url and parse_qs(urlsplit(response.url).query).get('note_id', [''])[0] == note_id:
                try:
                    value = response.json()
                    if response.status == 200 and isinstance(value, dict) and value.get('success') is not False and isinstance((value.get('data') or {}).get('comments'), list):
                        raw_pages.append(value)
                except Exception:
                    pass
        page.on('response', on_response)
        _goto(page, locator, wait=1600)
        from .xhs_reply import verify_owner
        verify_owner(page, note_id, expected_account_remote_id)
        for _ in range(3):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); page.wait_for_timeout(700)
        _risk(page)
        if not raw_pages:
            raise XhsBrowserError('comments_sync_unconfirmed')
        rows = xhs_comment._collect_comments(raw_pages, max(1, min(limit, 100)))
        clean = [{
            'id': str(row.get('id') or '')[:100], 'nickname': str(row.get('nickname') or '')[:80],
            'content': str(row.get('content') or '')[:2000], 'time': int(row.get('time') or 0),
            'time_str': str(row.get('time_str') or '')[:40], 'like': str(row.get('like') or '')[:40],
            'parent': str(row.get('parent') or '')[:100],
        } for row in rows]
        return {'note_id': note_id, 'comments': clean, 'count': len(clean), '_locators': [{'note_id': note_id, 'url': locator}]}
    finally:
        _close(p, context)


def _target(page, target: dict[str, Any]):
    cid = str(target.get('id') or '')[:100]
    if cid:
        from .xhs_reply import TARGET_JS
        element = page.evaluate_handle(TARGET_JS, cid).as_element()
        return (element, '') if element else (None, 'target_not_found_or_ambiguous')
    nickname = str(target.get('nickname') or '')[:80]
    content = str(target.get('content') or '')[:500]
    count = int(page.evaluate(COMMENT_TARGET_COUNT_JS, [cid, nickname, content]) or 0)
    if count == 0: return None, 'target_not_found'
    if count != 1: return None, 'target_ambiguous'
    handle = page.evaluate_handle(COMMENT_TARGET_JS, [cid, nickname, content])
    return handle.as_element(), ''


def _input(page):
    for selector in ("[contenteditable='true']", "[placeholder*='回复']", "[placeholder*='说点']", 'textarea'):
        item = page.locator(selector).first
        try:
            if item.count() and item.is_visible(): return item
        except Exception: pass
    return None


def _fill(locator, text: str) -> None:
    try: locator.fill(text)
    except Exception:
        locator.click(); locator.press('Control+A'); locator.press('Backspace'); locator.type(text)


def _verify_text(page, text: str) -> bool:
    try: return bool(page.evaluate(VISIBLE_TEXT_JS, text))
    except Exception: return False


def delete_comments(directory: Path, url: str, targets: list[dict[str, Any]], *, expected_account_remote_id: str = '', submission_file: str = '') -> dict[str, Any]:
    from .xhs_reply import verify_owner, TARGET_JS
    from .browser_submission import mark_submission
    note_id, _, locator = parse_note_url(url)
    p, context, page = _launch(directory, headed=False)
    results=[]
    try:
        _goto(page, locator, wait=1800)
        verify_owner(page, note_id, expected_account_remote_id)
        for target in targets[:20]:
            submitted = False
            container, reason = _target(page, target)
            if not container:
                results.append({'id': str(target.get('id') or ''), 'status':'not_submitted','reason':reason}); continue
            try:
                container.hover(); page.wait_for_timeout(250)
                more = None
                for sel in ("[class*='more']","[class*='dots']","[class*='operation']"):
                    try:
                        el=container.query_selector(sel)
                        if el and el.is_visible(): more=el; break
                    except Exception: pass
                if not more:
                    results.append({'id':str(target.get('id') or ''),'status':'not_submitted','reason':'delete_control_missing'}); continue
                more.click(); page.wait_for_timeout(300)
                menu = page.get_by_text('删除评论', exact=True).last
                if not menu.count(): menu = page.get_by_text('删除', exact=True).last
                if not menu.count():
                    results.append({'id':str(target.get('id') or ''),'status':'not_submitted','reason':'delete_menu_missing'}); continue
                verify_owner(page, note_id, expected_account_remote_id)
                mark_submission(submission_file, {'target_id': note_id, 'comment_id': target['id'], 'account_remote_id': expected_account_remote_id})
                submitted = True
                menu.click(); page.wait_for_timeout(250)
                for text in ('确定','确认','删除'):
                    try:
                        btn=page.get_by_text(text, exact=True).last
                        if btn.count() and btn.is_visible(): btn.click(); break
                    except Exception: pass
                page.wait_for_timeout(1000)
                remaining = page.evaluate_handle(TARGET_JS, str(target.get('id') or '')).as_element()
                results.append({'id':str(target.get('id') or ''),'status':'verified' if remaining is None else 'unknown_result','reason':''})
            except Exception as exc:
                results.append({'id':str(target.get('id') or ''),'status':'unknown_result' if submitted or isinstance(exc, FileExistsError) else 'not_submitted','reason':'interaction_error'})
        return {'results':results}
    finally:_close(p, context)


def post_comment(directory: Path, url: str, text: str, *, expected_account_remote_id: str = '', submission_file: str = '') -> dict[str, Any]:
    from .xhs_reply import receipt_evidence, verify_owner
    from .browser_submission import mark_submission
    note_id, _, locator = parse_note_url(url); text=str(text or '').strip()
    if not text or len(text)>1000: raise XhsBrowserError('invalid_comment')
    p, context, page=_launch(directory, headed=False)
    submitted = False
    try:
        _goto(page, locator, wait=1800)
        verify_owner(page, note_id, expected_account_remote_id)
        # 平台默认折叠评论框，先展开后再定位可编辑内容。
        trigger = page.locator('[class*="not-active"]').first
        if trigger.count() and trigger.is_visible():
            trigger.click()
            page.wait_for_timeout(300)
        inp = _input(page)
        if not inp:return {'status':'not_submitted','reason':'comment_input_missing'}
        _fill(inp,text)
        buttons = page.get_by_role('button', name='发送', exact=True)
        visible = [buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()]
        if len(visible) != 1:return {'status':'not_submitted','reason':'send_control_missing'}
        verify_owner(page, note_id, expected_account_remote_id)
        mark_submission(submission_file, {'target_id': note_id, 'account_remote_id': expected_account_remote_id})
        with page.expect_response(lambda response: urlsplit(response.url).path == '/api/sns/web/v1/comment/post'
                                  and response.request.method == 'POST', timeout=8000) as pending:
            submitted = True
            visible[0].click()
        evidence = receipt_evidence(pending.value, note_id, '', expected_account_remote_id, text)
        return {'status':'verified' if evidence else 'unknown_result', 'evidence': evidence or {},
                'reason': '' if evidence else '缺少平台评论回执，请核对后处理。'}
    except Exception as exc:
        return {'status': 'unknown_result' if submitted or isinstance(exc, FileExistsError) else 'not_submitted',
                'reason': '提交结果待核对。' if submitted or isinstance(exc, FileExistsError) else '提交前检查失败，本条未发送。'}
    finally:_close(p,context)


def run(action: str, directory: Path, params: dict[str, Any]) -> dict[str, Any]:
    limit=max(1,min(int(params.get('limit') or 12),100))
    if action=='feed': return feed(directory,limit)
    if action=='search': return search(directory,str(params.get('query') or ''),limit)
    if action=='notes': return account_notes(directory,limit,str(params.get('expected_account_remote_id') or ''))
    if action=='events':
        raw_event_limit = params.get('limit')
        raw_detail_limit = params.get('detail_limit')
        event_limit=max(1,min(int(CREATOR_ACTIVITY_MAX if raw_event_limit is None else raw_event_limit),CREATOR_ACTIVITY_MAX))
        detail_limit=max(0,min(int(12 if raw_detail_limit is None else raw_detail_limit),20))
        return creator_events(directory,event_limit,detail_limit)
    if action=='event_detail':
        return creator_event_detail(
            directory, str(params.get('url') or ''), str(params.get('activity_id') or ''),
            force=bool(params.get('force')),
        )
    if action=='note': return note_detail(directory,str(params.get('url') or ''))
    if action=='comments': return comments(directory,str(params.get('url') or ''),limit,str(params.get('expected_account_remote_id') or ''))
    if action=='reply':
        from .xhs_reply import reply
        return reply(directory, str(params.get('url') or ''), list(params.get('items') or []),
                     expected_account_remote_id=str(params.get('expected_account_remote_id') or ''),
                     submission_file=str(params.get('submission_file') or ''))
    if action=='delete': return delete_comments(directory,str(params.get('url') or ''),list(params.get('items') or []),
        expected_account_remote_id=str(params.get('expected_account_remote_id') or ''), submission_file=str(params.get('submission_file') or ''))
    if action=='comment': return post_comment(directory,str(params.get('url') or ''),str(params.get('text') or ''),
        expected_account_remote_id=str(params.get('expected_account_remote_id') or ''), submission_file=str(params.get('submission_file') or ''))
    raise XhsBrowserError('unsupported_action')
