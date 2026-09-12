from pathlib import Path
import pytest

from ripple import catalog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple import api as ripple_api


def test_biliup_candidates_cover_windows_python_root_and_venv_scripts():
    root = catalog.biliup_candidates(Path('C:/Python312/python.exe'), windows=True)
    assert root[-1].as_posix().endswith('Python312/Scripts/biliup.exe')
    venv = catalog.biliup_candidates(Path('D:/Ripple/.venv/Scripts/python.exe'), windows=True)
    assert venv[0].as_posix().endswith('.venv/Scripts/biliup.exe')


def test_environment_capabilities_hide_internal_browser_channel(monkeypatch):
    monkeypatch.setattr(catalog, 'environment_probe', lambda: {'browser': 'msedge', 'browsers': ['msedge', 'chrome'], 'biliup': True})
    monkeypatch.setattr(catalog, '_package_version', lambda _name: '1.2.4')
    items = {row['id']: row for row in catalog.environment_capabilities()['items']}
    assert items['browser']['ready'] is True
    assert items['browser']['browsers'] == ['Microsoft Edge', 'Google Chrome']
    assert items['browser']['summary'] == '已就绪 · 随项目环境管理'
    assert '无需每次启动重复安装' in items['browser']['detail']
    assert items['bilibili']['third_party'] is True
    assert items['bilibili']['summary'] == '已就绪 · 随项目环境管理'
    assert '非 B 站官方组件' in items['bilibili']['detail']


def test_bilibili_install_uses_fixed_version_and_verifies(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, 'biliup_binary', lambda: None)
    monkeypatch.setattr(catalog, '_run_install', lambda argv, timeout: calls.append((argv, timeout)))
    monkeypatch.setattr(catalog, 'environment_capabilities', lambda: {'items': [{'id': 'bilibili', 'ready': True}]})
    result = catalog.install_environment_component('bilibili')
    assert result['ready'] is True
    assert calls[0][0][-1] == 'biliup==1.2.4'
    assert calls[0][1] == 300


def test_browser_install_uses_playwright_chromium_and_verifies(monkeypatch):
    calls = []
    monkeypatch.setattr(catalog, 'available_browser_channels', lambda: [])
    monkeypatch.setattr(catalog.importlib.util, 'find_spec', lambda _name: object())
    monkeypatch.setattr(catalog, '_run_install', lambda argv, timeout: calls.append((argv, timeout)))
    monkeypatch.setattr(catalog, 'environment_capabilities', lambda: {'items': [{'id': 'browser', 'ready': True}]})
    result = catalog.install_environment_component('browser')
    assert result['ready'] is True
    assert calls == [([catalog.sys.executable, '-m', 'playwright', 'install', 'chromium'], 900)]


def test_unknown_environment_component_is_rejected():
    with pytest.raises(ValueError, match='不支持安装'):
        catalog.install_environment_component('unknown')


def test_environment_api_requires_confirmation_and_uses_allowlisted_installer(tmp_path, monkeypatch):
    application = FastAPI()
    ripple_api.install(application, tmp_path)
    with TestClient(application, base_url='http://localhost') as client:
        state = client.get('/api/ripple/environment')
        assert state.status_code == 200
        assert {row['id'] for row in state.json()['items']} == {'browser', 'bilibili'}
        assert client.post('/api/ripple/environment/bilibili/install', json={'confirmed': False}).status_code == 422
        assert client.post('/api/ripple/environment/unknown/install', json={'confirmed': True}).status_code == 404
        monkeypatch.setattr(ripple_api, 'install_environment_component', lambda component: {'id': component, 'ready': True})
        installed = client.post('/api/ripple/environment/bilibili/install', json={'confirmed': True})
        assert installed.status_code == 200
        assert installed.json() == {'id': 'bilibili', 'ready': True}
