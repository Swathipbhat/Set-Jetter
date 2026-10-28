# Deploy SetJetter AI on Render

Your Qloo key is entered only in Render's dashboard. It is never in the code, the repo, or the browser.

## 1. Put the project on GitHub
```
unzip setjetter.zip && cd setjetter
git init && git add . && git commit -m "SetJetter AI"
# create an empty repo on github.com, then:
git remote add origin https://github.com/<you>/setjetter.git
git branch -M main && git push -u origin main
```
`.gitignore` already excludes `.env`, so a local key file can't be pushed by accident.
Before pushing, check: `git grep -i "hack_"` should print nothing.

## 2. Create the service on Render
1. render.com -> **New +** -> **Blueprint** -> connect GitHub -> pick the repo. Render reads `render.yaml`.
2. It asks for `QLOO_API_KEY`: paste your key there -> **Apply**.
   (Manual alternative: New + -> Web Service; Build `pip install -r requirements.txt`; Start
   `gunicorn server:app --worker-class gthread --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT`;
   add the `QLOO_API_KEY` environment variable.)
3. Wait for the first deploy (a few minutes). Your site is at `https://setjetter-ai.onrender.com` (or similar).

## 3. Check it
- `<your-url>/healthz` shows `{"ok": true}`.
- Run a search. In **Agent trace**, results should be badged `qloo-live` (not `fixture`), with seconds per call.
- If something looks wrong: Render dashboard -> **Logs**, and send me the Agent trace text.

## Things to know
- **Free plan sleeps** after ~15 min idle; the next visit takes about a minute to wake. Open the site before a demo.
- **Cache is in memory**: it resets whenever the service restarts or sleeps.
- **Abuse protection**: each visitor is limited to 8 searches/min (`RATE_LIMIT_PER_MIN`), so strangers can't burn your Qloo quota.
- **Leave `ANTHROPIC_API_KEY` unset** on a public site unless you want to pay for Claude on every search; the scripted planner is free.
- **Rotate the key if it leaks**: the hackathon key was shared in a chat, so treat it as exposed if the repo is public. Generate a new one with Qloo and update it in Render -> Environment (Render redeploys automatically).
