import argparse
import os
import signal
from pathlib import Path

from .engine import Engine
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="Portkeeper local preservation/build manager")
    parser.add_argument("--data-dir", type=Path, default=Path(".data"))
    parser.add_argument("--input-root", type=Path, help="Existing game files; recipes only read this directory")
    parser.add_argument("--library-root", type=Path, help="Publish selected packages into this RomM folder")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--allowed-host", action="append", dest="allowed_hosts",
                        default=os.environ.get("PORTKEEPER_ALLOWED_HOSTS", "").split(","),
                        help="Extra hostname or IP the browser may use, such as your server's LAN address (repeatable)")
    parser.add_argument("--enable-docker", action="store_true", help="Allow explicit recipes to run pinned Docker images")
    parser.add_argument("--enable-profile-builds", action="store_true", help="Allow reviewed profile extractor builds with the local C/C++ toolchain")
    args = parser.parse_args()
    engine = Engine(args.data_dir, args.input_root, args.library_root, args.enable_docker, enable_profile_builds=args.enable_profile_builds)
    def stop_on_term(signum, frame):
        raise KeyboardInterrupt
    previous_term = signal.signal(signal.SIGTERM, stop_on_term)
    try:
        server = make_server(engine, args.host, args.port, args.allowed_hosts)
        engine.start_scheduler()
        print(f"Portkeeper local preview: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Inputs: {engine.input_root}\nPublish folder: {engine.library_root}", flush=True)
        print("Local proof of concept; keep access on your trusted machine. Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        engine.close()
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()
