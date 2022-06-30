"""Event loop and event loop policy."""
import contextvars
import os
import socket
import sys
import subprocess
import threading
import asyncio

from . import format_helpers


class Handle:
    """Object returned by callback registration methods."""

    __slots__ = ('_callback', '_args', '_cancelled', '_loop',
                 '_source_traceback', '_repr', '__weakref__',
                 '_context')

    def __init__(self, callback, args, loop, context=None):
        if context is None:
            context = contextvars.copy_context()
        self._context = context
        self._loop = loop
        self._callback = callback
        self._args = args
        self._cancelled = False
        self._repr = None
        if self._loop.get_debug():
            # only in DEBUG mode
            self._source_traceback = format_helpers.extract_stack(
                sys._getframe(1))
        else:
            self._source_traceback = None

    def _repr_info(self):
        info = [self.__class__.__name__]
        if self._cancelled:
            info.append('cancelled')
        if self._callback is not None:
            info.append(format_helpers._format_callback_source(
                self._callback, self._args))
        if self._source_traceback:
            frame = self._source_traceback[-1]
            info.append(f'created at {frame[0]}:{frame[1]}')
        return info

    def __repr__(self):
        if self._repr is not None:
            return self._repr
        info = self._repr_info()
        return '<{}>'.format(' '.join(info))

    def cancel(self):
        if not self._cancelled:
            self._cancelled = True
            if self._loop.get_debug():
                # Keep a representation in debug mode to keep callback and
                # parameters. For example, to log the warning
                # "Executing <Handle...> took 2.5 second"
                self._repr = repr(self)
            self._callback = None
            self._args = None

    def cancelled(self):
        return self._cancelled

    def _run(self):
        try:
            self._context.run(self._callback, *self._args)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            cb = format_helpers._format_callback_source(
                self._callback, self._args)
            msg = f'Exception in callback {cb}'
            context = {
                'message': msg,
                'exception': exc,
                'handle': self,
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)
        self = None  # Needed to break cycles when an exception occurs.


class TimerHandle(Handle):
    """Object returned by timed callback registration methods."""

    __slots__ = ['_scheduled', '_when']

    def __init__(self, when, callback, args, loop, context=None):
        assert when is not None
        super().__init__(callback, args, loop, context)
        # only in _DEBUG mode
        if self._source_traceback:
            del self._source_traceback[-1]
        self._when = when
        self._scheduled = False

    def _repr_info(self):
        info = super()._repr_info()
        pos = 2 if self._cancelled else 1
        info.insert(pos, f'when={self._when}')
        return info

    def __hash__(self):
        return hash(self._when)

    def __lt__(self, other):
        if isinstance(other, TimerHandle):
            return self._when < other._when
        return NotImplemented

    def __le__(self, other):
        if isinstance(other, TimerHandle):
            return self._when < other._when or self.__eq__(other)
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, TimerHandle):
            return self._when > other._when
        return NotImplemented

    def __ge__(self, other):
        if isinstance(other, TimerHandle):
            return self._when > other._when or self.__eq__(other)
        return NotImplemented

    def __eq__(self, other):
        if isinstance(other, TimerHandle):
            return (self._when == other._when and
                    self._callback == other._callback and
                    self._args == other._args and
                    self._cancelled == other._cancelled)
        return NotImplemented

    def cancel(self):
        """
        Sets self._cancelled to True;
        increments loop's ._timer_cancelled_count by 1
        (via _timer_handle_cancelled) - will only increment if this handle's
        .scheduled attr is True
        """
        if not self._cancelled:
            self._loop._timer_handle_cancelled(self)
        super().cancel()

    def when(self):
        """Return a scheduled callback time.

        The time is an absolute timestamp, using the same time
        reference as loop.time().
        """
        # base event loop uses time.monotonic()
        return self._when


class AbstractServer:
    """Abstract server returned by create_server()."""

    def close(self):
        """Stop serving.  This leaves existing connections open."""
        raise NotImplementedError

    def get_loop(self):
        """Get the event loop the Server object is attached to."""
        raise NotImplementedError

    def is_serving(self):
        """Return True if the server is accepting connections."""
        raise NotImplementedError

    async def start_serving(self):
        """Start accepting connections.

        This method is idempotent, so it can be called when
        the server is already being serving.
        """
        raise NotImplementedError

    async def serve_forever(self):
        """Start accepting connections until the coroutine is cancelled.

        The server is closed when the coroutine is cancelled.
        """
        raise NotImplementedError

    async def wait_closed(self):
        """Coroutine to wait until service is closed."""
        raise NotImplementedError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.close()
        await self.wait_closed()


