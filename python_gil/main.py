"""
GIL algorithm:

    Main GIL components:
        - is just a boolean variable (locked) whose access is protected
          by a mutex (gil_mutex), and whose changes are signalled by a condition
          variable (gil_cond)
        - the GIL-holding thread, the main loop (PyEval_EvalFrameEx) must be
          able to release the GIL on demand by another thread. A volatile boolean
          variable (gil_drop_request) is used for that purpose, which is checked
          at every turn of the eval loop.

    To change default timeout for thread switching `sys.setswitchinterval(0.001)`
    can be used (set to 1 ms in this case).

    High-level overview:
        - Python bytecode (opcodes) is executed by a thread within an
          execution loop PyEval_EvalFrameEx. When the thread is created
          and before entering the execution loop it tries to acquire the GIL.

        - If the GIL is locked the competing thread will block for 5ms (default)
          on awaiting a condition variable (gil_cond) to be signalled
          (signalled when GIL-holding thread drops the GIL).
          If waiting times out the competing thread will request the GIL
          by setting gil_drop_request to True and block again on the
          condition variable.

        - The GIL-holding thread will notice the GIL drop request at the
          beginning of another iteration of its bytecode eval loop and
          will drop the GIL signalling on the condition variable which will
          unblock the competing thread without timeout and it will acquire the
          GIL.

        - After the GIL-holding thread drops the GIL it may block on awaiting
          another condition variable (switch_cond) which will prevent it
          from repeatedly reacquiring the GIL causing other threads to starve.
          It will be unblocked when another competing thread successfully takes
          the GIL and signals on that condition variable. After that the
          thread that just dropped the GIL will try to reacquire it again
          effectively blocking until the whole process is repeated all
          over again.

    Algorithm:

    1) Take GIL:
       - Acquire the gil_mutex and check if the GIL is currently locked

       - If locked, enter a loop until the GIL will be unlocked

       - Within the loop copy gil_switch_number into a local variable
         and block for 5ms on awaiting gil_cond to be signalled

       - If a timeout occurs and the condition variable was not signalled,
         check whether the GIL is still locked and whether another thread
         has already issued a GIL drop request by setting gil_drop_request
         to True (done with the help of gil_switch_number - compare local
         value to global variable)

       - If no competing thread has issued a GIL drop request set
         gil_drop_request to True

       - Enter another iteration of the while loop and block for 5ms
         on awaiting gil_cond to be signalled

       - This time though the thread holding the GIL at the beginning of
         another iteration of its eval loop will find that gil_drop_request is
         True which will force it to drop the GIL signalling on the gil_cond
         effectively unblocking our thread (or another competing thread)
         blocked on awaiting gil_cond

       - After receiving signal without timeout on gil_cond the competing thread
         will break from the while loop

       - Acquire the switch_cond variable to block the thread that just dropped
         the GIL from immediately reacquiring it again

       - Will set lock variable to True

       - Set gil_last_holder to its own thread id - to prevent the thread
         that just dropped the GIL from indefinitely awaiting switch_cond
         to be signalled - this can happen if the thread dropped the GIL,
         signalled the fact to competing threads and before trying to block
         on awaiting switch_cond to be signalled a context switch happens and
         the competing thread acquires the GIL and signals on switch_cond, but
         there's no thread to receive the signal. When we switch back to the
         thread that just dropped the GIL and try to wait for switch_cond to
         be signalled - this can lead to waiting indefinitely.

       - Signal on switch_cond to unblock the thread that just dropped the GIL,
         if it previously blocked on awaiting switch_cond to be signalled.
         After this the thread that just dropped the GIL will try to reacquire
         the GIL if it needs to, thus block on gil_cond for 5ms until timeout

       - Release switch_cond lock

       - Set gil_drop_request to False

       - Release gil_mutex

    2) Drop GIL:

       - Acquire the gil_mutex

       - Set gil_last_holder to the current thread id which is dropping the GIL

       - Set gil_locked to False

       - Signal on gil_cond to to wake up one of the competing threads, it
         will take the GIL when it wakes up

       - Release the gil_mutex

       - If the GIL was dropped by this thread because another competing
         thread timed out waiting for the GIL and requested the drop via setting
         gil_drop_request to True, then this thread tries
         to acquire the switch_cond lock

       - If before acquiring the switch_cond lock a context switch happened
         and another competing thread has successfully taken the GIL,
         gil_last_holder will be set to that thread's id. When context is
         switched back to our thread we will notice it and just release the
         switch_cond lock, because there's no need to block on waiting for
         switch_cond variable to be signalled (to prevent our thread from
         acquiring the GIL a second time in a row, causing other threads to
         starve) - a competing thread has already successfully acquired the GIL.

       - If a context switch didn't occur and we're still in our GIL dropping
         thread we block on awaiting switch_cond variable to be signalled by
         another competing thread which will take the GIL when context switches
         to it. This prevents this thread from repeatedly reacquiring the
         GIL causing other threads to starve. When another competing thread
         acquires the GIL it will signal on switch_cond effectively
         unblocking our thread when context switches back to it.

       - Release switch_cond lock
"""

