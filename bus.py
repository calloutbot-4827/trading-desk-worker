"""asyncio queues: scanner -> auditor -> analyst -> executor -> risk."""
import asyncio

STAGES = ("scanner", "auditor", "analyst", "executor", "risk")


class Bus:
    def __init__(self, maxsize=1000):
        # queue named by its consumer: q["auditor"] carries scanner output, etc.
        self.q = {s: asyncio.Queue(maxsize) for s in STAGES[1:]}

    def next_of(self, stage):
        i = STAGES.index(stage)
        return STAGES[i + 1] if i + 1 < len(STAGES) else None

    async def publish(self, from_stage, item):
        nxt = self.next_of(from_stage)
        if nxt is None:
            raise ValueError(f"{from_stage} has no downstream")
        await self.q[nxt].put(item)

    async def consume(self, stage):
        return await self.q[stage].get()
