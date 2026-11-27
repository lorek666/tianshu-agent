"""Load diagnostics only in the real smoke test's Python subprocesses."""
from pathlib import Path


def pytest_runtest_call(item):
    if item.name != 'test_measure_slo_smoke_and_run_soak_real_subprocesses':
        return
    import os

    original = item.funcargs['monkeypatch']
    frozen = Path(item.config.rootpath)
    hook = Path(__file__).with_name('trace_startup.py')
    startup = frozen / 'tests' / '_startup' / 'sitecustomize.py'
    # Isolated checkout. Keep the existing test root overrides; add a trace
    # only when the measured Host, Daemon or its identity helper imports.
    assert startup.is_file()
    staged = Path(item.funcargs['tmp_path']) / 'startup_hook'
    staged.mkdir()
    suffix = '''

if os.environ.get("TIANSHU_STARTUP_TIMING_DIR") and sys.argv[:1]:
    if sys.argv[0] in ("-c", "-m") or sys.orig_argv[1:3] == ["-m", "tianshu"]:
        import trace_startup
'''
    (staged / 'sitecustomize.py').write_text(
        startup.read_text(encoding='utf-8') + suffix, encoding='utf-8',
    )
    (staged / hook.name).write_bytes(hook.read_bytes())
    original.setenv('PYTHONPATH', str(staged) + os.pathsep + os.environ.get('PYTHONPATH', ''))
