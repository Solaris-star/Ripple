from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple.accounts import AccountInput
from ripple.api import install


class BindingSpy:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def unbind(self, *, target_kind: str, account_id: str) -> None:
        self.calls.append((target_kind, account_id))


def test_account_delete_cleans_content_profile_binding(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    spy = BindingSpy()
    service.content_profiles = spy
    try:
        account = service.accounts.create(AccountInput(
            platform="xiaohongshu",
            label="synthetic review account",
            idempotency_key="profile-cleanup-account",
        ))
        with TestClient(app, base_url="http://localhost") as client:
            response = client.request(
                "DELETE",
                f"/api/ripple/accounts/{account['id']}",
                json={"confirmed": True},
            )
        assert response.status_code == 200
        assert spy.calls == [("account", account["id"])]
        assert all(row["id"] != account["id"] for row in service.accounts.list())
    finally:
        service.close()
