# Bellman

Markdown-first roadmap planning built on [pyfits](https://github.com/davidtgillard/pyfits/).

## Install

Download a platform binary from the rolling [`dev` release](https://github.com/davidtgillard/bellman/releases/tag/dev). Asset names include the version from `pyproject.toml`:

| Platform | Asset |
|----------|--------|
| Linux x86_64 | `bellman-{version}-linux-x86_64` |
| Windows x86_64 | `bellman-{version}-windows-x86_64.exe` |
| macOS arm64 | `bellman-{version}-macos-arm64` |

**Linux x86_64:**

```bash
curl -fsSL -o bellman \
  "https://github.com/davidtgillard/bellman/releases/download/dev/bellman-0.1.0-linux-x86_64"
chmod +x bellman
sudo mv bellman /usr/local/bin/   # or any directory on your PATH
```

**macOS arm64:**

```bash
curl -fsSL -o bellman \
  "https://github.com/davidtgillard/bellman/releases/download/dev/bellman-0.1.0-macos-arm64"
chmod +x bellman
sudo mv bellman /usr/local/bin/
```

**Windows x86_64** (PowerShell):

```powershell
Invoke-WebRequest -Uri "https://github.com/davidtgillard/bellman/releases/download/dev/bellman-0.1.0-windows-x86_64.exe" -OutFile bellman.exe
# Move bellman.exe onto your PATH
```

### Self-update

```bash
bellman update --check   # check only; exit 1 if a newer build is available
bellman update           # download and replace the binary (PyInstaller builds only)
```

`bellman update` selects the asset for the host platform automatically. Bellman also checks for updates in the background (at most once per 24 hours by default) when you run any other subcommand.

After upgrading to a release that uses libfits GUID wire ids (protocol v2), re-initialize each roadmap's pyfits tree: remove `.fits/`, `nodes/`, and `links/`, then run `bellman init .` and `bellman sync .`. Markdown remains the source of truth.

### Configuration

Settings live in `$HOME/.bellman/bellman-settings.toml`:

```toml
[update]
check_interval_hours = 24
timeout_seconds = 10
repository = "davidtgillard/bellman"
release_tag = "dev"
```

State (last check time, installed asset id) is stored in `.bellman/bellman-state.json` next to the `bellman` binary when possible, otherwise in `$HOME/.bellman/bellman-state.json`.

## About

Bellman defines a roadmap as initiatives, projects, work packages, milestones, and goals. Human-edited markdown is the source of truth; a pyfits graph is derived for validation and future tooling.

## Roadmap layout

```
initiatives/          # one .md per initiative (default)
projects/             # one folder per project
  {name}/
    {name}.md
    work-packages.yaml
milestones/
goals/
```

All natural names use **lowercase-kebab-case** (e.g. `billing-redesign`).

Run `bellman init` once at the roadmap root before other commands. It creates the markdown directories and the pyfits repository (`.fits/`, `nodes/`, `links/`). Graph sync commands do not create that scaffolding.

When you run a command from a subdirectory, bellman walks up to the nearest ancestor containing `.fits/`, stopping at the git root (the directory containing `.git`) so it does not search outside the work tree. `bellman init` always targets the path you give (or cwd) and does not walk upward.

## Commands

```bash
bellman init .
bellman create initiative explore-ml-ranking
bellman create project billing-redesign
bellman create milestone ga-release
bellman create goal reduce-churn
bellman promote billing-redesign   # after creating as initiative
bellman promote initiatives/billing-redesign
bellman demote billing-redesign    # park the project folder; restore the initiative
bellman demote projects/billing-redesign/billing-redesign.md
bellman validate .
bellman validate --no-registry .
bellman sync .
bellman version
bellman update --check
bellman delete my-goal
bellman delete goals/my-goal.md
bellman rename old-name new-name
bellman rename projects/old-proj new-proj
bellman rename goal system-mci renamed-goal   # when names collide across types
bellman plugin list
bellman plugin my-plugin
bellman report wbs tree --project billing-redesign   # PERT tree to stdout
bellman report wbs tree --project projects/billing-redesign
bellman report dependencies                          # all precedence edges
bellman report deps beta                             # predecessors/successors of beta
bellman report deps initiatives/beta
```

`validate` checks markdown in git and, by default, reports differences between those files and the pyfits registry (for example a goal added by hand without `bellman create`). Use `--no-registry` to skip registry comparison. `sync` runs the same markdown validation first, then updates the registry from git and prunes stale graph objects.

`create`, `delete`, `rename`, `promote`, and `demote` update the pyfits graph and `.fits/registry.json` directly when libfits is installed. Run `bellman init` first; `sync` will not bootstrap pyfits artifacts. If graph sync fails after a markdown change, the command exits with code 1; the markdown file is still written. When libfits is not available, those commands only change markdown and print a note. `delete` also prunes the removed entity from the graph; use `bellman sync` to reconcile other manual edits.

`demote` parks the whole project directory as `projects/{name}.archived/` (work packages and extra files included) and restores `initiatives/{name}.md`. A later `promote` of the same name restores that folder instead of creating an empty one. Promote and demote keep the same pyfits GUID and flip `instances[].type`; git history of `.fits/registry.json` is the type change.

`rename` moves the entity on disk (initiative, project, milestone, or goal), rewrites dependency references that name the old entity, and renames the matching pyfits instance (GUID preserved). Entity-targeting commands (`promote`, `demote`, `delete`, `rename`, `report deps`, `report wbs --project`) accept a bare name when it is unambiguous across types, a layout FQN such as `projects/foo` or `initiatives/foo`, a folder path (`projects/foo`), or the main markdown path (`projects/foo/foo.md`, `goals/foo.md`). Graph FQNs (`project/foo`, `goal/foo`) work as well. Use a type subcommand when initiative and goal (for example) share a name: `bellman rename goal foo bar`.

## Precedence dependencies

Declare predecessors **only on the successor** (the entity that depends on them). There is no `after:` / `before:` keyword.

**Initiatives and projects** — under `## Dependencies`:

```markdown
## Dependencies

- other-initiative [FS, Mandatory]
```

**Work packages** — in `work-packages.yaml` on the dependent package:

```yaml
dependencies:
  - predecessor: wp-setup
    relation: FS
    hardness: Mandatory
  # or: - wp-setup [FS, Mandatory]
```

Relation is one of `FF`, `FS`, `SF`, `SS`. Hardness is `Mandatory`, `Discretionary`, or `Optional`.

Use `bellman report dependencies` (alias `deps`) to list all edges, or pass an entity name / `project/slug` to see what it depends on and what depends on it.
## Plugins

Repo-local Python plugins live under `plugin/{name}/` in the roadmap root. Each plugin exports a `PLUGIN` object (`BellmanPlugin` from `bellman.plugin`). Plugins require a **Python install** of bellman (`uv run bellman` or `pip install`); the standalone PyInstaller binary cannot load arbitrary repo Python.

```bash
bellman plugin --path /path/to/roadmap list
bellman plugin --path /path/to/roadmap my-plugin
bellman plugin --path /path/to/roadmap my-plugin --help    # per-plugin argparse help
```

When the shell cwd is inside the roadmap tree, omit `--path`; bellman discovers the root automatically.

Example `plugin/report-deps/__init__.py` (prefer built-in `bellman report dependencies` for this use case):

```python
from bellman.plugin import (
    PluginArgumentSpecs,
    PluginArguments,
    BellmanContext,
    BellmanPlugin,
    TextIO,
)


def run(ctx: BellmanContext, args: PluginArguments, io: TextIO) -> int:
    for scope in ctx.roadmap().all_work_scopes():
        for edge in scope.dependencies:
            io.writeline(f"{edge.predecessor} -> {edge.successor}")
    return 0


PLUGIN = BellmanPlugin(
    name="report-deps",
    summary="Print scope precedence edges",
    args=PluginArgumentSpecs.empty(),
    run=run,
)
```

`BellmanContext` provides lazy access to the markdown roadmap (`roadmap()`), pyfits graph (`graph()`), registry audit history (`history()` — renames, tombstones, live instances from `.fits/registry.json`), and `sync_roadmap()`. Use `TextIO` for stdout/stderr so output is testable.

## Link naming

Links are identified as `{link_type}:{from}->{to}`. Precedence edges use registered types such as `precedes_FS_Mandatory`.

## libfits

Graph sync requires the host-platform libfits shared library (`libfits.so`, `libfits.dll`, or `libfits.dylib`; same as pyfits). The PyInstaller binary bundles libfits when built with `LIBFITS_PATH` set. For source installs, run `python ../pyfits.git/scripts/fetch_libfits.py`, build the sibling [fits](https://github.com/davidtgillard/fits) checkout, or set `PYFITS_LIB_PATH`.

## Development

```bash
uv sync --all-groups
uv run pytest
uv run ruff check src tests
```

### Build a standalone binary

```bash
uv sync --all-groups
python ../pyfits.git/scripts/fetch_libfits.py   # or export LIBFITS_PATH=...
uv run python packaging/write_build_version.py
uv run pyinstaller packaging/bellman.spec --noconfirm
uv run python packaging/package_release.py --platform linux-x86_64  # or windows-x86_64 / macos-arm64
./dist/bellman version
```

Integration tests are marked `@pytest.mark.integration` and skip when libfits is unavailable.
