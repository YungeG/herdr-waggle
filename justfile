# All recipes stay inside this standalone task workbench directory.
set positional-arguments

default:
    @just --list

run *args:
    uv run --no-project --python python3 app.py "$@"

demo:
    uv run --no-project --python python3 app.py --demo

# Normal interactive Pi, not an unattended agent; no settings or approval changes.
pi directory *args:
    uv run --no-project --python python3 runtime.py pi "$@"

# Explicitly add a project-local loader for an already running Pi, then /reload.
install-bridge directory:
    uv run --no-project --python python3 runtime.py install "$@"

test *args:
    uv run --no-project --python python3 --with textual==8.2.8 python -m unittest -v "$@"
