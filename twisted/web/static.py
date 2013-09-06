# -*- test-case-name: twisted.web.test.test_static -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Static resources for L{twisted.web}.
"""
from __future__ import division

import os
import warnings
import urllib
import itertools
import cgi
import time
from functools import partial

from zope.interface import implementer, implements

from twisted.web import server
from twisted.web import resource
from twisted.web import http
from twisted.web import util
from twisted.web.util import redirectTo

from twisted.python import compat, components, filepath, log
from twisted.internet import abstract, interfaces
from twisted.persisted import styles
from twisted.python.util import InsensitiveDict
from twisted.python.runtime import platformType


dangerousPathError = resource.NoResource("Invalid request URL.")

def isDangerous(path):
    return path == '..' or '/' in path or os.sep in path


class Data(resource.Resource):
    """
    This is a static, in-memory resource.
    """

    def __init__(self, data, type):
        resource.Resource.__init__(self)
        self.data = data
        self.type = type


    def render_GET(self, request):
        request.setHeader("content-type", self.type)
        request.setHeader("content-length", str(len(self.data)))
        if request.method == "HEAD":
            return ''
        return self.data
    render_HEAD = render_GET



def addSlash(request):
    """
    Return the I{URL} of C{request} with an added trailing slash.

    @param request: The request containing the I{URL}
    @type request: L{http.Request}

    @return: The I{URL} with added trailing C{"/"}
    @rtype: L{bytes}
    """
    qs = ''
    qindex = request.uri.find('?')
    if qindex != -1:
        qs = request.uri[qindex:]

    return "http%s://%s%s/%s" % (
        request.isSecure() and 's' or '',
        request.getHeader("host"),
        (request.uri.split('?')[0]),
        qs)



def removeSlash(request):
    """
    Return the I{URL} of C{request} with trailing slashes removed.

    @param request: The request containing the I{URL}
    @type request: L{http.Request}

    @return: The I{URL} with trailing C{"/"} removed.
    @rtype: L{bytes}
    """
    scheme = 'http'
    if request.isSecure():
        scheme = 'https'

    parts = request.uri.split('?', 1)
    path = parts.pop(0).rstrip('/')
    qs = ''
    if parts:
        qs = '?' + parts[0]

    return "%s://%s%s%s" % (scheme, request.getHeader("host"), path, qs)



class Redirect(resource.Resource):
    """
    XXX: This is misnamed. It should be called AddSlashRedirect
    """
    def __init__(self, request):
        resource.Resource.__init__(self)
        self.url = addSlash(request)

    def render(self, request):
        return redirectTo(self.url, request)


class Registry(components.Componentized, styles.Versioned):
    """
    I am a Componentized object that will be made available to internal Twisted
    file-based dynamic web content such as .rpy and .epy scripts.
    """

    def __init__(self):
        components.Componentized.__init__(self)
        self._pathCache = {}

    persistenceVersion = 1

    def upgradeToVersion1(self):
        self._pathCache = {}

    def cachePath(self, path, rsrc):
        self._pathCache[path] = rsrc

    def getCachedPath(self, path):
        return self._pathCache.get(path)


def loadMimeTypes(mimetype_locations=['/etc/mime.types']):
    """
    Multiple file locations containing mime-types can be passed as a list.
    The files will be sourced in that order, overriding mime-types from the
    files sourced beforehand, but only if a new entry explicitly overrides
    the current entry.
    """
    import mimetypes
    # Grab Python's built-in mimetypes dictionary.
    contentTypes = mimetypes.types_map
    # Update Python's semi-erroneous dictionary with a few of the
    # usual suspects.
    contentTypes.update(
        {
            '.conf':  'text/plain',
            '.diff':  'text/plain',
            '.exe':   'application/x-executable',
            '.flac':  'audio/x-flac',
            '.java':  'text/plain',
            '.ogg':   'application/ogg',
            '.oz':    'text/x-oz',
            '.swf':   'application/x-shockwave-flash',
            '.tgz':   'application/x-gtar',
            '.wml':   'text/vnd.wap.wml',
            '.xul':   'application/vnd.mozilla.xul+xml',
            '.py':    'text/plain',
            '.patch': 'text/plain',
        }
    )
    # Users can override these mime-types by loading them out configuration
    # files (this defaults to ['/etc/mime.types']).
    for location in mimetype_locations:
        if os.path.exists(location):
            more = mimetypes.read_mime_types(location)
            if more is not None:
                contentTypes.update(more)

    return contentTypes

def getTypeAndEncoding(filename, types, encodings, defaultType):
    p, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext in encodings:
        enc = encodings[ext]
        ext = os.path.splitext(p)[1].lower()
    else:
        enc = None
    type = types.get(ext, defaultType)
    return type, enc



class File(resource.Resource, styles.Versioned, filepath.FilePath):
    """
    File is a resource that represents a plain non-interpreted file
    (although it can look for an extension like .rpy or .cgi and hand the
    file to a processor for interpretation if you wish). Its constructor
    takes a file path.

    Alternatively, you can give a directory path to the constructor. In this
    case the resource will represent that directory, and its children will
    be files underneath that directory. This provides access to an entire
    filesystem tree with a single Resource.

    If you map the URL 'http://server/FILE' to a resource created as
    File('/tmp'), then http://server/FILE/ will return an HTML-formatted
    listing of the /tmp/ directory, and http://server/FILE/foo/bar.html will
    return the contents of /tmp/foo/bar.html .

    @cvar childNotFound: L{Resource} used to render 404 Not Found error pages.
    """

    contentTypes = loadMimeTypes()

    contentEncodings = {
        ".gz" : "gzip",
        ".bz2": "bzip2"
        }

    processors = {}

    indexNames = ["index", "index.html", "index.htm", "index.rpy"]

    type = None

    ### Versioning

    persistenceVersion = 6

    def upgradeToVersion6(self):
        self.ignoredExts = []
        if self.allowExt:
            self.ignoreExt("*")
        del self.allowExt


    def upgradeToVersion5(self):
        if not isinstance(self.registry, Registry):
            self.registry = Registry()


    def upgradeToVersion4(self):
        if not hasattr(self, 'registry'):
            self.registry = {}


    def upgradeToVersion3(self):
        if not hasattr(self, 'allowExt'):
            self.allowExt = 0


    def upgradeToVersion2(self):
        self.defaultType = "text/html"


    def upgradeToVersion1(self):
        if hasattr(self, 'indexName'):
            self.indexNames = [self.indexName]
            del self.indexName


    def __init__(self, path, defaultType="text/html", ignoredExts=(), registry=None, allowExt=0):
        """
        Create a file with the given path.

        @param path: The filename of the file from which this L{File} will
            serve data.
        @type path: C{str}

        @param defaultType: A I{major/minor}-style MIME type specifier
            indicating the I{Content-Type} with which this L{File}'s data
            will be served if a MIME type cannot be determined based on
            C{path}'s extension.
        @type defaultType: C{str}

        @param ignoredExts: A sequence giving the extensions of paths in the
            filesystem which will be ignored for the purposes of child
            lookup.  For example, if C{ignoredExts} is C{(".bar",)} and
            C{path} is a directory containing a file named C{"foo.bar"}, a
            request for the C{"foo"} child of this resource will succeed
            with a L{File} pointing to C{"foo.bar"}.

        @param registry: The registry object being used to handle this
            request.  If C{None}, one will be created.
        @type registry: L{Registry}

        @param allowExt: Ignored parameter, only present for backwards
            compatibility.  Do not pass a value for this parameter.
        """
        resource.Resource.__init__(self)
        filepath.FilePath.__init__(self, path)
        self.defaultType = defaultType
        if ignoredExts in (0, 1) or allowExt:
            warnings.warn("ignoredExts should receive a list, not a boolean")
            if ignoredExts or allowExt:
                self.ignoredExts = ['*']
            else:
                self.ignoredExts = []
        else:
            self.ignoredExts = list(ignoredExts)
        self.registry = registry or Registry()


    def ignoreExt(self, ext):
        """Ignore the given extension.

        Serve file.ext if file is requested
        """
        self.ignoredExts.append(ext)

    childNotFound = resource.NoResource("File not found.")

    def directoryListing(self):
        return DirectoryLister(self.path,
                               self.listNames(),
                               self.contentTypes,
                               self.contentEncodings,
                               self.defaultType)


    def getChild(self, path, request):
        """
        If this L{File}'s path refers to a directory, return a L{File}
        referring to the file named C{path} in that directory.

        If C{path} is the empty string, return a L{DirectoryLister} instead.
        """
        self.restat(reraise=False)

        if not self.isdir():
            return self.childNotFound

        if path:
            try:
                fpath = self.child(path)
            except filepath.InsecurePath:
                return self.childNotFound
        else:
            fpath = self.childSearchPreauth(*self.indexNames)
            if fpath is None:
                return self.directoryListing()

        if not fpath.exists():
            fpath = fpath.siblingExtensionSearch(*self.ignoredExts)
            if fpath is None:
                return self.childNotFound

        if platformType == "win32":
            # don't want .RPY to be different than .rpy, since that would allow
            # source disclosure.
            processor = InsensitiveDict(self.processors).get(fpath.splitext()[1])
        else:
            processor = self.processors.get(fpath.splitext()[1])
        if processor:
            return resource.IResource(processor(fpath.path, self.registry))
        return self.createSimilarFile(fpath.path)


    # methods to allow subclasses to e.g. decrypt files on the fly:
    def openForReading(self):
        """Open a file and return it."""
        return self.open()


    def getFileSize(self):
        """Return file size."""
        return self.getsize()


    def _parseRangeHeader(self, range):
        """
        Parse the value of a Range header into (start, stop) pairs.

        In a given pair, either of start or stop can be None, signifying that
        no value was provided, but not both.

        @return: A list C{[(start, stop)]} of pairs of length at least one.

        @raise ValueError: if the header is syntactically invalid or if the
            Bytes-Unit is anything other than 'bytes'.
        """
        try:
            kind, value = range.split('=', 1)
        except ValueError:
            raise ValueError("Missing '=' separator")
        kind = kind.strip()
        if kind != 'bytes':
            raise ValueError("Unsupported Bytes-Unit: %r" % (kind,))
        unparsedRanges = filter(None, map(str.strip, value.split(',')))
        parsedRanges = []
        for byteRange in unparsedRanges:
            try:
                start, end = byteRange.split('-', 1)
            except ValueError:
                raise ValueError("Invalid Byte-Range: %r" % (byteRange,))
            if start:
                try:
                    start = int(start)
                except ValueError:
                    raise ValueError("Invalid Byte-Range: %r" % (byteRange,))
            else:
                start = None
            if end:
                try:
                    end = int(end)
                except ValueError:
                    raise ValueError("Invalid Byte-Range: %r" % (byteRange,))
            else:
                end = None
            if start is not None:
                if end is not None and start > end:
                    # Start must be less than or equal to end or it is invalid.
                    raise ValueError("Invalid Byte-Range: %r" % (byteRange,))
            elif end is None:
                # One or both of start and end must be specified.  Omitting
                # both is invalid.
                raise ValueError("Invalid Byte-Range: %r" % (byteRange,))
            parsedRanges.append((start, end))
        return parsedRanges


    def _rangeToOffsetAndSize(self, start, end):
        """
        Convert a start and end from a Range header to an offset and size.

        This method checks that the resulting range overlaps with the resource
        being served (and so has the value of C{getFileSize()} as an indirect
        input).

        Either but not both of start or end can be C{None}:

         - Omitted start means that the end value is actually a start value
           relative to the end of the resource.

         - Omitted end means the end of the resource should be the end of
           the range.

        End is interpreted as inclusive, as per RFC 2616.

        If this range doesn't overlap with any of this resource, C{(0, 0)} is
        returned, which is not otherwise a value return value.

        @param start: The start value from the header, or C{None} if one was
            not present.
        @param end: The end value from the header, or C{None} if one was not
            present.
        @return: C{(offset, size)} where offset is how far into this resource
            this resource the range begins and size is how long the range is,
            or C{(0, 0)} if the range does not overlap this resource.
        """
        size = self.getFileSize()
        if start is None:
            start = size - end
            end = size
        elif end is None:
            end = size
        elif end < size:
            end += 1
        elif end > size:
            end = size
        if start >= size:
            start = end = 0
        return start, (end - start)


    def _contentRange(self, offset, size):
        """
        Return a string suitable for the value of a Content-Range header for a
        range with the given offset and size.

        The offset and size are not sanity checked in any way.

        @param offset: How far into this resource the range begins.
        @param size: How long the range is.
        @return: The value as appropriate for the value of a Content-Range
            header.
        """
        return 'bytes %d-%d/%d' % (
            offset, offset + size - 1, self.getFileSize())


    def _doSingleRangeRequest(self, request, (start, end)):
        """
        Set up the response for Range headers that specify a single range.

        This method checks if the request is satisfiable and sets the response
        code and Content-Range header appropriately.  The return value
        indicates which part of the resource to return.

        @param request: The Request object.
        @param start: The start of the byte range as specified by the header.
        @param end: The end of the byte range as specified by the header.  At
            most one of C{start} and C{end} may be C{None}.
        @return: A 2-tuple of the offset and size of the range to return.
            offset == size == 0 indicates that the request is not satisfiable.
        """
        offset, size  = self._rangeToOffsetAndSize(start, end)
        if offset == size == 0:
            # This range doesn't overlap with any of this resource, so the
            # request is unsatisfiable.
            request.setResponseCode(http.REQUESTED_RANGE_NOT_SATISFIABLE)
            request.setHeader(
                'content-range', 'bytes */%d' % (self.getFileSize(),))
        else:
            request.setResponseCode(http.PARTIAL_CONTENT)
            request.setHeader(
                'content-range', self._contentRange(offset, size))
        return offset, size


    def _doMultipleRangeRequest(self, request, byteRanges):
        """
        Set up the response for Range headers that specify a single range.

        This method checks if the request is satisfiable and sets the response
        code and Content-Type and Content-Length headers appropriately.  The
        return value, which is a little complicated, indicates which parts of
        the resource to return and the boundaries that should separate the
        parts.

        In detail, the return value is a tuple rangeInfo C{rangeInfo} is a
        list of 3-tuples C{(partSeparator, partOffset, partSize)}.  The
        response to this request should be, for each element of C{rangeInfo},
        C{partSeparator} followed by C{partSize} bytes of the resource
        starting at C{partOffset}.  Each C{partSeparator} includes the
        MIME-style boundary and the part-specific Content-type and
        Content-range headers.  It is convenient to return the separator as a
        concrete string from this method, becasue this method needs to compute
        the number of bytes that will make up the response to be able to set
        the Content-Length header of the response accurately.

        @param request: The Request object.
        @param byteRanges: A list of C{(start, end)} values as specified by
            the header.  For each range, at most one of C{start} and C{end}
            may be C{None}.
        @return: See above.
        """
        matchingRangeFound = False
        rangeInfo = []
        contentLength = 0
        boundary = "%x%x" % (int(time.time()*1000000), os.getpid())
        if self.type:
            contentType = self.type
        else:
            contentType = 'bytes' # It's what Apache does...
        for start, end in byteRanges:
            partOffset, partSize = self._rangeToOffsetAndSize(start, end)
            if partOffset == partSize == 0:
                continue
            contentLength += partSize
            matchingRangeFound = True
            partContentRange = self._contentRange(partOffset, partSize)
            partSeparator = (
                "\r\n"
                "--%s\r\n"
                "Content-type: %s\r\n"
                "Content-range: %s\r\n"
                "\r\n") % (boundary, contentType, partContentRange)
            contentLength += len(partSeparator)
            rangeInfo.append((partSeparator, partOffset, partSize))
        if not matchingRangeFound:
            request.setResponseCode(http.REQUESTED_RANGE_NOT_SATISFIABLE)
            request.setHeader(
                'content-length', '0')
            request.setHeader(
                'content-range', 'bytes */%d' % (self.getFileSize(),))
            return [], ''
        finalBoundary = "\r\n--" + boundary + "--\r\n"
        rangeInfo.append((finalBoundary, 0, 0))
        request.setResponseCode(http.PARTIAL_CONTENT)
        request.setHeader(
            'content-type', 'multipart/byteranges; boundary="%s"' % (boundary,))
        request.setHeader(
            'content-length', contentLength + len(finalBoundary))
        return rangeInfo


    def _setContentHeaders(self, request, size=None):
        """
        Set the Content-length and Content-type headers for this request.

        This method is not appropriate for requests for multiple byte ranges;
        L{_doMultipleRangeRequest} will set these headers in that case.

        @param request: The L{Request} object.
        @param size: The size of the response.  If not specified, default to
            C{self.getFileSize()}.
        """
        if size is None:
            size = self.getFileSize()
        request.setHeader('content-length', str(size))
        if self.type:
            request.setHeader('content-type', self.type)
        if self.encoding:
            request.setHeader('content-encoding', self.encoding)


    def makeProducer(self, request, fileForReading):
        """
        Make a L{StaticProducer} that will produce the body of this response.

        This method will also set the response code and Content-* headers.

        @param request: The L{Request} object.
        @param fileForReading: The file object containing the resource.
        @return: A L{StaticProducer}.  Calling C{.start()} on this will begin
            producing the response.
        """
        byteRange = request.getHeader('range')
        if byteRange is None:
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return NoRangeStaticProducer(request, fileForReading)
        try:
            parsedRanges = self._parseRangeHeader(byteRange)
        except ValueError:
            log.msg("Ignoring malformed Range header %r" % (byteRange,))
            self._setContentHeaders(request)
            request.setResponseCode(http.OK)
            return NoRangeStaticProducer(request, fileForReading)

        if len(parsedRanges) == 1:
            offset, size = self._doSingleRangeRequest(
                request, parsedRanges[0])
            self._setContentHeaders(request, size)
            return SingleRangeStaticProducer(
                request, fileForReading, offset, size)
        else:
            rangeInfo = self._doMultipleRangeRequest(request, parsedRanges)
            return MultipleRangeStaticProducer(
                request, fileForReading, rangeInfo)


    def render_GET(self, request):
        """
        Begin sending the contents of this L{File} (or a subset of the
        contents, based on the 'range' header) to the given request.
        """
        self.restat(False)

        if self.type is None:
            self.type, self.encoding = getTypeAndEncoding(self.basename(),
                                                          self.contentTypes,
                                                          self.contentEncodings,
                                                          self.defaultType)

        if not self.exists():
            return self.childNotFound.render(request)

        if self.isdir():
            return self.redirect(request)

        request.setHeader('accept-ranges', 'bytes')

        try:
            fileForReading = self.openForReading()
        except IOError, e:
            import errno
            if e[0] == errno.EACCES:
                return resource.ForbiddenResource().render(request)
            else:
                raise

        if request.setLastModified(self.getmtime()) is http.CACHED:
            return ''


        producer = self.makeProducer(request, fileForReading)

        if request.method == 'HEAD':
            return ''

        producer.start()
        # and make sure the connection doesn't get closed
        return server.NOT_DONE_YET
    render_HEAD = render_GET


    def redirect(self, request):
        return redirectTo(addSlash(request), request)


    def listNames(self):
        if not self.isdir():
            return []
        directory = self.listdir()
        directory.sort()
        return directory

    def listEntities(self):
        return map(lambda fileName, self=self: self.createSimilarFile(os.path.join(self.path, fileName)), self.listNames())


    def createSimilarFile(self, path):
        f = self.__class__(path, self.defaultType, self.ignoredExts, self.registry)
        # refactoring by steps, here - constructor should almost certainly take these
        f.processors = self.processors
        f.indexNames = self.indexNames[:]
        f.childNotFound = self.childNotFound
        return f



class StaticProducer(object):
    """
    Superclass for classes that implement the business of producing.

    @ivar request: The L{IRequest} to write the contents of the file to.
    @ivar fileObject: The file the contents of which to write to the request.
    """

    implements(interfaces.IPullProducer)

    bufferSize = abstract.FileDescriptor.bufferSize


    def __init__(self, request, fileObject):
        """
        Initialize the instance.
        """
        self.request = request
        self.fileObject = fileObject


    def start(self):
        raise NotImplementedError(self.start)


    def resumeProducing(self):
        raise NotImplementedError(self.resumeProducing)


    def stopProducing(self):
        """
        Stop producing data.

        L{IPullProducer.stopProducing} is called when our consumer has died,
        and subclasses also call this method when they are done producing
        data.
        """
        self.fileObject.close()
        self.request = None



class NoRangeStaticProducer(StaticProducer):
    """
    A L{StaticProducer} that writes the entire file to the request.
    """

    def start(self):
        self.request.registerProducer(self, False)


    def resumeProducing(self):
        if not self.request:
            return
        data = self.fileObject.read(self.bufferSize)
        if data:
            # this .write will spin the reactor, calling .doWrite and then
            # .resumeProducing again, so be prepared for a re-entrant call
            self.request.write(data)
        else:
            self.request.unregisterProducer()
            self.request.finish()
            self.stopProducing()



class SingleRangeStaticProducer(StaticProducer):
    """
    A L{StaticProducer} that writes a single chunk of a file to the request.
    """

    def __init__(self, request, fileObject, offset, size):
        """
        Initialize the instance.

        @param request: See L{StaticProducer}.
        @param fileObject: See L{StaticProducer}.
        @param offset: The offset into the file of the chunk to be written.
        @param size: The size of the chunk to write.
        """
        StaticProducer.__init__(self, request, fileObject)
        self.offset = offset
        self.size = size


    def start(self):
        self.fileObject.seek(self.offset)
        self.bytesWritten = 0
        self.request.registerProducer(self, 0)


    def resumeProducing(self):
        if not self.request:
            return
        data = self.fileObject.read(
            min(self.bufferSize, self.size - self.bytesWritten))
        if data:
            self.bytesWritten += len(data)
            # this .write will spin the reactor, calling .doWrite and then
            # .resumeProducing again, so be prepared for a re-entrant call
            self.request.write(data)
        if self.request and self.bytesWritten == self.size:
            self.request.unregisterProducer()
            self.request.finish()
            self.stopProducing()



class MultipleRangeStaticProducer(StaticProducer):
    """
    A L{StaticProducer} that writes several chunks of a file to the request.
    """

    def __init__(self, request, fileObject, rangeInfo):
        """
        Initialize the instance.

        @param request: See L{StaticProducer}.
        @param fileObject: See L{StaticProducer}.
        @param rangeInfo: A list of tuples C{[(boundary, offset, size)]}
            where:
             - C{boundary} will be written to the request first.
             - C{offset} the offset into the file of chunk to write.
             - C{size} the size of the chunk to write.
        """
        StaticProducer.__init__(self, request, fileObject)
        self.rangeInfo = rangeInfo


    def start(self):
        self.rangeIter = iter(self.rangeInfo)
        self._nextRange()
        self.request.registerProducer(self, 0)


    def _nextRange(self):
        self.partBoundary, partOffset, self._partSize = self.rangeIter.next()
        self._partBytesWritten = 0
        self.fileObject.seek(partOffset)


    def resumeProducing(self):
        if not self.request:
            return
        data = []
        dataLength = 0
        done = False
        while dataLength < self.bufferSize:
            if self.partBoundary:
                dataLength += len(self.partBoundary)
                data.append(self.partBoundary)
                self.partBoundary = None
            p = self.fileObject.read(
                min(self.bufferSize - dataLength,
                    self._partSize - self._partBytesWritten))
            self._partBytesWritten += len(p)
            dataLength += len(p)
            data.append(p)
            if self.request and self._partBytesWritten == self._partSize:
                try:
                    self._nextRange()
                except StopIteration:
                    done = True
                    break
        self.request.write(''.join(data))
        if done:
            self.request.unregisterProducer()
            self.request.finish()
            self.request = None



class ASISProcessor(resource.Resource):
    """
    Serve files exactly as responses without generating a status-line or any
    headers.  Inspired by Apache's mod_asis.
    """

    def __init__(self, path, registry=None):
        resource.Resource.__init__(self)
        self.path = path
        self.registry = registry or Registry()


    def render(self, request):
        request.startedWriting = 1
        res = File(self.path, registry=self.registry)
        return res.render(request)



def formatFileSize(size):
    """
    Format the given file size in bytes to human readable format.
    """
    if size < 1024:
        return '%iB' % size
    elif size < (1024 ** 2):
        return '%iK' % (size / 1024)
    elif size < (1024 ** 3):
        return '%iM' % (size / (1024 ** 2))
    else:
        return '%iG' % (size / (1024 ** 3))



class DirectoryLister(resource.Resource):
    """
    Print the content of a directory.

    @ivar template: page template used to render the content of the directory.
        It must contain the format keys B{header} and B{tableContent}.
    @type template: C{str}

    @ivar linePattern: template used to render one line in the listing table.
        It must contain the format keys B{class}, B{href}, B{text}, B{size},
        B{type} and B{encoding}.
    @type linePattern: C{str}

    @ivar contentEncodings: a mapping of extensions to encoding types.
    @type contentEncodings: C{dict}

    @ivar defaultType: default type used when no mimetype is detected.
    @type defaultType: C{str}

    @ivar dirs: filtered content of C{path}, if the whole content should not be
        displayed (default to C{None}, which means the actual content of
        C{path} is printed).
    @type dirs: C{NoneType} or C{list}

    @ivar path: directory which content should be listed.
    @type path: C{str}
    """

    template = """<html>
