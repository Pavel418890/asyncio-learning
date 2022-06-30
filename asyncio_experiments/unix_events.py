"""Selector event loop for Unix with signal handling."""
import errno
import itertools
import os
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import warnings

from . import base_subprocess
from . import events
from . import constants
from . import coroutines
from . import futures
from . import selector_events
from . import transports
from .log import logger


def waitstatus_to_exitcode(status):
    """
    - If the process exited normally (if WIFEXITED(status) is true), return the
      process exit status (return WEXITSTATUS(status)): result greater than
      or equal to 0.

    - If the process was terminated by a signal (if WIFSIGNALED(status) is true),
       return -signum where signum is the number of the signal that caused the
       process to terminate (return -WTERMSIG(status)): result less than 0.

    - Otherwise catch ValueError and return the original wait status
    """
    try:
        return os.waitstatus_to_exitcode(status)
    except ValueError:
        # The child exited, but we don't understand its status.
        # This shouldn't happen, but if it does, let's just
        # return that status; perhaps that helps debug it.
        return status


class _UnixSelectorEventLoop(selector_events.BaseSelectorEventLoop):
    """Unix event loop.

    Adds signal handling and UNIX Domain Socket support to SelectorEventLoop.
    """

    # TODO - ADD COMMENTS AND DOCSTRING TO THIS CLASS AND ALL SUPERCLASSES
    def __init__(self, selector=None):
        super().__init__(selector)
        self._signal_handlers = {}

    def add_signal_handler(self, sig, callback, *args):
        """Add a handler for a signal.  UNIX only.

        Raise ValueError if the signal number is invalid or uncatchable.
        Raise RuntimeError if there is a problem setting up the handler.
        """
        if (coroutines.iscoroutine(callback) or
                coroutines.iscoroutinefunction(callback)):
            raise TypeError("coroutines cannot be used "
                            "with add_signal_handler()")

        # validate that `sig` is a valid signal
        self._check_signal(sig)
        # validate that the event loop isn't closed
        self._check_closed()

    def _check_signal(self, sig):
        """Internal helper to validate a signal.

        Raise ValueError if the signal number is invalid or uncatchable.
        Raise RuntimeError if there is a problem setting up the handler.
        """
        if not isinstance(sig, int):
            raise TypeError(f'sig must be an int, not {sig!r}')

        if sig not in signal.valid_signals():
            raise ValueError(f'invalid signal number {sig}')


