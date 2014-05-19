# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from eventlet import sleep, Timeout
from eventlet.green import httplib, socket, urllib2
import json
import struct
from sys import exc_info
import zlib
from swift import gettext_ as _
import urlparse
from zlib import compressobj

from swift.common.utils import quote
from swift.common.http import HTTP_NOT_FOUND
from swift.common.swob import Request
from swift.common.wsgi import loadapp


class UnexpectedResponse(Exception):
    """
    Exception raised on invalid responses to InternalClient.make_request().

    :param message: Exception message.
    :param resp: The unexpected response.
    """

    def __init__(self, message, resp):
        super(UnexpectedResponse, self).__init__(message)
        self.resp = resp


class CompressingFileReader(object):
    """
    Wrapper for file object to compress object while reading.

    Can be used to wrap file objects passed to InternalClient.upload_object().

    Used in testing of InternalClient.

    :param file_obj: File object to wrap.
    :param compresslevel:  Compression level, defaults to 9.
    :param chunk_size:  Size of chunks read when iterating using object,
                        defaults to 4096.
    """

    def __init__(self, file_obj, compresslevel=9, chunk_size=4096):
        self._f = file_obj
        self.compresslevel = compresslevel
        self.chunk_size = chunk_size
        self.set_initial_state()

    def set_initial_state(self):
        """
        Sets the object to the state needed for the first read.
        """

        self._f.seek(0)
        self._compressor = compressobj(
            self.compresslevel, zlib.DEFLATED, -zlib.MAX_WBITS,
            zlib.DEF_MEM_LEVEL, 0)
        self.done = False
        self.first = True
        self.crc32 = 0
        self.total_size = 0

    def read(self, *a, **kw):
        """
        Reads a chunk from the file object.

        Params are passed directly to the underlying file object's read().

        :returns: Compressed chunk from file object.
        """

        if self.done:
            return ''
        x = self._f.read(*a, **kw)
        if x:
            self.crc32 = zlib.crc32(x, self.crc32) & 0xffffffff
            self.total_size += len(x)
            compressed = self._compressor.compress(x)
            if not compressed:
                compressed = self._compressor.flush(zlib.Z_SYNC_FLUSH)
        else:
            compressed = self._compressor.flush(zlib.Z_FINISH)
            crc32 = struct.pack("<L", self.crc32 & 0xffffffff)
            size = struct.pack("<L", self.total_size & 0xffffffff)
            footer = crc32 + size
            compressed += footer
            self.done = True
        if self.first:
            self.first = False
            header = '\037\213\010\000\000\000\000\000\002\377'
            compressed = header + compressed
        return compressed

    def __iter__(self):
        return self

    def next(self):
        chunk = self.read(self.chunk_size)
        if chunk:
            return chunk
        raise StopIteration

    def seek(self, offset, whence=0):
        if not (offset == 0 and whence == 0):
            raise NotImplementedError('Seek implemented on offset 0 only')
        self.set_initial_state()