class AbstractEventLoop:
    """Abstract event loop."""

    # Running and stopping the event loop.

    def run_forever(self):
        """Run the event loop until stop() is called."""
        raise NotImplementedError

    def run_until_complete(self, future):
        """Run the event loop until a Future is done.

        Return the Future's result, or raise its exception.
        """
        raise NotImplementedError

    def stop(self):
        """Stop the event loop as soon as reasonable.

        Exactly how soon that is may depend on the implementation, but
        no more I/O callbacks should be scheduled.
        """
        raise NotImplementedError

    def is_running(self):
        """Return whether the event loop is currently running."""
        raise NotImplementedError

    def is_closed(self):
        """Returns True if the event loop was closed."""
        raise NotImplementedError

    def close(self):
        """Close the loop.

        The loop should not be running.

        This is idempotent and irreversible.

        No other methods should be called after this one.
        """
        raise NotImplementedError

    async def shutdown_asyncgens(self):
        """Shutdown all active asynchronous generators."""
        raise NotImplementedError

    async def shutdown_default_executor(self):
        """Schedule the shutdown of the default executor."""
        raise NotImplementedError

    # Methods scheduling callbacks.  All these return Handles.

    def _timer_handle_cancelled(self, handle):
        """Notification that a TimerHandle has been cancelled."""
        raise NotImplementedError

    def call_soon(self, callback, *args):
        return self.call_later(0, callback, *args)

    def call_later(self, delay, callback, *args):
        raise NotImplementedError

    def call_at(self, when, callback, *args):
        raise NotImplementedError

    def time(self):
        raise NotImplementedError

    def create_future(self):
        raise NotImplementedError

    # Method scheduling a coroutine object: create a task.

    def create_task(self, coro, *, name=None):
        raise NotImplementedError

    # Methods for interacting with threads.

    def call_soon_threadsafe(self, callback, *args):
        raise NotImplementedError

    def run_in_executor(self, executor, func, *args):
        raise NotImplementedError

    def set_default_executor(self, executor):
        raise NotImplementedError

    # Network I/O methods returning Futures.

    async def getaddrinfo(self, host, port, *,
                          family=0, type=0, proto=0, flags=0):
        raise NotImplementedError

    async def getnameinfo(self, sockaddr, flags=0):
        raise NotImplementedError

    async def create_connection(
            self, protocol_factory, host=None, port=None,
            *, ssl=None, family=0, proto=0,
            flags=0, sock=None, local_addr=None,
            server_hostname=None,
            ssl_handshake_timeout=None,
            happy_eyeballs_delay=None, interleave=None):
        raise NotImplementedError

    async def create_server(
            self, protocol_factory, host=None, port=None,
            *, family=socket.AF_UNSPEC,
            flags=socket.AI_PASSIVE, sock=None, backlog=100,
            ssl=None, reuse_address=None, reuse_port=None,
            ssl_handshake_timeout=None,
            start_serving=True):
        """A coroutine which creates a TCP server bound to host and port.

        The return value is a Server object which can be used to stop
        the service.

        If host is an empty string or None all interfaces are assumed
        and a list of multiple sockets will be returned (most likely
        one for IPv4 and another one for IPv6). The host parameter can also be
        a sequence (e.g. list) of hosts to bind to.

        family can be set to either AF_INET or AF_INET6 to force the
        socket to use IPv4 or IPv6. If not set it will be determined
        from host (defaults to AF_UNSPEC).

        flags is a bitmask for getaddrinfo().

        sock can optionally be specified in order to use a preexisting
        socket object.

        backlog is the maximum number of queued connections passed to
        listen() (defaults to 100).

        ssl can be set to an SSLContext to enable SSL over the
        accepted connections.

        reuse_address tells the kernel to reuse a local socket in
        TIME_WAIT state, without waiting for its natural timeout to
        expire. If not specified will automatically be set to True on
        UNIX.

        reuse_port tells the kernel to allow this endpoint to be bound to
        the same port as other existing endpoints are bound to, so long as
        they all set this flag when being created. This option is not
        supported on Windows.

        ssl_handshake_timeout is the time in seconds that an SSL server
        will wait for completion of the SSL handshake before aborting the
        connection. Default is 60s.

        start_serving set to True (default) causes the created server
        to start accepting connections immediately.  When set to False,
        the user should await Server.start_serving() or Server.serve_forever()
        to make the server to start accepting connections.
        """
        raise NotImplementedError

    async def sendfile(self, transport, file, offset=0, count=None,
                       *, fallback=True):
        """Send a file through a transport.

        Return an amount of sent bytes.
        """
        raise NotImplementedError

    async def start_tls(self, transport, protocol, sslcontext, *,
                        server_side=False,
                        server_hostname=None,
                        ssl_handshake_timeout=None):
        """Upgrade a transport to TLS.

        Return a new transport that *protocol* should start using
        immediately.
        """
        raise NotImplementedError

    async def create_unix_connection(
            self, protocol_factory, path=None, *,
            ssl=None, sock=None,
            server_hostname=None,
            ssl_handshake_timeout=None):
        raise NotImplementedError

    async def create_unix_server(
            self, protocol_factory, path=None, *,
            sock=None, backlog=100, ssl=None,
            ssl_handshake_timeout=None,
            start_serving=True):
        """A coroutine which creates a UNIX Domain Socket server.

        The return value is a Server object, which can be used to stop
        the service.

        path is a str, representing a file system path to bind the
        server socket to.

        sock can optionally be specified in order to use a preexisting
        socket object.

        backlog is the maximum number of queued connections passed to
        listen() (defaults to 100).

        ssl can be set to an SSLContext to enable SSL over the
        accepted connections.

        ssl_handshake_timeout is the time in seconds that an SSL server
        will wait for the SSL handshake to complete (defaults to 60s).

        start_serving set to True (default) causes the created server
        to start accepting connections immediately.  When set to False,
        the user should await Server.start_serving() or Server.serve_forever()
        to make the server to start accepting connections.
        """
        raise NotImplementedError

    async def connect_accepted_socket(
            self, protocol_factory, sock,
            *, ssl=None,
            ssl_handshake_timeout=None):
        """Handle an accepted connection.

        This is used by servers that accept connections outside of
        asyncio, but use asyncio to handle connections.

        This method is a coroutine.  When completed, the coroutine
        returns a (transport, protocol) pair.
        """
        raise NotImplementedError

    async def create_datagram_endpoint(self, protocol_factory,
                                       local_addr=None, remote_addr=None, *,
                                       family=0, proto=0, flags=0,
                                       reuse_address=None, reuse_port=None,
                                       allow_broadcast=None, sock=None):
        """A coroutine which creates a datagram endpoint.

        This method will try to establish the endpoint in the background.
        When successful, the coroutine returns a (transport, protocol) pair.

        protocol_factory must be a callable returning a protocol instance.

        socket family AF_INET, socket.AF_INET6 or socket.AF_UNIX depending on
        host (or family if specified), socket type SOCK_DGRAM.

        reuse_address tells the kernel to reuse a local socket in
        TIME_WAIT state, without waiting for its natural timeout to
        expire. If not specified it will automatically be set to True on
        UNIX.

        reuse_port tells the kernel to allow this endpoint to be bound to
        the same port as other existing endpoints are bound to, so long as
        they all set this flag when being created. This option is not
        supported on Windows and some UNIX's. If the
        :py:data:`~socket.SO_REUSEPORT` constant is not defined then this
        capability is unsupported.

        allow_broadcast tells the kernel to allow this endpoint to send
        messages to the broadcast address.

        sock can optionally be specified in order to use a preexisting
        socket object.
        """
        raise NotImplementedError

    # Pipes and subprocesses.

    async def connect_read_pipe(self, protocol_factory, pipe):
        """Register read pipe in event loop. Set the pipe to non-blocking mode.

        protocol_factory should instantiate object with Protocol interface.
        pipe is a file-like object.
        Return pair (transport, protocol), where transport supports the
        ReadTransport interface."""
        # The reason to accept file-like object instead of just file descriptor
        # is: we need to own pipe and close it at transport finishing
        # Can get complicated errors if pass f.fileno(),
        # close fd in pipe transport then close f and vise versa.
        raise NotImplementedError

    async def connect_write_pipe(self, protocol_factory, pipe):
        """Register write pipe in event loop.

        protocol_factory should instantiate object with BaseProtocol interface.
        Pipe is file-like object already switched to nonblocking.
        Return pair (transport, protocol), where transport support
        WriteTransport interface."""
        # The reason to accept file-like object instead of just file descriptor
        # is: we need to own pipe and close it at transport finishing
        # Can get complicated errors if pass f.fileno(),
        # close fd in pipe transport then close f and vise versa.
        raise NotImplementedError

    async def subprocess_shell(self, protocol_factory, cmd, *,
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE,
                               **kwargs):
        raise NotImplementedError

    async def subprocess_exec(self, protocol_factory, *args,
                              stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              **kwargs):
        raise NotImplementedError

    # Ready-based callback registration methods.
    # The add_*() methods return None.
    # The remove_*() methods return True if something was removed,
    # False if there was nothing to delete.

    def add_reader(self, fd, callback, *args):
        raise NotImplementedError

    def remove_reader(self, fd):
        raise NotImplementedError

    def add_writer(self, fd, callback, *args):
        raise NotImplementedError

    def remove_writer(self, fd):
        raise NotImplementedError

    # Completion based I/O methods returning Futures.

    async def sock_recv(self, sock, nbytes):
        raise NotImplementedError

    async def sock_recv_into(self, sock, buf):
        raise NotImplementedError

    async def sock_sendall(self, sock, data):
        raise NotImplementedError

    async def sock_connect(self, sock, address):
        raise NotImplementedError

    async def sock_accept(self, sock):
        raise NotImplementedError

    async def sock_sendfile(self, sock, file, offset=0, count=None,
                            *, fallback=None):
        raise NotImplementedError

    # Signal handling.

    def add_signal_handler(self, sig, callback, *args):
        raise NotImplementedError

    def remove_signal_handler(self, sig):
        raise NotImplementedError

    # Task factory.

    def set_task_factory(self, factory):
        raise NotImplementedError

    def get_task_factory(self):
        raise NotImplementedError

    # Error handlers.

    def get_exception_handler(self):
        raise NotImplementedError

    def set_exception_handler(self, handler):
        raise NotImplementedError

    def default_exception_handler(self, context):
        raise NotImplementedError

    def call_exception_handler(self, context):
        raise NotImplementedError

    # Debug flag management.

    def get_debug(self):
        raise NotImplementedError

    def set_debug(self, enabled):
        raise NotImplementedError


