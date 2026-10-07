# Upload receiver (optional)

A small PHP script and an Apache `.htaccess` for your own web server. After a delivered run, the pipeline can send
three things to it: a slim copy of the database, the dashboard XML, and (optionally) the daily report, so the phone
notification can link to a permanent copy of the report. Nothing is uploaded unless `UPLOAD_URL` and `UPLOAD_TOKEN`
are set in `.env`.

This page covers the server side. For the Mac side see [Configuration](../docs/configuration.md#upload) (the
three variables) and [Operations](../docs/operations.md) (delivery and troubleshooting). For exactly what the
database copy contains see [Data](../docs/data.md#what-gets-uploaded).

## Install

1. On the server, put a long random token in a file outside the web root, readable by the web server only:
   `/etc/portfolio-upload/token` (one line, `root:www-data`, mode `0640`). For example, generate one with
   `openssl rand -hex 32`. The path is written in `upload_receiver.php`; edit it there to use another location.
   If the file is missing or empty, every request is refused.
2. Copy `upload_receiver.php` and `.htaccess` from this folder into the upload folder, for example `/admin/data/`.
   **If your folder is not `/admin/data/`, edit `.htaccess`** (see [What `.htaccess` blocks](#what-htaccess-blocks)).
   On nginx, translate the rules into `location` blocks, for example:

   ```
   autoindex off;
   location ~ \.(db|sqlite|sqlite3|bak|xml)$ { deny all; }
   location ^~ /admin/data/backup/ { deny all; }
   ```
3. Check that the data is not downloadable: `curl -I https://example.com/admin/data/portfolio.db` and
   `https://example.com/admin/data/backup/` must both return 403.
4. Check the PHP size limits (see [Size limits](#size-limits)).
5. On the Mac, add to `.env`:

   ```
   UPLOAD_URL=https://example.com/admin/data/upload_receiver.php
   UPLOAD_TOKEN=<the same token>
   REPORT_UPLOAD=1
   ```

   `REPORT_UPLOAD=1` is optional. With it, each run uploads the report under an unguessable name and the phone
   notification gets a permanent **Full report** button (ntfy.sh attachments expire after 3 hours).

## What the receiver does

`upload_receiver.php` handles one POST per run. It reads the token from the file outside the web root and compares
it with `hash_equals`; a missing or wrong token gets HTTP 403 `Unauthorized`. It then goes through every uploaded
file:

| Upload | Accepted when | Stored as |
|---|---|---|
| Daily report | form field `report` and file name `market-report-YYYY-MM-DD-HHMMZ.txt` | `reports/market-report-YYYY-MM-DD-HHMMZ-<24 random hex>.txt`. The reply contains the full `https://` link. Reports are **not** copied to `backup/`. |
| Database | file name exactly `portfolio.db` | `portfolio.db` in the upload folder, replacing the previous one |
| Dashboard XML | file name exactly `volume_dashboard.xml` | `volume_dashboard.xml` in the upload folder, replacing the previous one |
| Anything else | never | the reply says `rejected` for that file |

The pipeline sends the database as form field `portfolio`, the dashboard as `dashboard` and the report as `report`.
For the two replaceable files the receiver then keeps a **timestamped copy** in `backup/`, for example
`backup/portfolio_2026-10-07_09-31-45.db` and `backup/volume_dashboard_2026-10-07_09-31-45.xml` (server time). It
creates `backup/` and `reports/` on first use (mode 0750, and an empty `index.html` in `reports/`). Nothing
deletes old backups, so the folder grows with every upload; prune it with a cron job. The copies hold the legacy
ledger tables only (the pipeline strips the `v2_*` tables before sending), so they cannot restore the full
database.

The reply is JSON with one entry per file, for example `{"portfolio":{"status":"ok","name":"portfolio.db"}}`. A
file can be `ok`, `rejected` (name not allowed) or `error` (with a PHP upload error code), and the HTTP status is
still 200. The Mac side treats any HTTP 200 as success and does not read these entries, so check the stored reply
if something looks missing:

```bash
sqlite3 CME_Data/portfolio.db "select delivery_detail from v2_runs order by started_at desc limit 1"
```

The `upload` entry holds the first 300 characters of the reply.

## What `.htaccess` blocks

| Rule | Effect |
|---|---|
| `Options -Indexes` | no directory listings |
| `FilesMatch "\.(db\|sqlite\|sqlite3\|bak\|xml)$"` | no direct download of any `.db`, `.sqlite`, `.sqlite3`, `.bak` or `.xml` file, which covers `portfolio.db`, `volume_dashboard.xml` and every file in `backup/` |
| `If "%{REQUEST_URI} =~ m#^/admin/data/backup(/\|$)#"` | everything under `/admin/data/backup`, whatever the extension |
| `AddDefaultCharset UTF-8` and `AddCharset UTF-8 .txt` | reports display correctly in a browser |

- The backup path in the last rule is **hardcoded to `/admin/data/backup`**. If you install the receiver in another
  folder, edit that line to match; otherwise `backup/` is protected only by the extension rule. The extension
  rule is the one that matters for the files the receiver writes, because they all end in `.db` or `.xml`.
- The uploaded `portfolio.db` and `volume_dashboard.xml` are for server-side use only: the comment in `.htaccess`
  expects a page on your server (such as an `index.php`) to read `portfolio.db` from disk. That page is not part
  of this repository.
- Reports in `reports/` are readable by anyone who has the link. The random part of the name is 96 bits, and
  `Options -Indexes` plus the empty `index.html` stop anyone from listing the folder.
- The rules need Apache 2.4 and an `AllowOverride` that permits `.htaccess` to use `AuthConfig` (`Require`),
  `FileInfo` (charset) and `Options` (`-Indexes`). Step 3 of the install is the test that they work.

## Size limits

PHP refuses large uploads before the script runs, and its defaults are small (`upload_max_filesize` 2M,
`post_max_size` 8M). Set both above the size of the slim database copy. A delivered run prints
`Prepared portfolio.db (legacy tables, N MB)` when it builds the copy (in the run output or the schedule log);
keep both limits comfortably above N. Two failures look different:

- A single file over `upload_max_filesize` is reported as `{"status":"error","code":1}` for that file, with HTTP 200.
- A request over `post_max_size` loses all its fields, including the token, so the receiver answers 403
  `Unauthorized` even though the token is right.
