"""Tests for per-request LLM header parsing in ``web.server._read_llm_config``.

Focus: the K2 Think backend selector (``X-LLM-K2-Backend``) is read, validated to
the allowed set, and surfaced for the runner to thread into the Config.
"""
from __future__ import annotations

import pytest

from web.server import _read_llm_config, _resolve_scope_target


class _FakeHeaders(dict):
    """Minimal stand-in for Starlette's request.headers (exact-case .get)."""
    def get(self, key, default=""):
        return super().get(key, default)


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = _FakeHeaders(headers)


def _cfg(headers: dict) -> dict:
    return _read_llm_config(_FakeRequest(headers))


def test_k2_backend_header_passes_valid_values():
    for val in ("auto", "cerebras", "nvidia"):
        cfg = _cfg({"X-LLM-Backend": "openai", "X-LLM-Key": "k",
                    "X-LLM-K2-Backend": val})
        assert cfg["k2_backend"] == val


def test_k2_backend_header_normalises_case():
    cfg = _cfg({"X-LLM-Backend": "openai", "X-LLM-Key": "k",
                "X-LLM-K2-Backend": "NVIDIA"})
    assert cfg["k2_backend"] == "nvidia"


def test_k2_backend_header_invalid_becomes_empty():
    cfg = _cfg({"X-LLM-Backend": "openai", "X-LLM-Key": "k",
                "X-LLM-K2-Backend": "gpu-please"})
    assert cfg["k2_backend"] == ""


def test_k2_backend_absent_defaults_empty():
    cfg = _cfg({"X-LLM-Backend": "openai", "X-LLM-Key": "k"})
    assert cfg["k2_backend"] == ""


# --- scope resolution surfaces the repo root for include discovery -----------
# A single-file run must still know the repo root so the runner can discover the
# repo's include dirs (project headers usually live in a sibling include/).
# Without it, cc -E can't resolve them and CBMC reports "harness build failed".

