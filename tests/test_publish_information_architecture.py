from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_sidebar_keeps_topic_and_publish_navigation_flat():
    source = text("web/frontend/src/components/Sidebar.tsx")
    assert "TOPIC_CHILDREN" not in source
    assert "PUBLISH_CHILDREN" not in source
    assert 'nav-group-chevron' not in source
    assert 'ripple_last_topic_tab_v1' in source
    assert 'ripple_last_publish_tab_v1' in source


def test_publish_management_is_not_a_second_content_editor():
    source = text("web/frontend/src/components/workspace/Publisher.tsx")
    assert 'title="发布任务"' in source
    assert "新建版本" not in source
    assert 'r2-title-input' not in source
    assert 'r2-platform-body' not in source
    assert '上传素材' not in source
    assert '同步主稿' not in source
    assert '打开内容工作台' in source
    assert 'taskAction(selected, action)' in source


def test_content_workbench_owns_variant_editing_and_publish_execution():
    contents = text("web/frontend/src/components/workspace/Contents.tsx")
    executor = text("web/frontend/src/components/workspace/ContentVariantPublisher.tsx")
    assert "ContentVariantPublisher" in contents
    assert "创建平台版本" in contents
    assert "平台版本标题" in executor
    assert "平台版本正文" in executor
    assert "平台版本目标账号" in executor
    assert "平台版本计划时间" in executor
    assert "预检并审核" in executor
    assert "发布到所选账号" in executor
    assert "确认真实发布授权" in executor
    assert "发布管理" in executor


def test_publish_subnav_uses_task_management_label():
    source = text("web/frontend/src/components/SubNav.tsx")
    assert "发布任务" in source
    assert "发布工作台" not in source
    assert "subnav-group-label" not in source
    assert "groupLabel" not in source


def test_accounts_page_localizes_optional_blog_connector_failure():
    source = text("web/frontend/src/components/workspace/Accounts.tsx")
    assert "const [a, c, s, x, n] = await Promise.all" in source
    assert "'/api/ripple/execution-nodes'" in source
    assert "setBlogError(blogErrorText(value))" in source
    assert "其他平台账号不受影响" in source


def test_channel_connection_ui_distinguishes_live_wechat_and_planned_tiktok():
    source = text("web/frontend/src/components/workspace/Accounts.tsx")
    assert "r2-connection-options" in source
    assert "计划接入" in source  # TikTok remains planned.
    assert "连接公众号" in source
    assert "AppID" in source and "AppSecret" in source and "IP 白名单" in source
    assert "/api/ripple/wechat/connect" in source
    publisher = text("web/frontend/src/components/workspace/ContentVariantPublisher.tsx")
    assert "保存到公众号草稿箱" in publisher
    assert "创建草稿并提交发布" in publisher
    assert "写入公众号草稿箱" in publisher


def test_outputs_are_content_grouped_and_use_ripple_delete_confirmation():
    source = text("web/frontend/src/components/OutputsPage.tsx")
    assert "按内容归档" in source
    assert "历史产物 / 未关联内容" not in source  # backend supplies the legacy bucket label
    assert "window.confirm(`确定删除" not in source
    assert "确认删除" in source
    assert "deleteOutput(deleteTarget.path, true)" in source


def test_server_auth_shell_and_settings_exist_without_changing_local_mode():
    app = text("web/frontend/src/App.tsx")
    auth = text("web/frontend/src/components/AuthBoundary.tsx")
    settings = text("web/frontend/src/components/workspace/UserAccessSettings.tsx")
    assert "<AuthBoundary><RippleApp /></AuthBoundary>" in app
    assert "if (status.local || status.authenticated)" in auth
    assert "Server 安全配置未完成" in auth
    assert "用户与访问" in text("web/frontend/src/components/workspace/Integrations.tsx")
    assert "本机模式" in settings and "默认免登录" in settings
