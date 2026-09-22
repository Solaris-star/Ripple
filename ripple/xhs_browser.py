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
  const out=[];
  const cards=Array.from(document.querySelectorAll(".note-card,[class*=note-card],[class*=noteCard]"));
  for(const c of cards.slice(0,limit)) {
    const a=c.querySelector("a[href*='/explore/'],a[href*='/item/'],a[href*='xsec_token']");
    let noteId='';
    try { const raw=JSON.parse(c.getAttribute('data-impression')||'{}'); noteId=raw?.noteTarget?.value?.noteId||raw?.note_id||raw?.id||''; } catch(e) {}
    const title=(c.querySelector("[class*=title],.title")?.textContent||'').trim();
    const cover=c.querySelector('img')?.src||'';
    out.push({href:a?.href||'', noteId, title:title.slice(0,180), cover, text:(c.innerText||'').slice(0,800)});
  }
  return out;
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

def _creator_activity_rows(value: Any, *, limit: int = 50) -> list[dict[str, Any]]:
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
            'reward_summary': str(raw.get('activity_reward') or '')[:600],
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
    enabled = int(data.get('enabled') or 0) == 1 and int(data.get('status') or 0) == 1
    qualification_state = 'eligible' if enabled and post_note else 'unknown'
    return {
        'eligibility': [],
        'content_requirements': content_requirements[:30], 'required_topics': topics[:20],
        'reward_rules': reward_rules[:30], 'prizes': prizes[:20], 'winning_conditions': [],
        'submission_spec': {'formats': ['image_text', 'video'] if post_note else [],
                            'content_directions': [f'围绕 #{topic} 创作' for topic in topics[:12]],
                            'style_requirements': [],
                            'submission_method': '通过活动页带指定话题发布笔记' if post_note else '',
                            'required_mentions': [], 'required_music': []},
        'qualification_state': qualification_state,
        'qualification_basis': '创作者后台已向当前账号返回可执行投稿任务。' if qualification_state == 'eligible' else '活动对当前账号可见，但平台未返回明确的账号参赛资格结论。',
        'account_tasks': account_tasks[:50],
        'task_progress': {'total': len(account_tasks), 'completed': completed,
                          'pending': max(0, len(account_tasks) - completed),
                          'score_name': str(data.get('score_name') or '')[:80]},
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
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_creator_detail_cache(directory: Path, value: dict[str, Any]) -> None:
    path = _creator_detail_cache_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
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


def account_notes(directory: Path, limit: int) -> dict[str, Any]:
    p, context, page = _launch(directory)
    try:
        _goto(page, 'https://creator.xiaohongshu.com/new/note-manager', wait=2200)
        for _ in range(2):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); page.wait_for_timeout(700)
        raw = page.evaluate(MY_NOTES_JS, max(1, min(limit, 30))) or []
        items, locators = [], []
        for row in raw:
            href = str((row or {}).get('href') or '')
            nid = _note_id(href, str((row or {}).get('noteId') or ''))
            text = str((row or {}).get('text') or '')
            item = {'note_id': nid, 'title': str((row or {}).get('title') or '(无标题)')[:180],
                    'url': public_note_url(href), 'scope': 'account_notes', 'metrics': _metrics(text)}
            items.append(item)
            if nid and href: locators.append({'note_id': nid, 'url': href[:4096]})
        return {'items': items[:limit], '_locators': locators[:limit]}
    finally:
        _close(p, context)


