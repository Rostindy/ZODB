##############################################################################
#
# Copyright (c) 2001, 2002 Zope Corporation and Contributors.
# All Rights Reserved.
# 
# This software is subject to the provisions of the Zope Public License,
# Version 2.0 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
# 
##############################################################################
"""Implement a client cache

The cache is managed as two files, var/c0.zec and var/c1.zec.

Each cache file is a sequence of records of the form:

  oid -- 8-byte object id

  status -- 1-byte status 'v': valid, 'n': non-version valid, 'i': invalid

  tlen -- 4-byte (unsigned) record length

  vlen -- 2-bute (unsigned) version length

  dlen -- 4-byte length of non-version data

  serial -- 8-byte non-version serial (timestamp)

  data -- non-version data

  version -- Version string (if vlen > 0)

  vdlen -- 4-byte length of version data (if vlen > 0)

  vdata -- version data (if vlen > 0)

  vserial -- 8-byte version serial (timestamp) (if vlen > 0)

  tlen -- 4-byte (unsigned) record length (for redundancy and backward
          traversal)

There is a cache size limit.

The cache is managed as follows:

  - Data are written to file 0 until file 0 exceeds limit/2 in size.

  - Data are written to file 1 until file 1 exceeds limit/2 in size.

  - File 0 is truncated to size 0 (or deleted and recreated).

  - Data are written to file 0 until file 0 exceeds limit/2 in size.

  - File 1 is truncated to size 0 (or deleted and recreated).

  - Data are written to file 1 until file 1 exceeds limit/2 in size.

and so on.

On startup, index information is read from file 0 and file 1.
Current serial numbers are sent to the server for verification.
If any serial numbers are not valid, then the server will send back
invalidation messages and the cache entries will be invalidated.

When a cache record is invalidated, the data length is overwritten
with '\0\0\0\0'.

If var is not writable, then temporary files are used for
file 0 and file 1.

"""

__version__ = "$Revision: 1.23 $"[11:-2]

import os
import sys
import tempfile
from struct import pack, unpack
from thread import allocate_lock

import zLOG
from ZEO.ICache import ICache

def log(msg, level=zLOG.INFO):
    zLOG.LOG("ZEC", level, msg)

magic='ZEC0'