class AbstractEventLoopPolicy:
    """Abstract policy for accessing the event loop."""

    def get_event_loop(self):
        """Get the event loop for the current context.

        Returns an event loop object implementing the BaseEventLoop interface,
        or raises an exception in case no event loop has been set for the
        current context and the current policy does not specify to create one.

        It should never return None."""
        raise NotImplementedError

    def set_event_loop(self, loop):
        """Set the event loop for the current context to loop."""
        raise NotImplementedError

    def new_event_loop(self):
        """Create and return a new event loop object according to this
        policy's rules. If there's need to set this loop as the event loop for
        the current context, set_event_loop must be called explicitly."""
        raise NotImplementedError

    # Child processes handling (Unix only).

    def get_child_watcher(self):
        """ Get the watcher for child processes. """
        raise NotImplementedError

    def set_child_watcher(self, watcher):
        """Set the watcher for child processes."""
        raise NotImplementedError


class BaseDefaultEventLoopPolicy(AbstractEventLoopPolicy):
    """Default policy implementation for accessing the event loop.

    In this policy, each thread has its own event loop.  However, we
    only automatically create an event loop by default for the main
    thread; other threads by default have no event loop.

    Other policies may have different rules (e.g. a single global
    event loop, or automatically creating an event loop per thread, or
    using some other notion of context to which an event loop is
    associated).
    """

    _loop_factory = None

    class _Local(threading.local):
        _loop = None
        _set_called = False

    def __init__(self):
        self._local = self._Local()

    def get_event_loop(self):
        """Get the event loop for the current context.

        Returns an instance of EventLoop or raises an exception.
        """
        if (self._local._loop is None and
                not self._local._set_called and
                threading.current_thread() is threading.main_thread()):
            self.set_event_loop(self.new_event_loop())

        if self._local._loop is None:
            raise RuntimeError('There is no current event loop in thread %r.'
                               % threading.current_thread().name)

        return self._local._loop

    def set_event_loop(self, loop):
        """Set the event loop."""
        self._local._set_called = True
        assert loop is None or isinstance(loop, AbstractEventLoop)
        self._local._loop = loop

    def new_event_loop(self):
        """Create a new event loop.

        You must call set_event_loop() to make this the current event
        loop.
        """
        return self._loop_factory()