<head>
<title>%(header)s</title>
<style>
.even-dir { background-color: #efe0ef }
.even { background-color: #eee }
.odd-dir {background-color: #f0d0ef }
.odd { background-color: #dedede }
.icon { text-align: center }
.listing {
    margin-left: auto;
    margin-right: auto;
    width: 50%%;
    padding: 0.1em;
    }

body { border: 0; padding: 0; margin: 0; background-color: #efefef; }
h1 {padding: 0.1em; background-color: #777; color: white; border-bottom: thin white dashed;}

</style>
</head>

<body>
<h1>%(header)s</h1>

<table>
    <thead>
        <tr>
            <th>Filename</th>
            <th>Size</th>
            <th>Content type</th>
            <th>Content encoding</th>
        </tr>
    </thead>
    <tbody>
%(tableContent)s
    </tbody>
</table>

</body>
</html>
"""

    linePattern = """<tr class="%(class)s">
    <td><a href="%(href)s">%(text)s</a></td>
    <td>%(size)s</td>
    <td>%(type)s</td>
    <td>%(encoding)s</td>
</tr>
"""

    _filePathFactory = filepath.FilePath

    def __init__(self, pathname, dirs=None,
                 contentTypes=File.contentTypes,
                 contentEncodings=File.contentEncodings,
                 defaultType='text/html'):
        resource.Resource.__init__(self)
        self.contentTypes = contentTypes
        self.contentEncodings = contentEncodings
        self.defaultType = defaultType
        # dirs allows usage of the File to specify what gets listed
        self.dirs = dirs

        if filepath.IFilePath.providedBy(pathname):
            self.path = pathname.path
            self._path = pathname
        else:
            self.path = pathname
            self._path = self._filePathFactory(pathname)


    def _getFilesAndDirectories(self, directory):
        """
        Helper returning files and directories in given directory listing, with
        attributes to be used to build a table content with
        C{self.linePattern}.

        @return: tuple of (directories, files)
        @rtype: C{tuple} of C{list}
        """
        files = []
        dirs = []
        for path in directory:
            url = urllib.quote(path.basename())
            escapedPath = cgi.escape(path.basename())
            if path.isdir():
                url = url + '/'
                dirs.append({'text': escapedPath + "/", 'href': url,
                             'size': '', 'type': '[Directory]',
                             'encoding': ''})
            else:
                mimetype, encoding = getTypeAndEncoding(path.path, self.contentTypes,
                                                        self.contentEncodings,
                                                        self.defaultType)
                try:
                    size = path.getsize()
                except OSError:
                    continue
                files.append({
                    'text': escapedPath, "href": url,
                    'type': '[%s]' % mimetype,
                    'encoding': (encoding and '[%s]' % encoding or ''),
                    'size': formatFileSize(size)})
        return dirs, files


    def _buildTableContent(self, elements):
        """
        Build a table content using C{self.linePattern} and giving elements odd
        and even classes.
        """
        tableContent = []
        rowClasses = itertools.cycle(['odd', 'even'])
        for element, rowClass in zip(elements, rowClasses):
            element["class"] = rowClass
            tableContent.append(self.linePattern % element)
        return tableContent


    def render(self, request):
        """
        Render a listing of the content of C{self._path}.
        """
        request.setHeader("content-type", "text/html; charset=utf-8")
        if self.dirs is None:
            directory = sorted(self._path.children())
        else:
            directory = [self._path.child(d) for d in self.dirs]

        dirs, files = self._getFilesAndDirectories(directory)

        tableContent = "".join(self._buildTableContent(dirs + files))

        header = "Directory listing for %s" % (
            cgi.escape(urllib.unquote(request.uri)),)

        return (self.template % {"header": header, "tableContent": tableContent}).encode('utf-8')


    def __repr__(self):
        return '<DirectoryLister of %r>' % self._path.path

    __str__ = __repr__



@implementer(resource.IResource)
class Path(object):
    """
    A L{IFilePath} traversal resource.

    L{Path} handles URL traversal for locating files and
    sub-directories.

    If the target is found, L{Path} dispatches to a
    C{filePathRenderer} or C{directoryPathRenderer} depending on the
    target type.

    If the target is not found, L{Path} dispatches to a separate
    C{pathNotFoundRenderer}.
    """
    isLeaf = False

    def __init__(self, filePath=None, fileRenderer=None,
                 directoryRenderer=None, pathNotFoundRenderer=None,
                 pathFactory=None, redirectRenderer=None):
        """
        The constructor accepts various factory arguments partly for ease
        of testing, but also for "composability" (XXX: as I understand it).

        You ...

        @param filePath: The L{FilePath} instance to be traversed. Default
            to current working directory (".")

        @param fileRenderer: An L{IResource} class (or factory
            function) which returns a resource for rendering
            files. Passed C{filePath}. Default
            L{FilePathRenderer}. Pass in L{File} to get I{range
            request} handling.

        @param directoryRenderer: An L{IResource} class (or factory
            function) which returns a resource for rendering
            directories. Passed C{filePath}. Default
            L{DirectoryPathRenderer}.

        @param pathNotFoundRenderer: An L{IResource} class (or factory
            function) which returns a resource for handling and
            rendering I{FILE NOT FOUND} responses. Passed
            C{filePath}. Default L{resource.NotFound}.

        @param pathFactory: A class (or factory function) which
            returns a new L{Path} instance. Called at each round of
            traversal. Default L{Path}. Passed the child C{filePath}
            plus all the keyword arguments provided to the parent
            L{Path}.
        """
        if filePath is None:
            filePath = filepath.FilePath('.')
        else:
            if not filePath.exists():
                raise IOError(2, 'No such file or directory', filePath.path)
        self._filePath = filePath

        if fileRenderer is None:
            fileRenderer = FilePathRenderer
        self._fileRenderer = fileRenderer

        if directoryRenderer is None:
            directoryRenderer = DirectoryPathRenderer
        self._directoryRenderer = directoryRenderer

        if pathNotFoundRenderer is None:
            pathNotFoundRenderer = resource.NoResource
        self._pathNotFoundRenderer = pathNotFoundRenderer

        if pathFactory is None:
            pathFactory = self.__class__

        # XXX: File.createSimilarFile Is this is equivalent? Perhaps
        # it should be a separate method so it can be overridden in
        # subclasses. Forcing the same arguments may cause problems
        # for anyone wanting to subclass. See https://tm.tl/3762.
        self._pathFactory = partial(
            pathFactory,
            fileRenderer=fileRenderer,
            directoryRenderer=directoryRenderer,
            pathNotFoundRenderer=pathNotFoundRenderer,
            pathFactory=pathFactory,
            redirectRenderer=redirectRenderer)

        if redirectRenderer is None:
            redirectRenderer = util.Redirect
        self._redirectRenderer = redirectRenderer


    def getChildWithDefault(self, name, request):
        """
        Handle traversal of this path.

        In the special case of C{name == ''} means that the request
        URL has a trailing slash and is the root path eg
        http://example.com/

        If we are handling a name and the next URL segment is C{''} we
        are dealing with a non-root URL that has a trailing slash. In
        this case if the requested name is for a directory then return
        a C{directoryRenderer} otherwise return a
        C{pathNotFoundRenderer}.

        If we are handling a non-empty name and there are no remaining
        URL segments it means there is no trailing slash. In this case
        if the requested name is for a directory, we return a
        C{redirectRenderer} which adds a trailing slash. This allows
        HTML links relative to the directory to work as expected.  If
        the requested name is for a file, we return a C{fileRenderer}.

        If the requested name is an intermediate segment of the URL we
        return the named child path wrapped in a new instance of this
        class for further traversal and rendering.

        If the requested name is for a file which does not exist we
        call C{pathNotFoundRenderer} with the requested filePath and
        return it for further rendering.
        """
        # Specialcase handling for root slash
        if name == '':
#            import pdb; pdb.set_trace()
            if self._filePath.isdir():
                return self._directoryRenderer(self._filePath)
            else:
                return self._fileRenderer(self._filePath)

        # XXX: File.getChild Is this needed?
        # self.restat(reraise=False)
        try:
            child = self._filePath.child(name)
        except filepath.InsecurePath:
            return self._pathNotFoundRenderer()

        if child.exists():
            # XXX: File.getChild This should probably be part of (or a wrapper around) FilePathRenderer
            # if platformType == "win32":
            #     # don't want .RPY to be different than .rpy, since that would allow
            #     # source disclosure.
            #     processor = InsensitiveDict(self.processors).get(fpath.splitext()[1])
            # else:
            #     processor = self.processors.get(fpath.splitext()[1])
            # if processor:
            #     return resource.IResource(processor(fpath.path, self.registry))
            if request.postpath:
                # We are handling an intermediate segment
                if request.postpath[0] == '':
                    # We are handling the penultimate segment before a
                    # trailing slash
                    if child.isdir():
                        # Trailing slash is only expected for directories
                        return self._directoryRenderer(child)
                    else:
                        # Trailing slash not expected on files
                        return self._pathNotFoundRenderer()
                else:
                    # The next segment is a name of a file or directory
                    return self._pathFactory(child)
            else:
#                import pdb;pdb.set_trace()
                # We are handling the final segment
                if child.isdir():
                    # Directory without trailing slash - redirect
                    return self._redirectRenderer(addSlash(request))
                else:
                    return self._fileRenderer(child)
        else:
            # XXX: File.getChild Where should this go? Here or in the pathNotFoundRenderer?
            # if not fpath.exists():
            #     fpath = fpath.siblingExtensionSearch(*self.ignoredExts)
            #     if fpath is None:
            #         return self.childNotFound
            return self._pathNotFoundRenderer()


    def putChild(self, path, child):
        """
        XXX: Consider inheriting from Resource to get the default putChild
        behaviour.  Would then also need to rename getChildWithDefault
        to getChild I think.
        """


    def render(self, request):
        """
        Never called
        """



@implementer(resource.IResource)
class FilePathRenderer(object):
    """
    An {IResource} for rendering files.

    @param filePath: The I{IFilePath} provider to be rendered.
    """
    def __init__(self, filePath):
        self._filePath = filePath


    def getChildWithDefault(self, name, request):
        """
        """
        pass


    def putChild(self, path, child):
        pass


    def render(self, request):
        # XXX: File.render_GET Is this necessary?
        # self.restat(False)

        # XXX: File.render_GET
        # if self.type is None:
        #     self.type, self.encoding = getTypeAndEncoding(self.basename(),
        #                                                   self.contentTypes,
        #                                                   self.contentEncodings,
        #                                                   self.defaultType)


        # request.setHeader('accept-ranges', 'bytes')

        # try:
        #     fileForReading = self.openForReading()
        # except IOError, e:
        #     import errno
        #     if e[0] == errno.EACCES:
        #         return resource.ForbiddenResource().render(request)
        #     else:
        #         raise

        # if request.setLastModified(self.getmtime()) is http.CACHED:
        #     return ''


        # producer = self.makeProducer(request, fileForReading)

        # if request.method == 'HEAD':
        #     return ''

        # producer.start()
        # # and make sure the connection doesn't get closed
        # return server.NOT_DONE_YET


        return self._filePath.getContent()



@implementer(resource.IResource)
class DirectoryPathRenderer(object):
    """
    An {IResource} for rendering directories.

    @param filePath: The I{FilePath} object to be rendered. Must be a
        directory.

    @param indexNames: A L{list} of file names which will be
        considered to be directory index files. If a file with one of
        these names is found in C{filePath}, it will be rendered and
        returned to the client using C{fileRenderer}.

    @param fileRenderer: An L{IResource} class (or factory function)
        which returns a resource for rendering files. Default
        L{FilePathRenderer}. (Pass in L{File} to get full range request
        handling.)

    @param pathNotFoundRenderer: An L{IResource} class (or factory
        function) which returns a resource for handling and rendering
        responses when an index file is not found. Passed
        C{filePath}. Default L{resource.ForbiddenResource}. (Pass
        L{DirectoryLister} to generate a directory listing instead of
        generating an error.)
    """
    isLeaf = True

    _allowedMethods = (b'GET', b'HEAD')
    _indexNames = ("index", "index.html", "index.htm", "index.rpy")

    def __init__(self, filePath=None, indexNames=None, fileRenderer=None,
                 pathNotFoundRenderer=None):
        """
        """
        if filePath is None:
            filePath = filepath.FilePath('.')
        else:
            if not filePath.isdir():
                raise ValueError(
                    'Expected a path to a directory. Found %r' % (filePath,))
        self._filePath = filePath

        if indexNames is not None:
            self._indexNames = indexNames

        if fileRenderer is None:
            fileRenderer = FilePathRenderer
        self._fileRenderer = fileRenderer

        if pathNotFoundRenderer is None:
            pathNotFoundRenderer = resource.ForbiddenResource
        self._pathNotFoundRenderer = pathNotFoundRenderer


    def getChildWithDefault(self, name, request):
        """
        Return a File or Directory resource.

        Only called if C{self.isLeaf} is C{False}.
        """
        pass


    def putChild(self, path, child):
        pass


    def render(self, request):
        """
        Check that the C{request} uses a supported I{HTTP} I{method} and
        raise L{resource.UnsupportedMethod} if not.

        Check for a child with a name in C{indexNames}, wrap it in
        C{fileRenderer} and return the result of its C{render} method.

        If an index child is not found, instantiate
        C{pathNotFoundRenderer} and return the result of its C{render}
        method.
        """
        if compat.nativeString(request.method) not in self._allowedMethods:
            raise resource.UnsupportedMethod(
                allowedMethods=self._allowedMethods)

        for index in self._indexNames:
            child = self._filePath.child(index)
            if child.exists():
                return self._fileRenderer(child).render(request)

        return self._pathNotFoundRenderer(self._filePath).render(request)



_zipDemoConstructor = partial(
    Path,
    directoryRenderer=partial(DirectoryPathRenderer,
                              pathNotFoundRenderer=DirectoryLister),
)



from twisted.python.zippath import ZipArchive
def zipDemo(config):
    """
    A demonstration of static.Path with a ZipFile IFilePath provider
    and directoryRenderer customised to serve directory listings when
    an index file is not found.  eg:

        twistd -n web --class=twisted.web.static.zipDemo \
                      --path=/home/richard/Downloads/apidocs.zip
    """
    return _zipDemoConstructor(filePath=ZipArchive(config['path']))



_pathDemoConstructor = partial(
    Path,
    fileRenderer=lambda f: File(f.path),
    directoryRenderer=partial(DirectoryPathRenderer,
                              pathNotFoundRenderer=DirectoryLister,
                              indexNames=["index.html"]))



def pathDemo(config):
    """
    A demonstration of static.Path which uses the original static.File
    to render files and a non-listing DirectoryPathRenderer with
    custom index names for directories. eg

        twistd -n web --class=twisted.web.static.pathDemo \
                      --path=/home/richard/Downloads
    """
    return _pathDemoConstructor(
        filePath=filepath.FilePath(config['path']))



class ZipPathExplorer(components.proxyForInterface(filepath.IFilePath)):
    """
    An IFilePath which treats zip files as if they are folders.
    """
    @property
    def path(self):
        return self.original.path

    def child(self, path):
        path = self.original.child(path)
        if path.splitext()[1] == '.zip':
            return ZipArchive(path.path)

        return self.__class__(path)



def zipPathExplorer(config):
    """
    Serve a filesystem directory and automatically open and serve the
    contents of the zip files within. eg

        twistd -n web --class=twisted.web.static.zipPathExplorer \
                      --path=/home/richard/Downloads
    """
    return _zipDemoConstructor(
        filePath=ZipPathExplorer(filepath.FilePath(config['path'])),
    )
