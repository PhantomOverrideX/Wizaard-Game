EMBERWILD REALMS
================
Play alone:   python play.py            (opens the game in your browser)
Host co-op:   python play.py --lan      (prints the address your friends open)
Host co-op with password: python play.py --lan --password you_password

Friends: open the address the host's console prints (e.g. http://192.168.1.20:8000),
choose "Co-op multiplayer" on the title screen, type a name and the same address, then Join.
Over the internet: forward the port on your router, or use a tunnel (ngrok / cloudflared).
Options: --port 9000 | --password secret | --max 6 | --no-browser

Co-op tips for slower computers: pick Low graphics. Each enemy is simulated by whoever spawned it,
and the server only relays messages, so no single computer does all the work.
Keys: Enter = chat.  Everything else is the same as single player.

Co-op details
- Caves, dungeons and the Sunken Crypt (with its boss) are shared: walk through the same door and you are in the same place.
  The first player in runs its enemies; if they leave while you stay, the server hands the enemies to you.
- The server keeps the day/night clock, weather and which camps/bosses are cleared in world_state.json (next to play.py),
  so the world carries on between sessions. Use  python play.py --fresh  for a brand-new shared world.
- The server also sends each player only the enemies near them. Rendering (graphics) always happens on your own computer.

Auto-update
- Every time play.py starts it checks the game's GitHub repository (PhantomOverrideX/Wizaard-Game). If there is a newer
  commit it downloads only the changed files, verifies each against GitHub's checksum, and swaps them in (and replaces
  play.py itself, then restarts). If you are offline or GitHub is unreachable it just starts the version you have.
- Options: --no-update (skip the check)  --check (update then exit)  --force-update (re-verify everything)
           --repo owner/name  --branch name  --token xxxx (or set GITHUB_TOKEN, for a private repo)
- Your saves are in the browser, so updates never touch them. world_state.json (the shared world) is also left alone.
- To publish an update: put the files of this folder (index.html, assets/, lib/, play.py) in the repo and commit.
