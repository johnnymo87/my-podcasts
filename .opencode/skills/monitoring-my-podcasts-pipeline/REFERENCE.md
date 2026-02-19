# Monitoring Reference

## Contents
- SQLite state checks
- R2 object checks
- Common failures

## SQLite state checks

```bash
uv run python -c "import sqlite3; conn=sqlite3.connect('/persist/my-podcasts/state.sqlite3'); print('episodes', conn.execute('select count(*) from episodes').fetchone()[0]); print('processed', conn.execute('select count(*) from processed_emails').fetchone()[0]); print(conn.execute('select feed_slug, source_tag, title from episodes order by created_at desc limit 5').fetchall())"
```

## R2 object checks

```bash
R2_ACCOUNT_ID="$(sudo cat /run/secrets/r2_account_id)" R2_ACCESS_KEY_ID="$(sudo cat /run/secrets/r2_access_key_id)" R2_SECRET_ACCESS_KEY="$(sudo cat /run/secrets/r2_secret_access_key)" uv run python -c "from pipeline.r2 import R2Client; r2=R2Client(); keys=['feed.xml','feeds/levine.xml','feeds/yglesias.xml']; print({k:r2.head_object_size(k) for k in keys})"
```

## Common failures

- `LookupError: Resource punkt_tab not found`
  - confirm `NLTK_DATA=/persist/my-podcasts/nltk_data` for service
  - rerun service setup path via `sudo systemctl restart my-podcasts-consumer`
- queue backlog grows while service is up
  - inspect logs: `journalctl -u my-podcasts-consumer -n 200 --no-pager`
  - verify `CLOUDFLARE_QUEUE_ID` and token scopes
