# Render deployment

1. Push this directory as the repository root. Exclude `run_history.json`, `manual_verification.json`, `.env`, and `*.db`.
2. In Render, create a Blueprint from the repository. `render.yaml` defines a free Python web service. Provision PostgreSQL separately and set its connection string as `DATABASE_URL` in Render. Render's free web service filesystem is ephemeral; the app refuses to start there without `DATABASE_URL` to prevent silent data loss. Existing Blueprints do not prompt for newly added `sync: false` variables, so add this variable on the service's Environment page.
3. Supply `DEMO_USERNAME` and `DEMO_PASSWORD`. `APP_SECRET` is generated. The service refuses to start on Render without all three.
4. Verify `/healthz`, login, PDF and image upload, decision, preview, and history after a page refresh and a service restart.
5. To import old local history online, use a trusted environment with `DATABASE_URL` and `python migrate_legacy.py`. Never commit old JSON history to the public repo.

Gunicorn binds `0.0.0.0:$PORT` with a 180-second timeout. Local development uses SQLite; on Render, document bytes and run metadata must live in PostgreSQL. Render [free web services spin down and use ephemeral filesystems](https://render.com/docs/free). Keep database credentials in Render's Environment settings, never in the repository.
