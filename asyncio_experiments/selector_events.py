import collections
import errno
import functools
import selectors
import socket
import warnings
import weakref

try:
    import ssl
except ImportError:  # pragma: no cover
    ssl = None

from . import base_events
from . import constants
from . import events
from . import protocols
from . import futures
from . import transports
from . import trsock
from .log import logger


# TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#  Add docstrings where necessary (after you get a hang of the whole picture)
#  Needs some high-level description docstrings


def _test_selector_event(selector, fd, event):
    # Test if the selector is monitoring 'event' events
    # for the file descriptor 'fd'.
    try:
        key = selector.get_key(fd)
    except KeyError:
        return False
    else:
        return bool(key.events & event)


def _check_ssl_socket(sock):
    if ssl is not None and isinstance(sock, ssl.SSLSocket):
        raise TypeError("Socket cannot be of type SSLSocket")


class BaseSelectorEventLoop(base_events.BaseEventLoop):
    """Selector event loop.

    See events.EventLoop for API specification.
    """

    def __init__(self, selector=None):
        super().__init__()

        if selector is None:
            selector = selectors.DefaultSelector()

        logger.debug('Using selector: %s', selector.__class__.__name__)
        self._selector = selector

        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        self._make_self_pipe()

        self._transports = weakref.WeakValueDictionary()

    def _make_socket_transport(self, sock, protocol, waiter=None, *,
                               extra=None, server=None):
        return _SelectorSocketTransport(self, sock, protocol, waiter,
                                        extra, server)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #   AFTER YOU INVESTIGATE SSL TRANSPORT
    # def _make_ssl_transport(
    #         self, rawsock, protocol, sslcontext, waiter=None,
    #         *, server_side=False, server_hostname=None,
    #         extra=None, server=None,
    #         ssl_handshake_timeout=constants.SSL_HANDSHAKE_TIMEOUT):
    #     ssl_protocol = sslproto.SSLProtocol(
    #             self, protocol, sslcontext, waiter,
    #             server_side, server_hostname,
    #             ssl_handshake_timeout=ssl_handshake_timeout)
    #     _SelectorSocketTransport(self, rawsock, ssl_protocol,
    #                              extra=extra, server=server)
    #     return ssl_protocol._app_transport

    def _make_datagram_transport(self, sock, protocol,
                                 address=None, waiter=None, extra=None):
        return _SelectorDatagramTransport(self, sock, protocol,
                                          address, waiter, extra)

    def close(self):
        if self.is_running():
            raise RuntimeError("Cannot close a running event loop")
        if self.is_closed():
            return
        # Unregisters read end of socket-pair and closes both sockets
        self._close_self_pipe()
        # Clears the queues (ready and scheduled (for delayed TimerHandles)
        # and shuts down the executor, but does not wait for
        # the executor to finish.
        super().close()
        if self._selector is not None:
            # 1) Closes the control file descriptor of
            #    epoll/devpoll/kqueue object
            # of the epoll object
            # 2) Clears the fd to SelectorKey dict together with the
            #    file object to SelectorKey mapping
            self._selector.close()
            self._selector = None

    def _close_self_pipe(self):
        """
        Unregisters reader socket from socket-pair (loop's selector)
        and closes both sockets.
        """
        self._remove_reader(self._ssock.fileno())
        self._ssock.close()
        self._ssock = None
        self._csock.close()
        self._csock = None
        self._internal_fds -= 1

    def _make_self_pipe(self):
        """
        Creates a pair of AF_UNIX sockets automatically connected
        with each other (on non-unix platforms will be a pair of AF_INET sockets)

        The read end socket is immediately registered to be polled for read
        events, this is used to wake up the event loop from a different thread
        or to process an OS signal:

        1) Wake up event loop scenario:
            If at some point in time the event loop while still being active
            has no pending timer handlers, ready handlers to process
            or fds to poll for I/O it would indefinitely block on polling nothing
            without any mechanism to resume it.
            This read end socket guarantees that there's always at least one fd
            to be polled by the event loop.
            Using this read end socket the event loop can always be woken
            up by writing a null byte to its corresponding write end socket.
            This mechanism is used by event loop's
            `.call_soon_threadsafe` method.

        2) Process an OS signal:
            TODO!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
              (example with loop._add_callback_signalsafe)
        """
        # A self-socket, really. :-)
        # on unix will be a pair of AF_UNIX sockets, on other platforms
        # will be a pair of AF_INET sockets
        # these sockets will be
        self._ssock, self._csock = socket.socketpair()
        self._ssock.setblocking(False)
        self._csock.setblocking(False)
        self._internal_fds += 1
        # adds as a reader `self._read_from_self` which will on read events
        # pass the data read onto `self._process_self_data` method
        self._add_reader(self._ssock.fileno(), self._read_from_self)

    def _process_self_data(self, data):
        """
        On Unix calls signal handler for every signum in `data`
        """
        pass

    def _read_from_self(self):
        """
        Reads all available data from a UNIX socket (chunks of 4096 bytes) until
        read will block and passes the data on to `self._process_self_data`
        method.
        """
        while True:
            try:
                data = self._ssock.recv(4096)
                if not data:
                    break
                self._process_self_data(data)
            except InterruptedError:
                continue
            except BlockingIOError:
                break

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    def _write_to_self(self):
        """
        Used to wake up the event loop
        """
        # This may be called from a different thread, possibly after
        # _close_self_pipe() has been called or even while it is
        # running.  Guard for self._csock being None or closed.  When
        # a socket is closed, send() raises OSError (with errno set to
        # EBADF, but let's not rely on the exact error code).
        csock = self._csock
        if csock is None:
            return

        try:
            csock.send(b'\0')
        except OSError:
            if self._debug:
                logger.debug("Fail to write a null byte into the "
                             "self-pipe socket",
                             exc_info=True)

    def _start_serving(self, protocol_factory, sock,
                       sslcontext=None, server=None, backlog=100,
                       ssl_handshake_timeout=constants.SSL_HANDSHAKE_TIMEOUT):
        """
        Just a wrapper around `self._accept_connection` which is itself
        a wrapper around `self._accept_connection2`
        """
        self._add_reader(sock.fileno(), self._accept_connection,
                         protocol_factory, sock, sslcontext, server, backlog,
                         ssl_handshake_timeout)

    def _accept_connection(
            self, protocol_factory, sock,
            sslcontext=None, server=None, backlog=100,
            ssl_handshake_timeout=constants.SSL_HANDSHAKE_TIMEOUT):
        # This method is only called once for each event loop tick where the
        # listening socket has triggered an EVENT_READ. There may be multiple
        # connections waiting for an .accept() so it is called in a loop.
        # See https://bugs.python.org/issue27906 for more details.
        for _ in range(backlog):
            try:
                conn, addr = sock.accept()
                if self._debug:
                    logger.debug("%r got a new connection from %r: %r",
                                 server, addr, conn)
                conn.setblocking(False)
            except (BlockingIOError, InterruptedError, ConnectionAbortedError):
                # Early exit because the socket accept buffer is empty.
                return None
            except OSError as exc:
                # There's nowhere to send the error, so just log it.
                # errno.EMFILE -  to many open files
                # errno.ENFILE - file table overflow
                # errno.ENOBUFS - no buffer space available
                # errno.ENOMEM - out of memory
                if exc.errno in (errno.EMFILE, errno.ENFILE,
                                 errno.ENOBUFS, errno.ENOMEM):
                    # Some platforms (e.g. Linux keep reporting the FD as
                    # ready, so we remove the read handler temporarily.
                    # We'll try again in a while.
                    self.call_exception_handler({
                        'message': 'socket.accept() out of system resource',
                        'exception': exc,
                        'socket': trsock.TransportSocket(sock),
                    })
                    self._remove_reader(sock.fileno())
                    self.call_later(constants.ACCEPT_RETRY_DELAY,
                                    self._start_serving,
                                    protocol_factory, sock, sslcontext, server,
                                    backlog, ssl_handshake_timeout)
                else:
                    raise  # The event loop will catch, log and ignore it.
            else:
                extra = {'peername': addr}
                # This is the actual coro that creates a transport for the
                # client connection and starts serving it in a Task
                accept = self._accept_connection2(
                    protocol_factory, conn, extra, sslcontext, server,
                    ssl_handshake_timeout)
                self.create_task(accept)

    async def _accept_connection2(
            self, protocol_factory, conn, extra,
            sslcontext=None, server=None,
            ssl_handshake_timeout=constants.SSL_HANDSHAKE_TIMEOUT):
        # conn - client connection (socket)

        protocol = None
        transport = None

        try:
            # create protocol instance
            protocol = protocol_factory()
            # To wait until protocol's `.connection_made` method will be called
            # and transport will ad client's socket as reader to loop's
            # 'selector'
            waiter = self.create_future()

            # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            if sslcontext:
                transport = self._make_ssl_transport(
                    conn, protocol, sslcontext, waiter=waiter,
                    server_side=True, extra=extra, server=server,
                    ssl_handshake_timeout=ssl_handshake_timeout)
            else:
                # waiter future will be unblocked (result set) after
                # protocol's `.connection_made` will be called and after
                # transport will add client socket as reader to event loop
                transport = self._make_socket_transport(
                    conn, protocol, waiter=waiter, extra=extra,
                    server=server)

            try:
                # unblocked after protocol's `.connection_made` called and
                # transport added client's socket as a reader to loop's
                # 'selector'
                await waiter
            except BaseException:
                transport.close()
                raise
                # It's now up to the protocol to handle the connection.
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if self._debug:
                context = {
                    'message':
                        'Error on transport creation for incoming connection',
                    'exception': exc,
                }
                if protocol is not None:
                    context['protocol'] = protocol
                if transport is not None:
                    context['transport'] = transport
                self.call_exception_handler(context)

    def _ensure_fd_no_transport(self, fd):
        fileno = fd
        # if not already a file descriptor,
        # try to get the underlying file like object's fd by calling .fileno(),
        # to ensure that this object really has an underlying fd,
        # if fails raise error
        if not isinstance(fileno, int):
            try:
                fileno = int(fileno.fileno())
            except (AttributeError, TypeError, ValueError):
                # This code matches selectors._fileobj_to_fd function.
                raise ValueError(f"Invalid file object: {fd!r}") from None

        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        # ensure that this fd isn't owned by a Transport
        try:
            transport = self._transports[fileno]
        except KeyError:
            pass
        else:
            # if it's this fd is owned by a Transport and it's not closing
            # raise an error
            if not transport.is_closing():
                raise RuntimeError(
                    f'File descriptor {fd!r} is used by transport '
                    f'{transport!r}')

    def _add_reader(self, fd, callback, *args):
        self._check_closed()
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        handle = events.Handle(callback, args, self, None)
        try:
            # if the file object/fd was previously registered with the selector
            # we will get it's corresponding SelectorKey
            key = self._selector.get_key(fd)
        # first time register
        except KeyError:
            # SelectorKey.data here is equal to a tuple of (reader, writer)
            # events.Handles (writer is None)
            self._selector.register(fd, selectors.EVENT_READ,
                                    (handle, None))
        # was already registered
        else:
            # extend the events bitmask and add a reader handle (keep the writer
            # handle if there was one registered previously)
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | selectors.EVENT_READ,
                                  (handle, writer))
            # if this call overrode the reader handle registered previously,
            # then cancel the old reader handle
            if reader is not None:
                reader.cancel()

        return handle

    def _remove_reader(self, fd):
        if self.is_closed():
            return False

        try:
            key = self._selector.get_key(fd)
        # fd not registered with the selector
        except KeyError:
            return False
        # fd is registered with the selector
        else:
            mask, (reader, writer) = key.events, key.data

            # if this fd was also registered as a writer, then the resulting
            # mask will be equal to 2, if only as a reader then the mask
            # will be equal to 0
            mask &= ~selectors.EVENT_READ

            # if registered only as a reader,
            # unregister this fd from the selector completely
            if not mask:
                self._selector.unregister(fd)

            # if was also registered as a writer, modify
            # the selector fd registration to track only write events and
            # discard the reader Handle, keeping only the writer Handle
            else:
                self._selector.modify(fd, mask, (None, writer))

            # if this fd was indeed registered as a reader, then
            # cancel its reader Handle
            if reader is not None:
                reader.cancel()
                return True
            # this fd was only registered as a writer,
            # so basically this method did nothing
            else:
                return False

    def _add_writer(self, fd, callback, *args):
        self._check_closed()
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        handle = events.Handle(callback, args, self, None)

        try:
            # if the file object/fd was previously registered with the selector
            # we will get it's corresponding SelectorKey
            key = self._selector.get_key(fd)
        # first time register
        except KeyError:
            # SelectorKey.data here is equal to a tuple of (reader, writer)
            # events.Handles (reader is None)
            self._selector.register(fd, selectors.EVENT_WRITE,
                                    (None, handle))
        # was already registered
        else:
            # extend the events bitmask and add a writer handle (keep the reader
            # handle if there was one registered previously)
            mask, (reader, writer) = key.events, key.data
            self._selector.modify(fd, mask | selectors.EVENT_WRITE,
                                  (reader, handle))

            # if this call overrode the writer Handle registered previously,
            # then cancel the old writer Handle
            if writer is not None:
                writer.cancel()

        return handle

    def _remove_writer(self, fd):
        """Remove a writer callback."""
        if self.is_closed():
            return False
        try:
            key = self._selector.get_key(fd)
        # fd not registered with the selector
        except KeyError:
            return False
        else:
            mask, (reader, writer) = key.events, key.data

            # Remove both writer and connector.
            # if this fd was also registered as a reader, then the resulting
            # mask will be equal to 1, if only as a writer then the mask
            # will be equal to 0
            mask &= ~selectors.EVENT_WRITE

            # if registered only as a writer,
            # unregister this fd from the selector completely
            if not mask:
                self._selector.unregister(fd)

            # if was also registered as a reader, modify
            # the selector fd registration to track only read events and
            # discard the writer Handle, keeping only the reader Handle
            else:
                self._selector.modify(fd, mask, (reader, None))

            # if this fd was indeed registered as a writer, then
            # cancel its reader Handle
            if writer is not None:
                writer.cancel()
                return True
            # this fd was only registered as a reader,
            # so basically this method did nothing
            else:
                return False

    def add_reader(self, fd, callback, *args):
        """Add a reader callback."""
        # TODO !!!! - add a detailed description in docstring
        # if fd:
        #   - isn't already a file descriptor
        #   - isn't an object that has a .fileno() method which
        #     returns the object's underlying file descriptor
        #   - is a Transport instance that is active (not closing)
        #  raise an exception
        self._ensure_fd_no_transport(fd)
        self._add_reader(fd, callback, *args)

    def remove_reader(self, fd):
        """Remove a reader callback."""
        # checkout .add_reader for details
        self._ensure_fd_no_transport(fd)
        return self._remove_reader(fd)

    def add_writer(self, fd, callback, *args):
        """Add a writer callback.."""
        # checkout .add_reader for details
        self._ensure_fd_no_transport(fd)
        self._add_writer(fd, callback, *args)

    def remove_writer(self, fd):
        """Remove a writer callback."""
        # checkout .add_reader for details
        self._ensure_fd_no_transport(fd)
        return self._remove_writer(fd)

    async def sock_recv(self, sock, n):
        """Receive data from the socket.

        The return value is a bytes object representing the data received.
        The maximum amount of data to be received at once is specified by
        nbytes.
        """
        # socket cannot be an instance of ssl.SSLSocket
        _check_ssl_socket(sock)
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")
        try:
            return sock.recv(n)
        # if receiving data cannot happen right away on first attempt and the
        # operation would block a BlockingIOError will be raised (or signal
        # interrupt)
        except (BlockingIOError, InterruptedError):
            pass

        # future representing the completion of this read operation,
        # call to `return await fut` will unblock when the read operation
        # completes, and if successful, the read data will be in
        # StopIteration's .value attr
        fut = self.create_future()
        fd = sock.fileno()

        # checkout .add_reader for details
        self._ensure_fd_no_transport(fd)

        # will register this socket fd for read events polling
        # when the read operation completes, the data read will be present on
        # fut as its result, and 'return await fut' will unblock returning the
        # read data
        handle = self._add_reader(fd, self._sock_recv, fut, sock, n)

        # when the future completes, meaning the read operation has completed,
        # the socket will be unregistered from selector, it will no longer
        # be polled for I/O
        fut.add_done_callback(
            functools.partial(self._sock_read_done, fd, handle=handle))

        # blocks until .recv operation completes and returnS the data read
        # or raises an exception if one occurred during .recv
        return await fut

    def _sock_read_done(self, fd, fut, handle=None):
        """
        This is a done callback usually added to a Future representing
        a read operation on a non-blocking socket.

        When the result is set on the future meaning that the socket
        is ready for reading
        (recv data, incoming client connection, etc.) this done callback
        will be called unregistering the fd from being tracked by the selector
        instance, or modifying the selector instance so that it stops
        tracking this fd for read events.
        """
        if handle is None or not handle.cancelled():
            self.remove_reader(fd)

    def _sock_recv(self, fut, sock, n):
        # _sock_recv() can add itself as an I/O callback if the operation can't
        # be done immediately. Don't use it directly, call sock_recv().
        # If sock.recv(n) completes successfully - reads all data of size `n`
        # the passed in future is completed and read data is set on it as its
        # result
        if fut.done():
            return
        try:
            data = sock.recv(n)
        except (BlockingIOError, InterruptedError):
            return  # try again next time
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(data)

    async def sock_recv_into(self, sock, buf):
        """Receive data from the socket.

        The received data is written into *buf* (a writable buffer).
        The return value is the number of bytes written.
        """
        # socket cannot be an instance of ssl.SSLSocket
        _check_ssl_socket(sock)
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")
        try:
            # if receiving data cannot happen right away on first attempt and the
            # operation would block a BlockingIOError will be raised (or signal
            # interrupt)
            return sock.recv_into(buf)
        except (BlockingIOError, InterruptedError):
            pass

        # future representing the completion of this read operation,
        # call to `return await fut` will unblock when the read operation
        # completes, and if successful, the info about the number of bytes read
        # will be in StopIteration's .value attr
        fut = self.create_future()
        fd = sock.fileno()
        self._ensure_fd_no_transport(fd)

        # will register this socket fd for read events polling
        # when the read operation completes, the data read will be in the buffer
        # and the number of read bytes will be present on
        # fut as its result, and 'return await fut' will unblock returning the
        # number of read bytes
        handle = self._add_reader(fd, self._sock_recv_into, fut, sock, buf)

        # when the future completes, meaning the read operation has completed,
        # the socket will be unregistered from selector, it will no longer
        # be polled for read I/O
        fut.add_done_callback(
            functools.partial(self._sock_read_done, fd, handle=handle))

        # blocks until .recv_into operation completes and returns the number
        # of bytes read into buffer or raises an exception if one
        # occurred during .recv_into
        return await fut

    def _sock_recv_into(self, fut, sock, buf):
        # _sock_recv_into() can add itself as an I/O callback if the operation
        # can't be done immediately. Don't use it directly, call
        # sock_recv_into().
        # If sock.recv(n) completes successfully - reads all data into `buf`
        # the passed in future is completed and the number of read bytes
        # is set on it as its result
        if fut.done():
            return
        try:
            nbytes = sock.recv_into(buf)
        except (BlockingIOError, InterruptedError):
            return  # try again next time
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(nbytes)

    async def sock_sendall(self, sock, data):
        """Send data to the socket.

        The socket must be connected to a remote socket. This method continues
        to send data from data until either all data has been sent or an
        error occurs. None is returned on success. On error, an exception is
        raised, and there is no way to determine how much data, if any, was
        successfully processed by the receiving end of the connection.
        """
        # socket cannot be an instance of ssl.SSLSocket
        _check_ssl_socket(sock)
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")
        try:
            # if send will block and only part of the data or no data was
            # sent
            n = sock.send(data)
        except (BlockingIOError, InterruptedError):
            n = 0

        if n == len(data):
            # all data sent
            return

        # future representing the completion of this write operation,
        # call to `return await fut` will unblock when all data is sent
        fut = self.create_future()
        fd = sock.fileno()
        self._ensure_fd_no_transport(fd)

        # use a trick with a list in closure to store a mutable state
        # [n] -  is stored in events.Handle's .args attr (which is a tuple, so
        # if we would store just `n` we wouldn't be able to modify it, as it's
        # a member of a tuple, but because it's a list [n], we can modify `n`
        # as a member of the list)

        # will register this socket fd for write events polling,
        # when the write operation completes - we have written all the data
        # (can take more than one selector polling 'session'),
        # 'return await fut' will unblock returning None
        handle = self._add_writer(fd, self._sock_sendall, fut, sock,
                                  memoryview(data), [n])

        # when the future completes, meaning the read operation has completed,
        # the socket will be unregistered from selector, it will no longer
        # be polled for 'write' I/O
        fut.add_done_callback(
            functools.partial(self._sock_write_done, fd, handle=handle))

        # blocks until all data has been sent or raises an exception if one
        # occurred during the sending process (sending all the data may require
        # multiple selector polling 'sessions' thus multiple sock.send operations
        return await fut

    def _sock_sendall(self, fut, sock, view, pos):
        if fut.done():
            # Future cancellation can be scheduled on previous loop iteration
            return
        # previous position in `data`
        start = pos[0]

        try:
            # start sending data from where we previously stopped
            #  (sending this data may require more than one selector polling
            # 'session')
            n = sock.send(view[start:])
        except (BlockingIOError, InterruptedError):
            return
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            return

        start += n

        # we have sent all the data - unblock the `await future' call
        if start == len(view):
            fut.set_result(None)
        else:
            # not all data sent yet,
            # increment position for next polling session
            pos[0] = start

    async def sock_connect(self, sock, address):
        """Connect to a remote socket at address.

        This method is a coroutine.

        1) If address is a domain name, resolves it using event loop's threadpool
        2)
        """
        # socket cannot be an instance of ssl.SSLSocket
        _check_ssl_socket(sock)
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")

        # if not an AF_UNIX socket, needs DNS resolving
        # 1) If address is already a resolved IP, then just extracts info
        #    from it for socket creation
        # 2) If not already an IP does a DNS request in loop's threadpool
        #    and returns info for socket creation and the underlying
        #    IP address and port
        # Return example:
        # (<AddressFamily.AF_INET: 2>, <SocketKind.SOCK_STREAM: 1>, 6,
        #         '', ('93.184.216.34', 80))
        if not hasattr(socket, 'AF_UNIX') or sock.family != socket.AF_UNIX:
            resolved = await self._ensure_resolved(
                address, family=sock.family, proto=sock.proto, loop=self)
            # address will be of the following format ('93.184.216.34', 80)
            _, _, _, _, address = resolved[0]

        fut = self.create_future()
        self._sock_connect(fut, sock, address)
        return await fut

    def _sock_connect(self, fut, sock, address):
        fd = sock.fileno()
        try:
            # socket is in non-blocking mode
            # so if connecting doesn't happen right away a BlockingIOError
            # is raised
            sock.connect(address)
        except (BlockingIOError, InterruptedError):
            # Issue #23618: When the C function connect() fails with EINTR, the
            # connection runs in background. We have to wait until the socket
            # becomes writable to be notified when the connection succeed or
            # fails.

            # checkout .add_reader for details
            self._ensure_fd_no_transport(fd)

            # callback self._sock_connect_cb will be called with
            # fut, sock and address, when the selector discovers a write
            # event on it. If the socket connected successfully, will complete
            # the future, if error - will set exception on future. This will
            # ultimately unblock the call to `return await fut` in
            # self.sock_connect(self, sock, address)
            handle = self._add_writer(
                fd, self._sock_connect_cb, fut, sock, address)

            # when future representing this connect operation completes or
            # gets an exception set on it, this socket fd will
            # be unregistered from being tracked for write events by the
            # selector
            fut.add_done_callback(
                functools.partial(self._sock_write_done, fd, handle=handle))
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
        # if connection happened right away during the first call to this
        # set result on the future immediately
        else:
            fut.set_result(None)

    def _sock_write_done(self, fd, fut, handle=None):
        if handle is None or not handle.cancelled():
            self.remove_writer(fd)

    def _sock_connect_cb(self, fut, sock, address):
        """
        When a socket connects (or seams like it):
        1) If error, set the error on the future
        2) If still not connected, retry this callback later
        3) If connected successfully, complete the future with a None result
        """
        if fut.done():
            return

        try:
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err != 0:
                # Jump to any except clause below.
                raise OSError(err, f'Connect call failed {address}')
        except (BlockingIOError, InterruptedError):
            # socket is still registered, the callback will be retried later
            pass
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            fut.set_exception(exc)
        else:
            fut.set_result(None)

    async def sock_accept(self, sock):
        """Accept a connection.

        The socket must be bound to an address and listening for connections.
        The return value is a pair (conn, address) where conn is a new socket
        object usable to send and receive data on the connection, and address
        is the address bound to the socket on the other end of the connection.
        """
        # socket cannot be an instance of ssl.SSLSocket
        _check_ssl_socket(sock)
        if self._debug and sock.gettimeout() != 0:
            raise ValueError("the socket must be non-blocking")

        fut = self.create_future()
        # 1) Tries to accept an incoming connection
        # 2) If no client is currently trying to connect, registers the socket
        #    with the selector and self._socket_accept(fut, sock) will be called
        #    again, when the selector discovers a read event on this socket's
        #    fd during polling
        # 3) Result will be set (client conn, address) on the `fut` when a client
        #    connects, which will call Task's ._wakeup => .__step method
        #    which will unblock the `await fut` call by doing coro.send(None)
        self._sock_accept(fut, sock)

        # blocks until an incoming client wants to connect
        # when unblocks, returns via StopIteration.value a tuple
        # (client conn, address)
        return await fut

    def _sock_accept(self, fut, sock):
        fd = sock.fileno()

        try:
            # listening sock is already in non-blocking mode
            conn, address = sock.accept()
            # set the client's conn socket to non-blocking mode
            conn.setblocking(False)
        # 1) if no client is trying to connect at the time of this call,
        #    a BlockingIOError will be raise,
        # 2) If a signal interrupt occurs during the call, InterruptedError
        #    will be raised
        except (BlockingIOError, InterruptedError):
            # checkout .add_reader for details
            self._ensure_fd_no_transport(fd)

            handle = self._add_reader(fd, self._sock_accept, fut, sock)

            # when client connects some time in the future,
            # tell the selector to stop tracking this fd for read events
            fut.add_done_callback(
                functools.partial(self._sock_read_done, fd, handle=handle))
        except (SystemExit, KeyboardInterrupt):
            raise
        # if non-IO related exception, just set it on the future
        except BaseException as exc:
            fut.set_exception(exc)
        # this will 'awake' the future (Task's ._wakeup => .__step method
        # will do coro.send as part of the future's done callback)
        else:
            # this can happen either on the first attempt, if a client
            # is immediately ready to connect, or when the selector
            # registers a read event on the listening socket
            fut.set_result((conn, address))

    async def _sendfile_native(self, transp, file, offset, count):
        # We delete transport from `self._transports` because sendfile may take
        # quite some time to complete and because of
        # that we need to pause the transport from reading but we still
        # want to allow polling for read events on the underlying socket's
        # fd while `sendfile` is in progress - for this we need to ensure
        # that when loop's `.add_reader` => `._ensure_fd_no_transport` methods
        # are called this transport is not part of the `self._transports`
        # weak value dictionary, otherwise a RuntimeError will be raised by
        # `._ensure_fd_no_transport` -
        #   f'File descriptor {fd!r} is used by transport {transport!r}'
        del self._transports[transp._sock_fd]
        # should the transport resume reading after `sendfile` completes -
        # we should if the transport isn't already paused or closing
        resume_reading = transp.is_reading()
        # if transport isn't already paused or closing signal to it to pause
        # reading (will unregister the transport's underlying socket fd from
        # being polled for read events by loop's selector)
        transp.pause_reading()
        # wait until all data will be flushed from write buffer,
        # if buffer is already empty will return immediately without blocking,
        # else will unblock when transport's `._write_ready` method is called
        # as a write events polling callback and all data is written from
        # transport's write buffer
        await transp._make_empty_waiter()
        try:
            # TODO - check out _UnixSelectorEventLoop implementation
            #   !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            # will use native sendfile if available or fallback to a user-space
            # python implementation
            return await self.sock_sendfile(transp._sock, file, offset, count,
                                            fallback=False)
        finally:
            # sets transport's `._empty_waiter` attribute to None
            transp._reset_empty_waiter()
            # if protocol wasn't paused or closing before sendfile
            # operation was issued
            if resume_reading:
                # resume reading - will again register the transport's
                # underlying socket fd to be polled for read events by
                # loop's selector (may overwrite user's read callback if
                # one was registered while `sendfile` was in progress)
                transp.resume_reading()
            # add transport to `self._transports` again so if someone tries
            # to register transport's socket fd to be polled for read events
            # after this not via the transport
            # loop's `.add_reader` => `._ensure_fd_no_transport` will raise
            # a RuntimeError -
            # f'File descriptor {fd!r} is used by transport {transport!r}'
            self._transports[transp._sock_fd] = transp

    # event list is a list of (SelectorKey, events bitmask)
    def _process_events(self, event_list):
        for key, mask in event_list:
            fileobj, (reader, writer) = key.fileobj, key.data
            # if a read event occurred and the file object was registered
            # as a reader
            if mask & selectors.EVENT_READ and reader is not None:
                # if events.Handle is cancelled:
                #   1) If fileobj was only a reader, completely unregister
                #      it from selector
                #   2) If was also a writer, just modify the selector, so it
                #      tracks only write events and the reader events.Handle
                #      in the SelectorKey;s data will be set to None
                if reader._cancelled:
                    self._remove_reader(fileobj)
                # add the reader events.Handle to the event loop's ._ready deque,
                # it will be called during this/current iteration of the loop
                else:
                    self._add_callback(reader)

            # if a write event occurred and the file object was registered
            # as a writer
            if mask & selectors.EVENT_WRITE and writer is not None:
                # if events.Handle is cancelled:
                #   1) If fileobj was only a writer, completely unregister
                #      it from selector
                #   2) If was also a reader, just modify the selector, so it
                #      tracks only read events and the writer events.Handle
                #      in the SelectorKey's data will be set to None
                if writer._cancelled:
                    self._remove_writer(fileobj)
                else:
                    self._add_callback(writer)

    #  TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    #   easy to understand, but need to find out what code uses it.
    # def _stop_serving(self, sock):
    #     self._remove_reader(sock.fileno())
    #     sock.close()