class ClientCache:

    __implements__ = ICache

    def __init__(self, storage='', size=20000000, client=None, var=None):

        # Allocate locks:
        L = allocate_lock()
        self._acquire = L.acquire
        self._release = L.release

        if client:
            # Create a persistent cache
            if var is None:
                try:
                    var = CLIENT_HOME
                except:
                    try:
                        var = os.path.join(INSTANCE_HOME, 'var')
                    except:
                        var = os.getcwd()

            # Get the list of cache file names
            self._p = p = map(lambda i, p=storage, var=var, c=client:
                                os.path.join(var, 'c%s-%s-%s.zec' % (p, c, i)),
                              (0, 1))
            # get the list of cache files
            self._f = f = [None, None]

            # initialize cache serial numbers
            s=['\0\0\0\0\0\0\0\0', '\0\0\0\0\0\0\0\0']
            for i in 0, 1:
                if os.path.exists(p[i]):
                    fi = open(p[i],'r+b')
                    if fi.read(4) == magic: # Minimal sanity
                        fi.seek(0, 2)
                        if fi.tell() > 30:
                            fi.seek(22)
                            s[i] = fi.read(8)
                    # If we found a non-zero serial, then use the file
                    if s[i] != '\0\0\0\0\0\0\0\0':
                        f[i] = fi
                    fi = None

            # Whoever has the larger serial is the current
            if s[1] > s[0]:
                current = 1
            elif s[0] > s[1]:
                current = 0
            else:
                if f[0] is None:
                    # We started, open the first cache file
                    f[0] = open(p[0], 'w+b')
                    f[0].write(magic)
                current = 0
                f[1] = None
        else:
            self._f = f = [tempfile.TemporaryFile(suffix='.zec'), None]
            # self._p file name 'None' signifies an unnamed temp file.
            self._p = p = [None, None]
            f[0].write(magic)
            current = 0

        log("cache opened.  current = %s" % current)

        self._limit = size / 2
        self._current = current

    def open(self):
        # XXX open is overloaded to perform two tasks for
        # optimization reasons
        self._acquire()
        try:
            self._index=index={}
            self._get = index.get
            serial = {}
            f = self._f
            current = self._current
            if f[not current] is not None:
                read_index(index, serial, f[not current], not current)
            self._pos = read_index(index, serial, f[current], current)

            return serial.items()
        finally:
            self._release()

    def close(self):
        for f in self._f:
            if f is not None:
                # In 2.1 on Windows, the TemporaryFileWrapper doesn't allow
                # closing a file more than once.
                try:
                    f.close()
                except OSError:
                    pass

    def verify(self, verifyFunc):
        """Call the verifyFunc on every object in the cache.

        verifyFunc(oid, serialno, version)
        """
        for oid, (s, vs) in self.open():
            verifyFunc(oid, s, vs)

    def invalidate(self, oid, version):
        self._acquire()
        try:
            p = self._get(oid, None)
            if p is None:
                return None
            f = self._f[p < 0]
            ap = abs(p)
            f.seek(ap)
            h = f.read(8)
            if h != oid:
                return
            f.seek(8, 1) # Dang, we shouldn't have to do this. Bad Solaris & NT
            if version:
                f.write('n')
            else:
                del self._index[oid]
                f.write('i')
        finally:
            self._release()

    def load(self, oid, version):
        self._acquire()
        try:
            p = self._get(oid, None)
            if p is None:
                return None
            f = self._f[p < 0]
            ap = abs(p)
            seek = f.seek
            read = f.read
            seek(ap)
            h = read(27)
            if len(h)==27 and h[8] in 'nv' and h[:8]==oid:
                tlen, vlen, dlen = unpack(">iHi", h[9:19])
            else:
                tlen = -1
            if tlen <= 0 or vlen < 0 or dlen < 0 or vlen+dlen > tlen:
                del self._index[oid]
                return None

            if h[8]=='n':
                if version:
                    return None
                if not dlen:
                    del self._index[oid]
                    return None

            if not vlen or not version:
                if dlen:
                    return read(dlen), h[19:]
                else:
                    return None

            if dlen:
                seek(dlen, 1)
            v = read(vlen)
            if version != v:
                if dlen:
                    seek(-dlen-vlen, 1)
                    return read(dlen), h[19:]
                else:
                    return None

            dlen = unpack(">i", read(4))[0]
            return read(dlen), read(8)
        finally:
            self._release()

    def update(self, oid, serial, version, data):
        self._acquire()
        try:
            if version:
                # We need to find and include non-version data
                p = self._get(oid, None)
                if p is None:
                    return self._store(oid, '', '', version, data, serial)
                f = self._f[p < 0]
                ap = abs(p)
                seek = f.seek
                read = f.read
                seek(ap)
                h = read(27)
                if len(h)==27 and h[8] in 'nv' and h[:8]==oid:
                    tlen, vlen, dlen = unpack(">iHi", h[9:19])
                else:
                    return self._store(oid, '', '', version, data, serial)

                if tlen <= 0 or vlen < 0 or dlen <= 0 or vlen+dlen > tlen:
                    return self._store(oid, '', '', version, data, serial)

                if dlen:
                    p = read(dlen)
                    s = h[19:]
                else:
                    return self._store(oid, '', '', version, data, serial)

                self._store(oid, p, s, version, data, serial)
            else:
                # Simple case, just store new data:
                self._store(oid, data, serial, '', None, None)
        finally:
            self._release()

    def modifiedInVersion(self, oid):
        self._acquire()
        try:
            p = self._get(oid, None)
            if p is None:
                return None
            f = self._f[p < 0]
            ap = abs(p)
            seek = f.seek
            read = f.read
            seek(ap)
            h = read(27)
            if len(h)==27 and h[8] in 'nv' and h[:8]==oid:
                tlen, vlen, dlen = unpack(">iHi", h[9:19])
            else:
                tlen = -1
            if tlen <= 0 or vlen < 0 or dlen < 0 or vlen+dlen > tlen:
                del self._index[oid]
                return None

            if h[8] == 'n':
                return None

            if not vlen:
                return ''
            seek(dlen, 1)
            return read(vlen)
        finally:
            self._release()

    def checkSize(self, size):
        self._acquire()
        try:
            # Make sure we aren't going to exceed the target size.
            # If we are, then flip the cache.
            if self._pos + size > self._limit:
                current = not self._current
                self._current = current
                if self._p[current] is not None:
                    # Persistent cache file:
                    # Note that due to permission madness, waaa,
                    # we need to remove the old file before
                    # we open the new one. Waaaaaaaaaa.
                    if self._f[current] is not None:
                        self._f[current].close()
                        try:
                            os.remove(self._p[current])
                        except:
                            pass
                    self._f[current] = open(self._p[current],'w+b')
                else:
                    # Temporary cache file:
                    self._f[current] = tempfile.TemporaryFile(suffix='.zec')
                self._f[current].write(magic)
                self._pos = pos = 4
        finally:
            self._release()


    def store(self, oid, p, s, version, pv, sv):
        self._acquire()
        try:
            self._store(oid, p, s, version, pv, sv)
        finally:
            self._release()

    def _store(self, oid, p, s, version, pv, sv):
        if not s:
            p = ''
            s = '\0\0\0\0\0\0\0\0'
        tlen = 31 + len(p)
        if version:
            tlen = tlen + len(version) + 12 + len(pv)
            vlen = len(version)
        else:
            vlen = 0

        stlen = pack(">I", tlen)
        # accumulate various data to write into a list
        l = [oid, 'v', stlen, pack(">HI", vlen, len(p)), s]
        if p:
            l.append(p)
        if version:
            l.extend([version,
                      pack(">I", len(pv)),
                      pv, sv])
        l.append(stlen)
        f = self._f[self._current]
        f.seek(self._pos)
        f.write("".join(l))

        if self._current:
            self._index[oid] = - self._pos
        else:
            self._index[oid] = self._pos

        self._pos += tlen

def read_index(index, serial, f, current):
    seek = f.seek
    read = f.read
    pos = 4
    seek(0, 2)
    size = f.tell()

    while 1:
        f.seek(pos)
        h = read(27)

        if len(h)==27 and h[8] in 'vni':
            tlen, vlen, dlen = unpack(">iHi", h[9:19])
        else:
            tlen = -1
        if tlen <= 0 or vlen < 0 or dlen < 0 or vlen + dlen > tlen:
            break

        oid = h[:8]

        if h[8]=='v' and vlen:
            seek(dlen+vlen, 1)
            vdlen = read(4)
            if len(vdlen) != 4:
                break
            vdlen = unpack(">i", vdlen)[0]
            if vlen+dlen+42+vdlen > tlen:
                break
            seek(vdlen, 1)
            vs = read(8)
            if read(4) != h[9:13]:
                break
        else:
            vs = None

        if h[8] in 'vn':
            if current:
                index[oid] = -pos
            else:
                index[oid] = pos
            serial[oid] = h[-8:], vs
        else:
            if serial.has_key(oid):
                # We have a record for this oid, but it was invalidated!
                del serial[oid]
                del index[oid]


        pos = pos + tlen

    f.seek(pos)
    try:
        f.truncate()
    except:
        pass

    return pos
