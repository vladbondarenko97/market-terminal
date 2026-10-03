# Upload receiver (optional)

The pipeline can upload each run's database copy, dashboard XML and report to your own web server, so the phone
notification can link to a permanent copy of the report. Nothing is uploaded unless `UPLOAD_URL` and `UPLOAD_TOKEN`
are set in `.env`.

## Install

1. On the server, put a long random token in a file outside the web root, readable by the web server only:
   `/etc/portfolio-upload/token` (one line, `root:www-data`, mode `0640`).
2. Copy `upload_receiver.php` and `.htaccess` from this folder into the upload folder, for example `/admin/data/`.
   On nginx, translate `.htaccess` into `location` rules (`autoindex off; location ~ \.db$ { deny all; }`).
3. Check that the data is not downloadable: `curl -I https://example.com/admin/data/portfolio.db` and
   `https://example.com/admin/data/backup/` must both return 403.
4. On the Mac, add to `.env`:

   ```
   UPLOAD_URL=https://example.com/admin/data/upload_receiver.php
   UPLOAD_TOKEN=<the same token>
   REPORT_UPLOAD=1
   ```

   With `REPORT_UPLOAD=1`, each run uploads the report under an unguessable name and the phone notification gets a
   permanent **Full report** button (ntfy.sh attachments expire after 3 hours).

## What the receiver does

- Reads the token from the file outside the web root; it is never in a served file.
- Accepts only known filenames. Daily reports are stored under an unguessable name in `reports/`.
- `.htaccess` blocks directory listings and direct downloads of `*.db`.
