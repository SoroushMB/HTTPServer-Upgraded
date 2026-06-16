__version__ = "0.6"

__all__ = [
    "HTTPServer", "ThreadingHTTPServer",
    "HTTPSServer", "ThreadingHTTPSServer",
    "BaseHTTPRequestHandler", "SimpleHTTPRequestHandler",
    "CGIHTTPRequestHandler",
]

import copy
import datetime
import email.utils
import html
import http.client
import io
import itertools
import gzip
import json
import mimetypes
import os
import posixpath
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import time
import urllib.parse

from http import HTTPStatus


# Default error message template
DEFAULT_ERROR_MESSAGE = """\
<!DOCTYPE HTML>
<html lang="en">
    <head>
        <meta charset="utf-8">
        <style type="text/css">
            :root {
                color-scheme: light dark;
            }
        </style>
        <title>Error response</title>
    </head>
    <body>
        <h1>Error response</h1>
        <p>Error code: %(code)d</p>
        <p>Message: %(message)s.</p>
        <p>Error code explanation: %(code)s - %(explain)s.</p>
    </body>
</html>
"""

DEFAULT_ERROR_CONTENT_TYPE = "text/html;charset=utf-8"

# Data larger than this will be read in chunks, to prevent extreme
# overallocation.
_MIN_READ_BUF_SIZE = 1 << 20

