# pyrosimple

[![GitHub Workflow Status](http://img.shields.io/github/actions/workflow/status/kannibalox/pyrosimple/pylint.yml?branch=main)](https://github.com/kannibalox/pyrosimple/actions/workflows/pylint.yml)
[![PyPI](https://img.shields.io/pypi/v/pyrosimple)](https://pypi.org/project/pyrosimple/)
![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pyrosimple)

A overhauled Python 3 fork of the [pyrocore
tools](https://github.com/pyroscope/pyrocore), for working with the
[rTorrent client](https://github.com/rakshasa/rtorrent).

## Installation

```bash
pip install pyrosimple
# pip install 'pyrosimple[torque]' # Optional dependencies for using pyrotorque
```

See the [documentation for usage](https://kannibalox.github.io/pyrosimple/).
If you've used rtcontrol/rtxmlrpc before, you should feel right at home.

## What's the point of this?

The pyrocore tools are great, but being stuck on python 2, along with
the complicated install procedure made integrating both the tools and
the code into other processes very painful.

## Differences from pyrocore

The following lists are not exhaustive, and don't cover many of the
internal improvements and refactoring.

- Only supports python 3 and rTorrent 0.9.6+ (this includes
  rTorrent-PS and rTorrent-PS-CH)
  - pypy is supported, but not as well tested
- Simpler poetry-based build system, with a single package to install
- Performance improvements (faster templating and only fetching
  required fields)

### New features

- Multi-instance support for rtcontrol/rtxmlrpc
- Replaced Tempita with Jinja2
- Support for JSON-RPC (only implemented by
  https://github.com/jesec/rtorrent)
- Actions to move torrent between paths, or torrents between hosts

See https://kannibalox.github.io/pyrosimple/migrate/ for how to
migrate scripts to the new features.

## Legacy branch

If you just want to use the pyrocore tools on python 3 without all the
new features, you can use the `release-1.X` branch (1.3 is the latest
release at time of writing).  These releases will only receive bug
fixes or changes to maintain compatibility with the original pyrocore
tools.
