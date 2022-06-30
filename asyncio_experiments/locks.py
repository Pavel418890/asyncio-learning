"""Synchronization primitives."""

import collections

from . import exceptions
from . import mixins


class _ContextManagerMixin:
    async def __aenter__(self):
        await self.acquire()
        # We have no use for the "as ..."  clause in the with
        # statement for locks.
        return None

    async def __aexit__(self, exc_type, exc, tb):
        self.release()


class Lock(_ContextManagerMixin, mixins._LoopBoundMixin):
    """Primitive lock objects.

    A primitive lock is a synchronization primitive that is not owned
    by a particular coroutine when locked.  A primitive lock is in one
    of two states, 'locked' or 'unlocked'.

    It is created in the unlocked state.  It has two basic methods,
    acquire() and release().  When the state is unlocked, acquire()
    changes the state to locked and returns immediately.  When the
    state is locked, acquire() blocks until a call to release() in
    another coroutine changes it to unlocked, then the acquire() call
    resets it to locked and returns.  The release() method should only
    be called in the locked state; it changes the state to unlocked
    and returns immediately.  If an attempt is made to release an
    unlocked lock, a RuntimeError will be raised.

    When more than one coroutine is blocked in acquire() waiting for
    the state to turn to unlocked, only one coroutine proceeds when a
    release() call resets the state to unlocked; first coroutine which
    is blocked in acquire() is being processed.

    acquire() is a coroutine and should be called with 'await'.

    Locks also support the asynchronous context management protocol.
    'async with lock' statement should be used.

    Usage:

        lock = Lock()
        ...
        await lock.acquire()
        try:
            ...
        finally:
            lock.release()

    Context manager usage:

        lock = Lock()
        ...
        async with lock:
             ...

    Lock objects can be tested for locking state:

        if not lock.locked():
           await lock.acquire()
        else:
           # lock is acquired
           ...


    Details:

    1) The heart of this Lock implementation are 2 attributes:
       - ._locked boolean indicating whether this lock is already
          acquired/locked
       - ._waiters - a deque of futures (waiters) which is used as a FIFO,
          every time a coroutine tries to acquire the Lock if it's already locked
          a future will be created and added at the back of the deque.
          Then the .acquire call will block on await waiter future.
          A waiter future will be pulled from the head of the deque for every
          call to .release() and .set_result() will be called on the future
          effectively unblocking the call to .acquire associated with this
          future.

    .acquire:

        1) If when calling this the lock wasn't locked and there are no more
           waiter futures in self._waiters or all the waiter futures are
           cancelled, then self._locked is set to True and this call
           returns immediately (returns True)

        2) Else a waiter future is created bound to the current loop
           and added at the end of self._waiters

        3) The current call to .acquire 'blocks/suspends itself' by calling
           'await fut'

        4) 'await fut' will 'unblock/resume' when someone in another coroutine
           calls lock.release() => self._wake_up_first() which will
           pull the first waiter future from self._waiters and set its result
           effectively scheduling Task's ._wakeup method to run during
           loop's next iteration

        5) when 'await fut' unblocks we will remove this future from ._waiters
           and if it wasn't cancelled will set ._locked to True and return True.
           The lock was acquired again. And the above process will repeat itself.

        6) If for some reason the waiter future was cancelled while .acquire
           was suspended on 'await fut' then Task's .__step method will
           throw a cancelled error which will be raised in .acquire.
           In that case if the lock is not already locked we wake up the next
           waiter future and re-raise the CancelledError

    .release:

        1) if lock is locked we set self._locked to False and schedule the
           next waiter futures done callback to be called during loop's
           next iteration (.__wakeup => .__step) which will effectively unblock
           the call to .acquire in another coroutine
           (was suspended on 'await fut')

    ._wake_up_first:

        1) Get the waiter future from the head of the deque
        2) If it's not done then call .set_result(True) on it effectively
           scheduling the outer most Task's
           (associated with the future pulled out from the deque)
           .__wakeup method to be called during loop's next iteration,
           it will unblock 'await fut' in the .acquire call associated with
           the pulled out future
    """

    def __init__(self, *, loop=mixins._marker):
        super().__init__(loop=loop)
        # is a deque of waiter futures
        self._waiters = None
        self._locked = False

    def __repr__(self):
        res = super().__repr__()
        extra = 'locked' if self._locked else 'unlocked'
        if self._waiters:
            extra = f'{extra}, waiters:{len(self._waiters)}'
        return f'<{res[1:-1]} [{extra}]>'

    def locked(self):
        """Return True if lock is acquired."""
        return self._locked

    async def acquire(self):
        """Acquire a lock.

        This method blocks until the lock is unlocked, then sets it to
        locked and returns True.
        """

        # if _locked is False and there are no waiter futures in the queue,
        # or all the waiter futures are cancelled, just set _locked to
        # True and return True
        if (not self._locked and (self._waiters is None
                                  or all(w.cancelled() for w in self._waiters))):
            self._locked = True
            return True

        # waiter - a future is created bound to the current loop and added to
        # self._waiters (deque)
        if self._waiters is None:
            self._waiters = collections.deque()
        fut = self._get_loop().create_future()
        self._waiters.append(fut)

        # Finally block should be called before the CancelledError
        # handling as we don't want CancelledError to call
        # _wake_up_first() and attempt to wake up itself (meaning this future
        # 'await fut')
        try:
            try:
                # this will yield itself to the outer most Task and
                # suspend this coroutines execution
                # (effectively blocking the caller)
                # Execution will be resumed when this waiter future completes
                # and as one of its done callbacks the outer most Task's
                # .__wakeup method will be called triggering
                # .__step => coro.send(None)
                # If this future gets cancelled .__step will do coro.throw(exc)
                # that's why we catch the CancellationError
                await fut
            finally:
                self._waiters.remove(fut)
        except exceptions.CancelledError:
            if not self._locked:
                self._wake_up_first()
            raise

        self._locked = True
        return True

    def release(self):
        """Release a lock.

        When the lock is locked, reset it to unlocked, and return.
        If any other coroutines are blocked waiting for the lock to become
        unlocked, allow exactly one of them to proceed.

        When invoked on an unlocked lock, a RuntimeError is raised.

        There is no return value.
        """
        if self._locked:
            self._locked = False
            self._wake_up_first()
        else:
            raise RuntimeError('Lock is not acquired.')

    def _wake_up_first(self):
        """Wake up the first waiter if it isn't done."""
        if not self._waiters:
            return
        try:
            fut = next(iter(self._waiters))
        except StopIteration:
            return

        # .done() necessarily means that a waiter will wake up later on and
        # either take the lock, or, if it was cancelled and lock wasn't
        # taken already, will hit this again and wake up a new waiter.
        if not fut.done():
            # this will schedule fut's done callbacks to be run during
            # loop's next iteration, meaning the outer most Task's .__wakeup
            # method via .__send wil
            fut.set_result(True)


