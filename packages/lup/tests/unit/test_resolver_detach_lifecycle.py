"""A duplicate detached invocation cannot erase the active driver's history."""

from pathlib import Path
from unittest.mock import Mock

import pytest
import sh

import lup.devtools.harness.resolve as resolve
from lup.devtools.harness.resolve import AdmissionFlags, DetachedRun, SupervisorSpawn
from lup.diagnostics import Refusal
from lup.resolver.state import ResolverStateRepository


def invocation() -> DetachedRun:
    return DetachedRun(
        adapter="claude",
        run_id="existing",
        answers=[],
        admitted=AdmissionFlags(statements=[], notes=[], issues=[]),
        issues=False,
        wait=0,
        host_retries=2,
        host_backoff=1,
        supervisor=SupervisorSpawn(),
        adopt_config=False,
        auth_probe_delay=1,
        max_parallel_workers=1,
        recheck_standing_per_join=False,
        profile=None,
    )


def test_live_duplicate_is_refused_before_touching_the_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve, "project_root", lambda: tmp_path)
    launched = Mock()
    monkeypatch.setattr(sh, "Command", launched)
    repository = ResolverStateRepository(tmp_path / ".lup/resolve", "existing")
    log = resolve.detached_log(tmp_path, "existing")
    log.write_bytes(b"the entire running history\n")
    with (
        repository.exclusive(),
        pytest.raises(Refusal) as refused,
    ):
        resolve.detach_resolve(invocation())
    assert "already active" in refused.value.said["why"]
    launched.assert_not_called()
    assert log.read_bytes() == b"the entire running history\n"


def test_a_child_refused_after_the_initial_check_appends_its_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve, "project_root", lambda: tmp_path)
    log = resolve.detached_log(tmp_path, "existing")
    log.write_bytes(b"the entire running history\n")
    child = Mock(
        side_effect=lambda *_args, **kwargs: kwargs["_out"].write(
            b"duplicate refused\n"
        )
    )
    monkeypatch.setattr(sh, "Command", Mock(return_value=child))
    resolve.detach_resolve(invocation())
    output = child.call_args.kwargs["_out"]
    assert output.mode == "ab"
    assert output.closed
    assert child.call_args.kwargs["_err_to_out"] is True
    assert log.read_bytes() == b"the entire running history\nduplicate refused\n"