class _SelectorTransport(transports._FlowControlMixin, transports.Transport):
    max_size = 256 * 1024  # Buffer size passed to recv().

    _buffer_factory = bytearray  # Constructs initial value for self._buffer.

    # Attribute used in the destructor: it must be set even if the constructor
    # is not called (see _SelectorSslTransport which may start by raising an
    # exception)
    _sock = None

    def __init__(self, loop, sock, protocol, extra=None, server=None):
        # 1) BaseTransport sets self._extra = extra
        #    (this is optional transport info)
        # 2) FlowControlMixin sets:
        #    self._loop = loop
        #    self._protocol_paused = False
        #    self._set_write_buffer_limits() - in this case high and low water
        #    marks are not specified, so:
        #       self._high_water = 64KB
        #       self._low_water = 16KB
        super().__init__(extra, loop)

        # A socket-like wrapper for exposing real transport sockets
        # These objects can be safely returned by APIs like
        # `transport.get_extra_info('socket')`.  All potentially disruptive
        # operations (like "socket.close()") are banned
        self._extra['socket'] = trsock.TransportSocket(sock)

        try:
            # Return the socket’s own address. This is useful to find out
            # the port number of an IPv4/v6 socket, for instance.
            # (The format of the address returned depends on the address family)
            self._extra['sockname'] = sock.getsockname()
        except OSError:
            self._extra['sockname'] = None

        if 'peername' not in self._extra:
            try:
                # Return the remote address to which the socket is connected.
                # This is useful to find out the port number of a remote
                # IPv4/v6 socket, for instance.
                # (The format of the address returned depends
                # on the address family) On some systems this function
                # is not supported.
                self._extra['peername'] = sock.getpeername()
            except socket.error:
                self._extra['peername'] = None

        self._sock = sock
        self._sock_fd = sock.fileno()

        self._protocol_connected = False

        # sets self._protocol = protocol
        # sets self._protocol_connected = True
        self.set_protocol(protocol)

        # TODO - what is a server here?
        self._server = server

        # TODO - is a buffer for writing, but how exactly is it used?
        # constructs an empty bytearray (implementation specific)
        self._buffer = self._buffer_factory()

        # Incremented by 1 when  `connection_lost` scheduled
        # (for example in .close() call)
        self._conn_lost = 0
        # Set to True when close() or _force_close() were called.
        self._closing = False

        # TODO - how does the server work?
        if self._server is not None:
            self._server._attach()

        # TODO - how does the loop use ._transports?
        # loop._transports is a weakref.WeakValueDictionary
        loop._transports[self._sock_fd] = self

    def __repr__(self):
        info = [self.__class__.__name__]

        # add transport state to info
        if self._sock is None:
            info.append('closed')
        elif self._closing:
            info.append('closing')

        # add socket fd number to info
        info.append(f'fd={self._sock_fd}')

        # test if the transport was closed
        if self._loop is not None and not self._loop.is_closed():
            # test that this transport's socket fd is monitored by selector
            # for read events
            polling = _test_selector_event(
                self._loop._selector,
                self._sock_fd,
                selectors.EVENT_READ
            )

            # add whether this socket's fd is monitored by loop's selector
            # for read events to info
            if polling:
                info.append('read=polling')
            else:
                info.append('read=idle')

            # test that this transport's socket fd is monitored by selector
            # for write events
            polling = _test_selector_event(
                self._loop._selector,
                self._sock_fd,
                selectors.EVENT_WRITE
            )

            # prepare state info which depends on whether this socket's fd
            # is monitored by loop's selector for write events
            if polling:
                state = 'polling'
            else:
                state = 'idle'

            bufsize = self.get_write_buffer_size()
            # add info about write polling state and current write buffer
            # size
            info.append(f'write=<{state}, bufsize={bufsize}>')

        return '<{}>'.format(' '.join(info))

    def abort(self):
        # Clears buffer and removers writer if necessary
        # Removes reader if necessary
        # Schedules `.connection_lost` to be called on the underlying protocol
        # Closes underlying socket and removes all references
        self._force_close(None)

    def set_protocol(self, protocol):
        self._protocol = protocol  # noqa
        self._protocol_connected = True

    def get_protocol(self):
        return self._protocol

    def is_closing(self):
        return self._closing

    # TODO - add docstring with context where this method is used and how
    def close(self):
        # close() was already called
        if self._closing:
            return

        # stop polling this socket's fd for read events
        self._closing = True
        self._loop._remove_reader(self._sock_fd)

        # if buffer is empty, we remove writer, because there's no more data to
        # be written to the connection
        # We cannot remove writer when the buffer still contains data, because
        # this data must be flushed to the connection first
        if not self._buffer:
            self._conn_lost += 1
            self._loop._remove_writer(self._sock_fd)
            self._loop.call_soon(self._call_connection_lost, None)

    def __del__(self, _warn=warnings.warn):
        if self._sock is not None:
            _warn(f"unclosed transport {self!r}", ResourceWarning, source=self)
            self._sock.close()

    def _fatal_error(self, exc, message='Fatal error on transport'):
        # Should be called from exception handler only.
        if isinstance(exc, OSError):
            if self._loop.get_debug():
                logger.debug("%r: %s", self, message, exc_info=True)
        else:
            self._loop.call_exception_handler({
                'message': message,
                'exception': exc,
                'transport': self,
                'protocol': self._protocol,
            })

        # Clears buffer and removers writer if necessary
        # Removes reader if necessary
        # Schedules `.connection_lost` to be called on the underlying protocol
        # Closes underlying socket and removes all references
        self._force_close(exc)

    def _force_close(self, exc):
        # If _call_connection_lost or this method were already called,
        # just return immediately
        if self._conn_lost:
            return

        # If buffer is not empty at this point
        # clear it and remove writer (do not try to wait till buffer contents
        # are flushed to connection)
        if self._buffer:
            self._buffer.clear()
            self._loop._remove_writer(self._sock_fd)

        # if `.close()` wasn't called already and thus reader wasn't
        # already removed, remove the reader
        if not self._closing:
            self._closing = True
            self._loop._remove_reader(self._sock_fd)

        self._conn_lost += 1

        # will call connection_lost on the protocol and close the underlying
        # socket
        self._loop.call_soon(self._call_connection_lost, exc)

    def _call_connection_lost(self, exc):
        try:
            # Example with StreamReaderProtocol:
            # 1) If exc is None will signal EOF to StreamReader, if there
            #    was an exception will set an exception on it (calls to
            #    `await StreamReader.read() will unblock and read until eof
            #    or raise `exc`)
            # 2) Completes `._closed` future with exception or without it
            #    (used for blocking on `await StreamWriter.wait_closed()`)
            # 3) If protocol was paused via its `.pause_writing()` method
            #    and because of that someone is blocked on:
            #       StreamWriter.write(data)
            #       await StreamWriter.drain()
            #    completes `._drain_waiter` (which caused
            #    await StreamWriter.drain() to block) with or without exception
            #    effectively unblocking call to `await StreamWriter.drain()`.
            # 4) Removes references on the protocol to StreamWriter, StreamReader
            #    and transport
            if self._protocol_connected:
                self._protocol.connection_lost(exc)
        finally:
            self._sock.close()
            self._sock = None
            self._protocol = None
            self._loop = None
            # TODO - what does server._detach() do and what is a server anyway?
            server = self._server
            if server is not None:
                server._detach()
                self._server = None

    def get_write_buffer_size(self):
        return len(self._buffer)

    def _add_reader(self, fd, callback, *args):
        # is set to True when close() called
        if self._closing:
            return

        self._loop._add_reader(fd, callback, *args)


