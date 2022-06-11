""" rTorrent Watch Jobs.

    Copyright (c) 2012 The PyroScope Project <pyroscope.project@gmail.com>
"""
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

# TODO: Re-tie metafiles when they're moved in the tree

import asyncio
import logging
import os
import time

from pathlib import Path

from pyrosimple import config as configuration
from pyrosimple import error
from pyrosimple.scripts.base import ScriptBase, ScriptBaseWithConfig
from pyrosimple.torrent import formatting
from pyrosimple.util import logutil, metafile, pymagic, rpc, traits
from pyrosimple.util.parts import Bunch


try:
    import pyinotify
except ImportError:
    pyinotify = Bunch(WatchManager=None, ProcessEvent=object)


class MetafileHandler:
    """Handler for loading metafiles into rTorrent."""

    def __init__(self, job, pathname):
        """Create a metafile handler."""
        self.job = job
        self.metadata = None
        self.ns = Bunch(
            pathname=os.path.abspath(pathname),
            info_hash=None,
            tracker_alias=None,
        )

    def parse(self) -> bool:
        """Parse metafile and check pre-conditions."""
        try:
            if not os.path.getsize(self.ns.pathname):
                # Ignore 0-byte dummy files (Firefox creates these while downloading)
                self.job.LOG.warning("Ignoring 0-byte metafile '%s'", self.ns.pathname)
                return False
            self.metadata = metafile.checked_open(self.ns.pathname)
        except OSError as exc:
            self.job.LOG.error(
                "Can't read metafile '%s' (%s)",
                self.ns.pathname,
                str(exc).replace(f": '{self.ns.pathname}'", ""),
            )
            return False
        except ValueError as exc:
            self.job.LOG.error("Invalid metafile '%s': %s", self.ns.pathname, exc)
            return False

        self.ns.info_hash = metafile.info_hash(self.metadata)
        self.ns.info_name = self.metadata["info"]["name"]
        self.job.LOG.info(
            "Loaded '%s' from metafile '%s'", self.ns.info_name, self.ns.pathname
        )

        # Check whether item is already loaded
        try:
            name = self.job.proxy.d.name(self.ns.info_hash)
        except rpc.HashNotFound:
            pass
        except rpc.XmlRpcError as exc:
            if exc.faultString != "Could not find info-hash.":
                self.job.LOG.error("While checking for #%s: %s", self.ns.info_hash, exc)
                return False
        else:
            self.job.LOG.warn(
                "Item #%s '%s' already added to client", self.ns.info_hash, name
            )
            if self.job.config.remove_already_added:
                Path(self.ns.pathname).unlink()
            return False

        return True

    def addinfo(self):
        """Add known facts to templating namespace."""
        # Basic values
        self.ns.watch_path = self.job.config.path
        self.ns.relpath = None
        for watch in self.job.config.path:
            path = Path(self.ns.pathname)
            try:
                self.ns.relpath = path.relative_to(watch)
                break
            except ValueError:
                pass

        # Build indicator flags for target state from filename
        flags = self.ns.pathname.split(os.sep)
        flags.extend(flags[-1].split("."))
        self.ns.flags = {i for i in flags if i}

        # Metafile stuff
        announce = self.metadata.get("announce", None)
        if announce:
            self.ns.tracker_alias = configuration.map_announce2alias(announce)

        main_file = self.ns.info_name
        if "files" in self.metadata["info"]:
            main_file = list(
                sorted(
                    (i["length"], i["path"][-1]) for i in self.metadata["info"]["files"]
                )
            )[-1][1]
        self.ns.filetype = os.path.splitext(main_file)[1]

        # Add name traits
        kind, info = traits.name_trait(self.ns.info_name, add_info=True)
        self.ns.traits = Bunch(info)
        self.ns.traits.kind = kind
        self.ns.label = "/".join(
            traits.detect_traits(
                name=self.ns.info_name,
                alias=self.ns.tracker_alias,
                filetype=self.ns.filetype,
            )
        ).strip("/")

        # Finally, expand commands from templates
        self.ns.commands = []
        for key, cmd in sorted(self.job.custom_cmds.items()):
            try:
                self.ns.commands.append(formatting.format_item(cmd, self.ns))
            except error.LoggableError as exc:
                self.job.LOG.error(f"While expanding '{key}' custom command: {exc}")

    def load(self):
        """Load metafile into client."""
        if not self.ns.info_hash and not self.parse():
            return

        self.addinfo()

        # TODO: dry_run
        try:
            # TODO: Scrub metafile if requested

            # Determine target state
            start_it = self.job.config.load_mode.lower() in ("start", "started")
            queue_it = self.job.config.queued

            if "start" in self.ns.flags:
                start_it = True
            elif "load" in self.ns.flags:
                start_it = False

            if "queue" in self.ns.flags:
                queue_it = True

            # Load metafile into client
            load_cmd = self.job.proxy.load.verbose
            if queue_it:
                if not start_it:
                    self.ns.commands.append("d.priority.set=0")
            elif start_it:
                load_cmd = self.job.proxy.load.start_verbose

            self.job.LOG.debug(
                "Templating values are:\n    %s"
                % "\n    ".join(
                    "{}={}".format(key, repr(val))
                    for key, val in sorted(self.ns.items())
                )
            )

            load_cmd(rpc.NOHASH, self.ns.pathname, *tuple(self.ns.commands))
            time.sleep(0.05)  # let things settle

            # Announce new item
            if not self.job.config.quiet:
                try:
                    name = self.job.proxy.d.name(self.ns.info_hash)
                except rpc.HashNotFound:
                    name = "NOHASH"
                msg = "{}: Loaded '{}' from '{}/'{}{}".format(
                    self.job.__class__.__name__,
                    name,
                    os.path.dirname(self.ns.pathname).rstrip(os.sep),
                    " [queued]" if queue_it else "",
                    (" [startable]" if queue_it else " [started]")
                    if start_it
                    else " [normal]",
                )
                self.job.proxy.log(rpc.NOHASH, msg)

            # TODO: Evaluate fields and set client values
            # TODO: Add metadata to tied file if requested

            # TODO: Execute commands AFTER adding the item, with full templating
            # Example: Labeling - add items to a persistent view, i.e. "postcmd = view.set_visible={{label}}"
            #   could also be done automatically from the path, see above under "flags" (autolabel = True)
            #   and add traits to the flags, too, in that case

        except rpc.ERRORS as exc:
            self.job.LOG.error("While loading #%s: %s", self.ns.info_hash, exc)

    def handle(self):
        """Handle metafile."""
        if self.parse():
            self.load()