class _UnixReadPipeTransport(transports.ReadTransport):
    max_size = 256 * 1024  # max bytes we read in one event loop iteration

    def __init__(self, loop, pipe, protocol, waiter=None, extra=None):
        """
        Usually created by `loop._make_read_pipe_transport`.
            - `pipe` is an instance of subprocess.stdout, basically fd to which
              the subprocesses' stdout will be redirected, by default is a
              unix pipe (read end)
            - protocol - by default is usually an instance of
              base_subprocess.ReadSubprocessPipeProto

        1) Assigns all necessary attr
        2) Validates that `pipe` is indeed a unix pipe, socket or character
           device
        3) Sets pipe/socket to non-blocking I/O mode
        4) Arranges for base_subprocess.ReadSubprocessPipeProto's
          `.connection_made` method to be called during loop's next iteration,
           which just assigns this transport instance to its `.pipe` attr
        5) During loop's next iteration after
           base_subprocess.ReadSubprocessPipeProto's `.connection_made` method
           was called registers the underlying pipe to be polled for read events
        6) During loop's next iteration after the previous steps have
           completed completes the waiter future passed in by the caller
        """
        # assigns `extra` to `self._extra`
        super().__init__(extra)
        self._extra['pipe'] = pipe
        self._loop = loop
        # `pipe` is <Subprocess instance>.stdout attr
        # (usually the read end of a unix pipe)
        self._pipe = pipe
        self._fileno = pipe.fileno()
        # by default is usually an instance of
        # base_subprocess.ReadSubprocessPipeProto
        self._protocol = protocol
        self._closing = False
        self._paused = False

        # `os.fstat` returns information about the file/file-like resource,
        # using its fd
        # .st_mode - содержит биты  прав доступа  к файлу,  тип файла и
        # специальные биты
        mode = os.fstat(self._fileno).st_mode
        # if fd doesn't belong to a pipe, socket or character device,
        # raise exception
        if not (stat.S_ISFIFO(mode) or
                stat.S_ISSOCK(mode) or
                stat.S_ISCHR(mode)):
            self._pipe = None
            self._fileno = None
            self._protocol = None
            raise ValueError("Pipe transport is for pipes/sockets only.")

        os.set_blocking(self._fileno, False)

        # by default in base_subprocess.ReadSubprocessPipeProto just assigns
        # this transport instance to protocol's `.pipe` attr
        self._loop.call_soon(self._protocol.connection_made, self)
        # only start reading when connection_made() has been called
        # when a read event occurs reads at most 256KB and passes the data
        # on to base_subprocess.ReadSubprocessPipeProto's `.data_received`
        # method
        self._loop.call_soon(self._loop._add_reader,
                             self._fileno, self._read_ready)

        if waiter is not None:
            # only wake up the waiter when connection_made() has been called
            self._loop.call_soon(futures._set_result_unless_cancelled,
                                 waiter, None)

    def __repr__(self):
        info = [self.__class__.__name__]
        if self._pipe is None:
            info.append('closed')
        elif self._closing:
            info.append('closing')
        info.append(f'fd={self._fileno}')
        selector = getattr(self._loop, '_selector', None)
        if self._pipe is not None and selector is not None:
            polling = selector_events._test_selector_event(
                selector, self._fileno, selectors.EVENT_READ)
            if polling:
                info.append('polling')
            else:
                info.append('idle')
        elif self._pipe is not None:
            info.append('open')
        else:
            info.append('closed')
        return '<{}>'.format(' '.join(info))

    def _read_ready(self):
        """
        Called when a read event occurs on `self._pipe`
        (by default a read pipe handle replacing stdout to which a subprocess
        sends its results)

        1) Try to read at most 256KB from the underlying pipe
        2) If an OS error occurred, log exception and close this transport
        3) If data was read - pass it on to
           base_subprocess.ReadSubprocessPipeProto (by default)
        4) If eof was received (pipe was closed):
            - mark this transport as closing
            - stop polling the underlying pipe for read events
            - notify base_subprocess.ReadSubprocessPipeProto that eof was
              received, by default it does nothing
            - via `self._call_connection_lost` indirectly notifies
              user provided protocol that the underlying pipe was
              closed and if at this point all other pipes associated with
              unix_events._UnixSubprocessTransport notifies about this as well.
            - closes the underlying pipe
        """
        try:
            # read at most 256KB from pipe
            data = os.read(self._fileno, self.max_size)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError as exc:
            # log exception and close this transport
            self._fatal_error(exc, 'Fatal read error on pipe transport')
        else:
            # pass the data on to base_subprocess.ReadSubprocessPipeProto
            # (by default)
            if data:
                self._protocol.data_received(data)
            # eof, pipe connection was closed
            else:
                if self._loop.get_debug():
                    logger.info("%r was closed by peer", self)
                self._closing = True
                # stop monitoring closed pipe for read events
                self._loop._remove_reader(self._fileno)
                # by default base_subprocess.ReadSubprocessPipeProto
                # does nothing
                self._loop.call_soon(self._protocol.eof_received)
                self._loop.call_soon(self._call_connection_lost, None)

    def pause_reading(self):
        """
        Not called anywhere by asyncio, but can be called by user, to pause
        this transport from reading from pipe.
        Basically just sets the necessary flags on this transport and
        stop polling the underlying pipe for read events
        """
        if self._closing or self._paused:
            return
        self._paused = True
        self._loop._remove_reader(self._fileno)
        if self._loop.get_debug():
            logger.debug("%r pauses reading", self)

    def resume_reading(self):
        """
        Not called anywhere by asyncio, but can be called by user, to resume
        reading from underlying pipe by this transport.
        Basically just resumes polling the underlying pipe for read events.
        """
        if self._closing or not self._paused:
            return
        self._paused = False
        self._loop._add_reader(self._fileno, self._read_ready)
        if self._loop.get_debug():
            logger.debug("%r resumes reading", self)

    def set_protocol(self, protocol):
        self._protocol = protocol

    def get_protocol(self):
        return self._protocol

    def is_closing(self):
        return self._closing

    def close(self):
        """
        If not already closing:
            1) Marks this transport as closing
            2) Stops polling read end of the pipe for read events
            3) During event loop's next iteration:
                - indirectly notifies user provided protocol that this pipe was
                  closed and possibly also notifies that all pipes are already
                  closed if they are.
                - closes the underlying pipe
        """
        if not self._closing:
            self._close(None)

    def __del__(self, _warn=warnings.warn):
        """
        On mem free/GC warn user if this transport isn't closed and close
        the underlying pipe.
        """
        if self._pipe is not None:
            _warn(f"unclosed transport {self!r}", ResourceWarning, source=self)
            self._pipe.close()

    def _fatal_error(self, exc, message='Fatal error on pipe transport'):
        """
        On fatal error log a message and close this transport which:
            1) Marks this transport as closing
            2) Stops polling read end of the pipe for read events
            3) During event loop's next iteration:
                - indirectly notifies user provided protocol that this pipe was
                  closed and possibly also notifies that all pipes are already
                  closed if they are.
                - closes the underlying pipe
        """
        # should be called by exception handler only
        if (isinstance(exc, OSError) and exc.errno == errno.EIO):
            if self._loop.get_debug():
                logger.debug("%r: %s", self, message, exc_info=True)
        else:
            self._loop.call_exception_handler({
                'message': message,
                'exception': exc,
                'transport': self,
                'protocol': self._protocol,
            })
        self._close(exc)

    def _close(self, exc):
        """
        1) Marks this transport as closing
        2) Stops polling read end of the pipe for read events
        3) During event loop's next iteration:
            - indirectly notifies user provided protocol that this pipe was
              closed and possibly also notifies that all pipes are already closed
               if they are.
            - closes the underlying pipe
        """
        self._closing = True
        self._loop._remove_reader(self._fileno)
        self._loop.call_soon(self._call_connection_lost, exc)

    def _call_connection_lost(self, exc):
        """
        1) Calls `.connection_lost` on base_subprocess.ReadSubprocessPipeProto:
            In turn calls `._pipe_connection_lost` on
              unix_events._UnixSubprocessTransport which :
                - Calls `.pipe_connection_lost` on user-provided protocol,
                  basically notifies only about this pipe
                - Checks if maybe not only this pipe was closed but all pipes
                  associated with the subprocess, and if they are,
                  calls `.connection_lost` on user-provided protocol

        2) Closes read end of the pipe associated with transport
        """
        try:
            self._protocol.connection_lost(exc)
        finally:
            self._pipe.close()
            self._pipe = None
            self._protocol = None
            self._loop = None


