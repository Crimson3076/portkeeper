"""Package decisions, conservative retention, and the local notification inbox."""

import hashlib
import json
import re
import shutil
from pathlib import Path

from .safety import Invalid, checksum, confined, verify_tree
from .storage import now


VALIDATION_LEVELS = ("packaged", "launch_checked", "user_confirmed_playable", "failed")
NOTIFICATION_KINDS = ("ready", "build_failed", "needs_input", "upstream_unavailable", "needs_review", "new_platform")


def _component(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise Invalid("Invalid stored package identifier")
    return value


class Lifecycle:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.lock = engine.lock

    def pin(self, artifact_id, pinned=True):
        if not isinstance(pinned, bool):
            raise Invalid("pinned must be true or false")
        with self.lock:
            return self.store.update("artifacts", artifact_id, pinned=pinned)

    def validate(self, artifact_id, level, notes="", evidence=None):
        """Record a review; never infer gameplay from successful packaging."""
        if level not in VALIDATION_LEVELS:
            raise Invalid("Choose a valid package test result")
        if not isinstance(notes, str) or len(notes) > 4000:
            raise Invalid("Validation notes must be text up to 4000 characters")
        if evidence is not None and not isinstance(evidence, (str, dict)):
            raise Invalid("Validation evidence must be text or an object")
        try:
            encoded = json.dumps(evidence)
        except (TypeError, ValueError) as error:
            raise Invalid("Validation evidence must be serializable") from error
        if len(encoded) > 16000:
            raise Invalid("Validation evidence exceeds its size limit")
        if level != "packaged" and not notes.strip() and not evidence:
            raise Invalid("Describe the launch check or gameplay confirmation before advancing validation")
        with self.lock:
            artifact = self.store.get("artifacts", artifact_id)
            if artifact.get("demo") and level != "packaged":
                raise Invalid("Synthetic demo packages cannot be marked launch checked or playable")
            self._archive_directory(artifact, verify_hash=True)
            event = {"level": level, "notes": notes.strip(), "evidence": evidence, "recorded_at": now()}
            history = artifact.get("validation_history", []) + [event]
            if level in ("packaged", "failed"):
                project = self.store.get("projects", artifact['project_id'])
                if project.get('known_good', {}).get(artifact['target']) == artifact_id:
                    self.clear_known_good(project['id'], artifact['target'])
            return self.store.update("artifacts", artifact_id, validation_level=level,
                                     validation=event, validation_history=history[-100:])

    def set_known_good(self, artifact_id):
        with self.lock:
            artifact = self.store.get("artifacts", artifact_id)
            level = artifact.get("validation_level", "packaged")
            if level not in ("launch_checked", "user_confirmed_playable"):
                raise Invalid("Record a launch check or gameplay confirmation before selecting a known-good package")
            self._archive_directory(artifact, verify_hash=True)
            project = self.store.get("projects", artifact["project_id"])
            pointers = dict(project.get("known_good", {}))
            pointers[artifact["target"]] = artifact_id
            self.store.update("projects", project["id"], known_good=pointers)
            return artifact

    def clear_known_good(self, project_id, target):
        with self.lock:
            project = self.store.get("projects", project_id)
            pointers = dict(project.get("known_good", {}))
            pointers.pop(target, None)
            return self.store.update("projects", project_id, known_good=pointers)

    def retention(self, project_id, keep_last=None):
        """Return a deletion preview. None means retain every package."""
        if keep_last is not None and (isinstance(keep_last, bool) or not isinstance(keep_last, int)
                                      or not 1 <= keep_last <= 1000):
            raise Invalid("keep_last must be null (keep everything) or a number from 1 to 1000")
        with self.lock:
            project = self.store.get("projects", project_id)
            artifacts = [a for a in self.store.all("artifacts") if a["project_id"] == project_id]
            ordered = sorted(artifacts, key=lambda a: (a.get("created_at", ""), a["id"]), reverse=True)
            seen = {}
            protected = []
            candidates = []
            good = set(project.get("known_good", {}).values())
            for artifact in ordered:
                target = artifact["target"]
                seen[target] = seen.get(target, 0) + 1
                reasons = []
                if keep_last is None:
                    reasons.append("keep_all")
                elif seen[target] <= keep_last:
                    reasons.append("within_retention")
                if seen[target] == 1:
                    reasons.append("latest")
                if artifact.get("pinned"):
                    reasons.append("pinned")
                if artifact.get("published"):
                    reasons.append("published")
                if artifact["id"] in good:
                    reasons.append("known_good")
                entry = {"id": artifact["id"], "target": target, "version": artifact.get("version"),
                         "filename": artifact.get("filename"), "size": artifact.get("size", 0)}
                if reasons:
                    protected.append({**entry, "reasons": reasons})
                else:
                    candidates.append(entry)
            return {"project_id": project_id, "keep_last": keep_last, "scope": "packages_only",
                    "candidates": candidates, "protected": protected,
                    "reclaim_bytes": sum(a["size"] for a in candidates), "sources_preserved": True}

    def _archive_directory(self, artifact, verify_hash=False):
        project_id = _component(artifact["project_id"])
        artifact_id = _component(artifact["id"])
        root = confined(self.engine.archive_root, f"packages/{project_id}/{artifact_id}")
        if not root.is_dir():
            raise Invalid("Package archive directory is missing")
        archive = Path(artifact["file"])
        if archive.parent != root or archive.name != artifact.get("filename", archive.name):
            raise Invalid("Stored package path does not match its archive directory")
        if archive.is_symlink() or not archive.is_file():
            raise Invalid("Package archive is missing or is a symlink")
        verify_tree(root)
        if verify_hash and checksum(archive) != artifact["sha256"]:
            raise Invalid("Package checksum changed; validation cannot proceed")
        return root

    def apply_retention(self, project_id, keep_last=None):
        with self.lock:
            if project_id in self.engine.busy:
                raise Invalid("Wait for the project's running job before cleaning packages")
            plan = self.retention(project_id, keep_last)
            # Validate every deletion first so malformed later records do not cause partial cleanup.
            checked = [(entry["id"], self._archive_directory(self.store.get("artifacts", entry["id"])))
                       for entry in plan["candidates"]]
            removed = []
            for artifact_id, path in checked:
                shutil.rmtree(path)
                self.store.delete("artifacts", artifact_id)
                removed.append(artifact_id)
            return {**plan, "removed": removed}

    def notify(self, project_id, kind, message, artifactid=None, jobid=None, event_key=None):
        if kind not in NOTIFICATION_KINDS:
            raise Invalid("Unknown notification kind")
        if not isinstance(message, str) or not message.strip() or len(message) > 1000:
            raise Invalid("Notification message must be text up to 1000 characters")
        with self.lock:
            project = self.store.get("projects", project_id)
            # Identical events remain read/dismissed even when a worker retries reporting them.
            key = event_key or artifactid or jobid or message.strip()
            event_id = hashlib.sha256(json.dumps([project_id, kind, key], separators=(",", ":")).encode()).hexdigest()
            try:
                return self.store.get("notifications", event_id)
            except KeyError:
                pass
            return self.store.put("notifications", {
                "id": event_id, "project_id": project_id, "project_name": project["name"], "kind": kind,
                "message": message.strip(), "artifact_id": artifactid, "job_id": jobid,
                "created_at": now(), "read": False, "dismissed": False})

    def notifications(self, project_id=None, unread_only=False):
        records = [n for n in self.store.all("notifications") if not n.get("dismissed")
                   and (project_id is None or n["project_id"] == project_id)
                   and (not unread_only or not n.get("read"))]
        return sorted(records, key=lambda n: (n.get("created_at", ""), n["id"]), reverse=True)[:200]

    def mark_read(self, notification_id, read=True):
        if not isinstance(read, bool):
            raise Invalid("read must be true or false")
        with self.lock:
            return self.store.update("notifications", notification_id, read=read)

    def dismiss(self, notification_id):
        with self.lock:
            return self.store.update("notifications", notification_id, read=True, dismissed=True)
