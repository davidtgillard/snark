"""Sync roadmap model to pyfits graph."""

from __future__ import annotations

import uuid
from pathlib import Path

from pyfits import CreatedObject, Id, InstanceName, ObjectTypeName, Repo, ValidateResult
from pyfits._native import load_library
from pyfits.errors import FitsError
from pyfits.models import Graph
from pyfits.result import Err, Ok, Result

from bellman import layout
from bellman.graph import link_naming
from bellman.graph.desired import (
    DesiredLink,
    desired_link_from_graph_edge,
    desired_links,
    desired_node_ids,
    entity_node_id,
    flatten_wps,
    goal_node_id,
    local_name_from_node_id,
    milestone_node_id,
    resolve_entity_ref_from_layout,
    resolve_scope_ref,
    resolve_wp_ref,
    scope_node_id,
    wp_node_id,
)
from bellman.graph.fits_errors import (
    ignore_duplicate_instance,
    ignore_duplicate_link,
    ignore_nothing_to_remove,
)
from bellman.graph.history import BellmanHistoryError
from bellman.graph.identity import InstanceIndex
from bellman.graph.legacy import is_legacy_dash_qualified_id, is_legacy_flat_node_id
from bellman.graph.links_file import reconcile_link_artifacts
from bellman.graph.registry import (
    KIND_TYPE,
    WORK_SCOPE_KIND_ROOT,
    bellman_link_types,
    bootstrap_registry,
    ensure_kind_roots,
    precedes_scope_link_types,
)
from bellman.graph.schema_migrate import (
    migrate_registry_schema,
    remove_obsolete_link_types,
)
from bellman.model import Goal, Initiative, Milestone, Project, Roadmap
from bellman.parse.goal import parse_goal
from bellman.parse.milestone import parse_milestone
from bellman.parse.work_scope import parse_work_scope
from bellman.roadmap import load


def libfits_available() -> bool:
    """Return True when libfits can be loaded."""
    return isinstance(load_library(), Ok)


def _parent_logical_path(logical_name: str) -> str | None:
    if "/" not in logical_name:
        return None
    return logical_name.rsplit("/", 1)[0]


def _container_logical_name(type_name: str, logical_name: str) -> str | None:
    """Return the graph container that should host ``logical_name``.

    Initiatives and projects share the ``work_scope`` kind-root so mixed
    scope-precedence links are nested siblings.
    """
    if type_name in {"initiative", "project"}:
        return WORK_SCOPE_KIND_ROOT
    return _parent_logical_path(logical_name)


_WORK_SCOPE_TYPES = frozenset({"initiative", "project"})
_PROJECT_ONLY_LINK_TYPES = frozenset({"supports", "targets"})


def _prepare_registry_files(root: Path) -> Result[None, FitsError]:
    """Migrate schema and strip obsolete link types before opening a repo."""
    migrated = migrate_registry_schema(root)
    if isinstance(migrated, Err):
        return migrated
    obsolete_links = remove_obsolete_link_types(root)
    if isinstance(obsolete_links, Err):
        return obsolete_links
    if not obsolete_links.ok_value:
        return Ok(None)
    reconciled = reconcile_link_artifacts(root, drop_link_guids=obsolete_links.ok_value)
    if isinstance(reconciled, Err):
        return reconciled
    return Ok(None)


def _bootstrap_session(repo: Repo, root: Path) -> Result[None, FitsError]:
    migrated = migrate_registry_schema(root)
    if isinstance(migrated, Err):
        return migrated
    boot = bootstrap_registry(repo)
    if isinstance(boot, Err):
        return boot
    return ensure_kind_roots(repo)


def _migrate_legacy_node_ids(
    repo: Repo,
    root: Path,
    desired: set[str],
) -> Result[None, FitsError]:
    """Remove legacy flat/``--`` root nodes that map into desired slash paths.

    Nested creates cannot rename across scope; markdown sync recreates nodes.
    """
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Ok(None)
    index = index_result.ok_value

    for logical_name, inst in list(index.by_name.items()):
        if inst.kind != "node" or inst.type_name == KIND_TYPE:
            continue
        if logical_name in desired:
            continue
        legacy = (
            is_legacy_flat_node_id(inst.type_name, inst.instance_name)
            or is_legacy_dash_qualified_id(logical_name)
            or is_legacy_dash_qualified_id(inst.instance_name)
        )
        if not legacy:
            continue
        # Drop when a slash-qualified desired id covers this entity.
        if inst.type_name == "work_package":
            # project--slug → project/project/slug
            if "--" in inst.instance_name:
                project_name, slug = inst.instance_name.split("--", 1)
                if wp_node_id(project_name, slug) in desired:
                    removed = ignore_nothing_to_remove(repo.remove(Id(inst.guid)))
                    if isinstance(removed, Err):
                        return removed
            continue
        natural = inst.instance_name
        if "--" in natural:
            _prefix, natural = natural.split("--", 1)
        qualified = entity_node_id(inst.type_name, natural)
        if qualified in desired:
            removed = ignore_nothing_to_remove(repo.remove(Id(inst.guid)))
            if isinstance(removed, Err):
                return removed
    return Ok(None)


