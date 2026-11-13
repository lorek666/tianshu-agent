"""Run test commands without project or provider configuration; never print env values."""

import os
import subprocess
import sys

clean = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith("TIANSHU_") and not k.endswith(("BASE_URL", "API_KEY"))
}
clean["PYTHONPATH"] = os.path.abspath("src")
command = sys.argv[1:]
print("COMMAND", " ".join(command), flush=True)
result = subprocess.run(command, env=clean)
print("EXIT", result.returncode, flush=True)
raise SystemExit(result.returncode)