class InternalClient(object):
    """
    An internal client that uses a swift proxy app to make requests to Swift.

    This client will exponentially slow down for retries.

    :param conf_path: Full path to proxy config.
    :param user_agent: User agent to be sent to requests to Swift.
    :param request_tries: Number of tries before InternalClient.make_request()
                          gives up.
    """

    def __init__(self, conf_path, user_agent, request_tries,
                 allow_modify_pipeline=False):
        self.app = loadapp(conf_path,
                           allow_modify_pipeline=allow_modify_pipeline)
        self.user_agent = user_agent
        self.request_tries = request_tries

    def make_request(
            self, method, path, headers, acceptable_statuses, body_file=None):
        """
        Makes a request to Swift with retries.

        :param method: HTTP method of request.
        :param path: Path of request.
        :param headers: Headers to be sent with request.
        :param acceptable_statuses: List of acceptable statuses for request.
        :param body_file: Body file to be passed along with request,
                          defaults to None.

        :returns : Response object on success.

        :raises UnexpectedResponse: Exception raised when make_request() fails
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        headers = dict(headers)
        headers['user-agent'] = self.user_agent
        resp = exc_type = exc_value = exc_traceback = None
        for attempt in xrange(self.request_tries):
            req = Request.blank(
                path, environ={'REQUEST_METHOD': method}, headers=headers)
            if body_file is not None:
                if hasattr(body_file, 'seek'):
                    body_file.seek(0)
                req.body_file = body_file
            try:
                resp = req.get_response(self.app)
                if resp.status_int in acceptable_statuses or \
                        resp.status_int // 100 in acceptable_statuses:
                    return resp
            except (Exception, Timeout):
                exc_type, exc_value, exc_traceback = exc_info()
            # sleep only between tries, not after each one
            if attempt < self.request_tries - 1:
                sleep(2 ** (attempt + 1))
        if resp:
            raise UnexpectedResponse(
                _('Unexpected response: %s') % resp.status, resp)
        if exc_type:
            # To make pep8 tool happy, in place of raise t, v, tb:
            raise exc_type(*exc_value.args), None, exc_traceback

    def _get_metadata(
            self, path, metadata_prefix='', acceptable_statuses=(2,)):
        """
        Gets metadata by doing a HEAD on a path and using the metadata_prefix
        to get values from the headers returned.

        :param path: Path to do HEAD on.
        :param metadata_prefix: Used to filter values from the headers
                                returned.  Will strip that prefix from the
                                keys in the dict returned.  Defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :returns : A dict of metadata with metadata_prefix stripped from keys.
                   Keys will be lowercase.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        resp = self.make_request('HEAD', path, {}, acceptable_statuses)
        if not resp.status_int // 100 == 2:
            return {}
        metadata_prefix = metadata_prefix.lower()
        metadata = {}
        for k, v in resp.headers.iteritems():
            if k.lower().startswith(metadata_prefix):
                metadata[k[len(metadata_prefix):].lower()] = v
        return metadata

    def _iter_items(
            self, path, marker='', end_marker='',
            acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Returns an iterator of items from a json listing.  Assumes listing has
        'name' key defined and uses markers.

        :param path: Path to do GET on.
        :param marker: Prefix of first desired item, defaults to ''.
        :param end_marker: Last item returned will be 'less' than this,
                           defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        while True:
            resp = self.make_request(
                'GET', '%s?format=json&marker=%s&end_marker=%s' %
                (path, quote(marker), quote(end_marker)),
                {}, acceptable_statuses)
            if not resp.status_int == 200:
                break
            data = json.loads(resp.body)
            if not data:
                break
            for item in data:
                yield item
            marker = data[-1]['name'].encode('utf8')

    def make_path(self, account, container=None, obj=None):
        """
        Returns a swift path for a request quoting and utf-8 encoding the path
        parts as need be.

        :param account: swift account
        :param container: container, defaults to None
        :param obj: object, defaults to None

        :raises ValueError: Is raised if obj is specified and container is
                            not.
        """

        path = '/v1/%s' % quote(account)
        if container:
            path += '/%s' % quote(container)

            if obj:
                path += '/%s' % quote(obj)
        elif obj:
            raise ValueError('Object specified without container')
        return path

    def _set_metadata(
            self, path, metadata, metadata_prefix='',
            acceptable_statuses=(2,)):
        """
        Sets metadata on path using metadata_prefix to set values in headers of
        POST request.

        :param path: Path to do POST on.
        :param metadata: Dict of metadata to set.
        :param metadata_prefix: Prefix used to set metadata values in headers
                                of requests, used to prefix keys in metadata
                                when setting metadata, defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        headers = {}
        for k, v in metadata.iteritems():
            if k.lower().startswith(metadata_prefix):
                headers[k] = v
            else:
                headers['%s%s' % (metadata_prefix, k)] = v
        self.make_request('POST', path, headers, acceptable_statuses)

    # account methods

    def iter_containers(
            self, account, marker='', end_marker='',
            acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Returns an iterator of containers dicts from an account.

        :param account: Account on which to do the container listing.
        :param marker: Prefix of first desired item, defaults to ''.
        :param end_marker: Last item returned will be 'less' than this,
                           defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account)
        return self._iter_items(path, marker, end_marker, acceptable_statuses)

    def get_account_info(
            self, account, acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Returns (container_count, object_count) for an account.

        :param account: Account on which to get the information.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account)
        resp = self.make_request('HEAD', path, {}, acceptable_statuses)
        if not resp.status_int // 100 == 2:
            return (0, 0)
        return (int(resp.headers.get('x-account-container-count', 0)),
                int(resp.headers.get('x-account-object-count', 0)))

    def get_account_metadata(
            self, account, metadata_prefix='', acceptable_statuses=(2,)):
        """
        Gets account metadata.

        :param account: Account on which to get the metadata.
        :param metadata_prefix: Used to filter values from the headers
                                returned.  Will strip that prefix from the
                                keys in the dict returned.  Defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :returns : Returns dict of account metadata.  Keys will be lowercase.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account)
        return self._get_metadata(path, metadata_prefix, acceptable_statuses)

    def set_account_metadata(
            self, account, metadata, metadata_prefix='',
            acceptable_statuses=(2,)):
        """
        Sets account metadata.  A call to this will add to the account
        metadata and not overwrite all of it with values in the metadata dict.
        To clear an account metadata value, pass an empty string as
        the value for the key in the metadata dict.

        :param account: Account on which to get the metadata.
        :param metadata: Dict of metadata to set.
        :param metadata_prefix: Prefix used to set metadata values in headers
                                of requests, used to prefix keys in metadata
                                when setting metadata, defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account)
        self._set_metadata(
            path, metadata, metadata_prefix, acceptable_statuses)

    # container methods

    def container_exists(self, account, container):
        """
        Checks to see if a container exists.

        :param account: The container's account.
        :param container: Container to check.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.

        :returns : True if container exists, false otherwise.
        """

        path = self.make_path(account, container)
        resp = self.make_request('HEAD', path, {}, (2, HTTP_NOT_FOUND))
        return not resp.status_int == HTTP_NOT_FOUND

    def create_container(
            self, account, container, headers=None, acceptable_statuses=(2,)):
        """
        Creates container.

        :param account: The container's account.
        :param container: Container to create.
        :param headers: Defaults to empty dict.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        headers = headers or {}
        path = self.make_path(account, container)
        self.make_request('PUT', path, headers, acceptable_statuses)

    def delete_container(
            self, account, container, acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Deletes a container.

        :param account: The container's account.
        :param container: Container to delete.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container)
        self.make_request('DELETE', path, {}, acceptable_statuses)

    def get_container_metadata(
            self, account, container, metadata_prefix='',
            acceptable_statuses=(2,)):
        """
        Gets container metadata.

        :param account: The container's account.
        :param container: Container to get metadata on.
        :param metadata_prefix: Used to filter values from the headers
                                returned.  Will strip that prefix from the
                                keys in the dict returned.  Defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :returns : Returns dict of container metadata.  Keys will be lowercase.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container)
        return self._get_metadata(path, metadata_prefix, acceptable_statuses)

    def iter_objects(
            self, account, container, marker='', end_marker='',
            acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Returns an iterator of object dicts from a container.

        :param account: The container's account.
        :param container: Container to iterate objects on.
        :param marker: Prefix of first desired item, defaults to ''.
        :param end_marker: Last item returned will be 'less' than this,
                           defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container)
        return self._iter_items(path, marker, end_marker, acceptable_statuses)

    def set_container_metadata(
            self, account, container, metadata, metadata_prefix='',
            acceptable_statuses=(2,)):
        """
        Sets container metadata.  A call to this will add to the container
        metadata and not overwrite all of it with values in the metadata dict.
        To clear a container metadata value, pass an empty string as the value
        for the key in the metadata dict.

        :param account: The container's account.
        :param container: Container to set metadata on.
        :param metadata: Dict of metadata to set.
        :param metadata_prefix: Prefix used to set metadata values in headers
                                of requests, used to prefix keys in metadata
                                when setting metadata, defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container)
        self._set_metadata(
            path, metadata, metadata_prefix, acceptable_statuses)

    # object methods

    def delete_object(
            self, account, container, obj,
            acceptable_statuses=(2, HTTP_NOT_FOUND)):
        """
        Deletes an object.

        :param account: The object's account.
        :param container: The object's container.
        :param obj: The object.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2, HTTP_NOT_FOUND).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container, obj)
        self.make_request('DELETE', path, {}, acceptable_statuses)

    def get_object_metadata(
            self, account, container, obj, metadata_prefix='',
            acceptable_statuses=(2,)):
        """
        Gets object metadata.

        :param account: The object's account.
        :param container: The object's container.
        :param obj: The object.
        :param metadata_prefix: Used to filter values from the headers
                                returned.  Will strip that prefix from the
                                keys in the dict returned.  Defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :returns : Dict of object metadata.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container, obj)
        return self._get_metadata(path, metadata_prefix, acceptable_statuses)

    def iter_object_lines(
            self, account, container, obj, headers=None,
            acceptable_statuses=(2,)):
        """
        Returns an iterator of object lines from an uncompressed or compressed
        text object.

        Uncompress object as it is read if the object's name ends with '.gz'.

        :param account: The object's account.
        :param container: The object's container.
        :param obj: The object.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        headers = headers or {}
        path = self.make_path(account, container, obj)
        resp = self.make_request('GET', path, headers, acceptable_statuses)
        if not resp.status_int // 100 == 2:
            return

        last_part = ''
        compressed = obj.endswith('.gz')
        # magic in the following zlib.decompressobj argument is courtesy of
        # Python decompressing gzip chunk-by-chunk
        # http://stackoverflow.com/questions/2423866
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        for chunk in resp.app_iter:
            if compressed:
                chunk = d.decompress(chunk)
            parts = chunk.split('\n')
            if len(parts) == 1:
                last_part = last_part + parts[0]
            else:
                parts[0] = last_part + parts[0]
                for part in parts[:-1]:
                    yield part
                last_part = parts[-1]
        if last_part:
            yield last_part

    def set_object_metadata(
            self, account, container, obj, metadata,
            metadata_prefix='', acceptable_statuses=(2,)):
        """
        Sets an object's metadata.  The object's metadata will be overwritten
        by the values in the metadata dict.

        :param account: The object's account.
        :param container: The object's container.
        :param obj: The object.
        :param metadata: Dict of metadata to set.
        :param metadata_prefix: Prefix used to set metadata values in headers
                                of requests, used to prefix keys in metadata
                                when setting metadata, defaults to ''.
        :param acceptable_statuses: List of status for valid responses,
                                    defaults to (2,).

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        path = self.make_path(account, container, obj)
        self._set_metadata(
            path, metadata, metadata_prefix, acceptable_statuses)

    def upload_object(
            self, fobj, account, container, obj, headers=None):
        """
        :param fobj: File object to read object's content from.
        :param account: The object's account.
        :param container: The object's container.
        :param obj: The object.
        :param headers: Headers to send with request, defaults ot empty dict.

        :raises UnexpectedResponse: Exception raised when requests fail
                                    to get a response with an acceptable status
        :raises Exception: Exception is raised when code fails in an
                           unexpected way.
        """

        headers = dict(headers or {})
        if 'Content-Length' not in headers:
            headers['Transfer-Encoding'] = 'chunked'
        path = self.make_path(account, container, obj)
        self.make_request('PUT', path, headers, (2,), fobj)


