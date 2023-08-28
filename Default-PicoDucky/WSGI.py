import io
import gc
from micropython import const
import socketpool
import wifi

class BadRequestError(Exception):
    """Raised when the client sends an unexpected empty line"""
    pass

_BUFFER_SIZE = 32
_BUFFER = bytearray(_BUFFER_SIZE)

def readline(socketin):
    data_string = b""
    while True:
        try:
            num = socketin.recv_into(_BUFFER, 1)
            data_string += str(_BUFFER, 'utf8')[:num]
            if num == 0:
                return data_string
            if data_string[-2:] == b"\r\n":
                return data_string[:-2]
        except OSError as ex:
            if ex.errno == 11:  # [Errno 11] EAGAIN
                continue
            raise

def read(socketin, length=-1):
    total = 0
    data_string = b""
    try:
        if length > 0:
            while total < length:
                rest = length - total
                num = socketin.recv_into(_BUFFER, min(_BUFFER_SIZE, rest))
                if num == 0:
                    return data_string
                data_string += _BUFFER[:num]
                total = total + num
            return data_string
        else:
            while True:
                num = socketin.recv_into(_BUFFER, 1)
                data_string += str(_BUFFER, 'utf8')[:num]
                if num == 0:
                    return data_string
    except OSError as ex:
        if ex.errno == 11:  # [Errno 11] EAGAIN
            return data_string
        raise

def parse_headers(sock):
    headers = {}
    while True:
        line = readline(sock)
        if not line or line == b"\r\n":
            break
        title, content = line.split(b': ', 1)
        if title and content:
            title = str(title.lower(), 'utf-8')
            content = str(content, 'utf-8')
            headers[title] = content
    return headers

pool = socketpool.SocketPool(wifi.radio)
NO_SOCK_AVAIL = const(255)

class WSGIServer:
    def __init__(self, port=80, debug=False, application=None):
        self.application = application
        self.port = port
        self._server_sock = None
        self._client_sock = None
        self._debug = debug
        self._response_status = None
        self._response_headers = []

    def start(self):
        self._server_sock = pool.socket(pool.AF_INET, pool.SOCK_STREAM)
        self._server_sock.bind((repr(wifi.radio.ipv4_address_ap), self.port))
        self._server_sock.listen(1)

    def pretty_ip(self):
        return f"http://{wifi.radio.ipv4_address_ap}:{self.port}"

    def update_poll(self):
        self.client_available()
        if self._client_sock:
            try:
                environ = self._get_environ(self._client_sock)
                result = self.application(environ, self._start_response)
                self.finish_response(result)
            except BadRequestError:
                self._start_response("400 Bad Request", [])
                self.finish_response([])

    def finish_response(self, result):
        try:
            response = "HTTP/1.1 {0}\r\n".format(self._response_status)
            for header in self._response_headers:
                response += "{0}: {1}\r\n".format(*header)
            response += "\r\n"
            self._client_sock.send(response.encode("utf-8"))
            for data in result:
                if isinstance(data, str):
                    data = data.encode("utf-8")
                elif not isinstance(data, bytes):
                    data = str(data).encode("utf-8")
                bytes_sent = 0
                while bytes_sent < len(data):
                    try:
                        bytes_sent += self._client_sock.send(data[bytes_sent:])
                    except OSError as ex:
                        if ex.errno != 11:  # [Errno 11] EAGAIN
                            raise
            gc.collect()
        except OSError as ex:
            if ex.errno != 104:  # [Errno 104] ECONNRESET
                raise
        finally:
            self._client_sock.close()
            self._client_sock = None

    def client_available(self):
        sock = None
        if not self._server_sock:
            print("Server has not been started, cannot check for clients!")
        elif not self._client_sock:
            self._server_sock.setblocking(False)
            try:
                self._client_sock, addr = self._server_sock.accept()
            except OSError as ex:
                if ex.errno != 11:  # [Errno 11] EAGAIN
                    raise

    def _start_response(self, status, response_headers):
        self._response_status = status
        self._response_headers = [("Server", "esp32WSGIServer")] + response_headers

    def _get_environ(self, client):
        env = {}
        line = readline(client).decode("utf-8")
        try:
            (method, path, ver) = line.rstrip("\r\n").split(None, 2)
        except ValueError:
            raise BadRequestError("Unknown request from client.")

        env["wsgi.version"] = (1, 0)
        env["wsgi.url_scheme"] = "http"
        env["wsgi.multithread"] = False
        env["wsgi.multiprocess"] = False
        env["wsgi.run_once"] = False

        env["REQUEST_METHOD"] = method
        env["SCRIPT_NAME"] = ""
        env["SERVER_NAME"] = str(wifi.radio.ipv4_address_ap)
        env["SERVER_PROTOCOL"] = ver
        env["SERVER_PORT"] = self.port
        if path.find("?") >= 0:
            env["PATH_INFO"] = path.split("?")[0]
            env["QUERY_STRING"] = path.split("?")[1]
        else:
            env["PATH_INFO"] = path

        headers = parse_headers(client)
        if "content-type" in headers:
            env["CONTENT_TYPE"] = headers.get("content-type")
        if "content-length" in headers:
            env["CONTENT_LENGTH"] = headers.get("content-length")
            body = read(client, int(env["CONTENT_LENGTH"]))
            env["wsgi.input"] = io.StringIO(body)
        else:
            body = read(client)
            env["wsgi.input"] = io.StringIO(body)
        for name, value in headers.items():
            key = "HTTP_" + name.replace("-", "_").upper()
            if key in env:
                value = "{0},{1}".format(env[key], value)
            env[key] = value

        return env