class HTTPServer(socketserver.TCPServer):

    allow_reuse_address = True    # Seems to make sense in testing environment
    allow_reuse_port = False

    def server_bind(self):
        """Override server_bind to store the server name."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = socket.getfqdn(host)
        self.server_port = port


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class HTTPSServer(HTTPServer):
    def __init__(self, server_address, RequestHandlerClass,
                 bind_and_activate=True, *, certfile, keyfile=None,
                 password=None, alpn_protocols=None):
        try:
            import ssl
        except ImportError:
            raise RuntimeError("SSL module is missing; "
                               "HTTPS support is unavailable")

        self.ssl = ssl
        self.certfile = certfile
        self.keyfile = keyfile
        self.password = password
        # Support by default HTTP/1.1
        self.alpn_protocols = (
            ["http/1.1"] if alpn_protocols is None else alpn_protocols
        )

        super().__init__(server_address,
                         RequestHandlerClass,
                         bind_and_activate)

    def server_activate(self):
        """Wrap the socket in SSLSocket."""
        super().server_activate()
        context = self._create_context()
        self.socket = context.wrap_socket(self.socket, server_side=True)

    def _create_context(self):
        """Create a secure SSL context."""
        context = self.ssl.create_default_context(self.ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.certfile, self.keyfile, self.password)
        context.set_alpn_protocols(self.alpn_protocols)
        return context


class ThreadingHTTPSServer(socketserver.ThreadingMixIn, HTTPSServer):
    daemon_threads = True


class BaseHTTPRequestHandler(socketserver.StreamRequestHandler):

    """HTTP request handler base class.

    The following explanation of HTTP serves to guide you through the
    code as well as to expose any misunderstandings I may have about
    HTTP (so you don't need to read the code to figure out I'm wrong
    :-).

    HTTP (HyperText Transfer Protocol) is an extensible protocol on
    top of a reliable stream transport (e.g. TCP/IP).  The protocol
    recognizes three parts to a request:

    1. One line identifying the request type and path
    2. An optional set of RFC-822-style headers
    3. An optional data part

    The headers and data are separated by a blank line.

    The first line of the request has the form

    <command> <path> <version>

    where <command> is a (case-sensitive) keyword such as GET or POST,
    <path> is a string containing path information for the request,
    and <version> should be the string "HTTP/1.0" or "HTTP/1.1".
    <path> is encoded using the URL encoding scheme (using %xx to signify
    the ASCII character with hex code xx).

    The specification specifies that lines are separated by CRLF but
    for compatibility with the widest range of clients recommends
    servers also handle LF.  Similarly, whitespace in the request line
    is treated sensibly (allowing multiple spaces between components
    and allowing trailing whitespace).

    Similarly, for output, lines ought to be separated by CRLF pairs
    but most clients grok LF characters just fine.

    If the first line of the request has the form

    <command> <path>

    (i.e. <version> is left out) then this is assumed to be an HTTP
    0.9 request; this form has no optional headers and data part and
    the reply consists of just the data.

    The reply form of the HTTP 1.x protocol again has three parts:

    1. One line giving the response code
    2. An optional set of RFC-822-style headers
    3. The data

    Again, the headers and data are separated by a blank line.

    The response code line has the form

    <version> <responsecode> <responsestring>

    where <version> is the protocol version ("HTTP/1.0" or "HTTP/1.1"),
    <responsecode> is a 3-digit response code indicating success or
    failure of the request, and <responsestring> is an optional
    human-readable string explaining what the response code means.

    This server parses the request and the headers, and then calls a
    function specific to the request type (<command>).  Specifically,
    a request SPAM will be handled by a method do_SPAM().  If no
    such method exists the server sends an error response to the
    client.  If it exists, it is called with no arguments:

    do_SPAM()

    Note that the request name is case sensitive (i.e. SPAM and spam
    are different requests).

    The various request details are stored in instance variables:

    - client_address is the client IP address in the form (host,
    port);

    - command, path and version are the broken-down request line;

    - headers is an instance of email.message.Message (or a derived
    class) containing the header information;

    - rfile is a file object open for reading positioned at the
    start of the optional input data part;

    - wfile is a file object open for writing.

    IT IS IMPORTANT TO ADHERE TO THE PROTOCOL FOR WRITING!

    The first thing to be written must be the response line.  Then
    follow 0 or more header lines, then a blank line, and then the
    actual data (if any).  The meaning of the header lines depends on
    the command executed by the server; in most cases, when data is
    returned, there should be at least one header line of the form

    Content-type: <type>/<subtype>

    where <type> and <subtype> should be registered MIME types,
    e.g. "text/html" or "text/plain".

    """

    # The Python system version, truncated to its first component.
    sys_version = "Python/" + sys.version.split()[0]

    # The server software version.  You may want to override this.
    # The format is multiple whitespace-separated strings,
    # where each string is of the form name[/version].
    server_version = "BaseHTTP/" + __version__

    error_message_format = DEFAULT_ERROR_MESSAGE
    error_content_type = DEFAULT_ERROR_CONTENT_TYPE

    # The default request version.  This only affects responses up until
    # the point where the request line is parsed, so it mainly decides what
    # the client gets back when sending a malformed request line.
    # Most web servers default to HTTP 0.9, i.e. don't send a status line.
    default_request_version = "HTTP/0.9"

    def parse_request(self):
        """Parse a request (internal).

        The request should be stored in self.raw_requestline; the results
        are in self.command, self.path, self.request_version and
        self.headers.

        Return True for success, False for failure; on failure, any relevant
        error response has already been sent back.

        """
        is_http_0_9 = False
        self.command = None  # set in case of error on the first line
        self.request_version = version = self.default_request_version
        self.close_connection = True
        requestline = str(self.raw_requestline, 'iso-8859-1')
        requestline = requestline.rstrip('\r\n')
        self.requestline = requestline
        words = requestline.split()
        if len(words) == 0:
            return False

        if len(words) >= 3:  # Enough to determine protocol version
            version = words[-1]
            try:
                if not version.startswith('HTTP/'):
                    raise ValueError
                base_version_number = version.split('/', 1)[1]
                version_number = base_version_number.split(".")
                # RFC 2145 section 3.1 says there can be only one "." and
                #   - major and minor numbers MUST be treated as
                #      separate integers;
                #   - HTTP/2.4 is a lower version than HTTP/2.13, which in
                #      turn is lower than HTTP/12.3;
                #   - Leading zeros MUST be ignored by recipients.
                if len(version_number) != 2:
                    raise ValueError
                if any(not component.isdigit() for component in version_number):
                    raise ValueError("non digit in http version")
                if any(len(component) > 10 for component in version_number):
                    raise ValueError("unreasonable length http version")
                version_number = int(version_number[0]), int(version_number[1])
            except (ValueError, IndexError):
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad request version (%r)" % version)
                return False
            if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            if version_number >= (2, 0):
                self.send_error(
                    HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                    "Invalid HTTP version (%s)" % base_version_number)
                return False
            self.request_version = version

        if not 2 <= len(words) <= 3:
            self.send_error(
                HTTPStatus.BAD_REQUEST,
                "Bad request syntax (%r)" % requestline)
            return False
        command, path = words[:2]
        if len(words) == 2:
            self.close_connection = True
            if command != 'GET':
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad HTTP/0.9 request type (%r)" % command)
                return False
            is_http_0_9 = True
        self.command, self.path = command, path

        # gh-87389: The purpose of replacing '//' with '/' is to protect
        # against open redirect attacks possibly triggered if the path starts
        # with '//' because http clients treat //path as an absolute URI
        # without scheme (similar to http://path) rather than a path.
        if self.path.startswith('//'):
            self.path = '/' + self.path.lstrip('/')  # Reduce to a single /

        # For HTTP/0.9, headers are not expected at all.
        if is_http_0_9:
            self.headers = {}
            return True

        # Examine the headers and look for a Connection directive.
        try:
            self.headers = http.client.parse_headers(self.rfile,
                                                     _class=self.MessageClass)
        except http.client.LineTooLong as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Line too long",
                str(err))
            return False
        except http.client.HTTPException as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Too many headers",
                str(err)
            )
            return False

        conntype = self.headers.get('Connection', "")
        if conntype.lower() == 'close':
            self.close_connection = True
        elif (conntype.lower() == 'keep-alive' and
              self.protocol_version >= "HTTP/1.1"):
            self.close_connection = False
        # Examine the headers and look for an Expect directive
        expect = self.headers.get('Expect', "")
        if (expect.lower() == "100-continue" and
                self.protocol_version >= "HTTP/1.1" and
                self.request_version >= "HTTP/1.1"):
            if not self.handle_expect_100():
                return False
        return True

    def handle_expect_100(self):
        """Decide what to do with an "Expect: 100-continue" header.

        If the client is expecting a 100 Continue response, we must
        respond with either a 100 Continue or a final response before
        waiting for the request body. The default is to always respond
        with a 100 Continue. You can behave differently (for example,
        reject unauthorized requests) by overriding this method.

        This method should either return True (possibly after sending
        a 100 Continue response) or send an error response and return
        False.

        """
        self.send_response_only(HTTPStatus.CONTINUE)
        self.end_headers()
        return True

    def handle_one_request(self):
        """Handle a single HTTP request.

        You normally don't need to override this method; see the class
        __doc__ string for information on how to handle specific HTTP
        commands such as GET and POST.

        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                # An error code has been sent, just exit
                return
            mname = 'do_' + self.command
            if not hasattr(self, mname):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "Unsupported method (%r)" % self.command)
                return
            method = getattr(self, mname)
            method()
            self.wfile.flush() #actually send the response if not already done.
        except TimeoutError as e:
            #a read or a write timed out.  Discard this connection
            self.log_error("Request timed out: %r", e)
            self.close_connection = True
            return

    def handle(self):
        """Handle multiple requests if necessary."""
        self.close_connection = True

        self.handle_one_request()
        while not self.close_connection:
            self.handle_one_request()

    def send_error(self, code, message=None, explain=None):
        """Send and log an error reply.

        Arguments are
        * code:    an HTTP error code
                   3 digits
        * message: a simple optional 1 line reason phrase.
                   *( HTAB / SP / VCHAR / %x80-FF )
                   defaults to short entry matching the response code
        * explain: a detailed message defaults to the long entry
                   matching the response code.

        This sends an error response (so it must be called before any
        output has been generated), logs the error, and finally sends
        a piece of HTML explaining the error to the user.

        """

        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = '???', '???'
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        self.log_error("code %d, message %s", code, message)
        self.send_response(code, message)
        self.send_header('Connection', 'close')

        # Message body is omitted for cases described in:
        #  - RFC7230: 3.3. 1xx, 204(No Content), 304(Not Modified)
        #  - RFC7231: 6.3.6. 205(Reset Content)
        body = None
        if (code >= 200 and
            code not in (HTTPStatus.NO_CONTENT,
                         HTTPStatus.RESET_CONTENT,
                         HTTPStatus.NOT_MODIFIED)):
            # HTML encode to prevent Cross Site Scripting attacks
            # (see bug #1100201)
            content = (self.error_message_format % {
                'code': code,
                'message': html.escape(message, quote=False),
                'explain': html.escape(explain, quote=False)
            })
            body = content.encode('UTF-8', 'replace')
            self.send_header("Content-Type", self.error_content_type)
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()

        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def send_response(self, code, message=None):
        """Add the response header to the headers buffer and log the
        response code.

        Also send two standard headers with the server software
        version and the current date.

        """
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header('Server', self.version_string())
        self.send_header('Date', self.date_time_string())

    def send_response_only(self, code, message=None):
        """Send the response header only."""
        if self.request_version != 'HTTP/0.9':
            if message is None:
                if code in self.responses:
                    message = self.responses[code][0]
                else:
                    message = ''
            if not hasattr(self, '_headers_buffer'):
                self._headers_buffer = []
            self._headers_buffer.append(("%s %d %s\r\n" %
                    (self.protocol_version, code, message)).encode(
                        'latin-1', 'strict'))

    def send_header(self, keyword, value):
        """Send a MIME header to the headers buffer."""
        if self.request_version != 'HTTP/0.9':
            if not hasattr(self, '_headers_buffer'):
                self._headers_buffer = []
            self._headers_buffer.append(
                ("%s: %s\r\n" % (keyword, value)).encode('latin-1', 'strict'))

        if keyword.lower() == 'connection':
            if value.lower() == 'close':
                self.close_connection = True
            elif value.lower() == 'keep-alive':
                self.close_connection = False

    def end_headers(self):
        """Send the blank line ending the MIME headers."""
        if self.request_version != 'HTTP/0.9':
            self._headers_buffer.append(b"\r\n")
            self.flush_headers()

    def flush_headers(self):
        if hasattr(self, '_headers_buffer'):
            self.wfile.write(b"".join(self._headers_buffer))
            self._headers_buffer = []

    def log_request(self, code='-', size='-'):
        """Log an accepted request.

        This is called by send_response().

        """
        if isinstance(code, HTTPStatus):
            code = code.value
        self.log_message('"%s" %s %s',
                         self.requestline, str(code), str(size))

    def log_error(self, format, *args):
        """Log an error.

        This is called when a request cannot be fulfilled.  By
        default it passes the message on to log_message().

        Arguments are the same as for log_message().

        XXX This should go to the separate error log.

        """

        self.log_message(format, *args)

    # https://en.wikipedia.org/wiki/List_of_Unicode_characters#Control_codes
    _control_char_table = str.maketrans(
            {c: fr'\x{c:02x}' for c in itertools.chain(range(0x20), range(0x7f,0xa0))})
    _control_char_table[ord('\\')] = r'\\'

    def log_message(self, format, *args):
        """Log an arbitrary message.

        This is used by all other logging functions.  Override
        it if you have specific logging wishes.

        The first argument, FORMAT, is a format string for the
        message to be logged.  If the format string contains
        any % escapes requiring parameters, they should be
        specified as subsequent arguments (it's just like
        printf!).

        The client ip and current date/time are prefixed to
        every message.

        Unicode control characters are replaced with escaped hex
        before writing the output to stderr.

        """

        message = format % args
        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          message.translate(self._control_char_table)))

    def version_string(self):
        """Return the server software version string."""
        return self.server_version + ' ' + self.sys_version

    def date_time_string(self, timestamp=None):
        """Return the current date and time formatted for a message header."""
        if timestamp is None:
            timestamp = time.time()
        return email.utils.formatdate(timestamp, usegmt=True)

    def log_date_time_string(self):
        """Return the current time formatted for logging."""
        now = time.time()
        year, month, day, hh, mm, ss, x, y, z = time.localtime(now)
        s = "%02d/%3s/%04d %02d:%02d:%02d" % (
                day, self.monthname[month], year, hh, mm, ss)
        return s

    weekdayname = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    monthname = [None,
                 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    def address_string(self):
        """Return the client address."""

        return self.client_address[0]

    # Essentially static class variables

    # The version of the HTTP protocol we support.
    # Set this to HTTP/1.1 to enable automatic keepalive
    protocol_version = "HTTP/1.0"

    # MessageClass used to parse headers
    MessageClass = http.client.HTTPMessage

    # hack to maintain backwards compatibility
    responses = {
        v: (v.phrase, v.description)
        for v in HTTPStatus.__members__.values()
    }


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

    server_version = "SimpleHTTP/" + __version__
    index_pages = ("index.html", "index.htm")
    cors = False
    max_upload_size = 500 * 1024 * 1024  # 500 MB
    extensions_map = _encodings_map_default = {
        '.gz': 'application/gzip',
        '.Z': 'application/octet-stream',
        '.bz2': 'application/x-bzip2',
        '.xz': 'application/x-xz',
    }
    icon_map = {
        '.html': '🌐', '.htm': '🌐',
        '.css': '🎨',
        '.js': '⚡',
        '.py': '🐍',
        '.json': '📋',
        '.txt': '📝',
        '.md': '📖', '.rst': '📖',
        '.xml': '📰',
        '.png': '🖼', '.jpg': '🖼', '.jpeg': '🖼',
        '.gif': '📹',
        '.svg': '📐',
        '.ico': '🪟',
        '.zip': '📦', '.tar': '📦', '.gz': '📦',
        '.mp4': '🎬', '.mkv': '🎬', '.avi': '🎬', '.mov': '🎬',
        '.mp3': '🎵', '.wav': '🎵', '.flac': '🎵', '.ogg': '🎵',
        '.pdf': '📑',
        '.sh': '💻',
        '.yml': '⚙', '.yaml': '⚙',
        '.toml': '⚙',
        '.conf': '⚙', '.cfg': '⚙',
        '.exe': '💠', '.msi': '💠',
        '.ttf': '🖨', '.otf': '🖨', '.woff': '🖨', '.woff2': '🖨',
        '.db': '💾', '.sqlite': '💾', '.sqlite3': '💾',
        '.iso': '💿', '.img': '💿',
        '.deb': '📦', '.rpm': '📦',
        '.log': '📜',
        '.key': '🔑', '.pem': '🔑', '.crt': '🔑',
        '.lock': '🔒',
        '.dockerfile': '🐳',
    }

    def __init__(self, *args, directory=None, **kwargs):
        if directory is None:
            directory = os.getcwd()
        self.directory = os.fspath(directory)
        super().__init__(*args, **kwargs)

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def end_headers(self):
        if self.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        super().end_headers()

    def do_GET(self):
        if self.path in ('/terminal', '/terminal/'):
            self._serve_terminal_page()
            return
        f = self.send_head()
        if f:
            try:
                self.copyfile(f, self.wfile)
            finally:
                f.close()

    def do_POST(self):
        if self.path == '/terminal/exec':
            self._exec_terminal_command()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_PUT(self):
        path = self.translate_path(self.path)
        real_root = os.path.normcase(os.path.realpath(self.directory))
        real_path = os.path.normcase(os.path.realpath(path))
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Outside served directory")
            return
        if os.path.isdir(path):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Cannot PUT to a directory")
            return
        content_length = self.headers.get('Content-Length')
        if content_length is None:
            self.send_error(HTTPStatus.LENGTH_REQUIRED)
            return
            return
        try:
            length = int(content_length)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return
        if length < 0:
            self.send_error(HTTPStatus.BAD_REQUEST, "Negative Content-Length")
            return
        if length > self.max_upload_size:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            f"Max upload size is {self.max_upload_size} bytes")
            return
        fname = os.path.basename(path)
        if any(ord(c) < 32 or ord(c) == 127 or c == '/' for c in fname):
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid filename")
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                remaining = length
                while remaining > 0:
                    buf = self.rfile.read(min(remaining, 65536))
                    if not buf:
                        f.close()
                        os.remove(path)
                        return
                    f.write(buf)
                    remaining -= len(buf)
            self.send_response(HTTPStatus.CREATED)
            self.send_header('Content-Length', '0')
            self.end_headers()
        except OSError:
            try:
                self.send_error(HTTPStatus.FORBIDDEN, "Upload failed")
            except OSError:
                pass
        except Exception:
            try:
                self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Upload error")
            except OSError:
                pass

    def do_DELETE(self):
        path = self.translate_path(self.path)
        real_root = os.path.normcase(os.path.realpath(self.directory))
        real_path = os.path.normcase(os.path.realpath(path))
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Outside served directory")
            return
        if real_path == real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Cannot delete root directory")
            return
        if not os.path.exists(path) and not os.path.islink(path):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        except OSError as e:
            self.send_error(HTTPStatus.FORBIDDEN, str(e))

    def do_HEAD(self):
        if self.path in ('/terminal', '/terminal/'):
            self._serve_terminal_page()
            return
        f = self.send_head()
        if f:
            f.close()

    def do_MOVE(self):
        path = self.translate_path(self.path)
        real_root = os.path.normcase(os.path.realpath(self.directory))
        real_path = os.path.normcase(os.path.realpath(path))
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Outside served directory")
            return
        if not os.path.exists(path) and not os.path.islink(path):
            self.send_error(HTTPStatus.NOT_FOUND, "Source not found")
            return
        dest_header = self.headers.get('Destination')
        if not dest_header:
            self.send_error(HTTPStatus.BAD_REQUEST, "Missing Destination header")
            return
        dest_path = urllib.parse.urlsplit(dest_header).path
        dest_path = self.translate_path(dest_path)
        real_dest = os.path.normcase(os.path.realpath(dest_path))
        if not real_dest.startswith(real_root + os.sep) and real_dest != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Destination outside served directory")
            return
        try:
            os.rename(path, dest_path)
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
        except OSError as e:
            self.send_error(HTTPStatus.FORBIDDEN, str(e))

    def do_MKCOL(self):
        path = self.translate_path(self.path)
        real_root = os.path.normcase(os.path.realpath(self.directory))
        real_path = os.path.normcase(os.path.realpath(path))
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Outside served directory")
            return
        if os.path.exists(path):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Path already exists")
            return
        try:
            os.mkdir(path)
            self.send_response(HTTPStatus.CREATED)
            self.send_header('Content-Length', '0')
            self.end_headers()
        except OSError as e:
            self.send_error(HTTPStatus.FORBIDDEN, str(e))

    TERMINAL_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.min.css">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { height: 100%; background: #000; color: #fff; font-family: 'JetBrains Mono', 'Courier New', monospace; }
  #terminal { height: 100vh; padding: 8px; }
  .bar { background: #111; border-bottom: 1px solid #333; padding: 6px 14px; font-size: 13px; display: flex; justify-content: space-between; align-items: center; }
  .bar a { color: #0ae; text-decoration: none; font-size: 12px; }
  .bar a:hover { text-decoration: underline; }
  .warn{background:#221;color:#fc6;font-size:11px;padding:4px 14px;text-align:center;border-bottom:1px solid rgba(255,204,102,.15)}
</style>
</head>
<body>
<div class="bar">
  <span>&#9654; Web Terminal — <span id="cwd">/</span></span>
  <a href="/">&larr; Files</a>
</div>
<div class="warn">&#9888; All commands run on the server with your user privileges</div>
<div id="terminal"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit/lib/xterm-addon-fit.min.js"></script>
<script>
const term = new Terminal({ cursorBlink: true, fontSize: 14, fontFamily: '"JetBrains Mono","Courier New",monospace', theme: { background: '#000', foreground: '#fff', cursor: '#0ae' } });
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById('terminal'));
fit.fit();
let cwd = '/';

term.write('Welcome to Web Terminal\\r\\nType commands and press Enter.\\r\\n\\r\\n');
term.prompt = () => { term.write('\\r\\n$ '); };
term.prompt();

term.onData(data => {
  const code = data.charCodeAt(0);
  if (code === 13) {
    const cmd = term.buffer.active.getLine(term.buffer.active.cursorY)?.translateToString().trim();
    const promptIdx = cmd?.lastIndexOf('$ ');
    const actual = promptIdx >= 0 ? cmd.slice(promptIdx + 2).trim() : '';
    if (!actual) { term.prompt(); return; }
    term.write('\\r\\n');
    fetch('/terminal/exec', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: actual, cwd: cwd })
    })
    .then(r => r.json())
    .then(d => {
      if (d.output) term.write(d.output.replace(/\\n/g, '\\r\\n'));
      if (d.cwd) cwd = d.cwd;
      if (d.error) term.write('\\r\\n\\x1b[31m' + d.error.replace(/\\n/g, '\\r\\n') + '\\x1b[0m');
      term.prompt();
    })
    .catch(e => { term.write('\\r\\nError: ' + e.message.replace(/\\n/g, '\\r\\n')); term.prompt(); });
  } else if (code === 127) {
    term.write('\\b \\b');
  } else {
    term.write(data);
  }
});

