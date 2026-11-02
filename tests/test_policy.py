from critical_consensus.policy import Decision, evaluate_consensus


def test_consensus_fails_closed_when_any_gate_missing():
    result = evaluate_consensus(
        score=0.999,
        independent_reviewers=2,
        compliance_clear=True,
        risk_clear=False,
    )
    assert result.decision is Decision.GATHER_MORE
    assert "risk gate not cleared" in result.reasons


def test_consensus_approves_only_after_all_gates_pass():
    result = evaluate_consensus(
        score=0.999,
        independent_reviewers=2,
        unresolved_contradictions=[],
        compliance_clear=True,
        risk_clear=True,
    )
    assert result.decision is Decision.APPROVE
    assert result.reasons == ()


def test_invalid_score_never_approves():
    result = evaluate_consensus(
        score=1.5,
        independent_reviewers=2,
        compliance_clear=True,
        risk_clear=True,
    )
    assert result.decision is Decision.GATHER_MORE
