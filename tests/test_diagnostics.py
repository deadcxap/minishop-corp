from minishop_corp.diagnostics import failure, request_id, request_scope


def test_request_identifiers_are_nested_and_cleared() -> None:
    assert request_id() is None
    with request_scope() as first:
        with request_scope() as second:
            assert first != second and request_id() == second
        assert request_id() == first
    assert request_id() is None


def test_exception_frames_exclude_message_source_and_local_values() -> None:
    secret = "provider-secret-token"
    try:
        raise RuntimeError(secret)
    except RuntimeError as exc:
        data = failure(exc)
    assert data["exception"] == "RuntimeError"
    assert "test_diagnostics.py:" in data["frames"]
    assert secret not in str(data) and "raise RuntimeError" not in str(data)
