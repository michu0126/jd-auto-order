import json
import sqlite3
from datetime import datetime

from core.config import CST
from core.errors import StopOrder


class StateStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS submissions (
                task_key TEXT PRIMARY KEY, task_id TEXT, status TEXT, updated TEXT,
                price TEXT, order_id TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, task_id TEXT, time TEXT, state TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS states (
                task_id TEXT PRIMARY KEY, time TEXT, state TEXT, detail TEXT);
        ''')
        self.db.commit()

    def event(self, taskid, state, detail="", update_state=True):
        detail = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        stamp = datetime.now(CST).isoformat(timespec="milliseconds")
        with self.db:
            self.db.execute("INSERT INTO events(task_id,time,state,detail) VALUES(?,?,?,?)", (taskid, stamp, state, detail))
            if update_state:
                self.db.execute("INSERT OR REPLACE INTO states VALUES(?,?,?,?)", (taskid, stamp, state, detail))

    def get(self, key):
        return self.db.execute("SELECT status, order_id, detail FROM submissions WHERE task_key=?", (key,)).fetchone()

    def terminal_for_task(self, taskid):
        return self.db.execute("SELECT status FROM submissions WHERE task_id=? AND status IN ('confirmed','submitting','unknown') LIMIT 1", (taskid,)).fetchone()

    def claim(self, key, taskid, price):
        try:
            with self.db:
                self.db.execute("INSERT INTO submissions VALUES(?,?,?,?,?,?,?)", (key, taskid, "submitting",
                    datetime.now(CST).isoformat(), str(price), "", "提交意图已落盘"))
        except sqlite3.IntegrityError as exc:
            raise StopOrder("同一任务已有提交记录，请先核实京东订单中心") from exc

    def finish(self, key, status, orderid="", detail=""):
        with self.db:
            self.db.execute("UPDATE submissions SET status=?, updated=?, order_id=?, detail=? WHERE task_key=?",
                (status, datetime.now(CST).isoformat(), orderid, detail, key))

    def snapshot(self):
        states = [dict(zip(("task_id", "time", "state", "detail"), row)) for row in self.db.execute("SELECT * FROM states")]
        orders = [dict(zip(("task_key", "task_id", "status", "updated", "price", "order_id", "detail"), row))
                  for row in self.db.execute("SELECT * FROM submissions ORDER BY updated DESC LIMIT 50")]
        return {"tasks": states, "orders": orders}

    def close(self):
        self.db.close()
