# Render deployment

1. Push this directory as the repository root. Exclude `run_history.json`, `manual_verification.json`, `.env`, and `*.db`.
2. In Render, create a Blueprint from the repository. `render.yaml` defines a free Python web service using local SQLite. The free web service filesystem is ephemeral, so demo data may be lost after a restart or redeploy. For persistent data, provision PostgreSQL separately and set `DATABASE_URL` in Render.
3. Supply `DEMO_USERNAME` and `DEMO_PASSWORD`. `APP_SECRET` is generated. The service refuses to start on Render without all three.
4. Verify `/healthz`, login, PDF and image upload, decision, preview, history after refresh, and persistence after restart.
5. To import old local history online, use a trusted environment with `DATABASE_URL` and `python migrate_legacy.py`. Never commit old JSON history to the public repo.

Gunicorn binds `0.0.0.0:$PORT` with a 180-second timeout. Document bytes and run metadata live in local SQLite unless `DATABASE_URL` is set. Render [free web services spin down and use ephemeral filesystems](https://render.com/docs/free). This is a short demo deployment, not permanent retention.