class _UnixWritePipeTransport(transports._FlowControlMixin,
                              transports.WriteTransport):
    """
    Writes to this transport are usually happen in the following manner:
        user code =>
        <base_subprocess.BaseSubprocessTransport>.get_pipe_transport() =>
        <this _UnixWritePipeTransport>.write
    """

    def __init__(self, loop, pipe, protocol, waiter=None, extra=None):
        """
        Usually created by `loop.__make_write_pipe_transport` method:
            `pipe` - is an instance of subprocess.stdin, basically an fd to which
                     the subprocesses' stdin will be redirected, by default is a
                     socket pair
            `protocol` - by default is usually an instance of
                         base_subprocess.WriteSubprocessPipeProto

        1) Sets write buffer high watermark to 64KB and low watermark to 16KB

        2) Validates that provided `pipe` is a unix pipe, socket or character
           device

        3) Sets pipe/socket fd to non-blocking mode

        4) Schedules base_subprocess.WriteSubprocessPipeProto's `pipe` attr
           to be assigned to this transport instance during event loop's
           next iteration.

        4) After that starts tells the event loop to start polling the underlying
           pipe/socket for read events to get notified when the other end
           of the pipe/socket is closed by the subprocess - in reaction to this
           this transport will be forcefully closed.

        5) After registering the pipe/socket to be polled for read I/O,
           completes the waiter future passed in by `loop.connect_write_pipe`
           method to tell it that this pipe transport has finished its initial
           setup.
        """
        # 1) assigns `extra` to `self._extra`
        # 2) Sets `self._protocol_paused` to False
        # 3) Calls `self._set_write_buffer_limits()` which by default sets
        #    low watermark to 16KB and high water mark to 64KB
        #    (`self._low_water` and `self._high_water` respectively)
        super().__init__(extra, loop)

        # `pipe` is <Subprocess instance>.stdin attr
        # (usually a socket from a socket pair)
        self._extra['pipe'] = pipe
        self._pipe = pipe
        self._fileno = pipe.fileno()

        # by default is usually an instance of
        # base_subprocess.WriteSubprocessPipeProto
        self._protocol = protocol

        # corresponding protocol writes data to this buffer and this transports
        # writes data from it to the underlying pipe
        self._buffer = bytearray()

        self._conn_lost = 0
        self._closing = False  # Set when close() or write_eof() called.

        # `os.fstat` returns information about the file/file-like resource,
        # using its fd
        # .st_mode - содержит биты  прав доступа  к файлу,  тип файла и
        # специальные биты
        mode = os.fstat(self._fileno).st_mode
        # fd points to character device?
        is_char = stat.S_ISCHR(mode)
        # fd points to unix pipe?
        is_fifo = stat.S_ISFIFO(mode)
        # fd points to socket?
        is_socket = stat.S_ISSOCK(mode)

        # if `pipe` isn't a character device, unix pipe or socket,
        # raise and exception
        if not (is_char or is_fifo or is_socket):
            self._pipe = None
            self._fileno = None
            self._protocol = None
            raise ValueError("Pipe transport is only for "
                             "pipes, sockets and character devices")

        os.set_blocking(self._fileno, False)

        # by default in base_subprocess.WriteSubprocessPipeProto just assigns
        # this transport instance to protocol's `.pipe` attr
        self._loop.call_soon(self._protocol.connection_made, self)

        # We also poll pipe/socket for EOF read events to get notified
        # when the subprocess closes the read end of the pipe/socket act
        # accordingly and forcefully close this transport.
        ########################################################################
        # On AIX, the reader trick (to be notified when the read end of the
        # socket is closed) only works for sockets. On other platforms it
        # works for pipes and sockets. (Exception: OS X 10.4?  Issue #19294.)
        if is_socket or (is_fifo and not sys.platform.startswith("aix")):
            # only start reading when connection_made() has been called
            self._loop.call_soon(self._loop._add_reader,
                                 self._fileno, self._read_ready)

        if waiter is not None:
            # only wake up the waiter when connection_made() has been called
            self._loop.call_soon(futures._set_result_unless_cancelled,
                                 waiter, None)

    def __repr__(self):
        info = [self.__class__.__name__]
        if self._pipe is None:
            info.append('closed')
        elif self._closing:
            info.append('closing')
        info.append(f'fd={self._fileno}')
        selector = getattr(self._loop, '_selector', None)
        if self._pipe is not None and selector is not None:
            polling = selector_events._test_selector_event(
                selector, self._fileno, selectors.EVENT_WRITE)
            if polling:
                info.append('polling')
            else:
                info.append('idle')

            bufsize = self.get_write_buffer_size()
            info.append(f'bufsize={bufsize}')
        elif self._pipe is not None:
            info.append('open')
        else:
            info.append('closed')
        return '<{}>'.format(' '.join(info))

    def get_write_buffer_size(self):
        return len(self._buffer)

    def _read_ready(self):
        """
        This is a callback called when a read event occurs on the underlying
        socket/pipe, this can only happen if the pipe/socket was closed in
        the subprocess and thus an EOF read event occurred.

        If buffer still has data issue a forceful close with BrokenPipeError
        exception, else issue a forceful close without any exception:
            - stops polling for write events on the pipe/socket
            - removes all remaining data from buffer
            - stops polling for read events on the pipe/socket
            - closes underlying pipe/socket
            - notifies user-provided subprocess protocol that this pipe has been
              closed
        """
        # Pipe was closed by peer.
        if self._loop.get_debug():
            logger.info("%r was closed by peer", self)
        if self._buffer:
            self._close(BrokenPipeError())
        else:
            self._close()

    def write(self, data):
        """
        If underlying write buffer is currently empty, meaning that there's
        no I/O polling currently happening on the write end of the pipe/socket,
        straight away tries to send data through the pipe:
            1) All data was sent - just returns

            2) Not all or none of the data was written to pipe - remove
               already sent bytes from buffer

            3) Registers write end of the pipe/socket to be polled for I/O,
               and when a write event occurs remaining data will be sent of
               through the pipe/socket (may possibly require several attempts)

            4) Extends underlying write buffer with remaining data to be written

            5) Performs a check whether current buffer size doesn't
               exceed high watermark. If it does and the underlying
               base_subprocess.WriteSubprocessPipeProto instance isn't
               already paused, tells it to pause which proxies this request
               to user-provided subprocess protocol via calling its
               `.pause_writing()` method.
        """
        assert isinstance(data, (bytes, bytearray, memoryview)), repr(data)
        if isinstance(data, bytearray):
            data = memoryview(data)
        if not data:
            return

        if self._conn_lost or self._closing:
            if self._conn_lost >= constants.LOG_THRESHOLD_FOR_CONNLOST_WRITES:
                logger.warning('pipe closed by peer or '
                               'os.write(pipe, data) raised exception.')
            self._conn_lost += 1
            return

        # on first attempt try writing to pipe directly or if at
        # some point all data has been flushed from buffer to pipe
        if not self._buffer:
            # Attempt to send it right away first.
            try:
                n = os.write(self._fileno, data)
            except (BlockingIOError, InterruptedError):
                n = 0
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as exc:
                self._conn_lost += 1
                self._fatal_error(exc, 'Fatal write error on pipe transport')
                return

            # if all data was written just return immediately
            if n == len(data):
                return
            # not all data written, keep only non-written data
            elif n > 0:
                data = memoryview(data)[n:]

            # poll pipe for ability to write without blocking and write
            # data from buffer when poll is successful
            self._loop._add_writer(self._fileno, self._write_ready)

        # extend buffer with remaining data
        self._buffer += data
        self._maybe_pause_protocol()

    def _write_ready(self):
        """
        I/O polling for write events callback - called when a non-blocking write
        can be issued on the underlying pipe/socket. Writes data to pipe from
        buffer - possibly triggered multiple times if not all data can be written
        in one go.

        If all data from buffer was written during this call:
            1) Clears underlying buffer

            2) Tells the event loop's selector to stop polling for write events
               on this pipe/socket

            3) If if underlying base_subprocess.WriteSubprocessPipeProto
               was previously paused, tells it to resume writing which in turn
               proxies this request to user-provided subprocess protocol
               by calling its `.resume_writing()` method

            4) If in the mean time this transport was requested to be closed
               via calls to  `self.write_eof()`, `self.close()`:
                    - tell the event loop to stop polling the underlying
                      pipe/socket for read events. This polling was performed
                      to receive an EOF when the read end of the pipe is closed
                      in the subprocess and to act accordingly when this happens
                      by forcefully closing this transport.

                    - Close write end of the underlying unix pipe/socket and
                      during event loop's next iteration indirectly notify
                      user-provided subprocess protocol by calling its
                      `.pipe_connection_lost` method that this transport has
                      been closed.

                    - If at this point not only this pipe transport but all other
                      pipe transports attached to the instance of
                      unix_events._UnixSubprocessTransport are also closed,
                      notify user-provided protocol by calling its
                      `.connection_lost` method that its subprocess transport
                      has been closed.
        """
        assert self._buffer, 'Data should not be empty'

        try:
            n = os.write(self._fileno, self._buffer)
        except (BlockingIOError, InterruptedError):
            pass
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            self._buffer.clear()
            self._conn_lost += 1
            # Remove writer here, _fatal_error() doesn't
            # because _buffer is empty.
            self._loop._remove_writer(self._fileno)
            self._fatal_error(exc, 'Fatal write error on pipe transport')
        else:
            # all data written from buffer
            if n == len(self._buffer):
                self._buffer.clear()
                self._loop._remove_writer(self._fileno)
                self._maybe_resume_protocol()  # May append to buffer.
                if self._closing:
                    self._loop._remove_reader(self._fileno)
                    self._call_connection_lost(None)
                return
            # not all data was written, so remove written data from buffer,
            # and keep the rest
            elif n > 0:
                del self._buffer[:n]

    def can_write_eof(self):
        return True

    def write_eof(self):
        """
        Set 'closing' flag and only if the underlying buffer is currently
        empty stop polling pipe/socket for read events, close pipe/socket
        and notify user-provided protocol about lost connection.


        Why only when the underlying buffer is empty?

            This means that there will be no more writes to pipe/socket and we
            can safely close this transport, otherwise we need to postpone
            this transport's termination until all data from buffer
            has been flushed to pipe/socket and after that `self._write_ready`
            callback method will stop polling for write events and notice
            the 'closing' flag and do the necessary cleanup
            (stop polling for read events, close pipe,
            notify user-provided protocol)


        If underlying buffer is currently empty:

            1) Stop polling pipe for read events - no more need to get notified
               when the socket/pipe read end gets closed, after this call
               completes this transport will be considered closed.

            2) Close underlying pipe/socket

            3) During event loop's next iteration indirectly notify user-provided
               subprocess protocol by calling its `.pipe_connection_lost` method
               that this transport has been closed.

               If at this point not only this pipe transport but all other pipe
               transports attached to the instance of
               unix_events._UnixSubprocessTransport are also closed,
               indirectly notify user-provided protocol by calling its
               `.connection_lost` method that all pipes are closed and thus
               the subprocess transport is closed too.
        """
        if self._closing:
            return
        assert self._pipe
        self._closing = True
        if not self._buffer:
            self._loop._remove_reader(self._fileno)
            self._loop.call_soon(self._call_connection_lost, None)

    def set_protocol(self, protocol):
        self._protocol = protocol

    def get_protocol(self):
        return self._protocol

    def is_closing(self):
        return self._closing

    def close(self):
        """
        Graceful close - allows all data from buffer to be written to pipe
        and only then closes this transport.

        If not already closed or closing:

            Set 'closing' flag and only if the underlying buffer is currently
            empty stop polling pipe/socket for read events, close pipe/socket
            and notify user-provided protocol about lost connection.
            If the buffer still has some data, postpone termination until all
            data from buffer has been flushed to pipe.

            So this is basically a graceful close.

        For more details check out docstring for `self.write_eof` method.
        """
        if self._pipe is not None and not self._closing:
            # write_eof is all what we needed to close the write pipe
            self.write_eof()

    def __del__(self, _warn=warnings.warn):
        """
        On GC if this transport is till active, log a warning and abruptly
        close the underlying pipe/socket.
        """
        if self._pipe is not None:
            _warn(f"unclosed transport {self!r}", ResourceWarning, source=self)
            self._pipe.close()

    def abort(self):
        """
        Forceful close but without exception - doesn't flush remaining data
        from buffer to pipe before closing, but:
            - stops polling for write events on the pipe/socket
            - removes all remaining data from buffer
            - stops polling for read events on the pipe/socket
            - closes underlying pipe/socket
            - notifies user-provided subprocess protocol that this pipe has been
              closed
        """
        self._close(None)

    def _fatal_error(self, exc, message='Fatal error on pipe transport'):
        """
        Logs fatal error and then does a forceful close:
            - stops polling for write events on the pipe/socket
            - removes all remaining data from buffer
            - stops polling for read events on the pipe/socket
            - closes underlying pipe/socket
            - notifies user-provided subprocess protocol that this pipe has been
              closed
        """
        # should be called by exception handler only
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
        self._close(exc)

    def _close(self, exc=None):
        """
        Forceful close - doesn't flush remaining data from buffer to pipe before
        closing, but:
            - stops polling for write events on the pipe/socket
            - removes all remaining data from buffer
            - stops polling for read events on the pipe/socket
            - closes underlying pipe/socket
            - notifies user-provided subprocess protocol that this pipe has been
              closed
        """
        self._closing = True
        # there's a need to stop polling for write events only if the buffer
        # has some data, because otherwise `self._write_ready` has already
        # stopped polling when the buffer became empty.
        if self._buffer:
            self._loop._remove_writer(self._fileno)
        self._buffer.clear()
        self._loop._remove_reader(self._fileno)
        self._loop.call_soon(self._call_connection_lost, exc)

    def _call_connection_lost(self, exc):
        """
        1) Stop polling pipe for read events - no more need to get notified
           when the socket/pipe read end gets closed, after this call
           completes this transport will be considered closed.

        2) Close underlying pipe/socket

        3) During event loop's next iteration indirectly notify user-provided
           subprocess protocol by calling its `.pipe_connection_lost` method
           that this transport has been closed.

           If at this point not only this pipe transport but all other pipe
           transports attached to the instance of
           unix_events._UnixSubprocessTransport are also closed,
           indirectly notify user-provided protocol by calling its
           `.connection_lost` method that all pipes are closed and thus
           the subprocess transport is closed too.
        """
        try:
            self._protocol.connection_lost(exc)
        finally:
            self._pipe.close()
            self._pipe = None
            self._protocol = None
            self._loop = None


