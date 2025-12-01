```markdown
# DLK NSFW-Cleaner — Deploy to Heroku

This repo contains a Telegram bot (DLK NSFW Cleaner) that auto-detects and deletes explicit NSFW stickers / photos / GIFs / videos using NudeNet + Pyrogram.

Below are concise steps to deploy on Heroku.

## Files added for Heroku
- Procfile — tells Heroku to run the bot as a worker.
- requirements.txt — Python dependencies.
- Aptfile — install `ffmpeg` system package (needed to extract frames).
- runtime.txt — Python runtime.
- .env.example — example environment variables.

## Steps to deploy

1. Add files to your repo, commit and push to GitHub.

2. Create Heroku app:
   - heroku create your-app-name

3. Set buildpacks (important: add `apt` buildpack first, then python):
   - heroku buildpacks:clear
   - heroku buildpacks:add --index 1 https://github.com/heroku/heroku-buildpack-apt
   - heroku buildpacks:add heroku/python

4. Set config vars (replace values):
   - heroku config:set API_ID=123456
   - heroku config:set API_HASH=your_api_hash
   - heroku config:set BOT_TOKEN=123456:ABC...
   - heroku config:set MONGO_URI="your_mongo_connection_string"
   - (optional) heroku config:set NSFW_THRESHOLD=0.75 NSFW_STICKER_LIMIT=3 MUTE_DURATION_SECONDS=86400 LOG_CHAT_ID=@yourlogchannel

   Alternatively use the Heroku dashboard → Settings → Config Vars.

5. Push code to Heroku (if using Git):
   - git push heroku main

6. Scale worker dyno:
   - heroku ps:scale worker=1

7. View logs:
   - heroku logs --tail

## Notes / troubleshooting
- NudeNet will download the model on first run (it may take time and needs network access). Heroku dynos have ephemeral filesystem — the model will be downloaded into dyno filesystem; you may see model download each time dyno restarts.
- FFmpeg is installed via Aptfile + heroku-buildpack-apt.
- Ensure the bot is added to groups and made admin with "Delete messages" and "Restrict members" permissions for full functionality.
- Keep your real .env out of git. Use Heroku config vars instead.

If you want, I can:
- prepare a small Git patch / PR with these files added to your repository, or
- give commands to run locally to add and push them.

Tell me which you prefer and I will create the patch or give the exact git commands.
```