def get_auth(url, user, key, auth_version='1.0', **kwargs):
    if auth_version != '1.0':
        exit('ERROR: swiftclient missing, only auth v1.0 supported')
    req = urllib2.Request(url)
    req.add_header('X-Auth-User', user)
    req.add_header('X-Auth-Key', key)
    conn = urllib2.urlopen(req)
    headers = conn.info()
    return (
        headers.getheader('X-Storage-Url'),
        headers.getheader('X-Auth-Token'))


class SimpleClient(object):
    """
    Simple client that is used in bin/swift-dispersion-* and container sync
    """
    def __init__(self, url=None, token=None, starting_backoff=1,
                 max_backoff=5, retries=5):
        self.url = url
        self.token = token
        self.attempts = 0
        self.starting_backoff = starting_backoff
        self.max_backoff = max_backoff
        self.retries = retries

    def base_request(self, method, container=None, name=None, prefix=None,
                     headers=None, proxy=None, contents=None,
                     full_listing=None):
        # Common request method
        url = self.url

        if headers is None:
            headers = {}

        if self.token:
            headers['X-Auth-Token'] = self.token

        if container:
            url = '%s/%s' % (url.rstrip('/'), quote(container))

        if name:
            url = '%s/%s' % (url.rstrip('/'), quote(name))

        url += '?format=json'

        if prefix:
            url += '&prefix=%s' % prefix

        if proxy:
            proxy = urlparse.urlparse(proxy)
            proxy = urllib2.ProxyHandler({proxy.scheme: proxy.netloc})
            opener = urllib2.build_opener(proxy)
            urllib2.install_opener(opener)

        req = urllib2.Request(url, headers=headers, data=contents)
        req.get_method = lambda: method
        urllib2.urlopen(req)
        conn = urllib2.urlopen(req)
        body = conn.read()
        try:
            body_data = json.loads(body)
        except ValueError:
            body_data = None
        return [None, body_data]

    def retry_request(self, method, **kwargs):
        self.attempts = 0
        backoff = self.starting_backoff
        while self.attempts <= self.retries:
            self.attempts += 1
            try:
                return self.base_request(method, **kwargs)
            except (socket.error, httplib.HTTPException, urllib2.URLError):
                if self.attempts > self.retries:
                    raise
            sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    def get_account(self, *args, **kwargs):
        # Used in swift-dispersion-populate
        return self.retry_request('GET', **kwargs)

    def put_container(self, container, **kwargs):
        # Used in swift-dispersion-populate
        return self.retry_request('PUT', container=container, **kwargs)

    def get_container(self, container, **kwargs):
        # Used in swift-dispersion-populate
        return self.retry_request('GET', container=container, **kwargs)

    def put_object(self, container, name, contents, **kwargs):
        # Used in swift-dispersion-populate
        return self.retry_request('PUT', container=container, name=name,
                                  contents=contents.read(), **kwargs)


def put_object(url, **kwargs):
    """For usage with container sync """
    client = SimpleClient(url=url)
    client.retry_request('PUT', **kwargs)


def delete_object(url, **kwargs):
    """For usage with container sync """
    client = SimpleClient(url=url)
    client.retry_request('DELETE', **kwargs)