def _logical_scope_name(scope: Initiative | Project) -> str:
    return scope_node_id(scope)


def _logical_wp_name(project_name: str, slug: str) -> str:
    return wp_node_id(project_name, slug)


def _logical_milestone_name(name: str) -> str:
    return milestone_node_id(name)


def _logical_goal_name(name: str) -> str:
    return goal_node_id(name)


def _reload_graph(repo: Repo) -> Result[Graph, FitsError]:
    """Refresh the in-memory graph snapshot from the repository."""
    return repo.output_graph(include_nested=True)


def _desired_graph_node_names(roadmap: Roadmap) -> set[str]:
    """Logical node names that should exist after a full roadmap sync."""
    return desired_node_ids(roadmap)


def _history_to_fits_error(err: BellmanHistoryError) -> FitsError:
    return FitsError(err.format(), code="history_load_failed")


def _prune_stale_registry(
    repo: Repo,
    root: Path,
    desired: set[str],
) -> Result[None, FitsError]:
    """Remove live registry node instances whose logical name is not in ``desired``."""
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Ok(None)
    for logical_name, inst in index_result.ok_value.by_name.items():
        if inst.kind != "node":
            continue
        if inst.type_name == KIND_TYPE:
            continue
        if logical_name in desired:
            continue
        removed = ignore_nothing_to_remove(repo.remove(Id(inst.guid)))
        if isinstance(removed, Err):
            return removed
    return Ok(None)


def _prune_stale_graph(
    repo: Repo,
    root: Path,
    graph: Graph,
    desired: set[str],
    desired_link_set: set[DesiredLink],
) -> Result[Graph, FitsError]:
    """Remove stale links and nodes from the registry and graph snapshot."""
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    index = index_result.ok_value

    prune_link_types = bellman_link_types()
    stale_managed_links = [
        edge
        for edge in graph.edges
        if edge.link_type in prune_link_types
        and (
            desired_link := desired_link_from_graph_edge(
                link_type=edge.link_type,
                from_id_value=edge.from_id.value,
                to_id_value=edge.to_id.value,
                index=index,
            )
        )
        is not None
        and desired_link not in desired_link_set
    ]
    stale_managed_link_guids: set[str] = set()
    for edge in stale_managed_links:
        if edge.id is None:
            continue
        stale_managed_link_guids.add(edge.id.value)
        stale_managed_link_guids.add(edge.id.value.rsplit("/", 1)[-1])
    if stale_managed_link_guids:
        reconciled = reconcile_link_artifacts(
            root,
            drop_link_guids=stale_managed_link_guids,
        )
        if isinstance(reconciled, Err):
            return reconciled
        reloaded = _reload_graph(repo)
        if isinstance(reloaded, Err):
            return reloaded
        graph = reloaded.ok_value

    stale_edges = [
        edge
        for edge in graph.edges
        if (index.name_for_guid(edge.from_id.value) or "") not in desired
        or (index.name_for_guid(edge.to_id.value) or "") not in desired
    ]
    for edge in stale_edges:
        link_id = edge.id
        if link_id is None:
            continue
        removed = ignore_nothing_to_remove(repo.remove(link_id))
        if isinstance(removed, Err):
            return removed
    if stale_edges:
        reloaded = _reload_graph(repo)
        if isinstance(reloaded, Err):
            return reloaded
        graph = reloaded.ok_value

    stale_nodes = [
        node
        for node in graph.nodes
        if (name := index.name_for_guid(node.id.value)) is not None
        and name not in desired
        and name not in index.live_kind_names()
    ]
    stale_logical_names = {
        name
        for node in stale_nodes
        if (name := index.name_for_guid(node.id.value)) is not None
    }
    if stale_logical_names:
        reconciled = reconcile_link_artifacts(
            root,
            drop_touching_nodes=stale_logical_names,
        )
        if isinstance(reconciled, Err):
            return reconciled

    for node in stale_nodes:
        removed = ignore_nothing_to_remove(repo.remove(node.id))
        if isinstance(removed, Err):
            return removed

    registry_pruned = _prune_stale_registry(repo, root, desired)
    if isinstance(registry_pruned, Err):
        return registry_pruned

    repaired = reconcile_link_artifacts(root)
    if isinstance(repaired, Err):
        return repaired

    if stale_nodes or stale_edges or stale_managed_link_guids:
        reloaded = _reload_graph(repo)
        if isinstance(reloaded, Err):
            return reloaded
        return Ok(reloaded.ok_value)
    return Ok(graph)


_ENTITY_KIND_TO_TYPE = {
    "initiative": "initiative",
    "archived-initiative": "initiative",
    "project": "project",
    "milestone": "milestone",
    "goal": "goal",
}


