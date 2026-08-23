from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "scripts" / "maintenance" / "manage_formal_export_freeze.sh"


def test_formal_export_freeze_uses_noninteractive_scoped_systemctl_privilege() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "sudo -v" not in content
    assert "sudo -n systemctl" in content
    assert "require_systemctl_privilege" in content
    assert "xdn-task-markethub_futures_1m_daily.timer" in content
    assert "xdn-task-markethub_storage_governance_weekly.timer" in content
