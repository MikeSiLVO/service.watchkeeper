# Watchkeeper

Keeps your watched history, resume points and ratings through a library rebuild.

Watchkeeper records play counts, last played times, resume points and user ratings as they change,
in one file of its own. It matches items by unique id rather than file path, so after a rebuild, a
rescrape or a move to another drive it can write your data back into the library without touching
the freshly scraped metadata. It runs as a service, so there is nothing to remember to run.

## What it does

- Records your data as it changes, from the events Kodi sends, with a sweep every six hours to
  catch ratings, which Kodi does not announce
- Fills the library back in on its own once a rebuild settles, only where Kodi holds nothing
- Restores everything from Programs, or one movie or show from its context menu
- Undoes any restore, including one it made by itself
- Moves your saved data to another install, and merges one back in
- Exports a readable list of everything saved

## Where your data lives

`userdata/addon_data/service.watchkeeper/watchkeeper.db`, per Kodi profile. A few dated snapshots
sit beside it as insurance against the file itself going bad.

To carry your data to another install, use **Export your saved data to a file** in the addon's
settings and **Import saved data from a file** on the other machine. An import merges rather than
replaces: where both machines changed an item, the newer change is kept.

## Limits worth knowing

- An item with no unique id, such as a home video, cannot be matched and is not saved
- One resume point is kept per title. If you keep two copies of a film, the resume point belongs to
  whichever you played last, and it is only ever written back to that same file
- Watched state and ratings belong to the title, so watching one copy marks every copy watched
- Each Kodi install keeps its own file. Sharing one library between machines means running
  Watchkeeper on each of them

## Reporting a problem

Turn on **Verbose logging** in the addon's settings, reproduce the problem, then attach your Kodi
log. Without it the addon logs at debug level and a normal log shows nothing.
