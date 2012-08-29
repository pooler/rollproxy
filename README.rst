*********
RollProxy
*********

RollProxy is a mining proxy for Bitcoin and other cryptocurrencies that support
the getwork protocol.  It can take advantage of the X-Roll-NTime extension to
minimize the number of getwork requests sent to the work provider.  By keeping
more requests local, it minimizes latency, while drastically reducing the load
on mining pools.

Features
========

- Full support for persistent HTTP connections
- Long polling support
- Mining statistics available via web interface
- Basic failover mechanism
- Ability to connect through an additional proxy server

Requirements
============

- Python 2.6+ or 3+
- argparse (part of the standard library as of Python 2.7 and 3.2)
- urrlib3 <https://github.com/shazow/urllib3>

Quick start
===========

First make sure argparse and urllib3 are installed::

  # pip install argparse urllib3

If (and only if) you are going to use Python 3 or later, issue::

  $ 2to3 -w rollproxy.py

Start the proxy specifying the address and credentials for one or more mining
servers::

  $ python rollproxy.py http://username:password@host:port/

The proxy will listen on port 8345 by default.  A web interface is also made
available on the same port.  For a detailed list of supported options, run::

  $ python rollproxy.py --help

Miners connecting to the proxy do not need to authenticate, but if they supply
a username RollProxy will then be able to generate per-worker statistics.

License
=======

Copyright 2012 pooler <pooler@litecoinpool.org>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
