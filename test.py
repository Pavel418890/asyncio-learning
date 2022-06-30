import asyncio
import types


async def hello():
    print('helo')
    await asyncio.sleep(1)
    print('world')

task = asyncio.Task(hello(), name='hello')

loop = asyncio.get_event_loop()
loop.run_until_complete(task)