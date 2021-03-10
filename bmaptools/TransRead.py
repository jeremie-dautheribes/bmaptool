# -*- coding: utf-8 -*-
# vim: ts=4 sw=4 et ai si
#
# Copyright (c) 2012-2014 Intel, Inc.
# License: GPLv2
# Author: Artem Bityutskiy <artem.bityutskiy@linux.intel.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License, version 2,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.

"""
This module allows opening and reading local and remote files and decompress
them on-the-fly if needed. Remote files are read using urllib (except of
"ssh://" URLs, which are handled differently). Supported file extentions are:
'bz2', 'gz', 'xz', 'lzo', 'zst' and a "tar" version of them: 'tar.bz2', 'tbz2',
'tbz', 'tb2', 'tar.gz', 'tgz', 'tar.xz', 'txz', 'tar.lzo', 'tzo', 'tar.lz4',
'tlz4', '.tar.zst', 'tzst'.
This module uses the following system programs for decompressing: pbzip2, bzip2,
gzip, pigz, xz, lzop, lz4, zstd, tar and unzip.
"""

import os
import io
import errno
import sys
import logging
import threading
import subprocess
from six.moves.urllib import parse as urlparse
from bmaptools import BmapHelpers

_log = logging.getLogger(__name__)  # pylint: disable=C0103

# Disable the following pylint errors and recommendations:
#   * Instance of X has no member Y (E1101), because it produces
#     false-positives for many of 'subprocess' class members, e.g.
#     "Instance of 'Popen' has no 'wait' member".
#   * Too many instance attributes (R0902)
#   * Too many branches (R0912)
#   * Too many local variables (R0914)
#   * Too many statements (R0915)
# pylint: disable=E1101
# pylint: disable=R0902
# pylint: disable=R0912
# pylint: disable=R0914
# pylint: disable=R0915

# A list of supported compression types
SUPPORTED_COMPRESSION_TYPES = ('bz2', 'gz', 'xz', 'lzo', 'lz4', 'zst',
                               'tar.gz', 'tar.bz2', 'tar.xz', 'tar.lzo',
                               'tar.lz4', 'tar.zst', 'zip')


def _fake_seek_forward(file_obj, cur_pos, offset, whence=os.SEEK_SET):
    """
    This function implements the 'seek()' method for file object 'file_obj'.
    Only seeking forward and is allowed, and 'whence' may be either
    'os.SEEK_SET' or 'os.SEEK_CUR'.
    """

    if whence == os.SEEK_SET:
        new_pos = offset
    elif whence == os.SEEK_CUR:
        new_pos = cur_pos + offset
    else:
        raise Error("'seek()' method requires the 'whence' argument "
                    "to be %d or %d, but %d was passed"
                    % (os.SEEK_SET, os.SEEK_CUR, whence))

    if new_pos < cur_pos:
        raise Error("''seek()' method supports only seeking forward, "
                    "seeking from %d to %d is not allowed"
                    % (cur_pos, new_pos))

    length = new_pos - cur_pos
    to_read = length
    while to_read > 0:
        chunk_size = min(to_read, 1024 * 1024)
        buf = file_obj.read(chunk_size)
        if not buf:
            break
        to_read -= len(buf)

    if to_read < 0:
        raise Error("seeked too far: %d instead of %d"
                    % (new_pos - to_read, new_pos))

    return new_pos - to_read


class Error(Exception):
    """
    A class for exceptions generated by this module. We currently support only
    one type of exceptions, and we basically throw human-readable problem
    description in case of errors.
    """
    pass


def _decode_sshpass_exit_code(code):
    """
    A helper function which converts "sshpass" command-line tool's exit code
    into a human-readable string. See "man sshpass".
    """

    if code == 1:
        result = "invalid command line argument"
    elif code == 2:
        result = "conflicting arguments given"
    elif code == 3:
        result = "general run-time error"
    elif code == 4:
        result = "unrecognized response from ssh (parse error)"
    elif code == 5:
        result = "invalid/incorrect password"
    elif code == 6:
        result = "host public key is unknown. sshpass exits without " \
                 "confirming the new key"
    elif code == 255:
        # SSH result =s 255 on any error
        result = "ssh error"
    else:
        result = "unknown"

    return result


