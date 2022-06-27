import asyncio
import types

@types.coroutine
def counter(n):
    while True:
        n += 1
        yield n

async def check():
    await asyncio.sleep(3)

asyncio.run(check)


    
