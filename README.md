# ⚡ Productivity Desk

A beautiful, standalone productivity app with Pomodoro focus timers, task management, per-task notes, and a quick notes feed. Runs entirely in your browser — no server required.

**[🚀 Live Demo](https://mofasuhu.github.io/productivity_desk/)**

---

## ✨ Features

- **Task Board** — Create, prioritize (Low / Medium / High / Critical), complete, and filter tasks
- **Focus Timer** — Per-task circular Pomodoro timer with start, pause, reset, and new session controls
- **Per-Task Notes** — Each task has its own notes feed for context and progress tracking
- **Quick Notes** — Global scratchpad for quick thoughts
- **Dashboard** — Live stats: total tasks, open, done, running timers, completed sessions
- **Sound Alerts** — Bell notification when a focus session completes
- **Export / Import** — JSON data portability (compatible with the Flask version)
- **Offline & Private** — All data stored in `localStorage`, nothing leaves your browser

## 🎨 Design

- Dark glassmorphism UI with animated gradient background
- Inter + JetBrains Mono typography
- Animated circular progress timer
- Toast notifications & modal dialogs
- Smooth micro-animations and responsive layout

## 🚀 Deploy

This is a single `index.html` file. Deploy it anywhere that serves static files:

- **GitHub Pages** — Push to repo, enable Pages in Settings
- **Netlify / Vercel** — Drag and drop the file
- **Local** — Just open `index.html` in your browser

## 📦 Migrating from Flask Version

If you were using the Flask (`app.py`) version:

1. Locate your `productivity_data.json` file
2. Open the standalone app in your browser
3. Click **📤 Import** in the header and select the JSON file
4. All tasks, notes, timers, and sessions will be imported

## 🛠️ Development

The Flask server version is included for reference:

```bash
pip install -r requirements.txt
python app.py
```

## 📄 License

MIT
