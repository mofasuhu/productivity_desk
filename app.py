#!/usr/bin/env python3
"""Productivity Desk web app with per-task timers and append-only notes."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from tempfile import TemporaryDirectory
from typing import Any

from flask import Flask, jsonify, request, send_file


@dataclass
class TaskNote:
    id: int
    text: str
    created_at: str


@dataclass
class Task:
    id: int
    title: str
    priority: str
    done: bool
    created_at: str
    timer_total_seconds: int = 25 * 60
    timer_seconds_left: int = 25 * 60
    timer_running: bool = False
    timer_started_at: float | None = None
    completed_sessions: int = 0
    task_notes: list[TaskNote] = field(default_factory=list)


@dataclass
class NoteEntry:
    id: int
    text: str
    created_at: str


class DeskStore:
    PRIORITIES = ("Low", "Medium", "High", "Critical")

    def __init__(self, data_path: Path) -> None:
        self.data_path = data_path
        self.lock = Lock()
        self.tasks: list[Task] = []
        self.notes: list[NoteEntry] = []
        self.next_task_id = 1
        self.next_note_id = 1
        self.migrated_completed_sessions = 0
        self.load()

    def _now_text(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    @staticmethod
    def _validate_minutes(minutes: int) -> int:
        if minutes < 1 or minutes > 180:
            raise ValueError("Minutes must be between 1 and 180.")
        return minutes

    @staticmethod
    def _sync_task_timer_locked(task: Task, now: float) -> None:
        if not task.timer_running:
            return

        if task.timer_started_at is None:
            task.timer_started_at = now
            return

        elapsed = int(now - task.timer_started_at)
        if elapsed <= 0:
            return

        remaining = task.timer_seconds_left - elapsed
        if remaining <= 0:
            task.timer_seconds_left = 0
            task.timer_running = False
            task.timer_started_at = None
            task.completed_sessions += 1
            return

        task.timer_seconds_left = remaining
        task.timer_started_at = now

    def _sync_all_timers_locked(self, now: float) -> None:
        for task in self.tasks:
            self._sync_task_timer_locked(task, now)

    def _get_task_locked(self, task_id: int) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ValueError("Task not found.")

    def load(self) -> None:
        if not self.data_path.exists():
            return

        try:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        raw_tasks = payload.get("tasks", [])
        tasks: list[Task] = []
        max_task_id = 0
        for item in raw_tasks:
            if not isinstance(item, dict):
                continue
            task_notes: list[TaskNote] = []
            raw_task_notes = item.get("task_notes", [])
            if isinstance(raw_task_notes, list):
                for raw_note in raw_task_notes:
                    if not isinstance(raw_note, dict):
                        continue
                    note_text = str(raw_note.get("text", "")).strip()
                    if not note_text:
                        continue
                    task_notes.append(
                        TaskNote(
                            id=int(raw_note.get("id", 0)),
                            text=note_text,
                            created_at=str(raw_note.get("created_at", "")),
                        )
                    )
            task = Task(
                id=int(item.get("id", 0)),
                title=str(item.get("title", "Untitled")),
                priority=str(item.get("priority", "Medium")),
                done=bool(item.get("done", False)),
                created_at=str(item.get("created_at", "")),
                timer_total_seconds=int(item.get("timer_total_seconds", 25 * 60)),
                timer_seconds_left=int(item.get("timer_seconds_left", 25 * 60)),
                timer_running=bool(item.get("timer_running", False)),
                timer_started_at=item.get("timer_started_at"),
                completed_sessions=int(item.get("completed_sessions", 0)),
                task_notes=task_notes,
            )
            if task.priority not in self.PRIORITIES:
                task.priority = "Medium"
            if task.timer_total_seconds <= 0:
                task.timer_total_seconds = 25 * 60
            if task.timer_seconds_left < 0:
                task.timer_seconds_left = 0
            if task.timer_seconds_left > task.timer_total_seconds:
                task.timer_seconds_left = task.timer_total_seconds
            if task.timer_started_at is not None:
                try:
                    task.timer_started_at = float(task.timer_started_at)
                except (TypeError, ValueError):
                    task.timer_started_at = None
            tasks.append(task)
            max_task_id = max(max_task_id, task.id)

        raw_notes = payload.get("notes", [])
        notes: list[NoteEntry] = []
        max_note_id = 0
        if isinstance(raw_notes, list):
            for item in raw_notes:
                if not isinstance(item, dict):
                    continue
                note = NoteEntry(
                    id=int(item.get("id", 0)),
                    text=str(item.get("text", "")).strip(),
                    created_at=str(item.get("created_at", "")),
                )
                if not note.text:
                    continue
                notes.append(note)
                max_note_id = max(max_note_id, note.id)
        elif isinstance(raw_notes, str) and raw_notes.strip():
            notes.append(NoteEntry(id=1, text=raw_notes.strip(), created_at=self._now_text()))
            max_note_id = 1

        migrated = int(payload.get("migrated_completed_sessions", 0))

        with self.lock:
            self.tasks = tasks
            self.notes = notes
            self.next_task_id = max_task_id + 1 if max_task_id > 0 else 1
            self.next_note_id = max_note_id + 1 if max_note_id > 0 else 1
            self.migrated_completed_sessions = max(0, migrated)
            self._sync_all_timers_locked(time.time())

    def save(self) -> None:
        with self.lock:
            payload = {
                "tasks": [asdict(task) for task in self.tasks],
                "notes": [asdict(note) for note in self.notes],
                "migrated_completed_sessions": self.migrated_completed_sessions,
            }
        self.data_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._sync_all_timers_locked(time.time())
            total_completed = self.migrated_completed_sessions + sum(
                task.completed_sessions for task in self.tasks
            )
            return {
                "tasks": [asdict(task) for task in self.tasks],
                "notes": [asdict(note) for note in self.notes],
                "total_completed_sessions": total_completed,
                "server_time": int(time.time()),
            }

    def add_task(self, title: str, priority: str) -> None:
        title = title.strip()
        if not title:
            raise ValueError("Task title is required.")
        if priority not in self.PRIORITIES:
            raise ValueError("Priority is invalid.")

        with self.lock:
            self._sync_all_timers_locked(time.time())
            self.tasks.append(
                Task(
                    id=self.next_task_id,
                    title=title,
                    priority=priority,
                    done=False,
                    created_at=self._now_text(),
                )
            )
            self.next_task_id += 1
        self.save()

    def toggle_task(self, task_id: int) -> None:
        with self.lock:
            self._sync_all_timers_locked(time.time())
            task = self._get_task_locked(task_id)
            task.done = not task.done
            if task.done:
                task.timer_running = False
                task.timer_started_at = None
        self.save()

    def delete_task(self, task_id: int) -> None:
        with self.lock:
            self._sync_all_timers_locked(time.time())
            before = len(self.tasks)
            self.tasks = [task for task in self.tasks if task.id != task_id]
            if len(self.tasks) == before:
                raise ValueError("Task not found.")
        self.save()

    def clear_completed(self) -> None:
        with self.lock:
            self._sync_all_timers_locked(time.time())
            self.tasks = [task for task in self.tasks if not task.done]
        self.save()

    def start_timer(self, task_id: int, minutes: int | None = None) -> None:
        with self.lock:
            now = time.time()
            self._sync_all_timers_locked(now)
            task = self._get_task_locked(task_id)
            if task.done:
                raise ValueError("Re-open this task first to start timer.")

            if minutes is not None:
                valid = self._validate_minutes(minutes)
                requested_total = valid * 60
                if task.timer_seconds_left <= 0:
                    task.timer_total_seconds = requested_total
                    task.timer_seconds_left = requested_total
                elif not task.timer_running and task.timer_seconds_left == task.timer_total_seconds:
                    task.timer_total_seconds = requested_total
                    task.timer_seconds_left = requested_total

            if task.timer_seconds_left <= 0:
                task.timer_seconds_left = task.timer_total_seconds

            task.timer_running = True
            task.timer_started_at = now
        self.save()

    def pause_timer(self, task_id: int) -> None:
        with self.lock:
            now = time.time()
            self._sync_all_timers_locked(now)
            task = self._get_task_locked(task_id)
            if task.done:
                raise ValueError("Re-open this task first to start timer.")
            task.timer_running = False
            task.timer_started_at = None
        self.save()

    def reset_timer(self, task_id: int, minutes: int | None = None) -> None:
        with self.lock:
            self._sync_all_timers_locked(time.time())
            task = self._get_task_locked(task_id)
            if minutes is not None:
                task.timer_total_seconds = self._validate_minutes(minutes) * 60
            task.timer_seconds_left = task.timer_total_seconds
            task.timer_running = False
            task.timer_started_at = None
        self.save()

    def start_new_session(self, task_id: int, minutes: int | None = None) -> None:
        with self.lock:
            now = time.time()
            self._sync_all_timers_locked(now)
            task = self._get_task_locked(task_id)
            if task.done:
                raise ValueError("Re-open this task first to start timer.")
            if minutes is not None:
                task.timer_total_seconds = self._validate_minutes(minutes) * 60
            task.timer_seconds_left = task.timer_total_seconds
            task.timer_running = True
            task.timer_started_at = now
        self.save()

    def add_note(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            raise ValueError("Note text is required.")

        with self.lock:
            self.notes.append(
                NoteEntry(id=self.next_note_id, text=clean, created_at=self._now_text())
            )
            self.next_note_id += 1
        self.save()

    def delete_note(self, note_id: int) -> None:
        with self.lock:
            before = len(self.notes)
            self.notes = [note for note in self.notes if note.id != note_id]
            if len(self.notes) == before:
                raise ValueError("Note not found.")
        self.save()

    def clear_notes(self) -> None:
        with self.lock:
            self.notes = []
        self.save()

    def add_task_note(self, task_id: int, text: str) -> None:
        clean = text.strip()
        if not clean:
            raise ValueError("Task note text is required.")

        with self.lock:
            task = self._get_task_locked(task_id)
            next_note_id = 1 + max((note.id for note in task.task_notes), default=0)
            task.task_notes.append(
                TaskNote(id=next_note_id, text=clean, created_at=self._now_text())
            )
        self.save()

    def delete_task_note(self, task_id: int, note_id: int) -> None:
        with self.lock:
            task = self._get_task_locked(task_id)
            before = len(task.task_notes)
            task.task_notes = [note for note in task.task_notes if note.id != note_id]
            if len(task.task_notes) == before:
                raise ValueError("Task note not found.")
        self.save()

    def clear_task_notes(self, task_id: int) -> None:
        with self.lock:
            task = self._get_task_locked(task_id)
            task.task_notes = []
        self.save()


INDEX_HTML_PATH = Path(__file__).with_name("index.html")


def create_app(data_path: Path) -> Flask:
    app = Flask(__name__)
    store = DeskStore(data_path=data_path)

    @app.get("/")
    def index() -> Any:
        return send_file(INDEX_HTML_PATH)

    @app.get("/api/state")
    def state() -> Any:
        return jsonify(store.snapshot())

    @app.post("/api/tasks")
    def tasks() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", ""))
        try:
            if action == "add":
                store.add_task(
                    title=str(payload.get("title", "")),
                    priority=str(payload.get("priority", "Medium")),
                )
            elif action == "toggle":
                store.toggle_task(int(payload.get("id", 0)))
            elif action == "delete":
                store.delete_task(int(payload.get("id", 0)))
            elif action == "clear_completed":
                store.clear_completed()
            else:
                return jsonify({"error": "Invalid task action."}), 400
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(store.snapshot())

    @app.post("/api/timer")
    def timer() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", ""))
        minutes_raw = payload.get("minutes")
        minutes: int | None
        if minutes_raw in (None, ""):
            minutes = None
        else:
            minutes = int(minutes_raw)

        try:
            task_id = int(payload.get("id", 0))
            if action == "start":
                store.start_timer(task_id=task_id, minutes=minutes)
            elif action == "pause":
                store.pause_timer(task_id=task_id)
            elif action == "reset":
                store.reset_timer(task_id=task_id, minutes=minutes)
            elif action in ("new_session", "restart"):
                store.start_new_session(task_id=task_id, minutes=minutes)
            else:
                return jsonify({"error": "Invalid timer action."}), 400
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(store.snapshot())

    @app.post("/api/notes")
    def notes() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", ""))
        try:
            if action == "add":
                store.add_note(str(payload.get("text", "")))
            elif action == "delete":
                store.delete_note(int(payload.get("id", 0)))
            elif action == "clear":
                store.clear_notes()
            else:
                return jsonify({"error": "Invalid notes action."}), 400
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(store.snapshot())

    @app.post("/api/task-notes")
    def task_notes() -> Any:
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action", ""))
        try:
            task_id = int(payload.get("task_id", 0))
            if action == "add":
                store.add_task_note(task_id=task_id, text=str(payload.get("text", "")))
            elif action == "delete":
                store.delete_task_note(
                    task_id=task_id,
                    note_id=int(payload.get("note_id", 0)),
                )
            elif action == "clear":
                store.clear_task_notes(task_id=task_id)
            else:
                return jsonify({"error": "Invalid task-notes action."}), 400
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(store.snapshot())

    return app


def run_smoke_test(app: Flask) -> None:
    with app.test_client() as client:
        state = client.get("/api/state")
        assert state.status_code == 200

        add_a = client.post(
            "/api/tasks",
            json={"action": "add", "title": "Parallel Task A", "priority": "High"},
        )
        add_b = client.post(
            "/api/tasks",
            json={"action": "add", "title": "Parallel Task B", "priority": "Medium"},
        )
        assert add_a.status_code == 200
        assert add_b.status_code == 200

        current = add_b.get_json()
        assert current and len(current["tasks"]) >= 2
        task_a = current["tasks"][-2]["id"]
        task_b = current["tasks"][-1]["id"]

        start_a = client.post("/api/timer", json={"action": "start", "id": task_a})
        start_b = client.post("/api/timer", json={"action": "start", "id": task_b})
        assert start_a.status_code == 200
        assert start_b.status_code == 200

        running_state = start_b.get_json()
        running_count = sum(1 for task in running_state["tasks"] if task["timer_running"])
        assert running_count >= 2

        pause_a = client.post("/api/timer", json={"action": "pause", "id": task_a})
        assert pause_a.status_code == 200
        paused_payload = pause_a.get_json()
        paused_a = [task for task in paused_payload["tasks"] if task["id"] == task_a][0]
        assert paused_a["timer_running"] is False

        reset_a = client.post(
            "/api/timer",
            json={"action": "reset", "id": task_a, "minutes": 5},
        )
        assert reset_a.status_code == 200
        reset_payload = reset_a.get_json()
        reset_task = [task for task in reset_payload["tasks"] if task["id"] == task_a][0]
        assert reset_task["timer_total_seconds"] == 300
        assert reset_task["timer_seconds_left"] == 300

        restart_session = client.post(
            "/api/timer",
            json={"action": "restart", "id": task_a, "minutes": 2},
        )
        assert restart_session.status_code == 200
        session_payload = restart_session.get_json()
        session_task = [task for task in session_payload["tasks"] if task["id"] == task_a][0]
        assert session_task["timer_running"] is True
        assert session_task["timer_total_seconds"] == 120

        task_note_add = client.post(
            "/api/task-notes",
            json={"action": "add", "task_id": task_a, "text": "Task A note from smoke test"},
        )
        assert task_note_add.status_code == 200
        task_note_payload = task_note_add.get_json()
        task_a_after_note = [task for task in task_note_payload["tasks"] if task["id"] == task_a][0]
        task_b_after_note = [task for task in task_note_payload["tasks"] if task["id"] == task_b][0]
        assert len(task_a_after_note["task_notes"]) == 1
        assert task_a_after_note["task_notes"][0]["text"] == "Task A note from smoke test"
        assert len(task_b_after_note["task_notes"]) == 0

        note_a = client.post(
            "/api/notes",
            json={"action": "add", "text": "First note from smoke test"},
        )
        note_b = client.post(
            "/api/notes",
            json={"action": "add", "text": "Second note from smoke test"},
        )
        assert note_a.status_code == 200
        assert note_b.status_code == 200
        notes_payload = note_b.get_json()
        note_texts = [note["text"] for note in notes_payload["notes"]]
        assert "First note from smoke test" in note_texts
        assert "Second note from smoke test" in note_texts

        task_note_delete = client.post(
            "/api/task-notes",
            json={"action": "delete", "task_id": task_a, "note_id": 1},
        )
        assert task_note_delete.status_code == 200
        task_note_deleted_payload = task_note_delete.get_json()
        task_a_after_delete = [task for task in task_note_deleted_payload["tasks"] if task["id"] == task_a][0]
        assert len(task_a_after_delete["task_notes"]) == 0

        latest_note_id = notes_payload["notes"][-1]["id"]
        delete_note = client.post(
            "/api/notes",
            json={"action": "delete", "id": latest_note_id},
        )
        assert delete_note.status_code == 200

        toggle_b = client.post("/api/tasks", json={"action": "toggle", "id": task_b})
        assert toggle_b.status_code == 200
        toggled_payload = toggle_b.get_json()
        toggled_task = [task for task in toggled_payload["tasks"] if task["id"] == task_b][0]
        assert toggled_task["done"] is True
        assert toggled_task["timer_running"] is False

        start_done = client.post("/api/timer", json={"action": "start", "id": task_b})
        assert start_done.status_code == 400
        start_done_payload = start_done.get_json()
        assert "Re-open" in start_done_payload["error"]

    print("Smoke test passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Productivity Desk Flask app.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", default=8000, type=int, help="Port to bind.")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Run API smoke test and exit.",
    )
    args = parser.parse_args()

    if args.smoke_test:
        with TemporaryDirectory() as tmp_dir:
            temp_data_path = Path(tmp_dir) / "smoke_data.json"
            smoke_app = create_app(data_path=temp_data_path)
            run_smoke_test(smoke_app)
        return

    data_path = Path(__file__).with_name("productivity_data.json")
    app = create_app(data_path=data_path)

    print(f"Serving Productivity Desk at http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
