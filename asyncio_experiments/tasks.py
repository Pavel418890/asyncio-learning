"""Support for tasks, coroutines and the scheduler."""
import contextvars
import functools
import inspect
import itertools
import types
import warnings
import weakref
import concurrent.futures

from . import base_tasks
from . import coroutines
from . import exceptions
from . import futures
from . import events
from .coroutines import _is_coroutine

# Helper to generate new task names
# This uses itertools.count() instead of a "+= 1" operation because the latter
# is not thread safe. See bpo-11866 for a longer explanation.
# Implemented in C as a static uint64_t global variable
_task_name_counter = itertools.count(1).__next__


def current_task(loop=None):
    """Return a currently executed task."""
    if loop is None:
        loop = events.get_running_loop()
    return _current_tasks.get(loop)


def all_tasks(loop=None):
    """ Return a set of all tasks for the loop (only running/pending ones). """
    if loop is None:
        loop = events.get_running_loop()

    # Looping over a WeakSet (_all_tasks) isn't safe as it can be updated
    # from another thread while we do so. Therefore we cast it to list prior
    # to filtering. The list cast itself requires iteration, so we repeat
    # it several times ignoring RuntimeErrors
    # (which are not very likely to occur). See issues 34970 and 36607 for
    # details.

    i = 0
    while True:
        try:
            tasks = list(_all_tasks)
        except RuntimeError:
            i += 1
            if i >= 1000:
                raise
        else:
            break

    return {t for t in tasks
            if futures._get_loop(t) is loop and not t.done()}


def _set_task_name(task, name):
    # For task compatible objects,
    # because they might not have a .set_name method
    if name is not None:
        try:
            set_name = task.set_name
        except AttributeError:
            pass
        else:
            set_name(name)


