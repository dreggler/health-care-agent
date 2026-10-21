from __future__ import annotations

import asyncio
import io
import logging
import sys
import textwrap
import threading
import time
from typing import Any

from .base import BaseTool

logger = logging.getLogger(__name__)

_STDIO_LOCK = threading.Lock()


class CodeTool(BaseTool):

    name = "code"

    @property
    def is_concurrency_safe(self) -> bool:
        return False

    def __init__(self, data_store, memory_db_path, user_id: str) -> None:
        self.data_store = data_store
        self.memory_db_path = memory_db_path
        self.user_id = user_id
        self.memory_db_file = memory_db_path / f"{user_id}.db"

        from ...config import settings
        self._use_docker = getattr(settings, "CODE_TOOL_DOCKER_SANDBOX", False)
        self._docker_image = getattr(
            settings, "CODE_TOOL_DOCKER_IMAGE", "python:3.12-slim"
        )

        self._shell: Any = None
        self._init_lock = threading.Lock()

        self._df_high_watermark: str | None = None
        self._df_max_rowid: int | None = None
        self._df_last_updated_at: str | None = None
        self._df_retention_days: int = 14


    def _ensure_shell(self) -> None:
        if self._shell is not None:
            return
        with self._init_lock:
            if self._shell is not None:
                return

            from IPython.core.interactiveshell import InteractiveShell

            shell = InteractiveShell()
            shell.colors = "NoColor"

            from ...config import settings as _bootstrap_settings
            _local_tz_name = (_bootstrap_settings.TIMEZONE or "UTC").strip() or "UTC"
            bootstrap = textwrap.dedent(f"""\
                import pandas as pd
                import numpy as np
                from datetime import datetime, timedelta, timezone
                from zoneinfo import ZoneInfo as _ZoneInfo, ZoneInfoNotFoundError as _TZErr
                import sqlite3

                # Matplotlib (non-interactive backend for chart generation)
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                plt.rcParams['figure.figsize'] = (10, 6)
                plt.rcParams['figure.dpi'] = 100

                # Optional scientific libraries
                try:
                    import scipy
                    import scipy.stats as stats
                    import scipy.signal as signal
                except ImportError:
                    scipy = stats = signal = None
                try:
                    import statsmodels.api as sm
                except ImportError:
                    sm = None
                try:
                    import sklearn
                except ImportError:
                    sklearn = None

                # Pre-loaded health data — populated by refresh_df() before each cycle
                # columns: timestamp (datetime64[ns]), feature_type (str), value (float64)
                df = pd.DataFrame(columns=['timestamp', 'feature_type', 'value'])

                # Timezone helpers — DB stores UTC; user-facing display uses
                # settings.TIMEZONE. Use these when quoting wall-clock times.
                try:
                    _LOCAL_TZ = _ZoneInfo({_local_tz_name!r})
                except _TZErr:
                    _LOCAL_TZ = timezone.utc

                def to_local(ts):
                    \"\"\"Convert a UTC timestamp/string/datetime to user's local TZ.\"\"\"
                    t = pd.Timestamp(ts)
                    if t.tz is None:
                        t = t.tz_localize('UTC')
                    return t.tz_convert(_LOCAL_TZ)

                def local_now():
                    \"\"\"Current time as a tz-aware pd.Timestamp in user's local TZ.\"\"\"
                    return pd.Timestamp.now(tz=_LOCAL_TZ)
            """)

            result = shell.run_cell(bootstrap, silent=True, store_history=False)
            if result.error_in_exec:
                logger.error("Code tool bootstrap failed: %s", result.error_in_exec)

            try:
                import sqlite3 as _sq
                health_db_path = str(self.data_store.db_file)
                shell.user_ns["health_db"] = _sq.connect(
                    f"file:{health_db_path}?mode=ro", uri=True, check_same_thread=False
                )
                shell.user_ns["memory_db"] = _sq.connect(
                    f"file:{self.memory_db_file}?mode=ro", uri=True, check_same_thread=False
                )
                logger.info("Code tool: health_db and memory_db injected (both read-only)")
            except Exception as exc:
                logger.warning("Code tool: failed to inject DB connections: %s", exc)

            self._shell = shell
            logger.info("Code tool: IPython session initialized for %s", self.user_id)

    def reset_session(self) -> None:
        shell = self._shell
        self._shell = None
        self._df_high_watermark = None
        self._df_max_rowid = None
        if shell is not None:
            try:
                for name in ("health_db", "memory_db"):
                    conn = shell.user_ns.get(name)
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            except Exception as exc:
                logger.warning("Code tool: error closing DB connections during reset: %s", exc)

    def refresh_df(self, days: int = 14) -> None:
        self._ensure_shell()
        import pandas as _pd

        existing_df: _pd.DataFrame = self._shell.user_ns.get("df", _pd.DataFrame())
        cutoff = f"strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{days} days')"

        refresh_started_at = _pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%S")

        try:
            if self._df_max_rowid is not None and not existing_df.empty:
                cutoff_str = f"strftime('%Y-%m-%dT%H:%M:%S', 'now', '-{days} days')"

                sql_new = (
                    f"SELECT rowid, timestamp, feature_type, value FROM samples "
                    f"WHERE rowid > {self._df_max_rowid} AND timestamp >= {cutoff_str} "
                    f"ORDER BY rowid"
                )
                new_rows = self.data_store.query(sql_new)

                updated_rows = _pd.DataFrame()
                if self._df_last_updated_at:
                    try:
                        sql_updated = (
                            f"SELECT rowid, timestamp, feature_type, value FROM samples "
                            f"WHERE updated_at > '{self._df_last_updated_at}' "
                            f"AND rowid <= {self._df_max_rowid} "
                            f"AND timestamp >= {cutoff_str} "
                            f"ORDER BY rowid"
                        )
                        updated_rows = self.data_store.query(sql_updated)
                    except Exception:
                        pass

                if new_rows.empty and updated_rows.empty:
                    logger.debug("code: df incremental refresh — 0 new/updated rows")
                    return

                if not new_rows.empty:
                    max_new_rowid = int(new_rows["rowid"].max())
                    self._df_max_rowid = max_new_rowid

                if not updated_rows.empty:
                    updated_rows = updated_rows.drop(columns=["rowid"])
                    updated_rows["timestamp"] = _pd.to_datetime(
                        updated_rows["timestamp"], format="ISO8601"
                    )
                    update_keys = set(
                        zip(updated_rows["timestamp"], updated_rows["feature_type"], strict=False)
                    )
                    mask = existing_df.apply(
                        lambda r: (r["timestamp"], r["feature_type"]) not in update_keys,
                        axis=1,
                    )
                    existing_df = existing_df[mask]

                if not new_rows.empty:
                    new_rows = new_rows.drop(columns=["rowid"])
                    new_rows["timestamp"] = _pd.to_datetime(
                        new_rows["timestamp"], format="ISO8601"
                    )

                parts = [existing_df]
                if not new_rows.empty:
                    parts.append(new_rows)
                if not updated_rows.empty:
                    parts.append(updated_rows)
                combined = _pd.concat(parts, ignore_index=True)

                combined = combined.drop_duplicates(
                    subset=["timestamp", "feature_type"], keep="last"
                )

                cutoff_dt = _pd.Timestamp.now(tz="UTC") - _pd.Timedelta(days=days)
                if combined["timestamp"].dt.tz is None:
                    cutoff_dt = cutoff_dt.tz_localize(None)
                combined = combined[combined["timestamp"] >= cutoff_dt]

                self._shell.user_ns["df"] = combined
                self._df_high_watermark = str(combined["timestamp"].max())
                self._df_last_updated_at = refresh_started_at
                logger.info(
                    "code: df incremental refresh — +%d new, %d updated, %d total (last %d days)",
                    len(new_rows), len(updated_rows), len(combined), days,
                )
            else:
                sql = (
                    f"SELECT rowid, timestamp, feature_type, value FROM samples "
                    f"WHERE timestamp > {cutoff} "
                    f"ORDER BY rowid"
                )
                fresh_df = self.data_store.query(sql)
                if not fresh_df.empty:
                    self._df_max_rowid = int(fresh_df["rowid"].max())
                    fresh_df = fresh_df.drop(columns=["rowid"])
                    fresh_df["timestamp"] = _pd.to_datetime(
                        fresh_df["timestamp"], format="ISO8601"
                    )
                    fresh_df = fresh_df.drop_duplicates(subset=["timestamp", "feature_type"])
                    self._df_high_watermark = str(fresh_df["timestamp"].max())
                else:
                    self._df_max_rowid = None
                    self._df_high_watermark = None

                self._df_last_updated_at = refresh_started_at
                self._shell.user_ns["df"] = fresh_df if not fresh_df.empty else fresh_df
                self._df_retention_days = days
                logger.info(
                    "code: df full refresh — %d rows (last %d days)",
                    len(fresh_df), days,
                )
        except Exception as exc:
            logger.warning("code: df refresh failed: %s", exc)

    async def refresh_df_async(self, days: int = 14) -> None:
        await asyncio.to_thread(self.refresh_df, days)


    def get_definition(self) -> dict:
        return self._get_definition_from_json("code")


    async def warm_up(self) -> None:
        if self._shell is None:
            await asyncio.to_thread(self._ensure_shell)
        await self.refresh_df_async()

    async def execute(self, code: str) -> dict:
        self.report_progress({"status": "executing", "code_lines": code.count('\n') + 1})

        t0 = time.perf_counter()
        if self._use_docker:
            result = await asyncio.to_thread(self._run_in_docker, code)
        else:
            if self._shell is None:
                await asyncio.to_thread(self._ensure_shell)
            try:
                worker_thread_id: int | None = None
                original_run = self._run_in_shell

                def _tracked_run(code_str: str) -> dict[str, Any]:
                    nonlocal worker_thread_id
                    worker_thread_id = threading.get_ident()
                    return original_run(code_str)

                result = await asyncio.wait_for(
                    asyncio.to_thread(_tracked_run, code),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                if worker_thread_id is not None:
                    self._interrupt_thread(worker_thread_id)
                result = {"success": False, "error": "Code execution timed out (30s limit)."}
        elapsed = time.perf_counter() - t0
        logger.info("code: execution took %.2fs", elapsed)

        self.report_progress({
            "status": "done",
            "success": result.get("success", False),
            "has_output": bool(result.get("output")),
            "elapsed": f"{elapsed:.2f}s",
        })
        return result


    def _run_in_shell(self, code: str) -> dict[str, Any]:
        self._ensure_shell()

        code = self._sanitize_fstrings(code)

        try:
            stdout_buf = io.StringIO()
            stderr_buf = io.StringIO()
            with _STDIO_LOCK:
                old_stdout = sys.stdout
                old_stderr = sys.stderr
                sys.stdout = stdout_buf
                sys.stderr = stderr_buf
                try:
                    result = self._shell.run_cell(code, store_history=True)
                finally:
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr

            output = stdout_buf.getvalue().rstrip()
            errors = stderr_buf.getvalue().rstrip()

            exec_error = result.error_before_exec or result.error_in_exec
            if exec_error:
                df_info = self._dataframe_context()
                resp: dict[str, Any] = {
                    "success": False,
                    "error": str(exec_error) + df_info,
                    "error_type": type(exec_error).__name__,
                    "output": output,
                }
                if errors:
                    resp["errors"] = errors
                return resp

            df_info = self._dataframe_context()
            if df_info:
                output = output.rstrip() + "\n" + df_info

            resp = {
                "success": True,
                "output": output,
            }
            if errors:
                resp["errors"] = errors
            return resp

        except Exception as exc:
            df_info = self._dataframe_context()
            return {
                "success": False,
                "error": str(exc) + df_info,
                "error_type": type(exc).__name__,
                "output": "",
            }

    def _dataframe_context(self) -> str:
        if self._shell is None:
            return ""
        try:
            import pandas as _pd
            parts: list[str] = []
            for name, obj in self._shell.user_ns.items():
                if name.startswith("_"):
                    continue
                if isinstance(obj, _pd.DataFrame):
                    cols = list(obj.columns)
                    if len(str(cols)) > 200:
                        cols = cols[:5] + ["..."]
                    parts.append(f"  {name}: shape={obj.shape}, columns={cols}")
            if parts:
                return "\n[DataFrames in scope]\n" + "\n".join(parts)
        except Exception:
            pass
        return ""


    @staticmethod
    def _interrupt_thread(thread_id: int) -> None:
        import ctypes
        try:
            ret = ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(thread_id),
                ctypes.py_object(KeyboardInterrupt),
            )
            if ret == 0:
                logger.warning("code: thread %d not found for interrupt", thread_id)
            elif ret > 1:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(thread_id), None
                )
                logger.error("code: interrupt affected multiple threads, reverted")
            else:
                logger.info("code: interrupted stuck worker thread %d", thread_id)
        except Exception as exc:
            logger.warning("code: failed to interrupt thread %d: %s", thread_id, exc)

    @staticmethod
    def _sanitize_fstrings(code: str) -> str:
        import re

        def _replace_triple_fstring(match: re.Match) -> str:
            body = match.group(2)
            lines = [line.strip() for line in body.split("\n") if line.strip()]
            joined = "\\n".join(lines)
            return 'f"' + joined.replace('"', '\\"') + '"'

        pattern = r'f("""|\'\'\')(.*?)\1'
        return re.sub(pattern, _replace_triple_fstring, code, flags=re.DOTALL)


    def _run_in_docker(self, code: str) -> dict[str, Any]:
        import shutil
        import subprocess
        import tempfile

        if not shutil.which("docker"):
            logger.warning("Docker not found; falling back to IPython session.")
            return self._run_in_shell(code)

        preamble = textwrap.dedent("""
            import pandas as pd
            import numpy as np
            from datetime import datetime, timedelta
            import sqlite3
        """)
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        ) as f:
            f.write(preamble + "\n" + code)
            script_path = f.name

        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=64m",
            "--cpus", "0.5",
            "--memory", "256m",
            "--memory-swap", "256m",
            "-v", f"{script_path}:/script.py:ro",
            self._docker_image,
            "python", "/script.py",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "errors": result.stderr or None,
                **(
                    {"error": f"Process exited with code {result.returncode}"}
                    if result.returncode != 0 else {}
                ),
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Code execution timed out (30s limit)."}
        except Exception as exc:
            logger.error("Docker execution error: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}
        finally:
            import os
            try:
                os.unlink(script_path)
            except OSError:
                pass
