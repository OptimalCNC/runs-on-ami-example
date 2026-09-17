import signal
import sys

from .image import main


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("Build interrupted; temporary VM and build files were cleaned up.", file=sys.stderr)
        raise SystemExit(130)
