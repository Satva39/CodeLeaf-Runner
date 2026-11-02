from __future__ import annotations

import pytest

from app.runtime_catalogue import (
    available_versions,
    installed_runtime_lookup,
    resolve_package_version,
    validate_package_catalogue,
    version_sort_key,
)


def test_package_catalogue_discards_malformed_records() -> None:
    rows = validate_package_catalogue(
        [
            {"language": "python", "language_version": "3.12.1", "installed": True},
            {"language": "", "language_version": "3.11.0"},
            None,
            {"language": "c++", "version": "17.0.0", "installed": False},
        ]
    )
    assert rows == [
        {"language": "python", "language_version": "3.12.1", "installed": True},
        {"language": "c++", "language_version": "17.0.0", "installed": False},
    ]


def test_versions_are_sorted_numerically_not_lexicographically() -> None:
    values = ["3.9.12", "3.12.0", "3.10.9", "3.9.2"]
    assert sorted(values, key=version_sort_key) == ["3.9.2", "3.9.12", "3.10.9", "3.12.0"]


def test_package_candidates_choose_latest_available_version() -> None:
    catalogue = [
        {"language": "python", "language_version": "3.9.18", "installed": True},
        {"language": "python", "language_version": "3.12.1", "installed": False},
        {"language": "node", "language_version": "22.1.0", "installed": True},
    ]
    rows = available_versions(catalogue, ["PYTHON"])
    assert [row["language_version"] for row in rows] == ["3.12.1", "3.9.18"]
    assert resolve_package_version(catalogue, ["node", "javascript"]) == catalogue[2]


def test_package_version_can_be_pinned_exactly() -> None:
    catalogue = [
        {"language": "python", "language_version": "3.11.9", "installed": False},
        {"language": "python", "language_version": "3.12.1", "installed": True},
    ]
    assert resolve_package_version(catalogue, ["python"], "3.11.9") == catalogue[0]
    assert resolve_package_version(catalogue, ["python"], "3.10.0") is None


def test_runtime_lookup_indexes_language_alias_and_runtime_kind() -> None:
    lookup = installed_runtime_lookup(
        [
            {"language": "javascript", "version": "22.1.0", "aliases": ["js"], "runtime": "node"},
            {"language": "c++", "version": "17.0.0", "aliases": ["cpp"]},
        ]
    )
    assert lookup["javascript"][0]["runtime"] == "node"
    assert lookup["node"][0]["language"] == "javascript"
    assert lookup["cpp"][0]["language"] == "c++"
