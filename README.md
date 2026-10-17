# Porto Agent

A single-page, no-login web app for one PortoBuild project, adapted from
Task Bot (`web_app/task-bot`): same `config.json`/`credential.json` split,
`main/` + `util/` layout, `managed_agent.py` Ark client and `_log/` JSON-line
logging. Task Bot's response guardrail and output viewer are not included.

Open it with a project id:
`http://127.0.0.1:7012/?project=kino-kopi-3f9a1c98239-2026`
(without `?project=`, `config.json` → `project.default_project_id` is used).

## Project → session
`project_setting.json` maps each project id to its Managed Agent session:

```json
{
  "projects": {
    "kino-kopi-3f9a1c98239-2026": { "session_id": "sesn-20261008122736-peq7j" }
  }
}
```

An entry can also set `brainstorm_url` to override the URL template. The
file is re-read on every request, so new projects work without a restart.
`GET /api/project/<project_id>` returns the resolved session and brainstorm
URL as JSON.

## Tabs
- **Chat**: shows the session the project maps to; the session bar lets you
  type another session id and **Connect** (it goes into the URL as
  `?session=`, overriding the mapping). Works like Task Bot: on load the page
  reads the session's full REST event history, sending posts a `user.message`
  event, and the page polls `/api/events?since=` every 2 s until the session
  goes idle. Images/PDF are uploaded and mounted at
  `/mnt/session/uploads/<name>`; `.txt/.md/.csv/.json` are inlined as text.
- **Brainstorm**: shows
  `config.json` → `project.brainstorm_url_template` with `{project_id}`
  filled in (e.g.
  `https://portobuild.tos-ap-southeast-1.bytepluses.com/brand/<project_id>/brainstorm.html`)
  in an iframe, loaded the first time the tab is opened. The page is fetched
  server-side and served from this site at `/brainstorm/<project_id>` (with a
  `<base href>` at its TOS URL, so images and links still load from TOS), which
  puts it on this site's origin so the tab can read it. **Reload** fetches it
  again. **Send for Analysis** reads the idea picked on the page with its
  own "Select for analysis" button (the row marked `.sel`, falling back to
  `localStorage["bbd-brainstorm-<project_id>"]`), plus that idea's note and
  the general notes, and copies **only that idea's** designs into a new
  timestamp folder under the design folder (the notes go into its
  `selection.json`). Because the page now runs on this site's origin,
  picks made earlier on the TOS URL directly aren't visible here.
- **Design**: each **Send for Analysis** copies the idea's images into its
  own folder, `brand/<project_id>/design/<YYYYMMDD-HHMMSS>/` (UTC; `-2`,
  `-3`... if two land in the same second), with a `selection.json` (folder,
  idea id/title, time, notes, each file's label and source key), written
  last. Nothing already in the design folder is changed or deleted. Files
  are public-read with `Cache-Control: no-cache`. The originals stay in
  `img/` because `brainstorm.html` still uses them.

  The tab shows one group per folder, newest first and marked **Latest**,
  each with the idea's title, selection time and folder, and its images in
  the order of the pick (logo, brand character, brand story, business card);
  click an image for full size. Images from before the timestamp folders
  (directly in `design/`, with `design/selection.json`) show as the last
  group, "design/ (top level)". **Refresh** reloads the folder.

  **Edit** on an image opens the editor, ported from jonah-image-editor
  (`main/static/editor.js`): Draw / Box (move, resize, rotate) / Text, color,
  line and font size, Undo, Reset. **Crop** dims everything outside a crop
  area: drag its corners to resize, inside to move, or draw a new one; pick
  a ratio (Free, Original, 1:1, 4:3, 3:4, 3:2, 2:3, 16:9, 9:16), then
  **Apply crop** or Enter (Cancel / Esc drops it; Undo restores the
  uncropped image). Saving or generating with a crop area still open applies
  it first, and a saved crop changes the file's pixel size. **AI edit with Seedream** sends the
  current canvas (marks included) as the reference image, plus an optional
  extra reference, with your prompt, at the canvas's closest aspect ratio
  and `image.resolution` (2K). Results are kept in `_generated/<project_id>/`;
  **Use this** loads one into the canvas (Undo goes back). **Save to design
  folder** overwrites that file on TOS, in its own timestamp folder and
  format (JPEG for `.jpg`), and records `edited_at` for it in that folder's
  `selection.json`. Other picks are not touched.

  TOS sends no CORS headers, so the editor loads images through this site
  (`/api/design/<project_id>/file/<name>`); a canvas that drew them straight
  from TOS couldn't be exported.

The current tab is kept in the URL hash (`#chat` / `#brainstorm` / `#design`).

Design API: `GET /api/design/<project_id>` (`groups`, newest first: each
`folder`, `images`, `selection`), `GET`/`PUT /api/design/<project_id>/file/<folder>/<file>`
(read through this site / save an edited image as `{"image": "data:..."}`;
just `<file>` for the top-level group), `POST /api/design/<project_id>/generate`
(Seedream; multipart `prompt`, `image`, `reference`, `width`, `height`),
`GET /generated/<project_id>/<name>`, `POST /api/design/<project_id>/select`
with `{"idea_id": "3"}` (the listing plus `created`, the new folder). TOS calls go through `main/tos_client.py`, copied
from builder-brand-design (signed TOS4-HMAC-SHA256, no SDK).

## Configuration
- `config.json` → `agent.base_url`, upload limits; `site.title/host/port`
  (default `0.0.0.0:7012`); `project.setting_path`, `default_project_id`,
  `brainstorm_url_template`, `design_folder` (default `design`);
  `tos.endpoint/region/key_prefix_template` (`brand/{project_id}`).
  `image.model_id` (`dola-seedream-5-0-pro-260628`), `resolution`, `watermark`,
  `max_reference_mb` for the Seedream editor.
- `credential.json` (gitignored) → `agent_ark_key`, `model_ark_key` (Seedream; falls
  back to `agent_ark_key`), plus `tos_access_key_id`,
  `tos_secret_access_key`, `tos_bucket` (same as builder-brand-design's).

There is no login: anyone who can reach the port can chat with any session,
add picks to any project's design folder and overwrite its images with edits.

## Running
Windows: `start_portoagent.bat` · Ubuntu/Debian: `./start_portoagent.sh` ·
or `cd main && python app.py`, then open http://127.0.0.1:7012.

## Logs
`_log/app_<date>.log`, `_log/agent_<date>.log` (every Ark API call),
`_log/conversation_<date>.log` (messages sent from this page).