# Event loop policy.  The policy itself is always global, even if the
# policy's rules say that there is an event loop per thread (or other
# notion of context).  The default policy is installed by the first
# call to get_event_loop_policy().
_event_loop_policy = None

# Lock for protecting the on-the-fly creation of the event loop policy.
_lock = threading.Lock()


# _running_loop (_RunningLoop) is implemented in C:
#   Is of type PyRunningLoopHolder which is stored
#   in thread local storage (dict).
#   key - __asyncio_running_event_loop__, value - PyRunningLoopHolder *rl

# thread local storage dict:
#   _PyThreadState_GetDict(PyThreadState *tstate)
#   which returns a dictionary in which extensions can store
#   thread-specific state information.
#   Each extension should use a unique key to use to store state
#   in the dictionary. It's equivalent to thread local storage

# 'PyRunningLoopHolder *rl' is also cached
# in 'static PyObject *cached_running_holder'.
# And the thread's id that cached the running loop is stored in
# 'static volatile uint64_t cached_running_holder_tsid';

# PyRunningLoopHolder *rl:
#   1) holds a pointer to running loop (PyObject *rl_loop)
#   2) if NOT windows also holds the process id where the loop is running
#    (pid_t rl_pid)

# A TLS (thread local storage) for the running event loop,
# used by _get_running_loop.
class _RunningLoop(threading.local):
    loop_pid = (None, None)


