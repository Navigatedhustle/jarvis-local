import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location(
    "browser_recovery", Path(__file__).resolve().parents[1] / "scripts/ensure_outlook_browser.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def state(**values):
    data = {"outlook.provider": "browser", "setup.enabled.outlook": True, **values}
    return SimpleNamespace(get_state=lambda key, default=None: data.get(key, default), audit=Mock())


def test_browser_recovery_never_starts_disconnected_or_demo_sessions():
    for values in ({"demo_mode": True}, {"setup.enabled.outlook": False}, {"outlook.provider": "graph"}):
        runner = Mock()
        assert "skipped" in module.ensure(state(**values), runner)
        runner.assert_not_called()


def test_browser_recovery_leaves_live_hp_session_untouched():
    runner = Mock(return_value=SimpleNamespace(returncode=0))
    assert module.ensure(state(), runner) == {"running": True}
    runner.assert_called_once()
    assert "is-active" in runner.call_args.args[0]


def test_browser_recovery_only_requests_isolated_jarvis_browser_start():
    runner = Mock(return_value=SimpleNamespace(returncode=3))
    db = state()
    assert module.ensure(db, runner)["started"]
    command = runner.call_args_list[1].args[0]
    assert command[0] == "/usr/bin/python3" and command[-1] == "start"
    assert Path(command[1]).name == "nano_outlook_desktop.py"
    db.audit.assert_called_once()
