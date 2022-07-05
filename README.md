[BaseEventLoop](#loop)
- [\_run\_once](#runonce)
- [run_until_complete](#run_until_complete)
- [run_forever](#run_forever)

[Future](#future)
- [\_\_init\_\_](#future__init__)
- [\_\await\_\_](#future__await__)
- [set_result](#future_set_result)

[Task](#task)
- [\_\_init\_\_](#task__init__)
- [\_\_step\_\_](#task__step__)

[Selectors](#selectors)

<a id="loop"><h3>BaseEventLoop</h3></a>

<a id="runonce"><h4>_run_once</h4></a>

Run one full iteration of the event loop if there is not scheduled 
`TimerHandles` jump to `5 step`

1. Define `scheduled_count` of .\_scheduled `TimerHandles`
2. Check that count is greater than minimum number of scheduled
   `TimerHandles`(100)
   and current count of cancelled `TimerHandles` greater than 50%
   of `scheduled_count` before remove all delayed calls. If their number isn't
   too high remove(pop) delayed call from head of `heapq`, decrement cancelled
   handles count and mark handle as not scheduled while head in cancelled state.
   **Note:**
   At this point cancelled handles removed only from the top of queue, until
   caught handle that not cancelled.
3. **TODO** self._selector.select(event_list) when you have better
   **TODO** understanding selectors
4. In while loop remove(pop) handles in scheduled heap queue and append
   to `_ready` queue for those whose time has come. In scenario when head of
   heap queue is not ready yet just break out of the loop.
5. Iterate over `_ready` queue, pop handle(skip cancelled) and run them one by
   one using `_run` method which just call callback
   *In debug mode, if the execution of a callback or a step of a task exceed
   this duration in seconds(0.1), the slow callback/task is logged.*
   Iteration is over: handle set to None for break cycles when an exception
   occurs.   
   **Note:**
   This is the only place where callbacks are actually *called*. All other
   places just add them to `_ready` queue. We run all currently scheduled
   callbacks, but not any callbacks scheduled by callbacks run this time around
   -- they will be run the next time (after another I/O poll). Use an idiom that
   is thread-safe without using locks.
   


<a id="future"><h3>Future</h3></a>

Future - object which represents some action that will be completed 
"in future" and stores state, result/error of this action in itself and 
some instructions what need to do after this action.

`_asyncio_future_blocking` - field used for a dual purpose:

<a id="future__init__"><h4>\_\_init\_\_</h4></a>

<a id="future__await__"><h4>\_\_await\_\_</h4></a>

<a id="task"><h3>Task</h3></a>
Task - a coroutine wrapped in a Future
**TODO** better description

<a id="task__init__"><h4>\_\_init\_\_</h4></a>
1. 
Instantiate itself as Future all method are defined in parent 
class also available in Task except `set_result` and `set_exception`.
   
2. Check coro is instance of Awaitable, Generator or Coroutine, raise
TypeError if not.
   
3. If `name` isn't provided set to default Task-<next value in sequence>
4. `_num_cancels_requested` to 0 **TODO describe attr**
5. `_must_cancel` this attr used in `__step` for cancel task
6. `_fut_waiter` **TODO describe attr** 
7. `coro` - received instance of Awaitable, Generator or Coroutine
8. `context` -  generic mechanism of ensuring consistent access
   to non-local state in the context of out-of-order execution,
   such as in Python generators and coroutines.   
9. Wraps the task in Handle class with `__step` callback and append
to `_ready` queue in loop which call this handle as soon as possible.
   
10. Add task to global WeakSet containing all alive tasks


<a id="task__step__"><h4>\_\_step\_\_</h4></a>
This method is like a heartbeat of event loop
When loop is start running at least one Task will be created by the steps
described earlier.
1. If task is already done raise InvalidStateError.
2. In a scenario that optional arg  `exc` is passed  and task must cancelled
reset `exc` as CancelledException
