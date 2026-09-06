"""Integration tests for graph sync after roadmap mutations."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from pyfits import Repo
from pyfits.result import Err, Ok

from bellman import layout
from bellman.graph.desired import (
    DesiredLink,
    desired_link_from_graph_edge,
    entity_node_id,
)
from bellman.graph.history import load_graph_history
from bellman.graph.identity import InstanceIndex
from bellman.graph.sync import (
    init_pyfits_repo,
    libfits_available,
    prune_deleted_entity,
    sync_roadmap,
)


def _has_live_logical(root: Path, logical_name: str) -> bool:
    result = InstanceIndex.load(root)
    if isinstance(result, Err):
        return False
    return logical_name in result.ok_value.live_node_names()


def _logical_type(root: Path, logical_name: str) -> str | None:
    result = InstanceIndex.load(root)
    if isinstance(result, Err):
        return None
    inst = result.ok_value.by_name.get(logical_name)
    return None if inst is None else inst.type_name


def _bootstrap_pyfits(root: Path) -> None:
    result = init_pyfits_repo(root)
    assert isinstance(result, Ok)


@pytest.mark.integration
def test_create_initiative_registers_in_graph(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    _bootstrap_pyfits(tmp_path)
    layout.create_initiative(tmp_path, "settings-manager")
    result = sync_roadmap(tmp_path)
    assert isinstance(result, Ok)
    assert _logical_type(tmp_path, "initiative/settings-manager") == "initiative"
    assert _has_live_logical(tmp_path, "initiative/settings-manager")


@pytest.mark.integration
def test_delete_prunes_graph_node(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    _bootstrap_pyfits(tmp_path)
    layout.create_goal(tmp_path, "my-goal")
    assert isinstance(sync_roadmap(tmp_path), Ok)
    assert _has_live_logical(tmp_path, "goal/my-goal")
    layout.delete_entity(tmp_path, "my-goal")
    result = prune_deleted_entity(tmp_path, "goal", "my-goal")
    assert isinstance(result, Ok)
    assert not _has_live_logical(tmp_path, "goal/my-goal")
    history = load_graph_history(tmp_path)
    assert isinstance(history, Ok)
    assert all(
        not (i.type_name == "goal" and i.instance_name == "my-goal")
        for i in history.ok_value.instances
    )


def _child_guid(root: Path, logical_name: str) -> str | None:
    result = InstanceIndex.load(root)
    if isinstance(result, Err):
        return None
    return result.ok_value.child_guid_for_logical(logical_name)


@pytest.mark.integration
def test_promote_registers_project(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    _bootstrap_pyfits(tmp_path)
    layout.create_initiative(tmp_path, "grow-feature")
    assert isinstance(sync_roadmap(tmp_path), Ok)
    assert _logical_type(tmp_path, "initiative/grow-feature") == "initiative"
    initiative_guid = _child_guid(tmp_path, "initiative/grow-feature")
    layout.promote_initiative(tmp_path, "grow-feature")
    result = sync_roadmap(tmp_path, prune=True)
    assert isinstance(result, Ok)
    assert _logical_type(tmp_path, "project/grow-feature") == "project"
    assert _has_live_logical(tmp_path, "project/grow-feature")
    assert not _has_live_logical(tmp_path, "initiative/grow-feature")
    assert initiative_guid is not None
    assert _child_guid(tmp_path, "project/grow-feature") == initiative_guid


def _add_scope_dependency(root: Path, *, dep: str, target: str) -> None:
    dep_path = layout.initiative_path(root, dep)
    dep_path.write_text(
        dep_path.read_text(encoding="utf-8").replace(
            "## Dependencies\n\n",
            f"## Dependencies\n\n- {target} [FS, Mandatory]\n",
        ),
        encoding="utf-8",
    )


def _graph_has_desired_link(root: Path, link: DesiredLink) -> bool:
    return _desired_link_child_guid(root, link) is not None


def _desired_link_child_guid(root: Path, link: DesiredLink) -> str | None:
    open_result = Repo.open(root)
    if not isinstance(open_result, Ok):
        return None
    with open_result.ok_value as repo:
        graph_result = repo.output_graph(include_nested=True)
    if not isinstance(graph_result, Ok):
        return None
    index = InstanceIndex.load(root)
    if not isinstance(index, Ok):
        return None
    for edge in graph_result.ok_value.edges:
        desired = desired_link_from_graph_edge(
            link_type=edge.link_type,
            from_id_value=edge.from_id.value,
            to_id_value=edge.to_id.value,
            index=index.ok_value,
        )
        if desired == link and edge.id is not None:
            return edge.id.value.rsplit("/", 1)[-1]
    return None


@pytest.mark.integration
def test_promote_keeps_scope_link_to_remaining_initiative(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    layout.create_initiative(tmp_path, "kri-image-tools")
    layout.create_initiative(tmp_path, "settings-manager-mvp")
    _add_scope_dependency(
        tmp_path, dep="settings-manager-mvp", target="kri-image-tools"
    )
    _bootstrap_pyfits(tmp_path)
    assert isinstance(sync_roadmap(tmp_path), Ok)

    layout.promote_initiative(tmp_path, "kri-image-tools")
    result = sync_roadmap(tmp_path, prune=True)
    assert isinstance(result, Ok)
    assert _logical_type(tmp_path, "project/kri-image-tools") == "project"
    assert _has_live_logical(
        tmp_path, entity_node_id("initiative", "settings-manager-mvp")
    )
    promoted_guid = _child_guid(tmp_path, "project/kri-image-tools")
    assert promoted_guid is not None
    assert _graph_has_desired_link(
        tmp_path,
        DesiredLink(
            "precedes_FS_Mandatory_scope",
            "project/kri-image-tools",
            "initiative/settings-manager-mvp",
        ),
    )


@pytest.mark.integration
def test_promote_keeps_scope_link_from_new_project(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    layout.create_initiative(tmp_path, "kri-image-tools")
    layout.create_initiative(tmp_path, "settings-manager-mvp")
    _add_scope_dependency(
        tmp_path, dep="kri-image-tools", target="settings-manager-mvp"
    )
    _bootstrap_pyfits(tmp_path)
    assert isinstance(sync_roadmap(tmp_path), Ok)

    layout.promote_initiative(tmp_path, "kri-image-tools")
    result = sync_roadmap(tmp_path, prune=True)
    assert isinstance(result, Ok)
    assert _graph_has_desired_link(
        tmp_path,
        DesiredLink(
            "precedes_FS_Mandatory_scope",
            "initiative/settings-manager-mvp",
            "project/kri-image-tools",
        ),
    )


@pytest.mark.integration
def test_promote_second_sync_keeps_scope_link_guid(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    layout.create_initiative(tmp_path, "kri-image-tools")
    layout.create_initiative(tmp_path, "settings-manager-mvp")
    _add_scope_dependency(
        tmp_path, dep="settings-manager-mvp", target="kri-image-tools"
    )
    _bootstrap_pyfits(tmp_path)
    assert isinstance(sync_roadmap(tmp_path), Ok)
    layout.promote_initiative(tmp_path, "kri-image-tools")
    assert isinstance(sync_roadmap(tmp_path, prune=True), Ok)
    link = DesiredLink(
        "precedes_FS_Mandatory_scope",
        "project/kri-image-tools",
        "initiative/settings-manager-mvp",
    )
    first = _desired_link_child_guid(tmp_path, link)
    assert first is not None
    assert isinstance(sync_roadmap(tmp_path), Ok)
    assert _desired_link_child_guid(tmp_path, link) == first


def _inject_nested_link(
    root: Path, *, link_type: str, in_guid: str, out_guid: str
) -> str:
    """Write a nested subgraph link that pyfits will not create across parents."""
    link_guid = str(uuid.uuid4())
    for sub in (root / "nodes").rglob("subgraph.jsonc"):
        data = json.loads(sub.read_text(encoding="utf-8"))
        nodes = data.get("nodes") or []
        if not any(node.get("guid") == in_guid for node in nodes):
            continue
        data.setdefault("links", []).append(
            {
                "guid": link_guid,
                "link_type": link_type,
                "in": in_guid,
                "out": out_guid,
            }
        )
        sub.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return link_guid
    raise AssertionError(f"subgraph containing {in_guid} not found")


@pytest.mark.integration
def test_demote_drops_project_only_supports_link(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    layout.create_initiative(tmp_path, "kri-image-tools")
    layout.create_goal(tmp_path, "reduce-churn")
    _bootstrap_pyfits(tmp_path)
    assert isinstance(sync_roadmap(tmp_path), Ok)
    layout.promote_initiative(tmp_path, "kri-image-tools")
    assert isinstance(sync_roadmap(tmp_path), Ok)
    project_guid = _child_guid(tmp_path, "project/kri-image-tools")
    goal_guid = _child_guid(tmp_path, "goal/reduce-churn")
    assert project_guid is not None
    assert goal_guid is not None
    _inject_nested_link(
        tmp_path,
        link_type="supports",
        in_guid=project_guid,
        out_guid=goal_guid,
    )
    open_result = Repo.open(tmp_path)
    assert isinstance(open_result, Ok)
    with open_result.ok_value as repo:
        graph_result = repo.output_graph(include_nested=True)
    assert isinstance(graph_result, Ok)
    assert any(edge.link_type == "supports" for edge in graph_result.ok_value.edges)

    layout.demote_project(tmp_path, "kri-image-tools")
    result = sync_roadmap(tmp_path)
    assert isinstance(result, Ok)
    assert _logical_type(tmp_path, "initiative/kri-image-tools") == "initiative"
    assert _child_guid(tmp_path, "initiative/kri-image-tools") == project_guid
    open_result = Repo.open(tmp_path)
    assert isinstance(open_result, Ok)
    with open_result.ok_value as repo:
        graph_result = repo.output_graph(include_nested=True)
    assert isinstance(graph_result, Ok)
    assert all(edge.link_type != "supports" for edge in graph_result.ok_value.edges)


@pytest.mark.integration
def test_sync_coexists_goal_and_initiative_same_name(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    _bootstrap_pyfits(tmp_path)
    layout.create_goal(tmp_path, "system-mci")
    layout.create_initiative(tmp_path, "system-mci")
    result = sync_roadmap(tmp_path)
    assert isinstance(result, Ok)
    assert _has_live_logical(tmp_path, "goal/system-mci")
    assert _has_live_logical(tmp_path, "initiative/system-mci")


@pytest.mark.integration
def test_demote_removes_project_and_work_packages(tmp_path: Path) -> None:
    if not libfits_available():
        pytest.skip("libfits not available")
    layout.ensure_roadmap_dirs(tmp_path)
    layout.create_initiative(tmp_path, "kri-image-tools")
    layout.create_initiative(tmp_path, "settings-manager-mvp")
    _add_scope_dependency(
        tmp_path, dep="settings-manager-mvp", target="kri-image-tools"
    )
    _bootstrap_pyfits(tmp_path)
    assert isinstance(sync_roadmap(tmp_path), Ok)
    initiative_guid = _child_guid(tmp_path, "initiative/kri-image-tools")

    layout.promote_initiative(tmp_path, "kri-image-tools")
    layout.work_packages_path(tmp_path, "kri-image-tools").write_text(
        "version: 1\n\nwork_packages:\n  - title: wp-a\n    description: TBD.\n",
        encoding="utf-8",
    )
    assert isinstance(sync_roadmap(tmp_path, prune=True), Ok)
    assert _has_live_logical(tmp_path, "project/kri-image-tools/wp-a")

    layout.demote_project(tmp_path, "kri-image-tools")
    result = sync_roadmap(tmp_path)
    assert isinstance(result, Ok)
    assert _logical_type(tmp_path, "initiative/kri-image-tools") == "initiative"
    assert _has_live_logical(tmp_path, "initiative/kri-image-tools")
    assert not _has_live_logical(tmp_path, "project/kri-image-tools")
    assert not _has_live_logical(tmp_path, "project/kri-image-tools/wp-a")
    assert initiative_guid is not None
    assert _child_guid(tmp_path, "initiative/kri-image-tools") == initiative_guid
    assert _graph_has_desired_link(
        tmp_path,
        DesiredLink(
            "precedes_FS_Mandatory_scope",
            "initiative/kri-image-tools",
            "initiative/settings-manager-mvp",
        ),
    )

    layout.promote_initiative(tmp_path, "kri-image-tools")
    result = sync_roadmap(tmp_path)
    assert isinstance(result, Ok)
    assert _has_live_logical(tmp_path, "project/kri-image-tools")
    assert _has_live_logical(tmp_path, "project/kri-image-tools/wp-a")
    assert not _has_live_logical(tmp_path, "initiative/kri-image-tools")
    assert initiative_guid is not None
    assert _child_guid(tmp_path, "project/kri-image-tools") == initiative_guid
    assert _graph_has_desired_link(
        tmp_path,
        DesiredLink(
            "precedes_FS_Mandatory_scope",
            "project/kri-image-tools",
            "initiative/settings-manager-mvp",
        ),
    )
