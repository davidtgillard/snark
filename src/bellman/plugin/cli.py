"""CLI entry for repo-local plugins (single Typer command on main app)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from bellman import layout
from bellman.errors import BellmanLayoutError
from bellman.plugin.args import build_parser
from bellman.plugin.arguments import PluginArguments
from bellman.plugin.context import BellmanContext
from bellman.plugin.discover import discover_plugins
from bellman.plugin.loader import PluginLoadError, load_plugin_or_raise
from bellman.plugin.textio import TextIO


def _root(path: Path | None) -> Path:
    try:
        return layout.discover_roadmap_root(path)
    except BellmanLayoutError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(code=1) from exc


def _plugin_list(root: Path, io: TextIO) -> None:
    specs = discover_plugins(root)
    if not specs:
        plugin_dir = root / "plugin"
        if not plugin_dir.is_dir():
            io.writeline(f"No plugin/ directory at {root}")
        else:
            io.writeline("No plugins found under plugin/.")
        return
    for spec in specs:
        try:
            plugin = load_plugin_or_raise(root, spec.name)
        except PluginLoadError as exc:
            io.writeline_error(f"{spec.name}\t(load error: {exc.format()})")
            continue
        io.writeline(f"{plugin.name}\t{plugin.summary}")


def _dispatch_plugin(
    root: Path,
    name: str,
    argv: list[str],
    io: TextIO,
) -> int:
    plugin_dir = root / "plugin"
    if not plugin_dir.is_dir():
        io.writeline_error(f"No plugin/ directory at {root}")
        return 1

    if argv == ["--help"] or argv == ["-h"]:
        _plugin_help(root, name)
        return 0

    try:
        plugin = load_plugin_or_raise(root, name)
    except PluginLoadError as exc:
        io.writeline_error(exc.format())
        return 1

    parser = build_parser(plugin.name, plugin.summary, plugin.args)
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    bellman_ctx = BellmanContext(root=root)
    try:
        return plugin.run(bellman_ctx, PluginArguments(ns), io)
    finally:
        bellman_ctx.close()


def _plugin_help(root: Path, name: str) -> None:
    try:
        plugin = load_plugin_or_raise(root, name)
    except PluginLoadError as exc:
        typer.echo(exc.format(), err=True)
        raise typer.Exit(code=1) from exc
    parser = build_parser(plugin.name, plugin.summary, plugin.args)
    parser.print_help(file=sys.stdout)


def register_plugin_command(app: typer.Typer) -> None:
    """Register ``bellman plugin`` on the main application."""

    @app.command(
        "plugin",
        context_settings={
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        },
    )
    def plugin_command(
        ctx: typer.Context,
        path: Annotated[
            Path | None,
            typer.Option("--path", help="Roadmap root"),
        ] = None,
    ) -> None:
        """Run repo-local Python plugins from plugin/ (``list`` or ``<name>``)."""
        args = list(ctx.args)
        if not args:
            typer.echo(
                "Usage: bellman plugin [--path ROOT] list\n"
                "       bellman plugin [--path ROOT] <name> [plugin args...]",
                err=True,
            )
            raise typer.Exit(code=1)

        root = _root(path)
        io = TextIO(
            output_stream=sys.stdout,
            error_stream=sys.stderr,
        )

        if args[0] == "list":
            _plugin_list(root, io)
            return

        name = args[0]
        code = _dispatch_plugin(root, name, args[1:], io)
        raise typer.Exit(code=code)


plugin_app = None  # legacy; registration via register_plugin_command