class _SelectorSocketTransport(_SelectorTransport):
    # TODO - add comment
    _start_tls_compatible = True
    # TODO - add comment
    _sendfile_compatible = constants._SendfileMode.TRY_NATIVE

    # TODO -  when is this transport's __init__ called?
    def __init__(
            self, loop, sock, protocol, waiter=None,
            extra=None, server=None
    ):
        # TODO - add comment
        self._read_ready_cb = None

        # 1) BaseTransport sets self._extra = extra
        #    (this is optional transport info)
        # 2) FlowControlMixin sets:
        #    self._loop = loop
        #    self._protocol_paused = False
        #    self._set_write_buffer_limits() - in this case high and low water
        #    marks are not specified, so:
        #       self._high_water = 64KB
        #       self._low_water = 16KB
        # 3) _SelectorTransport wraps socket into socket-like
        #    wrapper (trsock.TransportSocket) for exposing real transport
        #    sockets and adds it to self._extra
        # 4) _SelectorTransport adds socket name and peer name to extra
        # 5) _SelectorTransport sets:
        #         self._sock = sock
        #         self._sock_fd = sock.fileno()
        #         self._protocol = protocol
        #         self._protocol_connected = True
        #         self._server = server
        #         self._buffer to an empty bytearray
        #         # Set to 1 when call connection_lost scheduled
        #         # (for example in .close() call)
        #         self._conn_lost = 0
        #         self._closing = False  # Set to True when close() called.
        #         self._read_ready_cb will be either
        #         `self._read_ready__data_received` or
        #         `self._read_ready__get_buffer` depending on the protocol type
        # 6) Calls self._server._attach() if self._server is not None
        # 7) Adds self to loop's `._transports` weakref.WeakValueDictionary
        #    (loop._transports[self._sock_fd] = self)
        super().__init__(loop, sock, protocol, extra, server)

        # TODO - add comments
        self._eof = False
        self._paused = False
        self._empty_waiter = None

        # Disable the Nagle algorithm -- small writes will be
        # sent without waiting for the TCP ACK.  This generally
        # decreases the latency (in some cases significantly.)
        base_events._set_nodelay(self._sock)

        # protocol's `.connection_made` method will be called during event
        # loop's next iteration
        # 1) Sets this transport on the protocol
        # 2) Sets this transport on the protocol's StreamReader
        # 3) If a client connected callback was provided to this protocol:
        #    - creates StreamWriter passing it this transport, self
        #      and StreamReader
        #    - calls or creates and schedules a task with the client connected
        #      callback passing it StreamReader and StreamWriter
        # 4) Clears strong reference to StreamReader on the protocol
        #    (only weakref is left)
        self._loop.call_soon(self._protocol.connection_made, self)

        # TODO - add comments about self._read_ready, is set by self.set_protocol
        # only start reading when connection_made() has been called
        # (this works because of the event loop's anatomy - 'call_soon' handles
        #  will be called in the order in which they were registered with
        #  `.call_soon`)
        # This will register this socket's fd to be polled for read events
        # by the selector and self._read_ready to be called as a callback
        # when read events happen
        # self._read_ready will call self._read_ready_cb()
        self._loop.call_soon(self._add_reader,
                             self._sock_fd, self._read_ready)

        # If someone wants to wait for protocol's `.connection_made` method
        # to be called, he can pass a waiter future and block on awaiting
        # it, the future will be completed after protocol's `.connection_made`
        # is called during loop's next iteration, effectively unblocking
        # our caller.
        if waiter is not None:
            # only wake up the waiter when connection_made() has been called
            # (this works because of the event loop's anatomy - 'call_soon'
            # handles will be called in the order in which they were
            # registered with `.call_soon`)
            self._loop.call_soon(futures._set_result_unless_cancelled,
                                 waiter, None)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    def set_protocol(self, protocol):
        if isinstance(protocol, protocols.BufferedProtocol):
            self._read_ready_cb = self._read_ready__get_buffer
        else:
            self._read_ready_cb = self._read_ready__data_received

        super().set_protocol(protocol)

    def is_reading(self):
        return not self._paused and not self._closing

    def pause_reading(self):
        """
        Pause the receiving end - in case of StreamReaderProtocol is called
        by its underlying StreamReader's method `.feed_data` if the buffer
        size goes below high watermark.

        No data will be passed to the protocol's data_received()
        method until resume_reading() is called.

        The method is idempotent, i.e. it can be called when the transport
        is already paused or closed.
        """
        if self._closing or self._paused:
            return
        self._paused = True
        self._loop._remove_reader(self._sock_fd)
        if self._loop.get_debug():
            logger.debug("%r pauses reading", self)

    def resume_reading(self):
        """
        Resume the receiving end - in case of StreamReaderProtocol is called
        by its underlying StreamReader's methods -
        `.read()`/`.readline()`/`.readuntil()`/`.readexactly()` after some
        data has been read and the StreamReader's buffer maybe be ready to
        except more data because its size dropped below the threshold after
        this read.

        Data received will once again be passed to the protocol's
        data_received() method.

        The method is idempotent, i.e. it can be called when
        the transport is already reading.
        """
        if self._closing or not self._paused:
            return
        self._paused = False
        self._add_reader(self._sock_fd, self._read_ready)
        if self._loop.get_debug():
            logger.debug("%r resumes reading", self)

    # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
    def _read_ready(self):
        self._read_ready_cb()

    # TODO - add more detailed docstring
    def _read_ready__get_buffer(self):
        """
        This is the 'callback' called by 'selector' when read events occur
        on `self._sock` (`self._read_ready` => this method).
        Used with buffered protocols.
        Is assigned to  `self._read_ready_cb` via `self.set_protocol` method.
        For unbuffered protocol `self._read_ready__data_received` is
        used instead.
        """
        if self._conn_lost:
            return

        try:
            # There's no 'builtin' in asyncio protocol that implements
            # BufferedProtocol (for users to implement)

            # Called to allocate a new receive buffer.
            # *sizehint* is a recommended minimal size for the returned
            # buffer.  When set to -1, the buffer size can be arbitrary.
            # Must return an object that implements the
            # :ref:`buffer protocol <bufferobjects>`.
            # It is an error to return a zero-sized buffer.
            buf = self._protocol.get_buffer(-1)
            if not len(buf):
                raise RuntimeError('get_buffer() returned an empty buffer')
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(
                exc, 'Fatal error: protocol.get_buffer() call failed.')
            return

        try:
            # receive data into buffer provided by protocol
            # (instance of BufferedProtocol - for manual control of the
            # receive buffer)
            nbytes = self._sock.recv_into(buf)
        except (BlockingIOError, InterruptedError):
            return
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(exc, 'Fatal read error on socket transport')
            return

        # if no data was received, this means that our client closed connection
        # on his side
        if not nbytes:
            # Removes reader and feeds eof to the underlying protocol's
            # StreamReader
            # If protocol requested also removes writer and schedules
            #  self._call_connection_lost method to be called during
            # loop's next iteration
            self._read_ready__on_eof()
            return

        # There's no 'builtin' in asyncio protocol that implements
        # BufferedProtocol (for users to implement)
        try:
            # Called when the buffer was updated with the received data.
            # *nbytes* is the total number of bytes that were written to
            # the buffer.
            # User can implement this method so that calls to 'await read'
            # on this custom BufferedProtocol will unblock at this point
            # and return the data to the user (from the custom buffer in buffered
            # protocol)
            self._protocol.buffer_updated(nbytes)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(
                exc, 'Fatal error: protocol.buffer_updated() call failed.')

    # TODO - add more detailed docstring
    def _read_ready__data_received(self):
        """
        This is the 'callback' called by 'selector' when read events occur
        on `self._sock` (`self._read_ready` => this method).
        Used with unbuffered protocols.
        Is assigned to  `self._read_ready_cb` via `self.set_protocol` method.
        For protocols.BufferedProtocol `self._read_ready__get_buffer` is
        used instead.
        """
        if self._conn_lost:
            return

        try:
            data = self._sock.recv(self.max_size)
        except (BlockingIOError, InterruptedError):
            return  # try again next time
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            # calls event loop's exception handler
            # clears buffer and removers writer if necessary
            # Removes reader if necessary
            # Schedules `.connection_lost` to be called on the
            # underlying protocol
            # Closes underlying socket and removes all references
            self._fatal_error(exc, 'Fatal read error on socket transport')
            return

        # if no data was received, this means that our client closed connection
        # on his side
        if not data:
            # Removes reader and feeds eof to the underlying protocol's
            # StreamReader
            # If protocol requested also removes writer and schedules
            #  self._call_connection_lost method to be called during
            # loop's next iteration
            self._read_ready__on_eof()
            return

        try:
            # passes read data from connection to protocol, which in turn
            # feeds this data to its StreamReader - this unblocks
            # calls to
            # await `.read()`/`.readline()`/`.readuntil()`/`.readexactly()`
            # on the StreamReader
            self._protocol.data_received(data)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(
                exc, 'Fatal error: protocol.data_received() call failed.')

    def _read_ready__on_eof(self):
        if self._loop.get_debug():
            logger.debug("%r received EOF", self)

        try:
            # If this returns a False (including None), this transport
            # will close itself.  If it returns a True, closing this
            # transport is up to the protocol.
            keep_open = self._protocol.eof_received()
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(
                exc, 'Fatal error: protocol.eof_received() call failed.')
            return

        if keep_open:
            # We're keeping the connection open so the
            # protocol can write more, but we still can't
            # receive more, so remove the reader callback.
            self._loop._remove_reader(self._sock_fd)
        else:
            # 1) sets `self._closing` to True
            # 2) removes the reader callback
            # 3) removes writer callback if write buffer is not empty and
            # schedules self._call_connection_lost method to be called during
            # loop's next iteration
            self.close()

    def write(self, data):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f'data argument must be a bytes-like object, '
                            f'not {type(data).__name__!r}')
        if self._eof:
            raise RuntimeError('Cannot call write() after write_eof()')

        # TODO - what is self._empty_waiter and what is sendfile
        if self._empty_waiter is not None:
            raise RuntimeError('unable to write; sendfile is in progress')

        if not data:
            return

        if self._conn_lost:
            # After the connection is lost, log warnings after this many
            # write()s.
            if self._conn_lost >= constants.LOG_THRESHOLD_FOR_CONNLOST_WRITES:
                logger.warning('socket.send() raised exception.')
            self._conn_lost += 1
            return

        # if write buffer is currently empty
        # this also means that this transport's underlying socket
        # isn't registered to be polled for write events
        # (if the buffer isn't empty, this means that the socket is already
        # registered to be polled fro write events)
        if not self._buffer:
            # Optimization: try to send now.
            try:
                n = self._sock.send(data)
            except (BlockingIOError, InterruptedError):
                pass
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                self._fatal_error(exc, 'Fatal write error on socket transport')
                return
            else:
                # if at least a partial send succeeded do not send the
                # already sent data part next time
                data = data[n:]
                # if all data was sent
                if not data:
                    return
            # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            # Not all was written; register write handler.
            self._loop._add_writer(self._sock_fd, self._write_ready)

        # Add it to the buffer, will be picked up by `self._write_ready`
        # when a write event occurs while polling the socket
        self._buffer.extend(data)
        # if after writing the data to buffer it exceeds the high watermark,
        # pause the protocol from writing
        self._maybe_pause_protocol()

    def _write_ready(self):
        assert self._buffer, 'Data should not be empty'

        if self._conn_lost:
            return
        try:
            n = self._sock.send(self._buffer)
        except (BlockingIOError, InterruptedError):
            pass
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._loop._remove_writer(self._sock_fd)
            self._buffer.clear()
            self._fatal_error(exc, 'Fatal write error on socket transport')
            # TODO - what is self._empty_waiter
            if self._empty_waiter is not None:
                self._empty_waiter.set_exception(exc)
        else:
            if n:
                # remove from write buffer data that was sent
                del self._buffer[:n]

            # If underlying protocol was paused (paused writing) and the
            # current write buffer's content size has gone below the
            # low-water mark, we notify the protocol that it can
            # resume writing

            # Sets the `._paused` flag to False on the underlying protocol
            # and if someone previously called
            #   StreamWriter.write(data)
            #   await StreamWriter.drain()
            # which created a `._drain_waiter` future and blocked
            # on awaiting it,then this call completes the future
            # (if not already completed) effectively unblocking the
            # call to await StreamWriter.drain()
            self._maybe_resume_protocol()  # May append to buffer.

            # if all data from buffer was sent during this poll cycle
            if not self._buffer:
                self._loop._remove_writer(self._sock_fd)

                # TODO - what is self._empty_waiter
                if self._empty_waiter is not None:
                    self._empty_waiter.set_result(None)

                if self._closing:
                    # Example with StreamReaderProtocol:
                    # 1) If exc is None will signal EOF to StreamReader, if there
                    #    was an exception will set an exception on it (calls to
                    #    `await StreamReader.read() will unblock and
                    #    read until eof or raise `exc`)
                    # 2) Completes `._closed` future with exception or without it
                    #    (used for blocking on `await
                    #    StreamWriter.wait_closed()`)
                    # 3) If protocol was paused via its `.pause_writing()` method
                    #    and because of that someone is blocked on:
                    #       StreamWriter.write(data)
                    #       await StreamWriter.drain()
                    #    completes `._drain_waiter` (which caused
                    #    await StreamWriter.drain() to block) with or
                    #    without exception effectively unblocking call to
                    #    `await StreamWriter.drain()`.
                    # 4) Removes references on the protocol to
                    #    StreamWriter, StreamReader and transport
                    # 5) Closes this transport's underlying socket
                    # 6) Calls `._detach()` on `self._server`
                    # 7) Sets exception on `self._empty_waiter` -
                    #    ConnectionError("Connection is closed by peer")
                    self._call_connection_lost(None)
                # `self._eof` is set to True when `.write_eof()` is called
                # on this transport
                elif self._eof:
                    # Shut down write half of the connection -
                    # further sends are disallowed
                    self._sock.shutdown(socket.SHUT_WR)

    def write_eof(self):
        if self._closing or self._eof:
            return
        self._eof = True
        # if no more data is to be sent
        if not self._buffer:
            # Shut down write half of the connection -
            # further sends are disallowed
            self._sock.shutdown(socket.SHUT_WR)

    def can_write_eof(self):
        return True

    def _call_connection_lost(self, exc):
        # Example with StreamReaderProtocol:
        # 1) If exc is None will signal EOF to StreamReader, if there
        #    was an exception will set an exception on it (calls to
        #    `await StreamReader.read() will unblock and
        #    read until eof or raise `exc`)
        # 2) Completes `._closed` future with exception or without it
        #    (used for blocking on `await
        #    StreamWriter.wait_closed()`)
        # 3) If protocol was paused via its `.pause_writing()` method
        #    and because of that someone is blocked on:
        #       StreamWriter.write(data)
        #       await StreamWriter.drain()
        #    completes `._drain_waiter` (which caused
        #    await StreamWriter.drain() to block) with or
        #    without exception effectively unblocking call to
        #    `await StreamWriter.drain()`.
        # 4) Removes references on the protocol to
        #    StreamWriter, StreamReader and transport
        # 5) Closes this transport's underlying socket
        # 6) Calls `._detach()` on `self._server`
        super()._call_connection_lost(exc)
        # TODO - what is self._empty_waiter
        if self._empty_waiter is not None:
            self._empty_waiter.set_exception(
                ConnectionError("Connection is closed by peer"))

    def _make_empty_waiter(self):
        # TODO - what is self._empty_waiter used for
        if self._empty_waiter is not None:
            raise RuntimeError("Empty waiter is already set")
        self._empty_waiter = self._loop.create_future()
        # if buffer is already empty set the result immediately so user's
        # call to `await` will not block
        if not self._buffer:
            self._empty_waiter.set_result(None)
        return self._empty_waiter

    def _reset_empty_waiter(self):
        # TODO - what is self._empty_waiter used for
        self._empty_waiter = None