def _deleted_node_names(kind: str, name: str, root: Path) -> set[str]:
    """Return logical node names to remove after deleting a layout entity."""
    type_name = _ENTITY_KIND_TO_TYPE.get(kind)
    if type_name is None:
        msg = f"unknown entity kind {kind!r}"
        raise ValueError(msg)

    names: set[str] = {entity_node_id(type_name, name), name}
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Ok) and kind == "project":
        project_logical = entity_node_id("project", name)
        prefix = f"{project_logical}/"
        for logical_name, inst in index_result.ok_value.by_name.items():
            if inst.kind == "node" and logical_name.startswith(prefix):
                names.add(logical_name)
        # Legacy -- scheme
        legacy_prefix = f"{name}--"
        for logical_name, inst in index_result.ok_value.by_name.items():
            if inst.kind == "node" and (
                logical_name.startswith(legacy_prefix)
                or inst.instance_name.startswith(legacy_prefix)
            ):
                names.add(logical_name)
    return names


def _park_and_remove_instance(
    repo: Repo,
    guid: Id,
    *,
    base_name: str,
) -> Result[None, FitsError]:
    """Rename ``guid`` aside, then remove it so the original name is not tombstoned.

    Args:
        repo: Open pyfits repository session.
        guid: Wire id of the instance to drop.
        base_name: Original local instance name (used only to build a unique
            parked name).

    Returns:
        ``Ok(None)`` when the instance is parked and removed.
        ``Err(FitsError)`` when rename or remove fails.
    """
    parked = InstanceName(f"{base_name}-archived-{uuid.uuid4().hex[:8]}")
    renamed = repo.rename_instance(guid=guid, new_name=parked)
    if isinstance(renamed, Err):
        return renamed
    return ignore_nothing_to_remove(repo.remove(guid))


def _work_package_logical_names(project_name: str, root: Path) -> set[str]:
    """Return live work-package logical names nested under a project."""
    project_logical = entity_node_id("project", project_name)
    prefix = f"{project_logical}/"
    return {
        logical_name
        for logical_name in _deleted_node_names("project", project_name, root)
        if logical_name.startswith(prefix)
    }


def _remove_nested_work_packages(
    repo: Repo,
    root: Path,
    project_name: str,
) -> Result[None, FitsError]:
    """Park and remove all work-package nodes under a project.

    Args:
        repo: Open pyfits repository session.
        root: Roadmap root directory.
        project_name: Project natural name (kebab-case).

    Returns:
        ``Ok(None)`` when work packages are removed or none exist.
        ``Err(FitsError)`` when removal fails.
    """
    wp_names = _work_package_logical_names(project_name, root)
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    index = index_result.ok_value
    for logical_name in sorted(
        wp_names,
        key=lambda node: node.count("/"),
        reverse=True,
    ):
        guid = index.guid_for_name(logical_name)
        if guid is None:
            continue
        slug = logical_name.rsplit("/", 1)[-1]
        parked_wp = _park_and_remove_instance(repo, guid, base_name=slug)
        if isinstance(parked_wp, Err):
            return parked_wp
    return Ok(None)


def _retype_drop_link_types(to_type: str) -> frozenset[str]:
    """Link types that cannot remain after retyping a work-scope to ``to_type``."""
    types: set[str] = set(precedes_scope_link_types())
    if to_type == "initiative":
        types |= _PROJECT_ONLY_LINK_TYPES
    return frozenset(types)


def _drop_incident_links(
    repo: Repo,
    root: Path,
    graph: Graph,
    guid: Id,
    link_types: frozenset[str],
) -> Result[Graph, FitsError]:
    """Remove incident links of ``link_types`` so libfits can retype ``guid``.

    ``precedes_*_scope`` links are registered as ``project``→``project``.
    ``supports`` / ``targets`` are project-typed. Sync recreates markdown
    scope edges afterward.

    Args:
        repo: Open pyfits repository session.
        root: Roadmap root directory.
        graph: Current graph snapshot.
        guid: Work-scope wire id whose incident links may be dropped.
        link_types: Link type names to remove when incident on ``guid``.

    Returns:
        ``Ok(graph)`` unchanged or reloaded after removals.
        ``Err(FitsError)`` when link removal fails.
    """
    stale_link_guids: set[str] = set()
    for edge in graph.edges:
        if edge.link_type not in link_types:
            continue
        if edge.id is None:
            continue
        if not (_same_endpoint(edge.from_id, guid) or _same_endpoint(edge.to_id, guid)):
            continue
        stale_link_guids.add(edge.id.value)
        stale_link_guids.add(edge.id.value.rsplit("/", 1)[-1])
        removed = ignore_nothing_to_remove(repo.remove(edge.id))
        if isinstance(removed, Err):
            return Err(removed.err_value)

    if not stale_link_guids:
        return Ok(graph)

    reconciled = reconcile_link_artifacts(root, drop_link_guids=stale_link_guids)
    if isinstance(reconciled, Err):
        return reconciled
    reloaded = _reload_graph(repo)
    if isinstance(reloaded, Err):
        return reloaded
    return Ok(reloaded.ok_value)


