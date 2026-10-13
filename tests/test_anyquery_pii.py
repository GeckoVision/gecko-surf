"""Contact details are stored whole and shown masked unless the caller asks."""

from __future__ import annotations

from pathlib import Path

from gecko.anyquery.pii import is_pii_field, mask_value, masked
from gecko.anyquery.types import FactRow
from gecko.anyquery.views import fact_view
from tests.test_anyquery_surface import FIRST, ScriptedRun, _call, _deps, _plan_and_run


def test_an_email_keeps_two_letters_and_the_domain() -> None:
    assert mask_value("work_email", "someone@example.com") == "so***@example.com"


def test_a_phone_field_keeps_only_the_last_four_digits_and_its_shape() -> None:
    assert mask_value("mobile_phone", "+55 11 98765-4321") == "+** ** *****-4321"


def test_an_email_in_any_field_is_masked() -> None:
    assert mask_value("bio", "Talk to me: ana.lima@example.com") == (
        "Talk to me: an***@example.com"
    )


def test_an_international_number_inside_text_is_masked() -> None:
    assert mask_value("bio", "WhatsApp +1 (555) 123-4567 anytime") == (
        "WhatsApp +* (***) ***-4567 anytime"
    )


def test_dates_and_counts_outside_contact_fields_are_left_alone() -> None:
    assert mask_value("follower_count", "4217896") == "4217896"
    assert mask_value("latest_post_date", "2026-10-11") == "2026-10-11"


def test_contact_field_names_are_matched_in_free_text() -> None:
    for name in ("work_email", "Email", "mobile_phone", "whatsapp", "phone_number"):
        assert is_pii_field(name), name
    for name in ("follower_count", "job_title", "current_company", "website"):
        assert not is_pii_field(name), name


def test_reveal_returns_the_value_whole_and_says_nothing_was_masked() -> None:
    assert masked("work_email", "someone@example.com", reveal=True) == (
        "someone@example.com",
        False,
    )
    assert masked("work_email", None, reveal=False) == (None, False)


def _row(field: str, value: str | None) -> FactRow:
    return FactRow(
        customer="acct",
        plan_id="plan-x",
        run_id="20261012-000000-aaaaaa",
        mode="live",
        subject="https://www.linkedin.com/in/someone",
        field=field,
        value=value,
        source="orthogonal:bytemine:POST:/contacts/enrich",
        fetched_at="2026-10-12T00:00:00Z",
        status="fresh",
        previous=None,
    )


def test_the_dataset_view_masks_by_default_and_flags_it() -> None:
    view = fact_view(_row("work_email", "someone@example.com"))
    assert view["value_untrusted"] == "so***@example.com"
    assert view["pii_masked"] is True


def test_the_dataset_view_reveals_only_on_request() -> None:
    view = fact_view(_row("work_email", "someone@example.com"), reveal_pii=True)
    assert view["value_untrusted"] == "someone@example.com"
    assert view["pii_masked"] is False


def test_a_non_contact_value_is_not_flagged() -> None:
    view = fact_view(_row("job_title", "CTO"))
    assert view["value_untrusted"] == "CTO"
    assert view["pii_masked"] is False


def test_the_tools_take_reveal_pii_and_refuse_a_non_boolean(tmp_path: Path) -> None:
    deps = _deps(tmp_path, runner=ScriptedRun([FIRST]))
    _planned, out = _plan_and_run(deps)
    run_id = out["run"]["run_id"]
    rows = _call(deps, "run_rows", run_id=run_id)["rows"]
    assert rows and all(r["pii_masked"] is False for r in rows)
    assert _call(deps, "run_rows", run_id=run_id, reveal_pii=True)["rows"]
    assert _call(deps, "run_rows", run_id=run_id, reveal_pii="yes")["code"] == (
        "bad_arguments"
    )
    assert "changes" in _call(deps, "run_changes", run_id=run_id, reveal_pii=True)