class TransRead(object):
    """
    This class implement the transparent reading functionality. Instances of
    this class are file-like objects which you can read and seek only forward.
    """

    def __init__(self, filepath):
        """
        Class constructor. The 'filepath' argument is the full path to the file
        to read transparently.
        """

        self.name = filepath
        # Size of the file (in uncompressed form), may be 'None' if the size is
        # unknown
        self.size = None
        # Type of the compression of the file
        self.compression_type = 'none'
        # Whether the 'bz2file' PyPI module was found
        self.bz2file_found = False
        # Whether the file is behind an URL
        self.is_url = False
        # List of child processes we forked
        self._child_processes = []
        # The reader thread
        self._rthread = None
        # This variable becomes 'True' when the instance of this class is not
        # usable any longer.
        self._done = False
        # There may be a chain of open files, and we save the intermediate file
        # objects in the 'self._f_objs' list. The final file object is stored
        # in th elast element of the list.
        #
        # For example, when the path is an URL to a bz2 file, the chain of
        # opened file will be:
        #   o self._f_objs[0] is the urllib2 file-like object
        #   o self._f_objs[1] is the stdout of the 'bzip2' process
        self._f_objs = []

        self._fake_seek = False
        self._pos = 0

        try:
            self._f_objs.append(open(self.name, "rb"))
        except IOError as err:
            if err.errno == errno.ENOENT:
                # This is probably an URL
                self._open_url(filepath)
            else:
                raise Error("cannot open file '%s': %s" % (filepath, err))

        self._open_compressed_file()

    def __del__(self):
        """The class destructor which closes opened files."""
        self._done = True

        if getattr(self, "_f_objs"):
            for file_obj in self._f_objs:
                file_obj.close()
            self._f_objs = None

        if getattr(self, "_rthread"):
            self._rthread.join()
            self._rthread = None

        if getattr(self, "_child_processes"):
            for child in self._child_processes:
                if child.poll() is None:
                    child.kill()
                    child.wait()
            self._child_processes = []

    def _read_thread(self, f_from, f_to):
        """
        This function is used when reading compressed files. It runs in a
        spearate thread, reads data from the 'f_from' file-like object, and
        writes them to the 'f_to' file-like object. 'F_from' may be a urllib
        object, while 'f_to' is usually stdin of the decompressor process.
        """

        chunk_size = 1024 * 1024
        try:
            while not self._done:
                buf = f_from.read(chunk_size)
                if not buf:
                    break

                f_to.write(buf)
        finally:
            # This will make sure the process decompressor gets EOF and exits, as
            # well as ublocks processes waiting on decompressor's stdin.
            f_to.close()

    def _open_compressed_file(self):
        """
        Detect file compression type and open it with the corresponding
        compression module, or just plain 'open() if the file is not
        compressed.
        """

        def is_gzip(name):
            """Returns 'True' if file 'name' is compressed with 'gzip'."""
            if name.endswith('.gzip') or \
               (name.endswith('.gz') and not name.endswith('.tar.gz')):
                return True
            return False

        def is_bzip2(name):
            """Returns 'True' if file 'name' is compressed with 'bzip2'."""
            if name.endswith('.bz2') and not name.endswith('.tar.bz2'):
                return True
            return False

        def is_xz(name):
            """Returns 'True' if file 'name' is compressed with 'xz'."""
            if name.endswith('.xz') and not name.endswith('.tar.xz'):
                return True
            return False

        def is_lzop(name):
            """Returns 'True' if file 'name' is compressed with 'lzop'."""
            if name.endswith('.lzo') and not name.endswith('.tar.lzo'):
                return True
            return False

        def is_lz4(name):
            """Returns 'True' if file 'name' is compressed with 'lz4'."""
            if name.endswith('.lz4') and not name.endswith('.tar.lz4'):
                return True
            return False

        def is_zst(name):
            """Returns 'True' if file 'name' is compressed with 'zstd'."""
            if name.endswith('.zst') and not name.endswith('.tar.zst'):
                return True
            return False

        def is_tar_gz(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with
            'gzip'.
            """

            if name.endswith('.tar.gz') or name.endswith('.tgz'):
                return True
            return False

        def is_tar_bz2(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with
            'bzip2'.
            """

            if name.endswith('.tar.bz2') or name.endswith('.tbz') or \
               name.endswith('.tbz2') or name.endswith('.tb2'):
                return True
            return False

        def is_tar_xz(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with 'xz'.
            """

            if name.endswith('.tar.xz') or name.endswith('.txz'):
                return True
            return False

        def is_tar_lzo(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with
            'lzop'.
            """

            if name.endswith('.tar.lzo') or name.endswith('.tzo'):
                return True
            return False

        def is_tar_lz4(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with
            'lz4'.
            """

            if name.endswith('.tar.lz4') or name.endswith('.tlz4'):
                return True
            return False

        def is_tar_zst(name):
            """
            Returns 'True' if file 'name' is a tar archive compressed with
            'zstd'.
            """

            if name.endswith('.tar.zst') or name.endswith('.tzst'):
                return True
            return False

        archiver = None
        if is_tar_gz(self.name) or is_gzip(self.name):
            self.compression_type = 'gzip'
            if BmapHelpers.program_is_available("pigz"):
                decompressor = "pigz"
            else:
                decompressor = "gzip"

            if is_gzip(self.name):
                args = "-d -c"
            else:
                archiver = "tar"
                args = "-x -z -O"
        elif is_tar_bz2(self.name) or is_bzip2(self.name):
            self.compression_type = 'bzip2'
            if BmapHelpers.program_is_available("pbzip2"):
                decompressor = "pbzip2"
            else:
                decompressor = "bzip2"

            if is_bzip2(self.name):
                args = "-d -c"
            else:
                archiver = "tar"
                args = "-x -j -O"
        elif is_tar_xz(self.name) or is_xz(self.name):
            self.compression_type = 'xz'
            decompressor = "xz"
            if is_xz(self.name):
                args = "-d -c"
            else:
                archiver = "tar"
                args = "-x -J -O"
        elif is_tar_lzo(self.name) or is_lzop(self.name):
            self.compression_type = 'lzo'
            decompressor = "lzop"
            if is_lzop(self.name):
                args = "-d -c"
            else:
                archiver = "tar"
                args = "-x --lzo -O"
        elif self.name.endswith(".zip"):
            self.compression_type = 'zip'
            decompressor = "funzip"
            args = ""
        elif is_tar_lz4(self.name) or is_lz4(self.name):
            self.compression_type = 'lz4'
            decompressor = "lz4"
            if is_lz4(self.name):
                args = "-d -c"
            else:
                archiver = "tar"
                args = "-x -Ilz4 -O"
        elif is_tar_zst(self.name) or is_zst(self.name):
            self.compression_type = 'zst'
            decompressor = "zstd"
            if is_zst(self.name):
                args = "-d"
            else:
                archiver = "tar"
                args = "-x -Izstd -O"
        else:
            if not self.is_url:
                self.size = os.fstat(self._f_objs[-1].fileno()).st_size
            return

        # Make sure decompressor and the archiver programs are available
        if not BmapHelpers.program_is_available(decompressor):
            raise Error("the \"%s\" program is not available but it is "
                        "required decompressing \"%s\""
                        % (decompressor, self.name))
        if archiver and not BmapHelpers.program_is_available(archiver):
            raise Error("the \"%s\" program is not available but it is "
                        "required reading \"%s\"" % (archiver, self.name))

        # Start the decompressor process. We'll send the data to its stdin and
        # read the decompressed data from its stdout.
        if archiver:
            args = archiver + " " + args
        else:
            args = decompressor + " " + args

        if self.is_url:
            child_stdin = subprocess.PIPE
        else:
            child_stdin = self._f_objs[-1].fileno()

        child_process = subprocess.Popen(args, shell=True,
                                         bufsize=1024 * 1024,
                                         stdin=child_stdin,
                                         stdout=subprocess.PIPE)

        if child_stdin == subprocess.PIPE:
            # A separate reader thread is created only when we are reading via
            # urllib2.
            args = (self._f_objs[-1], child_process.stdin, )
            self._rthread = threading.Thread(target=self._read_thread, args=args)
            self._rthread.daemon = True
            self._rthread.start()

        self._fake_seek = True
        self._f_objs.append(child_process.stdout)
        self._child_processes.append(child_process)

    def _open_url_ssh(self, parsed_url):
        """
        This function opens a file on a remote host using SSH. The URL has to
        have this format: "ssh://username@hostname:path". Currently we only
        support password-based authentication.
        """

        username = parsed_url.username
        password = parsed_url.password
        path = parsed_url.path
        hostname = parsed_url.hostname
        if username:
            hostname = username + "@" + hostname

        # Make sure the ssh client program is installed
        if not BmapHelpers.program_is_available("ssh"):
            raise Error("the \"ssh\" program is not available but it is "
                        "required for downloading over the ssh protocol")

        # Prepare the commands that we are going to run
        if password:
            # In case of password we have to use the sshpass tool to pass the
            # password to the ssh client utility
            popen_args = ["sshpass",
                          "-p" + password,
                          "ssh",
                          "-o StrictHostKeyChecking=no",
                          "-o PubkeyAuthentication=no",
                          "-o PasswordAuthentication=yes",
                          hostname]

            # Make sure the sshpass program is installed
            if not BmapHelpers.program_is_available("ssh"):
                raise Error("the \"sshpass\" program is not available but it "
                            "is required for password-based SSH authentication")
        else:
            popen_args = ["ssh",
                          "-o StrictHostKeyChecking=no",
                          "-o PubkeyAuthentication=yes",
                          "-o PasswordAuthentication=no",
                          "-o BatchMode=yes",
                          hostname]

        # Test if we can successfully connect
        child_process = subprocess.Popen(popen_args + ["true"])
        child_process.wait()
        retcode = child_process.returncode
        if retcode != 0:
            decoded = _decode_sshpass_exit_code(retcode)
            raise Error("cannot connect to \"%s\": %s (error code %d)"
                        % (hostname, decoded, retcode))

        # Test if file exists by running "test -f path && test -r path" on the
        # host
        command = "test -f " + path + " && test -r " + path
        child_process = subprocess.Popen(popen_args + [command],
                                         bufsize=1024 * 1024)
        child_process.wait()
        if child_process.returncode != 0:
            raise Error("\"%s\" on \"%s\" cannot be read: make sure it "
                        "exists, is a regular file, and you have read "
                        "permissions" % (path, hostname))

        # Read the entire file using 'cat'
        child_process = subprocess.Popen(popen_args + ["cat " + path],
                                         stdout=subprocess.PIPE)

        # Now the contents of the file should be available from sub-processes
        # stdout
        self._f_objs.append(child_process.stdout)

        self._child_processes.append(child_process)
        self.is_url = True
        self._fake_seek = True

    def _open_url(self, url):
        """
        Open an URL 'url' and return the file-like object of the opened URL.
        """

        def _print_warning(timeout):
            """
            This is a small helper function for printing a warning if we cannot
            open the URL for some time.
            """
            _log.warning("failed to open the URL with %d sec timeout, is the "
                         "proxy configured correctly? Keep trying ..." %
                         timeout)

        import socket

        from six.moves import http_client as httplib
        from six.moves.urllib import request as urllib
        from six.moves.urllib.error import URLError

        parsed_url = urlparse.urlparse(url)

        if parsed_url.scheme == "ssh":
            # Unfortunately, urllib2 does not handle "ssh://" URLs
            self._open_url_ssh(parsed_url)
            return

        username = parsed_url.username
        password = parsed_url.password

        if username and password:
            # Unfortunately, in order to handle URLs which contain user name
            # and password (e.g., http://user:password@my.site.org), we need to
            # do few extra things.
            new_url = list(parsed_url)
            if parsed_url.port:
                new_url[1] = "%s:%s" % (parsed_url.hostname, parsed_url.port)
            else:
                new_url[1] = parsed_url.hostname
            url = urlparse.urlunparse(new_url)

            # Build an URL opener which will do the authentication
            password_manager = urllib.HTTPPasswordMgrWithDefaultRealm()
            password_manager.add_password(None, url, username, password)
            auth_handler = urllib.HTTPBasicAuthHandler(password_manager)
            opener = urllib.build_opener(auth_handler)
        else:
            opener = urllib.build_opener()

        opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
        urllib.install_opener(opener)

        # Open the URL. First try with a short timeout, and print a message
        # which should supposedly give the a clue that something may be going
        # wrong.
        # The overall purpose of this is to improve user experience. For
        # example, if one tries to open a file but did not setup the proxy
        # environment variables propely, there will be a very long delay before
        # the failure message. And it is much nicer to pre-warn the user early
        # about something possibly being wrong.
        for timeout in (10, None):
            try:
                f_obj = opener.open(url, timeout=timeout)
            # Handling the timeout case in Python 2.7
            except socket.timeout as err:
                if timeout is not None:
                    _print_warning(timeout)
                else:
                    raise Error("cannot open URL '%s': %s" % (url, err))
            except URLError as err:
                # Handling the timeout case in Python 2.6
                if timeout is not None and \
                   isinstance(err.reason, socket.timeout):
                    _print_warning(timeout)
                else:
                    raise Error("cannot open URL '%s': %s" % (url, err))
            except (IOError, ValueError, httplib.InvalidURL) as err:
                raise Error("cannot open URL '%s': %s" % (url, err))
            except httplib.BadStatusLine:
                raise Error("cannot open URL '%s': server responds with an "
                            "HTTP status code that we don't understand" % url)

        self.is_url = True
        self._f_objs.append(f_obj)

    def read(self, size=-1):
        """
        Read the data from the file or URL and and uncompress it on-the-fly if
        necessary.
        """

        if size < 0:
            size = 0xFFFFFFFFFFFFFFFF
        buf = self._f_objs[-1].read(size)
        self._pos += len(buf)

        return buf

    def seek(self, offset, whence=os.SEEK_SET):
        """The 'seek()' method, similar to the one file objects have."""
        if self._fake_seek or not hasattr(self._f_objs[-1], "seek"):
            self._pos = _fake_seek_forward(self._f_objs[-1], self._pos,
                                           offset, whence)
        else:
            try:
                self._f_objs[-1].seek(offset, whence)
            except io.UnsupportedOperation:
                self._fake_seek = True
                self._pos = _fake_seek_forward(self._f_objs[-1], self._pos,
                                               offset, whence)

    def tell(self):
        """The 'tell()' method, similar to the one file objects have."""
        if self._fake_seek or not hasattr(self._f_objs[-1], "tell"):
            return self._pos
        else:
            return self._f_objs[-1].tell()

    def close(self):
        """Close the file-like object."""
        self.__del__()

    def __getattr__(self, name):
        """
        If we are backed by a local uncompressed file, then fall-back to using
        its operations.
        """

        if self.compression_type == 'none' and not self.is_url:
            return getattr(self._f_objs[-1], name)
        else:
            raise AttributeError
