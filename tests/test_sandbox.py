"""Sandbox rules for the agent's file writes, and the offline memory store."""
import json
import os

import pytest

import pocket_agent as pa


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setattr(pa, "WORKSPACE", ws)
    monkeypatch.setattr(pa, "DATA", tmp_path / "agent_data")
    monkeypatch.setattr(pa, "_session", {"files": 0, "bytes": 0})
    return ws


def files(*paths, content="hi"):
    return [{"path": p, "content": content} for p in paths]


@pytest.mark.parametrize("path", [
    "../escape.md", "..\\escape.md", "site/../../escape.md", "C:/Windows/evil.txt", "C:evil.txt",
    "/etc/evil.txt", "\\\\server\\share\\evil.txt", "notes.txt:hidden", "CON.txt", "nul", "sub/aux.md",
    "trailing.txt.", "dir. /a.txt", "space.md ",
])
def test_paths_outside_or_tricky_are_refused(path, sandbox):
    with pytest.raises(PermissionError):
        pa.write_files(files(path))
    assert not any(sandbox.rglob("*"))


def test_junction_out_of_the_workspace_is_refused(sandbox, tmp_path):
    _winapi = pytest.importorskip("_winapi")
    _winapi.CreateJunction(str(tmp_path / "outside"), str(sandbox / "link"))
    with pytest.raises(PermissionError):
        pa.write_files(files("link/evil.md"))
    assert not any((tmp_path / "outside").iterdir())


def test_symlink_out_of_the_workspace_is_refused(sandbox, tmp_path):
    try:
        os.symlink(tmp_path / "outside", sandbox / "link", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode on Windows")
    with pytest.raises(PermissionError):
        pa.write_files(files("link/evil.md"))


@pytest.mark.parametrize("name", ["run.exe", "a.bat", "a.cmd", "a.ps1", "a.dll", "a.lnk", "a.js", "a.vbs", "a.py",
                                  "a.MSI", "report.txt.exe"])
def test_executables_are_refused(name):
    with pytest.raises(PermissionError, match="executable"):
        pa.write_files(files(name))


@pytest.mark.parametrize("name", ["noext", "a.docx", "a.zip"])
def test_types_outside_the_allowlist_are_refused(name):
    with pytest.raises(PermissionError, match="not an allowed type"):
        pa.write_files(files(name))


def test_multiple_file_types_in_folders(sandbox):
    batch = [{"path": "site/index.html", "content": "<h1>hi</h1>"}, {"path": "site/style.css", "content": "h1{}"},
             {"path": "data/rows.csv", "content": "a,b\n1,2"}, {"path": "README.md", "content": "# x"}]
    out = pa.write_files(batch)
    assert out.count("bytes") == 4
    assert (sandbox / "site" / "style.css").read_text(encoding="utf-8") == "h1{}"


def test_one_bad_file_writes_nothing(sandbox):
    with pytest.raises(PermissionError):
        pa.write_files(files("ok.md", "ok2.txt", "bad.exe"))
    assert not any(sandbox.rglob("*"))


def test_existing_files_are_versioned_not_overwritten(sandbox):
    (sandbox / "summary.md").write_text("original", encoding="utf-8")
    assert "summary-2.md" in pa.preview_writes(files("summary.md"))
    assert not (sandbox / "summary-2.md").exists()  # the preview is a dry run
    pa.write_file("summary.md", "new")
    pa.write_files(files("summary.md", "summary.md"))
    assert (sandbox / "summary.md").read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in sandbox.iterdir()) == ["summary-2.md", "summary-3.md", "summary-4.md", "summary.md"]