class RemoteWatch:
    """rTorrent remote torrent file watch."""

    def __init__(self, config=None):
        """Set up remote watcher."""
        self.config = config or {}
        self.LOG = pymagic.get_class_logger(self)
        self.LOG.debug("Remote watcher created with config %r", self.config)

    def run(self):
        """Check remote watch target."""
        # TODO: ftp. ssh, and remote rTorrent instance (extra view?) as sources!
        # config:
        #   local_dir   storage path (default local sessiondir + '/remote-watch-' + jobname
        #   target      URL of target to watch


class TreeWatchHandler(pyinotify.ProcessEvent):
    """inotify event handler for rTorrent folder tree watch.

    See https://github.com/seb-m/pyinotify/.
    """

    METAFILE_EXT = (".torrent", ".load", ".start", ".queue")

    def handle_path(self, event):
        """Handle a path-related event."""
        self.job.LOG.debug(f"Notification {event!r}")
        if event.dir:
            return

        if any(event.pathname.endswith(i) for i in self.METAFILE_EXT):
            MetafileHandler(self.job, event.pathname).handle()
        elif os.path.basename(event.pathname) == "watch.ini":
            self.job.LOG.info(f"NOT YET Reloading watch config for '{event.path}'")
            # TODO: Load new metadata

    def process_IN_CLOSE_WRITE(self, event):
        """File written."""
        # <Event dir=False name=xx path=/var/torrent/watch/tmp pathname=/var/torrent/watch/tmp/xx>
        self.handle_path(event)

    def process_IN_MOVED_TO(self, event):
        """File moved into tree."""
        # <Event dir=False name=yy path=/var/torrent/watch/tmp pathname=/var/torrent/watch/tmp/yy>
        self.handle_path(event)

    def process_default(self, event):
        """Fallback."""
        if self.job.LOG.isEnabledFor(logging.DEBUG):
            # On debug level, we subscribe to ALL events, so they're expected in that case ;)
            if self.job.config.trace_inotify:
                self.job.LOG.debug(f"Ignored inotify event:\n    {event!r}")
        else:
            self.job.LOG.warning(f"Unexpected inotify event {event!r}")


