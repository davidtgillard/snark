"""Register bellman node and link types in a pyfits repository."""

from __future__ import annotations

from pyfits import InstanceName, ObjectTypeName, Repo
from pyfits.errors import FitsError
from pyfits.result import Err, Ok, Result

from bellman.graph.fits_errors import ignore_if_already_exists, is_already_exists
from bellman.model import Hardness, RelationType

KIND_TYPE = "kind"
"""Node type for type-root containers (``goal``, ``project``, …)."""

WORK_SCOPE_KIND_ROOT = "work_scope"
"""Kind-root instance that hosts both initiative and project nodes."""

KIND_ROOT_NAMES = ("goal", "initiative", "project", "milestone", WORK_SCOPE_KIND_ROOT)
"""Local names of kind-root instances.

``goal`` and ``milestone`` host those entity types. ``work_scope`` hosts both
``initiative`` and ``project`` so scope-precedence links can connect them as
nested siblings (libfits nested links require a shared parent). ``initiative``
and ``project`` kind-roots remain so kind names stay stable.
"""


def precedes_link_types() -> list[str]:
    """All registered precedence link type names."""
    types: list[str] = []
    for rel in RelationType:
        for hard in Hardness:
            types.append(f"precedes_{rel.value}_{hardness_suffix(hard)}")
    return types


def precedes_scope_link_types() -> frozenset[str]:
    """Return nested scope-precedence link type names (``precedes_*_scope``).

    Returns:
        Frozen set of ``precedes_{relation}_{hardness}_scope`` type names.
    """
    return frozenset(f"{lt}_scope" for lt in precedes_link_types())


def hardness_suffix(hard: Hardness) -> str:
    """Wire-safe hardness label for link type names."""
    return hard.value


def bellman_node_types() -> frozenset[str]:
    """Node type names managed by bellman roadmap sync."""
    return frozenset({"initiative", "project", "work_package", "milestone", "goal"})


def bellman_link_types() -> frozenset[str]:
    """Link type names managed by bellman roadmap sync."""
    types: set[str] = {"parent_of"}
    for lt in precedes_link_types():
        types.add(lt)
        types.add(f"{lt}_scope")
    return frozenset(types)


def bootstrap_registry(repo: Repo) -> Result[None, FitsError]:
    """Register bellman types if not already present.

    Entity types nest under ``kind`` roots; work packages nest under ``project``.
    Link types whose endpoints are nested node types register as nested link types.
    """
    steps: list[Result[None, FitsError]] = [
        repo.register_node_type(KIND_TYPE, create_folder=True),
        repo.register_node_type("work_scope", abstract=True),
        repo.register_node_type(
            "initiative",
            extends="work_scope",
            container_node=KIND_TYPE,
        ),
        repo.register_node_type(
            "project",
            extends="work_scope",
            container_node=KIND_TYPE,
            create_folder=True,
        ),
        repo.register_node_type("work_package", container_node="project"),
        repo.register_node_type("milestone", container_node=KIND_TYPE),
        repo.register_node_type("goal", container_node=KIND_TYPE),
        # Nested endpoint pairs → nested link types (same-parent creates only).
        repo.register_link_type("supports", "project", "goal"),
        repo.register_link_type("supports_wp", "work_package", "goal"),
        repo.register_link_type("targets", "project", "milestone"),
        repo.register_link_type("targets_wp", "work_package", "milestone"),
        repo.register_link_type("parent_of", "work_package", "work_package"),
    ]
    for rel in RelationType:
        for hard in Hardness:
            lt = f"precedes_{rel.value}_{hardness_suffix(hard)}"
            steps.append(repo.register_link_type(lt, "work_package", "work_package"))
            # Nested under the shared work_scope kind-root. Registering project→project
            # covers initiative/project mixes: nested create does not re-check types,
            # and both concrete types are siblings under that parent.
            scope_lt = f"{lt}_scope"
            steps.append(repo.register_link_type(scope_lt, "project", "project"))

    for step in steps:
        normalized = ignore_if_already_exists(step)
        if isinstance(normalized, Err):
            return normalized
    return Ok(None)


def ensure_kind_roots(repo: Repo) -> Result[None, FitsError]:
    """Create kind-root instances when missing.

    Args:
        repo: Open pyfits repository session.

    Returns:
        ``Ok(None)`` when all kind roots exist or were created.
        ``Err(FitsError)`` when creation fails for a reason other than duplicate.
    """
    for name in KIND_ROOT_NAMES:
        created = repo.new_node(
            ObjectTypeName(KIND_TYPE),
            name=InstanceName(name),
            title=name,
        )
        if isinstance(created, Err) and is_already_exists(created.err_value):
            continue
        if isinstance(created, Err):
            return created
    return Ok(None)