window.addEventListener('resize', () => fit.fit());
</script>
</body>
</html>"""

    def _serve_terminal_page(self):
        data = self.TERMINAL_PAGE.encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _exec_terminal_command(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > 65536:
                self._send_json({'output': '', 'error': 'Request too large'})
                return
            body = self.rfile.read(length)
            params = json.loads(body)
            cmd = params.get('command', '').strip()
            req_cwd = params.get('cwd', self.directory)
        except Exception:
            self._send_json({'output': '', 'error': 'Invalid request'})
            return

        if not cmd:
            self._send_json({'output': '', 'error': '', 'cwd': req_cwd})
            return

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=req_cwd,
                timeout=30,
            )
            out = result.stdout
            err = result.stderr
            if result.returncode != 0 and not err:
                err = f'exit code {result.returncode}'
            # Update cwd if cd was in the command
            new_cwd = req_cwd
            if cmd.startswith('cd '):
                target = cmd[3:].strip()
                try:
                    expanded = os.path.expanduser(target)
                    if os.path.isabs(expanded):
                        new_cwd = expanded
                    else:
                        new_cwd = os.path.normpath(os.path.join(req_cwd, expanded))
                except Exception:
                    pass
            self._send_json({'output': out, 'error': err, 'cwd': new_cwd})
        except subprocess.TimeoutExpired:
            self._send_json({'output': '', 'error': 'Command timed out (30s)'})
        except Exception as e:
            self._send_json({'output': '', 'error': str(e)})

    def _send_json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_head(self):
        """Common code for GET and HEAD commands.

        This sends the response code and MIME headers.

        Return value is either a file object (which has to be copied
        to the outputfile by the caller unless the command was HEAD,
        and must be closed by the caller under all circumstances), or
        None, in which case the caller has nothing further to do.

        """
        path = self.translate_path(self.path)
        # Guard against symlink escape outside served directory
        real_root = os.path.normcase(os.path.realpath(self.directory))
        real_path = os.path.normcase(os.path.realpath(path))
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            self.send_error(HTTPStatus.FORBIDDEN, "Path outside served directory")
            return None
        f = None
        if os.path.isdir(path):
            parts = urllib.parse.urlsplit(self.path)
            if not parts.path.endswith(('/', '%2f', '%2F')):
                # redirect browser - doing basically what apache does
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                new_parts = (parts[0], parts[1], parts[2] + '/',
                             parts[3], parts[4])
                new_url = urllib.parse.urlunsplit(new_parts)
                self.send_header("Location", new_url)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            for index in self.index_pages:
                index = os.path.join(path, index)
                if os.path.isfile(index):
                    path = index
                    break
            else:
                return self.list_directory(path)
        ctype = self.guess_type(path)
        # check for trailing "/" which should return 404. See Issue17324
        # The test for this was added in test_httpserver.py
        # However, some OS platforms accept a trailingSlash as a filename
        # See discussion on python-dev and Issue34711 regarding
        # parsing and rejection of filenames with a trailing slash
        if path.endswith("/"):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        try:
            f = open(path, 'rb')
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        try:
            fs = os.fstat(f.fileno())
            etag = f'W/"{fs.st_ino:x}-{int(fs.st_mtime):x}-{fs.st_size:x}"'

            # If-None-Match (ETag-based conditional request)
            if_none_match = self.headers.get("If-None-Match")
            if             if_none_match is not None:
                etags = [e.strip() for e in if_none_match.split(",")]
                if etag in etags or "*" in etags:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    f.close()
                    return None

            # If-Modified-Since (fallback when no If-None-Match)
            if ("If-Modified-Since" in self.headers
                    and "If-None-Match" not in self.headers):
                try:
                    ims = email.utils.parsedate_to_datetime(
                        self.headers["If-Modified-Since"])
                except (TypeError, IndexError, OverflowError, ValueError):
                    pass
                else:
                    if ims.tzinfo is None:
                        ims = ims.replace(tzinfo=datetime.timezone.utc)
                    if ims.tzinfo is datetime.timezone.utc:
                        last_modif = datetime.datetime.fromtimestamp(
                            fs.st_mtime, datetime.timezone.utc)
                        last_modif = last_modif.replace(microsecond=0)
                        if last_modif <= ims:
                            self.send_response(HTTPStatus.NOT_MODIFIED)
                            self.send_header("ETag", etag)
                            self.end_headers()
                            f.close()
                            return None

            # Range request handling (bytes only, single range)
            content_length = fs.st_size
            start = end = None
            if self.command == "GET" and "Range" in self.headers:
                if_range = self.headers.get("If-Range")
                if if_range is None or if_range == etag or if_range == self.date_time_string(fs.st_mtime):
                    range_header = self.headers["Range"]
                    if range_header.startswith("bytes="):
                        parts = range_header[6:].split(",")[0].strip().split("-")
                        rstart = parts[0].strip() if parts[0] else ""
                        rend = parts[1].strip() if len(parts) > 1 and parts[1] else ""
                        if rstart:
                            start = int(rstart)
                        if rend:
                            end = int(rend)
                        if rstart and not rend:
                            end = fs.st_size - 1
                        elif not rstart and rend:
                            start = max(0, fs.st_size - int(rend))
                            end = fs.st_size - 1
                        if start is not None and end is not None and start <= end and start < fs.st_size and end < fs.st_size:
                            f.seek(start)
                            content_length = end - start + 1
                            self.send_response(HTTPStatus.PARTIAL_CONTENT)
                            self.send_header("Content-Range", f"bytes {start}-{end}/{fs.st_size}")
                        else:
                            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                            self.send_header("Content-Range", f"bytes */{fs.st_size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            f.close()
                            return None
                    else:
                        self.send_response(HTTPStatus.OK)
                else:
                    self.send_response(HTTPStatus.OK)
            else:
                self.send_response(HTTPStatus.OK)

            self.send_header("Content-type", ctype)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Last-Modified",
                self.date_time_string(fs.st_mtime))
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return f
        except:
            f.close()
            raise

    @staticmethod
    def size_str(s):
        if s < 1024:
            return str(s) + ' B'
        elif s < 1024 * 1024:
            return f'{s / 1024:.1f} KB'
        elif s < 1024 * 1024 * 1024:
            return f'{s / (1024 * 1024):.1f} MB'
        else:
            return f'{s / (1024 * 1024 * 1024):.1f} GB'

    @staticmethod
    def date_str(ts):
        return time.strftime('%b %d %H:%M', time.localtime(ts))

    @staticmethod
    def entry_html(e):
        size_display = ''
        date_display = ''
        if e['cls'] == 'file':
            size_display = SimpleHTTPRequestHandler.size_str(e['size'])
        if e['mtime']:
            date_display = SimpleHTTPRequestHandler.date_str(e['mtime'])
        link = html.escape(urllib.parse.quote(e['link'], errors='surrogatepass'), quote=False)
        display = e['display']
        dname = html.escape(urllib.parse.quote(e['name'], errors='surrogatepass'), quote=False)
        attrs = f' data-name="{html.escape(e["name"].lower(), quote=True)}" data-size="{e["size"]}" data-mtime="{e["mtime"]}"'
        icon_html = '<span class="icon">' + e['icon'] + '</span>'
        if e['cls'] == 'file':
            _, ext = os.path.splitext(e['name'])
            ext = ext.lower()
            attrs += f' data-ext="{ext}"'
            if ext in ('.mp4', '.webm', '.mkv', '.mov'):
                icon_html = '<video class="vthumb" src="' + link + '?_thumb=1" preload="metadata" playsinline muted></video>'
        return (
            '<div class="entry-wrap">'
            + '<a class="entry ani ' + e['cls'] + '" href="' + link + '"' + attrs + '>'
            + icon_html
            + '<span class="name">' + display + '</span>'
            + '<span class="meta">'
            + ('<span class="size">' + size_display + '</span>' if size_display else '')
            + ('<span class="mtime">' + date_display + '</span>' if date_display else '')
            + '</span>'
            + '</a>'
            + '<button class="rn-btn" onclick="rename(this,'' + dname + '')" title="rename">✎</button>'
            + '</div>'
        )

    def list_directory(self, path):
        """Helper to produce a directory listing (absent index.html).

        Return value is either a file object, or None (indicating an
        error).  In either case, the headers are sent, making the
        interface the same as for send_head().

        """
        try:
            listing = os.listdir(path)
        except OSError:
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "No permission to list directory")
            return None
        listing.sort(key=str.lower)
        displaypath = self.path
        displaypath = displaypath.split('#', 1)[0]
        displaypath = displaypath.split('?', 1)[0]
        try:
            displaypath = urllib.parse.unquote(displaypath,
                                               errors='surrogatepass')
        except UnicodeDecodeError:
            displaypath = urllib.parse.unquote(displaypath)
        displaypath = html.escape(displaypath, quote=False)
        enc = sys.getfilesystemencoding()
        title = 'Directory listing for ' + displaypath

        dirs = []
        files = []
        for name in listing:
            fullname = os.path.join(path, name)
            linkname = name
            displayname = html.escape(name, quote=False)
            displayname = displayname.replace(' ', '&nbsp;')
            entry = {'name': name, 'link': linkname, 'display': displayname}
            try:
                st = os.stat(fullname)
                entry['size'] = st.st_size
                entry['mtime'] = int(st.st_mtime)
            except OSError:
                entry['size'] = 0
                entry['mtime'] = 0
            if os.path.isdir(fullname):
                entry['link'] += '/'
                entry['display'] += '/'
                entry['cls'] = 'dir'
                entry['icon'] = '📁'
                dirs.append(entry)
            else:
                entry['cls'] = 'file'
                entry['icon'] = self.icon_map.get(os.path.splitext(name)[1].lower(), '📄')
                files.append(entry)
            if os.path.islink(fullname):
                entry['display'] += '@'
                entry['cls'] = 'link'

        dir_count = len(dirs)
        file_count = len(files)

        back_link = ''
        parent_path = ''
        if displaypath != '/':
            back_link = '<a class="back-link ani" href=".."><span class="arr">&larr;</span> parent directory</a>\n'
            parent_path = displaypath

        dir_section = ''
        if dirs:
            dir_section = '<div class="section-label ani folders">folders</div>\n<div class="grid dirs">\n' + '\n'.join(SimpleHTTPRequestHandler.entry_html(e) for e in dirs) + '\n</div>\n'
        file_section = ''
        if files:
            file_section = '<div class="section-label ani files">files</div>\n<div class="grid">\n' + '\n'.join(SimpleHTTPRequestHandler.entry_html(e) for e in files) + '\n</div>\n'

        if displaypath == '/':
            breadcrumb_html = '<a href="/" class="path-part ani">~</a>'
        else:
            raw_path = self.path.split('#', 1)[0].split('?', 1)[0]
            segs = raw_path.strip('/').split('/')
            parts = ['<a href="/" class="path-part ani">~</a>']
            cum = ''
            for i, s in enumerate(segs):
                label = html.escape(urllib.parse.unquote(s, errors='surrogatepass'), quote=False)
                cum += '/' + s
                last = i == len(segs) - 1
                parts.append('<span class="path-sep ani">/</span>')
                if last:
                    parts.append(f'<span class="path-part active ani">{label}</span>')
                else:
                    parts.append(f'<a href="{html.escape(cum, quote=False)}" class="path-part ani">{label}</a>')
            breadcrumb_html = ''.join(parts)

        styled = (
            '<!DOCTYPE HTML>\n<html lang="en">\n<head>\n'
            + '<meta charset="' + enc + '">\n'
            + '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            + '<title>' + title + '</title>\n'
            + '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            + '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
            + '<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@300;400;500;700;900&display=swap" rel="stylesheet">\n'
            + '<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>\n'
            + '<style>'
            + '@keyframes grain{0%,100%{transform:translate(0)}10%{transform:translate(-2%,-2%)}20%{transform:translate(1%,-3%)}30%{transform:translate(-3%,1%)}40%{transform:translate(2%,2%)}50%{transform:translate(-1%,3%)}60%{transform:translate(3%,-1%)}70%{transform:translate(-2%,-1%)}80%{transform:translate(1%,2%)}90%{transform:translate(-1%,-2%)}}'
            + '*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}'
            + 'body{font-family:"Google Sans",Inter,-apple-system,BlinkMacSystemFont,sans-serif;background:#000;color:#fff;min-height:100vh;overflow-x:hidden;position:relative}'
            + 'body::before{content:"";position:fixed;inset:0;background:radial-gradient(ellipse at 30% 20%,rgba(255,255,255,.04),transparent 70%),radial-gradient(ellipse at 70% 80%,rgba(255,255,255,.02),transparent 60%);pointer-events:none;z-index:0}'
            + 'body::after{content:"";position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox=\'0 0 256 256\' xmlns=\'http://www.w3.org/2000/svg\'%3E%3Cfilter%3E%3CfeTurbulence type=\'fractalNoise\' baseFrequency=\'0.9\' numOctaves=\'4\' stitchTiles=\'stitch\'/%3E%3C/filter%3E%3Crect width=\'100%25\' height=\'100%25\' filter=\'url(%23noiseFilter)%27 opacity=\'0.03\'/%3E%3C/svg%3E");pointer-events:none;z-index:0;opacity:.15;animation:grain 8s steps(10) infinite}'
            + '.header{position:relative;z-index:1;padding:50px 48px 0;max-width:1400px;margin:0 auto}'
            + '.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:48px}'
            + '.logo{font-size:13px;font-weight:400;color:#fff;letter-spacing:4px;text-transform:uppercase;border:1px solid rgba(255,255,255,.15);padding:8px 18px;border-radius:6px}'
            + '.logo b{color:#fff;font-weight:500}'
            + '.stats-bar{display:flex;gap:24px;font-size:12px;color:#fff;letter-spacing:1px}'
            + '.stats-bar span{display:flex;align-items:center;gap:6px}'
            + '.stats-bar .num{color:#fff;font-weight:500}'
            + '.path-area{margin-bottom:56px}'
            + '.path-label{font-size:11px;font-weight:400;color:#fff;letter-spacing:3px;text-transform:uppercase;margin-bottom:12px}'
            + '.path-row{display:flex;align-items:center;gap:4px;flex-wrap:wrap}'
            + '.path-part{font-size:28px;font-weight:300;color:#fff;text-decoration:none;transition:color .3s;padding:2px 6px;border-radius:4px;letter-spacing:-.5px}'
            + '.path-part:hover{color:#fff;background:rgba(255,255,255,.04)}'
            + '.path-part.active{color:#fff;font-weight:500}'
            + '.path-sep{color:#fff;font-size:20px;font-weight:300;margin:0 2px}'
            + '.back-link{display:inline-flex;align-items:center;gap:10px;color:#fff;text-decoration:none;font-size:13px;font-weight:400;letter-spacing:1px;text-transform:uppercase;margin-bottom:40px;padding:10px 20px;border:1px solid rgba(255,255,255,.15);border-radius:8px;transition:all .3s}'
            + '.back-link:hover{color:#fff;border-color:#fff;background:rgba(255,255,255,.03)}'
            + '.back-link .arr{font-size:18px}'
            + '.up-btn{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#fff;cursor:pointer;padding:6px 14px;border:1px solid rgba(255,255,255,.15);border-radius:6px;background:none;transition:all .3s;letter-spacing:.5px;margin-left:auto}'
            + '.up-btn:hover{border-color:#fff;background:rgba(255,255,255,.03)}'
            + '.up-btn svg{width:14px;height:14px;fill:currentColor}'
            + '.content{position:relative;z-index:1;padding:0 48px 80px;max-width:1400px;margin:0 auto}'
            + '.section-label{font-size:11px;font-weight:500;color:#fff;letter-spacing:3px;text-transform:uppercase;margin-bottom:16px;padding-left:4px}'
            + '.section-label.folders{margin-top:0}'
            + '.section-label.files{margin-top:40px}'
            + '.ani{opacity:0}' + '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px}'
            + '.entry{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:14px;padding:14px 18px;text-decoration:none;color:#fff;font-size:14px;font-weight:400;border-radius:8px;transition:all .25s cubic-bezier(.22,1,.36,1)}'
            + '.entry:hover{color:#fff;background:rgba(255,255,255,.06);transform:translateX(4px) !important}'
            + '.entry .icon{font-size:16px;width:22px;text-align:center;flex-shrink:0;opacity:.6;transition:opacity .3s}'
            + '.entry:hover .icon{opacity:1}'
            + '.entry.dir .icon{color:#fff;opacity:.9}'
            + '.entry.file .icon{color:#fff;opacity:.5}'
            + '.entry.link .icon{color:#fff}'
            + '.entry .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
            + '.entry .meta{display:flex;gap:12px;align-items:center;flex-shrink:0;font-size:11px;color:#fff;opacity:0;transition:opacity .3s}'
            + '.entry:hover .meta{opacity:1}'
            + '.entry.dir .meta{opacity:0}'
            + '.entry.dir:hover .meta{opacity:1}'
            + '.meta .size{color:#fff;font-weight:400;font-variant-numeric:tabular-nums}'
            + '.meta .mtime{color:#fff}'
            + '.empty-state{padding:80px 0;text-align:center;color:#fff;font-size:15px;font-weight:300;letter-spacing:1px}'
            + '.decor{position:fixed;inset:0;pointer-events:none;z-index:0;overflow:hidden}'
            + '.decor-line{position:absolute;background:linear-gradient(90deg,transparent,rgba(255,255,255,.04),transparent)}'
            + '.decor-line-1{top:40%;left:0;width:40%;height:1px;transform:translateX(-30%)}'
            + '.decor-line-2{top:70%;right:0;width:60%;height:1px;transform:translateX(20%)}'
            + '.decor-dot{position:absolute;width:2px;height:2px;background:rgba(255,255,255,.15);border-radius:50%}'
            + '.decor-dot-1{top:25%;right:15%}'
            + '.decor-dot-2{top:55%;left:8%}'
            + '.decor-dot-3{bottom:20%;right:30%}'
            + '.decor-dot-4{top:15%;left:20%}'
            + '.decor-dot-5{bottom:35%;left:40%}'
            + '.decor-dot-6{top:45%;right:25%}'
            + '@media(max-width:768px){.header{padding:30px 20px 0}.content{padding:0 20px 80px}.top{flex-direction:column;align-items:flex-start;gap:16px}.grid{grid-template-columns:1fr}.path-part{font-size:20px}.entry .meta{opacity:1;font-size:10px}}'
            + '.entry-wrap{position:relative}.entry-wrap:hover .rn-btn{opacity:1}'
            + '.rn-btn{position:absolute;top:6px;right:6px;opacity:0;background:rgba(255,255,255,.1);border:none;color:#fff;cursor:pointer;font-size:13px;padding:2px 6px;border-radius:4px;transition:opacity .2s;z-index:2;line-height:1}'
            + '.rn-btn:hover{background:rgba(255,255,255,.2)}'
            + '.vthumb,.vpic{width:44px;height:44px;object-fit:cover;border-radius:4px;display:block}'
            + '.gthumb{display:none;width:100%;height:150px;object-fit:cover;border-radius:6px}'
            + '.gallery-btn{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:rgba(255,255,255,.4);cursor:pointer;padding:6px 10px;letter-spacing:1px;text-transform:uppercase;background:none;border:1px solid rgba(255,255,255,.12);border-radius:6px;font-family:inherit;transition:all .2s}'
            + '.gallery-btn:hover{color:rgba(255,255,255,.7);border-color:rgba(255,255,255,.3)}'
            + '.gallery-btn.active{color:#fff;border-color:#fff}'
            + '.gallery-btn svg{width:14px;height:14px;fill:currentColor}'
            + '.gallery .grid{grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}'
            + '.gallery .grid.dirs{display:none}'
            + '.gallery .section-label.folders{display:none}'
            + '.gallery .entry{grid-template-columns:1fr;gap:4px;padding:10px;text-align:center}'
            + '.gallery .entry .meta{display:none}'
            + '.gallery .entry .rn-btn{display:none}'
            + '.gallery .entry .icon{display:none}'
            + '.gallery .vthumb,.gallery .vpic,.gallery .gthumb{display:block;width:100%;height:150px;object-fit:cover;border-radius:6px}'
            + '.gallery .entry .name{white-space:normal;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;font-size:12px;line-height:1.3;margin-top:4px}'
            + '.search-bar{display:flex;gap:8px;align-items:center;width:200px;position:relative}'
            + '.search-bar input{width:100%;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:6px 10px;color:#fff;font-size:12px;font-family:inherit;outline:none;transition:border-color .2s}'
            + '.search-bar input:focus{border-color:rgba(255,255,255,.3)}'
            + '.search-bar input::placeholder{color:rgba(255,255,255,.3)}'
            + '.sort-btn{font-size:11px;color:rgba(255,255,255,.4);cursor:pointer;padding:6px 0;letter-spacing:1px;text-transform:uppercase;background:none;border:none;font-family:inherit;transition:color .2s;font-weight:500}'
            + '.sort-btn:hover{color:rgba(255,255,255,.7)}'
            + '.sort-btn.active{color:#fff}'
            + '.sort-btn .arrow{display:inline-block;width:10px;text-align:right}'
            + '.lightbox{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.92);display:none;justify-content:center;align-items:center;cursor:pointer}'
            + '.lightbox.open{display:flex}'
            + '.lightbox img,.lightbox video{max-width:90vw;max-height:90vh;object-fit:contain;border-radius:4px}'
            + '.lightbox .close{position:absolute;top:20px;right:30px;font-size:28px;color:#fff;cursor:pointer;opacity:.6;transition:opacity .2s;background:none;border:none;font-family:inherit}'
            + '.lightbox .close:hover{opacity:1}'
            + '.rn-overlay{position:fixed;inset:0;z-index:99;background:rgba(0,0,0,.7);display:none;justify-content:center;align-items:center}'
            + '.rn-overlay.open{display:flex}'
            + '.rn-dialog{background:#1a1a1a;border:1px solid rgba(255,255,255,.15);border-radius:8px;padding:24px;min-width:300px}'
            + '.rn-dialog h3{margin:0 0 12px;font-size:14px;font-weight:400;color:#fff}'
            + '.rn-dialog input{width:100%;padding:8px 10px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:6px;color:#fff;font-size:13px;font-family:inherit;outline:none;margin-bottom:12px}'
            + '.rn-dialog input:focus{border-color:rgba(255,255,255,.3)}'
            + '.rn-dialog .btns{display:flex;gap:8px;justify-content:flex-end}'
            + '.rn-dialog button{padding:6px 16px;border-radius:6px;border:1px solid rgba(255,255,255,.15);background:none;color:#fff;cursor:pointer;font-size:12px;font-family:inherit;transition:all .2s}'
            + '.rn-dialog button:hover{border-color:#fff;background:rgba(255,255,255,.06)}'
            + '.rn-dialog .ok{border-color:#0ae;color:#0ae}'
            + '@media(max-width:768px){.search-bar{width:100%}}'
            + '</style>\n</head>\n<body>\n'
            + '<div class="decor">'
            + '<div class="decor-line decor-line-1"></div>'
            + '<div class="decor-line decor-line-2"></div>'
            + '<div class="decor-dot decor-dot-1"></div>'
            + '<div class="decor-dot decor-dot-2"></div>'
            + '<div class="decor-dot decor-dot-3"></div>'
            + '<div class="decor-dot decor-dot-4"></div>'
            + '<div class="decor-dot decor-dot-5"></div>'
            + '<div class="decor-dot decor-dot-6"></div>'
            + '</div>\n'
            + '<div class="header">\n'
            + '<div class="top">\n'
            + '<div class="logo ani"><b>http</b> server</div>\n'
            + '<div class="stats-bar ani">'
            + '<span>' + str(dir_count) + ' <span class="num">folder' + ('' if dir_count == 1 else 's') + '</span></span>'
            + '<span>' + str(file_count) + ' <span class="num">file' + ('' if file_count == 1 else 's') + '</span></span>'
            + '<button class="up-btn ani" onclick="document.getElementById(\'fu\').click()">'
            + '<svg viewBox="0 0 24 24"><path d="M9 16h6v-6h4l-7-7-7 7h4zm-4 2h14v2H5z"/></svg> upload'
            + '</button>'
            + '<input type="file" id="fu" multiple style="display:none" onchange="uploadFiles(this.files)">'
            + '<div class="search-bar ani"><input type="text" id="filter" placeholder="filter..." oninput="filterList(this.value)"></div>'
            + '<button class="sort-btn ani active" data-sort="name" onclick="sortBy(\'name\')">name<span class="arrow">▲</span></button>'
            + '<button class="sort-btn ani" data-sort="size" onclick="sortBy(\'size\')">size<span class="arrow"></span></button>'
            + '<button class="sort-btn ani" data-sort="mtime" onclick="sortBy(\'mtime\')">date<span class="arrow"></span></button>'
            + '<button class="gallery-btn ani" onclick="toggleGallery()" title="gallery view">'
            + '<svg viewBox="0 0 24 24"><path d="M3 3h8v8H3zm0 10h8v8H3zm10-10h8v8h-8zm0 10h8v8h-8z"/></svg></button>'
            + '</div>\n'
            + '</div>\n'
            + '<script>'
            + 'function uploadFiles(files){'
            + 'var ok=0,fail=0;'
            + 'Promise.all([].map.call(files,function(f){'
            + 'return fetch(f.name,{method:"PUT",body:f})'
            + '.then(function(r){if(r.ok){ok++}else{fail++;return r.text().then(function(t){throw t})}})'
            + '.catch(function(e){fail++;console.error(f.name,e)})'
            + '})).then(function(){if(fail)alert(ok+" uploaded, "+fail+" failed");else if(ok)location.reload()})'
            + '}'
            + '</script>\n'
            + '<div class="path-area">\n'
            + '<div class="path-label ani">location</div>\n'
            + '<div class="path-row">'
            + breadcrumb_html
            + '</div>\n'
            + '</div>\n'
            + back_link
            + '</div>\n'
            + '<div class="content">\n'
            + dir_section
            + file_section
            + ('<div class="empty-state ani">empty directory</div>' if not dirs and not files else '')
            + '</div>\n'
            + '<div class="lightbox" id="lb" onclick="closeLB()"><button class="close" onclick="closeLB()">&times;</button><img id="lbi" src="" alt=""><video id="lbv" src="" controls onclick="event.stopPropagation()"></video></div>\n'
            + '<div class="rn-overlay" id="rno"><div class="rn-dialog"><h3>rename</h3><input type="text" id="rni" onkeydown="if(event.key==\'Enter\')doRename()"><div class="btns"><button onclick="closeRN()">cancel</button><button class="ok" onclick="doRename()">rename</button></div></div></div>\n'
            + '<script>'
            + 'var entries=[],sortField="name",sortAsc=true,galleryOn=false;'
            + 'function _s(){var e=document.querySelectorAll(".ani");for(var i=0;i<e.length;i++)e[i].style.opacity="1"}'
            + 'setTimeout(_s,2500);document.addEventListener("DOMContentLoaded",function(){if(typeof gsap==="undefined")_s()});'
            + 'gsap.config({nullTargetWarn:false});'
            + 'gsap.to(".logo,.stats-bar",{opacity:1,y:0,duration:.6,ease:"power2.out"});'
            + 'gsap.to(".path-label,.path-part,.back-link",{opacity:1,y:0,duration:.6,stagger:.08,ease:"power2.out",delay:.2});'
            + 'gsap.to(".section-label",{opacity:1,x:0,duration:.4,ease:"power2.out",delay:.4});'
            + 'gsap.to(".entry",{opacity:1,y:0,scale:1,duration:.5,stagger:.03,ease:"power3.out",delay:.5,onComplete:function(){gsap.set(".entry",{clearProps:"transform"});initEntries()}});'
            + 'function initEntries(){'
            + 'entries=[];document.querySelectorAll(".entry").forEach(function(e){'
            + 'entries.push({el:e.parentElement,el2:e,name:(e.getAttribute("data-name")||""),size:parseInt(e.getAttribute("data-size"))||0,mtime:parseInt(e.getAttribute("data-mtime"))||0,ext:e.getAttribute("data-ext")||""});'
            + 'e.addEventListener("click",function(ev){'
            + 'var ext=this.getAttribute("data-ext");'
            + 'if(ext&&(ext===".png"||ext===".jpg"||ext===".jpeg"||ext===".gif"||ext===".svg"||ext===".mp4"||ext===".webm"||ext===".mkv"||ext===".mov")){'
            + 'ev.preventDefault();openPreview(this.getAttribute("href"),ext)}'
            + '})'
            + '});'
            + 'renderEntries();genVThumbs()}'
            + 'function genVThumbs(){'
            + 'var vs=document.querySelectorAll("video.vthumb:not([data-vdone])");if(!vs.length)return;'
            + 'function l(v){v.setAttribute("data-vdone","1");'
            + 'v.addEventListener("loadedmetadata",function(){var t=this.duration*0.4;if(t>0)this.currentTime=t});'
            + 'v.addEventListener("seeked",function(){'
            + 'var c=document.createElement("canvas");c.width=this.videoWidth||88;c.height=this.videoHeight||88;'
            + 'c.getContext("2d").drawImage(this,0,0,c.width,c.height);'
            + 'var i=document.createElement("img");i.className="vpic";i.src=c.toDataURL();'
            + 'this.parentNode.replaceChild(i,this)});'
            + 'v.load()}'
            + 'if("IntersectionObserver"in window){'
            + 'var o=new IntersectionObserver(function(e){e.forEach(function(e){if(e.isIntersecting){o.unobserve(e.target);l(e.target)}})},{rootMargin:"200px"});'
            + 'vs.forEach(function(v){o.observe(v)})}'
            + 'else{vs.forEach(l)}}'
            + 'function toggleGallery(){'
            + 'galleryOn=!galleryOn;'
            + 'document.querySelector(".content").classList.toggle("gallery",galleryOn);'
            + 'document.querySelector(".gallery-btn").classList.toggle("active",galleryOn);'
            + 'if(galleryOn){'
            + 'document.querySelectorAll(\'.entry.file[data-ext]\').forEach(function(e){'
            + 'var ext=e.getAttribute("data-ext");'
            + 'if(ext===".png"||ext===".jpg"||ext===".jpeg"||ext===".gif"||ext===".svg"){'
            + 'if(!e.querySelector(".gthumb")){'
            + 'var img=document.createElement("img");img.className="gthumb";img.src=e.getAttribute("href");img.loading="lazy";'
            + 'var icon=e.querySelector(".icon");if(icon)icon.after(img)}}}})}}'
            + 'function fuzzyMatch(q,s){q=q.toLowerCase();s=s.toLowerCase();for(var i=0,j=0;j<s.length&&i<q.length;j++){if(q[i]===s[j])i++}return i===q.length}'
            + 'function filterList(v){document.querySelectorAll(".entry-wrap").forEach(function(e){e.style.display=fuzzyMatch(v,e.querySelector(".entry").getAttribute("data-name"))?"":"none"});updateCounts()}'
            + 'function updateCounts(){'
            + 'var vis=document.querySelectorAll(\'.entry-wrap:not([style*="display:none"])\').length;'
            + 'var els=document.querySelectorAll(".entry-wrap").length;'
            + 'document.querySelector(".stats-bar span:first-child .num").textContent=document.querySelectorAll(\'.entry-wrap:not([style*="display:none"]) .entry.dir\').length+" folder";'
            + 'document.querySelector(".stats-bar span:nth-child(2) .num").textContent=vis+" file"}'
            + 'function sortBy(field){'
            + 'if(sortField===field){sortAsc=!sortAsc}else{sortField=field;sortAsc=true}'
            + 'document.querySelectorAll(".sort-btn").forEach(function(b){b.classList.toggle("active",b.getAttribute("data-sort")===field)});'
            + 'document.querySelectorAll(".sort-btn .arrow").forEach(function(a){a.textContent=""});'
            + 'var btn=document.querySelector(\'.sort-btn[data-sort="\'+field+\'"]\');if(btn)btn.querySelector(".arrow").textContent=sortAsc?"▲":"▼";'
            + 'renderEntries()}'
            + 'function renderEntries(){'
            + 'var c=document.querySelector(".content");'
            + 'var dirs=[],files=[];'
            + 'entries.forEach(function(e){if(e.el2.classList.contains("dir"))dirs.push(e);else files.push(e)});'
            + 'function cmp(a,b){var va=a[sortField],vb=b[sortField];if(typeof va==="string")va=va.toLowerCase(),vb=vb.toLowerCase();return va<vb?-1:va>vb?1:0}'
            + 'var fn=sortAsc?cmp:function(a,b){return -cmp(a,b)};'
            + 'dirs.sort(fn);files.sort(fn);'
            + 'var sorted=dirs.concat(files);var parent=c;'
            + 'entries.forEach(function(e,i){parent.appendChild(e.el)});'
            + 'updateCounts()}'
            + 'function openPreview(url,ext){'
            + 'var lb=document.getElementById("lb"),img=document.getElementById("lbi"),vid=document.getElementById("lbv");'
            + 'img.style.display="none";vid.style.display="none";'
            + 'if(ext===".mp4"||ext===".webm"||ext===".mkv"||ext===".mov"){vid.style.display="";vid.src=url;vid.play()}'
            + 'else{img.style.display="";img.src=url}'
            + 'lb.classList.add("open")}'
            + 'function closeLB(){var lb=document.getElementById("lb"),vid=document.getElementById("lbv");lb.classList.remove("open");vid.pause();vid.src=""}'
            + 'var rnPath="";'
            + 'function rename(btn,name){rnPath=name;document.getElementById("rni").value=name;document.getElementById("rno").classList.add("open");document.getElementById("rni").focus();document.getElementById("rni").select()}'
            + 'function closeRN(){document.getElementById("rno").classList.remove("open")}'
            + 'function doRename(){'
            + 'var newName=document.getElementById("rni").value.trim();if(!newName||newName===rnPath){closeRN();return}'
            + 'fetch("/"+encodeURIComponent(rnPath),{method:"MOVE",headers:{"Destination":"/"+encodeURIComponent(newName)}})'
            + '.then(function(r){if(r.ok)location.reload();else return r.text().then(function(t){alert("rename failed: "+t)})})'
            + '.catch(function(e){alert("rename error: "+e.message)});closeRN()}'
            + 'function uploadFiles(files){'
            + 'var ok=0,fail=0;'
            + 'Promise.all([].map.call(files,function(f){'
            + 'return fetch(f.name,{method:"PUT",body:f})'
            + '.then(function(r){if(r.ok){ok++}else{fail++;return r.text().then(function(t){throw t})}})'
            + '.catch(function(e){fail++;console.error(f.name,e)})'
            + '})).then(function(){if(fail)alert(ok+" uploaded, "+fail+" failed");else if(ok)location.reload()})'
            + '}'
            + '</script>\n'
            + '</body>\n</html>'
        )
        encoded = styled.encode(enc, 'surrogateescape')
        # gzip the HTML if the client supports it (compress before headers)
        use_gzip = False
        accept_encoding = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding:
            compressed = gzip.compress(encoded)
            if len(compressed) < len(encoded):
                encoded = compressed
                use_gzip = True
        buf = io.BytesIO()
        buf.write(encoded)
        buf.seek(0)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "text/html; charset=%s" % enc)
        self.send_header("Content-Length", str(len(encoded)))
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        return buf

    def translate_path(self, path):
        """Translate a /-separated PATH to the local filename syntax.

        Components that mean special things to the local file system
        (e.g. drive or directory names) are ignored.  (XXX They should
        probably be diagnosed.)

        """
        # abandon query parameters
        path = path.split('#', 1)[0]
        path = path.split('?', 1)[0]
        # Don't forget explicit trailing slash when normalizing. Issue17324
        try:
            path = urllib.parse.unquote(path, errors='surrogatepass')
        except UnicodeDecodeError:
            path = urllib.parse.unquote(path)
        trailing_slash = path.endswith('/')
        path = posixpath.normpath(path)
        words = path.split('/')
        words = filter(None, words)
        path = self.directory
        for word in words:
            if os.path.dirname(word) or word in (os.curdir, os.pardir):
                # Ignore components that are not a simple file/directory name
                continue
            path = os.path.join(path, word)
        if trailing_slash:
            path += '/'
        return path

    def copyfile(self, source, outputfile):
        try:
            shutil.copyfileobj(source, outputfile)
        except OSError:
            pass

    def guess_type(self, path):
        """Guess the type of a file.

        Argument is a PATH (a filename).

        Return value is a string of the form type/subtype,
        usable for a MIME Content-type header.

        The default implementation looks the file's extension
        up in the table self.extensions_map, using application/octet-stream
        as a default; however it would be permissible (if
        slow) to look inside the data to make a better guess.

        """
        base, ext = posixpath.splitext(path)
        if ext in self.extensions_map:
            return self.extensions_map[ext]
        ext = ext.lower()
        if ext in self.extensions_map:
            return self.extensions_map[ext]
        guess, _ = mimetypes.guess_file_type(path)
        if guess:
            return guess
        return 'application/octet-stream'


# Utilities for CGIHTTPRequestHandler

def _url_collapse_path(path):
    """
    Given a URL path, remove extra '/'s and '.' path elements and collapse
    any '..' references and returns a collapsed path.

    Implements something akin to RFC-2396 5.2 step 6 to parse relative paths.
    The utility of this function is limited to is_cgi method and helps
    preventing some security attacks.

    Returns: The reconstituted URL, which will always start with a '/'.

    Raises: IndexError if too many '..' occur within the path.

    """
    # Query component should not be involved.
    path, _, query = path.partition('?')
    path = urllib.parse.unquote(path)

    # Similar to os.path.split(os.path.normpath(path)) but specific to URL
    # path semantics rather than local operating system semantics.
    path_parts = path.split('/')
    head_parts = []
    for part in path_parts[:-1]:
        if part == '..':
            head_parts.pop() # IndexError if more '..' than prior parts
        elif part and part != '.':
            head_parts.append( part )
    if path_parts:
        tail_part = path_parts.pop()
        if tail_part:
            if tail_part == '..':
                head_parts.pop()
                tail_part = ''
            elif tail_part == '.':
                tail_part = ''
    else:
        tail_part = ''

    if query:
        tail_part = '?'.join((tail_part, query))

    splitpath = ('/' + '/'.join(head_parts), tail_part)
    collapsed_path = "/".join(splitpath)

    return collapsed_path



nobody = None

def nobody_uid():
    """Internal routine to get nobody's uid"""
    global nobody
    if nobody:
        return nobody
    try:
        import pwd
    except ImportError:
        return -1
    try:
        nobody = pwd.getpwnam('nobody')[2]
    except KeyError:
        nobody = 1 + max(x[2] for x in pwd.getpwall())
    return nobody