def creator_events(directory: Path, limit: int = 30, detail_limit: int = 8) -> dict[str, Any]:
    p, context, page = _launch(directory)
    activity_payload: dict[str, Any] | None = None
    try:
        def on_response(response):
            nonlocal activity_payload
            if '/creator/activity_center/list' not in response.url:
                return
            try:
                if 'json' not in (response.headers.get('content-type') or '').lower():
                    return
                value = response.json()
                if isinstance(value, dict) and isinstance(value.get('data'), dict) and isinstance(value['data'].get('activity_list'), list):
                    activity_payload = value
            except Exception:
                pass

        page.on('response', on_response)
        _goto(page, 'https://creator.xiaohongshu.com/new/events', wait=2600)
        try: page.remove_listener('response', on_response)
        except Exception: pass
        body = ''
        try:
            body = (page.locator('body').inner_text(timeout=1500) or '')[:5000]
        except Exception:
            pass
        if 'login' in (page.url or '').lower() or ('登录' in body and ('扫码' in body or '手机号' in body)):
            raise XhsBrowserError('login_required')

        raw_count = 0
        items: list[dict[str, Any]] = []
        if activity_payload:
            values = ((activity_payload.get('data') or {}).get('activity_list') or [])
            raw_count = len(values) if isinstance(values, list) else 0
            items = _creator_activity_rows(activity_payload, limit=max(1, min(limit, 50)))
        source = 'creator_activity_center_api'
        if not items:
            source = 'creator_events_dom'
            raw = page.evaluate(CREATOR_EVENTS_JS, max(1, min(limit, 50))) or []
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
            task_payload: dict[str, Any] | None = None
            def on_detail(response):
                nonlocal task_payload
                if 'preview_task_list' not in response.url:
                    return
                try:
                    value = response.json()
                    if isinstance(value, dict):
                        task_payload = value
                except Exception:
                    pass
            page.on('response', on_detail)
            try:
                _goto(page, url, wait=3000)
                if task_payload:
                    detail = _creator_task_detail(task_payload)
                    item.update(detail)
                    detail_count += 1
                    if cache_key and detail.get('detail_source') == 'creator_task_api':
                        detail_cache[cache_key] = {'at': now, 'url': url, 'detail': detail}
                        cache_changed = True
            except Exception:
                pass
            finally:
                try: page.remove_listener('response', on_detail)
                except Exception: pass
            detail_fetched += 1
        if cache_changed:
            if len(detail_cache) > 200:
                detail_cache = dict(sorted(detail_cache.items(), key=lambda pair: int((pair[1] or {}).get('at') or 0), reverse=True)[:200])
            _write_creator_detail_cache(directory, detail_cache)
        return {'items': items[:limit], 'source': source,
                'page_url': 'https://creator.xiaohongshu.com/new/events',
                'raw_count': raw_count, 'detail_count': detail_count, 'detail_fetched': detail_fetched}
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


def comments(directory: Path, url: str, limit: int) -> dict[str, Any]:
    note_id, _, locator = parse_note_url(url)
    import xhs_comment
    p, context, page = _launch(directory)
    raw_pages: list[Any] = []
    try:
        def on_response(response):
            if 'comment/page' in response.url:
                try: raw_pages.append(response.json())
                except Exception: pass
        page.on('response', on_response)
        _goto(page, locator, wait=1600)
        for _ in range(3):
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)'); page.wait_for_timeout(700)
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


def reply(directory: Path, url: str, replies: list[dict[str, Any]]) -> dict[str, Any]:
    _, _, locator = parse_note_url(url)
    p, context, page = _launch(directory, headed=True)
    results = []
    try:
        _goto(page, locator, wait=1800)
        for target in replies[:20]:
            text = str(target.get('reply') or '').strip()
            if not text or len(text) > 1000:
                results.append({'id': str(target.get('id') or ''), 'status': 'not_submitted', 'reason': 'invalid_reply'}); continue
            container, reason = _target(page, target)
            if not container:
                results.append({'id': str(target.get('id') or ''), 'status': 'not_submitted', 'reason': reason}); continue
            try:
                reply_btn = None
                for el in container.query_selector_all('*'):
                    if (el.text_content() or '').strip() == '回复' and el.is_visible(): reply_btn = el; break
                if not reply_btn:
                    results.append({'id': str(target.get('id') or ''), 'status': 'not_submitted', 'reason': 'reply_button_missing'}); continue
                reply_btn.click(); page.wait_for_timeout(500)
                inp = _input(page)
                if not inp:
                    results.append({'id': str(target.get('id') or ''), 'status': 'not_submitted', 'reason': 'reply_input_missing'}); continue
                _fill(inp, text)
                clicked = False
                for sel in ("button:has-text('发送')", "[class*='send-btn']", "[class*='submit']"):
                    try:
                        btn = page.locator(sel).first
                        if btn.count() and btn.is_visible(): btn.click(); clicked = True; break
                    except Exception: pass
                if not clicked:
                    try: inp.press('Control+Enter'); clicked = True
                    except Exception: pass
                if not clicked:
                    results.append({'id': str(target.get('id') or ''), 'status': 'not_submitted', 'reason': 'send_control_missing'}); continue
                page.wait_for_timeout(1200)
                results.append({'id': str(target.get('id') or ''), 'status': 'verified' if _verify_text(page, text) else 'unknown_result', 'reason': ''})
            except Exception:
                results.append({'id': str(target.get('id') or ''), 'status': 'unknown_result', 'reason': 'interaction_error'})
        return {'results': results}
    finally:
        _close(p, context)


