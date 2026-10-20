# Northflank operating checklist

This repository does not contain Northflank project configuration. The service must be configured explicitly before a production rollout.

## Build and process

- Use the Python buildpack with Python 3.13 and verify the build log resolves `requirements.txt` successfully.
- Select the `worker` custom process from the root `Procfile`. The process command is `python main.py`.
- Keep the service at one replica. A Discord bot token must not be used by multiple active bot processes.
- Disable automatic CD while validating the candidate build. Re-enable it only after the staging gateway smoke test passes.

## Persistent state

1. Attach a Northflank persistent volume.
2. Mount it at an absolute container path such as `/data`.
3. Set `DATA_DIR=/data` in the service runtime environment. `STATE_DIR` is accepted as an alias.
4. Before the first restart, copy and verify both `daily_xp_data.pkl` and `achievements_data.pkl` in the mounted directory. The first startup with `DATA_DIR` set copies a missing legacy CWD file into the mounted directory, while an existing target file always wins.
5. Keep at least one external backup of the two primary Pickle files and their `.bak.1`–`.bak.3` generations before changing the service.

The state format remains Pickle and the historical filenames remain unchanged. The application writes `typing_cooldowns.json` and `bot_metrics.json` in the same configured directory.

## Secrets and rollout

Configure `DISCORD_TOKEN`, `HF_TOKEN`, `HF_IMG_TOKEN`, and `SPECIAL_ADMIN_USER_ID=855749166764654653` as runtime secrets. Add `SPECIAL_ADMIN_GUILD_IDS` if the special account must be limited to particular guilds.

For a rollback, stop the candidate service, verify the volume is mounted at the same path, restore the primary Pickle files from the last known-good backup if they were quarantined, and deploy the previous commit. Do not run old and new bot processes concurrently.

The first production rollout remains conditional on Northflank build logs, the mounted path, replica count, CI/CD branch and CD settings, runtime secrets, and a staging Discord interaction test being recorded.
