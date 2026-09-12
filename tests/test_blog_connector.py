from __future__ import annotations

import json
import time

import httpx
import pytest

from ripple import blog_connector
from ripple.blog_connector import BlogConnectorInput, OpenApiBlogClient
from ripple.library import MotherCreate
from ripple.publishing import ApprovalInput, WorkflowError
from ripple.variants import VariantBatchInput, VariantRevision, VariantTarget, VariantTaskCreate
from ripple.workspace import WorkspaceService


class FakeBlog:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.article = None
        self.thought = None
        self.article_creates = 0
        self.thought_creates = 0

    @staticmethod
    def spec(server="https://blog.example"):
        def op(capability):
            return {"x-blog-capability": capability, "responses": {"200": {"description": "ok"}}}
        return {
            "openapi": "3.1.0",
            "x-blog-connector": {
                "version": "1.0",
                "contentTypes": ["article", "thought"],
                "media": True,
                "articlePublicUrlTemplate": "/journal/{slug}",
                "thoughtPublicUrlTemplate": "/spark/{id}",
            },
            "servers": [{"url": server}],
            "paths": {
                "/api/custom/article-drafts": {"post": op("article.create")},
                "/api/custom/articles/{id}": {"get": op("article.read"), "patch": op("article.update")},
                "/api/custom/articles/{id}/go-live": {"post": op("article.publish")},
                "/v7/thought-drafts": {"post": op("thought.create")},
                "/v7/thoughts/{id}": {"get": op("thought.read"), "patch": op("thought.update")},
                "/v7/thoughts/{id}/release": {"post": op("thought.publish")},
                "/asset-ingest/raw": {"post": op("media.upload")},
            },
        }

    @staticmethod
    def _public(item):
        return {"item": dict(item)}

    @staticmethod
    def _update(item, body):
        expected = body.get("expectedRevision")
        if expected != item["revision"]:
            return httpx.Response(409, json={"error": "revision conflict", "currentRevision": item["revision"]})
        item["data"].update(body.get("data") or {})
        item["revision"] += 1
        return httpx.Response(200, json=FakeBlog._public(item))

    @staticmethod
    def _publish(item, body):
        expected = body.get("expectedRevision")
        if expected != item["revision"]:
            return httpx.Response(409, json={"error": "revision conflict", "currentRevision": item["revision"]})
        item["status"] = "published"
        item["revision"] += 1
        return httpx.Response(200, json=FakeBlog._public(item))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.url.host == "blog.example"
        assert request.headers.get("authorization") == "Bearer fixture-blog-token"
        path = request.url.path
        if path == "/spec/openapi.json" and request.method == "GET":
            return httpx.Response(200, json=self.spec())
        body = json.loads(request.content or b"{}") if request.headers.get("content-type", "").startswith("application/json") else {}
        if path == "/api/custom/article-drafts" and request.method == "POST":
            self.article_creates += 1
            self.article = {"id": "article-1", "kind": "posts", "status": "draft", "revision": 1, "data": body}
            return httpx.Response(201, json=self._public(self.article))
        if path == "/api/custom/articles/article-1" and request.method == "GET":
            return httpx.Response(200, json=self._public(self.article))
        if path == "/api/custom/articles/article-1" and request.method == "PATCH":
            return self._update(self.article, body)
        if path == "/api/custom/articles/article-1/go-live" and request.method == "POST":
            return self._publish(self.article, body)
        if path == "/v7/thought-drafts" and request.method == "POST":
            self.thought_creates += 1
            self.thought = {"id": "thought-1", "kind": "notes", "status": "draft", "revision": 1, "data": body}
            return httpx.Response(201, json=self._public(self.thought))
        if path == "/v7/thoughts/thought-1" and request.method == "GET":
            return httpx.Response(200, json=self._public(self.thought))
        if path == "/v7/thoughts/thought-1" and request.method == "PATCH":
            return self._update(self.thought, body)
        if path == "/v7/thoughts/thought-1/release" and request.method == "POST":
            return self._publish(self.thought, body)
        raise AssertionError(f"unexpected Blog request: {request.method} {request.url}")


def wait_for_task(work: WorkspaceService, task_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = work.get(task_id)
        if task["status"] != "dispatching":
            return task
        time.sleep(0.01)
    raise AssertionError("Blog publish worker did not finish")


def approve_and_publish(work: WorkspaceService, task: dict):
    preflight = work.preflight(task["id"], task["version_id"])
    assert preflight["ok"], preflight["problems"]
    approved = work.approve(task["id"], ApprovalInput(
        expected_version=preflight["task"]["version_id"], confirmed=True, real_publish_confirmed=True,
    ))
    work.dispatch(approved["id"], approved["version_id"])
    return wait_for_task(work, approved["id"])


def test_openapi_server_must_stay_on_same_origin():
    fake = FakeBlog()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=fake.spec("https://evil.example")))
    client = OpenApiBlogClient("https://blog.example/spec/openapi.json", "fixture-blog-token", transport=transport)
    try:
        with pytest.raises(WorkflowError, match="同源"):
            client.discover()
    finally:
        client.close()


