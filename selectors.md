Selectors - abstract class
supports registering file object to be monitored for specific I/O events.
The default selector uses the most efficient implementation on the current
platform.  
* [select/pselect](#select)
* [poll](#poll)
* [epoll](#epoll)
* [kqueue](#kqueue)
* [devpoll](#devpoll)

<a id="select"><h3>select/pselect</h3></a>
select() and pselect() allow a program to monitor multiple file descriptors,
waiting until one or more of the file descriptors become "ready" for some class
of I/O operation (e.g., input possible).  A file descriptor is considered ready 
if it is possible to perform a corresponding I/O operation 
(e.g., read, or a sufficiently small write) without blocking.
The operation of select() and pselect() is identical,
other than these three differences:

1. select() uses a timeout that is a struct timeval (with seconds and microseconds),
while pselect() uses a struct timespec (with seconds and nanoseconds).

2. select() may update the timeout argument to indicate how much time was left.
pselect() does not change this argument.

3. select() has no sigmask argument, and behaves as pselect() called with NULL sigmask.


Three independent sets of file descriptors are watched.  

1. The file descriptors listed in readfds will be watched to see if characters
become available for reading (more precisely, to see if a read will not block;
in  particular, a file descriptor is also ready on end-of-file).

2. The file descriptors in writefds will be watched to see if space is available
for write (though a large write may still block).

3. The file descriptors in exceptfds will be watched for exceptional conditions.

On exit, each of the file descriptor sets is modified in place to indicate which
file descriptors actually changed status.
(Thus, if using select() within a loop, the sets must be reinitialized before each call.)

Each of the three file descriptor sets may be specified as NULL 
if no file descriptors are to be watched for the corresponding class of events.

The timeout argument specifies the interval that select() should block waiting
for a file descriptor to become ready. The call will block until either:
* a file descriptor becomes ready;
* the call is interrupted by a signal handler; or
* the timeout expires.

Note  that  the  timeout  interval  will be rounded up to the system clock
granularity, and kernel scheduling delays mean that the blocking interval 
may overrun by a small amount.  If both fields of the timeval structure are zero,
then select() returns immediately.  (This is useful for polling.)
If timeout is NULL (no timeout), select() can block indefinitely.


On success, select() and pselect() return the number of file descriptors
contained in the three returned descriptor sets 
(that is, the total number of bits that are set in readfds, writefds, exceptfds)
which may be  zero  if the timeout expires before anything interesting happens.
On error, -1 is returned, and errno is set to indicate the error;
the file descriptor sets are unmodified, and timeout becomes undefined.



Pros:
1. Cross-platform 
2. Working with timeout in nanoseconds if platform support poll and epoll 
work only with milliseconds.
3. Some code calls select() with all three sets empty, nfds zero,
and a non-NULL timeout as a fairly portable way to sleep with subsecond precision.


Cons:
1. If a file descriptor being monitored by select() is closed in another thread,
the result is unspecified.  On some UNIX systems, select() unblocks and returns,
with an indication that the file descriptor is ready (
a subsequent  I/O operation will likely fail with an error,
unless another process reopens file descriptor between the time select()
returned and the I/O operation is performed
).
On Linux (and some other systems), closing the file descriptor in another thread
has no effect on select().
 
2. The Linux kernel imposes no fixed limit, but the glibc implementation makes
fd_set a fixed-size type, with FD_SETSIZE defined as 1024, 
To monitor file descriptors greater than 1023, use poll(2) instead.

3. select()  should  check  all specified file descriptors in the three file
descriptor sets, up to the limit - the highest-numbered file descriptor in any
of the three sets, plus 1). However, the current implementation ignores any file
descriptor in these sets that is greater than the maximum file descriptor number
that the process currently has open.