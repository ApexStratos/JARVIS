# Running JARVIS Nexus automatically at login

The simplest option on any OS: just run `./run.sh` (macOS/Linux) or `run.bat`
(Windows) whenever you want JARVIS up — no background service needed. The
options below are for leaving it running 24/7 without manually starting it.

## macOS

Already built in — see the main README's "24/7 Daemon" section:

```
bash manage_nexus.sh install-agents
bash manage_nexus.sh start
```

## Linux (systemd)

See `jarvis-nexus.service.example` in this folder. Copy it to
`~/.config/systemd/user/jarvis-nexus.service`, fill in the two paths it asks
for, then:

```
systemctl --user daemon-reload
systemctl --user enable --now jarvis-nexus
loginctl enable-linger $USER
```

## Windows (Task Scheduler)

1. Open Task Scheduler → Create Task
2. General tab: name it "JARVIS Nexus", check "Run whether user is logged on or not"
3. Triggers tab: New → "At log on"
4. Actions tab: New → Program/script: the full path to `run.bat` in your
   JARVIS install folder; "Start in" field: that same folder
5. Settings tab: check "If the task fails, restart every" → 1 minute

There's no first-party CLI for this on Windows the way `systemctl`/`launchctl`
work on the other two — Task Scheduler's UI is the standard way to do it.