def test_blog_connector_requires_new_token_when_origin_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(blog_connector, "protect", lambda data, decrypt=False: data)
    seen = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        server = f"https://{request.url.host}"
        return httpx.Response(200, json=FakeBlog.spec(server))

    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    work.blogs.transport = httpx.MockTransport(transport)
    try:
        connector = work.blogs.save(BlogConnectorInput(
            label="Origin-bound Blog", openapi_url="https://old.example/spec/openapi.json",
            token="fixture-old-token", confirmed=True,
        ))
        with pytest.raises(WorkflowError, match="重新输入 Agent Token"):
            work.blogs.save(BlogConnectorInput(
                label="Origin-bound Blog", openapi_url="https://new.example/spec/openapi.json",
                token="", confirmed=True,
            ), connector["id"])
        assert seen == [("old.example", "Bearer fixture-old-token")]
        updated = work.blogs.save(BlogConnectorInput(
            label="Origin-bound Blog", openapi_url="https://new.example/spec/openapi.json",
            token="fixture-new-token", confirmed=True,
        ), connector["id"])
        assert updated["openapi_url"].startswith("https://new.example/")
        assert seen[-1] == ("new.example", "Bearer fixture-new-token")
    finally:
        work.close()


def test_generic_blog_connector_publishes_articles_and_thoughts_without_fixed_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(blog_connector, "protect", lambda data, decrypt=False: data)
    fake = FakeBlog()
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    work.blogs.transport = httpx.MockTransport(fake)
    try:
        connector = work.blogs.save(BlogConnectorInput(
            label="My Blog", openapi_url="https://blog.example/spec/openapi.json",
            token="fixture-blog-token", confirmed=True,
        ))
        assert connector["status"] == "connected"
        assert "token_protected" not in connector
        assert {"article.create", "article.update", "thought.create", "thought.publish"}.issubset(connector["capabilities"])

        mother = work.library.create(MotherCreate(
            title="Generic connector article", body="First body", tags="alpha,beta", idempotency_key="generic-blog-mother",
        ))
        variant = work.variants.create_many(mother["id"], VariantBatchInput(
            expected_source_version=mother["version_id"], idempotency_key="generic-blog-variant",
            targets=[VariantTarget(platform="blog")],
        ))["items"][0]
        article_content = {
            **variant["content"], "target_id": connector["id"], "delivery": "remote",
            "options": {"content_type": "article", "slug": "generic-article", "excerpt": "summary", "tier": "Public"},
        }
        variant = work.variants.revise(variant["id"], VariantRevision(
            **article_content, expected_version=variant["version_id"], source_version_id=mother["version_id"],
        ))
        task = work.variants.create_task(variant["id"], VariantTaskCreate(
            expected_version=variant["version_id"], idempotency_key="generic-blog-publish-1",
        ))
        published = approve_and_publish(work, task)
        assert published["status"] == "published"
        assert published["receipt"]["public_url"] == "https://blog.example/journal/generic-article"
        assert fake.article_creates == 1
        assert ("POST", "/api/custom/article-drafts") in fake.calls
        assert all(not path.startswith("/posts") for _, path in fake.calls)

        variant = work.variants.get(variant["id"])
        updated_content = {**variant["content"], "body": "Second body"}
        variant = work.variants.revise(variant["id"], VariantRevision(
            **updated_content, expected_version=variant["version_id"], source_version_id=mother["version_id"],
        ))
        task = work.variants.create_task(variant["id"], VariantTaskCreate(
            expected_version=variant["version_id"], idempotency_key="generic-blog-publish-2",
        ))
        published = approve_and_publish(work, task)
        assert published["status"] == "published"
        assert fake.article_creates == 1
        assert fake.article["data"]["body"] == "Second body"

        variant = work.variants.get(variant["id"])
        thought_content = {
            **variant["content"], "title": "Short thought", "body": "A compact idea", "tags": "memo",
            "options": {"content_type": "thought", "thought_tag": "idea"},
        }
        variant = work.variants.revise(variant["id"], VariantRevision(
            **thought_content, expected_version=variant["version_id"], source_version_id=mother["version_id"],
        ))
        task = work.variants.create_task(variant["id"], VariantTaskCreate(
            expected_version=variant["version_id"], idempotency_key="generic-blog-thought-1",
        ))
        published = approve_and_publish(work, task)
        assert published["status"] == "published"
        assert published["receipt"]["public_url"] == "https://blog.example/spark/thought-1"
        assert fake.thought_creates == 1
        assert fake.thought["data"]["text"] == "A compact idea"
        assert fake.thought["data"]["tag"] == "idea"
        assert ("POST", "/v7/thought-drafts") in fake.calls
    finally:
        work.close()
