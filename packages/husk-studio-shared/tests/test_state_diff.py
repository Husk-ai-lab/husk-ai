"""State diff: added / removed / changed, by JSON-normalized value."""

from __future__ import annotations

from husk_studio_shared.state_diff import diff_is_empty, diff_states


def test_added_removed_changed() -> None:
    d = diff_states(
        {"keep": 1, "drop": 2, "edit": "old"},
        {"keep": 1, "edit": "new", "fresh": [1, 2]},
    )
    assert d["added"] == {"fresh": [1, 2]}
    assert d["removed"] == {"drop": 2}
    assert d["changed"] == {"edit": {"from": "old", "to": "new"}}
    assert not diff_is_empty(d)


def test_no_change_when_equal_after_normalization() -> None:
    # A tuple vs list of the same items normalizes to equal JSON.
    d = diff_states({"x": (1, 2)}, {"x": [1, 2]})
    assert diff_is_empty(d)
