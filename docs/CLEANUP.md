# Cleaning up Portkeeper

Activity → **Clear activity & logs** clears completed job logs, saved model prompt/reply records, and old notifications, and hides earlier activity entries. Active work remains visible. Package manifests, launch/playtest results, preserved releases, current instructions and game files remain intact. Historical import/test events are hidden by a cutoff; their validation evidence is retained.

A game's Advanced page has the same cleanup action for that game, **Clear build instructions**, and **Remove game from Portkeeper**. Clearing instructions disables automatic builds, publication and platform drafting, retains existing downloads, and saves the previous instructions as a restorable revision. Removing a game removes its app records, including associated package listings, without deleting files on disk or changing RomM. Downloads and captured sources can be recovered from the backup's recorded paths; removing a game does not reclaim their disk space. Package retention remains the separate disk-cleanup control.

Before every cleanup, Portkeeper saves a consistent, compacted SQLite snapshot and
allowlisted configuration/recipe metadata in `/data/state-backups/state-<id>/`.
`manifest.json` records the action and file checksums. Drafts are retained under
`drafts/` in the snapshot; raw worker logs and game payloads are never copied into it.
`PORTKEEPER_BACKUP_KEEP=3` retains the latest three completed, unchanged managed
snapshots (configurable 1–20). Legacy `maintenance-backups` and migration copies are
left for explicit review. These backups can contain credentials and diagnostic
prompts; keep them private. Recovery requires restoring records/configuration with
the service stopped; there is no in-app restore-all action. See [storage and backup
coverage](STORAGE.md), including material that recipe JSON alone cannot reproduce.

**Previous instructions** shows the current revision and number of saved copies. Each previous revision includes its date, packaging method, summary, platforms, game-input and step counts, full JSON, and a review-before-restore action. Games with no previous revisions show an explanation instead of an empty dropdown. Clearing previous revisions retains the current instructions and creates a local backup first.