def test_size_and_count_limits(monkeypatch):
    with pytest.raises(PermissionError, match="limit"):
        pa.write_files(files("big.txt", content="x" * (pa.MAX_FILE_BYTES + 1)))
    with pytest.raises(PermissionError, match="per batch"):
        pa.write_files(files(*[f"f{i}.txt" for i in range(pa.MAX_BATCH_FILES + 1)]))
    monkeypatch.setattr(pa, "MAX_SESSION_FILES", 3)
    pa.write_files(files("a.txt", "b.txt"))
    with pytest.raises(PermissionError, match="session"):
        pa.write_files(files("c.txt", "d.txt"))


@pytest.mark.parametrize("bad", [[], "a.txt", [{"path": "a.txt"}], [{"path": 1, "content": "x"}], None])
def test_malformed_batches_are_refused(bad):
    with pytest.raises(ValueError):
        pa.write_files(bad)


def test_every_write_is_audited(sandbox):
    pa.write_files(files("a.md", "b/c.json", content="{}"))
    log = [json.loads(line) for line in (pa.DATA / "writes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["path"] for e in log] == ["a.md", "b/c.json"]
    assert log[0]["bytes"] == 2 and len(log[0]["sha256"]) == 64
    assert not pa.DATA.resolve().is_relative_to(sandbox.resolve())  # the agent can't reach its own log


def fake_agent(monkeypatch, tool_calls, confirm):
    from types import SimpleNamespace as NS
    calls = iter([tool_calls, []])
    monkeypatch.setattr(pa.ollama, "chat", lambda **kw: NS(
        message=NS(tool_calls=[NS(function=NS(name=n, arguments=a)) for n, a in next(calls)], content="done"),
        prompt_eval_count=1, eval_count=1, eval_duration=1))
    results = []
    agent = pa.Agent(confirm=confirm, emit=lambda k, **d: results.append(d["text"]) if k == "tool_result" else None)
    agent.ask("go")
    return results


def test_batch_shows_preview_and_declined_writes_nothing(monkeypatch, sandbox):
    asked = []
    out = fake_agent(monkeypatch, [("write_files", {"files": files("a.md", "b.html")})],
                     lambda q: asked.append(q) or False)
    assert "a.md" in asked[0] and "b.html" in asked[0]
    assert out[0].startswith("ERROR") and not any(sandbox.iterdir())


def test_batch_approved_writes(monkeypatch, sandbox):
    out = fake_agent(monkeypatch, [("write_files", {"files": files("a.md", "b.html")})], lambda q: True)
    assert out[0].startswith("wrote") and (sandbox / "b.html").is_file()


def test_invalid_batch_is_refused_before_asking(monkeypatch, sandbox):
    asked = []
    out = fake_agent(monkeypatch, [("write_files", {"files": files("../x.md")})], lambda q: asked.append(q) or True)
    assert not asked and out[0].startswith("ERROR")


# ---------- memory ----------

def test_remember_and_recall_by_keyword(monkeypatch):
    monkeypatch.setattr(pa, "_embed", lambda text: None)  # no embedding model pulled
    assert pa.recall("anything") == "nothing in memory matches that"
    pa.remember("Venket prefers the talk slides in dark mode")
    pa.remember("The demo laptop GPU has 8 GB of VRAM")
    assert "dark mode" in pa.recall("which slides mode", k=1)
    assert pa.recall("pizza") == "nothing in memory matches that"
    assert (pa.DATA / "memory.db").is_file()


def test_recall_by_embedding(monkeypatch):
    vecs = {"cats": [1.0, 0.0], "elephants": [0.0, 1.0], "kitten": [0.9, 0.1]}
    monkeypatch.setattr(pa, "_embed", lambda text: next(v for k, v in vecs.items() if k in text))
    pa.remember("I like cats")
    pa.remember("I like elephants")
    assert pa.recall("a kitten", k=1) == f"- I like cats (saved {pa.dt.date.today().isoformat()})"


def test_memory_size_limit():
    with pytest.raises(ValueError):
        pa.remember("x" * (pa.MAX_MEMORY_CHARS + 1))
    with pytest.raises(ValueError):
        pa.remember("   ")