class _UnixSubprocessTransport(base_subprocess.BaseSubprocessTransport):
    def _start(self, args, shell, stdin, stdout, stderr, bufsize, **kwargs):
        stdin_w = None

        if stdin == subprocess.PIPE:
            # Use an AF_UNIX socket pair for stdin, since not all platforms
            # support selecting read events on the write end of a
            # socket (which we use in order to detect closing of the
            # other end).  Notably this is needed on AIX, and works
            # just fine on other platforms.
            # Selecting read events on the write end of a socket is done by
            # unix_events._UnixWritePipeTransport which issues a forceful
            # close of itself, for example when the subprocess
            # closes its stdin socket, which usually means that the
            # subprocess has terminated.
            stdin, stdin_w = socket.socketpair()

        try:
            self._proc = subprocess.Popen(
                args, shell=shell, stdin=stdin, stdout=stdout, stderr=stderr,
                universal_newlines=False, bufsize=bufsize, **kwargs)
        finally:
            pass


class AbstractChildWatcher:
    """Abstract base class for monitoring child processes.

    Objects derived from this class monitor a collection of subprocesses and
    report their termination or interruption by a signal.

    New callbacks are registered with .add_child_handler(). Starting a new
    process must be done within a 'with' block to allow the watcher to suspend
    its activity until the new process if fully registered (this is needed to
    prevent a race condition in some implementations).

    Example:
        with watcher:
            proc = subprocess.Popen("sleep 1")
            watcher.add_child_handler(proc.pid, callback)

    Notes:
        Implementations of this class must be thread-safe.

        Since child watcher objects may catch the SIGCHLD signal and call
        waitpid(-1), there should be only one active object per process.
    """

    def add_child_handler(self, pid, callback, *args):
        """Register a new child handler.

        Arrange for callback(pid, returncode, *args) to be called when
        process 'pid' terminates. Specifying another callback for the same
        process replaces the previous handler.

        Note: callback() must be thread-safe.
        """
        raise NotImplementedError()

    def remove_child_handler(self, pid):
        """Removes the handler for process 'pid'.

        The function returns True if the handler was successfully removed,
        False if there was nothing to remove."""

        raise NotImplementedError()

    def attach_loop(self, loop):
        """Attach the watcher to an event loop.

        If the watcher was previously attached to an event loop, then it is
        first detached before attaching to the new loop.

        Note: loop may be None.
        """
        raise NotImplementedError()

    def close(self):
        """Close the watcher.

        This must be called to make sure that any underlying resource is freed.
        """
        raise NotImplementedError()

    def is_active(self):
        """Return ``True`` if the watcher is active and is used by
           the event loop.

        Return True if the watcher is installed and ready to handle process exit
        notifications.
        """
        raise NotImplementedError()

    def __enter__(self):
        """Enter the watcher's context and allow starting new processes

        This function must return self"""
        raise NotImplementedError()

    def __exit__(self, a, b, c):
        """Exit the watcher's context"""
        raise NotImplementedError()


