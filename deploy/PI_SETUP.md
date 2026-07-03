# Headless Pi setup (over SSH)

crema is CLI-only. Everything below runs over SSH; nothing needs a desktop. The
web report binds to **loopback** on a custom port, so it won't clash with your
other webservers and isn't exposed on the LAN — you view it through an SSH
port-forward.

Assumes the project lives at `/home/pi/crema` and the user is `pi`. Adjust paths
in the three `deploy/*.service` / `*.timer` files if yours differ.

## 1. Get the project onto the Pi

Clone from GitHub (the repo is private, so authenticate once). Easiest is the
GitHub CLI:

```bash
ssh pi@<pi-host>
sudo apt install -y gh git        # if not already present
gh auth login                     # follow prompts (HTTPS, paste a token or use the browser code)
gh repo clone waevans10/crema ~/crema
```

Or with plain git + a personal access token:

```bash
git clone https://github.com/waevans10/crema.git ~/crema
```

`.env`, `.venv`, and the database are gitignored — you create them on the Pi in
the next steps.

## 2. Install uv and a pinned Python 3.13

Raspberry Pi OS (Bookworm) ships Python 3.11; crema needs 3.13. `uv` installs a
standalone 3.13 without touching the system Python:

```bash
ssh pi@<pi-host>
curl -LsSf https://astral.sh/uv/install.sh | sh    # installs uv
source ~/.bashrc                                    # put uv on PATH
cd ~/crema
uv python install 3.13
uv sync                                             # creates ./.venv with the `crema` command
```

## 3. Configure

```bash
cp .env.example .env
nano .env    # set ANTHROPIC_API_KEY and GAGGIMATE_GAGGIMATE_HOST
```

Pick a loopback port that's free on the Pi (default `8765` is uncommon; change
`CREMA_PORT` if something already uses it). Confirm the Pi can reach the machine:

```bash
ping -c1 gaggimate.local        # or your machine's IP
uv run crema ingest             # should report "Ingested N new shot(s)"
uv run crema review             # ingest + Claude review, prints suggestions
```

## 4. Schedule reviews + run the web report (systemd)

```bash
sudo cp deploy/crema-review.service deploy/crema-review.timer /etc/systemd/system/
sudo cp deploy/crema-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crema-review.timer   # periodic reviews
sudo systemctl enable --now crema-web.service     # always-on report on loopback
```

Check them:

```bash
systemctl list-timers crema-review.timer
journalctl -u crema-review.service -n 30 --no-pager
systemctl status crema-web.service
```

## 5. View the report from your laptop

The web service listens on `127.0.0.1:<CREMA_PORT>` on the Pi. Forward it over
SSH — no LAN port opened, no auth needed:

```bash
ssh -L 8765:localhost:8765 pi@<pi-host>
# then open http://localhost:8765 in your browser
```

(Match the left/right port to `CREMA_PORT` if you changed it.)

## Updating later

Push changes from your Mac (`git push`), then on the Pi:

```bash
ssh pi@<pi-host> 'cd ~/crema && git pull && uv sync && sudo systemctl restart crema-web.service'
```