class Event(mixins._LoopBoundMixin):
    """Asynchronous equivalent to threading.Event.

    Class implementing event objects. An event manages a flag that can be set
    to true with the set() method and reset to false with the clear() method.
    The wait() method blocks until the flag is true. The flag is initially
    false.

    Details:

    1) Calls to .wait if self._value is set to False, create a waiter future,
       add it to self._waiters and block on 'await waiter future'
    2) Calls to .set set self._value to True and call .set_result(True)
       on every waiter future in self._waiters - all calls to .wait
       will be unblocked during loop's next iteration
    """

    def __init__(self, *, loop=mixins._marker):
        super().__init__(loop=loop)
        # is a deque of waiter futures
        self._waiters = collections.deque()
        self._value = False

    def __repr__(self):
        res = super().__repr__()
        extra = 'set' if self._value else 'unset'
        if self._waiters:
            extra = f'{extra}, waiters:{len(self._waiters)}'
        return f'<{res[1:-1]} [{extra}]>'

    def is_set(self):
        """Return True if and only if the internal flag is true."""
        return self._value

    def set(self):
        """Set the internal flag to true. All coroutines waiting for it to
        become true are awakened. Coroutine that call wait() once the flag is
        true will not block at all.
        """
        if not self._value:
            self._value = True

            for fut in self._waiters:
                if not fut.done():
                    # this will unblock all calls to '.wait' => 'await fut'
                    # by scheduling the outer most Task's
                    # (associated with each 'fut') .__wakeup method =>
                    # .__step => coro.send(True)
                    fut.set_result(True)

    def clear(self):
        """Reset the internal flag to false. Subsequently, coroutines calling
        wait() will block until set() is called to set the internal flag
        to true again."""
        self._value = False

    async def wait(self):
        """Block until the internal flag is true.

        If the internal flag is true on entry, return True
        immediately.  Otherwise, block until another coroutine calls
        set() to set the flag to true, then return True.
        """
        if self._value:
            return True

        fut = self._get_loop().create_future()
        self._waiters.append(fut)
        try:
            await fut
            return True
        finally:
            self._waiters.remove(fut)