def delete_comments(directory: Path, url: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
    _, _, locator = parse_note_url(url)
    p, context, page = _launch(directory, headed=True)
    results=[]
    try:
        _goto(page, locator, wait=1800)
        for target in targets[:20]:
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
                menu.click(); page.wait_for_timeout(250)
                for text in ('确定','确认','删除'):
                    try:
                        btn=page.get_by_text(text, exact=True).last
                        if btn.count() and btn.is_visible(): btn.click(); break
                    except Exception: pass
                page.wait_for_timeout(1000)
                count=int(page.evaluate(COMMENT_TARGET_COUNT_JS,[str(target.get('id') or ''),str(target.get('nickname') or ''),str(target.get('content') or '')[:500]]) or 0)
                results.append({'id':str(target.get('id') or ''),'status':'verified' if count==0 else 'unknown_result','reason':''})
            except Exception:
                results.append({'id':str(target.get('id') or ''),'status':'unknown_result','reason':'interaction_error'})
        return {'results':results}
    finally:_close(p, context)


def post_comment(directory: Path, url: str, text: str) -> dict[str, Any]:
    _, _, locator = parse_note_url(url); text=str(text or '').strip()
    if not text or len(text)>1000: raise XhsBrowserError('invalid_comment')
    p, context, page=_launch(directory, headed=True)
    try:
        _goto(page, locator, wait=1800)
        inp=None
        for sel in ("[placeholder*='说点什么']","[placeholder*='来聊聊']","[class*='comment-input'] [contenteditable='true']","[contenteditable='true']"):
            try:
                cand=page.locator(sel).first
                if cand.count() and cand.is_visible(): inp=cand; break
            except Exception:pass
        if not inp:return {'status':'not_submitted','reason':'comment_input_missing'}
        _fill(inp,text)
        clicked=False
        for sel in ("button:has-text('发送')","[class*='send-btn']","[class*='submit']"):
            try:
                btn=page.locator(sel).first
                if btn.count() and btn.is_visible():btn.click();clicked=True;break
            except Exception:pass
        if not clicked:return {'status':'not_submitted','reason':'send_control_missing'}
        page.wait_for_timeout(1200)
        return {'status':'verified' if _verify_text(page,text) else 'unknown_result','reason':''}
    finally:_close(p,context)


def run(action: str, directory: Path, params: dict[str, Any]) -> dict[str, Any]:
    limit=max(1,min(int(params.get('limit') or 12),100))
    if action=='feed': return feed(directory,limit)
    if action=='search': return search(directory,str(params.get('query') or ''),limit)
    if action=='notes': return account_notes(directory,limit)
    if action=='events': return creator_events(directory,limit,max(0,min(int(params.get('detail_limit') or 8),20)))
    if action=='note': return note_detail(directory,str(params.get('url') or ''))
    if action=='comments': return comments(directory,str(params.get('url') or ''),limit)
    if action=='reply': return reply(directory,str(params.get('url') or ''),list(params.get('items') or []))
    if action=='delete': return delete_comments(directory,str(params.get('url') or ''),list(params.get('items') or []))
    if action=='comment': return post_comment(directory,str(params.get('url') or ''),str(params.get('text') or ''))
    raise XhsBrowserError('unsupported_action')
