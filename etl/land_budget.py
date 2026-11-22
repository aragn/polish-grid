"""
One deadline for the whole Land workflow job.

Each step has its own budget in minutes, but GitHub kills the job at its timeout, and the commit is the last step:
budgets that add up past the timeout lose the whole run.  The workflow sets LAND_DEADLINE (Unix seconds) when the job
starts; capped() shortens a step's budget so that it ends `reserve` minutes before that deadline, leaving room for the
steps after it that have no budget of their own (protected areas, flood, neighbours, the commit) and for the work a
step finishes after its budget runs out.  Without LAND_DEADLINE (a run on the laptop) budgets are left as they are.
"""

import os
import time


def capped(minutes, reserve, step):
    deadline = os.environ.get("LAND_DEADLINE")
    if not deadline:
        return minutes
    left = (float(deadline) - time.time()) / 60 - reserve
    if left < minutes:
        print(f"{step}: budget cut from {minutes:.0f} to {max(left, 0):.0f} minutes to end before the job's time limit", flush=True)
    return max(0.0, min(minutes, left))
