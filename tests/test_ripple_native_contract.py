"""Test the actual native-worker boundary without contacting a social platform."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import uuid

import pytest

from ripple import native_worker as worker
from ripple.catalog import NATIVE


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    from playwright.sync_api import BrowserType, Keyboard
    launches=[]
    monkeypatch.setattr(BrowserType, 'launch_persistent_context', lambda self,path,**kw: launches.append((path,kw)))
    monkeypatch.setattr(Keyboard, 'press', lambda self,key,**kw: None)
    calls=[]
    scripts={}
    for platform, spec in NATIVE.items():
        module=scripts.get(spec['module'])
        if module is None:
            module=ModuleType(spec['module'])
            module.cmd_login=lambda a: calls.append(('login',deepcopy(vars(a)))) or 0
            module.cmd_login_qr=lambda a: calls.append(('login-qr',deepcopy(vars(a)))) or 0
            def whoami(a):
                calls.append(('whoami',deepcopy(vars(a))))
                print(json.dumps({'loggedIn':True,'name':'Fixture identity','uid':'fixture-user','credential':'do-not-return'}))
                return 0
            module.cmd_whoami=whoami
            module.cmd_publish=lambda a: calls.append(('publish',deepcopy(vars(a)))) or 0
            module.cmd_publish_video=lambda a: calls.append(('video',deepcopy(vars(a)))) or 0
            module._click_publish=lambda *a,**kw: calls.append(('click',{}))
            module._verify_published=lambda *a,**kw: True
            scripts[spec['module']]=module
            monkeypatch.setitem(sys.modules,spec['module'],module)
    login=ModuleType('login_state'); login.write_status=lambda *a,**kw: None
    calendar=ModuleType('calendar_ops');calendar.record_publish=lambda *a,**kw: None
    monkeypatch.setitem(sys.modules,'login_state',login)
    monkeypatch.setitem(sys.modules,'calendar_ops',calendar)
    return SimpleNamespace(root=tmp_path, calls=calls, modules=scripts, launches=launches, monkeypatch=monkeypatch)


def payload(native, platform='zhihu', operation='publish', media=False):
    opid=uuid.uuid4().hex
    directory=native.root/'private'/uuid.uuid4().hex
    media_dir=directory/'operations'/opid/'media';media_dir.mkdir(parents=True)
    file=media_dir/'video.mp4';file.write_bytes(b'local-contract-fixture')
    return {'platform':platform,'operation':operation,'operation_id':opid,'private_dir':str(directory),
            'browser_channel':'msedge','headed':True,'confirmed':True,'task_id':uuid.uuid4().hex,'version_id':'a'*64,
            'identity':{'name':'Fixture identity','remote_id':'fixture-user'},
            'content':{'title':'标题 with spaces','body':'Literal body; not a shell command','tags':'one,two'},
            'media_paths':[str(file)] if media else []}


@pytest.mark.parametrize('platform',list(NATIVE))
def test_native_login_contract_each_platform(native,platform):
    p=payload(native,platform,'login')
    result=worker.execute(p)
    assert result['state']=='connected' and result['identity']['remote_id']=='fixture-user'
    assert 'credential' not in json.dumps(result)
    assert [x[0] for x in native.calls]==[('login-qr' if platform in {'kuaishou','zhihu','weixin-channels'} else 'login'),'whoami']
    opts=native.calls[0][1]
    assert Path(opts['profile_base']).is_relative_to(Path(p['private_dir']))
    assert Path(opts['cookie']).is_relative_to(Path(p['private_dir']))
    assert Path.cwd()==Path(p['private_dir'])/'operations'/p['operation_id']


def test_missing_publish_approval_never_checks_account_or_uploads(native):
    p=payload(native);p['confirmed']=False
    result=worker.execute(p)
    assert result['not_submitted'] is True
    assert not native.calls
    assert not (Path.cwd()/'submission.json').exists()


def test_identity_mismatch_stops_before_media_upload(native):
    p=payload(native);p['identity']['remote_id']='different-user'
    result=worker.execute(p)
    assert result['state']=='verification_required' and result['not_submitted'] is True
    assert [x[0] for x in native.calls]==['whoami']
    assert not (Path.cwd()/'submission.json').exists()


@pytest.mark.parametrize('platform',['xiaohongshu','douyin','kuaishou','weixin-channels','zhihu'])
def test_native_publisher_receives_literal_approved_content(native,platform):
    p=payload(native,platform,media=platform!='zhihu')
    result=worker.execute(p)
    assert result['state']=='accepted'
    name,opts=native.calls[-1]
    assert name==('video' if platform in {'xiaohongshu','douyin'} else 'publish')
    assert opts['title']==p['content']['title'] and opts['content']==p['content']['body']
    assert opts['exec'] is True and opts['no_proxy'] is True
    marker=json.loads((Path.cwd()/'submission.json').read_text())
    assert marker['task_id']==p['task_id'] and marker['version_id']==p['version_id']


def test_biliup_command_uses_account_cookie_and_literal_argv(native):
    invocations=[]
    native.monkeypatch.setattr(worker,'biliup_binary',lambda:native.root/'biliup.exe')
    native.monkeypatch.setattr(worker.subprocess,'run',lambda argv,**kw:invocations.append((argv,kw)) or SimpleNamespace(returncode=0))
    p=payload(native,'bilibili',media=True)
    assert worker.execute(p)['state']=='accepted'
    argv,opts=invocations[0]
    assert argv[1:4]==['-u',str(Path(p['private_dir'])/'cookies.json'),'upload']
    assert argv[4]==p['media_paths'][0]
    assert argv[argv.index('--title')+1]==p['content']['title']
    assert not opts.get('shell')
    assert opts['stdout'] is subprocess.DEVNULL and opts['stderr'] is subprocess.DEVNULL


def test_final_click_guard_escapes_swallowed_exception_retry(native):
    p=payload(native,'douyin',media=True)
    mod=native.modules['douyin_publish']
    def unsafe_legacy_retry(a):
        mod._click_publish(None)
        try:
            mod._click_publish(None)
        except Exception:
            raise AssertionError('Guard must not be swallowed by legacy retries')
        return 0
    mod.cmd_publish_video=unsafe_legacy_retry
    result=worker.execute(p)
    assert result['state']=='unknown_result'
    assert len([c for c in native.calls if c[0]=='click'])==1


def test_browser_profile_launch_guard_rejects_other_directory(native):
    from playwright.sync_api import BrowserType
    p=payload(native,operation='probe')
    worker.execute(p)
    BrowserType.launch_persistent_context(object(),Path(p['private_dir'])/'browser'/'Fixture',args=['--unsafe'],headless=True)
    _,kw=native.launches[0]
    assert kw['channel']=='msedge' and kw['chromium_sandbox'] is True
    assert '--unsafe' not in kw['args']
    with pytest.raises(ValueError):BrowserType.launch_persistent_context(object(),native.root/'personal-browser')


def test_login_honors_visible_browser_request_despite_legacy_headless_default(native):
    from playwright.sync_api import BrowserType
    p=payload(native,'zhihu','login')
    def login(opts):
        BrowserType.launch_persistent_context(object(),Path(opts.profile_base)/'Fixture',headless=True)
        return 0
    native.modules['web_publisher'].cmd_login_qr=login
    assert worker.execute(p)['state']=='connected'
    assert native.launches[0][1]['headless'] is False


def test_real_subprocess_denied_publish_is_offline_and_correlated(tmp_path):
    p={'platform':'zhihu','operation':'publish','confirmed':False,'operation_id':uuid.uuid4().hex,
       'private_dir':str(tmp_path/'private'),'task_id':uuid.uuid4().hex,'version_id':'b'*64}
    # Confirmed=false exits before whoami/browser launch; this exercises the real
    # process boundary and JSON handoff, not a social login or publication.
    result=subprocess.run([sys.executable,'-X','utf8','-m','ripple.native_worker'],
        cwd=Path(__file__).resolve().parents[1],input=json.dumps(p).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    report=json.loads(result.stdout)
    assert report['not_submitted'] is True and report['task_id']==p['task_id']
    saved=json.loads((tmp_path/'private'/'operations'/p['operation_id']/'result.json').read_text(encoding='utf-8'))
    assert saved==report
    assert not (tmp_path/'private'/'browser').exists()
