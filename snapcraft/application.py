# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2023-2024 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Main Snapcraft Application."""

from __future__ import annotations

import logging
import os
import pathlib
import signal
import sys
from contextlib import suppress
from typing import Any

import craft_application.commands as craft_app_commands
import craft_cli
from craft_application import Application, AppMetadata, util
from craft_application.errors import ProjectFileMissingError
from craft_cli import emit
from craft_parts.plugins.plugins import PluginType
from overrides import override

import snapcraft
import snapcraft_legacy
from snapcraft import cli, commands, errors, models, services
from snapcraft.commands import unimplemented
from snapcraft.extensions import apply_extensions
from snapcraft.models.project import SnapcraftBuildPlanner, apply_root_packages
from snapcraft.utils import get_host_architecture
from snapcraft_legacy.cli import legacy

from .legacy_cli import _LIB_NAMES, _ORIGINAL_LIB_NAME_LOG_LEVEL
from .parts import plugins
from .parts.yaml_utils import extract_parse_info

APP_METADATA = AppMetadata(
    name="snapcraft",
    summary="Package, distribute, and update snaps for Linux and IoT",
    ProjectClass=models.Project,
    BuildPlannerClass=SnapcraftBuildPlanner,
    source_ignore_patterns=["*.snap"],
    project_variables=["version", "grade"],
    mandatory_adoptable_fields=["version", "summary", "description"],
)


MAPPED_ENV_VARS = {
    ev: "SNAP" + ev for ev in ("CRAFT_BUILD_FOR", "CRAFT_BUILD_ENVIRONMENT")
}