class TreeWatch:
    """rTorrent folder tree watch via inotify."""

    def __init__(self, config=None):
        self.config = config or {}
        self.LOG = pymagic.get_class_logger(self)
        if "log_level" in self.config:
            self.LOG.setLevel(config["log_level"])
        self.LOG.debug("Tree watcher created with config %r", self.config)

        self.manager = None
        self.handler = None
        self.notifier = None

        if "path" not in self.config:
            raise error.UserError("You need to set 'path' in the configuration!")

        # self.config.quiet = bool_param("quiet", False)
        # self.config.queued = bool_param("queued", False)
        # self.config.trace_inotify = bool_param("trace_inotify", False)

        self.config["path"] = {
            Path(p).expanduser().absolute()
            for p in self.config["path"].split(os.pathsep)
        }
        for path in self.config["path"]:
            if not path.is_dir():
                raise error.UserError(f"Path '{path}' is not a directory!")

        # Assemble custom commands
        self.custom_cmds = {}
        for key, val in self.config.items():
            if key.startswith('cmd.'):
                self.custom_cmds[key] = val

        # Get client proxy
        self.proxy = rpc.RTorrentProxy(configuration.settings.SCGI_URL)

        self.setup()

    def setup(self):
        """Set up inotify manager.

        See https://github.com/seb-m/pyinotify/.
        """
        if not pyinotify.WatchManager:
            raise error.UserError(
                f"You need to install 'pyinotify' to use {self.__class__.__name__}!"
            )

        self.manager = pyinotify.WatchManager()
        self.handler = TreeWatchHandler(job=self)
        self.notifier = pyinotify.AsyncNotifier(self.manager, self.handler)

        if self.LOG.isEnabledFor(logging.DEBUG):
            mask = pyinotify.ALL_EVENTS
        else:
            mask = (
                pyinotify.IN_CLOSE_WRITE  # pylint: disable=no-member
                | pyinotify.IN_MOVED_TO  # pylint: disable=no-member
            )

        # Add all configured base dirs
        for path in self.config["path"]:
            self.manager.add_watch(path, mask, rec=True, auto_add=True)

    def run(self):
        """Regular maintenance and fallback task."""
        if self.config.get("check_unhandled", False):
            for path in self.config["path"]:
                for filepath in Path(path).rglob("**/*.torrent"):
                    MetafileHandler(self, filepath).handle()
                    if self.config.get("remove_unhandled", False) and filepath.exists():
                        filepath.unlink()


class TreeWatchCommand(ScriptBaseWithConfig):
    ### Keep things wrapped to fit under this comment... ##############################
    """
    Use tree watcher directly from cmd line, call it like this:
        python -m pyrosimple.torrent.watch <DIR>

    If the argument is a file, the templating namespace for that metafile is
    dumped (for testing and debugging purposes).
    """

    # log level for user-visible standard logging
    STD_LOG_LEVEL = logging.DEBUG

    # argument description for the usage information
    ARGS_HELP = "<directory>"

    OPTIONAL_CFG_FILES = ["torque.ini"]

    def mainloop(self):
        """The main loop."""
        # Print usage if not enough args or bad options
        if len(self.args) < 1:
            self.parser.error(
                "You have to provide the root directory of your watch tree, or a metafile path!"
            )

        pathname = os.path.abspath(self.args[0])
        if os.path.isdir(pathname):
            watch = TreeWatch(
                Bunch(
                    path=pathname,
                    job_name="watch",
                    active=True,
                    dry_run=True,
                    load_mode=None,
                )
            )
            asyncio.sleep(0)
        else:
            config = Bunch()
            config.update(
                {
                    key.split(".", 2)[-1]: val
                    for key, val in configuration.settings.TORQUE.items()
                    if key.startswith("job.treewatch.")
                }
            )
            config.update(
                dict(
                    path=os.path.dirname(os.path.dirname(pathname)),
                    job_name="treewatch",
                    active=False,
                    dry_run=True,
                )
            )
            watch = TreeWatch(config)
            handler = MetafileHandler(watch, pathname)

            ok = handler.parse()
            self.LOG.debug(
                "Metafile '%s' would've %sbeen loaded", pathname, ("" if ok else "NOT ")
            )

            if ok:
                handler.addinfo()
                self.LOG.info(
                    "Templating values are:\n    %s",
                    "\n    ".join(
                        f"{key}={repr(val)}"
                        for key, val in sorted(handler.ns.items())
                    ),
                )

    @classmethod
    def main(cls):
        """The entry point."""
        ScriptBase.setup()
        cls().run()


if __name__ == "__main__":
    TreeWatchCommand.main()