class PidfdChildWatcher(AbstractChildWatcher):
    """Child watcher implementation using Linux's pid file descriptors.

    This child watcher polls process file descriptors (pidfds) to await child
    process termination. In some respects, PidfdChildWatcher is a "Goldilocks"
    child watcher implementation. It doesn't require signals or threads, doesn't
    interfere with any processes launched outside the event loop, and scales
    linearly with the number of subprocesses launched by the event loop. The
    main disadvantage is that pidfds are specific to Linux, and only work on
    recent (5.3+) kernels.

    https://kernel-recipes.org/en/2019/talks/pidfds-process-file-descriptors-on-linux/
    https://copyconstruct.medium.com/file-descriptor-transfer-over-unix-domain-sockets-dcbbf5b3b6ec
    https://copyconstruct.medium.com/seamless-file-descriptor-transfer-between-processes-with-pidfd-and-pidfd-getfd-816afcd19ed4
    """

    def __init__(self):
        self._loop = None
        # mapping of pid to (pidfd, callback, args)
        self._callbacks = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        pass

    def is_active(self):
        return self._loop is not None and self._loop.is_running()

    def close(self):
        """
        1) Stops polling all pidfds and closes them
        2) Clears all callbacks
        3) Sets this watcher's loop reference to None
        """
        self.attach_loop(None)

    def attach_loop(self, loop):
        """
        1) If there still exist running subprocesses watched by this child
           watcher raise a warning if `loop` argument is None

        2) Stop polling all currently active pidfds and close them

        3) Clear all callbacks

        4) Set new loop
        """
        if self._loop is not None and loop is None and self._callbacks:
            warnings.warn(
                'A loop is being detached '
                'from a child watcher with pending handlers',
                RuntimeWarning)

        # stop polling all current pidfds for read events and close them
        for pidfd, _, _ in self._callbacks.values():
            self._loop._remove_reader(pidfd)
            os.close(pidfd)

        # clear callbacks mapping
        self._callbacks.clear()
        # attach to new loop
        self._loop = loop

    def add_child_handler(self, pid, callback, *args):
        """
        Adds pid key to `self._callbacks` mapping with a value of
        (pidfd, callback, args). The value will be retrieved later
        when a read event occurs on pidfd when the subprocess terminates.

        1) If subprocess with provided pid is already registered,
           substitute its callback and args in the mapping but keep the original
           pidfd

        2) If subprocess with provided pid wasn't registered earlier,
           call `os.pidfd_open(pid)` which returns a file descriptor referring
           to the process whose pid is specified in `pid`. This is a stable
           reference to the process.

        3) Register pidfd to be polled by loop's selector for read events,
           the read event will occur when the subprocess terminates.
           Read event callback will extract subprocesses' exitcode and
           call user's callback with `pid, returncode, *args`

        4) Add key-value pair of pid-(pidfd, callback, args) to
           `self._callbacks` mapping
        """
        existing = self._callbacks.get(pid)
        # if a child handler was already registered for this pid,
        # only replace callback and args keeping the original pidfd
        if existing is not None:
            self._callbacks[pid] = existing[0], callback, args
        else:
            # returns a file descriptor referring to the process whose
            # pid is specified in `pid`
            pidfd = os.pidfd_open(pid)
            self._loop._add_reader(pidfd, self._do_wait, pid)
            self._callbacks[pid] = pidfd, callback, args

    def _do_wait(self, pid):
        """
        Is triggered by a read event on the pidfd corresponding
        to processes' pid, the read event on the pidfd occurs when its
        corresponding subprocess terminates.

        The pidfd is registered as a 'reader` with `._do_wait` callback
        in `self.add_child_handler` method.

        1) Unregisters pidfd from being polled for read events by loop's
           selector - subprocess has terminated, no need to monitor its pidfd
           for read events

        2) Calls `os.waitpid` on pid to retrieve subprocesses exit status,
           will not block.

        3) Converts exit status to exitcode

        4) Closes pidfd and calls user provided callback previously registered
           via `self.add_child_handler` method passing it
                `pid, returncode, *args`
        """
        pidfd, callback, args = self._callbacks.pop(pid)
        # subprocess has terminated, no need to monitor its pidfd for read
        # events
        self._loop._remove_reader(pidfd)

        try:
            # should not block at this point, is used to retrieve terminated
            # subprocesses' exit status
            _, status = os.waitpid(pid, 0)
        except ChildProcessError:
            # The child process is already reaped
            # (may happen if waitpid() is called elsewhere).
            returncode = 255
            logger.warning(
                "child process pid %d exit status already read: "
                " will report returncode 255",
                pid)

        else:
            returncode = waitstatus_to_exitcode(status)

        os.close(pidfd)
        callback(pid, returncode, *args)

    def remove_child_handler(self, pid):
        """
        1) Removes callback from `self._callbacks` mapping

        2) Stops polling the corresponding pidfd for read events and closes
           it
        """
        try:
            pidfd, _, _ = self._callbacks.pop(pid)
        except KeyError:
            return False

        self._loop._remove_reader(pidfd)
        os.close(pidfd)

        return True