def _retype_work_scope(
    repo: Repo,
    root: Path,
    graph: Graph,
    name: str,
    *,
    from_type: str,
    to_type: str,
) -> Result[Graph, FitsError]:
    """Change a work-scope instance type while preserving GUID and local name.

    Args:
        repo: Open pyfits repository session.
        root: Roadmap root directory.
        graph: Current graph snapshot.
        name: Work-scope natural name (kebab-case).
        from_type: Expected current type (``initiative`` or ``project``).
        to_type: Target type (``initiative`` or ``project``).

    Returns:
        ``Ok(graph)`` when the instance is retyped or already has ``to_type``.
        ``Err(FitsError)`` when libfits retype fails.
    """
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    guid = index_result.ok_value.guid_for_name(entity_node_id(from_type, name))
    if guid is None:
        return Ok(graph)

    dropped = _drop_incident_links(
        repo,
        root,
        graph,
        guid,
        _retype_drop_link_types(to_type),
    )
    if isinstance(dropped, Err):
        return dropped

    changed = repo.change_instance_type(
        guid=guid,
        new_type=ObjectTypeName(to_type),
    )
    if isinstance(changed, Err):
        return Err(changed.err_value)
    reloaded = _reload_graph(repo)
    if isinstance(reloaded, Err):
        return reloaded
    return Ok(reloaded.ok_value)


def _retype_parked_project_to_initiative(
    repo: Repo,
    root: Path,
    graph: Graph,
    name: str,
) -> Result[Graph, FitsError]:
    """Retype a parked project back to an initiative; drop nested work packages.

    Work scopes share a local name under ``work_scope``. Demote keeps the same
    GUID and flips ``project`` → ``initiative`` after removing work packages.

    Args:
        repo: Open pyfits repository session.
        root: Roadmap root directory.
        graph: Current graph snapshot; returned unchanged when nothing changes.
        name: Project natural name (kebab-case).

    Returns:
        ``Ok(graph)`` with a reloaded snapshot when nodes were changed, or the
        original ``graph`` when no matching project instance existed.
        ``Err(FitsError)`` when link repair or retype fails.
    """
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    if index_result.ok_value.guid_for_name(entity_node_id("project", name)) is None:
        return Ok(graph)

    wp_names = _work_package_logical_names(name, root)
    if wp_names:
        reconciled = reconcile_link_artifacts(root, drop_touching_nodes=wp_names)
        if isinstance(reconciled, Err):
            return reconciled
    removed_wps = _remove_nested_work_packages(repo, root, name)
    if isinstance(removed_wps, Err):
        return removed_wps
    reloaded = _reload_graph(repo)
    if isinstance(reloaded, Err):
        return reloaded
    graph = reloaded.ok_value
    return _retype_work_scope(
        repo,
        root,
        graph,
        name,
        from_type="project",
        to_type="initiative",
    )


def _rename_graph_kind(kind: str) -> str:
    if kind == "archived-initiative":
        return "initiative"
    return kind


def _validate_graph(repo: Repo, root: Path) -> Result[ValidateResult, FitsError]:
    """Validate the repository after reconciling link artifacts."""
    repaired = reconcile_link_artifacts(root)
    if isinstance(repaired, Err):
        return repaired
    return repo.validate(include_nested_subgraphs=True)


def prune_deleted_entity(
    root: Path,
    kind: str,
    name: str,
) -> Result[None, FitsError]:
    """Remove a deleted layout entity from the pyfits graph without full roadmap load.

    Args:
        root: Roadmap root directory.
        kind: Entity kind from :func:`bellman.layout.delete_entity`
            (e.g. ``initiative``, ``project``, ``goal``).
        name: Natural entity name (kebab-case).

    Returns:
        ``Ok(None)`` when pruning and libfits validation succeed.
        ``Err(FitsError)`` when libfits is unavailable, the repo is not
        initialized, or pruning fails.
    """
    if not libfits_available():
        return Err(
            FitsError(
                "libfits not available; set PYFITS_LIB_PATH or build ../fits",
                code="lib_not_found",
            )
        )
    if not (root / ".fits").is_dir():
        return Err(
            FitsError(
                "Roadmap not initialized; run bellman init",
                code="not_initialized",
            )
        )

    prepared = _prepare_registry_files(root)
    if isinstance(prepared, Err):
        return prepared

    try:
        node_names = _deleted_node_names(kind, name, root)
    except ValueError as exc:
        return Err(FitsError(str(exc), code="invalid_entity"))

    reconciled = reconcile_link_artifacts(root, drop_touching_nodes=node_names)
    if isinstance(reconciled, Err):
        return reconciled

    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    index = index_result.ok_value

    open_result = Repo.open(root)
    if isinstance(open_result, Err):
        return open_result
    repo = open_result.ok_value
    with repo:
        boot = _bootstrap_session(repo, root)
        if isinstance(boot, Err):
            return boot

        # Deepest paths first so nested work packages are removed before projects.
        for logical_name in sorted(
            node_names,
            key=lambda name: name.count("/"),
            reverse=True,
        ):
            guid = index.guid_for_name(logical_name)
            if guid is None:
                continue
            removed = ignore_nothing_to_remove(repo.remove(guid))
            if isinstance(removed, Err):
                return removed

        val = _validate_graph(repo, root)
        if isinstance(val, Err):
            return val
    return Ok(None)


_CREATE_ENTITY_KINDS = frozenset({"initiative", "project", "milestone", "goal"})