import threading
from types import SimpleNamespace

# to change default timeout for thread switching `sys.setswitchinterval(0.001)`
# can be used (set to 1 ms in this case)
DEFAULT_INTERVAL = 0.05  # Default timeout for thread switching is 5 milliseconds

gil_mutex = threading.RLock()
gil_condition = threading.Condition(lock=gil_mutex)
# This condition variable is used to block the thread releasing the GIL
# from immediately reacquiring it again, causing other threads to starve
switch_condition = threading.Condition()

# A simple object subclass that provides attribute access to its namespace,
# as well as a meaningful repr.
# Unlike object, with SimpleNamespace you can add and remove attributes.
# If a SimpleNamespace object is initialized with keyword arguments,
# those are directly added to the underlying namespace.
# ------------------------------------------------------------------------------
# Dictionary-like object that supports dot (attribute) syntax
gil = SimpleNamespace(
    drop_request=False,
    locked=True,
    switch_number=0,
    last_holder=None,
    eval_breaker=True
)


def drop_gil(thread_id):
    if not gil.locked:
        raise Exception("GIL is not locked")

    gil_mutex.acquire()

    # `gil.last_holder` is used to skip blocking this thread on
    # `switch_condition` if a context switch occurred during this call
    # and another thread has taken the GIL, `gil.last_holder` will be equal
    # to the other thread's id that acquired the GIL, thus there's no need
    # to block this thread to prevent it from immediately reacquiring the GIL,
    # another thread has already acquired it.
    gil.last_holder = thread_id
    gil.locked = False

    # Signals that the GIL is now available for acquiring to the first
    # awaiting thread. The thread that is 'awakened' will acquire the GIL.
    gil_condition.notify()

    gil_mutex.release()

    # force switching
    # Lock current thread so it will not immediately reacquire the GIL
    # this ensures that another GIL-awaiting thread has a chance to get
    # scheduled

    # `gil.drop_request` is set to True when another thread tries to acquire
    #  the GIL (via `take_gil`) and it wasn't able to accomplish this within
    #  a 5ms time slice (5ms by default).

    # We only use `switch_condition` blocking hack if `gil.drop_request` was
    # set because this means that the current thread was forced to release the
    # GIL by another thread's request and because of that there is more stuff
    # to be done by this thread and there is a very high chance that it will
    # reacquire the GIL instantly if we do not block on `switch_condition`,
    # causing other threads to starve and extensive thrashing on multicore.
    if gil.drop_request:
        switch_condition.acquire()
        # We only try to wait on `switch_condition` if `gil.last_holder`
        # is still equal to this thread's id, meaning that another requesting
        # thread hasn't yet acquired the GIL. By blocking on `switch_condition`
        # we give another thread a chance to acquire the GIL. The thread that
        # will eventually acquire the GIL will signal on `switch_condition`
        # effectively unblocking this thread.

        # If `gil.last_holder` isn't equal to this thread's id, this means
        # that there was a context switch during this function call and another
        # thread has successfully acquired the GIL, thus there's no need to block
        # on `switch_condition`.
        if gil.last_holder == thread_id:
            gil.drop_request = False
            switch_condition.wait()

        switch_condition.release()