def executable(path):
    """Test for executable file."""
    return os.access(path, os.X_OK)


class CGIHTTPRequestHandler(SimpleHTTPRequestHandler):

    """Complete HTTP server with GET, HEAD and POST commands.

    GET and HEAD also support running CGI scripts.

    The POST command is *only* implemented for CGI scripts.

    """

    def __init__(self, *args, **kwargs):
        import warnings
        warnings._deprecated("http.server.CGIHTTPRequestHandler",
                             remove=(3, 15))
        super().__init__(*args, **kwargs)

    # Determine platform specifics
    have_fork = hasattr(os, 'fork')

    # Make rfile unbuffered -- we need to read one line and then pass
    # the rest to a subprocess, so we can't use buffered input.
    rbufsize = 0

    def do_POST(self):
        """Serve a POST request.

        This is only implemented for CGI scripts.

        """

        if self.is_cgi():
            self.run_cgi()
        else:
            self.send_error(
                HTTPStatus.NOT_IMPLEMENTED,
                "Can only POST to CGI scripts")

    def send_head(self):
        """Version of send_head that support CGI scripts"""
        if self.is_cgi():
            return self.run_cgi()
        else:
            return SimpleHTTPRequestHandler.send_head(self)

    def is_cgi(self):
        """Test whether self.path corresponds to a CGI script.

        Returns True and updates the cgi_info attribute to the tuple
        (dir, rest) if self.path requires running a CGI script.
        Returns False otherwise.

        If any exception is raised, the caller should assume that
        self.path was rejected as invalid and act accordingly.

        The default implementation tests whether the normalized url
        path begins with one of the strings in self.cgi_directories
        (and the next character is a '/' or the end of the string).

        """
        collapsed_path = _url_collapse_path(self.path)
        dir_sep = collapsed_path.find('/', 1)
        while dir_sep > 0 and not collapsed_path[:dir_sep] in self.cgi_directories:
            dir_sep = collapsed_path.find('/', dir_sep+1)
        if dir_sep > 0:
            head, tail = collapsed_path[:dir_sep], collapsed_path[dir_sep+1:]
            self.cgi_info = head, tail
            return True
        return False


    cgi_directories = ['/cgi-bin', '/htbin']

    def is_executable(self, path):
        """Test whether argument path is an executable file."""
        return executable(path)

    def is_python(self, path):
        """Test whether argument path is a Python script."""
        head, tail = os.path.splitext(path)
        return tail.lower() in (".py", ".pyw")

    def run_cgi(self):
        """Execute a CGI script."""
        dir, rest = self.cgi_info
        path = dir + '/' + rest
        i = path.find('/', len(dir)+1)
        while i >= 0:
            nextdir = path[:i]
            nextrest = path[i+1:]

            scriptdir = self.translate_path(nextdir)
            if os.path.isdir(scriptdir):
                dir, rest = nextdir, nextrest
                i = path.find('/', len(dir)+1)
            else:
                break

        # find an explicit query string, if present.
        rest, _, query = rest.partition('?')

        # dissect the part after the directory name into a script name &
        # a possible additional path, to be stored in PATH_INFO.
        i = rest.find('/')
        if i >= 0:
            script, rest = rest[:i], rest[i:]
        else:
            script, rest = rest, ''

        scriptname = dir + '/' + script
        scriptfile = self.translate_path(scriptname)
        if not os.path.exists(scriptfile):
            self.send_error(
                HTTPStatus.NOT_FOUND,
                "No such CGI script (%r)" % scriptname)
            return
        if not os.path.isfile(scriptfile):
            self.send_error(
                HTTPStatus.FORBIDDEN,
                "CGI script is not a plain file (%r)" % scriptname)
            return
        ispy = self.is_python(scriptname)
        if self.have_fork or not ispy:
            if not self.is_executable(scriptfile):
                self.send_error(
                    HTTPStatus.FORBIDDEN,
                    "CGI script is not executable (%r)" % scriptname)
                return

        # Reference: https://www6.uniovi.es/~antonio/ncsa_httpd/cgi/env.html
        # XXX Much of the following could be prepared ahead of time!
        env = copy.deepcopy(os.environ)
        env['SERVER_SOFTWARE'] = self.version_string()
        env['SERVER_NAME'] = self.server.server_name
        env['GATEWAY_INTERFACE'] = 'CGI/1.1'
        env['SERVER_PROTOCOL'] = self.protocol_version
        env['SERVER_PORT'] = str(self.server.server_port)
        env['REQUEST_METHOD'] = self.command
        uqrest = urllib.parse.unquote(rest)
        env['PATH_INFO'] = uqrest
        env['PATH_TRANSLATED'] = self.translate_path(uqrest)
        env['SCRIPT_NAME'] = scriptname
        env['QUERY_STRING'] = query
        env['REMOTE_ADDR'] = self.client_address[0]
        authorization = self.headers.get("authorization")
        if authorization:
            authorization = authorization.split()
            if len(authorization) == 2:
                import base64, binascii
                env['AUTH_TYPE'] = authorization[0]
                if authorization[0].lower() == "basic":
                    try:
                        authorization = authorization[1].encode('ascii')
                        authorization = base64.decodebytes(authorization).\
                                        decode('ascii')
                    except (binascii.Error, UnicodeError):
                        pass
                    else:
                        authorization = authorization.split(':')
                        if len(authorization) == 2:
                            env['REMOTE_USER'] = authorization[0]
        # XXX REMOTE_IDENT
        if self.headers.get('content-type') is None:
            env['CONTENT_TYPE'] = self.headers.get_content_type()
        else:
            env['CONTENT_TYPE'] = self.headers['content-type']
        length = self.headers.get('content-length')
        if length:
            env['CONTENT_LENGTH'] = length
        referer = self.headers.get('referer')
        if referer:
            env['HTTP_REFERER'] = referer
        accept = self.headers.get_all('accept', ())
        env['HTTP_ACCEPT'] = ','.join(accept)
        ua = self.headers.get('user-agent')
        if ua:
            env['HTTP_USER_AGENT'] = ua
        co = filter(None, self.headers.get_all('cookie', []))
        cookie_str = ', '.join(co)
        if cookie_str:
            env['HTTP_COOKIE'] = cookie_str
        # XXX Other HTTP_* headers
        # Since we're setting the env in the parent, provide empty
        # values to override previously set values
        for k in ('QUERY_STRING', 'REMOTE_HOST', 'CONTENT_LENGTH',
                  'HTTP_USER_AGENT', 'HTTP_COOKIE', 'HTTP_REFERER'):
            env.setdefault(k, "")

        self.send_response(HTTPStatus.OK, "Script output follows")
        self.flush_headers()

        decoded_query = query.replace('+', ' ')

        if self.have_fork:
            # Unix -- fork as we should
            args = [script]
            if '=' not in decoded_query:
                args.append(decoded_query)
            nobody = nobody_uid()
            self.wfile.flush() # Always flush before forking
            pid = os.fork()
            if pid != 0:
                # Parent
                pid, sts = os.waitpid(pid, 0)
                # throw away additional data [see bug #427345]
                while select.select([self.rfile], [], [], 0)[0]:
                    if not self.rfile.read(1):
                        break
                exitcode = os.waitstatus_to_exitcode(sts)
                if exitcode:
                    self.log_error(f"CGI script exit code {exitcode}")
                return
            # Child
            try:
                try:
                    os.setuid(nobody)
                except OSError:
                    pass
                os.dup2(self.rfile.fileno(), 0)
                os.dup2(self.wfile.fileno(), 1)
                os.execve(scriptfile, args, env)
            except:
                self.server.handle_error(self.request, self.client_address)
                os._exit(127)

        else:
            # Non-Unix -- use subprocess
            import subprocess
            cmdline = [scriptfile]
            if self.is_python(scriptfile):
                interp = sys.executable
                if interp.lower().endswith("w.exe"):
                    # On Windows, use python.exe, not pythonw.exe
                    interp = interp[:-5] + interp[-4:]
                cmdline = [interp, '-u'] + cmdline
            if '=' not in query:
                cmdline.append(query)
            self.log_message("command: %s", subprocess.list2cmdline(cmdline))
            try:
                nbytes = int(length)
            except (TypeError, ValueError):
                nbytes = 0
            p = subprocess.Popen(cmdline,
                                 stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 env = env
                                 )
            if self.command.lower() == "post" and nbytes > 0:
                cursize = 0
                data = self.rfile.read(min(nbytes, _MIN_READ_BUF_SIZE))
                while len(data) < nbytes and len(data) != cursize:
                    cursize = len(data)
                    # This is a geometric increase in read size (never more
                    # than doubling out the current length of data per loop
                    # iteration).
                    delta = min(cursize, nbytes - cursize)
                    try:
                        data += self.rfile.read(delta)
                    except TimeoutError:
                        break
            else:
                data = None
            # throw away additional data [see bug #427345]
            while select.select([self.rfile._sock], [], [], 0)[0]:
                if not self.rfile._sock.recv(1):
                    break
            stdout, stderr = p.communicate(data)
            self.wfile.write(stdout)
            if stderr:
                self.log_error('%s', stderr)
            p.stderr.close()
            p.stdout.close()
            status = p.returncode
            if status:
                self.log_error("CGI script exit status %#x", status)
            else:
                self.log_message("CGI script exited OK")