def _parse_created_entity(
    root: Path,
    kind: str,
    name: str,
) -> Result[Initiative | Project | Milestone | Goal, FitsError]:
    """Parse a single layout entity file for targeted graph sync."""
    try:
        if kind == "initiative":
            path = layout.initiative_path(root, name)
            initiative = parse_work_scope(path, is_project=False)
            assert isinstance(initiative, Initiative)
            return Ok(initiative)
        if kind == "project":
            path = layout.project_md_path(root, name)
            wp_path = layout.work_packages_path(root, name)
            project = parse_work_scope(
                path,
                is_project=True,
                work_packages_path=wp_path,
            )
            assert isinstance(project, Project)
            return Ok(project)
        if kind == "milestone":
            return Ok(parse_milestone(layout.milestone_path(root, name)))
        if kind == "goal":
            return Ok(parse_goal(layout.goal_path(root, name)))
        msg = f"unknown entity kind {kind!r}"
        raise ValueError(msg)
    except (ValueError, OSError) as exc:
        return Err(FitsError(str(exc), code="entity_load_failed"))


def _sync_scope_dependencies_layout(
    repo: Repo,
    root: Path,
    graph: Graph,
    scope: Initiative | Project,
) -> Result[None, FitsError]:
    """Ensure scope dependency links using filesystem entity resolution."""
    sid = _logical_scope_name(scope)
    try:
        for edge in scope.dependencies:
            pred = resolve_entity_ref_from_layout(root, edge.predecessor)
            lt = f"{link_naming.precedes_link_type(edge.relation, edge.hardness)}_scope"
            link_name = link_naming.wire_link_name(lt, pred, sid)
            link_res = _ensure_link(
                repo,
                root,
                graph,
                link_type=lt,
                from_logical=pred,
                to_logical=sid,
                link_name=link_name,
            )
            if isinstance(link_res, Err):
                return link_res
    except ValueError as exc:
        return Err(FitsError(str(exc), code="entity_load_failed"))
    return Ok(None)


def sync_renamed_entity(
    root: Path,
    kind: str,
    old_name: str,
    new_name: str,
) -> Result[None, FitsError]:
    """Rename layout entity instances in pyfits and refresh dependency links.

    Args:
        root: Roadmap root directory.
        kind: Entity kind from :func:`bellman.layout.rename_entity`.
        old_name: Previous natural entity name (kebab-case).
        new_name: New natural entity name (kebab-case).

    Returns:
        ``Ok(None)`` when renames and validation succeed.
        ``Err(FitsError)`` when libfits is unavailable, the repo is not
        initialized, or sync fails.
    """
    type_name = _ENTITY_KIND_TO_TYPE.get(kind)
    if type_name is None:
        return Err(FitsError(f"unknown entity kind {kind!r}", code="invalid_entity"))
    if not libfits_available():
        return Err(
            FitsError(
                "libfits not available; set PYFITS_LIB_PATH or build ../fits",
                code="lib_not_found",
            )
        )
    if not (root / ".fits").is_dir():
        return Err(
            FitsError(
                "Roadmap not initialized; run bellman init",
                code="not_initialized",
            )
        )

    prepared = _prepare_registry_files(root)
    if isinstance(prepared, Err):
        return prepared

    old_logical = entity_node_id(type_name, old_name)
    new_logical = entity_node_id(type_name, new_name)

    open_result = Repo.open(root)
    if isinstance(open_result, Err):
        return open_result
    repo = open_result.ok_value
    with repo:
        boot = _bootstrap_session(repo, root)
        if isinstance(boot, Err):
            return boot

        index_result = InstanceIndex.load(root)
        if isinstance(index_result, Err):
            return Err(_history_to_fits_error(index_result.err_value))
        index = index_result.ok_value

        if old_logical in index.live_node_names() and old_logical != new_logical:
            guid = index.guid_for_name(old_logical)
            if guid is not None:
                renamed = repo.rename_instance(
                    guid=guid,
                    new_name=InstanceName(new_name),
                )
                if isinstance(renamed, Err):
                    return renamed

        # Legacy flat / dash-qualified names
        for candidate in (old_name, f"{type_name}--{old_name}"):
            if candidate == old_logical:
                continue
            if candidate not in index.by_name:
                continue
            guid = index.guid_for_name(candidate)
            if guid is None:
                continue
            # Cannot rename into nested scope; remove and let resync recreate.
            removed = ignore_nothing_to_remove(repo.remove(guid))
            if isinstance(removed, Err):
                return removed

        sync_kind = _rename_graph_kind(kind)
        if kind in _CREATE_ENTITY_KINDS:
            resync = sync_created_entity(root, sync_kind, new_name)
            if isinstance(resync, Err):
                return resync

        val = _validate_graph(repo, root)
        if isinstance(val, Err):
            return val
    return Ok(None)