class ThreadedChildWatcher(AbstractChildWatcher):
    """Threaded child watcher implementation.

    The watcher uses a thread per process
    for waiting for the process finish.

    It doesn't require subscription on POSIX signal
    but a thread creation is not free.

    The watcher has O(1) complexity, its performance doesn't depend
    on amount of spawn processes.

    Default watcher used by _UnixDefaultEventLoopPolicy if not specified
    otherwise.
    """

    def __init__(self):
        # for naming thread objects incrementally
        self._pid_counter = itertools.count(0)
        # keep track of currently running child watcher threads
        self._threads = {}

    def is_active(self):
        return True

    def close(self):
        """
        Wait for all running and non-daemonic subprocess watcher threads
        to complete (by default this watcher only creates daemonic threads).
        """
        self._join_threads()

    def _join_threads(self):
        """
        Internal: Join all non-daemon threads

        Join on all running non-daemonic threads of this child watcher.
        """
        threads = [thread for thread in list(self._threads.values())
                   if thread.is_alive() and not thread.daemon]
        for thread in threads:
            thread.join()

    def __enter__(self):
        """
        Does nothing except of returning `self` because this watcher doesn't
        need to suspend its activity until the new process if fully registered
        (this is implementation doesn't cause race conditions).
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Does nothing because this watcher doesn't need to suspend its activity
        until the new process if fully registered (this is implementation
        doesn't cause race conditions).
        """
        pass

    def __del__(self, _warn=warnings.warn):
        """
        When this child watcher gets garbage collected if there are alive
        threads waiting for subprocesses to terminate, log a warning about
        this fact.
        """
        threads = [thread for thread in list(self._threads.values())
                   if thread.is_alive()]
        if threads:
            _warn(
                f"{self.__class__} has registered but not finished "
                f"child processes",
                ResourceWarning,
                source=self
            )

    def add_child_handler(self, pid, callback, *args):
        """
        Arrange for callback(loop, pid, returncode, args) to be called when
        process 'pid' terminates.

        Runs `._do_waitpid` method in a daemonic thread and adds the thread
        to a subprocess pid to thread object mapping.

        `._do_waitpid` - waits indefinitely for the subprocess to exit,
        then calls callback(loop, pid, returncode, args) during event loop's
        next iteration waking the loop up if necessary by writing a 0 byte to
        self-pipe. Removes the thread from the subprocess pid to thread object
        mapping.
        """
        loop = events.get_running_loop()

        # Daemon thread runs without blocking the main program from exiting.
        # And when main program exits, associated daemon threads are killed too
        thread = threading.Thread(target=self._do_waitpid,
                                  name=f"waitpid-{next(self._pid_counter)}",
                                  args=(loop, pid, callback, args),
                                  daemon=True)

        # the subprocess pid to thread object mapping is used:
        #   1) When this child watcher object is garbage collected to check
        #      if there are any threads still running and if so log a warning -
        #      "Instance has registered but not finished child processes"
        #   2) This child watcher implementation's `.close` method which in
        #      asyncio is only called when setting a new child watcher
        #      implementation on the event loop policy.
        #      `.close` joins on all non-daemonic and running threads in
        #      `self.threads` mapping
        self._threads[pid] = thread
        # run watcher thread
        thread.start()

    def remove_child_handler(self, pid):
        # asyncio never calls remove_child_handler() !!!
        # The method is no-op but is implemented because
        # abstract base class requires it
        return True

    def attach_loop(self, loop):
        """
        Does nothing because this watcher can only be attached to an event loop
        running in the current thread.
        """
        pass

    def _do_waitpid(self, loop, expected_pid, callback, args):
        """
        Is run in a separate daemonic thread:
            1) Indefinitely waits for subprocess to terminate

            2) Calls user supplied `callback` via `loop.call_soon_threadsafe`
               passing it `pid, returncode, *args` as arguments.

            3) `loop.call_soon_threadsafe` is threadsafe because:
                - doesn't check that the call happens from the same thread
                  in which the event loop is running unlike `loop.call_soon`

                - if the event loop has nothing to do it's currently indefinitely
                  blocked on polling 'self-pipe', unblock the event loop so it
                  can process the call by writing a 0 byte to 'self-pipe'

            4) Remove this thread from the subprocess pid to thread object
               mapping, because there's no need to check it during GC and no
               need to join on it during `self.close` - this thread is about to
               exit.
        """
        assert expected_pid > 0

        try:
            # indefinitely wait for child process with pid `expected_pid` to
            # complete
            pid, status = os.waitpid(expected_pid, 0)
        except ChildProcessError:
            # The child process is already reaped
            # (may happen if waitpid() is called elsewhere).
            pid = expected_pid
            returncode = 255  # meaning - exit status out of range
            logger.warning(
                "Unknown child process pid %d, will report returncode 255",
                pid)
        else:
            returncode = waitstatus_to_exitcode(status)
            if loop.get_debug():
                logger.debug('process %s exited with returncode %s',
                             expected_pid, returncode)

        if loop.is_closed():
            logger.warning("Loop %r that handles pid %r is closed", loop, pid)
        else:
            # 1) Is threadsafe because doesn't check that the call happens from
            #    event loop's thread unlike `loop.call_soon`
            # 2) If the event loop has nothing to do it's currently indefinitely
            #    blocked on polling 'self-pipe', unblock the event loop so it
            #    can process the call by writing a 0 byte to 'self-pipe'
            # 3) During loop's next iteration `callback` will be called with
            #    `pid`, `returncode` ad `*args`
            loop.call_soon_threadsafe(callback, pid, returncode, *args)

        # this thread is about to stop running, there's no need to inspect
        # it at garbage collection or join on it in `self.close`
        self._threads.pop(expected_pid)
