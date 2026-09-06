"""Unit tests for registry schema migration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from pyfits.result import Err, Ok

from bellman.graph.registry import KIND_TYPE
from bellman.graph.schema_migrate import (
    is_kind_root_name,
    migrate_registry_schema,
    registry_needs_schema_migration,
    registry_needs_work_scope_parent_migration,
    remove_obsolete_link_types,
)


def _write_registry(root: Path, data: dict) -> Path:
    fits = root / ".fits"
    fits.mkdir(parents=True, exist_ok=True)
    path = fits / "registry.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_needs_migration_missing_file(tmp_path: Path) -> None:
    assert registry_needs_schema_migration(tmp_path) is False
    assert isinstance(migrate_registry_schema(tmp_path), Ok)


def test_needs_migration_invalid_json(tmp_path: Path) -> None:
    fits = tmp_path / ".fits"
    fits.mkdir()
    (fits / "registry.json").write_text("{not-json", encoding="utf-8")
    assert registry_needs_schema_migration(tmp_path) is False
    assert registry_needs_work_scope_parent_migration(tmp_path) is False


def test_needs_migration_registry_not_object(tmp_path: Path) -> None:
    fits = tmp_path / ".fits"
    fits.mkdir()
    (fits / "registry.json").write_text("[]\n", encoding="utf-8")
    assert registry_needs_schema_migration(tmp_path) is False


def test_needs_migration_node_types_not_list(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": {"goal": True}})
    assert registry_needs_schema_migration(tmp_path) is False


def test_needs_migration_skips_non_dict_entries(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": ["goal", {"type": "goal"}]})
    assert registry_needs_schema_migration(tmp_path) is True


def test_needs_migration_nested_goal_returns_false(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": KIND_TYPE},
                {"type": "goal", "container_node": KIND_TYPE},
            ]
        },
    )
    assert registry_needs_schema_migration(tmp_path) is False


def test_needs_migration_legacy_goal_without_kind(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})
    assert registry_needs_schema_migration(tmp_path) is True


def test_migrate_noop_when_already_nested(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": KIND_TYPE},
                {"type": "goal", "container_node": KIND_TYPE},
            ]
        },
    )
    assert isinstance(migrate_registry_schema(tmp_path), Ok)


def test_migrate_strips_legacy_and_resets_links(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": "goal"},
                {"type": "initiative"},
                "skip-me",
                {"type": "custom_keep"},
            ],
            "instances": [
                {"type": "goal", "name": "g1"},
                {"kind": "link", "name": "l1"},
                "skip",
                {"type": "custom", "name": "keep"},
            ],
            "link_types": [
                {"link_type": "parent_of"},
                {"link_type": "precedes_FS_Mandatory"},
                {"link_type": "custom_edge"},
                "skip",
            ],
            "nested_link_types": [{"x": 1}],
            "nested_scopes": {"goal": {}},
        },
    )
    links = tmp_path / "links"
    links.mkdir()
    links_path = links / "links.jsonc"
    links_path.write_text('{"links":[{"id":"old"}]}\n', encoding="utf-8")

    result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Ok)

    data = json.loads(
        (tmp_path / ".fits" / "registry.json").read_text(encoding="utf-8")
    )
    assert data["node_types"] == [{"type": "custom_keep"}]
    assert data["instances"] == [{"type": "custom", "name": "keep"}]
    assert data["link_types"] == [{"link_type": "custom_edge"}]
    assert data["nested_link_types"] == []
    assert data["nested_scopes"] == {}

    reset = links_path.read_text(encoding="utf-8")
    assert '"links": []' in reset
    assert "fits-links-v1" in reset


def test_migrate_read_failure_returns_err(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})

    def _read(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if self == path:
            raise OSError("boom")
        return Path.read_text(self, encoding=encoding, errors=errors)

    with (
        patch(
            "bellman.graph.schema_migrate.registry_needs_schema_migration",
            return_value=True,
        ),
        patch.object(Path, "read_text", _read),
    ):
        result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "schema_migration_failed"


def test_migrate_write_registry_failure(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})
    original = Path.write_text

    def _write(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self == path:
            raise OSError("denied")
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    with patch.object(Path, "write_text", _write):
        result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "schema_migration_failed"


def test_migrate_write_links_failure(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})
    links = tmp_path / "links"
    links.mkdir()
    links_path = links / "links.jsonc"
    links_path.write_text("{}\n", encoding="utf-8")
    original = Path.write_text

    def _write(
        self: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if self == links_path:
            raise OSError("links denied")
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    with patch.object(Path, "write_text", _write):
        result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "schema_migration_failed"


def test_is_kind_root_name() -> None:
    assert is_kind_root_name("goal") is True
    assert is_kind_root_name("work_scope") is True
    assert is_kind_root_name("wp") is False


def test_work_scope_parent_migration_needed_for_split_kind_roots(
    tmp_path: Path,
) -> None:
    init_guid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    kind_guid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": KIND_TYPE},
                {"type": "goal", "container_node": KIND_TYPE},
            ],
            "instances": [
                {
                    "guid": kind_guid,
                    "name": "initiative",
                    "type": KIND_TYPE,
                    "kind": "node",
                },
                {
                    "guid": init_guid,
                    "name": "kri-image-tools",
                    "type": "initiative",
                    "kind": "node",
                    "parent_guid": kind_guid,
                },
                "skip",
                {"guid": 1, "name": "bad"},
            ],
        },
    )
    assert registry_needs_work_scope_parent_migration(tmp_path) is True
    assert registry_needs_schema_migration(tmp_path) is True


def test_work_scope_parent_migration_not_needed_when_hosted(
    tmp_path: Path,
) -> None:
    work_scope = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    init_guid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": KIND_TYPE},
                {"type": "goal", "container_node": KIND_TYPE},
            ],
            "instances": [
                {
                    "guid": work_scope,
                    "name": "work_scope",
                    "type": KIND_TYPE,
                    "kind": "node",
                },
                {
                    "guid": init_guid,
                    "name": "settings-manager",
                    "type": "initiative",
                    "kind": "node",
                    "parent_guid": work_scope,
                },
                {
                    "guid": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "name": "a-link",
                    "type": "precedes_FS_Mandatory_scope",
                    "kind": "link",
                },
            ],
        },
    )
    assert registry_needs_work_scope_parent_migration(tmp_path) is False
    assert registry_needs_schema_migration(tmp_path) is False


def test_work_scope_parent_migration_skips_non_list_instances(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "node_types": [
                {"type": KIND_TYPE},
                {"type": "goal", "container_node": KIND_TYPE},
            ],
            "instances": {"guid": "x"},
        },
    )
    assert registry_needs_work_scope_parent_migration(tmp_path) is False


def test_work_scope_parent_migration_missing_file(tmp_path: Path) -> None:
    assert registry_needs_work_scope_parent_migration(tmp_path) is False


def test_migrate_removes_nodes_dir(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})
    nodes = tmp_path / "nodes"
    nested = nodes / "kind" / "initiative"
    nested.mkdir(parents=True)
    (nested / "stale.txt").write_text("old", encoding="utf-8")

    result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Ok)
    assert not nodes.exists()


def test_migrate_rmtree_nodes_failure(tmp_path: Path) -> None:
    _write_registry(tmp_path, {"node_types": [{"type": "goal"}]})
    nodes = tmp_path / "nodes"
    nodes.mkdir()

    def _rmtree(_path: object, *_args: object, **_kwargs: object) -> None:
        raise OSError("busy")

    with patch("bellman.graph.schema_migrate.shutil.rmtree", _rmtree):
        result = migrate_registry_schema(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "schema_migration_failed"


def test_remove_obsolete_link_types_strips_promoted_from(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        {
            "nested_link_types": [
                {
                    "link_type": "promoted_from",
                    "in_type": "project",
                    "out_type": "initiative",
                    "next": 1,
                },
                {
                    "link_type": "parent_of",
                    "in_type": "work_package",
                    "out_type": "work_package",
                    "next": 1,
                },
            ],
            "instances": [
                {
                    "guid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "name": "promoted_from-1",
                    "type": "promoted_from",
                    "kind": "link",
                }
            ],
        },
    )
    result = remove_obsolete_link_types(tmp_path)
    assert isinstance(result, Ok)
    assert result.ok_value == {"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}
    data = json.loads(
        (tmp_path / ".fits" / "registry.json").read_text(encoding="utf-8")
    )
    assert all(
        entry.get("link_type") != "promoted_from"
        for entry in data.get("nested_link_types", [])
    )
    assert all(
        inst.get("guid") != "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for inst in data.get("instances", [])
    )