def sync_created_entity(
    root: Path,
    kind: str,
    name: str,
) -> Result[None, FitsError]:
    """Register a newly created layout entity in pyfits without full roadmap load.

    Args:
        root: Roadmap root directory.
        kind: Entity kind (``initiative``, ``project``, ``milestone``, ``goal``).
        name: Natural entity name (kebab-case).

    Returns:
        ``Ok(None)`` when the entity is registered and validation succeeds.
        ``Err(FitsError)`` when libfits is unavailable, the repo is not
        initialized, the entity file fails to parse, or sync fails.
    """
    if kind not in _CREATE_ENTITY_KINDS:
        return Err(FitsError(f"unknown entity kind {kind!r}", code="invalid_entity"))
    if not libfits_available():
        return Err(
            FitsError(
                "libfits not available; set PYFITS_LIB_PATH or build ../fits",
                code="lib_not_found",
            )
        )
    if not (root / ".fits").is_dir():
        return Err(
            FitsError(
                "Roadmap not initialized; run bellman init",
                code="not_initialized",
            )
        )

    prepared = _prepare_registry_files(root)
    if isinstance(prepared, Err):
        return prepared

    parsed = _parse_created_entity(root, kind, name)
    if isinstance(parsed, Err):
        return parsed
    entity = parsed.ok_value

    open_result = Repo.open(root)
    if isinstance(open_result, Err):
        return open_result
    repo = open_result.ok_value
    with repo:
        boot = _bootstrap_session(repo, root)
        if isinstance(boot, Err):
            return boot
        graph_result = _reload_graph(repo)
        if isinstance(graph_result, Err):
            return graph_result
        graph = graph_result.ok_value

        if kind == "initiative":
            initiative = entity
            assert isinstance(initiative, Initiative)
            ensured = _ensure_node(
                repo,
                root,
                graph,
                type_name="initiative",
                logical_name=_logical_scope_name(initiative),
                title=initiative.title,
            )
            if isinstance(ensured, Err):
                return ensured
            dep_sync = _sync_scope_dependencies_layout(repo, root, graph, initiative)
            if isinstance(dep_sync, Err):
                return dep_sync
        elif kind == "project":
            project = entity
            assert isinstance(project, Project)
            ensured = _ensure_node(
                repo,
                root,
                graph,
                type_name="project",
                logical_name=_logical_scope_name(project),
                title=project.title,
            )
            if isinstance(ensured, Err):
                return ensured
            wp_sync = _sync_project_wps(repo, root, graph, project)
            if isinstance(wp_sync, Err):
                return wp_sync
            dep_sync = _sync_scope_dependencies_layout(repo, root, graph, project)
            if isinstance(dep_sync, Err):
                return dep_sync
        elif kind == "milestone":
            milestone = entity
            assert isinstance(milestone, Milestone)
            ensured = _ensure_node(
                repo,
                root,
                graph,
                type_name="milestone",
                logical_name=_logical_milestone_name(milestone.name),
                title=milestone.title,
            )
            if isinstance(ensured, Err):
                return ensured
        else:
            goal = entity
            assert isinstance(goal, Goal)
            ensured = _ensure_node(
                repo,
                root,
                graph,
                type_name="goal",
                logical_name=_logical_goal_name(goal.name),
                title=goal.title,
            )
            if isinstance(ensured, Err):
                return ensured

        val = _validate_graph(repo, root)
        if isinstance(val, Err):
            return val
    return Ok(None)


def _with_ensure_context(error: FitsError, context: str) -> FitsError:
    """Rewrite a FitsError message with operation context, preserving code/status."""
    return FitsError(
        f"{context}: {error}",
        code=error.code,
        status=error.status,
    )


def _ensure_node(
    repo: Repo,
    root: Path,
    graph: Graph,
    *,
    type_name: str,
    logical_name: str,
    title: str,
) -> Result[CreatedObject, FitsError]:
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    index = index_result.ok_value

    if logical_name in index.live_node_names():
        guid = index.guid_for_name(logical_name)
        assert guid is not None
        return Ok(CreatedObject(guid=guid, name=logical_name))

    local_name = local_name_from_node_id(logical_name)
    if type_name in _WORK_SCOPE_TYPES:
        existing = index.guid_for_work_scope_name(local_name)
        if existing is not None:
            return Ok(CreatedObject(guid=existing, name=logical_name))

    parent_path = _container_logical_name(type_name, logical_name)
    container_guid: Id | None = None
    if parent_path is not None:
        container_guid = index.guid_for_name(parent_path)
        if container_guid is None:
            return Err(
                FitsError(
                    f"container not registered for {logical_name!r}: {parent_path!r}",
                    code="container_not_found",
                )
            )

    result = repo.new_node(
        ObjectTypeName(type_name),
        container_guid=container_guid,
        name=InstanceName(local_name),
        title=title,
    )
    # Reload index for duplicate recovery after create race
    index_result = InstanceIndex.load(root)
    guid = None
    if isinstance(index_result, Ok):
        guid = index_result.ok_value.guid_for_name(logical_name)
    ensured = ignore_duplicate_instance(
        result,
        logical_name=logical_name,
        guid=guid,
    )
    if isinstance(ensured, Err):
        return Err(
            _with_ensure_context(
                ensured.err_value,
                f"failed to ensure node {logical_name} (type={type_name})",
            )
        )
    return ensured


def _guid_tail(value: str) -> str:
    """Return the final GUID segment of a wire id path."""
    return value.rsplit("/", 1)[-1]


def _same_endpoint(left: Id, right: Id) -> bool:
    """Return True when two wire ids refer to the same instance."""
    return _guid_tail(left.value) == _guid_tail(right.value)


