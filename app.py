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

from flask import Flask, jsonify, render_template_string, request


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


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Productivity Desk</title>
  <style>
    :root {
      --bg: #f6f8fc;
      --card: #ffffff;
      --ink: #1f2937;
      --muted: #6b7280;
      --line: #d1d5db;
      --accent: #0f766e;
      --accent-2: #0ea5e9;
      --warn: #b91c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at top left, #dbeafe, var(--bg) 58%);
    }
    .page {
      max-width: 1360px;
      margin: 0 auto;
      padding: 24px;
    }
    h1 {
      margin: 0 0 14px;
      font-size: 2rem;
    }
    .grid {
      display: grid;
      grid-template-columns: 2.45fr 1fr;
      gap: 16px;
    }
    @media (max-width: 1020px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 16px;
      box-shadow: 0 6px 14px rgba(15, 23, 42, 0.08);
      margin-bottom: 16px;
    }
    .card h2 {
      margin: 0 0 12px;
      font-size: 1.12rem;
    }
    .row {
      display: flex;
      gap: 8px;
      margin-bottom: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    input, select, textarea, button {
      border: 1px solid var(--line);
      border-radius: 10px;
      font: inherit;
    }
    input, select, textarea {
      padding: 9px 10px;
      background: #fff;
      color: var(--ink);
    }
    button {
      cursor: pointer;
      padding: 9px 12px;
      background: #fff;
    }
    .btn-primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    .btn-primary:hover { background: #115e59; }
    .btn-danger {
      border-color: #fecaca;
      color: var(--warn);
      background: #fff5f5;
    }
    .btn-action {
      border: 0;
      color: #fff;
      font-weight: 400;
      font-size: 0.83rem;
      padding: 6px 8px;
    }
    .btn-action-done { background: #188f45; }
    .btn-action-done:hover { background: #14532d; }
    .btn-action-reopen { background: #366be0; }
    .btn-action-reopen:hover { background: #1d4ed8; }
    .btn-action-start { background: #0f766e; }
    .btn-action-start:hover { background: #115e59; }
    .btn-action-pause { background: #d97706; }
    .btn-action-pause:hover { background: #b45309; }
    .btn-action-restart { background: #0284c7; }
    .btn-action-restart:hover { background: #0369a1; }
    .btn-action-delete { background: #cf3232; }
    .btn-action-delete:hover { background: #991b1b; }
    .btn-action-reset { background: #4b5563; }
    .btn-action-reset:hover { background: #374151; }
    .task-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
    }
    .task-table-wrap.scrollable {
      max-height: 820px;
      overflow-y: auto;
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      margin-top: 10px;
    }
    .task-table-wrap.scrollable .task-table {
      margin-top: 0;
    }
    .task-table th, .task-table td {
      border-bottom: 1px solid #e5e7eb;
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }
    .task-table th {
      color: var(--muted);
      font-weight: 600;
      font-size: 0.85rem;
    }
    .actions-cell { white-space: nowrap; }
    .task-row { cursor: pointer; }
    .task-open-row { background: #f7fcf9; }
    .task-done-row { background: #f5f6f8; }
    .task-selected { box-shadow: inset 0 0 0 2px #93c5fd; }
    .task-done { text-decoration: line-through; color: var(--muted); }
    .priority-text { font-weight: 700; }
    .priority-low { color: #64748b; }
    .priority-medium { color: #0f766e; }
    .priority-high { color: #b45309; }
    .priority-critical { color: #b91c1c; }
    .mini { color: var(--muted); font-size: 0.87rem; }
    .mini-timer {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.95rem;
      font-weight: 600;
    }
    .mini-timer.running { color: #0f766e; }
    textarea {
      width: 100%;
      min-height: 96px;
      resize: vertical;
      padding: 10px;
    }
    .notes-list {
      max-height: 260px;
      overflow-y: auto;
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      padding: 8px;
      background: #f8fafc;
    }
    .task-notes-list {
      max-height: 180px;
      overflow-y: auto;
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      padding: 8px;
      background: #f8fafc;
      margin-top: 8px;
    }
    .note-item {
      border: 1px solid #e5e7eb;
      border-radius: 10px;
      background: #fff;
      padding: 8px;
      margin-bottom: 8px;
    }
    .note-item:last-child { margin-bottom: 0; }
    .note-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
    }
    .note-body {
      white-space: pre-wrap;
      word-break: break-word;
    }
    .timer {
      font-size: 2rem;
      font-weight: 700;
      margin: 8px 0 6px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .bar {
      width: 100%;
      height: 12px;
      border-radius: 999px;
      background: #e5e7eb;
      overflow: hidden;
      margin-bottom: 10px;
    }
    .bar > div {
      height: 100%;
      width: 0;
      background: linear-gradient(90deg, #14b8a6, var(--accent-2));
      transition: width 0.2s ease;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(120px, 1fr));
      gap: 8px;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      background: #f9fafb;
    }
    .pill b {
      display: block;
      font-size: 1.2rem;
      margin-top: 3px;
    }
    #status {
      margin-top: 6px;
      color: var(--muted);
      min-height: 1.2em;
    }
  </style>
</head>
<body>
  <div class="page">
    <h1>Productivity Desk</h1>
    <div class="grid">
      <div class="card">
        <h2>Task Board</h2>
        <div class="row">
          <input id="taskTitle" type="text" placeholder="Task title..." style="flex: 1; min-width: 240px;">
          <select id="taskPriority">
            <option>Low</option>
            <option selected>Medium</option>
            <option>High</option>
            <option>Critical</option>
          </select>
          <button id="addBtn" class="btn-primary">Add Task</button>
        </div>
        <div class="row">
          <span class="mini">Filter:</span>
          <select id="taskFilter">
            <option>All</option>
            <option>Open</option>
            <option>Done</option>
          </select>
          <button id="clearDoneBtn">Clear Completed</button>
        </div>
        <div id="taskTableWrap" class="task-table-wrap">
          <table class="task-table">
            <thead>
              <tr>
                <th style="width: 30%;">Task</th>
                <th style="width: 10%;">State</th>
                <th style="width: 10%;">Priority</th>
                <th style="width: 12%;">Timer</th>
                <th style="width: 13%;">Created</th>
                <th style="width: 25%;">Actions</th>
              </tr>
            </thead>
            <tbody id="taskBody"></tbody>
          </table>
        </div>
      </div>

      <div>
        <div class="card">
          <h2>Task Details</h2>
          <div id="taskDetailsEmpty" class="mini">Select a task from the list to see details and timer controls.</div>
          <div id="taskDetailsPanel" style="display:none;">
            <div id="detailTitle" style="font-size:1.1rem;font-weight:700;"></div>
            <div id="detailMeta" class="mini" style="margin-bottom:8px;"></div>
            <div id="detailTimer" class="timer">25:00</div>
            <div class="bar"><div id="detailTimerFill"></div></div>
            <div class="row">
              <span>Minutes:</span>
              <input id="detailMinutesInput" type="number" min="1" max="180" value="25" style="width:90px;">
            </div>
            <div class="row">
              <button id="detailStartBtn" class="btn-action btn-action-start">Start</button>
              <button id="detailPauseBtn" class="btn-action btn-action-pause">Pause</button>
              <button id="detailResetBtn" class="btn-action btn-action-reset">Reset</button>
              <button id="detailNewSessionBtn" class="btn-action btn-action-restart">Start New Session</button>
            </div>
            <div id="detailDoneHint" class="mini" style="display:none;">Task is done. Re-open first to start timer.</div>
            <div id="detailSessions" class="mini"></div>
            <div class="mini" style="margin-top:6px;">
              A session completes when this timer reaches 00:00. Use <b>Start New Session</b> to move to the next one.
            </div>
            <div class="mini" style="margin-top:10px;">Task Notes</div>
            <textarea id="taskNoteInput" placeholder="Write note for this task..."></textarea>
            <div class="row" style="margin-top:8px; justify-content:flex-end;">
              <button id="addTaskNoteBtn" class="btn-primary">Add Task Note</button>
              <button id="clearTaskNotesBtn" class="btn-danger">Clear Task Notes</button>
            </div>
            <div id="taskNotesList" class="task-notes-list"></div>
          </div>
        </div>

        <div class="card">
          <h2>Notes Feed</h2>
          <div class="mini" style="margin-bottom:8px;">New notes are appended. Older notes stay in the scrollable list.</div>
          <textarea id="noteInput" placeholder="Write a new note..."></textarea>
          <div class="row" style="margin-top:8px; justify-content:flex-end;">
            <button id="addNoteBtn" class="btn-primary">Add Note</button>
            <button id="clearNotesBtn" class="btn-danger">Clear Notes</button>
          </div>
          <div id="notesList" class="notes-list"></div>
        </div>

        <div class="card">
          <h2>Summary</h2>
          <div class="stats">
            <div class="pill">Total Tasks <b id="sTotal">0</b></div>
            <div class="pill">Open Tasks <b id="sOpen">0</b></div>
            <div class="pill">Done Tasks <b id="sDone">0</b></div>
            <div class="pill">Running Timers <b id="sRunning">0</b></div>
            <div class="pill" style="grid-column:1 / -1;">Completed Sessions <b id="sSessions">0</b></div>
          </div>
        </div>
      </div>
    </div>
    <div id="status"></div>
  </div>

  <script>
    let state = { tasks: [], notes: [], total_completed_sessions: 0, server_time: 0 };
    let filterMode = "All";
    let selectedTaskId = null;

    const statusEl = document.getElementById("status");
    const taskBody = document.getElementById("taskBody");
    const taskTableWrap = document.getElementById("taskTableWrap");
    const taskFilter = document.getElementById("taskFilter");
    const taskTitleInput = document.getElementById("taskTitle");
    const taskPriority = document.getElementById("taskPriority");

    const detailEmpty = document.getElementById("taskDetailsEmpty");
    const detailPanel = document.getElementById("taskDetailsPanel");
    const detailTitle = document.getElementById("detailTitle");
    const detailMeta = document.getElementById("detailMeta");
    const detailTimer = document.getElementById("detailTimer");
    const detailTimerFill = document.getElementById("detailTimerFill");
    const detailMinutesInput = document.getElementById("detailMinutesInput");
    const detailSessions = document.getElementById("detailSessions");
    const detailDoneHint = document.getElementById("detailDoneHint");
    const detailStartBtn = document.getElementById("detailStartBtn");
    const detailPauseBtn = document.getElementById("detailPauseBtn");
    const detailResetBtn = document.getElementById("detailResetBtn");
    const detailNewSessionBtn = document.getElementById("detailNewSessionBtn");
    const taskNoteInput = document.getElementById("taskNoteInput");
    const taskNotesList = document.getElementById("taskNotesList");

    const noteInput = document.getElementById("noteInput");
    const notesList = document.getElementById("notesList");

    function setStatus(message) {
      statusEl.textContent = message || "";
    }

    function apiPost(path, payload) {
      return fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      }).then(async (res) => {
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "Request failed.");
        }
        return data;
      });
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function getTask(taskId) {
      return state.tasks.find((task) => task.id === taskId) || null;
    }

    function taskRemainingSeconds(task) {
      let remaining = Number(task.timer_seconds_left || 0);
      if (task.timer_running && task.timer_started_at !== null && task.timer_started_at !== undefined) {
        const elapsed = Math.floor((Date.now() / 1000) - Number(task.timer_started_at));
        if (elapsed > 0) {
          remaining -= elapsed;
        }
      }
      return Math.max(0, remaining);
    }

    function formatDuration(totalSeconds) {
      let seconds = Math.max(0, Math.floor(totalSeconds));
      const hours = Math.floor(seconds / 3600);
      seconds %= 3600;
      const minutes = Math.floor(seconds / 60);
      const secs = seconds % 60;

      if (hours > 0) {
        return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
      }
      return `${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
    }

    function timerStateLabel(task) {
      const remaining = taskRemainingSeconds(task);
      if (task.timer_running && remaining > 0) {
        return "Running";
      }
      if (remaining === 0) {
        return "Complete";
      }
      return "Paused";
    }

    function applyState(nextState) {
      state = nextState;

      if (selectedTaskId !== null && !state.tasks.some((task) => task.id === selectedTaskId)) {
        selectedTaskId = null;
      }
      if (selectedTaskId === null && state.tasks.length > 0) {
        selectedTaskId = state.tasks[0].id;
      }

      renderAll(true);
    }

    function loadState() {
      return fetch("/api/state")
        .then((res) => res.json())
        .then((data) => {
          applyState(data);
          setStatus("Loaded.");
        })
        .catch((err) => setStatus(err.message));
    }

    function filteredTasks() {
      if (filterMode === "Open") {
        return state.tasks.filter((task) => !task.done);
      }
      if (filterMode === "Done") {
        return state.tasks.filter((task) => task.done);
      }
      return state.tasks;
    }

    function renderTasks() {
      const tasks = filteredTasks();
      taskTableWrap.classList.toggle("scrollable", tasks.length > 20);

      const rows = tasks.map((task) => {
        const titleClass = task.done ? "task-done" : "";
        const selectedClass = selectedTaskId === task.id ? "task-selected" : "";
        const rowStateClass = task.done ? "task-done-row" : "task-open-row";
        const stateText = task.done ? "Done" : "Open";
        const toggleLabel = task.done ? "Re-open" : "Done";
        const toggleActionClass = task.done ? "btn-action-reopen" : "btn-action-done";
        const remaining = taskRemainingSeconds(task);
        const timerClass = task.timer_running ? "mini-timer running" : "mini-timer";
        const priorityClass = {
          "Low": "priority-low",
          "Medium": "priority-medium",
          "High": "priority-high",
          "Critical": "priority-critical"
        }[task.priority] || "priority-medium";
        let timerActionButton = "";

        if (!task.done) {
          let timerAction = "start";
          let timerActionLabel = "Start";
          let timerActionClass = "btn-action-start";

          if (task.timer_running && remaining > 0) {
            timerAction = "pause";
            timerActionLabel = "Pause";
            timerActionClass = "btn-action-pause";
          } else if (remaining === 0) {
            timerAction = "restart";
            timerActionLabel = "Restart";
            timerActionClass = "btn-action-restart";
          }

          timerActionButton = `<button class="btn-action ${timerActionClass}" onclick="event.stopPropagation();quickTimer(${task.id}, '${timerAction}');">${timerActionLabel}</button>`;
        }

        return `
          <tr class="task-row ${rowStateClass} ${selectedClass}" onclick="selectTask(${task.id})">
            <td class="${titleClass}">${escapeHtml(task.title)}</td>
            <td>${stateText}</td>
            <td><span class="priority-text ${priorityClass}">${escapeHtml(task.priority)}</span></td>
            <td>
              <div class="${timerClass}">${formatDuration(remaining)}</div>
              <div class="mini">${timerStateLabel(task)}</div>
            </td>
            <td>${escapeHtml(task.created_at)}</td>
            <td class="actions-cell">
              <button class="btn-action ${toggleActionClass}" onclick="event.stopPropagation();toggleTask(${task.id});">${toggleLabel}</button>
              ${timerActionButton}
              <button class="btn-action btn-action-delete" onclick="event.stopPropagation();deleteTask(${task.id});">Delete</button>
            </td>
          </tr>
        `;
      }).join("");

      taskBody.innerHTML = rows || '<tr><td colspan="6" class="mini">No tasks yet.</td></tr>';
    }

    function renderTaskDetails(syncMinutesInput) {
      const task = getTask(selectedTaskId);
      if (!task) {
        detailEmpty.style.display = "block";
        detailPanel.style.display = "none";
        taskNoteInput.value = "";
        taskNotesList.innerHTML = '<div class="mini">Select a task to view notes.</div>';
        return;
      }

      detailEmpty.style.display = "none";
      detailPanel.style.display = "block";

      const remaining = taskRemainingSeconds(task);
      const progress = task.timer_total_seconds > 0
        ? ((task.timer_total_seconds - remaining) / task.timer_total_seconds) * 100
        : 0;

      detailTitle.textContent = task.title;
      detailMeta.textContent = `Priority: ${task.priority} | State: ${task.done ? "Done" : "Open"} | Created: ${task.created_at}`;
      detailTimer.textContent = formatDuration(remaining);
      detailTimerFill.style.width = `${Math.max(0, Math.min(100, progress))}%`;
      detailSessions.textContent = `Completed sessions for this task: ${task.completed_sessions}`;
      detailDoneHint.style.display = task.done ? "block" : "none";

      const isComplete = remaining === 0;
      const showRunControls = !task.done;
      detailStartBtn.style.display = showRunControls && !task.timer_running && !isComplete ? "inline-block" : "none";
      detailPauseBtn.style.display = showRunControls && (task.timer_running || isComplete) ? "inline-block" : "none";
      detailNewSessionBtn.style.display = showRunControls ? "inline-block" : "none";
      detailResetBtn.style.display = task.done ? "none" : "inline-block";

      if (showRunControls && task.timer_running && !isComplete) {
        detailPauseBtn.textContent = "Pause";
        detailPauseBtn.className = "btn-action btn-action-pause";
        detailPauseBtn.dataset.mode = "pause";
      } else if (showRunControls && isComplete) {
        detailPauseBtn.textContent = "Restart";
        detailPauseBtn.className = "btn-action btn-action-restart";
        detailPauseBtn.dataset.mode = "restart";
      } else if (showRunControls) {
        detailPauseBtn.textContent = "Pause";
        detailPauseBtn.className = "btn-action btn-action-pause";
        detailPauseBtn.dataset.mode = "pause";
      }

      if (syncMinutesInput) {
        detailMinutesInput.value = String(Math.max(1, Math.floor(task.timer_total_seconds / 60)));
      }
      renderTaskNotes(task);
    }

    function renderTaskNotes(task) {
      const notes = Array.isArray(task.task_notes) ? [...task.task_notes] : [];
      const orderedNotes = notes.sort((a, b) => Number(b.id) - Number(a.id));
      if (orderedNotes.length === 0) {
        taskNotesList.innerHTML = '<div class="mini">No task notes yet.</div>';
        return;
      }

      taskNotesList.innerHTML = orderedNotes.map((note) => {
        return `
          <div class="note-item">
            <div class="note-head">
              <span class="mini">${escapeHtml(note.created_at)}</span>
              <button onclick="deleteTaskNote(${task.id}, ${note.id})">Delete</button>
            </div>
            <div class="note-body">${escapeHtml(note.text)}</div>
          </div>
        `;
      }).join("");
    }

    function renderNotes() {
      const orderedNotes = [...state.notes].sort((a, b) => b.id - a.id);
      if (orderedNotes.length === 0) {
        notesList.innerHTML = '<div class="mini">No notes yet.</div>';
        return;
      }

      notesList.innerHTML = orderedNotes.map((note) => {
        return `
          <div class="note-item">
            <div class="note-head">
              <span class="mini">${escapeHtml(note.created_at)}</span>
              <button onclick="deleteNote(${note.id})">Delete</button>
            </div>
            <div class="note-body">${escapeHtml(note.text)}</div>
          </div>
        `;
      }).join("");
    }

    function renderSummary() {
      const total = state.tasks.length;
      const done = state.tasks.filter((task) => task.done).length;
      const open = total - done;
      const runningTimers = state.tasks.filter((task) => task.timer_running && taskRemainingSeconds(task) > 0).length;

      document.getElementById("sTotal").textContent = String(total);
      document.getElementById("sOpen").textContent = String(open);
      document.getElementById("sDone").textContent = String(done);
      document.getElementById("sRunning").textContent = String(runningTimers);
      document.getElementById("sSessions").textContent = String(state.total_completed_sessions || 0);
    }

    function renderAll(syncMinutesInput) {
      renderTasks();
      renderTaskDetails(syncMinutesInput);
      renderNotes();
      renderSummary();
    }

    function parseDetailMinutes() {
      const value = Number(detailMinutesInput.value);
      if (!Number.isInteger(value) || value < 1 || value > 180) {
        setStatus("Minutes must be an integer between 1 and 180.");
        return null;
      }
      return value;
    }

    function selectTask(taskId) {
      selectedTaskId = taskId;
      taskNoteInput.value = "";
      renderTasks();
      renderTaskDetails(true);
      const selected = getTask(taskId);
      if (selected) {
        setStatus(`Selected task: ${selected.title}`);
      }
    }

    function addTask() {
      const title = taskTitleInput.value.trim();
      const priority = taskPriority.value;
      if (!title) {
        setStatus("Task title is required.");
        return;
      }
      apiPost("/api/tasks", { action: "add", title, priority })
        .then((data) => {
          applyState(data);
          const latest = data.tasks[data.tasks.length - 1];
          selectedTaskId = latest ? latest.id : selectedTaskId;
          renderTasks();
          renderTaskDetails(true);
          taskTitleInput.value = "";
          setStatus("Task added.");
        })
        .catch((err) => setStatus(err.message));
    }

    function toggleTask(taskId) {
      apiPost("/api/tasks", { action: "toggle", id: taskId })
        .then((data) => {
          applyState(data);
          const changed = getTask(taskId);
          if (changed && changed.done) {
            setStatus(`Task marked done. Re-open to start timer again.`);
          } else {
            setStatus("Task re-opened.");
          }
        })
        .catch((err) => setStatus(err.message));
    }

    function deleteTask(taskId) {
      apiPost("/api/tasks", { action: "delete", id: taskId })
        .then((data) => {
          applyState(data);
          setStatus("Task deleted.");
        })
        .catch((err) => setStatus(err.message));
    }

    function clearCompleted() {
      apiPost("/api/tasks", { action: "clear_completed" })
        .then((data) => {
          applyState(data);
          setStatus("Completed tasks removed.");
        })
        .catch((err) => setStatus(err.message));
    }

    function quickTimer(taskId, action) {
      const task = getTask(taskId);
      if (!task) {
        setStatus("Task not found.");
        return;
      }
      if (task.done) {
        setStatus("Re-open this task first to start timer.");
        return;
      }
      apiPost("/api/timer", { action, id: taskId })
        .then((data) => {
          applyState(data);
          if (action === "pause") {
            setStatus("Timer paused.");
          } else if (action === "restart") {
            setStatus("Timer restarted.");
          } else {
            setStatus("Timer started.");
          }
        })
        .catch((err) => setStatus(err.message));
    }

    function startSelectedTimer() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      const task = getTask(selectedTaskId);
      if (!task || task.done) {
        setStatus("Re-open this task first to start timer.");
        return;
      }
      const minutes = parseDetailMinutes();
      if (minutes === null) {
        return;
      }
      apiPost("/api/timer", { action: "start", id: selectedTaskId, minutes })
        .then((data) => {
          applyState(data);
          setStatus("Task timer started.");
        })
        .catch((err) => setStatus(err.message));
    }

    function pauseSelectedTimer() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      const task = getTask(selectedTaskId);
      if (!task || task.done) {
        setStatus("Re-open this task first to start timer.");
        return;
      }
      const action = detailPauseBtn.dataset.mode === "restart" ? "restart" : "pause";
      apiPost("/api/timer", { action, id: selectedTaskId })
        .then((data) => {
          applyState(data);
          if (action === "restart") {
            setStatus("Task timer restarted.");
          } else {
            setStatus("Task timer paused.");
          }
        })
        .catch((err) => setStatus(err.message));
    }

    function resetSelectedTimer() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      const task = getTask(selectedTaskId);
      if (!task || task.done) {
        setStatus("Re-open this task first to start timer.");
        return;
      }
      const minutes = parseDetailMinutes();
      if (minutes === null) {
        return;
      }
      apiPost("/api/timer", { action: "reset", id: selectedTaskId, minutes })
        .then((data) => {
          applyState(data);
          setStatus("Task timer reset.");
        })
        .catch((err) => setStatus(err.message));
    }

    function startNewSession() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      const task = getTask(selectedTaskId);
      if (!task || task.done) {
        setStatus("Re-open this task first to start timer.");
        return;
      }
      const minutes = parseDetailMinutes();
      if (minutes === null) {
        return;
      }
      apiPost("/api/timer", { action: "new_session", id: selectedTaskId, minutes })
        .then((data) => {
          applyState(data);
          setStatus("New session started for selected task.");
        })
        .catch((err) => setStatus(err.message));
    }

    function addTaskNote() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      const text = taskNoteInput.value.trim();
      if (!text) {
        setStatus("Task note text is required.");
        return;
      }
      apiPost("/api/task-notes", { action: "add", task_id: selectedTaskId, text })
        .then((data) => {
          applyState(data);
          taskNoteInput.value = "";
          setStatus("Task note added.");
        })
        .catch((err) => setStatus(err.message));
    }

    function deleteTaskNote(taskId, noteId) {
      apiPost("/api/task-notes", { action: "delete", task_id: taskId, note_id: noteId })
        .then((data) => {
          applyState(data);
          setStatus("Task note deleted.");
        })
        .catch((err) => setStatus(err.message));
    }

    function clearTaskNotes() {
      if (selectedTaskId === null) {
        setStatus("Select a task first.");
        return;
      }
      apiPost("/api/task-notes", { action: "clear", task_id: selectedTaskId })
        .then((data) => {
          applyState(data);
          setStatus("Task notes cleared.");
        })
        .catch((err) => setStatus(err.message));
    }

    function addNote() {
      const text = noteInput.value.trim();
      if (!text) {
        setStatus("Note text is required.");
        return;
      }
      apiPost("/api/notes", { action: "add", text })
        .then((data) => {
          applyState(data);
          noteInput.value = "";
          setStatus("Note added.");
        })
        .catch((err) => setStatus(err.message));
    }

    function deleteNote(noteId) {
      apiPost("/api/notes", { action: "delete", id: noteId })
        .then((data) => {
          applyState(data);
          setStatus("Note deleted.");
        })
        .catch((err) => setStatus(err.message));
    }

    function clearNotes() {
      apiPost("/api/notes", { action: "clear" })
        .then((data) => {
          applyState(data);
          setStatus("Notes cleared.");
        })
        .catch((err) => setStatus(err.message));
    }

    document.getElementById("addBtn").addEventListener("click", addTask);
    taskTitleInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        addTask();
      }
    });
    taskFilter.addEventListener("change", (event) => {
      filterMode = event.target.value;
      renderTasks();
    });
    document.getElementById("clearDoneBtn").addEventListener("click", clearCompleted);

    detailStartBtn.addEventListener("click", startSelectedTimer);
    detailPauseBtn.addEventListener("click", pauseSelectedTimer);
    detailResetBtn.addEventListener("click", resetSelectedTimer);
    detailNewSessionBtn.addEventListener("click", startNewSession);
    document.getElementById("addTaskNoteBtn").addEventListener("click", addTaskNote);
    document.getElementById("clearTaskNotesBtn").addEventListener("click", clearTaskNotes);

    document.getElementById("addNoteBtn").addEventListener("click", addNote);
    document.getElementById("clearNotesBtn").addEventListener("click", clearNotes);

    window.selectTask = selectTask;
    window.toggleTask = toggleTask;
    window.deleteTask = deleteTask;
    window.quickTimer = quickTimer;
    window.deleteTaskNote = deleteTaskNote;
    window.deleteNote = deleteNote;

    loadState();
    setInterval(() => {
      renderTasks();
      renderTaskDetails(false);
      renderSummary();
    }, 1000);
  </script>
</body>
</html>
"""


def create_app(data_path: Path) -> Flask:
    app = Flask(__name__)
    store = DeskStore(data_path=data_path)

    @app.get("/")
    def index() -> str:
        return render_template_string(INDEX_HTML)

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