def _workspace_with_repo(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    (repo / "include").mkdir(parents=True)
    (repo / "src" / "foo.c").write_text('#include "foo.h"\nint foo(void){return 0;}\n')
    return tmp_path, repo


def test_resolve_scope_file_returns_repo_root(tmp_path):
    workspace, repo = _workspace_with_repo(tmp_path)
    target, is_dir, repo_dir = _resolve_scope_target(
        workspace, {"mode": "file", "repo": "myrepo", "path": "src/foo.c"}
    )
    assert is_dir is False
    assert target == repo / "src" / "foo.c"
    assert repo_dir == repo


def test_resolve_scope_whole_returns_repo_root(tmp_path):
    workspace, repo = _workspace_with_repo(tmp_path)
    target, is_dir, repo_dir = _resolve_scope_target(
        workspace, {"mode": "whole", "repo": "myrepo"}
    )
    assert is_dir is True
    assert target == repo and repo_dir == repo


# --- end-to-end run-settings wiring over HTTP --------------------------------
# Upload a synthetic repo, then exercise /api/estimate + /api/run + retry to
# confirm the body's `options` are parsed, clamped, stored in scope, reflected in
# the estimate, and round-tripped on retry. ``run_job`` is stubbed so no pipeline
# actually runs — we only assert the job's stored configuration.

def _client(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from web import jobs, server
    monkeypatch.setattr(jobs, "run_job", lambda job, factory: None)
    return TestClient(server.app)


def _upload(client, name, path, content):
    r = client.post("/api/upload", json={"name": name, "files": [{"path": path, "content": content}]})
    body = r.json()
    assert body["ok"], r.text
    return body["repo"]


def test_run_options_clamped_stored_and_round_trip_on_retry(monkeypatch):
    from web import limits
    client = _client(monkeypatch)
    repo = _upload(client, "demo", "demo.c", "int add(int a,int b){return a+b;}\n")

    r = client.post("/api/run", headers={"X-LLM-Key": "sk-test"}, json={
        "repo": repo, "mode": "file", "path": "demo.c",
        "options": {"depth": {"cbmc_unwind": 9999}, "unknown_group": {"x": 1}},
    })
    assert r.json()["ok"], r.text
    run_id = r.json()["run_id"]

    opts = client.get("/api/run/" + run_id).json()["scope"]["options"]
    assert opts["depth"]["cbmc_unwind"] == limits.MAX_CBMC_UNWIND   # clamped server-side
    assert "unknown_group" not in opts                              # dropped, not a 500

    r2 = client.post("/api/run/" + run_id + "/retry", json={"mode": "rerun_all"})
    assert r2.json()["ok"], r2.text
    assert client.get("/api/run/" + r2.json()["run_id"]).json()["scope"]["options"] == opts


def test_sanitize_repo_name_rejects_traversal():
    # ``.`` stays in the sanitizer's charset, so a name of "." / ".." used to
    # survive and resolve the clone/upload dest to the workspace itself or its
    # parent (the shared sessions root) — an rmtree there wipes other sessions.
    from web.gitclone import sanitize_repo_name
    assert sanitize_repo_name("https://github.com/..") == "repo"
    assert sanitize_repo_name("https://github.com/.") == "repo"
    assert sanitize_repo_name("https://github.com/owner/proj.git") == "proj"


def test_upload_traversal_name_does_not_escape_workspace(tmp_path):
    # A ".." upload must not delete or write outside the session workspace.
    from web.server import _write_upload
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sibling = tmp_path / "victim"  # stands in for another visitor's workspace
    sibling.mkdir()
    (sibling / "keep.c").write_text("int main(){return 0;}\n")

    result = _write_upload(workspace, "..", [{"path": "a.c", "content": "int f(){return 0;}\n"}])

    # Either rejected outright or confined; never resolves above the workspace.
    if isinstance(result, dict):
        assert (workspace / result["repo"]).resolve().is_relative_to(workspace.resolve())
    assert sibling.exists() and (sibling / "keep.c").exists()


def test_estimate_reflects_options_over_http(monkeypatch):
    client = _client(monkeypatch)
    repo = _upload(client, "est", "m.c",
                   "int f(int x){int r=0;for(int i=0;i<x;i++)r+=i;return r;}\n")
    base = client.post("/api/estimate", json={"repo": repo, "mode": "file", "path": "m.c"}).json()["estimate"]
    withr = client.post("/api/estimate", json={
        "repo": repo, "mode": "file", "path": "m.c",
        "options": {"ai_layers": {"enable_realism_check": True}},
    }).json()["estimate"]
    assert withr["requests"]["expected"] > base["requests"]["expected"]


# --- per-function picker + domain knowledge ----------------------------------

def test_api_functions_lists_names(monkeypatch):
    client = _client(monkeypatch)
    repo = _upload(client, "fns", "m.c",
                   "int alpha(int x){return x;}\nint beta(void){return 0;}\n")
    body = client.get("/api/functions", params={"repo": repo, "path": "m.c"}).json()
    assert body["ok"], body
    assert set(body["functions"]) == {"alpha", "beta"}


def test_api_functions_rejects_traversal(monkeypatch):
    client = _client(monkeypatch)
    repo = _upload(client, "fns2", "m.c", "int f(void){return 0;}\n")
    r = client.get("/api/functions", params={"repo": repo, "path": "../../etc/passwd"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_run_passes_only_functions_and_domain_knowledge(monkeypatch):
    client = _client(monkeypatch)
    repo = _upload(client, "wire", "m.c",
                   "int a(void){return 0;}\nint b(void){return 1;}\n")
    r = client.post("/api/run", headers={"X-LLM-Key": "sk-test"}, json={
        "repo": repo, "mode": "file", "path": "m.c",
        "only_functions": ["a"], "domain_knowledge": "caller guarantees x>0",
    })
    assert r.json()["ok"], r.text
    scope = client.get("/api/run/" + r.json()["run_id"]).json()["scope"]
    assert scope["only_functions"] == ["a"]
    assert scope["domain_knowledge"] == "caller guarantees x>0"


def test_estimate_respects_only_functions(monkeypatch):
    client = _client(monkeypatch)
    repo = _upload(client, "estfn", "m.c",
                   "int a(int x){int r=0;for(int i=0;i<x;i++)r+=i;return r;}\n"
                   "int b(int y){int s=0;for(int j=0;j<y;j++)s+=j;return s;}\n")
    full = client.post("/api/estimate", json={
        "repo": repo, "mode": "file", "path": "m.c"}).json()["estimate"]
    one = client.post("/api/estimate", json={
        "repo": repo, "mode": "file", "path": "m.c", "only_functions": ["a"],
    }).json()["estimate"]
    assert full["n_functions"] == 2
    assert one["n_functions"] == 1
