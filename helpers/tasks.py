import asyncio
import string
import random

# Global dictionaries to track tasks
ACTIVE_TASKS = {}        # task_id -> asyncio.Task
USER_BATCH_TASKS = {}    # user_id -> list of active task_ids for that user

def generate_task_id(length=6):
    chars = string.ascii_letters + string.digits
    while True:
        tid = ''.join(random.choice(chars) for _ in range(length))
        if tid not in ACTIVE_TASKS:
            return tid

def register_task(task_id: str, task: asyncio.Task, user_id: int):
    ACTIVE_TASKS[task_id] = task
    if user_id not in USER_BATCH_TASKS:
        USER_BATCH_TASKS[user_id] = []
    USER_BATCH_TASKS[user_id].append(task_id)

def unregister_task(task_id: str, user_id: int):
    if task_id in ACTIVE_TASKS:
        del ACTIVE_TASKS[task_id]
    if user_id in USER_BATCH_TASKS and task_id in USER_BATCH_TASKS[user_id]:
        USER_BATCH_TASKS[user_id].remove(task_id)

def cancel_task(task_id: str):
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id].cancel()
        return True
    return False

def cancel_user_batch(user_id: int):
    if user_id in USER_BATCH_TASKS:
        for tid in list(USER_BATCH_TASKS[user_id]):
            cancel_task(tid)
        USER_BATCH_TASKS[user_id] = []