_running_loop = _RunningLoop()


def get_running_loop():
    """Return the running event loop.  Raise a RuntimeError if there is none.

    This function is thread-specific.
    """
    # NOTE: this function is implemented in C (see _asynciomodule.c)
    loop = _get_running_loop()
    if loop is None:
        raise RuntimeError('no running event loop')
    return loop


def _get_running_loop():
    """Return the running event loop or None.

    This is a low-level function intended to be used by event loops.
    This function is thread-specific.

    C implementation (first look at the C implementation details
    in _set_running_loop defined below):
    _asyncio_get_running_loop_impl => get_running_loop:
        1) Gets the current thread state and thread's id;
        2) Check's whether cached_running_holder_tsid is equal to
           current thread id and if cached_running_holder points
           to a running loop holder
        3) If both above cases are true, then we get (return)
           the running loop straight from cached_running_holder.rl_loop
            (optimization).
        4) If the cases do not resolve to true, then we get thread local storage
           dict via _PyThreadState_GetDict(ts) and retrieve running loop holder
           by '___asyncio_running_event_loop__' key from it. If the running
           loop holder was not it thread local storage, None is returned.
           If loop holder was in storage we cache it in cached_running_holder
           and set cached_running_holder_tsid to current thread's id.
        5) After we've got a hold of the running loop holder we check
           that running loop holder is of type &PyRunningLoopHolder_Type
           (assertion error otherwise),
           .rl_loop of holder is not None (this function returns None if it is),
           on systems other than windows we also check that the current pid
           is equal to .rl_pid of holder
           (otherwise return None from this function)
        6) In the end _asyncio__get_running_loop_impl returns
           the currently running loop for this thread.
    """
    # NOTE: this function is implemented in C (see _asynciomodule.c)
    # does equivalent stuff to this pure python implementation in C
    running_loop, pid = _running_loop.loop_pid
    if running_loop is not None and pid == os.getpid():
        return running_loop


