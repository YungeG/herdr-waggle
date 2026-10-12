# All recipes stay inside this standalone task workbench directory.
set positional-arguments

default:
    @just --list

run *args:
    ./run.sh "$@"

demo:
    ./run.sh --demo

# Normal interactive Pi, not an unattended agent; no settings or approval changes.
pi directory *args:
    ./pi.sh "$@"

# Explicitly add a project-local loader for an already running Pi, then /reload.
install-bridge directory:
    uv run --no-project --python python3 runtime.py install "$@"

test *args:
    uv run --no-project --python python3 --with textual==8.2.8 python -m unittest -v "$@"

# Local archive only: current working tree, no commit/tag/push or Pi operations.
package *args:
    python3 release.py "$@"