def take_gil(thread_id):
    gil_mutex.acquire()

    # We block with timeout of 5ms on `gil_condition` in a loop, so
    # that there is chance to set the `gil.drop_request` flag to True
    # every 5ms until we acquire the GIL (`gil_condition` will be signalled,
    # so timeout will no occur and after it was signalled `gil.locked` will be
    # False, which will break the while loop).
    while gil.locked:
        # `gil.switch_number` is used to check whether another competing
        # thread has made a GIL drop request while this thread was blocked
        # on awaiting `gil_condition` (5ms by default). If another thread
        # has already made a GIL drop request, we block this thread again
        # without settings `gil.drop_request` to True a second time (unnecessary)
        saved_switchnum = gil.switch_number

        # Release the lock and wait for a signal from a GIL holding thread,
        # set drop_request=True if the wait is timed out

        timed_out = not gil_condition.wait(timeout=DEFAULT_INTERVAL)

        # if we timed out and the GIL is still held by another thread and no
        # other thread has made a GIL drop request during the period we were
        # blocked - make a gil drop request by setting `gil.drop_request`
        # volatile variable to True.
        if timed_out and gil.locked and gil.switch_number == saved_switchnum:
            gil.drop_request = True

    # lock for force switching
    switch_condition.acquire()

    # Now we hold the GIL
    gil.locked = True

    if gil.last_holder != thread_id:
        # This is done so that if this thread is able to acquire the GIL
        # before the last GIL holder blocks on `switch_condition`, there
        # will be no need for it to block on it, because if `gil.last_holder`
        # will be equal to this thread's id the other thread that just released
        # the GIL will know that another thread successfully acquired the GIL
        gil.last_holder = thread_id
        # this variable is used to prevent unnecessary duplicate attempts
        # to set `gil.drop_request` to True by other threads competing for the
        # GIL
        gil.switch_number += 1

    # force switching, send signal to drop_gil
    switch_condition.notify()
    switch_condition.release()

    if gil.drop_request:
        gil.drop_request = False

    gil_mutex.release()


# 1) Each thread executes its code in the separate execution_loop which
#    is run by the real OS threads.

# 2) When Python creates a thread it calls the take_gil function before
#    entering the execution_loop. So before calling `execution_loop`
#    `take_gil(thread_id)` will be called.

# 3) Basically, the job of the GIL is to pause the while loop for all threads
#    except for a thread that currently owns the GIL. For example,
#    if you have three threads, two of them will be suspended.
#    Typically but not necessarily, only one Python thread can execute
#    Python opcodes at a time, and the rest will be waiting a split second
#    of time until the GIL will be switched to them.
def execution_loop(target_function, thread_id):
    # Compile Python function down to bytecode and execute it in the while loop

    bytecode = compile(target_function)

    while True:

        # drop_request indicates that one or more threads are awaiting for
        # the GIL
        if gil.drop_request:
            # release the gil from the current thread
            drop_gil(thread_id)

            # immediately request the GIL for the current thread
            # at this point the thread will be waiting for GIL and suspended
            # until the function return
            take_gil(thread_id)

        # bytecode execution logic, executes one instruction at a time
        instruction = bytecode.next_instruction()
        if instruction is not None:
            execute_opcode(instruction)
        else:
            return


"""
Notes about the implementation:

- The GIL is just a boolean variable (locked) whose access is protected
 by a mutex (gil_mutex), and whose changes are signalled by a condition
 variable (gil_cond). gil_mutex is taken for short periods of time,
 and therefore mostly uncontended.

- In the GIL-holding thread, the main loop (PyEval_EvalFrameEx) must be
 able to release the GIL on demand by another thread. A volatile boolean
 variable (gil_drop_request) is used for that purpose, which is checked
 at every turn of the eval loop. That variable is set after a wait of
 `interval` microseconds on `gil_cond` has timed out.

  [Actually, another volatile boolean variable (eval_breaker) is used
   which ORs several conditions into one. Volatile booleans are
   sufficient as inter-thread signalling means since Python is run
   on cache-coherent architectures only.]

- A thread wanting to take the GIL will first let pass a given amount of
 time (`interval` microseconds) before setting gil_drop_request. This
 encourages a defined switching period, but doesn't enforce it since
 opcodes can take an arbitrary time to execute.

 The `interval` value is available for the user to read and modify
 using the Python API `sys.{get,set}switchinterval()`.

- When a thread releases the GIL and gil_drop_request is set, that thread
 ensures that another GIL-awaiting thread gets scheduled.
 It does so by waiting on a condition variable (switch_cond) until
 the value of last_holder is changed to something else than its
 own thread state pointer, indicating that another thread was able to
 take the GIL.

 This is meant to prohibit the latency-adverse behaviour on multi-core
 machines where one thread would speculatively release the GIL, but still
 run and end up being the first to re-acquire it, making the "timeslices"
 much longer than expected.
 (Note: this mechanism is enabled with FORCE_SWITCHING above)
"""