class Snapcraft(Application):
    """Snapcraft application definition."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Whether we know that we should use the core24-based codepath.
        self._known_core24 = False
        self._parse_info: dict[str, list[str]] = {}

        # Locate the project file. It's used in early execution to determine
        # compatibility with previous versions of the snapcraft codebase, and in
        # the package service to copy the project file into the snap payload if
        # manifest generation is enabled.
        try:
            self._snapcraft_yaml_path: pathlib.Path | None = self._resolve_project_path(
                None
            )
        except FileNotFoundError:
            self._snapcraft_yaml_path = None

        for craft_var, snapcraft_var in MAPPED_ENV_VARS.items():
            if env_val := os.getenv(snapcraft_var):
                os.environ[craft_var] = env_val

    def _get_app_plugins(self) -> dict[str, PluginType]:
        return plugins.get_plugins(core22=False)

    @override
    def _configure_services(self, provider_name: str | None) -> None:
        self.services.set_kwargs(
            "package",
            build_plan=self._build_plan,
            snapcraft_yaml_path=self._snapcraft_yaml_path,
            parse_info=self._parse_info,
        )

        super()._configure_services(provider_name)

    @property
    def command_groups(self):
        """Short-circuit the standard command groups for now."""
        # TODO: Remove this once we've got lifecycle commands and version migrated.
        return self._command_groups

    @override
    def _resolve_project_path(self, project_dir: pathlib.Path | None) -> pathlib.Path:
        """Overridden to handle the two possible locations for snapcraft.yaml."""
        if project_dir is None:
            project_dir = pathlib.Path.cwd()

        with suppress(FileNotFoundError):
            return super()._resolve_project_path(project_dir / "snap")

        with suppress(FileNotFoundError):
            return super()._resolve_project_path(project_dir)

        with suppress(FileNotFoundError):
            return super()._resolve_project_path(project_dir / "build-aux" / "snap")

        raise ProjectFileMissingError(
            f"Project file '{self.app.name+'.yaml'}' not found in '{project_dir}'",
            details="Please ensure that the project file is present in the project directory.",
            resolution=(
                "For more information, see https://snapcraft.io/docs/creating-snapcraft-yaml"
            ),
            retcode=66,  # EX_NOINPUT from sysexits.h
        )

    @property
    def app_config(self) -> dict[str, Any]:
        """Overridden to add "core" knowledge to the config."""
        config = super().app_config
        config["core24"] = self._known_core24
        return config

    @override
    def _extra_yaml_transform(
        self, yaml_data: dict[str, Any], *, build_on: str, build_for: str | None
    ) -> dict[str, Any]:
        arch = build_on
        target_arch = build_for if build_for else get_host_architecture()
        new_yaml_data = apply_extensions(yaml_data, arch=arch, target_arch=target_arch)
        self._parse_info = extract_parse_info(new_yaml_data)
        return apply_root_packages(new_yaml_data)

    @override
    def _get_dispatcher(self) -> craft_cli.Dispatcher:
        """Configure this application. Should be called by the run method.

        Side-effect: This method may exit the process.

        :returns: A ready-to-run Dispatcher object
        """
        # Handle "multiplexing" of Snapcraft "codebases" depending on the
        # project's base (if any). Here, we handle the case where there *is*
        # a project and it's core24, which means it should definitely fall into
        # the craft-application-based flow.
        if self._snapcraft_yaml_path:
            with self._snapcraft_yaml_path.open() as file:
                yaml_data = util.safe_yaml_load(file)
            base = yaml_data.get("base")
            build_base = yaml_data.get("build-base")
            if "core24" in (base, build_base) or build_base == "devel":
                # We know for sure that we're handling a core24 project
                self._known_core24 = True
            elif any(arg in ("version", "--version", "-V") for arg in sys.argv):
                pass
            else:
                raise errors.ClassicFallback()

        dispatcher = craft_cli.Dispatcher(
            self.app.name,
            self.command_groups,
            summary=str(self.app.summary),
            extra_global_args=self._global_arguments,
            # TODO: craft-application should allow setting the default command without
            # overriding `_get_dispatcher()`
            default_command=craft_app_commands.lifecycle.PackCommand,
        )

        try:
            craft_cli.emit.trace("pre-parsing arguments...")
            # Workaround for the fact that craft_cli requires a command.
            # https://github.com/canonical/craft-cli/issues/141
            if any(arg in ("--version", "-V") for arg in sys.argv) and (
                "version" not in sys.argv
            ):
                global_args = dispatcher.pre_parse_args(["version", *sys.argv[1:]])
            else:
                global_args = dispatcher.pre_parse_args(sys.argv[1:])
        except craft_cli.ProvideHelpException as err:
            print(err, file=sys.stderr)  # to stderr, as argparse normally does
            craft_cli.emit.ended_ok()
            sys.exit(0)
        except craft_cli.ArgumentParsingError as err:
            print(err, file=sys.stderr)  # to stderr, as argparse normally does
            craft_cli.emit.ended_ok()
            sys.exit(64)  # Command line usage error from sysexits.h
        except KeyboardInterrupt as err:
            self._emit_error(craft_cli.CraftError("Interrupted."), cause=err)
            sys.exit(128 + signal.SIGINT)
        # pylint: disable-next=broad-exception-caught
        except Exception as err:  # noqa: BLE001
            self._emit_error(
                craft_cli.CraftError(
                    f"Internal error while loading {self.app.name}: {err!r}"
                )
            )
            if os.getenv("CRAFT_DEBUG") == "1":
                raise
            sys.exit(70)  # EX_SOFTWARE from sysexits.h

        craft_cli.emit.trace("Preparing application...")
        self.configure(global_args)

        return dispatcher


def create_app() -> Snapcraft:
    """Create a Snapcraft application with the proper commands."""
    snapcraft_services = services.SnapcraftServiceFactory(app=APP_METADATA)

    app = Snapcraft(
        app=APP_METADATA,
        services=snapcraft_services,
        extra_loggers={"snapcraft.remote"},
    )

    app.add_command_group(
        "Lifecycle",
        [
            craft_app_commands.lifecycle.CleanCommand,
            craft_app_commands.lifecycle.PullCommand,
            craft_app_commands.lifecycle.BuildCommand,
            craft_app_commands.lifecycle.StageCommand,
            craft_app_commands.lifecycle.PrimeCommand,
            craft_app_commands.lifecycle.PackCommand,
            commands.SnapCommand,  # Hidden (legacy compatibility)
            unimplemented.RemoteBuild,
            unimplemented.Plugins,
            unimplemented.ListPlugins,
            unimplemented.Try,
        ],
    )
    app.add_command_group(
        "Extensions",
        [
            commands.ListExtensions,
            commands.ExpandExtensions,
        ],
    )
    app.add_command_group(
        "Store Account",
        [
            unimplemented.Login,
            unimplemented.ExportLogin,
            unimplemented.Logout,
            unimplemented.Whoami,
        ],
    )
    app.add_command_group(
        "Store Snap Names",
        [
            unimplemented.Register,
            unimplemented.Names,
            unimplemented.ListRegistered,
            unimplemented.List,
            unimplemented.Metrics,
            unimplemented.UploadMetadata,
        ],
    )
    app.add_command_group(
        "Store Snap Release Management",
        [
            unimplemented.Release,
            unimplemented.Close,
            unimplemented.Status,
            unimplemented.Upload,
            unimplemented.Push,
            unimplemented.Promote,
            unimplemented.ListRevisions,
            unimplemented.Revisions,
        ],
    )
    app.add_command_group(
        "Store Snap Tracks",
        [
            unimplemented.ListTracks,
            unimplemented.Tracks,
            unimplemented.SetDefaultTrack,
        ],
    )
    app.add_command_group(
        "Store Key Management",
        [
            unimplemented.CreateKey,
            unimplemented.RegisterKey,
            unimplemented.SignBuild,
            unimplemented.ListKeys,
        ],
    )
    app.add_command_group(
        "Store Validation Sets",
        [
            unimplemented.EditValidationSets,
            unimplemented.ListValidationSets,
            unimplemented.Validate,
            unimplemented.Gated,
        ],
    )
    app.add_command_group(
        "Other",
        list(craft_app_commands.get_other_command_group().commands)
        + [
            unimplemented.Lint,
            unimplemented.Init,
        ],
    )

    return app


def main() -> int:
    """Run craft-application based snapcraft with classic fallback."""
    if os.getenv("SNAPCRAFT_BUILD_ENVIRONMENT") == "managed-host":
        snapcraft.ProjectOptions = snapcraft_legacy.ProjectOptions  # type: ignore
        legacy.legacy_run()
        return 0  # never called in normal operation

    # set lib loggers to debug level so that all messages are sent to Emitter
    for lib_name in _LIB_NAMES:
        logger = logging.getLogger(lib_name)
        _ORIGINAL_LIB_NAME_LOG_LEVEL[lib_name] = logger.level
        logger.setLevel(logging.DEBUG)

    app = create_app()

    try:
        return app.run()
    except errors.ClassicFallback:
        emit.debug("Falling back from craft-application to snapcraft.")
        return cli.run()
