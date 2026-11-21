# Importing your archive

**In short:** Turn your [CMS](GLOSSARY.md#cms) archive, feed or spreadsheet into article files Dewey can
search: try 20 articles, check them, import everything, then index.

**Who this is for:** whoever sets Dewey up for a newsroom, after the
[setup wizard](GLOSSARY.md#setup-wizard) in [INSTALL.md](INSTALL.md#run-the-setup-wizard). Commands are
for macOS and Linux; where Windows differs, a [PowerShell](GLOSSARY.md#powershell) version follows
(*not tested by us*).

**You'll need:** a [terminal](GLOSSARY.md#terminal) open in the [Dewey folder](GLOSSARY.md#dewey-folder),
with the [virtual environment](GLOSSARY.md#virtual-environment) on
([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)). Every command below assumes that.

**Time:** indexing a large archive with local [search models](GLOSSARY.md#search-models) can take hours.

> [!NOTE]
> On every [setup](GLOSSARY.md#setup) except [Azure](STACKS.md#azure), the setup wizard's **Get your
> archive into Dewey** step does all this for you, including a 20-article [trial run](GLOSSARY.md#trial-run)
> and its checks. This page is for running [importers](GLOSSARY.md#importer) yourself, or again later.

**On this page:**

- [How importing works](#how-importing-works)
- [Choose an importer](#choose-an-importer)
- [Run an import](#run-an-import)
- [Article file format](#article-file-format)
- [Index your articles](#index-your-articles)
- [Run it again later](#run-it-again-later)
- [Not handled yet](#not-handled-yet)
- [Troubleshooting](#troubleshooting)

## How importing works

An importer copies articles from your CMS, an [RSS or Atom feed](GLOSSARY.md#rss-and-atom-feeds) or a
[CSV](GLOSSARY.md#csv) file into [JSON](GLOSSARY.md#json) files in the `data` folder.
[Indexing](GLOSSARY.md#indexing) then loads them into Dewey's search database.

- **Importers only read.** They never change anything on your site.
- **They keep published, public stories only.** Drafts, scheduled, private, password-protected and
  (where the importer can tell) members-only posts are left out. The report counts those the importer saw
  and skipped, not drafts or private posts a site's API never sends.
- **They never invent data.** A story without a byline gets no author, never a guessed one. A story with
  no headline, text, link or date is skipped and counted.
- **They need only [Python](GLOSSARY.md#python) 3.11 or newer**, not Dewey's packages.

<details><summary>Why?</summary>

- **Python 3.11 or newer:** older versions can't read some ISO dates, such as
  `2024-05-01T13:00:00.1234+00:00`, and skip those articles as undated. Apple's built-in `python3` is 3.9.
- **On Windows,** options that name a time zone, such as `--timezone America/New_York`, need the `tzdata`
  package from `requirements.txt`, so run the importers in the virtual environment. Without it, the
  WordPress importer quietly uses the site's fixed offset from UTC, which ignores daylight saving time.

</details>

To list the importers, run:

```bash
python app/import_archive.py --help
```

You should see something like:

```text
usage: import_archive.py [-h] SOURCE ...

Import a newsroom archive into Dewey's data/ folder.
```

Put an importer's name before `--help` to see its options and where to find each value:

```bash
python app/import_archive.py csv --help
```

Neither command imports anything.

## Choose an importer

Pick the row that matches where your archive lives. If two fit, the CMS's own importer reaches furthest
back: feeds often list only recent stories.

| Your archive | Importer | What you need |
|---|---|---|
| A WordPress site | `wordpress-api` | The site's address. A login only if the site closes its API. |
| A WordPress export file | `wordpress-export` | The file from wp-admin > Tools > Export. No password. |
| An RSS or Atom feed | `feed` | The feed's address, or saved feed files. Public feeds only. |
| A spreadsheet or CSV file | `csv` | A table with a header row and one row per article. |
| A Drupal site (beta) | `drupal` | Drupal 8.7 or newer, with the core JSON:API module on. |
| A Ghost site (beta) | `ghost` | A Content [API key](GLOSSARY.md#api-key) from Ghost Admin. |

> [!NOTE]
> As of November 2026, only `wordpress-api` has been run on a real newsroom site (40 public stories, no
> login). The others, and all logins and keys, were tested only against sample files and stand-in sites
> (small fake servers shaped like the real ones). **Beta** (Drupal, Ghost) means tested against sample
> exports only: check their trial run closely and [tell the maintainers](https://github.com/TeamTrace/dewey-ai/issues)
> what you find.

Where to find what each importer needs:

- **`wordpress-api`:** in a browser, your address plus `/wp-json/` (such as
  `https://www.example.com/wp-json/`) should show text starting with `{"name":`. For a closed API or
  private posts (`--include-private`), use an **Application Password** from wp-admin > Users > Profile >
  Application Passwords, not your normal password.
- **`wordpress-export`:** at Tools > Export, choose "All content" and click "Download Export File". A
  `.xml`, `.xml.gz` or `.zip` file works, or a folder of them. It works offline, even for sites that no
  longer exist.
- **`feed`:** the address ends in `/feed/` on WordPress, `/rss.xml` on Drupal and `/rss/` on Ghost, or
  give your homepage if it advertises its feed; `--file` reads saved feed files. The importer follows
  links to older pages when the feed has them.
- **`csv`:** save the spreadsheet as CSV (choose UTF-8 if offered), or use a database export.
- **`drupal` (beta):** your address plus `/jsonapi` should show JSON whose `links` list `node--article`.
  A login is needed only if JSON:API refuses anonymous visitors: an account with no extra roles, through
  the core HTTP Basic Authentication module.
- **`ghost` (beta):** in Ghost Admin (your address plus `/ghost`), open Settings > Integrations (under
  Advanced in recent versions), click "Add custom integration" and name it Dewey. Copy its API URL and
  Content API key, not the Admin API key.

<details><summary>Where these menu names come from</summary>

Source code read on GitHub, November 2026:

- WordPress: [`user-edit.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/user-edit.php)
  (Application Passwords; the button is "Add Application Password" since 6.8, "Add New Application
  Password" before), [`menu.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/menu.php)
  and [`export.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-admin/export.php)
  (Tools > Export and its buttons), [`rest-api.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-includes/rest-api.php)
  (`wp-json`), [`class-wp-rewrite.php`](https://github.com/WordPress/wordpress-develop/blob/trunk/src/wp-includes/class-wp-rewrite.php)
  (`/feed/`).
- Drupal core: the [`jsonapi`](https://github.com/drupal/drupal/tree/8.7.x/core/modules/jsonapi) module
  (first in 8.7.x, absent from 8.6.x), [`basic_auth.info.yml`](https://github.com/drupal/drupal/blob/11.x/core/modules/basic_auth/basic_auth.info.yml)
  ("HTTP Basic Authentication"), the front-page view's
  [`rss.xml`](https://github.com/drupal/drupal/blob/10.3.x/core/modules/node/config/optional/views.view.frontpage.yml)
  path.
- Ghost 6.0.0: [Integrations settings](https://github.com/TryGhost/Ghost/blob/v6.0.0/apps/admin-x-settings/src/components/settings/advanced/integrations/CustomIntegrationModal.tsx)
  (button and key names, under Advanced), [`RSSRouter.js`](https://github.com/TryGhost/Ghost/blob/v6.0.0/ghost/core/core/frontend/services/routing/RSSRouter.js)
  (`/rss/`). Ghost's [`secret.ts`](https://github.com/TryGhost/framework/blob/main/packages/security/lib/secret.ts):
  Content API keys are 26 hex characters.

Other versions may differ; spreadsheet menus are not named here because they weren't checked.

</details>

## Run an import

### Start with a trial run

With `--limit 20`, the importer stops after 20 articles and writes them to a separate `-sample` file, so
a trial doesn't replace an earlier full import. The exception is `--prefix NAME`: a trial with it writes
`NAME-0001.json`, replacing a full import that used the same `NAME`.

The `wordpress-api`, `drupal` and `ghost` importers start with your oldest stories, so their trial shows
your site's earliest ones. To see recent ones instead, add `--after` and a date to the command, such as
`--after 2026-01-01`.

1. **Set your password or key, if your source needs one** ([Passwords and keys](#passwords-and-keys)).
   Ghost always does; WordPress and Drupal only if the site closes its API to the public.
2. **Run your importer with `--limit 20`.** Copy the line for your source and paste it, but don't press
   Enter yet. First replace `https://www.example.com` with your site's address, or `YOUR_FILE` with your
   file's full [path](GLOSSARY.md#path): move back with the left arrow key, then delete it.

   On a Mac, you can then drag the file from Finder into Terminal. Elsewhere, type the full path, in
   double quotes if it has spaces.

   A WordPress site:

   ```bash
   python app/import_archive.py wordpress-api --site https://www.example.com --limit 20
   ```

   A WordPress export file:

   ```bash
   python app/import_archive.py wordpress-export --file YOUR_FILE --limit 20
   ```

   An RSS or Atom feed:

   ```bash
   python app/import_archive.py feed --url https://www.example.com/feed/ --limit 20
   ```

   A CSV file:

   ```bash
   python app/import_archive.py csv --file YOUR_FILE --limit 20
   ```

   A Drupal site:

   ```bash
   python app/import_archive.py drupal --site https://www.example.com --limit 20
   ```

   A Ghost site (your site's address or the integration's API URL), with its key already set:

   ```bash
   python app/import_archive.py ghost --url https://www.example.com --limit 20
   ```

3. **Check the report.** You should see something like:

   ```text
   Imported 20 article(s) from wordpress-api.
     Publish dates: 2024-04-11 to 2024-05-01
     Wrote 1 file(s): /Users/you/dewey-ai/data/wordpress-example-com-sample-0001.json
   ```

   If you see `Import failed:` instead, the message says what to change; nothing in `data` was changed.
4. **Open the sample file and check a few stories.** Compare the headline (`headline`), text (`content`),
   byline (`authors`), date (`publish_date`) and link (`url`) of three or four with your live site. On a
   Mac, this opens the `data` folder in Finder:

   ```bash
   open data
   ```

   Select the `-sample-0001.json` file and press Space to preview it
   ([Apple: Quick Look](https://support.apple.com/en-us/102650)), or open it in a text editor. On Linux,
   use `xdg-open data` (*unverified* on every desktop). On Windows, open `C:\Users\YOURNAME\dewey-ai\data`
   in File Explorer and the file in Notepad.
5. **If something looks wrong, change the settings and run the trial again.** Each importer's `--help`
   lists its options; a new trial replaces the last one's `-sample` files. In the wizard, choose
   **change** at **What next?**.

### Read the report

Every run ends with a report like the one above. It also counts stories with no byline, duplicates and
skipped stories, with the reason. From a CSV file:

```text
Imported 6 article(s) from csv.
  Publish dates: 2023-09-05 to 2025-01-15
  2 have no byline: they are searchable, but author filters won't find them. No placeholder author was added.
  1 skipped: status 'draft' is not published (list it in --published-values to import it)
```

The CSV importer also prints its column mapping first: check that each Dewey field comes from the right
column.

After a trial run, the setup wizard also checks the articles it kept, because a wrongly mapped field
imports without error. If all is well:

```text
  Checked 20 article(s): bylines, ids, links, dates and article length all look usable.
```

Otherwise it marks each finding **needs a look** or **worth knowing** and says what to change. Running an
importer yourself, check the same things by eye:

| The wizard says | Marked | What to do |
|---|---|---|
| None of the 20 articles has a byline. | worth knowing | [Author filters](GLOSSARY.md#byline-filter) will match nothing. Try `--byline-field` or a WordPress export file. |
| All 20 articles are credited to 'Jane Doe'. | needs a look | The byline isn't per article (Yoast's `yoast_head_json` does this). Use another field or an export file. |
| 2 byline(s) hold more than one name in one string | worth knowing | Filters match whole bylines ([the rule](MCP.md#the-two-tools)): split them (CSV: `--authors-sep`). |
| 2 article id(s) appear more than once | needs a look | Indexing keeps one per id. Give each source its own `--prefix`. |
| 3 of 20 articles have no usable url. | worth knowing | They can be found but not cited. Check the importer reads the public link. |
| Every article has the publish date '2024-05-01'. | worth knowing | Date filters can't narrow anything. Use the publish date, not an import or edit date. |
| Bylines came from each post's WordPress author field. | worth knowing | That may be who posted the story, not who wrote it. Compare a few with the live site. |
| The median article is 74 characters long. | needs a look | Under 400 characters is an excerpt. Use the field with the full text. |

### Import everything

When the trial looks right, run the same command without `--limit 20` (in the wizard: **full** at **What
next?**). For a WordPress site:

```bash
python app/import_archive.py wordpress-api --site https://www.example.com
```

Leave the window open until the report appears. To stop early, press Control+C; you'll see
`Cancelled; nothing was changed in the output folder.`

**Where the files go.** The importer writes `data/NAME-0001.json`, `data/NAME-0002.json` and so on, 1,000
articles per file; `NAME` comes from the source, such as `wordpress-example-com`. Running the same import
again replaces them only once the new run has finished, so a failed or empty run changes nothing.

<details><summary>Big sites, a year at a time</summary>

The WordPress, Drupal and Ghost importers take `--after` and `--before` dates (`YYYY-MM-DD`). `--after`
includes that day; `--before` stops the day before. Each window gets its own files, so windows don't
replace each other:

```bash
python app/import_archive.py wordpress-api --site https://www.example.com --after 2023-01-01 --before 2024-01-01
```

</details>

**Delete the sample files.** Indexing them too doesn't duplicate articles (they have the same ids), but it
[embeds](GLOSSARY.md#embedding) them again, which costs money with hosted [Voyage AI](GLOSSARY.md#voyage-ai).
The wizard offers to remove them. On macOS and Linux:

```bash
rm data/*-sample-*.json
```

On Windows (PowerShell, *not tested by us*):

```powershell
Remove-Item data\*-sample-*.json
```

### Passwords and keys

Passwords and keys go in [environment variables](GLOSSARY.md#environment-variable), not on the command
line, where your [shell](GLOSSARY.md#shell) history and the list of running programs could show them. The
importers read these:

| Importer | Username | Password or key |
|---|---|---|
| `wordpress-api` | `WORDPRESS_USER` | `WORDPRESS_APP_PASSWORD` |
| `drupal` | `DRUPAL_USER` | `DRUPAL_PASSWORD` |
| `ghost` | (none) | `GHOST_CONTENT_API_KEY` |

The setup wizard asks for them hidden and never writes them to the [.env file](GLOSSARY.md#env-file).

**On macOS and Linux (zsh or bash),** for a Ghost key:

1. **Set the key.** Paste this line and press Enter:

   ```bash
   read -rs GHOST_CONTENT_API_KEY && export GHOST_CONTENT_API_KEY
   ```

   The terminal now waits. Paste your key and press Enter. Nothing appears, not even a new line; that's
   expected.
2. **Check that it was stored.** This prints how many characters you pasted, without showing them:

   ```bash
   echo "${#GHOST_CONTENT_API_KEY}"
   ```

   You should see:

   ```text
   26
   ```

   A Ghost Content API key has 26 characters. `0` means nothing was stored: do step 1 again.
3. **Run the importer in the same window.** The key lasts until you close the window and isn't saved
   anywhere. To forget it sooner, run `unset GHOST_CONTENT_API_KEY`.

   ```bash
   python app/import_archive.py ghost --url https://www.example.com --limit 20
   ```

For WordPress, set the username, then the Application Password the same way. A username isn't secret,
but this line stays in your shell history:

```bash
export WORDPRESS_USER='YOUR_USERNAME'
```

```bash
read -rs WORDPRESS_APP_PASSWORD && export WORDPRESS_APP_PASSWORD
```

For Drupal:

```bash
export DRUPAL_USER='YOUR_USERNAME'
```

```bash
read -rs DRUPAL_PASSWORD && export DRUPAL_PASSWORD
```

Then run the importer in the same window.

**On Windows (PowerShell, *not tested by us*),** paste this line and press Enter. Then paste the key at
the `GHOST_CONTENT_API_KEY:` prompt and press Enter; you'll see asterisks.

```powershell
$env:GHOST_CONTENT_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'GHOST_CONTENT_API_KEY')).Password
```

Then, in the same window:

```powershell
python app/import_archive.py ghost --url 'https://www.example.com' --limit 20
```

For WordPress:

```powershell
$env:WORDPRESS_USER = 'YOUR_USERNAME'
```

```powershell
$env:WORDPRESS_APP_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'WORDPRESS_APP_PASSWORD')).Password
```

For Drupal:

```powershell
$env:DRUPAL_USER = 'YOUR_USERNAME'
```

```powershell
$env:DRUPAL_PASSWORD = [System.Net.NetworkCredential]::new('', (Read-Host -AsSecureString 'DRUPAL_PASSWORD')).Password
```

These lines work in Windows PowerShell 5.1 and PowerShell 7. A value lasts until you close the window and
isn't saved. To forget it sooner:

```powershell
Remove-Item -Path Env:\GHOST_CONTENT_API_KEY
```

<details><summary>Sources</summary>

Checked November 2026:

- zsh's `read` options `-r` and `-s`: [builtins.yo](https://github.com/zsh-users/zsh/blob/zsh-5.9/Doc/Zsh/builtins.yo)
  (zsh 5.9); bash's: the [GNU Bash manual](https://www.gnu.org/software/bash/manual/bash.txt).
- PowerShell's [Read-Host](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/5.1/Microsoft.PowerShell.Utility/Read-Host.md):
  `-AsSecureString` shows asterisks; `-MaskInput` needs PowerShell 7.1.
- [about_Environment_Variables](https://github.com/MicrosoftDocs/PowerShell-Docs/blob/main/reference/5.1/Microsoft.PowerShell.Core/About/about_Environment_Variables.md):
  a value set this way lasts for this window only, and programs started from it see it.
- .NET's [NetworkCredential](https://github.com/dotnet/dotnet-api-docs/blob/main/xml/System.Net/NetworkCredential.xml)
  turns the hidden input back into text.

</details>

## Article file format

The importers write these files for you. Each file in `data` holds one article (a JSON object) or a list
of them, and Dewey reads every file whose name ends in `.json`. A valid file with one article:

```json
[
  {
    "id": "example-2024-05-01-storm",
    "headline": "Storm knocks out power to 10,000 homes",
    "content": "Crews worked overnight to restore power after Tuesday's storm.\n\nThe utility said most homes would have power again by Friday.",
    "url": "https://www.example.com/2024/05/01/storm-power/",
    "authors": ["Jane Doe"],
    "publish_date": "2024-05-01T13:00:00Z",
    "publisher": "Your Newsroom"
  }
]
```

In `content`, `\n\n` is how JSON writes a blank line between paragraphs. Some fields differ on the `azure`
[stack](GLOSSARY.md#stack) (Microsoft's Azure OpenAI and Azure AI Search); every other setup stores
articles in [Postgres](GLOSSARY.md#postgres).

| Field | Required | What goes in it |
|---|---|---|
| `id` | No | A unique id. Postgres makes a missing one from the URL; on `azure`, it only names the uploaded file. |
| `headline` | Yes | The article's title. |
| `content` | Yes | The full text, as plain text. Dewey searches this. |
| `url` | Yes | The public address. Answers cite it, so it must work. |
| `authors` | Yes | Bylines, one name per string: `["Jane Doe"]`. May be empty (`[]`) on Postgres, but author filters won't find those; `azure` skips them. |
| `publish_date` | Yes | An [ISO 8601](GLOSSARY.md#iso-8601-date) date and time: `2024-05-01T13:00:00Z` is 1 p.m. UTC on May 1, 2024. |
| `publisher` | No | Postgres only: the [publisher](GLOSSARY.md#publisher). Omit it for your own articles (the indexer uses `NEWSROOM_NAME`); set it for articles from elsewhere, by hand or with an importer's `--publisher`. |

> [!IMPORTANT]
> Articles without a `publisher` are labelled with `NEWSROOM_NAME` when you index, so answers present
> them as your own reporting. For a source that isn't yours (a partner's feed, a wire service), add
> `--publisher` to the import command, with the outlet's name in quotes:
>
> ```bash
> python app/import_archive.py feed --url https://PARTNER.EXAMPLE/feed/ --publisher "PARTNER NAME"
> ```
>
> The indexer stops with an error if an article has no `publisher` and `NEWSROOM_NAME` isn't set
> (step 1 of [Index your articles](#index-your-articles)). Earlier versions labelled such articles as
> The Philadelphia Inquirer's; if you indexed that way, set the name and index again.

<details><summary>Making files yourself</summary>

Put them in `data`, saved as UTF-8, and choose **json** at the wizard's **Where is your archive?**
question. `data/test.json.example` is a one-article sample; the indexer ignores it, because its name
doesn't end in `.json`. To check that a file is valid JSON, run this with its name:

```bash
python -m json.tool data/YOUR_FILE.json
```

It prints the file back if it's valid, or says where it's broken, such as `line 5 column 3`.

</details>

## Index your articles

These steps are for every setup except Azure: there, run `python app/setup.py` instead
([AZURE.md](AZURE.md#build-the-search-index)). On a server with [Docker Compose](GLOSSARY.md#docker-compose),
the indexer runs in a container ([DEPLOYMENT.md](DEPLOYMENT.md#a-one-server-with-docker-compose)).

> [!IMPORTANT]
> The indexer labels your own stories with `NEWSROOM_NAME` from `.env`, and stops with an error if it
> isn't set. Check it first (step 1). Earlier versions used The Philadelphia Inquirer's name instead:
> if you indexed that way, set the name and index again.

1. **Check that `.env` names your newsroom.** This prints the setting, if it's there:

   ```bash
   grep NEWSROOM_NAME .env
   ```

   You should see something like:

   ```text
   NEWSROOM_NAME='Your Newsroom'
   ```

   On Windows (PowerShell):

   ```powershell
   Select-String -Path .env -Pattern 'NEWSROOM_NAME'
   ```

   If it prints nothing, or only a line with `#` before `NEWSROOM_NAME` (a comment, which Dewey ignores),
   run `python install.py` again and fill in **Newsroom name**
   ([INSTALL.md](INSTALL.md#change-your-settings)).
2. **Run the indexer.** This is the slow step; run it with the same `.env` that Dewey runs with. With
   local search models, the first run also downloads the embedding model (about 0.7 GB) from
   [Hugging Face](GLOSSARY.md#hugging-face) ([STACKS.md](STACKS.md#local-search-models)).

   ```bash
   python app/setup_postgres.py
   ```

   You should see something like:

   ```text
   🔧 Embeddings: local (voyage-4-nano, 1024 dims); reranking: none
   ...
   🎉 Indexed 1000 article(s) as 4585 chunk(s).
   ```

   The first line names your search models. Leave the window open until the last line appears.
3. **Check the last line.** Its article count should match what you imported. A `Skipping document` line
   means an article lacks a required field ([Article file format](#article-file-format)).
4. **Try it.** Ask questions you already know the answers to, on the [demo page](GLOSSARY.md#demo-page)
   ([how](INSTALL.md#try-the-demo-page)) or in your AI tool ([how](INSTALL.md#connect-an-ai-tool)).

**How long it takes.** With local search models, embedding runs on the processor (CPU) unless PyTorch can use an NVIDIA card. It took about 9
minutes per 1,000 articles of about 1,300 words (measured once on an Apple M3 Max with 36 GB, November
2026; yours will differ). At that rate, 10,000 articles take about an hour and a half, plus database
writes. On a Mac, `caffeinate -i` in front of the command keeps the Mac awake:

```bash
caffeinate -i python app/setup_postgres.py
```

**With Voyage AI,** the indexer sends your whole archive to Voyage
([what the hosted providers receive](STACKS.md#what-the-hosted-providers-receive)), which bills per token
([prices and an estimate](STACKS.md#claude-with-voyage-ai)); indexing time with it wasn't measured. Add
a payment method to your Voyage account first: without one, the free limits stop the indexer after a few
articles (our reading of [Voyage's limits](https://www.mongodb.com/docs/voyageai/api-reference/overview/),
checked November 2026; not tested).

**Running it again is safe.** Each run updates and adds articles but never removes any. It embeds every
article in `data` again: time with local models, money with Voyage AI. If a run stops partway, the
finished articles stay indexed. Run the indexer again:

- after you add or re-import articles;
- after an update, when the update notes say so;
- when Dewey says the database "was set up by an earlier version of Dewey".

## Run it again later

Nothing runs on a schedule. To pick up new stories, import again, then index again.

1. **Open a terminal in the Dewey folder, with the virtual environment on**
   ([how](INSTALL.md#turn-it-on-in-every-new-terminal-window)).
2. **Set any password or key again** ([Passwords and keys](#passwords-and-keys)): it doesn't carry over.
3. **Run the same import command.** The setup wizard printed it after the line that starts
   `To run this import`, with secrets only as `read -rs` lines.
4. **Run the indexer again** ([Index your articles](#index-your-articles)).

Things to know:

- **Feeds** often list only recent stories, so older ones drop out of `data` when the files are replaced.
  They stay in Dewey's database, but a fresh index built from `data` would lose them. To build up an
  archive from a feed, give each run its own `--prefix` (a story in two runs is indexed once):

  ```bash
  python app/import_archive.py feed --url https://www.example.com/feed/ --prefix feed-example-2026-11
  ```

- **Big sites:** `--after` with a recent date fetches only recent stories, in files next to the earlier
  ones; articles in both are indexed once.
- **On Windows,** the wizard prints these commands for bash (a known problem: [#25](https://github.com/TeamTrace/dewey-ai/issues/25)). In PowerShell, put web
  addresses in single quotes (an unquoted `&` breaks the command), and set secrets as in
  [Passwords and keys](#passwords-and-keys).
- **Deleted stories** stay in Dewey's index: see [Not handled yet](#not-handled-yet).

## Not handled yet

- **Incremental re-sync.** There is no "fetch only what changed since last time": each import reads the
  whole source again, and each index run embeds every article again.
- **Stories deleted or unpublished in the CMS.** Re-importing drops them from `data`, but not from Dewey's
  database: indexing only adds and updates. To remove them, index into a fresh database, or delete them by
  id in Postgres (`DELETE FROM articles WHERE id = 'ARTICLE_ID'`; their chunks go with them).
- **Text the public can't see.** Without a login, importers get only what an anonymous visitor gets. So
  text a paywall or membership plugin hides from the WordPress API is missing; a paywall that only works
  in the reader's browser hides nothing. Ghost members-only posts are skipped by default, and many feeds
  carry only excerpts (`--require-full-text` skips those). Check a few paywalled stories in a trial run (with
  `wordpress-api` or `ghost`, add `--after` to reach recent ones).
- **Other CMSs** (Arc XP, Brightspot, ...): no dedicated importer yet. Use an RSS/Atom feed or a CSV
  export, if the CMS offers one.
- **Other publishers' articles.** No importer writes `publisher`, so everything imported is labelled as
  your newsroom's own. For a partner's stories or an outside report, add `publisher` to its file by hand
  before indexing ([Article file format](#article-file-format)).

## Troubleshooting

For problems with Python, the virtual environment, `.env` settings or the database, such as
`ModuleNotFoundError` or `Embedding settings don't match this database`, see
[INSTALL.md](INSTALL.md#troubleshooting).

**While importing:**

| You see | What to do |
|---|---|
| `zsh: no matches found:` and a web address | Put the address in single quotes: `--url 'https://www.example.com/?feed=rss2'`. |
| `Import failed: --file '...': no such file.` (or `Export file not found`, `Feed file not found`) | Use the file's full path (on a Mac, drag the file into Terminal). |
| `Dates in column 'Date Published' are written like '5/1/2024 1:05 PM'` | Add the `--date-format` the message prints, such as `--date-format '%m/%d/%Y %I:%M %p'` for US dates. |
| The same date message, in the wizard | Choose **change** and type it at **Extra options**. On Windows, type it without quotes; options with spaces work only if you run the importer yourself. |
| `The WordPress REST API at ... refused the request` or `Couldn't find a WordPress REST API` | Set an Application Password ([Passwords and keys](#passwords-and-keys)), or use a `wordpress-export` file. |
| `No Content API key.` (Ghost) | Set `GHOST_CONTENT_API_KEY` in this window ([Passwords and keys](#passwords-and-keys)). |
| `That looks like an Admin API key (it contains a colon).` | Use Ghost's Content API key: 26 hex characters (letters a-f and digits), no colon. |
| `No JSON:API at ... (HTTP 404)` (Drupal) | An administrator turns JSON:API on at Manage > Extend. For a headless site, add `--api-url`. |
| `CERTIFICATE_VERIFY_FAILED`, then `Import failed: Couldn't reach` | On a Mac with Python from python.org, double-click **Install Certificates.command** in Applications > Python 3.13 ([INSTALL.md](INSTALL.md#get-your-mac-ready)). |

**While indexing:**

| You see | What to do |
|---|---|
| `article id(s) appear in more than one document, on different sites` | Nothing was embedded. Keep the right file; remove or re-import the other. For CSV files from two CMSs, give each its own `--id-prefix`. |
| A rate-limit error with Voyage AI (exact text *unverified*) | Add a payment method to your Voyage account, wait a minute and run the indexer again. |

**Where to go next:**

- Ask Dewey questions on the demo page: [INSTALL.md](INSTALL.md#try-the-demo-page).
- Connect your AI tool: [INSTALL.md](INSTALL.md#connect-an-ai-tool), then [MCP.md](MCP.md).
- Run Dewey for a team: [DEPLOYMENT.md](DEPLOYMENT.md).
- Back to the [README](../README.md).
