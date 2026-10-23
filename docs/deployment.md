# Running Hapilot for a team on a local network

By default the UI listens on 127.0.0.1 and is opened through an SSH tunnel (README, "Opening the UI"). This page
describes a long-running install that colleagues open in their browser on the local network, and that follows a
git branch: new commits are tested and switched to automatically, with a rollback when something fails.

## What is reachable from the network

- Only the Chainlit UI, when `HAPILOT_UI_HOST` is set to the machine's LAN address or `0.0.0.0`. The model proxy,
  `llama-server` and the optional n8n stay on 127.0.0.1 whatever `HAPILOT_UI_HOST` says.
- Use it with login on (`HAPILOT_UI_USERS_FILE`) and HTTPS (`HAPILOT_UI_SSL_CERT`, `HAPILOT_UI_SSL_KEY`); without
  HTTPS, passwords and document text cross the network unencrypted. `hapilotctl.py make-cert` creates a self-signed
  certificate: each browser warns once; people can compare the SHA-256 fingerprint the command prints with the one in
  the browser's certificate details before accepting it.
- A model server started outside Hapilot (for example LM Studio) has its own bind setting. Keep it on 127.0.0.1
  (`lms server start --bind 127.0.0.1`): an OpenAI-compatible server on the network answers anyone, without login.
- If the machine has a firewall, allow the UI port from the local network only.

## Layout

| What | Where | Changed by |
|---|---|---|
| Code | a git checkout, e.g. `~/Hapilot-team` | the updater only — the updater refuses to run when tracked files were edited by hand |
| Settings | `.env` in the checkout (not tracked) | the operator |
| Data: accounts, chats, custom nodes, templates, signatures, knowledge base, workspaces, logs, backups | a folder outside the checkout (`HAPILOT_DATA_DIR`) | the running UI |
| Company documents | e.g. `<data folder>/docs` (`HAPILOT_DOCS_DIR`) | administrators in the agent panel (**📚 Baza wiedzy**), or the operator in the folder; the running UI indexes the changes |

Keeping the data outside the checkout means an update can never touch it.

## Setting it up

```bash
git clone https://github.com/majaszturmaj/Hapilot.git ~/Hapilot-team && cd ~/Hapilot-team
python3 -m venv .venv
.venv/bin/pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -r requirements.txt -r requirements-test.txt   # the updater runs the unit tests
```

`.env` (example values; use ports that are free on the machine):

```bash
HAPILOT_DATA_DIR=/home/USER/hapilot-team-data
HAPILOT_DOCS_DIR=/home/USER/hapilot-team-data/docs
HAPILOT_UI_HOST=0.0.0.0
HAPILOT_UI_PORT=8641
HAPILOT_MODELSWAP_PORT=8651
HAPILOT_BACKEND_PORT=8652
HAPILOT_UI_USERS_FILE=/home/USER/hapilot-team-data/ui_users.txt
HAPILOT_UI_SSL_CERT=/home/USER/hapilot-team-data/tls/hapilot.crt
HAPILOT_UI_SSL_KEY=/home/USER/hapilot-team-data/tls/hapilot.key
# HAPILOT_PROFILES_FILE=/home/USER/hapilot-team-data/profiles.yaml   # models of this install
# HAPILOT_UPDATE_BRANCH=main
```

`HAPILOT_UI_HOST=0.0.0.0` keeps working when the machine gets another address from DHCP; a fixed LAN address
binds the UI to that interface only.

```bash
.venv/bin/python scripts/hapilotctl.py setup                       # sandbox and OCR images, embedding model, data folders
.venv/bin/python scripts/hapilotctl.py make-cert --ip 192.0.2.10 --dns hapilot.example.test
.venv/bin/python scripts/hapilotctl.py add-user admin              # and one account per person or role
.venv/bin/python scripts/hapilotctl.py ingest                      # documents in HAPILOT_DOCS_DIR
.venv/bin/python scripts/hapilotctl.py models up --profile best-local   # or a model server of your own
.venv/bin/python scripts/hapilotctl.py ui up                       # prints the address to open
```

`make-cert` names every address and host name people will type (`--ip`, `--dns`, repeatable); `localhost` and
127.0.0.1 are always included, for an SSH tunnel.

## Company documents

Once the UI runs, the knowledge base follows the documents folder without `ingest`:

- administrators add, replace and delete documents in the agent panel (`/hapilot/admin`, tab **📚 Baza wiedzy**);
  new files are saved into `HAPILOT_DOCS_DIR` and indexed in the background, OCR included;
- files copied into the folder on the server, changed or deleted there are picked up at start-up and every
  `HAPILOT_KB_RESCAN_MINUTES` minutes (default 5; 0 = only at start-up and on "Sprawdź folder teraz");