def _set_running_loop(loop):
    """Set the running event loop.

    This is a low-level function intended to be used by event loops.
    This function is thread-specific.

    C implementation:
    _asyncio__set_running_loop => set_running_loop (does something similar
    to this pure python implementation).
       1) Gets thread local storage dict via _PyThreadState_GetDict(tstate)
       2) Creates a new running event loop holder holding the loop and loop's
          process id (not on Windows) via
          PyRunningLoopHolder *rl = new_running_loop_holder(loop);
       3) Stores the event loop holder with the loop in the thread local storage
          dict (key: ___asyncio_running_event_loop__, value: (PyObject *)rl)
       4) Caches the running event loop in static PyObject *cached_running_holder
       5) Sets 'static volatile uint64_t cached_running_holder_tsid' equal to
          the thread id that set the running event loop.

       Steps 4 and 5 speed up the running event loop lookup.
       We can just check if *cached_running_holder is not NULL and
       cached_running_holder_tsid is equal to currently running/(requesting the
       loop) thread id - if so, we just return the running loop
       from *cached_running_holder without ever touching the thread
       local storage dict.

       This optimization is especially useful with the default event loop
       policy, where only the main thread has a running loop
       launched automatically.
       Not so useful with other policies where each thread can have it's
       own event loop, so cached_running_holder will hold the event loop
       of the thread that was spawned last (cached_running_holder_tsid will hold
       last spawned thread's id).
    """
    # NOTE: this function is implemented in C (see _asynciomodule.c)
    # does equivalent stuff to this pure python implementation in C
    _running_loop.loop_pid = (loop, os.getpid())


# TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#  AFTER _UnixDefaultEventLoopPolicy
def _init_event_loop_policy():
    global _event_loop_policy
    with _lock:
        if _event_loop_policy is None:  # pragma: no branch
            from . import DefaultEventLoopPolicy
            _event_loop_policy = DefaultEventLoopPolicy()


def get_event_loop_policy():
    """Get the current event loop policy."""
    if _event_loop_policy is None:
        _init_event_loop_policy()
    return _event_loop_policy


def set_event_loop_policy(policy):
    """Set the current event loop policy.

    If policy is None, the default policy is restored."""
    global _event_loop_policy
    assert policy is None or isinstance(policy, AbstractEventLoopPolicy)
    _event_loop_policy = policy


def get_event_loop():
    """Return an asyncio event loop.

    When called from a coroutine or a callback (e.g. scheduled with call_soon
    or similar API), this function will always return the running event loop.

    If there is no running event loop set, the function will return
    the result of `get_event_loop_policy().get_event_loop()` call.

    C implementation:
        _asyncio_get_event_loop_impl => get_event_loop

        1) first calls the C function get_running_loop and if the loop
           returned from it is not NULL, returns the loop
        2) If get_running_loop returned NULL, then calls the python
          implementation from this module of get_event_loop_policy
          and then calls the policy's method .get_event_loop()
    """
    # NOTE: this function is implemented in C (see _asynciomodule.c)
    # does equivalent stuff to this pure python implementation in C
    current_loop = _get_running_loop()
    if current_loop is not None:
        return current_loop
    return get_event_loop_policy().get_event_loop()


def set_event_loop(loop):
    """Equivalent to calling get_event_loop_policy().set_event_loop(loop)."""
    get_event_loop_policy().set_event_loop(loop)


def new_event_loop():
    """Equivalent to calling get_event_loop_policy().new_event_loop()."""
    return get_event_loop_policy().new_event_loop()


def get_child_watcher():
    """Equivalent to calling get_event_loop_policy().get_child_watcher()."""
    return get_event_loop_policy().get_child_watcher()


def set_child_watcher(watcher):
    """Equivalent to calling
    get_event_loop_policy().set_child_watcher(watcher)."""
    return get_event_loop_policy().set_child_watcher(watcher)


# Alias pure-Python implementations for testing purposes.
_py__get_running_loop = _get_running_loop
_py__set_running_loop = _set_running_loop
_py_get_running_loop = get_running_loop
_py_get_event_loop = get_event_loop

try:
    # get_event_loop() is one of the most frequently called
    # functions in asyncio.  Pure Python implementation is
    # about 4 times slower than C-accelerated.
    from _asyncio import (_get_running_loop, _set_running_loop,
                          get_running_loop, get_event_loop)
except ImportError:
    pass
else:
    # Alias C implementations for testing purposes.
    _c__get_running_loop = _get_running_loop
    _c__set_running_loop = _set_running_loop
    _c_get_running_loop = get_running_loop
    _c_get_event_loop = get_event_loop
