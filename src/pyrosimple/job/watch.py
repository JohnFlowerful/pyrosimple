""" rTorrent Watch Jobs.

    Copyright (c) 2012 The PyroScope Project <pyroscope.project@gmail.com>
"""


import os
import threading
import time

from pathlib import Path
from typing import Dict, Optional, Sequence

import inotify.adapters
import inotify.constants

from pyrosimple import config as configuration
from pyrosimple import error
from pyrosimple.job.base import BaseJob
from pyrosimple.torrent import rtorrent
from pyrosimple.util import metafile, rpc


class TreeWatch(BaseJob):
    """Uses a thread to load torrent files via inotify. The scheduled
    run is used to check for the thread's liveness, and optionally try
    to load any files the watch may have missed"""

    def __init__(self, config: Optional[Dict] = None, name=""):
        """Initialize watch job and set default"""
        super().__init__(config or {}, name=name)
        self.watch_thread: Optional[threading.Thread] = None
        self.config.setdefault("print_to_client", True)
        self.config.setdefault("started", False)
        self.config.setdefault("trace_inotify", False)
        self.config.setdefault("check_unhandled", False)
        self.config.setdefault("remove_unhandled", False)
        self.config.setdefault("remove_already_added", False)
        self.config["paths"] = {
            Path(p).expanduser().absolute()
            for p in self.config["path"].split(os.pathsep)
        }
        self.custom_cmds = {}
        for key, val in self.config.items():
            if key.startswith("cmd_"):
                self.custom_cmds[key] = val
        self.run()

    def run(self):
        """Start the watcher if it's not running, and load any unhandled files"""
        if self.watch_thread is not None and not self.watch_thread.is_alive():
            self.log.warning("Watcher thread died, restarting")
            self.watch_thread = None
        if self.watch_thread is None:
            self.watch_thread = threading.Thread(
                target=self.watch_trees,
                args=(self.config["paths"],),
                daemon=True,
            )
            self.watch_thread.start()
        if self.config.get("check_unhandled", False):
            for path in self.config["paths"]:
                for filepath in path.rglob("**/*.torrent"):
                    self.load_metafile(filepath)
                    if (
                        self.config.get("remove_unhandled", False)
                        and filepath.exists()
                        and not self.config["dry_run"]
                    ):
                        filepath.unlink()

    def load_metafile(self, metapath: Path):
        """Load file into client, with templating and load commands"""
        # Perform some sanity checks on the file
        if metapath.suffix not in {".torrent", ".load", ".start", ".queue"}:
            self.log.debug("Unrecognized extension %s, skipping", metapath.suffix)
            return
        if not metapath.is_file():
            self.log.debug("Path is not a file: %s", metapath)
            return
        if metapath.stat().st_size == 0:
            self.log.debug("Skipping 0-byte file %s", metapath)
            return
        metainfo = metafile.Metafile.from_file(metapath)
        try:
            metainfo.check_meta()
        except ValueError as exc:
            self.log.error("Could not validate torrent file %s: %s", metapath, exc)
            return
        proxy = self.engine.open()
        try:
            proxy.d.hash(metainfo.info_hash())
            self.log.info(
                "Hash %s already found in client, skipping", metainfo.info_hash()
            )
            return
        except rpc.HashNotFound:
            pass
        # Build templating values
        template_vars = {
            "pathname": str(metapath),
            "info_hash": metainfo.info_hash(),
            "info_name": metainfo["info"]["name"],
            "watch_path": self.config["path"],
        }
        if metainfo.get("announce", ""):
            template_vars["tracker_alias"] = configuration.map_announce2alias(
                metainfo["announce"]
            )
        main_file = metainfo["info"]["name"]
        if "files" in metainfo["info"]:
            main_file = list(
                sorted((i["length"], i["path"][-1]) for i in metainfo["info"]["files"])
            )[-1][1]
        template_vars["filetype"] = os.path.splitext(main_file)[1]
        template_vars["commands"] = []
        flags = str(metapath).split(os.sep)
        flags.extend(flags[-1].split("."))
        template_vars["flags"] = {i for i in flags if i}
        for key, cmd in sorted(self.custom_cmds.items()):
            try:
                template = rtorrent.env.from_string(cmd)
                for split_cmd in rtorrent.format_item(
                    template, {}, defaults=template_vars
                ).split():
                    template_vars["commands"].append(split_cmd.strip())
            except error.LoggableError as exc:
                self.log.error("While expanding '%s' custom command: %r", key, exc)

        if self.config["load_mode"] in ("start", "started"):
            load_cmd = proxy.load.start_verbose
        else:
            load_cmd = proxy.load.verbose
        if "start" in template_vars["flags"]:
            load_cmd = proxy.load.start_verbose
        elif "load" in template_vars["flags"]:
            load_cmd = proxy.load.verbose
        self.log.debug("Templating values are: %r", template_vars.items())
        if self.config["dry_run"]:
            self.log.info(
                "Would load %s with commands %r", metapath, template_vars["commands"]
            )
            return

        self.log.info(
            "Loading %s with commands %r", metapath, template_vars["commands"]
        )
        load_cmd(rpc.NOHASH, str(metapath), *tuple(template_vars["commands"]))
        time.sleep(0.05)  # let things settle
        # Announce new item
        if self.config["print_to_client"]:
            try:
                name = proxy.d.name(metainfo.info_hash())
            except rpc.HashNotFound:
                name = "NOHASH"
            proxy.log(rpc.NOHASH, f"{self.name}: Loaded	{name} '{str(metapath)}'")

    def watch_trees(self, paths: Sequence[os.PathLike]):
        """Thread-able inotify watcher"""
        watcher = inotify.adapters.InotifyTrees(
            [str(p) for p in paths],
            block_duration_s=5,
            mask=inotify.constants.IN_CLOSE_WRITE | inotify.constants.IN_MOVED_TO,
        )
        for event in watcher.event_gen():
            if event is None:
                continue
            try:
                _header, _type_names, path, filename = event
                if self.config["trace_inotify"]:
                    self.log.info("%r", event)
                metapath = Path(path, filename)
                self.load_metafile(metapath)
            except Exception as exc:  # pylint: disable=broad-except
                self.log.error("Could not load metafile from event %s: %s", event, exc)
