# Screenshots

The README shows a screenshot of the crema web report. To add or update it:

1. Run the web report with some data in the database:

   ```bash
   crema serve
   ```

   (Ideally after at least one `crema review`, so the report shows a scored
   review, a drafted edit, and a few recent shots — not an empty state.)

2. Open `http://127.0.0.1:8765` (or your `CREMA_HOST:CREMA_PORT`) and take a
   screenshot of the report.

3. Save it as **`docs/report.png`** in this folder.

4. In the top-level `README.md`, uncomment the image line in the **Screenshots**
   section:

   ```markdown
   ![crema web report](docs/report.png)
   ```

   and remove the "_Add a screenshot…_" placeholder line.

Tips:
- Crop to the report itself; a browser window at a sensible width reads best on
  the GitHub page.
- If the report contains a real machine hostname/IP you'd rather not publish,
  blur or crop it before committing.