- replaced and deleted files are kept in `<data folder>/knowledge_base/trash/` for `HAPILOT_KB_TRASH_DAYS` days
  (default 30) and can be restored in the panel; after that they are deleted;
- one file may have up to `HAPILOT_KB_MAX_UPLOAD_MB` MB (default 50).

Only the changed documents are indexed. While the UI runs, `hapilotctl.py ingest` refuses to run: the embedded
database takes one writer, and the UI is it. To rebuild the whole index: `ui down`, `ingest --force`, `ui up`.
Indexing counts as activity for the updater, so it never restarts the UI in the middle of it; a re-check that finds
nothing to do does not.

## Updates

```bash
.venv/bin/python scripts/hapilotctl.py update                 # once
.venv/bin/python scripts/hapilotctl.py status                 # processes, and the result of the last check
```

`update` fetches `origin/<branch>` (`--branch`, else `HAPILOT_UPDATE_BRANCH`, else `main`). When it has moved:

1. **Waits while people use the UI**: it restarts nothing while an answer, a document form or a regeneration is in
   progress, or within 10 minutes of the last chat action (`--idle-minutes`; the UI records its activity in
   `<data folder>/run/ui_activity.json`). The next run tries again. `--now` skips the wait.
2. **Backs up** the chat database (SQLite online backup), the accounts file, custom nodes, templates, prompt and
   template edits, the audit log, signatures and the list of who added which document to
   `<data folder>/backups/<time>-<old commit>/` (the 10 newest are kept). Workspaces, the documents (and their
   trash) and the knowledge base are not copied: updates do not change them, and `ingest` rebuilds the index.
3. **Prepares the new commit next to the running one** (a temporary git worktree): installs `requirements*.txt` if
   they changed, builds those of its Docker images (sandbox, OCR) whose tag does not exist yet — an existing tag is
   never rebuilt, a changed Dockerfile comes with a new tag — and runs the unit tests there.
4. **Switches** the checkout to the new commit; with the new commit's launcher it stops the UI, brings the knowledge
   base up to date (`ingest`: re-indexed only if the documents, the index version or the OCR changed — never while
   the UI has the database open) and starts the UI again, if it was running; then checks that it answers and still
   asks for a login.

If step 3 fails, the running UI is never touched. If step 4 fails, the checkout goes back to the previous commit
(and its requirements), the knowledge base is rebuilt by that commit (`ingest --force`) and the UI is restarted on it. A commit that failed is not tried again until a newer one
arrives (`--retry` forces it). Every change is logged in `<data folder>/log/update.log`; test output of a failed
attempt in `<data folder>/log/update-tests.log`; the last check in `<data folder>/run/update.json`.

### Automatic updates with cron

No root rights needed. `crontab -e`, then (one line each; adapt the paths):

```bash
@reboot cd ~/Hapilot-team && .venv/bin/python scripts/hapilotctl.py ui ensure >> ~/hapilot-team-data/log/cron.log 2>&1
*/10 * * * * cd ~/Hapilot-team && .venv/bin/python scripts/hapilotctl.py update --keep-running >> ~/hapilot-team-data/log/cron.log 2>&1
```

`ui ensure` and `--keep-running` start the UI when it is not running — after a reboot or a crash — unless it was
stopped on purpose with `ui down` or `down` (`ui up` lifts that). A model server outside Hapilot must be up before
the UI is useful; start it from the same `@reboot` line or its own.

### Restoring a backup

```bash
.venv/bin/python scripts/hapilotctl.py ui down
cp ~/hapilot-team-data/backups/<time>-<commit>/chat_history.sqlite ~/hapilot-team-data/chat_history.sqlite
rm -f ~/hapilot-team-data/chat_history.sqlite-wal ~/hapilot-team-data/chat_history.sqlite-shm
.venv/bin/python scripts/hapilotctl.py ui up
```

The other folders in a backup are copied back the same way. To pin the install to an older commit, check it out
in a branch of your own and set `HAPILOT_UPDATE_BRANCH` to it.

### Limits

- Changed requirements are installed into the install's venv before the tests, while the old UI still runs (its
  modules are already loaded); a failed attempt installs the old requirements again. A new torch version needs the
  CPU wheel by hand (see above), or pip fetches the CUDA build.
- A restart closes the open browser connections; Chainlit reconnects, and chats are kept in the history. A document
  form left open longer than the idle time is lost when the UI restarts.
- The updater goes online: `git fetch` from the checkout's `origin`, pip when requirements change, Docker when an
  image's Dockerfile changed (base image and packages). The UI itself still makes no outbound connections (README, "Privacy").