class Condition(_ContextManagerMixin, mixins._LoopBoundMixin):
    """Asynchronous equivalent to threading.Condition.

    This class implements condition variable objects. A condition variable
    allows one or more coroutines to wait until they are notified by another
    coroutine.

    A new Lock object is created and used as the underlying lock.
    """

    def __init__(self, lock=None, *, loop=mixins._marker):
        super().__init__(loop=loop)
        if lock is None:
            lock = Lock()
        elif lock._loop is not self._get_loop():
            raise ValueError("loop argument must agree with lock")

        self._lock = lock
        # Export the lock's locked(), acquire() and release() methods.
        self.locked = lock.locked
        self.acquire = lock.acquire
        self.release = lock.release

        # is a deque of waiter futures
        self._waiters = collections.deque()

    def __repr__(self):
        res = super().__repr__()
        extra = 'locked' if self.locked() else 'unlocked'
        if self._waiters:
            extra = f'{extra}, waiters:{len(self._waiters)}'
        return f'<{res[1:-1]} [{extra}]>'

    async def wait(self):
        """Wait until notified.

        If the calling coroutine has not acquired the lock when this
        method is called, a RuntimeError is raised.

        This method releases the underlying lock, and then blocks
        until it is awakened by a notify() or notify_all() call for
        the same condition variable in another coroutine.  Once
        awakened, it re-acquires the lock and returns True.
        """

        if not self.locked():
            raise RuntimeError('cannot wait on un-acquired lock')

        # release the underlying lock
        # will pull and 'unblock' one waiter future from
        # the lock's _waiters deque
        self.release()
        try:
            fut = self._get_loop().create_future()
            self._waiters.append(fut)
            try:
                # will block here
                await fut
                return True
            finally:
                self._waiters.remove(fut)

        finally:
            # Must reacquire lock even if wait is cancelled
            cancelled = False
            while True:
                try:
                    # this will make all other coro's trying
                    # to acquire the lock and then call .wait
                    # on this condition variable to block
                    await self.acquire()
                    break
                except exceptions.CancelledError:
                    cancelled = True

            if cancelled:
                raise exceptions.CancelledError

    async def wait_for(self, predicate):
        """Wait until a predicate becomes true.

        The predicate should be a callable which result will be
        interpreted as a boolean value.  The final predicate value is
        the return value.
        """
        result = predicate()
        while not result:
            await self.wait()
            result = predicate()
        return result

    def notify(self, n=1):
        """By default, wake up one coroutine waiting on this condition, if any.
        If the calling coroutine has not acquired the lock when this method
        is called, a RuntimeError is raised.

        This method wakes up at most n of the coroutines waiting for the
        condition variable; it is a no-op if no coroutines are waiting.

        Note: an awakened coroutine does not actually return from its
        wait() call until it can reacquire the lock. Since notify() does
        not release the lock, its caller should.
        """

        if not self.locked():
            raise RuntimeError('cannot notify on un-acquired lock')

        # release n waiters
        # all .wait calls will not unblock immediately
        # will unblock one by one as each coro associated with the
        # .wait call releases the lock
        idx = 0
        for fut in self._waiters:
            if idx >= n:
                break

            if not fut.done():
                idx += 1
                fut.set_result(False)

    def notify_all(self):
        """Wake up all threads waiting on this condition. This method acts
        like notify(), but wakes up all waiting threads instead of one. If the
        calling thread has not acquired the lock when this method is called,
        a RuntimeError is raised.
        """
        self.notify(len(self._waiters))


class Semaphore(_ContextManagerMixin, mixins._LoopBoundMixin):
    """A Semaphore implementation.

    A semaphore manages an internal counter which is decremented by each
    acquire() call and incremented by each release() call. The counter
    can never go below zero; when acquire() finds that it is zero, it blocks,
    waiting until some other thread calls release().

    Semaphores also support the context management protocol.

    The optional argument gives the initial value for the internal
    counter; it defaults to 1. If the value given is less than 0,
    ValueError is raised.
    """

    def __init__(self, value=1, *, loop=mixins._marker):
        super().__init__(loop=loop)
        if value < 0:
            raise ValueError("Semaphore initial value must be >= 0")
        self._value = value
        self._waiters = collections.deque()

    def __repr__(self):
        res = super().__repr__()
        extra = 'locked' if self.locked() else f'unlocked, value:{self._value}'
        if self._waiters:
            extra = f'{extra}, waiters:{len(self._waiters)}'
        return f'<{res[1:-1]} [{extra}]>'

    def _wake_up_next(self):
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return

    def locked(self):
        """Returns True if semaphore can not be acquired immediately."""
        return self._value == 0

    async def acquire(self):
        """Acquire a semaphore.

        If the internal counter is larger than zero on entry,
        decrement it by one and return True immediately.  If it is
        zero on entry, block, waiting until some other coroutine has
        called release() to make it larger than 0, and then return
        True.
        """
        while self._value <= 0:
            fut = self._get_loop().create_future()
            self._waiters.append(fut)
            try:
                await fut
            except:
                # See the similar code in Queue.get.
                fut.cancel()  # Just in case fut is not done yet.
                # only called when there are waiters and there was an exception
                # but it was raised when the future was done (not cancelled)
                if self._value > 0 and not fut.cancelled():
                    self._wake_up_next()
                raise
        self._value -= 1
        return True

    def release(self):
        """Release a semaphore, incrementing the internal counter by one.
        When it was zero on entry and another coroutine is waiting for it to
        become larger than zero again, wake up that coroutine.
        """
        self._value += 1
        self._wake_up_next()


class BoundedSemaphore(Semaphore):
    """A bounded semaphore implementation.

    This raises ValueError in release() if it would increase the value
    above the initial value.
    """

    def __init__(self, value=1, *, loop=mixins._marker):
        self._bound_value = value
        super().__init__(value, loop=loop)

    def release(self):
        if self._value >= self._bound_value:
            raise ValueError('BoundedSemaphore released too many times')
        super().release()
