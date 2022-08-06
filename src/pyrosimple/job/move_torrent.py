"""Move a torrent to a remote host

Currently uses consistent hashing to determine which remote host
gets the torrent (if multiple are specified)."""
import hashlib

from pathlib import Path
from typing import Dict, List

import bencode

import pyrosimple

from pyrosimple import config, error
from pyrosimple.job import base
from pyrosimple.util import matching, pymagic, rpc


def nodes_by_hash_weight(meta_id: str, nodes: List[str]) -> Dict[str, int]:
    """Weight nodes by hashing the meta_id"""
    result = {
        n: int.from_bytes(hashlib.sha256(meta_id.encode() + n.encode()).digest(), "big")
        for n in nodes
    }
    return dict(sorted(result.items(), key=lambda item: item[1]))


def get_custom_fields(infohash, proxy):
    """Try using rtorrent-ps commands to list custom keys, otherwise fall back to reading from a session file."""
    if "d.custom.keys" in proxy.system.listMethods():
        custom_fields = {}
        for key in proxy.d.custom.keys(infohash):
            custom_fields[key] = proxy.d.custom(infohash, key)
    else:
        info_file = Path(proxy.session.path(), f"{infohash}.torrent.rtorrent")
        proxy.d.save_full_session(infohash)
        with open(info_file, "rb") as fh:
            custom_fields = bencode.bread(fh)["custom"]
    return custom_fields


class Mover(base.MatchableJob):
    """Move torrent to remote host(s)"""

    def __init__(self, job_config=None):
        """Initalize torrent mover job"""
        super().__init__(job_config)
        self.config["hosts"] = [
            config.lookup_connection_alias(h) for h in self.config["hosts"]
        ]

    def run_item(self, item):
        """Statistics logger job callback."""
        for host in nodes_by_hash_weight(i.hash + i.alias, self.config["hosts"]):
            rproxy = rpc.RTorrentProxy(host)
            metahash = i.hash
            try:
                rproxy.d.hash(i.hash)
            except rpc.HashNotFound:
                pass
            else:
                self.LOG.info("Hash %s already exists at remote URL %s", i.hash, host)
                continue
            if self.config["dry_run"]:
                self.LOG.info("Would move %s to %s", metahash, rproxy.system.hostname())
                break
            i.move_to_host(host)
            self.LOG.info("Moved %s to %s", metahash, rproxy.system.hostname())
            break
