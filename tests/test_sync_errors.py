"""Unit and mock tests for sync error and helper paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pyfits import CreatedObject, Id, InstanceName
from pyfits.errors import FitsError
from pyfits.models import Graph, GraphEdge, GraphNode
from pyfits.result import Err, Ok

from bellman.graph.history import BellmanHistoryError, GraphHistory, InstanceRecord
from bellman.graph.identity import InstanceIndex
from bellman.graph.sync import (
    _bootstrap_session,
    _container_logical_name,
    _ensure_link,
    _ensure_node,
    _history_to_fits_error,
    _parent_logical_path,
    _prune_stale_graph,
    _prune_stale_registry,
    _rename_graph_kind,
    _with_ensure_context,
    init_pyfits_repo,
    prune_deleted_entity,
    sync_created_entity,
    sync_renamed_entity,
    sync_roadmap,
)


def _fits_err(code: str = "boom") -> FitsError:
    return FitsError("boom", code=code)


def _history_err() -> BellmanHistoryError:
    return BellmanHistoryError("registry missing", path=".fits/registry.json")


def _index(*records: InstanceRecord) -> InstanceIndex:
    return InstanceIndex.from_history(GraphHistory(instances=records))


def test_parent_logical_path() -> None:
    assert _parent_logical_path("goal") is None
    assert _parent_logical_path("project/demo") == "project"
    assert _parent_logical_path("project/demo/wp") == "project/demo"


def test_container_logical_name_shares_work_scope() -> None:
    assert _container_logical_name("initiative", "initiative/foo") == "work_scope"
    assert _container_logical_name("project", "project/foo") == "work_scope"
    assert _container_logical_name("goal", "goal/foo") == "goal"
    assert _container_logical_name("work_package", "project/foo/wp") == "project/foo"


def test_with_ensure_context_preserves_code_and_status() -> None:
    err = FitsError("raw", code="DuplicateGuid", status=None)
    wrapped = _with_ensure_context(err, "failed to ensure node x")
    assert wrapped.code == "DuplicateGuid"
    assert "failed to ensure node x: " in str(wrapped)


def test_history_to_fits_error() -> None:
    err = _history_to_fits_error(_history_err())
    assert err.code == "history_load_failed"
    assert ".fits/registry.json" in str(err)


def test_rename_graph_kind() -> None:
    assert _rename_graph_kind("archived-initiative") == "initiative"
    assert _rename_graph_kind("goal") == "goal"


def test_bootstrap_session_migrate_err() -> None:
    repo = MagicMock()
    with patch(
        "bellman.graph.sync.migrate_registry_schema",
        return_value=Err(_fits_err("migrate")),
    ):
        result = _bootstrap_session(repo, Path("/tmp"))
    assert isinstance(result, Err)
    assert result.err_value.code == "migrate"


def test_bootstrap_session_bootstrap_err() -> None:
    repo = MagicMock()
    with (
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch(
            "bellman.graph.sync.bootstrap_registry",
            return_value=Err(_fits_err("bootstrap")),
        ),
    ):
        result = _bootstrap_session(repo, Path("/tmp"))
    assert isinstance(result, Err)
    assert result.err_value.code == "bootstrap"


def test_migrate_legacy_wp_remove_err() -> None:
    from bellman.graph.sync import _migrate_legacy_node_ids

    repo = MagicMock()
    repo.remove.return_value = Err(_fits_err("legacy-remove"))
    index = _index(
        InstanceRecord(
            guid="ghost-guid",
            instance_name="demo--first",
            type_name="work_package",
            kind="node",
        ),
    )
    desired = {"project/demo/first"}
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _migrate_legacy_node_ids(repo, Path("/tmp"), desired)
    assert isinstance(result, Err)
    assert result.err_value.code == "legacy-remove"


def test_prune_stale_registry_index_err_is_ok() -> None:
    repo = MagicMock()
    with patch(
        "bellman.graph.sync.InstanceIndex.load",
        return_value=Err(_history_err()),
    ):
        result = _prune_stale_registry(repo, Path("/tmp"), set())
    assert isinstance(result, Ok)
    repo.remove.assert_not_called()


def test_prune_stale_registry_skips_links_and_kind() -> None:
    repo = MagicMock()
    repo.remove.return_value = Ok(None)
    index = _index(
        InstanceRecord(
            guid="kind-guid",
            instance_name="goal",
            type_name="kind",
            kind="node",
        ),
        InstanceRecord(
            guid="link-guid",
            instance_name="some-link",
            type_name="precedes_FS_Mandatory",
            kind="link",
        ),
        InstanceRecord(
            guid="stale-guid",
            instance_name="stale-goal",
            type_name="goal",
            kind="node",
        ),
        InstanceRecord(
            guid="keep-guid",
            instance_name="keep-goal",
            type_name="goal",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _prune_stale_registry(repo, Path("/tmp"), {"keep-goal"})
    assert isinstance(result, Ok)
    assert repo.remove.call_count == 1
    assert repo.remove.call_args.args[0].value == "stale-guid"


def test_prune_stale_registry_remove_err() -> None:
    repo = MagicMock()
    repo.remove.return_value = Err(_fits_err("remove"))
    index = _index(
        InstanceRecord(
            guid="stale-guid",
            instance_name="stale-goal",
            type_name="goal",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _prune_stale_registry(repo, Path("/tmp"), set())
    assert isinstance(result, Err)
    assert result.err_value.code == "remove"


def test_prune_stale_graph_index_load_err() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    with patch(
        "bellman.graph.sync.InstanceIndex.load",
        return_value=Err(_history_err()),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, set(), set())
    assert isinstance(result, Err)
    assert result.err_value.code == "history_load_failed"


def test_prune_stale_graph_reconcile_managed_link_err() -> None:
    repo = MagicMock()
    pred = InstanceRecord(
        guid="pred-guid",
        instance_name="pred",
        type_name="initiative",
        kind="node",
    )
    succ = InstanceRecord(
        guid="succ-guid",
        instance_name="succ",
        type_name="initiative",
        kind="node",
    )
    index = _index(pred, succ)
    edge = GraphEdge(
        from_id=Id("succ-guid"),
        to_id=Id("pred-guid"),
        kind="registered_link",
        link_type="precedes_FS_Mandatory_scope",
        id=Id("link-guid"),
    )
    graph = Graph(nodes=(), edges=(edge,))
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch(
            "bellman.graph.sync.reconcile_link_artifacts",
            return_value=Err(_fits_err("reconcile")),
        ),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"pred", "succ"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "reconcile"


def test_prune_stale_graph_reload_after_managed_link_err() -> None:
    repo = MagicMock()
    repo.output_graph.return_value = Err(_fits_err("reload"))
    pred = InstanceRecord(
        guid="pred-guid",
        instance_name="pred",
        type_name="initiative",
        kind="node",
    )
    succ = InstanceRecord(
        guid="succ-guid",
        instance_name="succ",
        type_name="initiative",
        kind="node",
    )
    index = _index(pred, succ)
    edge = GraphEdge(
        from_id=Id("succ-guid"),
        to_id=Id("pred-guid"),
        kind="registered_link",
        link_type="precedes_FS_Mandatory_scope",
        id=Id("link-guid"),
    )
    graph = Graph(nodes=(), edges=(edge,))
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"pred", "succ"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "reload"


def test_prune_stale_graph_stale_edge_remove_err() -> None:
    repo = MagicMock()
    repo.remove.return_value = Err(_fits_err("edge-remove"))
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    gone = InstanceRecord(
        guid="gone-guid",
        instance_name="gone",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, gone)
    edge = GraphEdge(
        from_id=Id("keep-guid"),
        to_id=Id("gone-guid"),
        kind="registered_link",
        link_type="parent_of",
        id=Id("edge-guid"),
    )
    null_id_edge = GraphEdge(
        from_id=Id("keep-guid"),
        to_id=Id("gone-guid"),
        kind="registered_link",
        link_type="parent_of",
        id=None,
    )
    graph = Graph(nodes=(), edges=(null_id_edge, edge))
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "edge-remove"


def test_prune_stale_graph_stale_edge_reload_err() -> None:
    repo = MagicMock()
    repo.remove.return_value = Ok(None)
    repo.output_graph.return_value = Err(_fits_err("edge-reload"))
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    gone = InstanceRecord(
        guid="gone-guid",
        instance_name="gone",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, gone)
    edge = GraphEdge(
        from_id=Id("keep-guid"),
        to_id=Id("gone-guid"),
        kind="registered_link",
        link_type="parent_of",
        id=Id("edge-guid"),
    )
    graph = Graph(nodes=(), edges=(edge,))
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "edge-reload"


def test_prune_stale_graph_stale_node_reconcile_err() -> None:
    repo = MagicMock()
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    stale = InstanceRecord(
        guid="stale-guid",
        instance_name="stale",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, stale)
    graph = Graph(
        nodes=(
            GraphNode(id=Id("keep-guid")),
            GraphNode(id=Id("stale-guid")),
        ),
        edges=(),
    )
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch(
            "bellman.graph.sync.reconcile_link_artifacts",
            return_value=Err(_fits_err("node-reconcile")),
        ),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "node-reconcile"


def test_prune_stale_graph_stale_node_remove_err() -> None:
    repo = MagicMock()
    repo.remove.return_value = Err(_fits_err("node-remove"))
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    stale = InstanceRecord(
        guid="stale-guid",
        instance_name="stale",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, stale)
    graph = Graph(
        nodes=(
            GraphNode(id=Id("keep-guid")),
            GraphNode(id=Id("stale-guid")),
        ),
        edges=(),
    )
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "node-remove"


def test_prune_deleted_entity_libfits_unavailable(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=False):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "lib_not_found"


def test_prune_deleted_entity_not_initialized(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=True):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "not_initialized"


def test_prune_deleted_entity_unknown_kind(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    with patch("bellman.graph.sync.libfits_available", return_value=True):
        result = prune_deleted_entity(tmp_path, "bogus", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "invalid_entity"


def test_prune_deleted_entity_open_err(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    index = _index()
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch(
            "bellman.graph.sync.Repo.open",
            return_value=Err(_fits_err("open")),
        ),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "open"


def test_prune_deleted_entity_bootstrap_err(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    index = _index(
        InstanceRecord(
            guid="g1",
            instance_name="goal/x",
            type_name="goal",
            kind="node",
        ),
    )
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch(
            "bellman.graph.sync._bootstrap_session",
            return_value=Err(_fits_err("boot")),
        ),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "boot"


def test_prune_deleted_entity_remove_err(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    repo.remove.return_value = Err(_fits_err("remove"))
    kind = InstanceRecord(
        guid="kind-goal",
        instance_name="goal",
        type_name="kind",
        kind="node",
    )
    goal = InstanceRecord(
        guid="g1",
        instance_name="x",
        type_name="goal",
        kind="node",
        parent_guid="kind-goal",
    )
    index = _index(kind, goal)
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "remove"


def test_prune_deleted_entity_validate_err(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    repo.remove.return_value = Ok(None)
    kind = InstanceRecord(
        guid="kind-goal",
        instance_name="goal",
        type_name="kind",
        kind="node",
    )
    goal = InstanceRecord(
        guid="g1",
        instance_name="x",
        type_name="goal",
        kind="node",
        parent_guid="kind-goal",
    )
    index = _index(kind, goal)
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._validate_graph",
            return_value=Err(_fits_err("validate")),
        ),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "validate"


def test_sync_renamed_entity_unknown_kind(tmp_path: Path) -> None:
    result = sync_renamed_entity(tmp_path, "bogus", "old", "new")
    assert isinstance(result, Err)
    assert result.err_value.code == "invalid_entity"


def test_sync_renamed_entity_libfits_unavailable(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=False):
        result = sync_renamed_entity(tmp_path, "goal", "old", "new")
    assert isinstance(result, Err)
    assert result.err_value.code == "lib_not_found"


def test_sync_renamed_entity_not_initialized(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=True):
        result = sync_renamed_entity(tmp_path, "goal", "old", "new")
    assert isinstance(result, Err)
    assert result.err_value.code == "not_initialized"


def test_sync_created_entity_unknown_kind(tmp_path: Path) -> None:
    result = sync_created_entity(tmp_path, "wp", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "invalid_entity"


def test_sync_created_entity_libfits_unavailable(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=False):
        result = sync_created_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "lib_not_found"


def test_sync_created_entity_not_initialized(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=True):
        result = sync_created_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "not_initialized"


def test_init_pyfits_repo_libfits_unavailable(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=False):
        result = init_pyfits_repo(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "lib_not_found"


def test_sync_roadmap_libfits_unavailable(tmp_path: Path) -> None:
    with patch("bellman.graph.sync.libfits_available", return_value=False):
        result = sync_roadmap(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "lib_not_found"


def test_ensure_node_index_err() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    with patch(
        "bellman.graph.sync.InstanceIndex.load",
        return_value=Err(_history_err()),
    ):
        result = _ensure_node(
            repo,
            Path("/tmp"),
            graph,
            type_name="goal",
            logical_name="goal/x",
            title="X",
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "history_load_failed"


def test_ensure_node_container_not_found() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    index = _index()
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_node(
            repo,
            Path("/tmp"),
            graph,
            type_name="work_package",
            logical_name="project/demo/first",
            title="First",
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "container_not_found"


def test_ensure_node_create_err_without_recovery() -> None:
    repo = MagicMock()
    repo.new_node.return_value = Err(_fits_err("create"))
    graph = Graph(nodes=(), edges=())
    kind = InstanceRecord(
        guid="kind-goal",
        instance_name="goal",
        type_name="kind",
        kind="node",
    )
    index = _index(kind)
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_node(
            repo,
            Path("/tmp"),
            graph,
            type_name="goal",
            logical_name="goal/x",
            title="X",
        )
    assert isinstance(result, Err)
    assert "failed to ensure node goal/x" in str(result.err_value)


def test_ensure_link_index_err() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    with patch(
        "bellman.graph.sync.InstanceIndex.load",
        return_value=Err(_history_err()),
    ):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="initiative/a",
            to_logical="initiative/b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "history_load_failed"


def test_ensure_link_missing_from_endpoint() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    index = _index(
        InstanceRecord(
            guid="b",
            instance_name="initiative/b",
            type_name="initiative",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="initiative/a",
            to_logical="initiative/b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "endpoint_not_found"
    assert "initiative/a" in str(result.err_value)


def test_ensure_link_missing_to_endpoint() -> None:
    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    index = _index(
        InstanceRecord(
            guid="a",
            instance_name="initiative/a",
            type_name="initiative",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="initiative/a",
            to_logical="initiative/b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "endpoint_not_found"
    assert "initiative/b" in str(result.err_value)


def test_ensure_link_existing_edge_short_circuit() -> None:
    repo = MagicMock()
    a = InstanceRecord(
        guid="a-guid",
        instance_name="a",
        type_name="initiative",
        kind="node",
    )
    b = InstanceRecord(
        guid="b-guid",
        instance_name="b",
        type_name="initiative",
        kind="node",
    )
    index = _index(a, b)
    edge = GraphEdge(
        from_id=Id("b-guid"),
        to_id=Id("a-guid"),
        kind="registered_link",
        link_type="precedes_FS_Mandatory_scope",
        id=Id("edge-guid"),
    )
    graph = Graph(nodes=(), edges=(edge,))
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="a",
            to_logical="b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Ok)
    assert result.ok_value.guid.value == "edge-guid"
    repo.new_link.assert_not_called()


def test_ensure_link_unexpected_create_name() -> None:
    repo = MagicMock()
    repo.new_link.return_value = Ok(CreatedObject(guid=Id("new"), name="initiative"))
    a = InstanceRecord(
        guid="a-guid",
        instance_name="a",
        type_name="initiative",
        kind="node",
    )
    b = InstanceRecord(
        guid="b-guid",
        instance_name="b",
        type_name="initiative",
        kind="node",
    )
    index = _index(a, b)
    graph = Graph(nodes=(), edges=())
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="a",
            to_logical="b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Err)
    assert result.err_value.code == "link_create_failed"


def test_ensure_link_create_err() -> None:
    repo = MagicMock()
    repo.new_link.return_value = Err(_fits_err("link-create"))
    a = InstanceRecord(
        guid="a-guid",
        instance_name="a",
        type_name="initiative",
        kind="node",
    )
    b = InstanceRecord(
        guid="b-guid",
        instance_name="b",
        type_name="initiative",
        kind="node",
    )
    index = _index(a, b)
    graph = Graph(nodes=(), edges=())
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_link(
            repo,
            Path("/tmp"),
            graph,
            link_type="precedes_FS_Mandatory_scope",
            from_logical="a",
            to_logical="b",
            link_name=InstanceName("link"),
        )
    assert isinstance(result, Err)
    assert "failed to ensure link" in str(result.err_value)


def test_migrate_legacy_index_err_is_ok() -> None:
    from bellman.graph.sync import _migrate_legacy_node_ids

    repo = MagicMock()
    with patch(
        "bellman.graph.sync.InstanceIndex.load",
        return_value=Err(_history_err()),
    ):
        result = _migrate_legacy_node_ids(repo, Path("/tmp"), set())
    assert isinstance(result, Ok)


def test_migrate_legacy_non_legacy_skipped() -> None:
    from bellman.graph.sync import _migrate_legacy_node_ids

    repo = MagicMock()
    index = _index(
        InstanceRecord(
            guid="g",
            instance_name="goal/modern",
            type_name="goal",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _migrate_legacy_node_ids(repo, Path("/tmp"), set())
    assert isinstance(result, Ok)
    repo.remove.assert_not_called()


def test_migrate_legacy_entity_remove_err() -> None:
    from bellman.graph.sync import _migrate_legacy_node_ids

    repo = MagicMock()
    repo.remove.return_value = Err(_fits_err("legacy-entity"))
    index = _index(
        InstanceRecord(
            guid="g",
            instance_name="old-goal",
            type_name="goal",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _migrate_legacy_node_ids(repo, Path("/tmp"), {"goal/old-goal"})
    assert isinstance(result, Err)


def test_prune_stale_graph_registry_and_repair_errs() -> None:
    repo = MagicMock()
    repo.remove.return_value = Ok(None)
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    stale = InstanceRecord(
        guid="stale-guid",
        instance_name="stale",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, stale)
    graph = Graph(
        nodes=(
            GraphNode(id=Id("keep-guid")),
            GraphNode(id=Id("stale-guid")),
        ),
        edges=(),
    )
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._prune_stale_registry",
            return_value=Err(_fits_err("reg")),
        ),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "reg"

    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch(
            "bellman.graph.sync.reconcile_link_artifacts",
            side_effect=[Ok(None), Err(_fits_err("repair"))],
        ),
        patch("bellman.graph.sync._prune_stale_registry", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "repair"


def test_prune_stale_graph_final_reload_err() -> None:
    repo = MagicMock()
    repo.remove.return_value = Ok(None)
    repo.output_graph.return_value = Err(_fits_err("final-reload"))
    keep = InstanceRecord(
        guid="keep-guid",
        instance_name="keep",
        type_name="goal",
        kind="node",
    )
    stale = InstanceRecord(
        guid="stale-guid",
        instance_name="stale",
        type_name="goal",
        kind="node",
    )
    index = _index(keep, stale)
    graph = Graph(
        nodes=(
            GraphNode(id=Id("keep-guid")),
            GraphNode(id=Id("stale-guid")),
        ),
        edges=(),
    )
    with (
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch("bellman.graph.sync._prune_stale_registry", return_value=Ok(None)),
    ):
        result = _prune_stale_graph(repo, Path("/tmp"), graph, {"keep"}, set())
    assert isinstance(result, Err)
    assert result.err_value.code == "final-reload"


def test_validate_graph_reconcile_err() -> None:
    from bellman.graph.sync import _validate_graph

    repo = MagicMock()
    with patch(
        "bellman.graph.sync.reconcile_link_artifacts",
        return_value=Err(_fits_err("val-reconcile")),
    ):
        result = _validate_graph(repo, Path("/tmp"))
    assert isinstance(result, Err)


def test_deleted_node_names_project_with_wp(tmp_path: Path) -> None:
    from bellman.graph.sync import _deleted_node_names

    index = _index(
        InstanceRecord(
            guid="p",
            instance_name="project/demo",
            type_name="project",
            kind="node",
        ),
        InstanceRecord(
            guid="wp",
            instance_name="project/demo/first",
            type_name="work_package",
            kind="node",
        ),
        InstanceRecord(
            guid="leg",
            instance_name="demo--legacy",
            type_name="work_package",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        names = _deleted_node_names("project", "demo", tmp_path)
    assert "project/demo/first" in names
    assert "demo--legacy" in names


def test_prune_deleted_reconcile_and_index_err(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync.reconcile_link_artifacts",
            return_value=Err(_fits_err("reconcile")),
        ),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.reconcile_link_artifacts", return_value=Ok(None)),
        patch(
            "bellman.graph.sync.InstanceIndex.load",
            return_value=Err(_history_err()),
        ),
    ):
        result = prune_deleted_entity(tmp_path, "goal", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "history_load_failed"


def test_parse_created_entity_errors(tmp_path: Path) -> None:
    from bellman.graph.sync import _parse_created_entity

    result = _parse_created_entity(tmp_path, "bogus", "x")
    assert isinstance(result, Err)
    assert result.err_value.code == "entity_load_failed"

    layout_root = tmp_path
    from bellman import layout as layout_mod

    layout_mod.ensure_roadmap_dirs(layout_root)
    result = _parse_created_entity(layout_root, "goal", "missing-goal")
    assert isinstance(result, Err)


def test_sync_scope_dependencies_layout_errors(tmp_path: Path) -> None:
    from bellman.graph.sync import _sync_scope_dependencies_layout
    from bellman.model import Hardness, Initiative, PrecedenceEdge, RelationType

    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    scope = Initiative(
        name="a",
        title="A",
        path="initiatives/a.md",
        introduction="",
        motivation="",
        detailed_description="",
        dependencies=(
            PrecedenceEdge(
                predecessor="clash",
                successor="a",
                relation=RelationType.FS,
                hardness=Hardness.MANDATORY,
            ),
        ),
    )
    with patch(
        "bellman.graph.sync.resolve_entity_ref_from_layout",
        side_effect=ValueError("ambiguous"),
    ):
        result = _sync_scope_dependencies_layout(repo, tmp_path, graph, scope)
    assert isinstance(result, Err)

    with (
        patch(
            "bellman.graph.sync.resolve_entity_ref_from_layout",
            return_value="initiative/b",
        ),
        patch(
            "bellman.graph.sync._ensure_link",
            return_value=Err(_fits_err("link")),
        ),
    ):
        result = _sync_scope_dependencies_layout(repo, tmp_path, graph, scope)
    assert isinstance(result, Err)


def test_sync_renamed_entity_error_matrix(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    repo.rename_instance.return_value = Err(_fits_err("rename"))
    index = _index(
        InstanceRecord(
            guid="g",
            instance_name="goal/old",
            type_name="goal",
            kind="node",
        ),
    )
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Err(_fits_err("open"))),
    ):
        assert isinstance(sync_renamed_entity(tmp_path, "goal", "old", "new"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch(
            "bellman.graph.sync._bootstrap_session",
            return_value=Err(_fits_err("boot")),
        ),
    ):
        assert isinstance(sync_renamed_entity(tmp_path, "goal", "old", "new"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync.InstanceIndex.load",
            return_value=Err(_history_err()),
        ),
    ):
        result = sync_renamed_entity(tmp_path, "goal", "old", "new")
    assert isinstance(result, Err)
    assert result.err_value.code == "history_load_failed"

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
    ):
        result = sync_renamed_entity(tmp_path, "goal", "old", "new")
    assert isinstance(result, Err)
    assert result.err_value.code == "rename"


def test_sync_renamed_legacy_and_resync(tmp_path: Path) -> None:
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    repo.remove.return_value = Ok(None)
    index = _index(
        InstanceRecord(
            guid="legacy",
            instance_name="old",
            type_name="goal",
            kind="node",
        ),
        InstanceRecord(
            guid="dash",
            instance_name="goal--old",
            type_name="goal",
            kind="node",
        ),
    )
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)),
        patch("bellman.graph.sync.sync_created_entity", return_value=Ok(None)),
        patch("bellman.graph.sync._validate_graph", return_value=Ok(None)),
    ):
        result = sync_renamed_entity(tmp_path, "goal", "old", "new")
    assert isinstance(result, Ok)


def test_sync_created_entity_error_matrix(tmp_path: Path) -> None:
    from bellman import layout as layout_mod
    from bellman.model import Goal

    layout_mod.ensure_roadmap_dirs(tmp_path)
    layout_mod.create_goal(tmp_path, "g1")
    (tmp_path / ".fits").mkdir()
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Err(_fits_err("parse")),
        ),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Ok(
                Goal(name="g1", title="G1", path="goals/g1.md", description="d")
            ),
        ),
        patch("bellman.graph.sync.Repo.open", return_value=Err(_fits_err("open"))),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Ok(
                Goal(name="g1", title="G1", path="goals/g1.md", description="d")
            ),
        ),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch(
            "bellman.graph.sync._bootstrap_session",
            return_value=Err(_fits_err("boot")),
        ),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Ok(
                Goal(name="g1", title="G1", path="goals/g1.md", description="d")
            ),
        ),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Err(_fits_err("graph")),
        ),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Ok(
                Goal(name="g1", title="G1", path="goals/g1.md", description="d")
            ),
        ),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Ok(Graph(nodes=(), edges=())),
        ),
        patch(
            "bellman.graph.sync._ensure_node",
            return_value=Err(_fits_err("ensure")),
        ),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch(
            "bellman.graph.sync._parse_created_entity",
            return_value=Ok(
                Goal(name="g1", title="G1", path="goals/g1.md", description="d")
            ),
        ),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Ok(Graph(nodes=(), edges=())),
        ),
        patch(
            "bellman.graph.sync._ensure_node",
            return_value=Ok(CreatedObject(guid=Id("g"), name="goal/g1")),
        ),
        patch(
            "bellman.graph.sync._validate_graph",
            return_value=Err(_fits_err("validate")),
        ),
    ):
        assert isinstance(sync_created_entity(tmp_path, "goal", "g1"), Err)


def test_ensure_node_already_present() -> None:
    repo = MagicMock()
    index = _index(
        InstanceRecord(
            guid="g",
            instance_name="goal/x",
            type_name="goal",
            kind="node",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_node(
            repo,
            Path("/tmp"),
            Graph(nodes=(), edges=()),
            type_name="goal",
            logical_name="goal/x",
            title="X",
        )
    assert isinstance(result, Ok)
    assert result.ok_value.guid.value == "g"
    repo.new_node.assert_not_called()


def test_ensure_node_reuses_work_scope_guid() -> None:
    repo = MagicMock()
    index = _index(
        InstanceRecord(
            guid="init-guid",
            instance_name="foo",
            type_name="initiative",
            kind="node",
            parent_guid="ws-guid",
        ),
    )
    with patch("bellman.graph.sync.InstanceIndex.load", return_value=Ok(index)):
        result = _ensure_node(
            repo,
            Path("/tmp"),
            Graph(nodes=(), edges=()),
            type_name="project",
            logical_name="project/foo",
            title="Foo",
        )
    assert isinstance(result, Ok)
    assert result.ok_value.guid == index.guid_for_work_scope_name("foo")
    repo.new_node.assert_not_called()


def test_sync_project_wps_and_scope_dep_errors() -> None:
    from bellman.graph.sync import _sync_project_wps, _sync_scope_dependencies
    from bellman.model import (
        Hardness,
        Initiative,
        PrecedenceEdge,
        Project,
        RelationType,
        Roadmap,
        WorkPackage,
    )

    repo = MagicMock()
    graph = Graph(nodes=(), edges=())
    child = WorkPackage(slug="child", title="c", description="d")
    parent = WorkPackage(
        slug="parent",
        title="p",
        description="d",
        sub_packages=(child,),
        dependencies=(
            PrecedenceEdge(
                predecessor="other",
                successor="parent",
                relation=RelationType.FS,
                hardness=Hardness.MANDATORY,
            ),
        ),
    )
    project = Project(
        name="demo",
        title="Demo",
        path="projects/demo/demo.md",
        introduction="",
        motivation="",
        detailed_description="",
        work_packages=(parent,),
    )
    with patch(
        "bellman.graph.sync._ensure_node",
        return_value=Err(_fits_err("wp")),
    ):
        assert isinstance(_sync_project_wps(repo, Path("/tmp"), graph, project), Err)

    with (
        patch(
            "bellman.graph.sync._ensure_node",
            return_value=Ok(CreatedObject(guid=Id("n"), name="n")),
        ),
        patch(
            "bellman.graph.sync._ensure_link",
            return_value=Err(_fits_err("plink")),
        ),
    ):
        assert isinstance(_sync_project_wps(repo, Path("/tmp"), graph, project), Err)

    calls = {"n": 0}

    def _link_side(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return Ok(CreatedObject(guid=Id("l"), name="l"))
        return Err(_fits_err("dep"))

    with (
        patch(
            "bellman.graph.sync._ensure_node",
            return_value=Ok(CreatedObject(guid=Id("n"), name="n")),
        ),
        patch("bellman.graph.sync._ensure_link", side_effect=_link_side),
    ):
        assert isinstance(_sync_project_wps(repo, Path("/tmp"), graph, project), Err)

    initiative = Initiative(
        name="a",
        title="A",
        path="initiatives/a.md",
        introduction="",
        motivation="",
        detailed_description="",
        dependencies=(
            PrecedenceEdge(
                predecessor="b",
                successor="a",
                relation=RelationType.FS,
                hardness=Hardness.MANDATORY,
            ),
        ),
    )
    roadmap = Roadmap(root="/tmp", initiatives=(initiative,))
    with patch(
        "bellman.graph.sync._ensure_link",
        return_value=Err(_fits_err("scope")),
    ):
        assert isinstance(
            _sync_scope_dependencies(repo, Path("/tmp"), graph, roadmap, initiative),
            Err,
        )


def test_init_pyfits_error_matrix(tmp_path: Path) -> None:
    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Err(_fits_err("open"))),
    ):
        assert isinstance(init_pyfits_repo(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch.object(repo, "init", return_value=Err(_fits_err("init"))),
    ):
        assert isinstance(init_pyfits_repo(tmp_path), Err)

    (tmp_path / ".fits").mkdir()
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch(
            "bellman.graph.sync._bootstrap_session",
            return_value=Err(_fits_err("boot")),
        ),
    ):
        assert isinstance(init_pyfits_repo(tmp_path), Err)


def test_sync_roadmap_error_matrix(tmp_path: Path) -> None:
    from bellman.model import Roadmap

    with patch("bellman.graph.sync.libfits_available", return_value=True):
        assert isinstance(sync_roadmap(tmp_path), Err)

    (tmp_path / ".fits").mkdir()
    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", side_effect=ValueError("bad md")),
    ):
        result = sync_roadmap(tmp_path)
    assert isinstance(result, Err)
    assert result.err_value.code == "roadmap_load_failed"

    repo = MagicMock()
    repo.__enter__.return_value = repo
    repo.__exit__.return_value = None
    empty = Roadmap(root=str(tmp_path))

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch(
            "bellman.graph.sync.migrate_registry_schema",
            return_value=Err(_fits_err("migrate")),
        ),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Err(_fits_err("open"))),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch(
            "bellman.graph.sync._bootstrap_session",
            return_value=Err(_fits_err("boot")),
        ),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Err(_fits_err("graph")),
        ),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Ok(Graph(nodes=(), edges=())),
        ),
        patch(
            "bellman.graph.sync._migrate_legacy_node_ids",
            return_value=Err(_fits_err("legacy")),
        ),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            side_effect=[
                Ok(Graph(nodes=(), edges=())),
                Err(_fits_err("reload2")),
            ],
        ),
        patch("bellman.graph.sync._migrate_legacy_node_ids", return_value=Ok(None)),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Ok(Graph(nodes=(), edges=())),
        ),
        patch("bellman.graph.sync._migrate_legacy_node_ids", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._validate_graph",
            return_value=Err(_fits_err("validate")),
        ),
    ):
        assert isinstance(sync_roadmap(tmp_path), Err)

    with (
        patch("bellman.graph.sync.libfits_available", return_value=True),
        patch("bellman.graph.sync.load", return_value=empty),
        patch("bellman.graph.sync.migrate_registry_schema", return_value=Ok(None)),
        patch("bellman.graph.sync.Repo.open", return_value=Ok(repo)),
        patch("bellman.graph.sync._bootstrap_session", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._reload_graph",
            return_value=Ok(Graph(nodes=(), edges=())),
        ),
        patch("bellman.graph.sync._migrate_legacy_node_ids", return_value=Ok(None)),
        patch(
            "bellman.graph.sync._prune_stale_graph",
            return_value=Err(_fits_err("prune")),
        ),
        patch("bellman.graph.sync._validate_graph", return_value=Ok(None)),
    ):
        assert isinstance(sync_roadmap(tmp_path, prune=True), Err)
