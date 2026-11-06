# Watchkeeper

Keeps your watched history, resume points and ratings through a library rebuild.

If you find this useful, [buy me a coffee](https://ko-fi.com/mikesilvo) ☕

Watchkeeper records play counts, last played times, resume points and user ratings as they change, in one file of its own. It matches items by unique id rather than file path, so after a rebuild, a rescrape or a move to another drive it can write your data back into the library without touching the freshly scraped metadata. It runs as a service, so there is nothing to remember to run.

## What it does

- Records your data as it changes, from the events Kodi sends, with a sweep every six hours to catch ratings, which Kodi does not announce
- Fills the library back in on its own once a rebuild settles, only where Kodi holds nothing
- Restores everything from Programs, or one movie or show from its context menu
- Undoes any restore, including one it made by itself
- Moves your saved data to another install, and merges one back in
- Keeps several installs in sync through a shared folder
- Exports a readable list of everything saved

## Where your data lives

`userdata/addon_data/service.watchkeeper/watchkeeper.db`, per Kodi profile. A few dated snapshots sit beside it as insurance against the file itself going bad.

## Restoring

After a rebuild, Watchkeeper fills the library back in by itself once the scan has finished and nothing has played for a few minutes. It only fills in what the library is missing: anything the library already holds is left alone, and a play count is never lowered.

**Restore my data to the library** in Programs does the same for the whole library on demand.

The **Watchkeeper** item in a movie's or TV show's context menu restores that one title, or a show and every episode under it. It then offers any earlier state the title was in, listed by date. Putting an earlier state back replaces what the library holds for that title. The item is on by default, under **Show the context menu item** in the addon's settings.

Any restore can be undone, including one Watchkeeper made by itself. **Undo a restore** in Programs lists them newest first and puts back what the library held before the one you pick.

## Moving between installs

To carry your data to another install, use **Export your saved data to a file** from Programs or the addon's settings, then **Import saved data from a file** on the other machine. An import merges rather than replaces: where both machines changed an item, the newer change is kept and put into the library.

To keep several installs in sync, turn on **Sync through a shared folder** under Export / Import in the addon's settings on each one, and pick the same folder. It can be any folder every install can write to, such as a network share. Each install keeps a file of its own there.

Play counts, last played times, resume points and ratings all travel. A change made on one install reaches the others at their next startup, or within about a minute while they run. Changes from other installs wait until no video is playing. Where two installs changed the same item, the latest change wins.

## Limits worth knowing

- An item with no unique id, such as a home video, cannot be matched and is not saved
- One resume point is kept per title. If you keep two copies of a film, the resume point belongs to whichever you played last, and it is only ever written back to that same file
- Watched state and ratings belong to the title, so watching one copy marks every copy watched
- Each Kodi install keeps its own file. Sharing one library between machines means running Watchkeeper on each of them

## Reporting a problem

Turn on **Verbose logging** in the addon's settings, reproduce the problem, then attach your Kodi log.