# Inherit Python Task implementation
# from a Python Future implementation.
class Task(futures._PyFuture):
    """
    A coroutine wrapped in a Future.

    Task as well as Future are both implemented mostly in C now.
    """

    # An important invariant maintained while a Task not done:
    #
    # - Either _fut_waiter is None, and _step() is scheduled;
    # - or _fut_waiter is some Future, and _step() is *not* scheduled.
    #
    # The only transition from the latter to the former is through
    # _wakeup().  When _fut_waiter is not None, one of its callbacks
    # must be _wakeup().

    # If False, don't log a message if the task is destroyed whereas its
    # status is still pending
    _log_destroy_pending = True

    def __init__(self, coro, *, loop=None, name=None):
        """
        Implemented in C:
        _asyncio_Task___init___impl - does the same stuff as this pure python
        implementation, but _task_name_counter is just a static uint64_t global
        variable.
        """

        super().__init__(loop=loop)

        if self._source_traceback:
            del self._source_traceback[-1]

        if not coroutines.iscoroutine(coro):
            # raise after Future.__init__(), attrs are required for __del__
            # prevent logging for pending task in __del__
            self._log_destroy_pending = False
            raise TypeError(f"a coroutine was expected, got {coro!r}")

        if name is None:
            self._name = f'Task-{_task_name_counter()}'
        else:
            self._name = str(name)

        self._must_cancel = False
        self._fut_waiter = None
        self._coro = coro
        self._context = contextvars.copy_context()

        # arrange for self.__step to be called as soon as possible
        # so as soon as the task is created it's step method will be arranged
        # to be called as soon as possible
        self._loop.call_soon(self.__step, context=self._context)
        # adds the task to the WeakSet containing all alive tasks
        _register_task(self)

    def __del__(self):
        """
        Logs that task was destroyed while still _PENDING if
        self._log_destroy_pending is set to True.
        And calls loop's exception handler.

        Is implemented in C now:
        TaskObj_finalize - does basically the same thing as this pure python
        implementation.
        """
        if self._state == futures._PENDING and self._log_destroy_pending:
            context = {
                'task': self,
                'message': 'Task was destroyed but it is pending!',
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            self._loop.call_exception_handler(context)
        # call future's finalizer
        super().__del__()

    def __class_getitem__(cls, type):
        """
        Implemented in C:
        task_cls_getitem - does the same stuff as
        this pure python implementation
        """
        return cls

    def _repr_info(self):
        return base_tasks._task_repr_info(self)

    def get_coro(self):
        """
        Implemented in C:
        _asyncio_Task_get_coro_impl - does the same stuff as
        this pure python implementation
        """
        return self._coro

    def get_name(self):
        """
        Implemented in C:
        _asyncio_Task_get_name_impl - does the same stuff as
        this pure python implementation
        """
        return self._name

    def set_name(self, value):
        """
        Implemented in C:
        _asyncio_Task_set_name - does the same stuff as
        this pure python implementation
        """
        self._name = str(value)

    def set_result(self, result):
        """
        Implemented in C:
        _asyncio_Task_set_result - does the same stuff as
        this pure python implementation
        """
        raise RuntimeError('Task does not support set_result operation')

    def set_exception(self, exception):
        """
        Implemented in C:
        _asyncio_Task_set_exception - does the same stuff as
        this pure python implementation
        """
        raise RuntimeError('Task does not support set_exception operation')

    def get_stack(self, *, limit=None):
        """Return the list of stack frames for this task's coroutine.

        If the coroutine is not done, this returns the stack where it is
        suspended.  If the coroutine has completed successfully or was
        cancelled, this returns an empty list.  If the coroutine was
        terminated by an exception, this returns the list of traceback
        frames.

        The frames are always ordered from oldest to newest.

        The optional limit gives the maximum number of frames to
        return; by default all available frames are returned.  Its
        meaning differs depending on whether a stack or a traceback is
        returned: the newest frames of a stack are returned, but the
        oldest frames of a traceback are returned.  (This matches the
        behavior of the traceback module.)

        For reasons beyond our control, only one stack frame is
        returned for a suspended coroutine.
        """
        return base_tasks._task_get_stack(self, limit)

    def print_stack(self, *, limit=None, file=None):
        """Print the stack or traceback for this task's coroutine.

        This produces output similar to that of the traceback module,
        for the frames retrieved by get_stack().  The limit argument
        is passed to get_stack().  The file argument is an I/O stream
        to which the output is written; by default output is written
        to sys.stderr.
        """
        return base_tasks._task_print_stack(self, limit, file)

    def cancel(self, msg=None):
        """Request that this task cancel itself.

        This arranges for a CancelledError to be thrown into the
        wrapped coroutine on the next cycle through the event loop.
        The coroutine then has a chance to clean up or even deny
        the request using try/except/finally.

        Unlike Future.cancel, this does not guarantee that the
        task will be cancelled: the exception might be caught and
        acted upon, delaying cancellation of the task or preventing
        cancellation completely.  The task may also return a value or
        raise a different exception.

        Immediately after this method is called, Task.cancelled() will
        not return True (unless the task was already cancelled).  A
        task will be marked as cancelled when the wrapped coroutine
        terminates with a CancelledError exception (even if cancel()
        was not called).

        Implemented in C:
        _asyncio_Task_cancel_impl - basically does the same stuff as this
        pure python implementation.
        """
        # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        #  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        #  Examine this again when you get a good understanding of what the
        #  self.__step method does
        self._log_traceback = False
        if self.done():
            return False
        if self._fut_waiter is not None:
            # if self._fut_waiter is a pending future or task this will cancel
            # it and return True
            if self._fut_waiter.cancel(msg=msg):
                # Leave self._fut_waiter; it may be a Task that
                # catches and ignores the cancellation so we may have
                # to cancel it again later.
                return True
        # It must be the case that self.__step is already scheduled.
        self._must_cancel = True
        self._cancel_message = msg
        return True

    def __step(self, exc=None):
        """
        Implemented in C:
        task_step => task_step_impl - pretty much the same implementation
        as this pure python one. Subtle differences:
        1) _enter_task(self._loop, self) is called before anything else.
        2) pure Futures and Tasks are checked for exact type match first,
           instead of using getattr(result, '_asyncio_future_blocking', None).
           Future-compatible objects are checked later on separately.
        3) Negative case checks are duplicated for pure Futures/Tasks
           and Future-compatible objects
           (via getattr(result, '_asyncio_future_blocking', None))
        """
        if self.done():
            raise exceptions.InvalidStateError(
                f'_step(): already done: {self!r}, {exc!r}')
        if self._must_cancel:
            if not isinstance(exc, exceptions.CancelledError):
                exc = self._make_cancelled_error()
            self._must_cancel = False
        coro = self._coro
        self._fut_waiter = None

        # add task to _current_tasks so we know it's currently "running"
        # and if some other task is going to try and _enter_task an exception
        # will be raised
        _enter_task(self._loop, self)

        # Call either coro.throw(exc) or coro.send(None).
        try:
            if exc is None:
                # We use the `send` method directly, because coroutines
                # don't have `__iter__` and `__next__` methods.
                # await is like yield from, the inner most future or None will
                # bubble up the "callstack pipe" into result
                result = coro.send(None)
            else:
                # if not caught in the coroutine this exception will propagate
                # immediately and caught by one of the except clauses below
                # and the exception will be set on this task instance and all
                # callbacks will be scheduled and finally __step will exit
                # calling _leave_task
                result = coro.throw(exc)
        except StopIteration as exc:
            # we go a return instead of a yielded future or None,
            # this is the result
            if self._must_cancel:
                # Task is cancelled right before coro stops.
                # all callbacks will be scheduled and finally __step will exit
                # calling _leave_task
                self._must_cancel = False
                super().cancel(msg=self._cancel_message)
            else:
                # set result from the return value, will be set on the
                # StopIteration like with all generators
                # All done callbacks will be scheduled to be called by the loop
                # a soon as possible at this point and finally __step will exit
                # calling _leave_task
                super().set_result(exc.value)
        except exceptions.CancelledError as exc:
            # a CancelledError was raised from the coroutine
            # Save the original exception so we can chain it later.
            # All done callbacks will be scheduled to be called by the loop
            # a soon as possible at this point and finally __step will exit
            # calling _leave_task
            self._cancelled_exc = exc
            super().cancel()  # I.e., Future.cancel(self).
        except (KeyboardInterrupt, SystemExit) as exc:
            # All done callbacks will be scheduled to be called by the loop
            # a soon as possible at this point and finally __step will exit
            # calling _leave_task
            super().set_exception(exc)
            raise
        except BaseException as exc:
            # All done callbacks will be scheduled to be called by the loop
            # a soon as possible at this point and finally __step will exit
            # calling _leave_task
            super().set_exception(exc)
        else:
            # if the coro did not return or error out and just yielded
            # a future (or None) up the pipe then we continue processing
            # the yielded future, None or something else

            # Check if a future was yielded through the "yield from/await pipe"
            blocking = getattr(result, '_asyncio_future_blocking', None)
            if blocking is not None:
                # Yielded Future must come from Future.__iter__().
                if futures._get_loop(result) is not self._loop:
                    new_exc = RuntimeError(
                        f'Task {self!r} got Future '
                        f'{result!r} attached to a different loop')
                    # this exception will be thrown into the coroutine
                    # on next loop iteration and if not caught will propagate
                    # to self.__step and in the end will be set on this
                    # task and self.__step will exit
                    self._loop.call_soon(
                        self.__step, new_exc, context=self._context)
                elif blocking:
                    if result is self:
                        new_exc = RuntimeError(
                            f'Task cannot await on itself: {self!r}')
                        # this exception will be thrown into the coroutine
                        # on next loop iteration and if not caught will propagate
                        # to self.__step and in the end will be set on this
                        # task and self.__step will exit
                        self._loop.call_soon(
                            self.__step, new_exc, context=self._context)
                    else:
                        result._asyncio_future_blocking = False
                        # wake up this task when the future is done
                        # this done future will be passed to self.__wakeup
                        result.add_done_callback(
                            self.__wakeup, context=self._context)
                        self._fut_waiter = result
                        if self._must_cancel:
                            # if future is still _PENDING, then it will be
                            # cancelled, callbacks will be scheduled and
                            # this call will return True
                            # when self.__wakeup will be called future.result()
                            # will raise a CancelledException and it will be
                            # thrown in to the coroutine and
                            # caught in self.__step() and set on this task
                            if self._fut_waiter.cancel(
                                    msg=self._cancel_message):
                                self._must_cancel = False
                else:
                    new_exc = RuntimeError(
                        f'yield was used instead of yield from '
                        f'in task {self!r} with {result!r}')
                    # _asyncio_future_blocking is set to True
                    # on the future in __await__ or __iter__ just before it
                    # yields itself
                    # this exception will be thrown into the coroutine
                    # on next loop iteration and if not caught will propagate
                    # to self.__step and in the end will be set on this
                    # task and self.__step will exit
                    self._loop.call_soon(
                        self.__step, new_exc, context=self._context)

            elif result is None:
                # Bare yield relinquishes control for one event loop iteration.
                self._loop.call_soon(self.__step, context=self._context)
            elif inspect.isgenerator(result):
                # Yielding a generator is just wrong.
                new_exc = RuntimeError(
                    f'yield was used instead of yield from for '
                    f'generator in task {self!r} with {result!r}')
                # this exception will be thrown into the coroutine
                # on next loop iteration and if not caught will propagate
                # to self.__step and in the end will be set on this
                # task and self.__step will exit
                self._loop.call_soon(
                    self.__step, new_exc, context=self._context)
            else:
                # Yielding something else is an error.
                new_exc = RuntimeError(f'Task got bad yield: {result!r}')
                # this exception will be thrown into the coroutine
                # on next loop iteration and if not caught will propagate
                # to self.__step and in the end will be set on this
                # task and self.__step will exit
                self._loop.call_soon(
                    self.__step, new_exc, context=self._context)
        finally:
            # remove loop key and task value from _current_tasks
            _leave_task(self._loop, self)
            self = None  # Needed to break cycles when an exception occurs.

    def __wakeup(self, future):
        """
        Implemented in C:
        task_wakeup - pretty much the same implementation
        as this pure python one. Subtle differences:
        1) "pure" Futures/Tasks are checked separately from Future-compatible
        objects.
        2) For future-compatible objects their method .result() is called,
           for "pure" Future/Tasks future_get_result C func is called.
        """

        # when yielded future in step is done this will be called,
        # if and exception was raise in the future, then self.__step()
        # will throw it into the coroutine and if not caught inside it
        # it will propagate back to the self.__step() caller and set on this task
        try:
            future.result()
        except BaseException as exc:
            # This may also be a cancellation.
            self.__step(exc)
        else:
            # Don't pass the value of `future.result()` explicitly,
            # as `Future.__iter__` and `Future.__await__` don't need it.
            # If we call `_step(value, None)` instead of `_step()`,
            # Python eval loop would use `.send(value)` method call,
            # instead of `__next__()`, which is slower for futures
            # that return non-generator iterators from their `__iter__`.
            # the result will be acquired by catching StopIteration
            # in self.__step(), because after resuming from 'yield self'
            # future will return self.result() and it will be read from
            # StopIteration.value and set as the result on this Task instance
            self.__step()


_PyTask = Task

# Replace the pure python Task implementation with its C implementation
# (_asynciomodule.c)
try:
    import _asyncio
except ImportError:
    pass
else:
    # _CTask is needed for tests.
    Task = _CTask = _asyncio.Task


def create_task(coro, *, name=None):
    """Schedule the execution of a coroutine object in a spawn task.

    Return a Task object.
    """
    loop = events.get_running_loop()
    task = loop.create_task(coro)
    _set_task_name(task, name)
    return task


# wait() and as_completed() similar to those in PEP 3148.

FIRST_COMPLETED = concurrent.futures.FIRST_COMPLETED
FIRST_EXCEPTION = concurrent.futures.FIRST_EXCEPTION
ALL_COMPLETED = concurrent.futures.ALL_COMPLETED


async def wait(fs, *, timeout=None, return_when=ALL_COMPLETED):
    """Wait for the Futures and coroutines given by fs to complete.

    The fs iterable must not be empty.

    Coroutines will be wrapped in Tasks.

    Returns two sets of Future: (done, pending).

    Usage:

        done, pending = await asyncio.wait(fs)

    Note: This does not raise TimeoutError! Futures that aren't done
    when the timeout occurs are returned in the second set.

    -----------------------------------------------------------------------

    Under the hood:

    1) Passing raw coroutines is deprecated (check out the comments below).
        Should pass in a lust of Tasks/Futures.

    2) Creates Tasks from coroutines (only those that aren't already
       Tasks or Futures) which are schedules via .call_soon within Task's
       __init__

    3) The awaits '_wait' coro and propagates result '_wait' coro's StopIteration
       via a returns statement. The StopIteration exception contains
       done and pending tasks.


    '_wait' coro details:

    1) Receives a collection of already scheduled Tasks (via loop's .call_soon
       method)

    2) Creates a waiter future bound to the loop

    3) If timeout was provided schedules a TimerHandle on the loop which will
       be called by the loop in due time and will .set_result on the waiter
       future marking it as FINISHED and thus scheduling its done callbacks
       (the only one will be the parent task's .__wakeup method which via .__step
       will do coro.send(None) again thus 'unblocking' the 'await waiter'
       statement used in this coro)

    4) Adds a done callback to all passed in Futures/Tasks which will:
       - ALL_COMPLETED - decrements the Task counter from a closure
         on every Task's completion and when the counter reaches 0
         will cancel the TimerHandle if one was created and mark the waiter
         future FINISHED thus scheduling the parent Task's .__wakeup method
       - FIRST_COMPLETED - will do the same stuff when just one of the
         Tasks/Futures completes and will not wait for the counter to reach 0.
       - FIRST_EXCEPTION - will do the same stuff but only if the counter reaches
         0 or if one of the Tasks/Futures had and exception and it wasn't a
         Cancelled exception

    5) Awaits the waiter future which will yield itself through the
       yield from/await conduit to the outer most task,
       which will add its .__wakeup method as the waiter future's done callback.
       So when the waiter future will be marked as FINISHED
       by the TimerHandle's ._run method (if timeout expires)
       or by one of the done callbacks attached to every Task/Future
       from the list passed in, .__wakeup will call Task's .__step again
       which will 'send' to this inner most coroutine and the 'await waiter'
       statement will return a StopIteration either to the outer most task
       or to a coro right above.

    6) The StopIteration will have as its value a tuple of 2 sets:
       - 1st set - all done Tasks
       - 2nd set - still pending Tasks

    7) Before the StopIteration exception will be 'yielded up the conduit'
       the TimerHandle (timeout handle will be cancelled and the added done
       callback to every Task will be removed from them).

    8) Computes the resulting tuple by iterating through the list of
       Tasks/Futures passed in and adding them to the 'done set' if it
       is marked FINISHED otherwise adding to the 'pending set'

    9) Returns the done and pending Tasks/Futures (will bubble up to
       the awaiting coro through StopIteration)
    """
    if futures.isfuture(fs) or coroutines.iscoroutine(fs):
        raise TypeError(f"expect a list of futures, not {type(fs).__name__}")

    if not fs:
        raise ValueError('Set of coroutines/Futures is empty.')

    if return_when not in (FIRST_COMPLETED, FIRST_EXCEPTION, ALL_COMPLETED):
        raise ValueError(f'Invalid return_when value: {return_when}')

    loop = events.get_running_loop()

    fs = set(fs)

    if any(coroutines.iscoroutine(f) for f in fs):
        # Because the result will be confusing
        # 'done' and 'pending' will be Tasks and not the passed in coroutines
        # It is now preferable to first create tasks from coroutines
        # asyncio.create_task(foo()) which will create a Task from the coro
        # and schedule the Task on the currently running event loop

        # Explicitly creating Tasks ensures that something like this
        # will work as expected:

        # async def foo():
        #     return 42
        #
        # task = asyncio.create_task(foo())
        # done, pending = await asyncio.wait({task})
        #
        # previously if Tasks were created implicitly
        # by this coro function, the code bellow would never run

        # if task in done:
        #     # Everything will work as expected now.
        warnings.warn("The explicit passing of coroutine objects to "
                      "asyncio.wait() is deprecated since Python 3.8, and "
                      "scheduled for removal in Python 3.11.",
                      DeprecationWarning, stacklevel=2)

        # Creates Tasks from coros and schedules them to be run on the event
        # loop
        fs = {ensure_future(f, loop=loop) for f in fs}

        # this will return done and pending Tasks via StopIteration,
        # the StopIteration will be propagated upwards by this 'return statement'
        # without modifying the resulting tuple of done and pending Tasks/Futures
        return await _wait(fs, timeout, return_when, loop)


def _release_waiter(waiter, *args):
    if not waiter.done():
        waiter.set_result(None)


async def wait_for(fut, timeout):
    """Wait for the single Future or coroutine to complete, with timeout.

    Coroutine will be wrapped in Task.

    Returns result of the Future or coroutine.  When a timeout occurs,
    it cancels the task and raises TimeoutError.  To avoid the task
    cancellation, wrap it in shield().

    If the wait is cancelled, the task is also cancelled.

    This function is a coroutine.

    Details:

    1) If timeout is not provided just awaits the passed in Future or coroutine

    2) If timeout is less than or equal to 0:
       - creates a task from the coroutine (if it was a coroutine, else just
         keeps the passed in future) and schedules the task's .__step
         method to be called during loop's next iteration
         (will do passed_in_coro.send(None) and add its .__wakeup method to the
         yielded future from passed_in_coro as a done callback)

        - if the fut passed in was already a future check if it's done
          and if it is just return the future's result

        - emulates 0 or less timeout by cancelling the Task/Future immediately
          via _cancel_and_wait which will do this in 2 loop iterations from now
          if the coroutine wrapped in the Task doesn't decide
          to ignore the CancellationError thrown in to it and do some long
          running stuff

        - converts CancellationError raised by Future/Task .result() method
          into a TimeoutError and raises it

    3) If timeout was greater than 0:
       - creates a waiter future and a TimerHandle wrapping a function
         which will complete the waiter future. The TimerHandle's ._run
         method will call this function in due time (when the timeout
         expires it will be picked up by the loop and it's ._run method
         will be executed)

       - creates a task from the coroutine (if it was a coroutine, else just
         keeps the passed in future) and schedules the task's .__step
         method to be called during loop's next iteration
         (will do passed_in_coro.send(None) and adds its .__wakeup method to the
         yielded future from passed_in_coro as a done callback)

        - adds a done callback to the Future or Task which will complete the
          waiter future

        - so either the passed in Future or created Task completes first
          and its done callback will complete the waiter future or timeout
          expires and the TimerHandle's ._run method will call the function
          cancelling the waiter future

        - calls 'await waiter' which will suspend this coroutine's execution and
          yield the waiter future up through the 'await/yield from' conduit
          right to the outer most task which will add it's .__wakeup method
          as a done callback to the waiter future.

        - when the waiter future completes either because the passed in
         'Future/created Task' has completed or the timeout has expired
         triggering the timeout_handle, the done callback will be executed
         calling the outer most Task's .__wakeup method which will call
         its .__step method which will do
         waiter.send(waiter_future.result() == None) thus resuming the execution
         of this coroutine

       - if the passed in 'Future/Task created from passed in coro'
         has completed then just returns its .result()

       - if the passed in 'Future/Task created from passed in coro' is not done
         meaning the timeout has expired removes the done callback from it
         which would cancel the waiter future again.

       - cancels the passed in 'Future or created Task from coro' via
         _cancel_and_wait which will do this in 2 loop iterations from now
          if the coroutine wrapped in the Task doesn't decide
          to ignore the CancellationError thrown in to it and do some long
          running stuff

       - converts CancellationError raised by Future/Task .result() method
         into a TimeoutError and raises it

       - finally cancels the TimerHandle so it will be ignored by the loop

    """
    loop = events.get_running_loop()

    # if no timeout just await the coro/future,
    # the 'wait_for' coro is only useful when a timeout is provided
    if timeout is None:
        return await fut

    if timeout <= 0:
        # create a Task from coroutine and schedule it to be called during
        # loop's next iteration
        fut = ensure_future(fut, loop=loop)

        # if this was already a Future check if its done and return its result
        if fut.done():
            return fut.result()

        # because the requested timeout was 0 or less, we emulate it
        # by cancelling the future immediately (the await _cancel_and_wait
        # statement will resume in 2 loop iterations from now - check out
        # the comments for _cancel_and_wait)
        await _cancel_and_wait(fut, loop=loop)
        # convert CancelledError to TimeoutError
        try:
            fut.result()
        except exceptions.CancelledError as exc:
            raise exceptions.TimeoutError() from exc
        else:
            raise exceptions.TimeoutError()

    # complete the waiter future after timeout expires
    # done via call_later, the loop will pick up the timeout_handle (TimerHandle)
    # in due time
    waiter = loop.create_future()
    timeout_handle = loop.call_later(timeout, _release_waiter, waiter)

    # partial because waiter will not be passed to 'fut' don callback,
    # 'fut' will be passed into it
    cb = functools.partial(_release_waiter, waiter)

    # create a task from the passed in coroutine and schedule it via
    # loop.call_soon
    fut = ensure_future(fut, loop=loop)

    # this will cancel the waiter if the task created from the passed in 'fut'
    # completes before timeout expires
    fut.add_done_callback(cb)

    try:
        # wait until the future completes or the timeout
        try:
            await waiter
        # if somehow the waiter future gets cancelled
        except exceptions.CancelledError:
            if fut.done():
                return fut.result()
            else:
                fut.remove_done_callback(cb)
                # We must ensure that the task is not running
                # after wait_for() returns.
                # See https://bugs.python.org/issue32751
                await _cancel_and_wait(fut, loop=loop)
                raise

        # if passed in coro completed before timeout
        if fut.done():
            return fut.result()
        # if passed in coro didn't complete before timeout
        else:
            # TODO !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
            #  look at those bugs more closely when you get a good
            #  understanding of asyncio's syncronization primitives
            #  https://bugs.python.org/issue32751 =>
            #  https://bugs.python.org/issue33638 =>
            #  https://github.com/python/cpython/commit/e2b340ab4196e1beb902327f503574b5d7369185

            # don't try to complete the waiter again
            fut.remove_done_callback(cb)
            # We must ensure that the task is not running
            # after wait_for() returns.
            # See https://bugs.python.org/issue32751
            await _cancel_and_wait(fut, loop=loop)
            # In case task cancellation failed with some
            # exception, we should re-raise it
            # See https://bugs.python.org/issue40607
            try:
                fut.result()
            except exceptions.CancelledError as exc:
                raise exceptions.TimeoutError() from exc
            else:
                raise exceptions.TimeoutError()
    finally:
        # in all cases don't forget to cancel the TimerHandle
        timeout_handle.cancel()


async def _wait(fs, timeout, return_when, loop):
    """Internal helper for wait().

    The fs argument must be a collection of Futures (usually Tasks
    already scheduled Tasks via .call_soon)

    Details:
    1) Receives a collection of already scheduled Tasks (via loop's .call_soon
       method)

    2) Creates a waiter future bound to the loop

    3) If timeout was provided schedules a TimerHandle on the loop which will
       be called by the loop in due time and will .set_result on the waiter
       future marking it as FINISHED and thus scheduling its done callbacks
       (the only one will be the parent task's .__wakeup method which via .__step
       will do coro.send(None) again thus 'unblocking' the 'await waiter'
       statement used in this coro)

    4) Adds a done callback to all passed in Futures/Tasks which will:
       - ALL_COMPLETED - decrements the Task counter from a closure
         on every Task's completion and when the counter reaches 0
         will cancel the TimerHandle if one was created and mark the waiter
         future FINISHED thus scheduling the parent Task's .__wakeup method
       - FIRST_COMPLETED - will do the same stuff when just one of the
         Tasks/Futures completes and will not wait for the counter to reach 0.
       - FIRST_EXCEPTION - will do the same stuff but only if the counter reaches
         0 or if one of the Tasks/Futures had and exception and it wasn't a
         Cancelled exception

    5) Awaits the waiter future which will yield itself through the
       yield from/await conduit to the outer most task,
       which will add its .__wakeup method as the waiter future's done callback.
       So when the waiter future will be marked as FINISHED
       by the TimerHandle's ._run method (if timeout expires)
       or by one of the done callbacks attached to every Task/Future
       from the list passed in, .__wakeup will call Task's .__step again
       which will 'send' to this inner most coroutine and the 'await waiter'
       statement will return a StopIteration either to the outer most task
       or to a coro right above.

    6) The StopIteration will have as its value a tuple of 2 sets:
       - 1st set - all done Tasks
       - 2nd set - still pending Tasks

    7) Before the StopIteration exception will be 'yielded up the conduit'
       the TimerHandle (timeout handle will be cancelled and the added done
       callback to every Task will be removed from them).

    8) Computes the resulting tuple by iterating through the list of
       Tasks/Futures passed in and adding them to the 'done set' if it
       is marked FINISHED otherwise adding to the 'pending set'

    9) Returns the done and pending Tasks/Futures (will bubble up to
       the awaiting coro through StopIteration)
    """
    assert fs, 'Set of Futures is empty.'

    waiter = loop.create_future()

    timeout_handle = None
    if timeout is not None:
        # _release_waiter will be called by the loop when timout expires
        # _release_waiter will set_result on the waiter future
        # which will mark it as done and call all done callbacks on it
        # This returns a TimerHandle, it's ._run method will be called
        # by the loop when time comes
        timeout_handle = loop.call_later(timeout, _release_waiter, waiter)

    counter = len(fs)

    def _on_completion(f):
        nonlocal counter
        counter -= 1

        # 1) If all Tasks are done (ALL_COMPLETED)
        # 2) If the caller asked for FIRST_COMPLETED
        # 3) If the caller asked for FIRST_EXCEPTION and this Task
        #    wasn't cancelled and an exception was raised during its execution.
        if (
                counter <= 0 or
                return_when == FIRST_COMPLETED or
                (
                        return_when == FIRST_EXCEPTION
                        and (not f.cancelled() and f.exception() is not None)
                )
        ):
            # If we have already met the callers requirement
            # and the TimerHandle's ._run method hasn't been called yet (timeout
            # not expired) then just cancel the TimerHandle (the loop will
            # not take it into account from now on and some time in the future
            # maybe will remove it from its ._scheduled list)
            if timeout_handle is not None:
                timeout_handle.cancel()
            # Mark the waiter future as done
            # TimerHandle's ._run method would have done it in due time
            if not waiter.done():
                # This will also call all waiter future done callbacks
                # Task's .__wakeup method will be called as one of them
                # which will call .__step and do coro.send(None) again
                # which will yield StopIteration from 'await waiter'
                # This will happen because when waiter is yielded to
                # Task's.__step for the first time
                # it will add its .__wakeup method as the waiter's done callback
                waiter.set_result(None)

    # call the above function on every Task completion
    for f in fs:
        f.add_done_callback(_on_completion)

    try:
        await waiter
    finally:
        if timeout_handle is not None:
            timeout_handle.cancel()
        for f in fs:
            f.remove_done_callback(_on_completion)

    done, pending = set(), set()
    for f in fs:
        if f.done():
            done.add(f)
        else:
            pending.add(f)
    # This will bubble up to Task's .__step method with a StopIteration
    # exception (or a coroutine awaiting this right above)
    return done, pending


async def _cancel_and_wait(fut, loop):
    """
    Cancel the *fut* future or task and wait until it completes.

    Details:

    1) Creates a new waiter future

    2) Adds a done callback to the passed in Future/Task which will complete
       the waiter future

    3) Cancels the passed in Future/Task which will via it's  _release_waiter
       done callback added in step 2 complete the waiter future (can
       be just one of its many done callbacks) - the done callbacks will be
       scheduled via .call_soon thus executed in loop's next iteration

    4) Steps 1 - 3 will happen when the outer most Task performs
       a coro.send(None) in its .__step method

    5) After steps 1-3 are executed this coro will block
       on 'await waiter' and the outer most Task in its .__step method
       will receive the waiter future through the 'await/yield from' conduit

    6) The outer most Task will add its .__wakeup method to the waiter future
       as a done callback which will be called on the 2nd loop iteration from now
       (on the next iteration the passed in future's done callback
       (_release_waiter => waiter_future.set_result()
       => waiter_future.__schedule_callbacks() => loop.call_soon(task.__wakeup)
       will schedule the outer most Task's .__wakeup method to be run
       during next iteration)

    7) Outer most Task's .__wakeup method will call it's .__step method
        which will again call coro.send(None) and the send will reach this
        inner most coroutine thus resuming its execution from 'await waiter'

    8) _release_waiter done callback will be removed from passed in future's
       done callbacks

    9) On return StopIteration will be propagated to the awaiting caller
      with a None value as the result.
    """

    waiter = loop.create_future()
    # partial because 'fut' will be passed to the fut's done callback
    # and not waiter, because it's fut's done callback
    cb = functools.partial(_release_waiter, waiter)
    # the _release_waiter callback will mark the waiter future as done,
    # so when the 'fut' passed in completes the waiter future will also complete.
    fut.add_done_callback(cb)

    try:
        # this will also call the _release_waiter callback added to this future
        fut.cancel()
        # We cannot wait on *fut* directly to make
        # sure _cancel_and_wait itself is reliably cancellable.

        # This will yield the waiter on Task's 'first' .__step call
        # and when Task's .__wakeup will be called when the 'fut' passed in
        # completes .__step will 'send' to this inner most coro and 'await
        # waiter' will unblock and the finally block will be run then propagating
        # a StopIteration exception with None as its value
        await waiter
    finally:
        fut.remove_done_callback(cb)


# This is *not* a @coroutine!  It is just an iterator (yielding Futures).
def as_completed(fs, *, timeout=None):
    """Return an iterator whose values are coroutines.

    When waiting for the yielded coroutines you'll get the results (or
    exceptions!) of the original Futures (or coroutines), in the order
    in which and as soon as they complete.

    This differs from PEP 3148; the proper way to use this is:

        for f in as_completed(fs):
            'f' is the async def _wait_for_one() coro
            result = await f  # The 'await' may raise.
            # Use result.

    If a timeout is specified, the 'await' will raise
    TimeoutError when the timeout occurs before all Futures are done.

    Note: The futures 'f' are not necessarily members of fs.

    Details:

    for f in as_completed(fs):
        result = await f  # The 'await' may raise.
            # Use result.

    1) 'f' here is a yielded coroutine _wait_for_one which blocks on getting
       Task/Futures from an internal 'done' queue,
       when a Task/Future arrives on the queue _wait_for_one calls .result()
       on it and returns it to the caller.
       If a sentinel None arrived on the queue, this means that the passed in
       timeout has expired so _wait_for_one raises a TimeoutError

    2) completed Tasks/Futures will be pushed to the 'done' queue (to be picked
       up by _wait_for_one) via a done callback for every 'todo' Task.Future
       which will put the completed Task/Future on to the 'done' queue

    3) if timeout passed in to this function was not None a TimerHandle
       will be scheduled to run by the event loop after timeout expires.
       TimerHandle's ._run method will call a function which will
       remove all _on_completion done callbacks from the 'todo' set
       of Tasks/Futures, put a sentinel None into the 'done' queue and clear
       the set of 'todo' Tasks/Futures.
       The sentinel None will be picked up by _wait_for_one as a result
       of which it will raise a TimeoutError to our caller.
       Even if the caller ignores the TimeoutError and keeps on iterating over
       'as_completed' it will not yield anything, because after every
       yield it checks the length of 'todo' Tasks/Futures and it was wiped
       out by the TimerHandler.
    """

    if futures.isfuture(fs) or coroutines.iscoroutine(fs):
        raise TypeError(
            f"expect an iterable of futures, not {type(fs).__name__}"
        )

    from .queues import Queue  # Import here to avoid circular import problem.
    done = Queue()

    loop = events.get_event_loop()
    # 1) if already a Future or Task, will just add it to this set
    # 2) if a coroutine, will create a Task and add it to this set,
    #    the created Task's .__step method wrapped in an event.Handler
    #    will be scheduled to run via Handler's ._run method during event loop's
    #    next iteration
    todo = {ensure_future(f, loop=loop) for f in set(fs)}
    timeout_handle = None

    def _on_timeout():
        for f in todo:
            f.remove_done_callback(_on_completion)
            done.put_nowait(None)  # Queue a dummy value for _wait_for_one().
        todo.clear()  # Can't do todo.remove(f) in the loop.

    def _on_completion(f):
        if not todo:
            return  # _on_timeout() was here first.
        todo.remove(f)
        done.put_nowait(f)
        # if this was the last Future/Task in 'todo'
        # cancel the timout_handle (events.TimeHandle) - will be ignored
        # by the event loop from now on and possibly removed from
        # the event loop's ._scheduled list/heap some time in the future
        if not todo and timeout_handle is not None:
            timeout_handle.cancel()

    async def _wait_for_one():
        # while queue is empty will block
        f = await done.get()
        if f is None:
            # Dummy value from _on_timeout().
            raise exceptions.TimeoutError
        return f.result()  # May raise f.exception().

    for f in todo:
        # 1) for every completed Future/Task from todo, _on_completion will be
        #    called with the completed Future/Task
        # 2) _on_completion will remove the completed Task/Future from 'todo'
        #   and put it into the done queue, the .result() of the Task/Future
        #   will be retrieved by the caller of 'as_completed' via
        #   awaiting _wait_for_one
        f.add_done_callback(_on_completion)

    # the fs param was not empty and a timeout was provided
    if todo and timeout is not None:
        # 1) timeout_handle (TimerHandle) will be picked up
        # by the loop when the timeout expires and it's ._run method
        # will be called which in turn will call _on_timeout
        # 2) _on_timeout will remove all _on_completion done callbacks
        #   from the 'todo' Tasks/Futures, will put the sentinel value None
        #   into the done queue and clear the set of 'todo' Futures/Tasks.
        # 3) _wait_for_one() (is awaited by our caller) will receive the None
        #    sentinel and raise a TimeoutError which will be propagated to
        #    our caller.
        timeout_handle = loop.call_later(timeout, _on_timeout)

    # our caller will call 'await _wait_for_one()' which will
    # return .result() from completed Tasks/Futures or raise a TimeoutException
    for _ in range(len(todo)):
        # will only yield when there are
        yield _wait_for_one()


@types.coroutine
def __sleep0():
    """Skip one event loop run cycle.

    This is a private helper for 'asyncio.sleep()', used
    when the 'delay' is set to 0.  It uses a bare 'yield'
    expression (which Task.__step knows how to handle)
    instead of creating a Future object.

    Details:

    In Task's .__step method
    elif result is None:
        # Bare yield relinquishes control for one event loop iteration.
        self._loop.call_soon(self.__step, context=self._context)

    So basically Task will do coro.send(None) during event loop's next iteration
    which will reach this coro and bubble up StopIteration to the caller
    or back up to the outer most Task.
    """
    yield


async def sleep(delay, result=None):
    """
    Coroutine that completes after a given time (in seconds).

    Details:

    1) If delay is <= 0 just yield for one loop iteration and then returns

    2) Else schedules a TimerHandle which will in due time set_result
       on the waiter future created during this call, effectively causing
       the outer most Task to do coro.send again, unblocking the call to sleep
    """
    if delay <= 0:
        # will sleep for 1 loop iteration
        await __sleep0()
        return result

    loop = events.get_running_loop()
    future = loop.create_future()

    # returns a TimerHandle which wraps
    # futures._set_result_unless_cancelled(future, result)
    # in it's ._run method, the TimerHandle will be picked up and executed
    # by the event loop in due time

    # if 'future' wasn't cancelled will set the passed in result as its result
    h = loop.call_later(
        delay,
        futures._set_result_unless_cancelled,
        future,
        result
    )

    try:
        # block until result will be set on the future, which will schedule the
        # outer most Task's .__wakeup => .__step method which will send to this
        # coro unblocking the call to 'await future'
        # the caller/or outer most task will get a StopIteration
        return await future
    finally:
        h.cancel()


def ensure_future(coro_or_future, *, loop=None):
    """Wrap a coroutine or an awaitable in a future.

    If the argument is a Future, it is returned directly.

    I) If the passed in coro_or_future was a coroutine:

        1) If the loop wasn't passed in get the currently running event loop
           (or create it and set it)

        2) Create a Task from the coroutine attached to the passed in loop
           or the current loop set globally
           (Task's .__step method will do coro.send(None) and other stuff)

        3) When creating the Task will ensure that it's .__step method
           is called in the current or next iteration of the loop to which
           it is bound to.

        4) Return the newly created (and already scheduled task)

    II) If the passed in coro_or_future is already a Future/Task:

        1) Chek that the Future belongs to the loop that was passed in (if passed
           in) - raise a ValueError if it isn't

        2) Return the  coro_or_future

    III) If the passed in coro_or_future is some other type of awaitable:

         1) Call _wrap_awaitable with the coro_or_future;

         2) _wrap_awaitable is just a generator-based coroutine which
            is decorated with @types.coroutine for backwards compatibility

        3) _wrap_awaitable will just 'yield from' the coro_or_future awaitable's
           __await__() method (so coro_or_future awaitable will be wrapped in
           a coroutine)

        4) Then we will recursively call this function (ensure_future)
           on the wrapped coro_or_future (_wrap_awaitable) one more time

        5) Create a Task from the coroutine passed in attached to
           the passed in loop or the current loop set globally

        6) Create a Task from the coroutine attached to the passed in loop
           or the current loop set globally
           (Task's .__step method will do coro.send(None) and other stuff)

        7) When creating the Task will ensure that it's .__step method
           is called in the current or next iteration of the loop to which
           it is bound to.

        8) Return the newly created (and already scheduled task) first to the
           recursive caller and then to the original; caller

    IV) If the passed in coro_or_future wasn't a coroutine, Future/Task or
        awaitable raise a TypeError
    """
    if coroutines.iscoroutine(coro_or_future):
        if loop is None:
            loop = events.get_event_loop()
        task = loop.create_task(coro_or_future)
        # only in _DEBUG mode
        if task._source_traceback:
            del task._source_traceback[-1]
        return task
    elif futures.isfuture(coro_or_future):
        if loop is not None and loop is not futures._get_loop(coro_or_future):
            raise ValueError('The future belongs to a different loop than '
                             'the one specified as the loop argument')
        return coro_or_future
    elif inspect.isawaitable(coro_or_future):
        return ensure_future(_wrap_awaitable(coro_or_future), loop=loop)
    else:
        raise TypeError('An asyncio.Future, a coroutine or an awaitable is '
                        'required')


@types.coroutine
def _wrap_awaitable(awaitable):
    """Helper for asyncio.ensure_future().

    Wraps awaitable (an object with __await__) into a coroutine
    that will later be wrapped in a Task by ensure_future().

    Basically just to be a proxy between the coro.send(None) sender and
    the awaitable's __await__ method which yields something (presumably self).

    This is done so the awaitable will have all the necessary flags set on it
    to be recognized by Python as a coroutine.
    """
    return (yield from awaitable.__await__())


_wrap_awaitable._is_coroutine = _is_coroutine


class _GatheringFuture(futures.Future):
    """Helper for gather().

    This overrides cancel() to cancel all the children and act more
    like Task.cancel(), which doesn't immediately mark itself as
    cancelled.
    """

    def __init__(self, children, *, loop=None):
        super().__init__(loop=loop)
        self._children = children
        self._cancel_requested = False

    def cancel(self, msg=None):
        if self.done():
            return False
        ret = False
        for child in self._children:
            # if at least one of the children was still pending and thus
            # successfully cancelled set ret to True
            # Do not break because we need to cancel every child
            if child.cancel(msg=msg):
                ret = True

        if ret:
            # If any child tasks were actually cancelled, we should
            # propagate the cancellation request regardless of
            # *return_exceptions* argument.  See issue 32684.

            # This gathering future will only actually be cancelled when all
            # child futures complete (successfully or get cancelled)
            self._cancel_requested = True
        return ret


def gather(*coros_or_futures, return_exceptions=False):
    """Return a future aggregating results from the given coroutines/futures.

    Coroutines will be wrapped in a future and scheduled in the event
    loop. They will not necessarily be scheduled in the same order as
    passed in.

    All futures must share the same event loop.  If all the tasks are
    done successfully, the returned future's result is the list of
    results (in the order of the original sequence, not necessarily
    the order of results arrival).  If *return_exceptions* is True,
    exceptions in the tasks are treated the same as successful
    results, and gathered in the result list; otherwise, the first
    raised exception will be immediately propagated to the returned
    future.

    Cancellation: if the outer Future is cancelled, all children (that
    have not completed yet) are also cancelled.  If any child is
    cancelled, this is treated as if it raised CancelledError --
    the outer Future is *not* cancelled in this case.  (This is to
    prevent the cancellation of one child to cause other children to
    be cancelled.)

    If *return_exceptions* is False, cancelling gather() after it
    has been marked done won't cancel any submitted awaitables.
    For instance, gather can be marked done after propagating an
    exception to the caller, therefore, calling ``gather.cancel()``
    after catching an exception (raised by one of the awaitables) from
    gather won't cancel any other awaitables.

    Details:

    1) If coros_or_futures is empty then a future is created, it's result
       is immediately set to an empty list,
        which will result in that our awaiter/caller will receive the empty list
        right away (not during event loop's next iteration but proper
        synchronously during this iteration):
       As if he called something like:
       def gather(*coros_or_futures, return_exceptions=False):
           if not coros_or_futures:
               return []

    2) All coros, tasks, futures are added to a dict 'arg_to_fut', where keys
       are the original objects passed in and values - result of calling
       `ensure_future` on the same objects
       (if coro, then a Task is created and scheduled
       to be run during event loop's next iteration, else the same object
       is returned from the call)
       This dict is used to discard duplicate Task/Futures

    3) A counter `nfuts` is also incremented for every coro, future or task
        passed in

    4) Every Task/Future is also added to a `children` list which will be
       used in tandem with a _GatheringFuture

    5) An `outer` _GatheringFuture is created with the `children` list, when
        cancelling it all of it's children will be canceled. This `outer`
        _GatheringFuture is returned to the caller to be 'awaited'

    6) AND NOW HERE COMES THE TRICK:

       - every Task/Future from the 'gathered' ones gets a done callback
         attached (`_done_callback(fut)`)

       - when a future completes the done callback is scheduled to run during
         event loop's next iteration:
           a) this done callback increments a counter of finished Tasks/Futures

           b) if the outer _GatheringFuture is done for some reason the callback
              just returns

           c) if return_exceptions=False and the Task/Future is cancelled
              its Cancelled exception is set on the outer _GatheringFuture,
              so during event loop's next iteration when coro.send happens
              the _GatheringFuture's __await__ method will return a result to
              our awaiter that will raise a CancelledError

           d) if return_exceptions=False and the Task/Future resulted in an
              exception after its completion this exception is set
              on the outer _GatheringFuture, so during event loop's
              next iteration hen coro.send happens
              the _GatheringFuture's __await__ method will return a result to
              our awaiter that will raise this exception

           e) else if return_exceptions=True and all passed in coros, tasks,
              futures have completed we iterate through every Task/Future
              in `children` and add their CancelledErrors, Exceptions or
              results to the result list.

           f) If in the mean time the outer _GatheringFuture was requested
              to be cancelled we ignore the result list and set a cancelled
              error onto the outer _GatheringFuture which so during
              event loop's next iteration when coro.send happens
              the _GatheringFuture's __await__ method will return a result to
              our awaiter that will raise a CancelledError. The result list
              will be ignored

           g) Else, if everything is ok, we set the result list consisting
              of children's results, exceptions and cancelled errors as a result
              on the outer _GatheringFuture, so during
              event loop's next iteration when coro.send happens
              the _GatheringFuture's __await__ method will return the resulting
              list to our awaiter
    """

    # 1) if *coros_or_futures was empty just create a future and immediately
    # complete it by settings its result to an empty list
    # 2) in this case a call to await outer via gather()
    # will just immediately cause a StopIteration exception to be propagated
    # with the value of outer.result()
    # 3) the caller of the await gather() will get the empty list instantly
    if not coros_or_futures:
        loop = events.get_event_loop()
        outer = loop.create_future()
        outer.set_result([])
        return outer

    def _done_callback(fut):
        nonlocal nfinished
        nfinished += 1

        if outer.done():
            if not fut.cancelled():
                # Mark exception retrieved.
                # fut.__log_traceback will be set to False
                # and the exception will not be logged during fut's GC
                fut.exception()
            return

        if not return_exceptions:
            if fut.cancelled():
                # Check if 'fut' is cancelled first, as
                # 'fut.exception()' will *raise* a CancelledError
                # instead of returning it.
                exc = fut._make_cancelled_error()

                # this will set exc on the outer future and schedule all it's
                # done callbacks, so when the most outer Task's .__wakeup
                # method is called as a done callback the previously set
                # exception will be raised as a result of outer.result()
                # and the Task will do coro.throw(this exception)
                # so the the caller which is awaiting us (our outer future)
                # will get this exception raised as the 'await gather/outer'
                # result (will be a CancellationError)
                outer.set_exception(exc)
                return
            else:
                exc = fut.exception()
                if exc is not None:
                    # this will result in the same stuff as described in the
                    # previous comment bu the exception thrown into the coro
                    # will be some other exception (not a CancelledError)
                    outer.set_exception(exc)
                    return

        if nfinished == nfuts:
            # All futures are done; create a list of results
            # and set it to the 'outer' future.
            results = []

            # basically adds result values, CancelledErrors and other exceptions
            # to the results list
            for fut in children:
                if fut.cancelled():
                    # Check if 'fut' is cancelled first, as 'fut.exception()'
                    # will *raise* a CancelledError instead of returning it.
                    # Also, since we're adding the exception return value
                    # to 'results' instead of raising it, don't bother
                    # setting __context__.  This also lets us preserve
                    # calling '_make_cancelled_error()' at most once.
                    res = exceptions.CancelledError(
                        '' if fut._cancel_message is None else
                        fut._cancel_message)
                else:
                    res = fut.exception()
                    if res is None:
                        res = fut.result()
                results.append(res)

            if outer._cancel_requested:
                # If gather is being cancelled we must propagate the
                # cancellation regardless of *return_exceptions* argument.
                # See issue 32684.

                # this will set exc on the outer future and schedule all it's
                # done callbacks, so when the most outer Task's .__wakeup
                # method is called as a done callback the previously set
                # exception will be raised as a result of outer.result()
                # and the Task will do coro.throw(this exception)
                # so the the caller which is awaiting us (our outer future)
                # will get this exception raised as the 'await gather/outer'
                # result (will be a CancellationError)

                # So in the end the caller, even through all results were
                # ready will get a CancelledError raised, the gathered results
                # will just be thrown away
                exc = fut._make_cancelled_error()
                outer.set_exception(exc)
            else:
                # this will set the list of all future results onto
                # the outer _GatheringFuture and the outer most Task's
                # .__wakeup method will be scheduled, and when it will
                # be executed the result list will be propagated to our
                # caller as .result() from the _GatheringFuture
                outer.set_result(results)

    arg_to_fut = {}
    children = []
    nfuts = 0
    nfinished = 0
    loop = None

    for arg in coros_or_futures:
        if arg not in arg_to_fut:
            # create a Task and schedule it to be called during event loop's
            # next iteration
            fut = ensure_future(arg, loop=loop)
            if loop is None:
                loop = futures._get_loop(fut)
            if fut is not arg:
                # 'arg' was not a Future, therefore, 'fut' is a new
                # Future created specifically for 'arg'.  Since the caller
                # can't control it, disable the "destroy pending task"
                # warning.
                fut._log_destroy_pending = False

            nfuts += 1
            arg_to_fut[arg] = fut
            # when the Task/Future completes this done callback will increment
            # nfinished and check it against nfuts, if they are equal - results
            # will be collected from all the Tasks/Futures and added to the
            # outer gathering future
            fut.add_done_callback(_done_callback)

        else:
            # There's a duplicate Future object in coros_or_futures.
            fut = arg_to_fut[arg]

        children.append(fut)

    outer = _GatheringFuture(children, loop=loop)
    return outer


def shield(arg):
    """Wait for a future, shielding it from cancellation.

    The statement

        res = await shield(something())

    is exactly equivalent to the statement

        res = await something()

    *except* that if the coroutine containing it is cancelled, the
    task running in something() is not cancelled.  From the POV of
    something(), the cancellation did not happen.  But its caller is
    still cancelled, so the yield-from expression still raises
    CancelledError.  Note: If something() is cancelled by other means
    this will still cancel shield().

    If you want to completely ignore cancellation (not recommended)
    you can combine shield() with a try/except clause, as follows:

        try:
            res = await shield(something())
        except CancelledError:
            res = None

    Details:
    await shield(something())
    Basically returns another future from the shield call, not the resulting Task
    from something()

    1) Create a task from the coro passed to shield, which will be scheduled
       to run during event loop's next iteration

    2) Creates another future bound to the same event loop as the passed in
       coro's Task. This future is bubbled up to the Task that did the coro.send
       to us.

    3) If the newly created future gets cancelled, in its done callback it
       removes the done callback from the user's Task
       (this callback would have set the user's Task result
       or exception on the newly created future)
    """

    # create a Task from coro and schedule it to run during event loop's
    # next iteration (if not already a Future or Task)
    inner = ensure_future(arg)
    if inner.done():
        # Shortcut.
        return inner

    loop = futures._get_loop(inner)
    # create a future bound to the same loop as the one that was passed in
    outer = loop.create_future()

    def _inner_done_callback(inner):
        if outer.cancelled():
            if not inner.cancelled():
                # Mark inner's result as retrieved.
                inner.exception()
            return

        # cancel the outer future instead of the Task created from the coro
        # passed in (when cancelled the outer caller will remove this callback
        # from thr Task created from the coro passed in)
        if inner.cancelled():
            outer.cancel()
        else:
            exc = inner.exception()
            if exc is not None:
                outer.set_exception(exc)
            else:
                outer.set_result(inner.result())

    def _outer_done_callback(outer):
        if not inner.done():
            inner.remove_done_callback(_inner_done_callback)

    inner.add_done_callback(_inner_done_callback)
    outer.add_done_callback(_outer_done_callback)

    return outer


def run_coroutine_threadsafe(coro, loop):
    """Submit a coroutine object to a given event loop.

    Return a concurrent.futures.Future to access the result.

    This allows us to add coros and schedule the to run via Tasks
    to a loop running in a different thread
    (different from the one we're currently) and then retrieve the result
    from a concurrent.futures.Future returned to us inside our thread.
    """
    if not coroutines.iscoroutine(coro):
        raise TypeError('A coroutine object is required')
    future = concurrent.futures.Future()

    def callback():
        try:
            # 1) Ensure future creates a Task wrapping our coro, which will
            #    be scheduled to run during event loop's next iteration

            # 2) when the Task completes it's result/exception will be copied
            #    to our concurrent.futures.Future
            futures._chain_future(ensure_future(coro, loop=loop), future)
        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException as exc:
            if future.set_running_or_notify_cancel():
                future.set_exception(exc)
            raise

    loop.call_soon_threadsafe(callback)
    return future


# WeakSet containing all alive tasks.
# Is implemented as a WeakSet in C (basically the same stuff here)
_all_tasks = weakref.WeakSet()

# Dictionary containing tasks that are currently active in
# all running event loops.  {EventLoop: Task}
# entries are entered via _enter_task(loop, task) at the beginning of
# Task.__step() and removed via _leave_task(self._loop, self) at the end of
# a Task.__step() call.
# Is initialized in C code in module_init:
# current_tasks = PyDict_New();
_current_tasks = {}


def _register_task(task):
    """
    Register a new task in asyncio as executed by loop.

    Implemented in C:
        register_task - does the same stuff
    """
    _all_tasks.add(task)


def _enter_task(loop, task):
    """
    Implemented in C:
    _asyncio__enter_task_impl => enter_task - does pretty much
    the same stuff as this pure python implementation
    """
    current_task = _current_tasks.get(loop)
    if current_task is not None:
        raise RuntimeError(f"Cannot enter into task {task!r} while another "
                           f"task {current_task!r} is being executed.")
    _current_tasks[loop] = task


def _leave_task(loop, task):
    """
    Implemented in C:
    _asyncio__leave_task_impl => leave_task - does pretty much
    the same stuff as this pure python implementation
    """
    current_task = _current_tasks.get(loop)
    if current_task is not task:
        raise RuntimeError(f"Leaving task {task!r} does not match "
                           f"the current task {current_task!r}.")
    del _current_tasks[loop]


def _unregister_task(task):
    """
    Unregister a task.

    Implemented in C:
        unregister_task - does the same stuff
    """
    _all_tasks.discard(task)


_py_register_task = _register_task
_py_unregister_task = _unregister_task
_py_enter_task = _enter_task
_py_leave_task = _leave_task

try:
    from _asyncio import (_register_task, _unregister_task,
                          _enter_task, _leave_task,
                          _all_tasks, _current_tasks)
except ImportError:
    pass
else:
    _c_register_task = _register_task
    _c_unregister_task = _unregister_task
    _c_enter_task = _enter_task
    _c_leave_task = _leave_task
