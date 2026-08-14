from coding_agent_harness.demo import run_governance_demo


def test_governance_demo_proves_required_mechanisms(tmp_path) -> None:
    report = run_governance_demo(tmp_path)
    assert report.network_used is False
    assert report.real_keyring_used is False
    assert report.scenes[0].name == "dangerous_action_blocked"
    assert report.scenes[0].dispatcher_calls == 0
    assert report.scenes[1].name == "feedback_changes_next_action"
    assert report.scenes[1].passed is True
    assert report.scenes[2].name == "persistent_single_use_approval"
    assert report.scenes[2].executions == 1
    assert report.scenes[2].replay_decision == "DENY"