class _SelectorDatagramTransport(_SelectorTransport):
    # This buffer contains (<datagram data>, <address to send to>)
    _buffer_factory = collections.deque

    def __init__(self, loop, sock, protocol, address=None,
                 waiter=None, extra=None):
        # asyncio doesn't provide UDP protocol implementations out of the box

        # 1) BaseTransport sets self._extra = extra
        #    (this is optional transport info)
        # 2) FlowControlMixin sets:
        #    self._loop = loop
        #    self._protocol_paused = False
        #    self._set_write_buffer_limits() - in this case high and low water
        #    marks are not specified, so:
        #       self._high_water = 64KB
        #       self._low_water = 16KB
        # 3) _SelectorTransport wraps socket into socket-like
        #    wrapper (trsock.TransportSocket) for exposing real transport
        #    sockets and adds it to self._extra
        # 4) _SelectorTransport adds socket name and peer name to extra
        # 5) _SelectorTransport sets:
        #         self._sock = sock
        #         self._sock_fd = sock.fileno()
        #         self._protocol = protocol
        #         self._protocol_connected = True
        #         self._server = server
        #         self._buffer to an empty bytearray
        #         # Set to 1 when call connection_lost scheduled
        #         # (for example in .close() call)
        #         self._conn_lost = 0
        #         self._closing = False  # Set to True when close() called.
        # 6) Calls self._server._attach() if self._server is not None
        # 7) Adds self to loop's `._transports` weakref.WeakValueDictionary
        #    (loop._transports[self._sock_fd] = self)
        super().__init__(loop, sock, protocol, extra)

        self._address = address

        # protocol's `.connection_made` method will be called during event
        # loop's next iteration
        self._loop.call_soon(self._protocol.connection_made, self)

        # only start reading when connection_made() has been called
        # (this works because of the event loop's anatomy - 'call_soon' handles
        #  will be called in the order in which they were registered with
        #  `.call_soon`)
        # This will register this socket's fd to be polled for read events
        # by the selector and self._read_ready to be called as a callback
        # when read events happen
        self._loop.call_soon(self._add_reader,
                             self._sock_fd, self._read_ready)

        if waiter is not None:
            # only wake up the waiter when connection_made() has been called
            # (this works because of the event loop's anatomy - 'call_soon'
            # handles will be called in the order in which they were
            # registered with `.call_soon`)
            self._loop.call_soon(futures._set_result_unless_cancelled,
                                 waiter, None)

    def _read_ready(self):
        """
        Is called when a read event occurs on the socket, try's to read whole
        datagram (
            256 KB is much larger than max size of a datagram,

            The maximum safe UDP payload is 508 bytes,
            Any UDP payload this size or smaller is guaranteed
            to be deliverable over IP,
            Anything larger is allowed to be outright dropped by any router
            for any reason. Except on an IPv6-only route, where the maximum
            payload is 1,212 bytes.

            The maximum possible UDP payload is 67 KB, split into 45 IP packets,
            adding an additional 900 bytes of overhead (IPv4, MTU 1500,
            minimal 20-byte IP headers).
        ).

        If datagram was read during this call - passed it on to the
        DatagramProtocol.
        """
        if self._conn_lost:
            return
        try:
            # `self.max_size is 256 KB by default
            data, addr = self._sock.recvfrom(self.max_size)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as exc:
            self._protocol.error_received(exc)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._fatal_error(exc, 'Fatal read error on datagram transport')
        else:
            self._protocol.datagram_received(data, addr)

    def sendto(self, data, addr=None):
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f'data argument must be a bytes-like object, '
                            f'not {type(data).__name__!r}')
        if not data:
            return

        if self._address:
            if addr not in (None, self._address):
                raise ValueError(
                    f'Invalid address: must be None or {self._address}')
            addr = self._address

        # After the connection is lost, log warnings after this many
        # write()s. Only if this Datagram Transport is sort of bound to an
        # address
        if self._conn_lost and self._address:
            if self._conn_lost >= constants.LOG_THRESHOLD_FOR_CONNLOST_WRITES:
                logger.warning('socket.send() raised exception.')
            self._conn_lost += 1
            return

        if not self._buffer:
            # Attempt to send it right away first.
            try:
                #  If there is a remote address to which the socket is connected
                if self._extra['peername']:
                    self._sock.send(data)
                # else send to `self._address`
                else:
                    self._sock.sendto(data, addr)
                return
            except (BlockingIOError, InterruptedError):
                # Data wasn't sent during this call; register write handler.
                self._loop._add_writer(self._sock_fd, self._sendto_ready)
            except OSError as exc:
                # Only for DatagramProtocols
                # Called when a send or receive operation raises an OSError
                self._protocol.error_received(exc)
                return
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                self._fatal_error(
                    exc, 'Fatal write error on datagram transport')
                return

        # Ensure that what we buffer is immutable.
        # Datagram will be later retrieved by `self._sendto_ready()` when
        # socket is ready for writing
        self._buffer.append((bytes(data), addr))
        # If if size of write buffer does exceed the high water mark
        # and wasn't paused earlier, signal to the underlying DatagramProtocol
        # to pause writing
        self._maybe_pause_protocol()

    def _sendto_ready(self):
        # try sending datagrams from buffer until write operation will block
        while self._buffer:
            data, addr = self._buffer.popleft()
            try:
                #  If there is a remote address to which the socket is connected
                if self._extra['peername']:
                    self._sock.send(data)
                # else send to `self._address`
                else:
                    self._sock.sendto(data, addr)
            except (BlockingIOError, InterruptedError):
                self._buffer.appendleft((data, addr))  # Try again later.
                break
            except OSError as exc:
                # Only for DatagramProtocols
                # Called when a send or receive operation raises an OSError
                self._protocol.error_received(exc)
                return
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                self._fatal_error(
                    exc, 'Fatal write error on datagram transport')

        # If if size of write buffer dropped below the high water mark
        # and protocol was paused earlier, signal to the underlying
        # DatagramProtocol that it can resume writing
        self._maybe_resume_protocol()  # May append to buffer.

        # if all datagrams from buffer were sent during this call
        if not self._buffer:
            self._loop._remove_writer(self._sock_fd)
            if self._closing:
                # Calls `.connection_lost()` on the underlying DatagramProtocol
                # closes socket and calls `._detach()` on `self._server`
                self._call_connection_lost(None)
