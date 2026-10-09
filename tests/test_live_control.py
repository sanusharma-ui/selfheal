"""Local control contract tests for manual developer execution."""
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from selfheal import HealthCheckResult, HealthReport, LiveControlServer, LiveTarget
from selfheal.models import RepairResult


class RuntimeStub:
    """Transport tests do not activate application code or use external AI."""
    def __init__(self, root):
        self.project_root = root
        self.healing_disabled = False
        self.language = self
        self.calls = []

    def source_file(self, name):
        return self.project_root / "app.py"

    def inspect(self, **options):
        self.calls.append(options)
        return RepairResult(True, "proposal", "Applied", status="applied")


@pytest.fixture
def live(tmp_path):
    runtime = RuntimeStub(tmp_path)
    target = LiveTarget("sample", "app", "run",
                        lambda module: HealthReport((HealthCheckResult("run", True),)), retry_safe=True)
    with LiveControlServer(runtime, (target,), port=0, session_file=tmp_path / "session.json") as server:
        session = json.loads(server.session_file.read_text())
        yield runtime, server, session
    assert not (tmp_path / "session.json").exists()


def request(live, *, path="/status", body=None, authorized=True, origin=None):
    _, _, session = live
    headers = {"Content-Type": "application/json"}
    if authorized:
        headers["Authorization"] = "Bearer " + session["token"]
    if origin:
        headers["Origin"] = origin
    data = None if body is None else json.dumps(body).encode()
    return build_opener(ProxyHandler({})).open(Request(session["url"] + path, data=data, headers=headers), timeout=5)


@pytest.mark.parametrize("authorized,origin,code", [(False, None, 401), (True, "http://example.invalid", 403)])
def test_missing_credential_and_browser_origin_cannot_change_code(live, authorized, origin, code):
    with pytest.raises(HTTPError) as exc:
        request(live, path="/command", body={"operation": "heal", "target": "sample", "instructions": "", "preview": False},
                authorized=authorized, origin=origin)
    assert exc.value.code == code
    exc.value.close()
    assert not live[0].calls


def test_command_can_only_use_registered_targets(live):
    with pytest.raises(HTTPError) as exc:
        request(live, path="/command", body={"operation": "heal", "target": "../arbitrary.py", "instructions": "", "preview": False})
    assert exc.value.code == 400
    exc.value.close()
    assert not live[0].calls


def test_preview_and_apply_authority_are_explicit_and_result_is_streamed(live):
    for preview in (True, False):
        with request(live, path="/command", body={"operation": "heal", "target": "sample", "instructions": "", "preview": preview}) as response:
            data = [json.loads(line) for line in response]
        assert data[-1]["kind"] == "result"
        assert live[0].calls[-1]["apply"] is (not preview)
        assert live[0].calls[-1]["editable_symbols"] == ("run",)


def test_existing_session_file_is_not_overwritten(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("existing-session")
    runtime = RuntimeStub(tmp_path)
    target = LiveTarget("sample", "app", "run", lambda module: HealthReport(()))
    server = LiveControlServer(runtime, (target,), port=0, session_file=path)
    with pytest.raises(RuntimeError, match="Session file exists"):
        server.start()
    assert path.read_text() == "existing-session"
