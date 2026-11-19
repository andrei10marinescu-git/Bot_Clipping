"""Background job queue with progress/logs (one lane for live clips, one for VOD processing)."""

import itertools
import logging
import queue
import threading
import time
import traceback
import uuid
from collections import OrderedDict

from .config import LOG_DIR

logger = logging.getLogger("clipbot")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _fh = logging.FileHandler(LOG_DIR / "clipbot.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_fh)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, kind, streamer, label, fn, args, kwargs, lane, key):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.streamer = streamer
        self.label = label
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.lane = lane
        self.key = key
        self.status = "queued"
        self.progress = 0.0
        self.message = "În așteptare…"
        self.logs: list[str] = []
        self.created = time.time()
        self.started = None
        self.finished = None
        self.error = None
        self.result = None
        self.cancel_requested = False

    def log(self, msg: str) -> None:
        self.logs.append(time.strftime("%H:%M:%S ") + msg)
        del self.logs[:-200]
        self.message = msg
        logger.info("[%s] %s", self.streamer, msg)

    def set_progress(self, p: float, msg: str | None = None) -> None:
        self.progress = max(0.0, min(1.0, p))
        if msg:
            self.log(msg)

    def check(self) -> None:
        if self.cancel_requested:
            raise Cancelled()

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "streamer": self.streamer, "label": self.label,
            "status": self.status, "progress": round(self.progress, 3), "message": self.message,
            "logs": self.logs[-30:], "created": self.created, "started": self.started,
            "finished": self.finished, "error": self.error, "result": self.result,
        }


class JobManager:
    def __init__(self):
        self._queues = {"live": queue.PriorityQueue(), "vod": queue.PriorityQueue(), "maint": queue.PriorityQueue(),
                        "edit": queue.PriorityQueue()}
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()
        self._counter = itertools.count()
        for lane in self._queues:
            threading.Thread(target=self._worker, args=(lane,), daemon=True, name=f"jobs-{lane}").start()

    def submit(self, kind, streamer, label, fn, *args, lane="vod", key=None, priority=10, **kwargs) -> Job:
        with self._lock:
            if key:
                for j in self._jobs.values():
                    if j.key == key and j.status in ("queued", "running"):
                        return j
            job = Job(kind, streamer, label, fn, args, kwargs, lane, key)
            self._jobs[job.id] = job
            done = [k for k, j in self._jobs.items() if j.status not in ("queued", "running")]
            for k in done[:-150]:
                self._jobs.pop(k, None)
        self._queues[lane].put((priority, next(self._counter), job.id))
        return job

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while True:
            _, _, jid = q.get()
            job = self._jobs.get(jid)
            if not job or job.status != "queued":
                continue
            job.status = "running"
            job.started = time.time()
            try:
                job.result = job.fn(job, *job.args, **job.kwargs)
                job.status = "done"
                job.progress = 1.0
                if job.message.endswith("…"):
                    job.log("Gata ✔")
            except Cancelled:
                job.status = "cancelled"
                job.log("Anulat")
            except Exception as e:  # noqa: BLE001 - surface every failure in the UI
                job.status = "error"
                job.error = str(e)[:800]
                job.log(f"Eroare: {job.error}")
                logger.error("job %s failed:\n%s", job.id, traceback.format_exc())
            finally:
                job.finished = time.time()

    def list(self, limit: int = 40) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        active = [j for j in jobs if j.status in ("queued", "running")]
        rest = [j for j in jobs if j.status not in ("queued", "running")]
        rest.sort(key=lambda j: j.finished or 0, reverse=True)
        return [j.to_dict() for j in active + rest[: max(0, limit - len(active))]]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.finished = time.time()
        return True

    def busy_keys(self) -> set:
        with self._lock:
            return {j.key for j in self._jobs.values() if j.key and j.status in ("queued", "running")}


manager = JobManager()
