import collections
import subprocess
import warnings

from . import protocols
from . import transports
from .log import logger


class BaseSubprocessTransport(transports.SubprocessTransport):

    def __init__(
        self, loop, protocol, args, shell,
        stdin, stdout, stderr, bufsize,
        waiter=None, extra=None, **kwargs
    ):
        # sets `self._extra` to extra
        super().__init__(extra)

        # TODO - add attr comments
        self._closed = False

        # an instance of a user-provided subprocess protocol which implements
        # the protocols.SubprocessProtocol interface (may implement it only
        # partially)
        self._protocol = protocol
        self._loop = loop

        # instance of subprocess.Popen after `self._start` method is called
        self._proc = None
        self._pid = None
        # exitcode returned by subprocess
        self._returncode = None
        self._exit_waiters = []
        self._pending_calls = collections.deque()
        self._pipes = {}
        self._finished = False

        # TODO - why set `self._pipes` elements to None
        # user wants to redirect stdin to a pipe
        if stdin == subprocess.PIPE:
            self._pipes[0] = None
        # user wants to redirect stdout to a pipe
        if stdout == subprocess.PIPE:
            self._pipes[1] = None
        # user wants to redirect stdout to a pipe
        if stderr == subprocess.PIPE:
            self._pipes[2] = None


class WriteSubprocessPipeProto(protocols.BaseProtocol):

    def __init__(self, proc, fd):
        """
        Usually instantiated by `loop.connect_write_pipe` method called
        by unix_events._UnixSubprocessTransport when it connects all required
        pipes.
        """
        # usually an instance of unix_events._UnixSubprocessTransport
        self.proc = proc
        # fd of write end of the underlying unix pipe or socket from a socket
        # pair
        self.fd = fd
        # usually and instance of unix_events._UnixWritePipeTransport
        self.pipe = None
        self.disconnected = False

    def connection_made(self, transport):
        """
        Usually called by unix_events._UnixWritePipeTransport instance when
        it has done all its setup.
        `transport` is an instance of unix_events._UnixWritePipeTransport
        """
        self.pipe = transport

    def __repr__(self):
        return f'<{self.__class__.__name__} fd={self.fd} pipe={self.pipe!r}>'

    def connection_lost(self, exc):
        """
        Usually called by unix_events._UnixWritePipeTransport after it looses
        connection.

        This in turn through an instance of unix_events._UnixSubprocessTransport
        notifies a subprocess.SubprocessStreamProtocol instance or a
        user-provided subprocess protocol about the fact that this
        particular pipe connection has been lost/closed.

        If at this point not only this pipe connection is lost but also
        all pipe connections associated with the
        unix_events._UnixSubprocessTransport, then notifies user-provided
        subprocess protocol that its subprocess/subprocess transport has
        terminated.
        """
        self.disconnected = True
        self.proc._pipe_connection_lost(self.fd, exc)
        self.proc = None

    def pause_writing(self):
        """
        May be called by unix_events._UnixWritePipeTransport after it extends
        its buffer with data to be written to pipe and its buffer went above
        a high watermark of 64KB.
        Proxy this pause writing request to user-provided subprocess protocol.
        """
        self.proc._protocol.pause_writing()

    def resume_writing(self):
        """
        May be called by unix_events._UnixWritePipeTransport when all data from
        buffer was written to pipe/socket.
        Proxy the resume request to user-provided subprocess protocol.
        """
        self.proc._protocol.resume_writing()


class ReadSubprocessPipeProto(WriteSubprocessPipeProto,
                              protocols.Protocol):
    """
    Usually instantiated by `loop.connect_read_pipe` method called
    by unix_events._UnixSubprocessTransport when it connects all required
    pipes.
    """

    def data_received(self, data):
        """
        Usually called by unix_events._UnixSubprocessTransport when it receives
        data from a subprocesses' stdout pipe or some other pipe to be read from.

        Directly passes the read data on to user-provided subprocess protocol.

        Basically proxies read data from pipe by pipe transport to user code.
        """
        self.proc._pipe_data_received(self.fd, data)
