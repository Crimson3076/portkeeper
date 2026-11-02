"""Publish packages into RomM through its API and confirm that RomM indexed them.

A local library copy is a separate action. It reaches RomM only when that folder is
RomM's own platform folder, which this module does not assume.
"""
from datetime import datetime, timezone
from pathlib import Path

from .safety import Invalid, checksum
from .storage import now


class Publisher:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def queue(self, artifact_id):
        with self.engine.lock:
            artifact = self.store.get("artifacts", artifact_id)
            if artifact.get("demo"):
                raise Invalid("Synthetic demo packages are not published to RomM")
            state = (artifact.get("romm") or {}).get("status")
            if state == "uploading":
                raise Invalid("This package is already being uploaded to RomM")
            if state == "in_romm":
                raise Invalid("This package is already in RomM")
            if not self.engine.romm.public_config().get("configured"):
                raise Invalid("Connect RomM in Settings before publishing")
            project = self.store.get("projects", artifact["project_id"])
            job = self.engine.job(project, artifact["target"], artifact["version"])
            job = self.store.update("jobs", job["id"], kind="publish", artifact_id=artifact_id, stage="publishing")
            self.store.update("artifacts", artifact_id, romm={"status": "uploading", "queued_at": now()})
        self.engine.executor.submit(self.run_job, job["id"], artifact_id)
        return {"job": job}

    def run_job(self, job_id, artifact_id):
        log = lambda message: self.store.append_log(job_id, message)
        try:
            self.store.update("jobs", job_id, status="building")
            record = self.publish(artifact_id, log, lambda stage, message: self.engine.stage(job_id, stage, message))
            self.store.update("jobs", job_id, status="ready", finished_at=now())
            self.engine.stage(job_id, "complete", "Uploaded to RomM" if record["status"] != "in_romm" else "Package is in RomM")
        except Exception as error:
            log(str(error))
            self.store.update("jobs", job_id, status="failed", error=str(error), finished_at=now())

    def publish(self, artifact_id, log, stage=None):
        """Upload synchronously. Used by manual jobs and automatic publication after a build."""
        stage = stage or (lambda _s, message: log(message))
        artifact = self.store.get("artifacts", artifact_id)
        romm = self.engine.romm
        try:
            stage("publishing", "Checking the package before upload")
            if checksum(Path(artifact["file"])) != artifact["sha256"]:
                raise Invalid("Package checksum changed; refusing to publish")
            platform = romm.platform_for(artifact["target"])
            base = artifact["filename"][:-4]
            record, filename = None, None
            for revision in range(1, 21):
                candidate = base + ("" if revision == 1 else f"-r{revision}") + ".zip"
                existing = romm.find_file(platform["id"], candidate)
                if existing:
                    sizes = {f.get("size") for f in existing.get("files", []) if f.get("name") == candidate}
                    if artifact["size"] in sizes and existing.get("files") and len(existing["files"]) == 1:
                        log(f"RomM already has {candidate} with the same size; linking it instead of uploading again")
                        record = existing
                        filename = candidate
                        break
                    continue
                try:
                    stage("publishing", f"Uploading {candidate} to RomM ({platform['name'] or platform['fs_slug']})")
                    log(f"Uploading {candidate} to RomM platform {platform['fs_slug']} ({artifact['size']} bytes)")
                    last = {"percent": -10}

                    def progress(done, total):
                        percent = done * 100 // total
                        if percent >= last["percent"] + 10 or done == total:
                            last["percent"] = percent
                            log(f"Uploaded {percent}% ({done}/{total} chunks)")
                    romm.upload(artifact["file"], platform["id"], candidate, progress, stop=self.engine.stop)
                    filename = candidate
                    break
                except FileExistsError:
                    log(f"{candidate} already exists in RomM's folder but is not indexed yet or differs; trying the next revision name")
                    continue
            if not filename:
                raise Invalid("RomM already has 20 differing files with this package name")
            status = {"status": "in_romm", "rom_id": record["id"], "indexed_at": now()} if record else {"status": "awaiting_scan"}
            result = {**status, "filename": filename, "platform_id": platform["id"], "platform": platform["fs_slug"],
                      "server": romm.public_config()["url"], "uploaded_at": now()}
            self.store.update("artifacts", artifact_id, romm=result)
            self.engine.match_schedule.kick(f"{filename} was published")
            if not record:
                log("Upload complete. RomM's filesystem watcher adds the file to the library after its rescan delay; Portkeeper checks on its RomM schedule.")
                self.check(artifact_id)
            return self.store.get("artifacts", artifact_id)["romm"]
        except Exception as error:
            self.store.update("artifacts", artifact_id, romm={"status": "failed", "error": str(error)[:500], "failed_at": now()})
            raise

    def check(self, artifact_id):
        artifact = self.store.get("artifacts", artifact_id)
        state = artifact.get("romm") or {}
        if state.get("status") != "awaiting_scan":
            return state
        try:
            record = self.engine.romm.find_file(state["platform_id"], state["filename"])
        except Invalid as error:
            return {**state, "last_error": str(error)}
        if record:
            state = {**state, "status": "in_romm", "rom_id": record["id"], "indexed_at": now()}
            self.store.update("artifacts", artifact_id, romm=state)
            self.engine.notify(artifact["project_id"], "ready", f"{artifact['project_name']} {artifact['version']} ({artifact['target']}) is now in RomM",
                               artifactid=artifact_id, event_key="romm:" + artifact_id)
            self.engine.reviews.cache.clear()
        else:
            uploaded = datetime.fromisoformat(state["uploaded_at"])
            waited = (datetime.now(timezone.utc) - uploaded).total_seconds()
            if waited > 1800 and not state.get("warned"):
                state = {**state, "warned": True, "note": "RomM has not indexed this upload after 30 minutes. Check that its filesystem watcher is enabled, or run a scan of this platform in RomM."}
                self.store.update("artifacts", artifact_id, romm=state)
                self.engine.notify(artifact["project_id"], "needs_review", state["note"], artifactid=artifact_id, event_key="romm-wait:" + artifact_id)
        return state

    def poll(self):
        for artifact in self.store.all("artifacts"):
            if (artifact.get("romm") or {}).get("status") == "awaiting_scan":
                self.check(artifact["id"])

    def recover(self):
        """Uploads interrupted by a restart are marked failed so they can be retried."""
        for artifact in self.store.all("artifacts"):
            if (artifact.get("romm") or {}).get("status") == "uploading":
                self.store.update("artifacts", artifact["id"], romm={"status": "failed", "error": "Service restarted during the upload. Publish again.", "failed_at": now()})