def _get_best_family(*address):
    infos = socket.getaddrinfo(
        *address,
        type=socket.SOCK_STREAM,
        flags=socket.AI_PASSIVE,
    )
    family, type, proto, canonname, sockaddr = next(iter(infos))
    return family, sockaddr


def test(HandlerClass=BaseHTTPRequestHandler,
         ServerClass=ThreadingHTTPServer,
         protocol="HTTP/1.0", port=8000, bind=None,
         tls_cert=None, tls_key=None, tls_password=None):
    """Test the HTTP request handler class.

    This runs an HTTP server on port 8000 (or the port argument).

    """
    ServerClass.address_family, addr = _get_best_family(bind, port)
    HandlerClass.protocol_version = protocol

    if tls_cert:
        server = ServerClass(addr, HandlerClass, certfile=tls_cert,
                             keyfile=tls_key, password=tls_password)
    else:
        server = ServerClass(addr, HandlerClass)

    with server as httpd:
        host, port = httpd.socket.getsockname()[:2]
        url_host = f'[{host}]' if ':' in host else host
        protocol = 'HTTPS' if tls_cert else 'HTTP'
        print(
            f"Serving {protocol} on {host} port {port} "
            f"({protocol.lower()}://{url_host}:{port}/) ..."
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received, exiting.")
            sys.exit(0)

if __name__ == '__main__':
    import argparse
    import contextlib

    parser = argparse.ArgumentParser(color=True)
    parser.add_argument('--cgi', action='store_true',
                        help='run as CGI server')
    parser.add_argument('-b', '--bind', metavar='ADDRESS',
                        help='bind to this address '
                             '(default: all interfaces)')
    parser.add_argument('-d', '--directory', default=os.getcwd(),
                        help='serve this directory '
                             '(default: current directory)')
    parser.add_argument('-p', '--protocol', metavar='VERSION',
                        default='HTTP/1.0',
                        help='conform to this HTTP version '
                             '(default: %(default)s)')
    parser.add_argument('--tls-cert', metavar='PATH',
                        help='path to the TLS certificate chain file')
    parser.add_argument('--tls-key', metavar='PATH',
                        help='path to the TLS key file')
    parser.add_argument('--tls-password-file', metavar='PATH',
                        help='path to the password file for the TLS key')
    parser.add_argument('--cors', action='store_true',
                        help='enable CORS headers (Access-Control-Allow-Origin: *)')
    parser.add_argument('port', default=8000, type=int, nargs='?',
                        help='bind to this port '
                             '(default: %(default)s)')
    args = parser.parse_args()

    if not args.tls_cert and args.tls_key:
        parser.error("--tls-key requires --tls-cert to be set")

    tls_key_password = None
    if args.tls_password_file:
        if not args.tls_cert:
            parser.error("--tls-password-file requires --tls-cert to be set")

        try:
            with open(args.tls_password_file, "r", encoding="utf-8") as f:
                tls_key_password = f.read().strip()
        except OSError as e:
            parser.error(f"Failed to read TLS password file: {e}")

    if args.cgi:
        handler_class = CGIHTTPRequestHandler
    else:
        handler_class = SimpleHTTPRequestHandler

    if args.cors:
        handler_class.cors = True

    # ensure dual-stack is not disabled; ref #38907
    class DualStackServerMixin:

        def server_bind(self):
            # suppress exception when protocol is IPv4
            with contextlib.suppress(Exception):
                self.socket.setsockopt(
                    socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            return super().server_bind()

        def finish_request(self, request, client_address):
            self.RequestHandlerClass(request, client_address, self,
                                     directory=args.directory)

    class HTTPDualStackServer(DualStackServerMixin, ThreadingHTTPServer):
        pass
    class HTTPSDualStackServer(DualStackServerMixin, ThreadingHTTPSServer):
        pass

    ServerClass = HTTPSDualStackServer if args.tls_cert else HTTPDualStackServer

    test(
        HandlerClass=handler_class,
        ServerClass=ServerClass,
        port=args.port,
        bind=args.bind,
        protocol=args.protocol,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        tls_password=tls_key_password,
    )
