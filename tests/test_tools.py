import pytest

import pocket_agent as pa


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setattr(pa, "WORKSPACE", ws)
    return ws


@pytest.mark.parametrize("path", ["../secret.txt", "..\\secret.txt", "C:/Windows/win.ini", "sub/../../secret.txt"])
def test_paths_outside_workspace_are_refused(path):
    with pytest.raises(PermissionError):
        pa.read_file(path)
    with pytest.raises(PermissionError):
        pa.move_file("notes.txt", path)


def test_write_only_text_files(workspace):
    assert "wrote" in pa.write_file("out/summary.md", "- a\n- b")
    assert (workspace / "out" / "summary.md").read_text(encoding="utf-8") == "- a\n- b"
    with pytest.raises(PermissionError):
        pa.write_file("run.bat", "del *")


def test_move_and_list(workspace):
    pa.move_file("notes.txt", "Docs")
    assert (workspace / "Docs" / "notes.txt").is_file()
    assert pa.list_files(".") == "Docs/"
    assert pa.list_files("Docs") == "notes.txt"
    with pytest.raises(FileNotFoundError):
        pa.move_file("missing.txt", "Docs")


@pytest.mark.parametrize("expr,expected", [("17*60 - (16*60+40)", "20"), ("7/2", "3.5"), ("-3**2", "-9"), ("2**10", "1024")])
def test_calculate(expr, expected):
    assert pa.calculate(expr) == expected


@pytest.mark.parametrize("expr", ["__import__('os').system('dir')", "open('x')", "a+1", "2**999999", "True+1"])
def test_calculate_rejects_non_arithmetic(expr):
    with pytest.raises(ValueError):
        pa.calculate(expr)


def test_frozen_clock(monkeypatch):
    monkeypatch.setattr(pa, "DEMO_NOW", "16:40")
    assert pa.get_time().endswith("16:40")


def test_local_pages_only(monkeypatch):
    monkeypatch.setattr(pa, "ALLOW_WEB", False)
    with pytest.raises(PermissionError):
        pa.open_page("https://example.com")
    with pytest.raises(FileNotFoundError):
        pa.open_page("../../pocket_agent.py")


def test_agent_refuses_changes_when_not_allowed(monkeypatch, workspace):
    """A declined approval must stop write tools without touching the files."""
    calls = iter([
        [("move_file", {"path": "notes.txt", "to_folder": "Docs"})],
        [],
    ])

    class Msg:
        def __init__(self, tool_calls):
            from types import SimpleNamespace as NS
            self.tool_calls = [NS(function=NS(name=n, arguments=a)) for n, a in tool_calls]
            self.content = "done"

    def fake_chat(**kw):
        from types import SimpleNamespace as NS
        return NS(message=Msg(next(calls)), prompt_eval_count=1, eval_count=1, eval_duration=1)

    monkeypatch.setattr(pa.ollama, "chat", fake_chat)
    results = []
    agent = pa.Agent(confirm=lambda q: False, emit=lambda k, **d: results.append(d.get("text")) if k == "tool_result" else None)
    agent.ask("move it")
    assert (workspace / "notes.txt").is_file()
    assert results and results[0].startswith("ERROR")