def _ensure_link(
    repo: Repo,
    root: Path,
    graph: Graph,
    *,
    link_type: str,
    from_logical: str,
    to_logical: str,
    link_name: InstanceName,
) -> Result[CreatedObject, FitsError]:
    index_result = InstanceIndex.load(root)
    if isinstance(index_result, Err):
        return Err(_history_to_fits_error(index_result.err_value))
    index = index_result.ok_value

    in_guid = index.guid_for_name(from_logical)
    out_guid = index.guid_for_name(to_logical)
    if in_guid is None:
        return Err(
            FitsError(
                f"link endpoint not registered: {from_logical!r}",
                code="endpoint_not_found",
            )
        )
    if out_guid is None:
        return Err(
            FitsError(
                f"link endpoint not registered: {to_logical!r}",
                code="endpoint_not_found",
            )
        )

    for edge in graph.edges:
        # libfits stores edges as out → in; we pass in=from_logical, out=to_logical.
        if edge.link_type != link_type:
            continue
        if _same_endpoint(edge.from_id, out_guid) and _same_endpoint(
            edge.to_id, in_guid
        ):
            edge_guid = edge.id
            if edge_guid is not None:
                return Ok(CreatedObject(guid=edge_guid, name=link_name.value))

    # Nested endpoints: omit explicit names so libfits autonumbers in nested scope.
    # Explicit names collide on idempotent sync and can corrupt the registry.
    nested = "/" in in_guid.value or "/" in out_guid.value
    result = repo.new_link(
        link_type,
        in_guid,
        out_guid,
        name=None if nested else link_name,
    )
    if isinstance(result, Ok) and result.ok_value.name in {
        "goal",
        "initiative",
        "project",
        "milestone",
    }:
        return Err(
            FitsError(
                f"unexpected link create result for {link_type!r}",
                code="link_create_failed",
            )
        )
    existing_guid = index.guid_for_name(link_name.value)
    ensured = ignore_duplicate_link(
        result,
        link_name=link_name.value,
        guid=existing_guid,
    )
    if isinstance(ensured, Err):
        return Err(
            _with_ensure_context(
                ensured.err_value,
                (
                    f"failed to ensure link {link_type} "
                    f"{from_logical!r} -> {to_logical!r}"
                ),
            )
        )
    return ensured


def _sync_project_wps(
    repo: Repo,
    root: Path,
    graph: Graph,
    project: Project,
) -> Result[None, FitsError]:
    flat = flatten_wps(project.work_packages, None, project.name)
    for wp, parent in flat:
        wid = _logical_wp_name(project.name, wp.slug)
        created = _ensure_node(
            repo,
            root,
            graph,
            type_name="work_package",
            logical_name=wid,
            title=wp.title,
        )
        if isinstance(created, Err):
            return created
        if parent is not None:
            parent_name = _logical_wp_name(project.name, parent.slug)
            plink = _ensure_link(
                repo,
                root,
                graph,
                link_type="parent_of",
                from_logical=parent_name,
                to_logical=wid,
                link_name=link_naming.wire_link_name("parent_of", parent_name, wid),
            )
            if isinstance(plink, Err):
                return plink
        for edge in wp.dependencies:
            pred = resolve_wp_ref(project.name, edge.predecessor)
            lt = link_naming.precedes_link_type(edge.relation, edge.hardness)
            link_name = link_naming.wire_link_name(lt, pred, wid)
            link_res = _ensure_link(
                repo,
                root,
                graph,
                link_type=lt,
                from_logical=pred,
                to_logical=wid,
                link_name=link_name,
            )
            if isinstance(link_res, Err):
                return link_res
    return Ok(None)


def _sync_scope_dependencies(
    repo: Repo,
    root: Path,
    graph: Graph,
    roadmap: Roadmap,
    scope: Initiative | Project,
) -> Result[None, FitsError]:
    sid = _logical_scope_name(scope)

    for edge in scope.dependencies:
        pred = resolve_scope_ref(roadmap, edge.predecessor)
        lt = f"{link_naming.precedes_link_type(edge.relation, edge.hardness)}_scope"
        link_name = link_naming.wire_link_name(lt, pred, sid)
        link_res = _ensure_link(
            repo,
            root,
            graph,
            link_type=lt,
            from_logical=pred,
            to_logical=sid,
            link_name=link_name,
        )
        if isinstance(link_res, Err):
            return link_res
    return Ok(None)


def init_pyfits_repo(root: Path) -> Result[None, FitsError]:
    """Initialize pyfits repository scaffolding at ``root``.

    Creates ``.fits/``, graph roots, and registers bellman types when missing.
    Idempotent when the repository is already initialized.

    Args:
        root: Roadmap root directory.

    Returns:
        ``Ok(None)`` when initialization succeeds or was already done.
        ``Err(FitsError)`` when libfits is unavailable or initialization fails.
    """
    if not libfits_available():
        return Err(
            FitsError(
                "libfits not available; set PYFITS_LIB_PATH or build ../fits",
                code="lib_not_found",
            )
        )
    prepared = _prepare_registry_files(root)
    if isinstance(prepared, Err):
        return prepared
    open_result = Repo.open(root)
    if isinstance(open_result, Err):
        return open_result
    repo = open_result.ok_value
    with repo:
        if not (root / ".fits").is_dir():
            init_res = repo.init()
            if isinstance(init_res, Err):
                return init_res
        boot = _bootstrap_session(repo, root)
        if isinstance(boot, Err):
            return boot
    return Ok(None)


def sync_roadmap(
    root: Path,
    *,
    prune: bool = False,
) -> Result[None, FitsError]:
    """Load roadmap and sync nodes/links into pyfits repository at ``root``.

    Requires an initialized pyfits repository (``bellman init``). Does not create
    ``.fits/``, ``nodes/``, or ``links/``.

    Args:
        root: Roadmap root directory.
        prune: When True, remove graph objects no longer present in markdown.

    Returns:
        ``Ok(None)`` when sync and libfits validation succeed.
        ``Err(FitsError)`` when libfits is unavailable, the repo is not
        initialized, roadmap load fails, or sync/validation fails.
    """
    if not libfits_available():
        return Err(
            FitsError(
                "libfits not available; set PYFITS_LIB_PATH or build ../fits",
                code="lib_not_found",
            )
        )
    if not (root / ".fits").is_dir():
        return Err(
            FitsError(
                "Roadmap not initialized; run bellman init",
                code="not_initialized",
            )
        )
    try:
        roadmap = load(root)
    except (ValueError, OSError) as exc:
        return Err(FitsError(str(exc), code="roadmap_load_failed"))

    # Schema migration and obsolete-link cleanup rewrite registry.json; run
    # before opening a long session.
    prepared = _prepare_registry_files(root)
    if isinstance(prepared, Err):
        return prepared

    open_result = Repo.open(root)
    if isinstance(open_result, Err):
        return open_result
    repo = open_result.ok_value
    with repo:
        boot = _bootstrap_session(repo, root)
        if isinstance(boot, Err):
            return boot
        graph_result = _reload_graph(repo)
        if isinstance(graph_result, Err):
            return graph_result
        graph = graph_result.ok_value
        desired = _desired_graph_node_names(roadmap)
        migrated = _migrate_legacy_node_ids(repo, root, desired)
        if isinstance(migrated, Err):
            return migrated
        graph_result = _reload_graph(repo)
        if isinstance(graph_result, Err):
            return graph_result
        graph = graph_result.ok_value

        for initiative in roadmap.initiatives:
            if not layout.archived_project_dir(root, initiative.name).is_dir():
                continue
            removed = _retype_parked_project_to_initiative(
                repo,
                root,
                graph,
                initiative.name,
            )
            if isinstance(removed, Err):
                return removed
            graph = removed.ok_value

        for initiative in roadmap.initiatives:
            res = _ensure_node(
                repo,
                root,
                graph,
                type_name="initiative",
                logical_name=_logical_scope_name(initiative),
                title=initiative.title,
            )
            if isinstance(res, Err):
                return res

        for archived in roadmap.archived_initiatives:
            if roadmap.project_by_name(archived.name) is not None:
                continue
            res = _ensure_node(
                repo,
                root,
                graph,
                type_name="initiative",
                logical_name=_logical_scope_name(archived),
                title=archived.title,
            )
            if isinstance(res, Err):
                return res

        for project in roadmap.projects:
            if layout.archived_initiative_path(root, project.name).exists():
                retyped = _retype_work_scope(
                    repo,
                    root,
                    graph,
                    project.name,
                    from_type="initiative",
                    to_type="project",
                )
                if isinstance(retyped, Err):
                    return retyped
                graph = retyped.ok_value
                reloaded = _reload_graph(repo)
                if isinstance(reloaded, Err):
                    return reloaded
                graph = reloaded.ok_value
            res = _ensure_node(
                repo,
                root,
                graph,
                type_name="project",
                logical_name=_logical_scope_name(project),
                title=project.title,
            )
            if isinstance(res, Err):
                return res
            wp_sync = _sync_project_wps(repo, root, graph, project)
            if isinstance(wp_sync, Err):
                return wp_sync

        for milestone in roadmap.milestones:
            res = _ensure_node(
                repo,
                root,
                graph,
                type_name="milestone",
                logical_name=_logical_milestone_name(milestone.name),
                title=milestone.title,
            )
            if isinstance(res, Err):
                return res

        for goal in roadmap.goals:
            res = _ensure_node(
                repo,
                root,
                graph,
                type_name="goal",
                logical_name=_logical_goal_name(goal.name),
                title=goal.title,
            )
            if isinstance(res, Err):
                return res

        for scope in roadmap.all_work_scopes():
            dep_sync = _sync_scope_dependencies(repo, root, graph, roadmap, scope)
            if isinstance(dep_sync, Err):
                return dep_sync

        if prune:
            pruned = _prune_stale_graph(
                repo,
                root,
                graph,
                desired,
                desired_links(roadmap),
            )
            if isinstance(pruned, Err):
                return pruned
            graph = pruned.ok_value

        val = _validate_graph(repo, root)
        if isinstance(val, Err):
            return val
    return Ok(None)
